import os
import time
import threading
import docker
import re

WRAPPER_IMAGE = 'alacarte-wrapper:local'
WRAPPER_CONTAINER = 'alacarte-wrapper'
TEMP_CONTAINER = 'alacarte-wrapper-login'
WRAPPER_2FA_PATH = '/app/rootfs/data/data/com.apple.android.music/files/2fa.txt'
WRAPPER_DATA_IN_WEB = '/wrapper-data'

_active_login = None
_active_login_lock = threading.Lock()
_hard_block_reason = None

def get_docker_client():
    try:
        return docker.from_env()
    except Exception:
        return None

def is_docker_reachable():
    client = get_docker_client()
    if not client:
        return False
    try:
        client.ping()
        return True
    except Exception:
        return False

def get_hard_block():
    return _hard_block_reason

def clear_hard_block():
    global _hard_block_reason
    _hard_block_reason = None

def get_login_status():
    with _active_login_lock:
        if not _active_login:
            return {'inProgress': False}
        return {
            'inProgress': True,
            'status': _active_login['status']
        }

def emit_status(patch):
    with _active_login_lock:
        if not _active_login:
            return
        _active_login['status'].update(patch)
        _active_login['status']['ts'] = int(time.time() * 1000)
        from .queue_manager import emit_event
        emit_event('wrapper.login', _active_login['status'])

def extract_wrapper_failure_reason(collected_log):
    lines = [l.strip() for l in collected_log.split('\n') if l.strip()]
    
    # Check for dialogHandler printouts from the binary
    # E.g. [.] dialogHandler: {title: "...", message: "..."}
    dialog_re = re.compile(r'^\[\.\]\s*dialogHandler:\s*\{title:\s*(.*?),\s*message:\s*(.*?)\}$', re.IGNORECASE)
    
    for line in reversed(lines):
        m = dialog_re.match(line)
        if m:
            title = m.group(1).strip().strip('"').strip("'")
            message = m.group(2).strip().strip('"').strip("'")
            if not title or title.lower() == 'sign in':
                continue
            if 'disabled' in title.lower():
                return f"Your Apple Account is disabled. {message or 'Reset it at iforgot.apple.com, then try again.'}"
            if 'account information' in title.lower():
                return "Apple rejected the email or password. Double-check both and try again."
            if 'locked' in title.lower():
                return f"Apple Account locked. {message or 'Reset it at iforgot.apple.com before retrying.'}"
            if 'billing' in title.lower() or 'payment' in title.lower():
                return f"Apple Music sign-in needs attention: {title}. {message}"
            
            joined = " — ".join(filter(None, [title, message]))
            if joined:
                return joined[:240]
                
    for line in reversed(lines):
        if '[!] Failed to get 2FA Code' in line:
            return "2FA code wasn’t entered in time. Try again."
            
    return None

def run_login_worker(email, password, client):
    global _active_login, _hard_block_reason
    
    emit_status({'phase': 'preparing', 'message': 'Stopping active wrapper container'})
    
    # 1. Capture Wrapper Spec
    spec = {'bind': None, 'network': None, 'labels': None}
    try:
        c = client.containers.get(WRAPPER_CONTAINER)
        info = c.attrs
        mounts = info.get('Mounts', [])
        bind = None
        for m in mounts:
            if m.get('Destination') == '/app/rootfs/data':
                bind = f"{m.get('Source')}:/app/rootfs/data"
                break
        spec['bind'] = bind
        spec['network'] = info.get('HostConfig', {}).get('NetworkMode')
        spec['labels'] = info.get('Config', {}).get('Labels')
        
        # Stop and remove container
        emit_status({'phase': 'preparing', 'message': 'Removing active wrapper container'})
        c.stop(timeout=3)
        c.remove(force=True)
    except Exception as e:
        print(f"Failed to capture wrapper spec: {e}")
        
    # Determine bind fallback
    if not spec['bind']:
        try:
            # Fallback from web container mounts
            hostname = os.environ.get('HOSTNAME')
            if hostname:
                self_c = client.containers.get(hostname)
                self_mounts = self_c.attrs.get('Mounts', [])
                for sm in self_mounts:
                    if sm.get('Destination') == WRAPPER_DATA_IN_WEB:
                        spec['bind'] = f"{sm.get('Source')}:/app/rootfs/data"
                        break
        except Exception:
            pass
            
    if not spec['bind']:
        emit_status({
            'phase': 'failed',
            'error': 'Cannot locate wrapper data volume — start the stack with docker compose first'
        })
        with _active_login_lock:
            _active_login = None
        return

    binds = [spec['bind']] + [
        '/dev/null:/app/rootfs/dev/null',
        '/dev/urandom:/app/rootfs/dev/urandom',
        '/dev/random:/app/rootfs/dev/random',
        '/dev/zero:/app/rootfs/dev/zero'
    ]

    emit_status({'phase': 'creating', 'message': 'Starting login container'})
    
    # 2. Start Login Container
    login_container = None
    try:
        login_arg = f"{email}:{password}"
        login_container = client.containers.create(
            image=WRAPPER_IMAGE,
            # Execute with original parameters: -L username:password -F -H 0.0.0.0
            command=['-L', login_arg, '-F', '-H', '0.0.0.0'],
            name=TEMP_CONTAINER,
            entrypoint=['/app/wrapper'],
            environment={'LD_PRELOAD': '/app/libwrapper_strtok_fix.so'},
            network=spec['network'] or 'alacarte-net',
            volumes=binds,
            labels=spec['labels'],
            ports={'10020/tcp': None, '20020/tcp': None, '30020/tcp': None}
        )
        
        with _active_login_lock:
            if not _active_login:
                login_container.remove(force=True)
                restore_daemon_wrapper(client, spec)
                return
            _active_login['container'] = login_container
            
        login_container.start()
    except Exception as e:
        emit_status({'phase': 'failed', 'error': f"Failed to start login container: {e}"})
        restore_daemon_wrapper(client, spec)
        with _active_login_lock:
            _active_login = None
        return

    # 3. Read logs from container
    try:
        emit_status({'phase': 'signing-in', 'message': 'Authenticating with Apple...'})
        logs_generator = login_container.logs(stdout=True, stderr=True, stream=True)
        
        collected_log = ""
        success = False
        two_fa = False
        error_msg = None
        
        for log_line in logs_generator:
            line = log_line.decode('utf-8', errors='replace')
            # Redact credentials
            redacted = line.replace(email, '[redacted]').replace(password, '[redacted]')
            collected_log += redacted
            
            # Print to stdout for web logs inspection
            print(f"[wrapper-login-container] {redacted.strip()}")
            
            # Emit live log line to client
            from .queue_manager import emit_event
            emit_event('wrapper.login.log', {'line': redacted.strip()})
            
            # Match 2FA prompt
            if not two_fa and '[!] Enter your 2FA code into rootfs' in redacted:
                two_fa = True
                emit_status({'phase': '2fa-required', 'message': 'Enter the 2FA code sent to your Apple device.'})
                
            # Match success
            if 'account info cached successfully' in redacted.lower():
                success = True
                break
                
        if success:
            emit_status({'phase': 'success', 'message': 'Apple Sign-In successful!'})
        else:
            reason = extract_wrapper_failure_reason(collected_log)
            if reason and ('disabled' in reason.lower() or 'locked' in reason.lower()):
                _hard_block_reason = reason
            emit_status({'phase': 'failed', 'error': reason or 'Sign-in failed'})
            
    except Exception as e:
        emit_status({'phase': 'failed', 'error': str(e)})
    finally:
        # 4. Clean up and Restore
        try:
            login_container.stop(timeout=2)
            login_container.remove(force=True)
        except Exception:
            pass
        restore_daemon_wrapper(client, spec)
        with _active_login_lock:
            _active_login = None

def restore_daemon_wrapper(client, spec):
    emit_status({'phase': 'restoring', 'message': 'Restarting decryption daemon'})
    try:
        binds = [spec['bind']] + [
            '/dev/null:/app/rootfs/dev/null',
            '/dev/urandom:/app/rootfs/dev/urandom',
            '/dev/random:/app/rootfs/dev/random',
            '/dev/zero:/app/rootfs/dev/zero'
        ]
        
        client.containers.run(
            image=WRAPPER_IMAGE,
            command=['-H', '0.0.0.0'],
            name=WRAPPER_CONTAINER,
            entrypoint=['/app/wrapper'],
            environment={'LD_PRELOAD': '/app/libwrapper_strtok_fix.so'},
            network=spec['network'] or 'alacarte-net',
            volumes=binds,
            labels=spec['labels'],
            ports={'10020/tcp': None, '20020/tcp': None, '30020/tcp': None},
            detach=True,
            restart_policy={'Name': 'on-failure'}
        )
    except Exception as e:
        print(f"Failed to restore daemon: {e}")

def start_wrapper_login(email, password):
    global _active_login
    client = get_docker_client()
    if not client:
        return False
        
    with _active_login_lock:
        if _active_login:
            return False
        _active_login = {
            'email': email,
            'password': password,
            'two_fa_code': None,
            'status': {'phase': 'preparing', 'ts': int(time.time() * 1000)}
        }
        
    t = threading.Thread(target=run_login_worker, args=(email, password, client), daemon=True)
    t.start()
    return True

def submit_2fa(code):
    with _active_login_lock:
        if not _active_login:
            return False
        container = _active_login.get('container')
        if not container:
            return False
            
        try:
            safe_code = re.sub(r'\D', '', code)[:8]
            # Write 2fa.txt inside container
            exec_res = container.exec_run(
                ['sh', '-c', f'printf %s "{safe_code}" > "{WRAPPER_2FA_PATH}"']
            )
            print(f"[wrapper-login] 2FA write exit={exec_res.exit_code}")
            emit_status({'phase': 'signing-in', 'message': 'Submitting 2FA code...'})
            return exec_res.exit_code == 0
        except Exception as e:
            print(f"Failed to write 2FA from request thread: {e}")
            return False

def cancel_login():
    global _active_login
    with _active_login_lock:
        if not _active_login:
            return False
        container = _active_login.get('container')
        if container:
            try:
                container.stop(timeout=1)
                container.remove(force=True)
            except Exception:
                pass
        _active_login = None
    return True
