import os
import re
import time
from django.conf import settings
from .folder_layout import sanitize_segment

AUDIO_RE = re.compile(r'\.(flac|m4a|mp3)$', re.IGNORECASE)
SCAN_TTL_SECONDS = 30

_scan_cache = None
_scan_cache_at = 0

def get_music_root():
    return getattr(settings, 'AMDL_MUSIC_PATH', os.path.join(settings.BASE_DIR, 'music'))

def strip_trailing_year(title):
    if not title:
        return ''
    return re.sub(r'\s*[(\[]\d{4}[)\]]\s*$', '', str(title)).strip()

def make_album_key(artist_name, album_name):
    artist_key = sanitize_segment(artist_name).lower()
    album_key = sanitize_segment(strip_trailing_year(album_name)).lower()
    if not artist_key or not album_key or artist_key == '_' or album_key == '_':
        return ''
    return f"{artist_key}::{album_key}"

def make_song_key(artist_name, song_name):
    artist_key = sanitize_segment(artist_name).lower()
    song_key = sanitize_segment(song_name).lower()
    if not artist_key or not song_key or artist_key == '_' or song_key == '_':
        return ''
    return f"{artist_key}::{song_key}"

def song_name_from_filename(filename):
    if not filename:
        return ''
    base = re.sub(AUDIO_RE, '', str(filename))
    # Strip leading track-number prefixes
    base = re.sub(r'^\s*\d{1,3}\s*[.\-]?\s+', '', base)
    # Strip [E]/[C]/[M] suffixes
    base = re.sub(r'\s*\[[ECM]\]\s*$', '', base, flags=re.IGNORECASE)
    return base.strip()

def parse_playlist_m3u_text(text):
    lines = str(text or '').splitlines()
    playlist_title = None
    catalog_playlist_id = None
    library_playlist_id = None
    track_count = 0

    for line in lines:
        trimmed = line.strip()
        if not trimmed:
            continue
        if trimmed.startswith('#'):
            play_m = re.match(r'^#PLAYLIST:(.+)$', trimmed)
            if play_m:
                playlist_title = play_m.group(1).strip()
                continue
            catalog_m = re.match(r'^#ALACARTE_PLAYLIST_ID:(.+)$', trimmed)
            if catalog_m:
                catalog_playlist_id = catalog_m.group(1).strip()
                continue
            lib_m = re.match(r'^#ALACARTE_LIBRARY_PLAYLIST_ID:(.+)$', trimmed)
            if lib_m:
                library_playlist_id = lib_m.group(1).strip()
                continue
            continue
        track_count += 1

    return {
        'playlistTitle': playlist_title,
        'catalogPlaylistId': catalog_playlist_id,
        'libraryPlaylistId': library_playlist_id,
        'trackCount': track_count
    }

def read_playlist_m3u_file_meta(abs_path):
    try:
        with open(abs_path, 'r', encoding='utf-8') as f:
            text = f.read()
        return parse_playlist_m3u_text(text)
    except Exception:
        return {
            'playlistTitle': None,
            'catalogPlaylistId': None,
            'libraryPlaylistId': None,
            'trackCount': 0
        }

def has_sibling_lrc(audio_path):
    base, _ = os.path.splitext(audio_path)
    lrc_path = base + '.lrc'
    return os.path.isfile(lrc_path)

def to_rel(abs_path, music_root):
    try:
        rel = os.path.relpath(abs_path, music_root)
        return rel.replace(os.path.sep, '/')
    except Exception:
        return abs_path

def get_cached_index():
    global _scan_cache, _scan_cache_at
    now = time.time()
    if _scan_cache and (now - _scan_cache_at) < SCAN_TTL_SECONDS:
        return _scan_cache
    _scan_cache = scan_library()
    _scan_cache_at = now
    return _scan_cache

def invalidate_library_cache():
    global _scan_cache, _scan_cache_at
    _scan_cache = None
    _scan_cache_at = 0

def scan_library_once():
    return get_cached_index()

def scan_library():
    music_root = get_music_root()
    albums = []
    singles = []
    album_keys = set()
    song_keys = set()
    album_track_keys = {}
    singles_song_keys = set()
    playlist_ids = set()
    playlists = []

    # 1. Scan playlists
    playlists_dir = os.path.join(music_root, 'Playlists')
    if os.path.exists(playlists_dir):
        try:
            entries = os.listdir(playlists_dir)
            for entry in entries:
                abs_path = os.path.join(playlists_dir, entry)
                if os.path.isfile(abs_path) and entry.lower().endswith('.m3u8'):
                    meta = read_playlist_m3u_file_meta(abs_path)
                    if meta['catalogPlaylistId']:
                        playlist_ids.add(meta['catalogPlaylistId'])
                    if meta['libraryPlaylistId']:
                        playlist_ids.add(meta['libraryPlaylistId'])
                    
                    try:
                        stat = os.stat(abs_path)
                        added_at = int(max(stat.st_mtime, stat.st_ctime) * 1000)
                    except Exception:
                        added_at = 0
                        
                    rel_path = to_rel(abs_path, music_root)
                    display_name = meta['playlistTitle'] or os.path.splitext(entry)[0] or entry
                    playlists.append({
                        'id': rel_path,
                        'relPath': rel_path,
                        'fileName': entry,
                        'playlistName': display_name,
                        'catalogPlaylistId': meta['catalogPlaylistId'],
                        'libraryPlaylistId': meta['libraryPlaylistId'],
                        'trackCount': meta['trackCount'],
                        'addedAt': added_at
                    })
        except Exception as e:
            print(f"Failed to scan playlists: {e}")

    playlists.sort(key=lambda x: (x['playlistName'].lower(), x['relPath'].lower()))

    # 2. Scan artists, albums, and tracks
    if os.path.exists(music_root):
        try:
            artists = os.listdir(music_root)
            for artist_name in artists:
                artist_path = os.path.join(music_root, artist_name)
                if not os.path.isdir(artist_path):
                    continue
                if artist_name.startswith('.') or artist_name == 'Playlists':
                    continue

                children = os.listdir(artist_path)
                for child_name in children:
                    child_path = os.path.join(artist_path, child_name)
                    if not os.path.isdir(child_path) or child_name.startswith('.'):
                        continue

                    # Handle Singles directory
                    if child_name.lower() == 'singles':
                        files = os.listdir(child_path)
                        for file_name in files:
                            audio_path = os.path.join(child_path, file_name)
                            if os.path.isfile(audio_path) and AUDIO_RE.search(file_name):
                                has_lyrics = has_sibling_lrc(audio_path)
                                try:
                                    stat = os.stat(audio_path)
                                    added_at = int(max(stat.st_mtime, stat.st_ctime) * 1000)
                                except Exception:
                                    added_at = 0
                                
                                rel_path = to_rel(audio_path, music_root)
                                song_name = song_name_from_filename(file_name)
                                singles.append({
                                    'id': rel_path,
                                    'artistName': artist_name,
                                    'songName': song_name,
                                    'relPath': rel_path,
                                    'hasLyrics': has_lyrics,
                                    'addedAt': added_at
                                })
                                song_key = make_song_key(artist_name, song_name)
                                if song_key:
                                    song_keys.add(song_key)
                                    singles_song_keys.add(song_key)
                        continue

                    # Handle Album directory
                    files = os.listdir(child_path)
                    audio_files = [f for f in files if os.path.isfile(os.path.join(child_path, f)) and AUDIO_RE.search(f)]
                    if not audio_files:
                        continue

                    lyrics_count = 0
                    for file_name in audio_files:
                        audio_path = os.path.join(child_path, file_name)
                        if has_sibling_lrc(audio_path):
                            lyrics_count += 1

                    try:
                        stat = os.stat(child_path)
                        added_at = int(max(stat.st_mtime, stat.st_ctime) * 1000)
                    except Exception:
                        added_at = 0

                    rel_path = to_rel(child_path, music_root)
                    albums.append({
                        'id': rel_path,
                        'artistName': artist_name,
                        'albumName': child_name,
                        'relPath': rel_path,
                        'trackCount': len(audio_files),
                        'lyricsCount': lyrics_count,
                        'hasLyrics': lyrics_count > 0,
                        'addedAt': added_at
                    })
                    
                    album_key = make_album_key(artist_name, child_name)
                    if album_key:
                        album_keys.add(album_key)
                    
                    track_set = set()
                    for file_name in audio_files:
                        song_name = song_name_from_filename(file_name)
                        if not song_name:
                            continue
                        song_key = make_song_key(artist_name, song_name)
                        if not song_key:
                            continue
                        song_keys.add(song_key)
                        track_set.add(song_key)
                        
                    if album_key:
                        album_track_keys[album_key] = track_set
        except Exception as e:
            print(f"Failed to scan library directories: {e}")

    singles.sort(key=lambda x: (x['artistName'].lower(), x['songName'].lower()))
    albums.sort(key=lambda x: (x['artistName'].lower(), x['albumName'].lower()))

    return {
        'albums': albums,
        'singles': singles,
        'albumKeys': album_keys,
        'songKeys': song_keys,
        'albumTrackKeys': album_track_keys,
        'singlesSongKeys': singles_song_keys,
        'playlistIds': playlist_ids,
        'playlists': playlists
    }

def is_playlist_in_library(playlist_id, pre_scanned_index=None):
    if not playlist_id:
        return False
    index = pre_scanned_index or get_cached_index()
    return str(playlist_id) in index['playlistIds']

def has_album_in_library(artist_name, album_name, pre_scanned_index=None):
    key = make_album_key(artist_name, strip_trailing_year(album_name))
    if not key:
        return False
    index = pre_scanned_index or get_cached_index()
    return key in index['albumKeys']

def has_song_in_library(artist_name, song_name, pre_scanned_index=None):
    key = make_song_key(artist_name, song_name)
    if not key:
        return False
    index = pre_scanned_index or get_cached_index()
    return key in index['songKeys']

def get_album_track_presence(artist_name, album_name, tracks, pre_scanned_index=None):
    index = pre_scanned_index or get_cached_index()
    album_key = make_album_key(artist_name, album_name)
    album_track_set = index['albumTrackKeys'].get(album_key) if album_key else None
    singles_set = index['singlesSongKeys'] or set()
    
    present = {}
    count = 0
    for track in (tracks or []):
        track_id = str(track.get('id', ''))
        if not track_id:
            continue
        song_key = make_song_key(artist_name, track.get('name', ''))
        has = bool(
            song_key and (
                (album_track_set and song_key in album_track_set) or
                song_key in singles_set
            )
        )
        present[track_id] = has
        if has:
            count += 1
            
    expected = len(tracks or [])
    return {
        'tracks': present,
        'present': count,
        'expected': expected,
        'complete': expected > 0 and count == expected and album_track_set is not None,
        'folderExists': album_track_set is not None
    }

def purge_playlist_exports_sharing_ids(music_root, playlist_id=None, library_playlist_id=None, keep_abs_path=None):
    playlists_dir = os.path.join(music_root, 'Playlists')
    catalog_str = str(playlist_id).strip() if playlist_id else ''
    library_str = str(library_playlist_id).strip() if library_playlist_id else ''
    if not catalog_str and not library_str:
        return

    if not os.path.exists(playlists_dir):
        return

    try:
        entries = os.listdir(playlists_dir)
        keep_resolved = os.path.abspath(keep_abs_path) if keep_abs_path else None

        for entry in entries:
            abs_path = os.path.join(playlists_dir, entry)
            if not os.path.isfile(abs_path) or not entry.lower().endswith('.m3u8'):
                continue
            if keep_resolved and os.path.abspath(abs_path) == keep_resolved:
                continue
                
            meta = read_playlist_m3u_file_meta(abs_path)
            match_catalog = bool(catalog_str and meta['catalogPlaylistId'] == catalog_str)
            match_library = bool(library_str and meta['libraryPlaylistId'] == library_str)
            
            if match_catalog or match_library:
                try:
                    os.remove(abs_path)
                except Exception:
                    pass
                
                stem, _ = os.path.splitext(entry)
                for ext in ['.jpg', '.jpeg', '.png', '.webp']:
                    try:
                        os.remove(os.path.join(playlists_dir, f"{stem}{ext}"))
                    except Exception:
                        pass
                
                companion_dir = os.path.join(playlists_dir, stem)
                if os.path.isdir(companion_dir):
                    try:
                        shutil.rmtree(companion_dir)
                    except Exception:
                        pass
    except Exception as e:
        print(f"Failed to purge shared playlists: {e}")
