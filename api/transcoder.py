import os
import subprocess
import shutil

def run_ffmpeg(args):
    # Hide terminal window on Windows
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    cmd = ['ffmpeg'] + args
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        stderr_output = e.stderr.decode('utf-8', errors='replace')[-500:]
        raise Exception(f"ffmpeg exit {e.returncode}: {stderr_output}")
    except FileNotFoundError:
        raise Exception("ffmpeg binary not found. Please install ffmpeg and make sure it is in your system PATH.")

def convert_to_flac(input_path, delete_original=True):
    directory = os.path.dirname(input_path)
    filename = os.path.basename(input_path)
    name, _ = os.path.splitext(filename)
    out_path = os.path.join(directory, f"{name}.flac")
    
    args = [
        '-y',
        '-i', input_path,
        '-map', '0',
        '-map_metadata', '0',
        '-c:a', 'flac',
        '-compression_level', '8',
        '-c:v', 'copy',
        '-disposition:v:0', 'attached_pic',
        '-metadata', 'encoder=FLAC',
        out_path
    ]
    
    run_ffmpeg(args)
    
    if delete_original:
        try:
            os.remove(input_path)
        except Exception:
            pass
            
    return out_path

def collect_input_files(directory):
    files_list = []
    for root, dirs, files in os.walk(directory):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in ('.m4a', '.alac'):
                files_list.append(os.path.join(root, f))
    return files_list

def convert_dir_to_flac(directory, delete_original=True, on_progress=None):
    files = collect_input_files(directory)
    total = len(files)
    converted = 0
    failed = 0
    
    for i, p in enumerate(files):
        try:
            convert_to_flac(p, delete_original=delete_original)
            converted += 1
        except Exception as err:
            print(f"FLAC convert failed for {p}: {err}")
            failed += 1
            
        if on_progress:
            try:
                on_progress(p, i + 1, total)
            except Exception:
                pass
                
    return {
        'converted': converted,
        'failed': failed,
        'total': total
    }

def extract_folder_art(directory, size=1000):
    target = os.path.join(directory, 'folder.jpg')
    if os.path.exists(target):
        return target
        
    try:
        entries = os.listdir(directory)
    except Exception:
        return None
        
    audio = None
    for entry in entries:
        full_path = os.path.join(directory, entry)
        if os.path.isfile(full_path):
            _, ext = os.path.splitext(entry.lower())
            if ext in ('.flac', '.m4a', '.mp3'):
                audio = full_path
                break
                
    if not audio:
        return None
        
    try:
        args = [
            '-y',
            '-i', audio,
            '-an',
            '-vcodec', 'mjpeg',
            '-vf', f"scale='min({size},iw)':-1",
            target
        ]
        run_ffmpeg(args)
        return target if os.path.exists(target) else None
    except Exception as err:
        print(f"folder.jpg extraction failed: {err}")
        return None
