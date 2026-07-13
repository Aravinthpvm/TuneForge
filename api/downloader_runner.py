import os
import re
import subprocess
import threading
from django.conf import settings

ANSI_ESCAPE = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]')

def strip_ansi(s):
    return ANSI_ESCAPE.sub('', s)

def emit_yaml(obj):
    lines = []
    for k, v in obj.items():
        if isinstance(v, str):
            lines.append(f"{k}: {json_escape(v)}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        elif v is None:
            lines.append(f"{k}: \"\"")
        else:
            lines.append(f"{k}: {json_escape(str(v))}")
    return '\n'.join(lines) + '\n'

def json_escape(s):
    # Simple escaping for yaml inline strings
    import json
    return json.dumps(s)

def write_amdp_config(amdl_settings, media_user_token, staging_root):
    lyrics_enabled = bool(amdl_settings.get('downloadLyrics') and media_user_token)
    
    wrapper_host = os.environ.get('AMDL_WRAPPER_HOST', 'alacarte-wrapper')
    wrapper_decrypt_port = os.environ.get('AMDL_WRAPPER_DECRYPT_PORT', '10020')
    wrapper_m3u8_port = os.environ.get('AMDL_WRAPPER_M3U8_PORT', '20020')
    
    cfg = {
        'media-user-token': media_user_token or '',
        'authorization-token': '',
        'language': amdl_settings.get('language', ''),
        'lrc-type': amdl_settings.get('lyricsType', 'lyrics'),
        'lrc-format': amdl_settings.get('lyricsFormat', 'lrc'),
        'embed-lrc': lyrics_enabled,
        'save-lrc-file': lyrics_enabled,
        'save-artist-cover': False,
        'save-animated-artwork': False,
        'emby-animated-artwork': False,
        'embed-cover': True,
        'cover-size': amdl_settings.get('coverSize', '1400x1400'),
        'cover-format': 'jpg',
        'alac-save-folder': staging_root,
        'atmos-save-folder': staging_root,
        'aac-save-folder': staging_root,
        'mv-save-folder': staging_root,
        'max-memory-limit': 256,
        'decrypt-m3u8-port': f"{wrapper_host}:{wrapper_decrypt_port}",
        'get-m3u8-port': f"{wrapper_host}:{wrapper_m3u8_port}",
        'get-m3u8-from-device': True,
        'get-m3u8-mode': 'hires',
        'aac-type': 'aac-lc',
        'alac-max': 192000,
        'atmos-max': 2768,
        'limit-max': 200,
        'album-folder-format': '{AlbumName}',
        'playlist-folder-format': '{PlaylistName}',
        'song-file-format': '{SongNumer}. {SongName}',
        'artist-folder-format': '{ArtistName}',
        'explicit-choice': '[E]',
        'clean-choice': '[C]',
        'apple-master-choice': '[M]',
        'use-songinfo-for-playlist': False,
        'dl-albumcover-for-playlist': False,
        'mv-audio-type': 'atmos',
        'mv-max': 2160,
        'storefront': amdl_settings.get('storefront', 'us'),
        'convert-after-download': False,
        'convert-format': 'flac',
        'convert-keep-original': False,
        'convert-skip-if-source-matches': True,
        'ffmpeg-path': 'ffmpeg',
        'convert-extra-args': '',
        'convert-with-metadata': True,
        'convert-warn-lossy-to-lossless': True,
        'convert-skip-lossy-to-lossless': True,
        'convert-check-bad-alac': False,
        'convert-delete-bad-alac': False,
    }
    
    config_path = os.path.join(staging_root, 'config.yaml')
    os.makedirs(staging_root, exist_ok=True)
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write(emit_yaml(cfg))
    return config_path

def run_stream_reader(stream, which, on_line):
    buffer = b''
    while True:
        try:
            chunk = stream.read(1)
        except Exception:
            break
        if not chunk:
            if buffer:
                line = strip_ansi(buffer.decode('utf-8', errors='replace')).strip()
                if line:
                    on_line(line, which)
            break
        if chunk in (b'\n', b'\r'):
            line = strip_ansi(buffer.decode('utf-8', errors='replace')).strip()
            if line:
                on_line(line, which)
            buffer = b''
        else:
            buffer += chunk

def spawn_amdp(args, cwd, on_line):
    # We execute 'apple-music-dl'
    # Wait, in development or inside docker, 'apple-music-dl' needs to be on PATH.
    # On Windows, we could run 'apple-music-dl.exe' or python command depending on installation.
    # To cover both, we will check if apple-music-dl exists as a command, otherwise run it via python.
    cmd = ['apple-music-dl'] + args
    
    # We hide window on windows to prevent popups
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        startupinfo=startupinfo
    )
    
    threads = []
    t_out = threading.Thread(target=run_stream_reader, args=(proc.stdout, 'stdout', on_line), daemon=True)
    t_err = threading.Thread(target=run_stream_reader, args=(proc.stderr, 'stderr', on_line), daemon=True)
    
    threads.extend([t_out, t_err])
    t_out.start()
    t_err.start()
    
    return proc, threads
