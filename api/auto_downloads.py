import os
import re
import time
import threading
from datetime import datetime
from django.utils import timezone
from django.conf import settings
from .models import FollowedArtist
from .settings_store import read_settings
from .apple_api import get_artist, normalize_album
from .library_index import scan_library_once, make_album_key, strip_trailing_year
from .queue_manager import enqueue_album, emit_event

# Auto-download interval configurations
HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS

FIXED_INTERVAL_MS = {
    '1h': HOUR_MS,
    '6h': 6 * HOUR_MS,
    '12h': 12 * HOUR_MS,
    'daily': DAY_MS,
    'weekly': 7 * DAY_MS,
}

DAILY_BUDGET = max(50, int(os.environ.get('AMDL_FOLLOW_DAILY_BUDGET', '300')))
MIN_AUTO_INTERVAL = max(60000, int(os.environ.get('AMDL_FOLLOW_MIN_INTERVAL_MS', str(30 * 60 * 1000))))
MAX_AUTO_INTERVAL = max(MIN_AUTO_INTERVAL, int(os.environ.get('AMDL_FOLLOW_MAX_INTERVAL_MS', str(7 * DAY_MS))))

def auto_interval_ms(followed_count):
    n = max(1, followed_count)
    raw = (n * DAY_MS) / DAILY_BUDGET
    return min(MAX_AUTO_INTERVAL, max(MIN_AUTO_INTERVAL, raw))

def resolve_interval_ms(frequency, followed_count):
    if frequency == 'auto':
        return auto_interval_ms(followed_count)
    return FIXED_INTERVAL_MS.get(frequency, DAY_MS)

_scheduler_thread = None

def filter_releases_by_scope(albums, scope):
    # scope: 'all', 'lp', 'ep', 'singles'
    if not scope or scope == 'all':
        return albums
    
    filtered = []
    for a in albums:
        is_single = a.get('isSingle', False)
        # Simplified determination of EP vs LP: usually track count
        track_count = a.get('trackCount', 1)
        
        if scope == 'singles':
            if is_single:
                filtered.append(a)
        elif scope == 'ep':
            if not is_single and 2 <= track_count <= 6:
                filtered.append(a)
        elif scope == 'lp':
            if not is_single and track_count > 6:
                filtered.append(a)
    return filtered

def check_followed_artist_now(artist):
    settings_data = read_settings()
    lib_index = scan_library_once()
    
    # Run check for single artist
    return check_followed_artist(artist, settings_data, lib_index)

def check_followed_artist(artist, settings_data, lib_index, followed_count=1):
    emit_event('following.check', {
        'phase': 'artist-started',
        'artistId': artist.id,
        'artistName': artist.name,
    })
    
    storefront = settings_data.get('storefront', 'us')
    language = settings_data.get('language', 'en-US')
    
    try:
        raw = get_artist(storefront, artist.id, language)
        data = raw.get('data', [{}])[0]
        attr = data.get('attributes', {})
        
        # Get albums relationship
        raw_albums = data.get('relationships', {}).get('albums', {}).get('data', [])
        albums = [normalize_album(raw_album) for raw_album in raw_albums if raw_album]
    except Exception as e:
        emit_event('following.check', {
            'phase': 'artist-completed',
            'artistId': artist.id,
            'artistName': artist.name,
            'error': f"Failed to fetch artist catalog: {e}",
            'discovered': 0,
            'queued': 0
        })
        return {'discovered': 0, 'queued': 0}
        
    scope = artist.release_scope or 'all'
    filtered_albums = filter_releases_by_scope(albums, scope)
    
    known = set(artist.known_release_ids or [])
    new_albums = [a for a in filtered_albums if a.get('id') and a['id'] not in known]
    
    successful_ids = set(artist.known_release_ids or [])
    queued = 0
    
    for album in new_albums:
        artist_name = album.get('artistName')
        album_name = album.get('name')
        
        # Check if already exists in local files
        if artist_name and album_name:
            album_key = make_album_key(artist_name, strip_trailing_year(album_name))
            if album_key in lib_index['albumKeys']:
                successful_ids.add(album['id'])
                continue
                
        # Queue the new album
        try:
            from .queue_manager import enqueue_album
            job = enqueue_album(album['id'], storefront, expected_artist_id=artist.id)
            queued += 1
            successful_ids.add(album['id'])
            
            emit_event('following.download', {
                'artistId': artist.id,
                'artistName': artist.name,
                'albumId': album['id'],
                'albumTitle': album_name,
                'jobId': job.id
            })
        except Exception as err:
            # If already running or in queue
            if getattr(err, 'code', None) == 'ALREADY_IN_LIBRARY':
                successful_ids.add(album['id'])
            else:
                emit_event('following.download', {
                    'artistId': artist.id,
                    'artistName': artist.name,
                    'albumId': album['id'],
                    'albumTitle': album_name,
                    'error': str(err)
                })
                
    # Update followed artist meta
    release_dates = [a.get('releaseDate') for a in filtered_albums if a.get('releaseDate')]
    release_dates.sort()
    latest_release_date = release_dates[-1] if release_dates else artist.latest_release_date
    
    # Calculate missing releases count
    missing_count = 0
    for album in filtered_albums:
        artist_name = album.get('artistName')
        album_name = album.get('name')
        if artist_name and album_name:
            album_key = make_album_key(artist_name, strip_trailing_year(album_name))
            if album_key not in lib_index['albumKeys']:
                missing_count += 1
                
    artist.name = attr.get('name', artist.name)
    artist.genre_names = attr.get('genreNames', artist.genre_names)
    artist.artwork_template = attr.get('artwork', {}).get('url') or artist.artwork_template
    artist.artwork_color = attr.get('artwork', {}).get('bgColor') or artist.artwork_color
    artist.known_release_ids = list(successful_ids)
    artist.latest_release_date = latest_release_date
    artist.last_checked_at = timezone.now()
    artist.total_release_count = len(filtered_albums)
    artist.missing_release_count = missing_count
    artist.save()
    
    emit_event('following.check', {
        'phase': 'artist-completed',
        'artistId': artist.id,
        'artistName': artist.name,
        'discovered': len(new_albums),
        'queued': queued
    })
    
    return {'discovered': len(new_albums), 'queued': queued}

def run_auto_download_check(force=False):
    s = read_settings()
    if not s.get('autoDownloadsEnabled') and not force:
        emit_event('following.check', {
            'phase': 'skipped',
            'reason': 'settings-disabled',
            'message': 'Auto-downloads are paused'
        })
        return {'ok': True, 'skipped': True}
        
    artists = FollowedArtist.objects.all()
    followed_count = artists.count()
    if followed_count == 0:
        return {'ok': True, 'artists': 0, 'queued': 0, 'discovered': 0}
        
    interval_ms = resolve_interval_ms(s.get('autoDownloadCheckFrequency', 'auto'), followed_count)
    now = timezone.now()
    
    # Find due artists
    due_artists = []
    for artist in artists:
        if force or not artist.last_checked_at:
            due_artists.append(artist)
        else:
            diff_ms = (now - artist.last_checked_at).total_seconds() * 1000
            if diff_ms >= interval_ms:
                due_artists.append(artist)
                
    # Limit check concurrency per tick (max 6) to stay under API limits
    due_artists.sort(key=lambda x: x.last_checked_at or datetime.min.replace(tzinfo=timezone.utc))
    check_limit = 6 if not force else len(due_artists)
    due_artists = due_artists[:check_limit]
    
    if not due_artists:
        return {'ok': True, 'artists': 0, 'queued': 0, 'discovered': 0}
        
    emit_event('following.check', {
        'phase': 'started',
        'reason': 'scheduled' if not force else 'manual',
        'artists': len(due_artists),
        'totalArtists': followed_count,
        'deferred': max(0, followed_count - len(due_artists))
    })
    
    lib_index = scan_library_once()
    queued = 0
    discovered = 0
    
    for artist in due_artists:
        res = check_followed_artist(artist, s, lib_index, followed_count)
        queued += res['queued']
        discovered += res['discovered']
        
    emit_event('following.check', {
        'phase': 'completed',
        'reason': 'scheduled' if not force else 'manual',
        'artists': len(due_artists),
        'queued': queued,
        'discovered': discovered
    })
    
    return {'ok': True, 'artists': len(due_artists), 'queued': queued, 'discovered': discovered}

def auto_downloads_scheduler_loop():
    # Run once on start
    try:
        run_auto_download_check(force=False)
    except Exception as e:
        print(f"Initial scheduler check failed: {e}")
        
    # Check frequency: tick loop runs every 5 minutes
    TICK_INTERVAL_SECONDS = max(60, int(os.environ.get('AMDL_FOLLOW_TICK_MS', str(5 * 60 * 1000))) // 1000)
    
    while True:
        time.sleep(TICK_INTERVAL_SECONDS)
        try:
            run_auto_download_check(force=False)
        except Exception as e:
            print(f"Periodic scheduler check failed: {e}")

def start_auto_download_scheduler():
    global _scheduler_thread
    if _scheduler_thread is not None:
        return
        
    _scheduler_thread = threading.Thread(target=auto_downloads_scheduler_loop, daemon=True)
    _scheduler_thread.start()
