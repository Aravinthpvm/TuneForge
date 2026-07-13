import os
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
from .library_index import scan_library_once, get_album_track_presence, invalidate_library_cache
from .queue_manager import (
    enqueue_album, enqueue_song, enqueue_playlist, list_jobs, get_job, cancel_job,
    probe_wrapper_ports, register_listener, unregister_listener, emit_event
)
from .apple_library_api import get_my_storefront, fetch_library_page, get_library_playlist_detail
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

# 4. Search View
@api_require_auth
def search_view(request):
    term = request.GET.get('term', '')
    storefront = request.GET.get('storefront', 'us')
    limit = int(request.GET.get('limit', '25'))
    offset = int(request.GET.get('offset', '0'))
    
    s = read_settings()
    media_user_token = decrypt_secret(s.get('mediaUserToken'))
    
    try:
        results = search_catalog(
            storefront=storefront,
            term=term,
            limit=limit,
            offset=offset,
            language=s.get('language', 'en-US'),
            media_user_token=media_user_token
        )
        return JsonResponse(results)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

# 5. Detail Views (Album, Artist, Playlist)
@api_require_auth
def album_detail_view(request, id):
    s = read_settings()
    storefront = request.GET.get('storefront', s.get('storefront', 'us'))
    try:
        raw = get_album(storefront, id, s.get('language', 'en-US'))
        data_list = raw.get('data', [])
        if not data_list:
            return JsonResponse({'error': 'Album not found'}, status=404)
            
        album = normalize_album(data_list[0])
        # Check track presence
        presence = get_album_track_presence(album['artistName'], album['name'], album['tracks'])
        album['presence'] = presence
        return JsonResponse(album)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@api_require_auth
def artist_detail_view(request, id):
    s = read_settings()
    storefront = request.GET.get('storefront', s.get('storefront', 'us'))
    try:
        raw = get_artist(storefront, id, s.get('language', 'en-US'))
        return JsonResponse(raw)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@api_require_auth
def playlist_detail_view(request, id):
    s = read_settings()
    storefront = request.GET.get('storefront', s.get('storefront', 'us'))
    try:
        raw = get_playlist(storefront, id, s.get('language', 'en-US'))
        data_list = raw.get('data', [])
        if not data_list:
            return JsonResponse({'error': 'Playlist not found'}, status=404)
        return JsonResponse(normalize_playlist(data_list[0]))
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
