import re
import time
import urllib.request
import urllib.parse
import json

BASE_URL = 'https://amp-api.music.apple.com/v1/catalog'
TOKEN_CACHE = {'token': None, 'expires_at': 0}
TTL_SECONDS = 25 * 60  # 25 minutes

def get_bearer_token():
    now = time.time()
    if TOKEN_CACHE['token'] and TOKEN_CACHE['expires_at'] > now:
        return TOKEN_CACHE['token']

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    # 1. Fetch main page to find index JS bundle
    req = urllib.request.Request('https://music.apple.com', headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        html = response.read().decode('utf-8')
        
    m = re.search(r'/assets/index~[^"\']+\.js', html)
    if not m:
        raise Exception('could not locate index~*.js bundle URL')
    
    # 2. Fetch JS bundle to extract token
    js_url = 'https://music.apple.com' + m.group(0)
    req_js = urllib.request.Request(js_url, headers=headers)
    with urllib.request.urlopen(req_js, timeout=10) as response:
        js = response.read().decode('utf-8')
        
    token_match = re.search(r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', js)
    if not token_match:
        raise Exception('no JWT token found in bundle')
        
    token = token_match.group(0)
    TOKEN_CACHE['token'] = token
    TOKEN_CACHE['expires_at'] = now + TTL_SECONDS
    return token

def invalidate_bearer_cache():
    TOKEN_CACHE['token'] = None
    TOKEN_CACHE['expires_at'] = 0

def api_get(url, language='', media_user_token=None):
    token = get_bearer_token()
    
    def run(t):
        headers = {
            'Authorization': f'Bearer {t}',
            'Origin': 'https://music.apple.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': language or 'en-US'
        }
        if media_user_token:
            headers['Music-User-Token'] = media_user_token
        
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                status = response.status
                body = response.read().decode('utf-8')
                return status, body
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8') if e.fp else ''
            return e.code, body

    status, body = run(token)
    if status in (401, 403):
        invalidate_bearer_cache()
        token = get_bearer_token()
        status, body = run(token)
        
    if status < 200 or status >= 300:
        parsed_url = urllib.parse.urlparse(url)
        raise Exception(f'Apple API {status} on {parsed_url.path}: {body[:200]}')
        
    return json.loads(body)

def search_catalog(storefront, term, types='albums,artists,songs,playlists', limit=25, offset=0, language='en-US', media_user_token=None):
    params = {
        'term': term,
        'types': types,
        'include': 'artists',
        'limit': str(limit),
        'offset': str(offset),
        'l': language
    }
    qs = urllib.parse.urlencode(params)
    url = f"{BASE_URL}/{urllib.parse.quote(storefront)}/search?{qs}"
    return api_get(url, language, media_user_token)

def get_album(storefront, id, language='en-US'):
    params = {
        'omit[resource]': 'autos',
        'include': 'tracks,artists,record-labels',
        'include[songs]': 'artists',
        'extend': 'editorialVideo,extendedAssetUrls',
        'l': language
    }
    qs = urllib.parse.urlencode(params)
    url = f"{BASE_URL}/{urllib.parse.quote(storefront)}/albums/{urllib.parse.quote(str(id))}?{qs}"
    return api_get(url, language)

def get_song(storefront, id, language='en-US'):
    params = {
        'include': 'albums,artists',
        'l': language
    }
    qs = urllib.parse.urlencode(params)
    url = f"{BASE_URL}/{urllib.parse.quote(storefront)}/songs/{urllib.parse.quote(str(id))}?{qs}"
    return api_get(url, language)

def get_artist(storefront, id, language='en-US'):
    params = {
        'include': 'albums',
        'limit[albums]': '50',
        'l': language
    }
    qs = urllib.parse.urlencode(params)
    url = f"{BASE_URL}/{urllib.parse.quote(storefront)}/artists/{urllib.parse.quote(str(id))}?{qs}"
    return api_get(url, language)

def get_playlist(storefront, id, language='en-US'):
    params = {
        'include': 'tracks',
        'include[songs]': 'artists',
        'include[albums]': 'artists',
        'extend': 'editorialVideo,extendedAssetUrls',
        'l': language
    }
    qs = urllib.parse.urlencode(params)
    url = f"{BASE_URL}/{urllib.parse.quote(storefront)}/playlists/{urllib.parse.quote(str(id))}?{qs}"
    return api_get(url, language)

def normalize_album(raw):
    if not raw:
        return None
    a = raw.get('attributes', {})
    
    artists_raw = raw.get('relationships', {}).get('artists', {}).get('data', [])
    artists = []
    for x in artists_raw:
        artists.append({
            'id': x.get('id'),
            'name': x.get('attributes', {}).get('name') or a.get('artistName')
        })
        
    tracks_raw = raw.get('relationships', {}).get('tracks', {}).get('data', [])
    tracks = []
    for t in tracks_raw:
        ta = t.get('attributes', {})
        audio_traits = ta.get('audioTraits', [])
        tracks.append({
            'id': t.get('id'),
            'name': ta.get('name'),
            'trackNumber': ta.get('trackNumber'),
            'discNumber': ta.get('discNumber'),
            'durationMs': ta.get('durationInMillis'),
            'isrc': ta.get('isrc'),
            'artistName': ta.get('artistName'),
            'hasLossless': 'lossless' in audio_traits,
            'hasHiRes': 'hi-res-lossless' in audio_traits,
            'hasAtmos': 'atmos' in audio_traits or 'spatial' in audio_traits,
        })
        
    audio_traits = a.get('audioTraits', [])
    return {
        'id': raw.get('id'),
        'type': raw.get('type'),
        'name': a.get('name'),
        'artistName': a.get('artistName'),
        'artistId': artists[0]['id'] if artists else None,
        'artists': artists,
        'genreNames': a.get('genreNames', []),
        'releaseDate': a.get('releaseDate'),
        'year': str(a.get('releaseDate'))[:4] if a.get('releaseDate') else None,
        'trackCount': a.get('trackCount'),
        'isCompilation': a.get('isCompilation'),
        'isSingle': a.get('isSingle'),
        'recordLabel': a.get('recordLabel'),
        'copyright': a.get('copyright'),
        'upc': a.get('upc'),
        'url': a.get('url'),
        'contentRating': a.get('contentRating'),
        'artworkTemplate': a.get('artwork', {}).get('url') if a.get('artwork') else None,
        'artworkColor': a.get('artwork', {}).get('bgColor') if a.get('artwork') else None,
        'hasLossless': 'lossless' in audio_traits,
        'hasHiRes': 'hi-res-lossless' in audio_traits,
        'hasAtmos': 'atmos' in audio_traits or 'spatial' in audio_traits,
        'tracks': tracks
    }

def normalize_playlist(raw):
    if not raw:
        return None
    a = raw.get('attributes', {})
    curators = raw.get('relationships', {}).get('curators', {}).get('data', [])
    curator = curators[0] if curators else None
    
    tracks_raw = raw.get('relationships', {}).get('tracks', {}).get('data', [])
    tracks = []
    for t in tracks_raw:
        ta = t.get('attributes', {})
        artists_rel = t.get('relationships', {}).get('artists', {}).get('data', [])
        audio_traits = ta.get('audioTraits', [])
        tracks.append({
            'id': t.get('id'),
            'name': ta.get('name'),
            'trackNumber': ta.get('trackNumber'),
            'durationMs': ta.get('durationInMillis'),
            'isrc': ta.get('isrc'),
            'artistName': ta.get('artistName'),
            'artistId': artists_rel[0].get('id') if artists_rel else None,
            'albumName': ta.get('albumName'),
            'artworkTemplate': ta.get('artwork', {}).get('url') if ta.get('artwork') else None,
            'hasLossless': 'lossless' in audio_traits,
            'hasHiRes': 'hi-res-lossless' in audio_traits,
            'hasAtmos': 'atmos' in audio_traits or 'spatial' in audio_traits,
        })
        
    audio_traits = a.get('audioTraits', [])
    return {
        'id': raw.get('id'),
        'type': raw.get('type'),
        'name': a.get('name'),
        'description': a.get('description', {}).get('standard', ''),
        'curatorName': curator.get('attributes', {}).get('name') if curator else (a.get('curatorName') or 'Apple Music'),
        'curatorId': curator.get('id') if curator else None,
        'trackCount': a.get('trackCount'),
        'url': a.get('url'),
        'artworkTemplate': a.get('artwork', {}).get('url') if a.get('artwork') else None,
        'artworkColor': a.get('artwork', {}).get('bgColor') if a.get('artwork') else None,
        'lastModifiedDate': a.get('lastModifiedDate'),
        'hasLossless': 'lossless' in audio_traits,
        'hasHiRes': 'hi-res-lossless' in audio_traits,
        'hasAtmos': 'atmos' in audio_traits or 'spatial' in audio_traits,
        'tracks': tracks
    }

def artwork_url(template, size=600):
    if not template:
        return ''
    # Replace templates {w}x{h} and {f} with size and format
    url = template.replace('{w}', str(size)).replace('{h}', str(size)).replace('{f}', 'jpg')
    return url
