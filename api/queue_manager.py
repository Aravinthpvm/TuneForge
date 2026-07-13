import os
import re
import uuid
import time
import socket
import threading
import queue
import shutil
from django.conf import settings
from .models import Job, AppSetting
from .apple_api import get_album, get_song, get_playlist, normalize_album, normalize_playlist, artwork_url
from .library_index import make_album_key, make_song_key, scan_library_once, invalidate_library_cache, strip_trailing_year, has_album_in_library, has_song_in_library, purge_playlist_exports_sharing_ids
from .folder_layout import compute_final_dir, ensure_dir, merge_move, sanitize_segment, apply_naming_convention
from .downloader_runner import write_amdp_config, spawn_amdp
from .transcoder import convert_dir_to_flac, extract_folder_art
from .settings_store import read_settings

# Global SSE listeners
_listeners = []
_listeners_lock = threading.Lock()

def emit_event(event_type, data):
    payload = {'event': event_type, 'data': data}
    with _listeners_lock:
        for q in _listeners:
            try:
                q.put(payload)
            except Exception:
                pass

def register_listener():
    q = queue.Queue()
    with _listeners_lock:
        _listeners.append(q)
    return q

def unregister_listener(q):
    with _listeners_lock:
        if q in _listeners:
            _listeners.remove(q)

# Global queue state
_job_queue = queue.Queue()
_running_processes = {}  # job_id -> subprocess.Popen
_running_processes_lock = threading.Lock()
_worker_thread = None

STAGING_ROOT_OUTSIDE = '/tmp/alacarte-staging'
STAGING_ROOT_INSIDE = None  # Will be configured as music_root / .amdl-tmp

FATAL_DOWNLOAD_PATTERNS = [
    re.compile(r'invalid CKC', re.IGNORECASE),
    re.compile(r'CKC.*error', re.IGNORECASE),
    re.compile(r'failed to get CKC', re.IGNORECASE),
    re.compile(r'decryption failed', re.IGNORECASE),
    re.compile(r'decrypt.*error', re.IGNORECASE),
    re.compile(r'license.*error', re.IGNORECASE),
    re.compile(r'DRM.*error', re.IGNORECASE),
]

def get_wrapper_host_ports():
    return {
        'host': os.environ.get('AMDL_WRAPPER_HOST', 'alacarte-wrapper'),
        'decrypt': int(os.environ.get('AMDL_WRAPPER_DECRYPT_PORT', '10020')),
        'm3u8': int(os.environ.get('AMDL_WRAPPER_M3U8_PORT', '20020')),
        'account': int(os.environ.get('AMDL_WRAPPER_ACCOUNT_PORT', '30020')),
    }

def probe_tcp(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except Exception as e:
        return False, str(e)

def probe_wrapper_ports(timeout=1.5):
    w = get_wrapper_host_ports()
    results = {}
    failed = []
    
    for name in ['decrypt', 'm3u8', 'account']:
        port = w[name]
        ok, err = probe_tcp(w['host'], port, timeout)
        results[name] = {'port': port, 'ok': ok, 'error': err}
        if not ok:
            failed.append({'name': name, 'port': port, 'error': err})
            
    return {
        'ok': len(failed) == 0,
        'host': w['host'],
        'failedPorts': failed,
        'probes': results
    }

def job_to_dict(job):
    return {
        'id': job.id,
        'kind': job.kind,
        'status': job.status,
        'progress': job.progress,
        'albumId': job.album_id,
        'songId': job.song_id,
        'playlistId': job.playlist_id,
        'libraryPlaylistId': job.library_playlist_id,
        'albumTitle': job.album_title,
        'artist': job.artist,
        'artistId': job.artist_id,
        'artworkUrl': job.artwork_url,
        'currentTrack': job.current_track,
        'message': job.message,
        'error': job.error,
        'cancelled': job.cancelled,
        'createdAt': int(job.created_at.timestamp() * 1000) if job.created_at else 0,
        'updatedAt': int(job.updated_at.timestamp() * 1000) if job.updated_at else 0,
        'finalDir': job.final_dir,
        'stats': job.stats,
    }

def update_job(job_id, patch):
    try:
        job = Job.objects.get(id=job_id)
        for k, v in patch.items():
            setattr(job, k, v)
        job.save()
        emit_event('job.update', job_to_dict(job))
        return job
    except Job.DoesNotExist:
        return None

def list_jobs():
    return [job_to_dict(j) for j in Job.objects.all().order_by('-created_at')[:100]]

def get_job(job_id):
    try:
        j = Job.objects.get(id=job_id)
        return job_to_dict(j)
    except Job.DoesNotExist:
        return None

def cancel_job(job_id):
    try:
        job = Job.objects.get(id=job_id)
        if job.status in ('completed', 'failed', 'cancelled'):
            return {'ok': False, 'message': 'Job already completed'}
            
        update_job(job_id, {
            'cancelled': True,
            'status': 'cancelled',
            'message': 'Job cancelled by user'
        })
        
        # Terminate running process if any
        with _running_processes_lock:
            proc = _running_processes.get(job_id)
            if proc:
                try:
                    proc.terminate()
                    proc.kill()
                except Exception:
                    pass
        return {'ok': True}
    except Job.DoesNotExist:
        return {'ok': False, 'message': 'Job not found'}

def clamp01(val):
    try:
        f = float(val)
        return max(0.0, min(1.0, f))
    except Exception:
        return 0.0

def compute_progress_percent(p_state):
    download_total = p_state.get('downloadTotal', 1)
    download_done = p_state.get('downloadDone', 0)
    download_partial = p_state.get('downloadPartial', 0.0)
    
    download_done_units = min(
        download_total,
        max(0.0, download_done) + (download_partial if download_done < download_total else 0.0)
    )
    
    convert_enabled = p_state.get('convertEnabled', False)
    convert_total = p_state.get('convertTotal', 0)
    convert_done = p_state.get('convertDone', 0)
    
    convert_done_units = min(convert_total, max(0.0, convert_done)) if convert_enabled else 0.0
    finalize_done_units = clamp01(p_state.get('finalizeProgress', 0.0))
    
    total_units = max(
        1.0,
        float(download_total + (convert_total if convert_enabled else 0) + 1)
    )
    
    val = (download_done_units + convert_done_units + finalize_done_units) / total_units
    return max(0, min(100, int(round(val * 100))))

def apply_progress(job, progress_state, patch=None):
    if not patch:
        patch = {}
    patch['progress'] = compute_progress_percent(progress_state)
    update_job(job.id, patch)

def extract_bracket_title(line):
    m = re.search(r'\]\s*(.+?)(?:\s*\[|$)', line)
    return m.group(1).strip() if m else None

def handle_amdp_line(job, line, which, progress_state):
    matched_track_header = False
    
    track_header = re.match(r'^Track\s+(\d+)\s+of\s+(\d+)\s*:?\s*(.*)$', line, re.IGNORECASE)
    if track_header:
        current = int(track_header.group(1))
        total = int(track_header.group(2))
        if total > 0:
            matched_track_header = True
            progress_state['downloadTotal'] = total
            inferred_done = max(0, min(total, current - 1))
            progress_state['downloadDone'] = inferred_done
            progress_state['downloadPartial'] = 0.0
            if progress_state['convertEnabled'] and progress_state['convertDone'] == 0:
                progress_state['convertTotal'] = total
                
            job.stats['total'] = total
            job.stats['done'] = inferred_done
            
            title = track_header.group(3).strip()
            apply_progress(job, progress_state, {
                'current_track': title if title and not re.match(r'^(songs|music-videos)$', title, re.IGNORECASE) else job.current_track,
                'stats': job.stats
            })
            
    if not matched_track_header:
        bracketed = re.search(r'\[(\d+)/(\d+)\]', line)
        if bracketed:
            done = int(bracketed.group(1))
            total = int(bracketed.group(2))
            if total > 0:
                matched_track_header = True
                progress_state['downloadTotal'] = total
                progress_state['downloadDone'] = max(progress_state['downloadDone'], min(total, done))
                progress_state['downloadPartial'] = 0.0
                if progress_state['convertEnabled'] and progress_state['convertDone'] == 0:
                    progress_state['convertTotal'] = total
                    
                job.stats['total'] = total
                job.stats['done'] = progress_state['downloadDone']
                
                apply_progress(job, progress_state, {
                    'current_track': extract_bracket_title(line),
                    'stats': job.stats
                })
                
    if not matched_track_header:
        downloading = re.match(r'^Downloading\s+(\d+)\s*/\s*(\d+)\s*:\s*(.+)$', line, re.IGNORECASE)
        if downloading:
            current = int(downloading.group(1))
            total = int(downloading.group(2))
            if total > 0:
                matched_track_header = True
                progress_state['downloadTotal'] = total
                inferred_done = max(0, min(total, current - 1))
                progress_state['downloadDone'] = inferred_done
                progress_state['downloadPartial'] = 0.0
                if progress_state['convertEnabled'] and progress_state['convertDone'] == 0:
                    progress_state['convertTotal'] = total
                    
                job.stats['total'] = total
                job.stats['done'] = inferred_done
                
                apply_progress(job, progress_state, {
                    'current_track': downloading.group(3).strip() or job.current_track,
                    'stats': job.stats
                })
                
    if not matched_track_header:
        pct_match = re.search(r'(\d{1,3})\s*%', line)
        if pct_match:
            pct = max(0, min(100, int(pct_match.group(1))))
            if progress_state['downloadDone'] < progress_state['downloadTotal']:
                partial = pct / 100.0
                if partial > progress_state['downloadPartial']:
                    progress_state['downloadPartial'] = partial
                    apply_progress(job, progress_state)
                    
    if which == 'stderr' and re.search(r'error|failed|forbidden', line, re.IGNORECASE):
        job.stats['failed'] = job.stats.get('failed', 0) + 1
        
    # Append log line to DB
    job.logs += f"[{which.upper()}] {line}\n"
    job.save()
    emit_event('job.log', {'id': job.id, 'line': line, 'which': which})

def enqueue_album(album_id, storefront='us', expected_artist_id=None):
    # Check duplicate
    try:
        raw_meta = get_album(storefront, album_id)
        meta = normalize_album(raw_meta.get('data', [{}])[0])
    except Exception as e:
        raise Exception(f"Failed to fetch metadata for album {album_id}: {e}")
        
    if not meta:
        raise Exception(f"No metadata returned for album {album_id}")
        
    artist_name = meta['artistName']
    album_title = meta['name']
    
    lib_index = scan_library_once()
    if has_album_in_library(artist_name, album_title, lib_index):
        err = Exception(f"Album already in library: {artist_name} - {album_title}")
        err.code = 'ALREADY_IN_LIBRARY'
        raise err
        
    job_id = str(uuid.uuid4())
    job = Job.objects.create(
        id=job_id,
        kind='album',
        status='pending',
        progress=0,
        album_id=album_id,
        album_title=album_title,
        artist=artist_name,
        artist_id=meta.get('artistId') or expected_artist_id,
        artwork_url=artwork_url(meta['artworkTemplate'], 600),
        stats={'total': meta['trackCount'], 'done': 0, 'failed': 0}
    )
    
    _job_queue.put(job_id)
    emit_event('job.update', job_to_dict(job))
    return job

def enqueue_song(song_id, storefront='us', expected_album_id=None):
    try:
        raw_meta = get_song(storefront, song_id)
        meta = raw_meta.get('data', [{}])[0]
        attr = meta.get('attributes', {})
    except Exception as e:
        raise Exception(f"Failed to fetch metadata for song {song_id}: {e}")
        
    artist_name = attr.get('artistName', 'Unknown Artist')
    song_name = attr.get('name', 'Unknown Song')
    
    album_id = expected_album_id
    if not album_id:
        song_url = attr.get('url', '')
        m = re.search(r'/album/(?:[^/]+/)?(\d+)(?:\?|$)', song_url)
        album_id = m.group(1) if m else None
        
    lib_index = scan_library_once()
    if has_song_in_library(artist_name, song_name, lib_index):
        err = Exception(f"Song already in library: {artist_name} - {song_name}")
        err.code = 'ALREADY_IN_LIBRARY'
        raise err
        
    job_id = str(uuid.uuid4())
    job = Job.objects.create(
        id=job_id,
        kind='song',
        status='pending',
        progress=0,
        song_id=song_id,
        album_id=album_id,
        album_title=song_name,
        artist=artist_name,
        artwork_url=artwork_url(attr.get('artwork', {}).get('url'), 600),
        stats={'total': 1, 'done': 0, 'failed': 0}
    )
    
    _job_queue.put(job_id)
    emit_event('job.update', job_to_dict(job))
    return job

def enqueue_playlist(playlist_id, storefront='us', expected_playlist_name=None):
    try:
        raw_meta = get_playlist(storefront, playlist_id)
        meta = normalize_playlist(raw_meta.get('data', [{}])[0])
    except Exception as e:
        raise Exception(f"Failed to fetch metadata for playlist {playlist_id}: {e}")
        
    if not meta:
        raise Exception(f"No metadata returned for playlist {playlist_id}")
        
    playlist_name = meta['name'] or expected_playlist_name or 'Playlist'
    
    job_id = str(uuid.uuid4())
    job = Job.objects.create(
        id=job_id,
        kind='playlist',
        status='pending',
        progress=0,
        playlist_id=playlist_id,
        album_title=playlist_name,
        artist='Apple Music',
        artwork_url=artwork_url(meta['artworkTemplate'], 600),
        stats={'total': meta['trackCount'], 'done': 0, 'failed': 0}
    )
    
    _job_queue.put(job_id)
    emit_event('job.update', job_to_dict(job))
    return job

def run_job_execution(job_id):
    job = Job.objects.get(id=job_id)
    if job.cancelled:
        return
        
    update_job(job_id, {'status': 'downloading', 'message': 'Checking wrapper health'})
    
    # 1. Probe wrapper health
    health = probe_wrapper_ports()
    if not health['ok']:
        update_job(job_id, {
            'status': 'failed',
            'error': f"Decryption wrapper is not reachable. Failed ports: {health['failedPorts']}. Make sure the wrapper service is running."
        })
        return
        
    # 2. Setup Staging Roots
    s = read_settings()
    music_root = getattr(settings, 'AMDL_MUSIC_PATH', os.path.join(settings.BASE_DIR, 'music'))
    global STAGING_ROOT_INSIDE
    STAGING_ROOT_INSIDE = os.path.join(music_root, '.amdl-tmp')
    
    staging_root = STAGING_ROOT_INSIDE if s.get('stagingInsideMusicLibrary') else STAGING_ROOT_OUTSIDE
    job_staging = os.path.join(staging_root, job_id)
    os.makedirs(job_staging, exist_ok=True)
    
    progress_state = {
        'downloadTotal': job.stats.get('total', 1),
        'downloadDone': 0,
        'downloadPartial': 0.0,
        'convertEnabled': s.get('quality') == 'flac',
        'convertTotal': job.stats.get('total', 1) if s.get('quality') == 'flac' else 0,
        'convertDone': 0,
        'finalizeProgress': 0.0
    }
    
    try:
        # Write config.yaml in staging
        config_path = write_amdp_config(s, s.get('mediaUserToken'), job_staging)
        
        # Build Args
        is_song = (job.kind == 'song')
        quality = s.get('quality', 'flac')
        url_target = ""
        if job.kind == 'album':
            url_target = f"https://music.apple.com/{s.get('storefront', 'us')}/album/_/{job.album_id}"
        elif job.kind == 'song':
            url_target = f"https://music.apple.com/{s.get('storefront', 'us')}/album/_/{job.album_id}?i={job.song_id}"
        elif job.kind == 'playlist':
            url_target = f"https://music.apple.com/{s.get('storefront', 'us')}/playlist/_/{job.playlist_id}"
            
        args = []
        if is_song:
            args.append('--song')
        if quality == 'atmos':
            args.append('--atmos')
        elif quality == 'aac':
            args.append('--aac')
        args.append(url_target)
        
        # Run downloader
        proc, threads = spawn_amdp(args, job_staging, lambda line, which: handle_amdp_line(job, line, which, progress_state))
        
        with _running_processes_lock:
            _running_processes[job_id] = proc
            
        # Wait for download process
        exit_code = proc.wait()
        
        # Cleanup process reference
        with _running_processes_lock:
            if job_id in _running_processes:
                del _running_processes[job_id]
                
        # Join reader threads
        for t in threads:
            t.join(timeout=1.0)
            
        job.refresh_from_db()
        if job.cancelled:
            # Cleanup staging
            shutil.rmtree(job_staging, ignore_errors=True)
            return
            
        if exit_code != 0:
            raise Exception(f"Downloader process exited with code {exit_code}")
            
        # 3. Transcode ALAC to FLAC if enabled
        job.refresh_from_db()
        if progress_state['convertEnabled']:
            update_job(job_id, {'status': 'transcoding', 'message': 'Converting ALAC files to FLAC'})
            
            def convert_on_progress(file_path, idx, total):
                progress_state['convertDone'] = idx
                apply_progress(job, progress_state, {'message': f"Converting track {idx} of {total}"})
                
            convert_dir_to_flac(job_staging, delete_original=True, on_progress=convert_on_progress)
            
        # 4. Extract folder art
        update_job(job_id, {'message': 'Extracting folder art'})
        extract_folder_art(job_staging)
        
        # 5. Move files to music library
        update_job(job_id, {'status': 'moving', 'message': 'Moving files into music library'})
        
        # We need to determine the artist and album name from files inside staging,
        # or fall back to metadata.
        # Let's see: apple-music-dl saves files inside:
        # staging_root / artist_name / album_name / tracks...
        # Let's inspect directories inside job_staging:
        entries = os.listdir(job_staging)
        moved_anything = False
        
        for entry in entries:
            entry_path = os.path.join(job_staging, entry)
            if os.path.isdir(entry_path) and not entry.startswith('.'):
                # This directory is the ArtistName folder
                artist_name_cleaned = entry
                
                # Check for subdirectories inside Artist folder
                album_dirs = os.listdir(entry_path)
                for album_name in album_dirs:
                    album_path = os.path.join(entry_path, album_name)
                    if os.path.isdir(album_path) and not album_name.startswith('.'):
                        # This is the Album folder (or 'Singles' folder)
                        # We merge move this folder directly to the destination library
                        dest_artist_dir = resolve_artist_dir_case_insensitive(music_root, artist_name_cleaned)
                        dest_album_dir = os.path.join(music_root, dest_artist_dir, album_name)
                        
                        merge_move(album_path, dest_album_dir)
                        moved_anything = True
                        
        if not moved_anything:
            # Fallback if downloader saved files directly in staging
            dest_artist = resolve_artist_dir_case_insensitive(music_root, job.artist)
            dest_album = sanitize_segment(job.album_title)
            dest_dir = os.path.join(music_root, dest_artist, dest_album)
            merge_move(job_staging, dest_dir)
            
        # Invalidate Cache
        invalidate_library_cache()
        
        # 6. Navidrome Scan Trigger
        if s.get('navidromeEnabled'):
            update_job(job_id, {'message': 'Triggering Navidrome library scan'})
            trigger_navidrome_scan(s)
            
        update_job(job_id, {
            'status': 'completed',
            'progress': 100,
            'message': 'Completed successfully'
        })
        
    except Exception as err:
        update_job(job_id, {
            'status': 'failed',
            'error': str(err),
            'message': 'Failed'
        })
    finally:
        # Cleanup staging
        shutil.rmtree(job_staging, ignore_errors=True)

def resolve_artist_dir_case_insensitive(music_root, desired_artist):
    # Match resolveArtistDir in folderLayout.mjs
    desired = sanitize_segment(desired_artist)
    try:
        if os.path.exists(music_root):
            entries = os.listdir(music_root)
            lower = desired.lower()
            for entry in entries:
                if os.path.isdir(os.path.join(music_root, entry)) and entry.lower() == lower:
                    return entry
    except Exception:
        pass
    return desired

def trigger_navidrome_scan(s):
    # Triggers Navidrome scan via Subsonic API
    import urllib.request
    import urllib.parse
    import hashlib
    import secrets
    
    url = (s.get('navidromeUrl') or '').strip()
    user = (s.get('navidromeUser') or '').strip()
    pwd = (s.get('navidromePassword') or '').strip()
    if not url or not user or not pwd:
        return
        
    # Build subsonic request
    # /rest/startScan.view?u=user&t=token&s=salt&v=1.16.0&c=alacarte&f=json
    salt = secrets.token_hex(8)
    token_src = pwd + salt
    token = hashlib.md5(token_src.encode('utf-8')).hexdigest()
    
    params = {
        'u': user,
        't': token,
        's': salt,
        'v': '1.16.0',
        'c': 'alacarte',
        'f': 'json'
    }
    
    qs = urllib.parse.urlencode(params)
    scan_url = f"{url.rstrip('/')}/rest/startScan.view?{qs}"
    try:
        req = urllib.request.Request(scan_url)
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode('utf-8')
            print(f"Navidrome scan triggered: {body[:200]}")
    except Exception as e:
        print(f"Failed to trigger Navidrome scan: {e}")

def queue_worker_loop():
    print("[TuneForge Queue] Worker loop starting...")
    # Reset any interrupted 'downloading' or 'transcoding' jobs to 'pending'
    # so they get retried or listed correctly.
    try:
        updated = Job.objects.filter(status__in=('downloading', 'transcoding', 'moving')).update(status='pending', progress=0)
        if updated > 0:
            print(f"[TuneForge Queue] Reset {updated} interrupted jobs on startup.")
        # Load any pending jobs into memory queue
        pending_jobs = Job.objects.filter(status='pending').order_by('created_at')
        for job in pending_jobs:
            _job_queue.put(job.id)
            update_job(job.id, {'status': 'queued'})
            print(f"[TuneForge Queue] Queued pending job {job.id} ({job.album_title})")
    except Exception as e:
        print(f"Error resetting running jobs on startup: {e}")
        
    while True:
        try:
            job_id = _job_queue.get()
            print(f"[TuneForge Queue] Worker picked up job {job_id}")
            # Double check database
            try:
                job = Job.objects.get(id=job_id)
                if job.status == 'cancelled' or job.cancelled:
                    print(f"[TuneForge Queue] Job {job_id} was already cancelled. Skipping.")
                    _job_queue.task_done()
                    continue
                update_job(job_id, {'status': 'downloading'})
                print(f"[TuneForge Queue] Executing job {job_id} ({job.album_title})...")
                run_job_execution(job_id)
                print(f"[TuneForge Queue] Job {job_id} finished execution.")
            except Exception as e:
                print(f"Error running job {job_id}: {e}")
            finally:
                _job_queue.task_done()
        except Exception as e:
            print(f"Queue worker loop encountered error: {e}")
            time.sleep(2)

def init_queue():
    global _worker_thread
    if _worker_thread is not None:
        return
        
    # Start the worker thread
    _worker_thread = threading.Thread(target=queue_worker_loop, daemon=True)
    _worker_thread.start()
