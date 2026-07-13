import os
import json
import base64
from django.conf import settings
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from .secret_key import get_raw_key, load_secrets_at_boot

DEFAULTS = {
    'storefront': 'us',
    'language': 'en-US',
    'quality': 'flac',
    'albumFolderFormat': '{AlbumName} ({ReleaseYear})',
    'artistFolderFormat': '{ArtistName}',
    'songFileFormat': '{SongNumer}. {SongName}',
    'convertToFlac': True,
    'keepAlac': False,
    'coverSize': '1400x1400',
    'downloadLyrics': False,
    'lyricsFormat': 'lrc',
    'lyricsType': 'lyrics',
    'promptForDownloadQuality': False,
    'explicitFilter': 'explicit',
    'appleEmail': None,
    'applePassword': None,
    'mediaUserToken': None,
    'navidromeEnabled': False,
    'navidromeUrl': 'http://navidrome:4533',
    'navidromeUser': None,
    'navidromePassword': None,
    'autoDownloadsEnabled': True,
    'autoDownloadCheckFrequency': 'auto',
    'stagingInsideMusicLibrary': False,
    'namingConvention': 'apple',
}

QUALITY_VALUES = {'flac', 'alac', 'atmos', 'aac'}
NAMING_CONVENTION_VALUES = {'apple', 'qobuz'}
AUTO_DOWNLOAD_FREQUENCY_VALUES = {'auto', '1h', '6h', '12h', 'daily', 'weekly'}

def get_config_dir():
    return getattr(settings, 'AMDL_CONFIG_DIR', os.path.join(settings.BASE_DIR, 'config'))

def get_settings_file_path():
    return os.path.join(get_config_dir(), 'settings.json')

def ensure_config_initialized():
    config_dir = get_config_dir()
    os.makedirs(config_dir, exist_ok=True)
    load_secrets_at_boot(config_dir)
    
    settings_file = get_settings_file_path()
    if not os.path.exists(settings_file):
        write_settings(DEFAULTS)

def encrypt_secret(plaintext):
    if plaintext is None or plaintext == '':
        return None
    try:
        raw_key = get_raw_key()
        iv = os.urandom(12)
        encryptor = Cipher(
            algorithms.AES(raw_key),
            modes.GCM(iv),
            backend=default_backend()
        ).encryptor()
        ciphertext = encryptor.update(plaintext.encode('utf-8')) + encryptor.finalize()
        tag = encryptor.tag
        # Node format: iv (12 bytes) + tag (16 bytes) + ciphertext
        return base64.b64encode(iv + tag + ciphertext).decode('utf-8')
    except Exception as e:
        print(f"Failed to encrypt secret: {e}")
        return None

def decrypt_secret(b64):
    if not b64:
        return None
    try:
        raw_key = get_raw_key()
        data = base64.b64decode(b64)
        if len(data) < 28:
            return None
        iv = data[:12]
        tag = data[12:28]
        ciphertext = data[28:]
        
        decryptor = Cipher(
            algorithms.AES(raw_key),
            modes.GCM(iv, tag),
            backend=default_backend()
        ).decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext.decode('utf-8')
    except Exception as e:
        print(f"Failed to decrypt secret: {e}")
        return None

def to_bool(value, fallback=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ('true', '1', 'yes', 'on'):
            return True
        if s in ('false', '0', 'no', 'off', ''):
            return False
    return bool(fallback)

def normalize_settings(parsed):
    if not isinstance(parsed, dict):
        parsed = {}
        
    quality = parsed.get('quality')
    if quality not in QUALITY_VALUES:
        legacy_flac = parsed.get('convertToFlac', parsed.get('flac_conversion', DEFAULTS['convertToFlac']))
        quality = 'flac' if legacy_flac else 'alac'
        
    freq = parsed.get('autoDownloadCheckFrequency')
    if freq not in AUTO_DOWNLOAD_FREQUENCY_VALUES:
        freq = DEFAULTS['autoDownloadCheckFrequency']
        
    naming = parsed.get('namingConvention')
    if naming not in NAMING_CONVENTION_VALUES:
        naming = DEFAULTS['namingConvention']
        
    res = {}
    res.update(DEFAULTS)
    res.update(parsed)
    
    res['quality'] = quality
    res['convertToFlac'] = (quality == 'flac')
    res['keepAlac'] = to_bool(parsed.get('keepAlac'), DEFAULTS['keepAlac'])
    res['downloadLyrics'] = to_bool(parsed.get('downloadLyrics'), DEFAULTS['downloadLyrics'])
    res['promptForDownloadQuality'] = to_bool(parsed.get('promptForDownloadQuality'), DEFAULTS['promptForDownloadQuality'])
    res['navidromeEnabled'] = to_bool(parsed.get('navidromeEnabled'), DEFAULTS['navidromeEnabled'])
    res['autoDownloadsEnabled'] = to_bool(parsed.get('autoDownloadsEnabled'), DEFAULTS['autoDownloadsEnabled'])
    res['autoDownloadCheckFrequency'] = freq
    res['stagingInsideMusicLibrary'] = to_bool(parsed.get('stagingInsideMusicLibrary'), DEFAULTS['stagingInsideMusicLibrary'])
    res['namingConvention'] = naming
    
    return res

def read_settings():
    ensure_config_initialized()
    settings_file = get_settings_file_path()
    try:
        with open(settings_file, 'r', encoding='utf-8') as f:
            parsed = json.load(f)
        return normalize_settings(parsed)
    except Exception:
        return normalize_settings({})

def write_settings(patch):
    ensure_config_initialized()
    current = read_settings()
    merged = {}
    merged.update(current)
    merged.update(patch)
    
    if 'convertToFlac' in patch and 'quality' not in patch:
        merged['quality'] = 'flac' if patch['convertToFlac'] else 'alac'
        
    next_settings = normalize_settings(merged)
    settings_file = get_settings_file_path()
    try:
        with open(settings_file, 'w', encoding='utf-8') as f:
            json.dump(next_settings, f, indent=2)
    except Exception as e:
        print(f"Failed to write settings file: {e}")
        
    return next_settings

def mask_email(email):
    if not email:
        return '••••'
    parts = str(email).split('@')
    if len(parts) != 2:
        return '••••'
    u, d = parts
    if len(u) <= 2:
        masked = u[0] if u else '•'
    else:
        masked = u[0] + '•••' + u[-1]
    return f"{masked}@{d}"

def read_public_settings():
    s = read_settings()
    return {
        'storefront': s['storefront'],
        'language': s['language'],
        'quality': s['quality'],
        'albumFolderFormat': s['albumFolderFormat'],
        'artistFolderFormat': s['artistFolderFormat'],
        'songFileFormat': s['songFileFormat'],
        'convertToFlac': s['quality'] == 'flac',
        'keepAlac': s['keepAlac'],
        'coverSize': s['coverSize'],
        'downloadLyrics': bool(s['downloadLyrics']),
        'lyricsFormat': s['lyricsFormat'] or 'lrc',
        'lyricsType': s['lyricsType'] or 'lyrics',
        'promptForDownloadQuality': bool(s['promptForDownloadQuality']),
        'explicitFilter': s['explicitFilter'] or 'explicit',
        'appleEmailMasked': mask_email(s['appleEmail']) if s['appleEmail'] else None,
        'hasApplePassword': bool(s['applePassword']),
        'hasMediaUserToken': bool(s['mediaUserToken']),
        'navidromeEnabled': bool(s['navidromeEnabled']),
        'navidromeUrl': s['navidromeUrl'],
        'navidromeUser': s['navidromeUser'],
        'hasNavidromePassword': bool(s['navidromePassword']),
        'autoDownloadsEnabled': bool(s['autoDownloadsEnabled']),
        'autoDownloadCheckFrequency': s['autoDownloadCheckFrequency'],
        'stagingInsideMusicLibrary': bool(s['stagingInsideMusicLibrary']),
        'namingConvention': s['namingConvention'],
    }
