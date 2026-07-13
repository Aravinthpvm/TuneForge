import os
import re
import shutil

BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
FEAT_SUFFIX_RE = re.compile(r'\s+[\(\[](feat\.|ft\.)[^\)\]]*[\)\]]', re.IGNORECASE)
SINGLE_SUFFIX_RE = re.compile(r'\s+[\u2013\-]\s+Single$', re.IGNORECASE)

def sanitize_segment(name):
    if not name:
        return '_'
    name_str = str(name)
    name_str = BAD_CHARS.sub('_', name_str)
    name_str = re.sub(r'\.+$', '', name_str)
    return name_str.strip()[:200] or '_'

def apply_naming_convention(name, convention):
    if convention != 'qobuz':
        return name
    name_str = FEAT_SUFFIX_RE.sub('', name)
    name_str = SINGLE_SUFFIX_RE.sub('', name_str)
    return name_str.strip()

def resolve_artist_dir(music_root, desired_artist):
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

def compute_final_dir(music_root, artist, album, year=None):
    artist_dir = resolve_artist_dir(music_root, artist)
    album_seg = sanitize_segment(album)
    return os.path.join(music_root, artist_dir, album_seg)

def ensure_dir(p):
    os.makedirs(p, exist_ok=True)

def merge_move(src, dest):
    ensure_dir(dest)
    if not os.path.exists(src):
        return
    entries = os.listdir(src)
    for entry in entries:
        from_path = os.path.join(src, entry)
        to_path = os.path.join(dest, entry)
        if os.path.isdir(from_path):
            merge_move(from_path, to_path)
        else:
            try:
                # shutil.move handles rename and copies across filesystems
                shutil.move(from_path, to_path)
            except shutil.Error:
                if os.path.exists(to_path):
                    if os.path.isdir(to_path):
                        shutil.rmtree(to_path)
                    else:
                        os.remove(to_path)
                shutil.move(from_path, to_path)
    try:
        os.rmdir(src)
    except Exception:
        pass
