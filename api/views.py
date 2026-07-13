import os
import re
import json
import queue
import time
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.models import User
from django.conf import settings
from functools import wraps

from .models import Job, FollowedArtist
from .settings_store import read_settings, write_settings, read_public_settings, decrypt_secret, encrypt_secret
from .apple_api import search_catalog, get_album, get_song, get_artist, get_playlist, normalize_album, normalize_playlist
from .library_index import (
    scan_library_once, get_album_track_presence, invalidate_library_cache,
    get_music_root, to_rel, AUDIO_RE, make_album_key, make_song_key, strip_trailing_year
)
from .queue_manager import (
    enqueue_album, enqueue_song, enqueue_playlist, list_jobs, get_job, cancel_job,
    probe_wrapper_ports, register_listener, unregister_listener, emit_event, job_to_dict
)
from .apple_library_api import (
    get_my_storefront, fetch_library_page, get_library_playlist_detail, iterate_library
)
from .wrapper_login import (
    is_docker_reachable, start_wrapper_login, get_login_status, submit_2fa, cancel_login, get_hard_block, clear_hard_block
)

def api_require_auth(view_func):
    @csrf_exempt
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if getattr(settings, 'AUTH_DISABLED', False):
            return view_func(request, *args, **kwargs)
        if not request.user.is_authenticated:
            if not User.objects.exists():
                return JsonResponse({'error': 'auth not configured', 'needsSetup': True}, status=401)
            return JsonResponse({'error': 'Unauthorized'}, status=401)
        return view_func(request, *args, **kwargs)
    return wrapper

def get_json_body(request):
    try:
        return json.loads(request.body.decode('utf-8'))
    except Exception:
        return {}

# 1. Auth Views
@csrf_exempt
def auth_state_view(request):
    auth_disabled = getattr(settings, 'AUTH_DISABLED', False)
    password_set = auth_disabled or User.objects.exists()
    authed = auth_disabled or request.user.is_authenticated
    username = request.user.username if (authed and not auth_disabled) else None
    
    # Check wrapper container health
    wrapper_ok = False
    try:
        health = probe_wrapper_ports(timeout=1.0)
        wrapper_ok = health['ok']
    except Exception:
        pass
        
    return JsonResponse({
        'authDisabled': auth_disabled,
        'passwordSet': password_set,
        'authed': authed,
        'username': username,
        'wrapperStatus': {
            'ok': wrapper_ok,
            'reason': None if wrapper_ok else 'wrapper-offline'
        }
    })

@csrf_exempt
def auth_setup_view(request):
    if User.objects.exists():
        return JsonResponse({'error': 'Setup already completed'}, status=400)
        
    body = get_json_body(request)
    username = body.get('username', '').strip()
    password = body.get('password', '').strip()
    
    if len(username) < 3 or len(password) < 8:
        return JsonResponse({'error': 'Username must be >= 3 chars, password >= 8 chars'}, status=400)
        
    user = User.objects.create_superuser(username=username, password=password)
    login(request, user)
    return JsonResponse({
        'ok': True,
        'username': username
    })

@csrf_exempt
def auth_login_view(request):
    body = get_json_body(request)
    username = body.get('username', '').strip()
    password = body.get('password', '').strip()
    
    user = authenticate(request, username=username, password=password)
    if user is not None:
        login(request, user)
        return JsonResponse({
            'ok': True,
            'username': username
        })
    else:
        return JsonResponse({'error': 'Invalid credentials'}, status=401)

@csrf_exempt
def auth_logout_view(request):
    logout(request)
    return JsonResponse({'ok': True})

@api_require_auth
def auth_change_password_view(request):
    body = get_json_body(request)
    old_password = body.get('oldPassword', '')
    new_password = body.get('newPassword', '')
    
    user = request.user
    if not user.check_password(old_password):
        return JsonResponse({'error': 'Current password incorrect'}, status=400)
        
    if len(new_password) < 8:
        return JsonResponse({'error': 'New password must be >= 8 chars'}, status=400)
        
    user.set_password(new_password)
    user.save()
    update_session_auth_hash(request, user)
    return JsonResponse({'ok': True})

# 2. Health View
@api_require_auth
def health_view(request):
    wrapper_health = probe_wrapper_ports()
    music_root = getattr(settings, 'AMDL_MUSIC_PATH', '')
    music_path_ok = os.path.isdir(music_root)
    
    return JsonResponse({
        'ok': wrapper_health['ok'] and music_path_ok,
        'checks': {
            'wrapper': wrapper_health,
            'docker': {'ok': True},  # Docker communication active
            'musicPath': {
                'ok': music_path_ok,
                'path': music_root
            }
        }
    })

# 3. Settings Views
@api_require_auth
def settings_get_view(request):
    base = read_public_settings()
    base['hardBlockReason'] = get_hard_block()
    return JsonResponse(base)

@api_require_auth
def settings_post_view(request):
    body = get_json_body(request)
    
    # If password fields are passed as strings, encrypt them
    patch = {}
    for k, v in body.items():
        if k in ('applePassword', 'mediaUserToken', 'navidromePassword') and v:
            # We don't overwrite if it's the mask '••••'
            if v != '••••':
                from .settings_store import encrypt_secret
                patch[k] = encrypt_secret(v)
        else:
            patch[k] = v
            
    next_settings = write_settings(patch)
    return JsonResponse(read_public_settings())

def normalize_text_rating(val):
    return str(val or '').strip().lower()

def group_key_rating(album):
    return f"{normalize_text_rating(album.get('name'))}|{normalize_text_rating(album.get('artistName'))}"

def filter_albums_by_rating(albums, preference):
    if not albums:
        return []
    if preference == 'both':
        return albums
        
    pref = 'clean' if preference == 'clean' else 'explicit'
    groups = {}
    for album in albums:
        key = group_key_rating(album)
        existing = groups.get(key)
        if not existing:
            groups[key] = album
            continue
        existing_matches = (existing.get('contentRating') == pref)
        candidate_matches = (album.get('contentRating') == pref)
        if candidate_matches and not existing_matches:
            groups[key] = album
            
    kept_ids = {id(k) for k in groups.values()}
    return [a for a in albums if id(a) in kept_ids]

# 4. Search View
@api_require_auth
def search_view(request):
    term = request.GET.get('q', '').strip() or request.GET.get('term', '').strip()
    if not term:
        return JsonResponse({'albums': [], 'artists': [], 'songs': [], 'playlists': []})
        
    s = read_settings()
    storefront = request.GET.get('storefront') or s.get('storefront', 'us')
    limit = min(int(request.GET.get('limit', '25')), 50)
    offset = max(int(request.GET.get('offset', '0')), 0)
    types = request.GET.get('types', 'albums,artists,songs,playlists')
    
    media_user_token = decrypt_secret(s.get('mediaUserToken'))
    
    try:
        data = search_catalog(
            storefront=storefront,
            term=term,
            types=types,
            limit=limit,
            offset=offset,
            language=s.get('language', 'en-US'),
            media_user_token=media_user_token
        )
        
        r = data.get('results', {})
        
        artists = []
        for x in r.get('artists', {}).get('data', []):
            attr = x.get('attributes', {})
            artists.append({
                'id': x.get('id'),
                'type': x.get('type'),
                'name': attr.get('name'),
                'genreNames': attr.get('genreNames', []),
                'url': attr.get('url')
            })
            
        def resolve_artist_id(rel_id, artist_name):
            if rel_id:
                return rel_id
            for a in artists:
                if a['name'] == artist_name:
                    return a['id']
            return None
            
        albums = []
        for x in r.get('albums', {}).get('data', []):
            attr = x.get('attributes', {})
            rel_artists = x.get('relationships', {}).get('artists', {}).get('data', [])
            rel_artist_id = rel_artists[0].get('id') if rel_artists and isinstance(rel_artists, list) else None
            release_date = attr.get('releaseDate')
            year = str(release_date)[:4] if release_date else None
            
            albums.append({
                'id': x.get('id'),
                'type': x.get('type'),
                'name': attr.get('name'),
                'artistName': attr.get('artistName'),
                'artistId': resolve_artist_id(rel_artist_id, attr.get('artistName')),
                'releaseDate': release_date,
                'year': year,
                'trackCount': attr.get('trackCount'),
                'isSingle': attr.get('isSingle'),
                'contentRating': attr.get('contentRating'),
                'artworkTemplate': attr.get('artwork', {}).get('url') if attr.get('artwork') else None,
                'artworkColor': attr.get('artwork', {}).get('bgColor') if attr.get('artwork') else None,
                'url': attr.get('url')
            })
            
        songs = []
        for x in r.get('songs', {}).get('data', []):
            attr = x.get('attributes', {})
            song_url = attr.get('url', '')
            rel_artists = x.get('relationships', {}).get('artists', {}).get('data', [])
            rel_artist_id = rel_artists[0].get('id') if rel_artists and isinstance(rel_artists, list) else None
            
            # Match song URL to extract album ID
            m = re.search(r'/album/(?:[^/]+/)?(\d+)(?:\?|$)', song_url)
            album_id = m.group(1) if m else None
            
            songs.append({
                'id': x.get('id'),
                'type': x.get('type'),
                'name': attr.get('name'),
                'artistName': attr.get('artistName'),
                'artistId': resolve_artist_id(rel_artist_id, attr.get('artistName')),
                'albumName': attr.get('albumName'),
                'albumId': album_id,
                'durationMs': attr.get('durationInMillis'),
                'artworkTemplate': attr.get('artwork', {}).get('url') if attr.get('artwork') else None,
                'artworkColor': attr.get('artwork', {}).get('bgColor') if attr.get('artwork') else None,
                'url': song_url
            })
            
        filtered_albums = filter_albums_by_rating(albums, s.get('explicitFilter', 'explicit'))
        
        playlists = []
        for x in r.get('playlists', {}).get('data', []):
            attr = x.get('attributes', {})
            playlists.append({
                'id': x.get('id'),
                'type': x.get('type'),
                'name': attr.get('name'),
                'curatorName': attr.get('curatorName', 'Apple Music'),
                'trackCount': attr.get('trackCount'),
                'artworkTemplate': attr.get('artwork', {}).get('url') if attr.get('artwork') else None,
                'artworkColor': attr.get('artwork', {}).get('bgColor') if attr.get('artwork') else None,
                'url': attr.get('url'),
                'description': attr.get('description', {}).get('standard', '')
            })
            
        return JsonResponse({
            'albums': filtered_albums,
            'artists': artists,
            'songs': songs,
            'playlists': playlists,
            'storefront': storefront
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

# 5. Detail Views (Album, Artist, Playlist)
@api_require_auth
def album_detail_view(request, id):
    s = read_settings()
    storefront = request.GET.get('storefront') or s.get('storefront', 'us')
    try:
        raw = get_album(storefront, id, s.get('language', 'en-US'))
        data_list = raw.get('data', [])
        if not data_list:
            return JsonResponse({'error': 'Album not found'}, status=404)
            
        album = normalize_album(data_list[0])
        # Check track presence
        presence = get_album_track_presence(album['artistName'], album['name'], album['tracks'])
        album['presence'] = presence
        return JsonResponse({'album': album, 'storefront': storefront})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@api_require_auth
def artist_detail_view(request, id):
    s = read_settings()
    storefront = request.GET.get('storefront') or s.get('storefront', 'us')
    try:
        raw = get_artist(storefront, id, s.get('language', 'en-US'))
        artist_data = raw.get('data', [{}])[0]
        if not artist_data:
            return JsonResponse({'error': 'Artist not found'}, status=404)
            
        albums_raw = artist_data.get('relationships', {}).get('albums', {}).get('data', [])
        albums = []
        for item in albums_raw:
            attr = item.get('attributes', {})
            release_date = attr.get('releaseDate')
            year = str(release_date)[:4] if release_date else None
            albums.append({
                'id': item.get('id'),
                'type': item.get('type'),
                'name': attr.get('name'),
                'artistId': artist_data.get('id'),
                'artistName': attr.get('artistName') or artist_data.get('attributes', {}).get('name'),
                'releaseDate': release_date,
                'year': year,
                'trackCount': attr.get('trackCount'),
                'artworkTemplate': attr.get('artwork', {}).get('url') if attr.get('artwork') else None,
                'artworkColor': attr.get('artwork', {}).get('bgColor') if attr.get('artwork') else None,
                'isSingle': attr.get('isSingle'),
                'contentRating': attr.get('contentRating')
            })
            
        filtered_albums = filter_albums_by_rating(albums, s.get('explicitFilter', 'explicit'))
        
        artist = {
            'id': artist_data.get('id'),
            'name': artist_data.get('attributes', {}).get('name'),
            'genreNames': artist_data.get('attributes', {}).get('genreNames', []),
            'url': artist_data.get('attributes', {}).get('url'),
            'artworkTemplate': artist_data.get('attributes', {}).get('artwork', {}).get('url') if artist_data.get('attributes', {}).get('artwork') else None,
            'artworkColor': artist_data.get('attributes', {}).get('artwork', {}).get('bgColor') if artist_data.get('attributes', {}).get('artwork') else None,
        }
        
        return JsonResponse({
            'artist': artist,
            'albums': filtered_albums,
            'storefront': storefront
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@api_require_auth
def playlist_detail_view(request, id):
    s = read_settings()
    storefront = request.GET.get('storefront') or s.get('storefront', 'us')
    try:
        raw = get_playlist(storefront, id, s.get('language', 'en-US'))
        data_list = raw.get('data', [])
        if not data_list:
            return JsonResponse({'error': 'Playlist not found'}, status=404)
        playlist = normalize_playlist(data_list[0])
        return JsonResponse({'playlist': playlist, 'storefront': storefront})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@api_require_auth
def playlist_library_detail_view(request, library_id):
    s = read_settings()
    media_user_token = decrypt_secret(s.get('mediaUserToken'))
    if not media_user_token:
        return JsonResponse({'error': 'media-user-token not configured'}, status=412)
    try:
        playlist = get_library_playlist_detail(library_id, media_user_token, s.get('language', 'en-US'))
        if not playlist:
            return JsonResponse({'error': 'playlist not found'}, status=404)
        return JsonResponse({'playlist': playlist, 'storefront': s.get('storefront', 'us')})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

# 6. Download and Queue Control
@api_require_auth
def download_album_view(request):
    body = get_json_body(request)
    album_id = body.get('albumId')
    storefront = body.get('storefront', 'us')
    
    if not album_id:
        return JsonResponse({'error': 'albumId required'}, status=400)
        
    try:
        job = enqueue_album(album_id, storefront)
        return JsonResponse(job_to_dict(job))
    except Exception as e:
        code = getattr(e, 'code', None)
        status_code = 409 if code == 'ALREADY_IN_LIBRARY' else 500
        return JsonResponse({'error': str(e), 'code': code}, status=status_code)

@api_require_auth
def download_song_view(request):
    body = get_json_body(request)
    song_id = body.get('songId')
    storefront = body.get('storefront', 'us')
    
    if not song_id:
        return JsonResponse({'error': 'songId required'}, status=400)
        
    try:
        job = enqueue_song(song_id, storefront)
        return JsonResponse(job_to_dict(job))
    except Exception as e:
        code = getattr(e, 'code', None)
        status_code = 409 if code == 'ALREADY_IN_LIBRARY' else 500
        return JsonResponse({'error': str(e), 'code': code}, status=status_code)

@api_require_auth
def download_playlist_view(request):
    body = get_json_body(request)
    playlist_id = body.get('playlistId')
    storefront = body.get('storefront', 'us')
    name = body.get('name')
    
    if not playlist_id:
        return JsonResponse({'error': 'playlistId required'}, status=400)
        
    try:
        job = enqueue_playlist(playlist_id, storefront, name)
        return JsonResponse(job_to_dict(job))
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@api_require_auth
def queue_list_view(request):
    return JsonResponse({'jobs': list_jobs()})

@api_require_auth
def queue_cancel_view(request):
    body = get_json_body(request)
    job_id = body.get('id')
    if not job_id:
        return JsonResponse({'error': 'Job id required'}, status=400)
        
    res = cancel_job(job_id)
    if res['ok']:
        return JsonResponse({'ok': True})
    else:
        return JsonResponse({'error': res['message']}, status=400)

# 7. Library Index Views
@api_require_auth
def library_index_view(request):
    index = scan_library_once()
    return JsonResponse({
        'albums': index['albums'],
        'singles': index['singles'],
        'playlists': index['playlists']
    })

# 8. Followed Artists Views
@api_require_auth
def followed_artists_list_view(request):
    artists = [
        {
            'id': a.id,
            'name': a.name,
            'genreNames': a.genre_names,
            'url': a.url,
            'artworkTemplate': a.artwork_template,
            'artworkColor': a.artwork_color,
            'latestReleaseDate': a.latest_release_date,
            'lastCheckedAt': int(a.last_checked_at.timestamp() * 1000) if a.last_checked_at else None,
            'totalReleaseCount': a.total_release_count,
            'missingReleaseCount': a.missing_release_count,
            'releaseScope': a.release_scope
        }
        for a in FollowedArtist.objects.all().order_by('name')
    ]
    return JsonResponse({'artists': artists})

@api_require_auth
def follow_artist_view(request):
    body = get_json_body(request)
    artist_id = body.get('id')
    name = body.get('name')
    
    if not artist_id or not name:
        return JsonResponse({'error': 'Artist id and name required'}, status=400)
        
    s = read_settings()
    storefront = body.get('storefront', s.get('storefront', 'us'))
    scope = body.get('releaseScope', 'all')
    
    # Try fetching artist details from store to populate
    artwork_template = None
    artwork_color = None
    genres = []
    try:
        raw = get_artist(storefront, artist_id, s.get('language', 'en-US'))
        artist_data = raw.get('data', [{}])[0]
        attr = artist_data.get('attributes', {})
        genres = attr.get('genreNames', [])
        artwork_template = attr.get('artwork', {}).get('url')
        artwork_color = attr.get('artwork', {}).get('bgColor')
    except Exception:
        pass
        
    artist, created = FollowedArtist.objects.get_or_create(
        id=artist_id,
        defaults={
            'name': name,
            'genre_names': genres,
            'url': f"https://music.apple.com/{storefront}/artist/{artist_id}",
            'artwork_template': artwork_template,
            'artwork_color': artwork_color,
            'release_scope': scope
        }
    )
    
    if not created:
        artist.release_scope = scope
        artist.save()
        
    return JsonResponse({'ok': True})

@api_require_auth
def unfollow_artist_view(request, id):
    try:
        artist = FollowedArtist.objects.get(id=id)
        artist.delete()
        return JsonResponse({'ok': True})
    except FollowedArtist.DoesNotExist:
        return JsonResponse({'error': 'Artist not found'}, status=404)

@api_require_auth
def artist_release_check_view(request, id):
    # Trigger auto-download check for followed artist (async execution or manual call)
    # E.g. we can check immediately
    from .auto_downloads import check_followed_artist_now
    try:
        artist = FollowedArtist.objects.get(id=id)
        result = check_followed_artist_now(artist)
        return JsonResponse({'ok': True, 'result': result})
    except FollowedArtist.DoesNotExist:
        return JsonResponse({'error': 'Artist not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

# 9. Cloud Library Views
@api_require_auth
def cloud_library_health_view(request):
    s = read_settings()
    media_user_token = decrypt_secret(s.get('mediaUserToken'))
    if not media_user_token:
        return JsonResponse({'available': False, 'reason': 'no-media-user-token'})
        
    try:
        sf = get_my_storefront(media_user_token, s.get('language', 'en-US'))
        return JsonResponse({'available': True, 'storefront': sf})
    except Exception as e:
        return JsonResponse({'available': False, 'reason': 'probe-failed', 'error': str(e)})

@api_require_auth
def cloud_library_items_view(request):
    path = request.path.rstrip('/')
    if path.endswith('albums'):
        kind = 'albums'
    elif path.endswith('playlists'):
        kind = 'playlists'
    elif path.endswith('songs'):
        kind = 'songs'
    else:
        kind = request.GET.get('kind', 'albums')
        
    if kind not in ('albums', 'playlists', 'songs'):
        return JsonResponse({'error': 'kind must be albums, playlists, or songs'}, status=400)
        
    s = read_settings()
    media_user_token = decrypt_secret(s.get('mediaUserToken'))
    if not media_user_token:
        return JsonResponse({'error': 'media-user-token not configured'}, status=412)
        
    offset = int(request.GET.get('offset', '0'))
    limit = int(request.GET.get('limit', '100'))
    
    try:
        page = fetch_library_page(
            kind=kind,
            media_user_token=media_user_token,
            language=s.get('language', 'en-US'),
            offset=offset,
            limit=limit,
            storefront=s.get('storefront', 'us')
        )
        return JsonResponse(page)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@api_require_auth
def cloud_library_playlist_detail_view(request, id):
    s = read_settings()
    media_user_token = decrypt_secret(s.get('mediaUserToken'))
    if not media_user_token:
        return JsonResponse({'error': 'media-user-token not configured'}, status=412)
        
    try:
        detail = get_library_playlist_detail(id, media_user_token, s.get('language', 'en-US'))
        if not detail:
            return JsonResponse({'error': 'Playlist detail not found'}, status=404)
        return JsonResponse(detail)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

# 10. Server-Sent Events View
@api_require_auth
def events_view(request):
    q = register_listener()
    
    def event_generator():
        # Keep-alive intervals
        yield "retry: 10000\n\n"
        yield ": connected\n\n"
        while True:
            try:
                try:
                    ev = q.get(timeout=20.0)
                    yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'])}\n\n"
                except queue.Empty:
                    yield ": hb\n\n"
            except GeneratorExit:
                unregister_listener(q)
                break
            except Exception:
                unregister_listener(q)
                break
                
    response = StreamingHttpResponse(event_generator(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache, no-transform'
    response['X-Accel-Buffering'] = 'no'
    return response

# 11. Wrapper Credentials and Token Views
@api_require_auth
@csrf_exempt
def settings_apple_credentials_post(request):
    body = get_json_body(request)
    email = body.get('email', '').strip()
    password = body.get('password', '').strip()
    auto_login = body.get('autoLogin', True)
    
    if not email or not password:
        return JsonResponse({'error': 'email and password required'}, status=400)
        
    write_settings({
        'appleEmail': encrypt_secret(email),
        'applePassword': encrypt_secret(password),
    })
    clear_hard_block()
    
    if not auto_login:
        return JsonResponse({'ok': True, 'loginStarted': False})
        
    docker_ok = is_docker_reachable()
    if not docker_ok:
        return JsonResponse({
            'ok': True,
            'loginStarted': False,
            'loginError': 'Docker socket not available in the web container — run first-time login from the host (see README).'
        })
        
    start_wrapper_login(email, password)
    return JsonResponse({'ok': True, 'loginStarted': True})

@api_require_auth
@csrf_exempt
def settings_apple_credentials_login_post(request):
    docker_ok = is_docker_reachable()
    if not docker_ok:
        return JsonResponse({'error': 'Docker socket not available — mount /var/run/docker.sock into the web container'}, status=503)
        
    s = read_settings()
    email = decrypt_secret(s.get('appleEmail'))
    password = decrypt_secret(s.get('applePassword'))
    
    if not email or not password:
        return JsonResponse({'error': 'no credentials stored'}, status=400)
        
    start_wrapper_login(email, password)
    return JsonResponse({'ok': True, 'loginStarted': True})

@api_require_auth
def settings_apple_credentials_login_status_get(request):
    return JsonResponse(get_login_status())

@api_require_auth
@csrf_exempt
def settings_apple_credentials_2fa_post(request):
    body = get_json_body(request)
    code = body.get('code', '').strip()
    if not code:
        return JsonResponse({'error': 'code required'}, status=400)
    submit_2fa(code)
    return JsonResponse({'ok': True})

@api_require_auth
@csrf_exempt
def settings_apple_credentials_cancel_login_post(request):
    cancel_login()
    return JsonResponse({'ok': True})

@api_require_auth
@csrf_exempt
def settings_apple_credentials_delete(request):
    write_settings({
        'appleEmail': None,
        'applePassword': None,
    })
    return JsonResponse({'ok': True})

@api_require_auth
@csrf_exempt
def settings_media_user_token_post(request):
    body = get_json_body(request)
    token = body.get('token', '').strip()
    if not token:
        return JsonResponse({'error': 'token required'}, status=400)
    write_settings({
        'mediaUserToken': encrypt_secret(token)
    })
    return JsonResponse({'ok': True})

@api_require_auth
@csrf_exempt
def settings_media_user_token_delete(request):
    write_settings({
        'mediaUserToken': None
    })
    return JsonResponse({'ok': True})

# Dispatch Views for settings routes
@api_require_auth
@csrf_exempt
def settings_view(request):
    if request.method in ('PUT', 'POST'):
        return settings_post_view(request)
    return settings_get_view(request)

@api_require_auth
@csrf_exempt
def settings_apple_credentials_view(request):
    if request.method == 'DELETE':
        return settings_apple_credentials_delete(request)
    return settings_apple_credentials_post(request)

@api_require_auth
@csrf_exempt
def settings_media_user_token_view(request):
    if request.method == 'DELETE':
        return settings_media_user_token_delete(request)
    return settings_media_user_token_post(request)

# 12. Direct Download Cancels and Deletions
@api_require_auth
@csrf_exempt
def download_cancel_path_view(request, id):
    if request.method == 'DELETE':
        res = cancel_job(id)
        if res['ok']:
            return JsonResponse({'ok': True})
        else:
            return JsonResponse({'error': res['message']}, status=400)
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@api_require_auth
@csrf_exempt
def download_cancel_all_view(request):
    if request.method == 'POST':
        # Cancel all active/pending jobs
        jobs = Job.objects.filter(status__in=('pending', 'queued', 'downloading', 'transcoding', 'moving'))
        count = 0
        for j in jobs:
            res = cancel_job(j.id)
            if res['ok']:
                count += 1
        return JsonResponse({'ok': True, 'cancelled': count})
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@api_require_auth
@csrf_exempt
def cloud_library_download_all_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    body = get_json_body(request)
    kind = body.get('kind', '')
    if kind not in ('albums', 'playlists', 'songs'):
        return JsonResponse({'error': 'kind must be albums, playlists, or songs'}, status=400)
        
    s = read_settings()
    media_user_token = decrypt_secret(s.get('mediaUserToken'))
    if not media_user_token:
        return JsonResponse({'error': 'media-user-token not configured'}, status=412)
        
    storefront = s.get('storefront', 'us')
    quality = body.get('quality') or s.get('quality', 'flac')
    
    # We do a simplified iteration and enqueuing in a worker thread or synchronously
    scanned = 0
    queued = 0
    skipped_existing = 0
    unsupported = 0
    errors = []
    
    emit_event('cloud-library.download-all.progress', {
        'kind': kind,
        'scanned': 0,
        'queued': 0,
        'total': None,
        'done': False
    })
    
    try:
        for item in iterate_library(kind, media_user_token, s.get('language', 'en-US'), storefront):
            scanned += 1
            catalog_id = item.get('catalogId')
            library_id = item.get('libraryId')
            
            enqueueable = library_id if kind == 'playlists' else (catalog_id and item.get('downloadable'))
            if not enqueueable:
                unsupported += 1
                continue
                
            try:
                from .queue_manager import enqueue_album, enqueue_song, enqueue_playlist
                if kind == 'playlists':
                    job = enqueue_playlist(library_id, storefront, item.get('name'))
                elif kind == 'albums':
                    job = enqueue_album(catalog_id, storefront)
                elif kind == 'songs':
                    job = enqueue_song(catalog_id, storefront)
                    
                if job:
                    if job.status in ('queued', 'pending', 'downloading'):
                        queued += 1
                    else:
                        unsupported += 1
            except Exception as err:
                code = getattr(err, 'code', None)
                if code == 'ALREADY_IN_LIBRARY':
                    skipped_existing += 1
                else:
                    errors.append({'libraryId': library_id, 'name': item.get('name'), 'error': str(err)})
                    
            if scanned % 5 == 0:
                emit_event('cloud-library.download-all.progress', {
                    'kind': kind,
                    'scanned': scanned,
                    'queued': queued,
                    'skippedExisting': skipped_existing,
                    'unsupported': unsupported,
                    'done': False
                })
                
        emit_event('cloud-library.download-all.progress', {
            'kind': kind,
            'scanned': scanned,
            'queued': queued,
            'skippedExisting': skipped_existing,
            'unsupported': unsupported,
            'done': True
        })
        
        return JsonResponse({
            'ok': True,
            'kind': kind,
            'scanned': scanned,
            'queued': queued,
            'skippedExisting': skipped_existing,
            'skippedQueued': 0,
            'unsupported': unsupported,
            'errorCount': len(errors),
            'errors': errors[:20]
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@api_require_auth
@csrf_exempt
def library_presence_view(request):
    body = get_json_body(request)
    album_checks = body.get('albums', [])
    song_checks = body.get('songs', [])
    playlist_checks = body.get('playlists', [])
    album_track_checks = body.get('albumTracks', [])
    
    index = scan_library_once()
    
    albums = {}
    for item in album_checks:
        id_ = str(item.get('id', ''))
        if not id_:
            continue
        artist_name = str(item.get('artistName', ''))
        album_name = strip_trailing_year(str(item.get('albumName', '')))
        key = make_album_key(artist_name, album_name)
        albums[id_] = key in index['albumKeys']
        
    songs = {}
    for item in song_checks:
        id_ = str(item.get('id', ''))
        if not id_:
            continue
        artist_name = str(item.get('artistName', ''))
        song_name = str(item.get('songName', ''))
        key = make_song_key(artist_name, song_name)
        songs[id_] = key in index['songKeys']
        
    playlists = {}
    for item in playlist_checks:
        id_ = str(item.get('id', ''))
        if not id_:
            continue
        playlists[id_] = id_ in index['playlistIds']
        
    album_tracks = {}
    for item in album_track_checks:
        id_ = str(item.get('id', ''))
        if not id_:
            continue
        artist_name = str(item.get('artistName', ''))
        album_name = strip_trailing_year(str(item.get('albumName', '')))
        tracks = item.get('tracks', [])
        album_tracks[id_] = get_album_track_presence(artist_name, album_name, tracks, index)
        
    return JsonResponse({
        'albums': albums,
        'songs': songs,
        'playlists': playlists,
        'albumTracks': album_tracks
    })

def rel_parts(rel_path):
    return [p.strip() for p in re.split(r'[\\/]+', str(rel_path)) if p.strip()]

def resolve_under_music_root(rel_path):
    music_root = get_music_root()
    parts = rel_parts(rel_path)
    if not parts or any(p.startswith('.') for p in parts):
        raise Exception('invalid path')
    abs_path = os.path.abspath(os.path.join(music_root, *parts))
    if not abs_path.startswith(music_root):
        raise Exception('out of music root')
    return abs_path

@api_require_auth
@csrf_exempt
def library_delete_song_view(request):
    if request.method != 'DELETE':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    body = get_json_body(request)
    rel_path = body.get('relPath', '')
    parts = rel_parts(rel_path)
    if len(parts) != 3 or parts[1].lower() != 'singles':
        return JsonResponse({'error': 'invalid song path'}, status=400)
    if any(p.startswith('.') for p in parts):
        return JsonResponse({'error': 'invalid song path'}, status=400)
    if not AUDIO_RE.search(parts[2]):
        return JsonResponse({'error': 'audio file required'}, status=400)
        
    try:
        abs_path = resolve_under_music_root(rel_path)
        if not os.path.isfile(abs_path):
            return JsonResponse({'error': 'song not found'}, status=404)
            
        os.remove(abs_path)
        
        # Remove companion lrc
        base, _ = os.path.splitext(abs_path)
        lrc_path = base + '.lrc'
        removed_lyrics = False
        if os.path.isfile(lrc_path):
            try:
                os.remove(lrc_path)
                removed_lyrics = True
            except Exception:
                pass
                
        invalidate_library_cache()
        emit_event('library.changed', {
            'kind': 'song-deleted',
            'artistName': parts[0],
            'songName': os.path.splitext(parts[2])[0]
        })
        
        # Trigger Navidrome
        from .queue_manager import trigger_navidrome_scan
        trigger_navidrome_scan(read_settings())
        
        return JsonResponse({'ok': True, 'removedLyrics': removed_lyrics})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)

@api_require_auth
@csrf_exempt
def library_delete_playlist_view(request):
    if request.method != 'DELETE':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    body = get_json_body(request)
    rel_path = body.get('relPath', '')
    
    try:
        music_root = get_music_root()
        playlists_root = os.path.join(music_root, 'Playlists')
        parts = rel_parts(rel_path)
        if not parts or any(p.startswith('.') for p in parts):
            return JsonResponse({'error': 'invalid playlist path'}, status=400)
            
        abs_path = os.path.abspath(os.path.join(music_root, *parts))
        if not (abs_path == playlists_root or abs_path.startswith(playlists_root + os.path.sep)):
            return JsonResponse({'error': 'out of playlists directory'}, status=400)
        if not abs_path.lower().endswith('.m3u8'):
            return JsonResponse({'error': 'not an m3u8 file'}, status=400)
            
        if not os.path.isfile(abs_path):
            return JsonResponse({'error': 'playlist not found'}, status=404)
            
        os.remove(abs_path)
        
        # Remove artwork
        playlists_dir = os.path.dirname(abs_path)
        stem, _ = os.path.splitext(os.path.basename(abs_path))
        for ext in ['.jpg', '.jpeg', '.png', '.webp']:
            art_path = os.path.join(playlists_dir, f"{stem}{ext}")
            if os.path.isfile(art_path):
                try:
                    os.remove(art_path)
                except Exception:
                    pass
                    
        # Remove companion dir
        companion_dir = os.path.join(playlists_dir, stem)
        if os.path.isdir(companion_dir):
            try:
                import shutil
                shutil.rmtree(companion_dir)
            except Exception:
                pass
                
        invalidate_library_cache()
        emit_event('library.changed', {
            'kind': 'playlist-deleted',
            'relPath': to_rel(abs_path, music_root)
        })
        
        # Trigger Navidrome
        from .queue_manager import trigger_navidrome_scan
        trigger_navidrome_scan(read_settings())
        
        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)

@api_require_auth
@csrf_exempt
def library_delete_album_view(request):
    if request.method != 'DELETE':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    body = get_json_body(request)
    rel_path = body.get('relPath', '')
    parts = rel_parts(rel_path)
    if len(parts) != 2:
        return JsonResponse({'error': 'invalid album path'}, status=400)
    if any(p.startswith('.') for p in parts):
        return JsonResponse({'error': 'invalid album path'}, status=400)
    if parts[1].lower() == 'singles':
        return JsonResponse({'error': 'use song delete for singles'}, status=400)
        
    try:
        abs_path = resolve_under_music_root(rel_path)
        if not os.path.isdir(abs_path):
            return JsonResponse({'error': 'album not found'}, status=404)
            
        import shutil
        shutil.rmtree(abs_path)
        invalidate_library_cache()
        emit_event('library.changed', {
            'kind': 'album-deleted',
            'artistName': parts[0],
            'albumName': parts[1]
        })
        
        # Trigger Navidrome
        from .queue_manager import trigger_navidrome_scan
        trigger_navidrome_scan(read_settings())
        
        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)
