import urllib.request
import urllib.parse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from .apple_api import get_bearer_token, invalidate_bearer_cache, search_catalog

ME_URL = 'https://amp-api.music.apple.com/v1/me'
LIBRARY_PAGE_SIZE = 100
_resolve_cache = {}

def api_get(url, media_user_token, language='en-US'):
    if not media_user_token:
        raise Exception('media-user-token not configured')
        
    token = get_bearer_token()
    
    def run(t):
        headers = {
            'Authorization': f'Bearer {t}',
            'Music-User-Token': media_user_token,
            'Origin': 'https://music.apple.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': language or 'en-US'
        }
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
    if status == 401:
        invalidate_bearer_cache()
        token = get_bearer_token()
        status, body = run(token)
        
    if status in (401, 403):
        raise Exception(f'Apple library {status}: media-user-token rejected ({body[:120]})')
        
    if status < 200 or status >= 300:
        parsed_url = urllib.parse.urlparse(url)
        raise Exception(f'Apple library {status} on {parsed_url.path}: {body[:200]}')
        
    return json.loads(body)

def get_my_storefront(media_user_token, language='en-US'):
    try:
        data = api_get(f"{ME_URL}/storefront", media_user_token, language)
        return data.get('data', [{}])[0].get('id')
    except Exception:
        return None

def build_library_url(kind, offset=0, limit=LIBRARY_PAGE_SIZE, language='en-US'):
    params = {
        'include': 'catalog',
        'extend': 'playParams,catalogId',
        'limit': str(max(1, min(limit, LIBRARY_PAGE_SIZE))),
        'offset': str(max(0, offset)),
        'l': language
    }
    if kind == 'songs':
        params['include[library-songs]'] = 'catalog,albums'
        params['include[songs]'] = 'albums'
    elif kind == 'albums':
        params['include[library-albums]'] = 'catalog'
    elif kind == 'playlists':
        params['include[library-playlists]'] = 'catalog'
        
    qs = urllib.parse.urlencode(params)
    return f"{ME_URL}/library/{kind}?{qs}"

CATALOG_NUMERIC_RE = re.compile(r'^\d{6,15}$')
CATALOG_PLAYLIST_RE = re.compile(r'^pl\.[A-Za-z0-9-]+$')
LIBRARY_PREFIX_RE = re.compile(r'^[ilp]\.')

def is_apple_catalog_id(id_val):
    if not id_val:
        return False
    s = str(id_val)
    if LIBRARY_PREFIX_RE.match(s):
        return False
    return bool(CATALOG_NUMERIC_RE.match(s) or CATALOG_PLAYLIST_RE.match(s))

def pick_catalog_id(raw):
    pp = raw.get('attributes', {}).get('playParams', {})
    if pp.get('catalogId'):
        return str(pp['catalogId'])
    rel = raw.get('relationships', {}).get('catalog', {}).get('data', [])
    if rel and isinstance(rel, list) and rel[0].get('id'):
        return str(rel[0]['id'])
    if pp.get('purchasedId') and is_apple_catalog_id(pp['purchasedId']):
        return str(pp['purchasedId'])
    if pp.get('id') and is_apple_catalog_id(pp['id']):
        return str(pp['id'])
    return None

def pick_catalog_playlist_id(raw):
    rel = raw.get('relationships', {}).get('catalog', {}).get('data', [])
    if rel and isinstance(rel, list) and rel[0].get('id'):
        return str(rel[0]['id'])
    pp = raw.get('attributes', {}).get('playParams', {})
    if pp.get('globalId') and CATALOG_PLAYLIST_RE.match(pp['globalId']):
        return str(pp['globalId'])
    if pp.get('id') and CATALOG_PLAYLIST_RE.match(pp['id']) and pp.get('isLibrary') is not True and raw.get('attributes', {}).get('canEdit') is not True:
        return str(pp['id'])
    return None

def relation_id(raw, name):
    rel = raw.get('relationships', {}).get(name, {}).get('data', [])
    return str(rel[0]['id']) if rel and isinstance(rel, list) and rel[0].get('id') else None

def normalize_library_album(raw):
    if not raw:
        return None
    a = raw.get('attributes', {})
    catalog_id = pick_catalog_id(raw)
    return {
        'libraryId': raw.get('id'),
        'catalogId': catalog_id,
        'name': a.get('name') or 'Unknown album',
        'artistName': a.get('artistName') or 'Unknown artist',
        'artworkTemplate': a.get('artwork', {}).get('url') if a.get('artwork') else None,
        'artworkColor': a.get('artwork', {}).get('bgColor') if a.get('artwork') else None,
        'trackCount': int(a.get('trackCount') or 0),
        'dateAdded': a.get('dateAdded'),
        'downloadable': bool(catalog_id),
    }

def normalize_library_playlist(raw):
    if not raw:
        return None
    a = raw.get('attributes', {})
    catalog_id = pick_catalog_playlist_id(raw)
    is_user_created = a.get('canEdit') is True or (a.get('playParams', {}).get('isLibrary') is True and not catalog_id)
    return {
        'libraryId': raw.get('id'),
        'catalogId': catalog_id,
        'name': a.get('name') or 'Untitled playlist',
        'curatorName': a.get('curatorName') or ('You' if is_user_created else 'Apple Music'),
        'description': a.get('description', {}).get('standard', ''),
        'artworkTemplate': a.get('artwork', {}).get('url') if a.get('artwork') else None,
        'artworkColor': a.get('artwork', {}).get('bgColor') if a.get('artwork') else None,
        'dateAdded': a.get('dateAdded'),
        'isUserCreated': is_user_created,
        'downloadable': bool(catalog_id),
    }

def normalize_library_song(raw, album_lookup=None):
    if not raw:
        return None
    a = raw.get('attributes', {})
    catalog_id = pick_catalog_id(raw)
    catalog_album_id = album_lookup.get(catalog_id) if catalog_id and album_lookup else None
    return {
        'libraryId': raw.get('id'),
        'catalogId': catalog_id,
        'catalogAlbumId': catalog_album_id,
        'name': a.get('name') or 'Unknown song',
        'artistName': a.get('artistName') or 'Unknown artist',
        'albumName': a.get('albumName') or '',
        'durationMs': int(a.get('durationInMillis') or 0),
        'artworkTemplate': a.get('artwork', {}).get('url') if a.get('artwork') else None,
        'contentRating': a.get('contentRating'),
        'downloadable': bool(catalog_id),
    }

def build_song_album_lookup(included):
    lookup = {}
    if not isinstance(included, list):
        return lookup
    for entry in included:
        if entry.get('type') != 'songs':
            continue
        rel_albums = entry.get('relationships', {}).get('albums', {}).get('data', [])
        album_id = rel_albums[0].get('id') if rel_albums and isinstance(rel_albums, list) else None
        if entry.get('id') and album_id:
            lookup[str(entry['id'])] = str(album_id)
    return lookup

def normalize_library_track(raw):
    if not raw:
        return None
    a = raw.get('attributes', {})
    catalog_id = pick_catalog_id(raw)
    traits = a.get('audioTraits', [])
    return {
        'id': catalog_id or raw.get('id'),
        'libraryId': raw.get('id'),
        'catalogId': catalog_id,
        'name': a.get('name') or 'Unknown song',
        'artistName': a.get('artistName') or 'Unknown artist',
        'albumName': a.get('albumName') or '',
        'durationMs': int(a.get('durationInMillis') or 0),
        'artworkTemplate': a.get('artwork', {}).get('url') if a.get('artwork') else None,
        'artworkColor': a.get('artwork', {}).get('bgColor') if a.get('artwork') else None,
        'contentRating': a.get('contentRating'),
        'hasLossless': 'lossless' in traits,
        'hasHiRes': 'hi-res-lossless' in traits,
        'hasAtmos': 'atmos' in traits or 'spatial' in traits,
        'downloadable': bool(catalog_id),
    }

def normalize_text(val):
    import unicodedata
    s = str(val or '')
    s = ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))
    s = s.lower().replace('&', 'and')
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return s.strip()

def make_resolve_cache_key(kind, item):
    return f"{kind}|{normalize_text(item['artistName'])}|{normalize_text(item['name'])}"

def same_text(a, b):
    x = normalize_text(a)
    y = normalize_text(b)
    return bool(x) and x == y

def name_matches_loose(item_name, apple_name):
    x = normalize_text(item_name)
    y = normalize_text(apple_name)
    if not x or not y:
        return False
    if x == y:
        return True
    if y.startswith(x + ' ') or x.startswith(y + ' '):
        return True
    return False

def artist_matches_loose(item_artist, apple_artist):
    x = normalize_text(item_artist)
    y = normalize_text(apple_artist)
    if not x or not y:
        return False
    if x == y:
        return True
    xt = x.split()
    yt = set(y.split())
    if not xt:
        return False
    return all(t in yt for t in xt)

def score_catalog_song(item, raw):
    a = raw.get('attributes', {})
    score = 0
    if name_matches_loose(item['name'], a.get('name')):
        score += 4
    if artist_matches_loose(item['artistName'], a.get('artistName')):
        score += 3
    if same_text(item['albumName'], a.get('albumName')):
        score += 2
    return score

def score_catalog_album(item, raw):
    a = raw.get('attributes', {})
    score = 0
    if name_matches_loose(item['name'], a.get('name')):
        score += 4
    if artist_matches_loose(item['artistName'], a.get('artistName')):
        score += 3
    if int(item.get('trackCount') or 0) == int(a.get('trackCount') or 0):
        score += 1
    return score

def pick_best_search_match(kind, item, data):
    best_score = -1
    best = None
    for raw in data:
        score = score_catalog_song(item, raw) if kind == 'songs' else score_catalog_album(item, raw)
        if score > best_score:
            best_score = score
            best = raw
    if best_score < 7:
        return None
    return best

def fetch_catalog_match(kind, item, storefront, language, media_user_token):
    term = f"{item['artistName'] or ''} {item['name'] or ''}".strip()
    if not term or not storefront:
        return None
    types = 'songs' if kind == 'songs' else 'albums'
    try:
        json_data = search_catalog(
            storefront=storefront,
            term=term,
            types=types,
            limit=10,
            language=language,
            media_user_token=media_user_token
        )
        data = json_data.get('results', {}).get(types, {}).get('data', [])
    except Exception:
        return None
        
    best = pick_best_search_match(kind, item, data)
    if not best or not best.get('id'):
        return None
    return {
        'catalogId': str(best['id']),
        'albumId': relation_id(best, 'albums') if kind == 'songs' else None
    }

def resolve_missing_catalog_ids(kind, items, storefront, language, media_user_token):
    if not storefront or not items or kind not in ('songs', 'albums'):
        return items
        
    unresolved = [i for i in items if not i.get('catalogId') and i.get('name') and i.get('artistName')]
    if not unresolved:
        return items
        
    now = time.time()
    
    def resolve_item(item):
        key = make_resolve_cache_key(kind, item)
        cached = _resolve_cache.get(key)
        if cached and cached['expiresAt'] > now:
            result = cached['result']
        else:
            result = fetch_catalog_match(kind, item, storefront, language, media_user_token)
            ttl = 24 * 60 * 60 if result else 60 * 60 # negative cache is 1 hour
            _resolve_cache[key] = {'result': result, 'expiresAt': now + ttl}
            
        if result:
            item['catalogId'] = result['catalogId']
            if kind == 'songs' and result['albumId']:
                item['catalogAlbumId'] = result['albumId']
            item['downloadable'] = True

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(resolve_item, item) for item in unresolved]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                pass
                
    return items

def fetch_library_page(kind, media_user_token, language='en-US', offset=0, limit=LIBRARY_PAGE_SIZE, storefront='us'):
    if kind not in ('albums', 'playlists', 'songs'):
        raise Exception(f"unknown library kind: {kind}")
        
    url = build_library_url(kind, offset, limit, language)
    json_data = api_get(url, media_user_token, language)
    data = json_data.get('data', [])
    
    if kind == 'albums':
        items = [normalize_library_album(raw) for raw in data if raw]
    elif kind == 'playlists':
        items = [normalize_library_playlist(raw) for raw in data if raw]
    else:
        lookup = build_song_album_lookup(json_data.get('included'))
        items = [normalize_library_song(raw, lookup) for raw in data if raw]
        
    items = resolve_missing_catalog_ids(kind, items, storefront, language, media_user_token)
    
    next_url = json_data.get('next')
    total = json_data.get('meta', {}).get('total')
    return {
        'items': items,
        'next': offset + len(items) if next_url else None,
        'total': total
    }

def fetch_library_playlist_tracks_page(library_id, media_user_token, language='en-US', offset=0, limit=LIBRARY_PAGE_SIZE):
    params = {
        'include': 'catalog',
        'include[library-songs]': 'catalog',
        'extend': 'playParams,catalogId',
        'limit': str(max(1, min(limit, LIBRARY_PAGE_SIZE))),
        'offset': str(max(0, offset)),
        'l': language
    }
    qs = urllib.parse.urlencode(params)
    url = f"{ME_URL}/library/playlists/{urllib.parse.quote(library_id)}/tracks?{qs}"
    json_data = api_get(url, media_user_token, language)
    data = json_data.get('data', [])
    return {
        'items': [normalize_library_track(raw) for raw in data if raw],
        'next': offset + len(data) if json_data.get('next') else None
    }

def get_library_playlist_detail(library_id, media_user_token, language='en-US'):
    # Fetch playlist header
    url = f"{ME_URL}/library/playlists/{urllib.parse.quote(library_id)}"
    json_data = api_get(url, media_user_token, language)
    raw_head = json_data.get('data', [{}])[0]
    if not raw_head:
        return None
    head = normalize_library_playlist(raw_head)
    
    # Iterate tracks
    tracks = []
    offset = 0
    while True:
        page = fetch_library_playlist_tracks_page(library_id, media_user_token, language, offset)
        tracks.extend(page['items'])
        if not page['next'] or not page['items']:
            break
        offset += len(page['items'])
        
    undownloadable_count = len([t for t in tracks if not t['downloadable']])
    return {
        'libraryId': library_id,
        'catalogId': head['catalogId'],
        'name': head['name'],
        'curatorName': head['curatorName'],
        'description': head['description'],
        'artworkTemplate': head['artworkTemplate'],
        'artworkColor': head['artworkColor'],
        'isUserCreated': head['isUserCreated'],
        'trackCount': len(tracks),
        'tracks': tracks,
        'hasLossless': any(t['hasLossless'] for t in tracks),
        'hasHiRes': any(t['hasHiRes'] for t in tracks),
        'hasAtmos': any(t['hasAtmos'] for t in tracks),
        'undownloadableCount': undownloadable_count,
        'downloadable': any(t['downloadable'] for t in tracks),
    }

def iterate_library(kind, media_user_token, language='en-US', storefront='us'):
    offset = 0
    while True:
        page = fetch_library_page(kind, media_user_token, language, offset, limit=100, storefront=storefront)
        items = page['items']
        if not items:
            break
        for item in items:
            yield item
        if not page['next']:
            break
        offset = page['next']
