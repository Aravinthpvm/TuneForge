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

    emit_status({'phase': 'connecting', 'message': 'Starting login container'})
    
    # 2. Start Login Container
    login_container = None
    try:
        # Recreate network config
        nw_config = None
        network_mode = spec['network'] or 'alacarte-net'
        if network_mode and not network_mode.startswith('container:'):
            nw_config = {network_mode: {}}
            
        login_container = client.containers.create(
            image=WRAPPER_IMAGE,
            command=['login', email, password],
            name=TEMP_CONTAINER,
            entrypoint=['/app/wrapper'],
            environment={'LD_PRELOAD': '/app/libwrapper_strtok_fix.so'},
            network=network_mode,
            volumes=binds,
            labels=spec['labels'],
            ports={'10020/tcp': None, '20020/tcp': None, '30020/tcp': None}
        )
        
        with _active_login_lock:
            if not _active_login:
                # Cancelled before start
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

    # 3. Read logs from container to detect 2FA or success
    try:
        emit_status({'phase': 'logging-in', 'message': 'Authenticating with Apple...'})
        logs_generator = login_container.logs(stdout=True, stderr=True, stream=True)
        
        success = False
        two_fa = False
        error_msg = None
        
        for log_line in logs_generator:
            line = log_line.decode('utf-8', errors='replace').strip()
            print(f"[wrapper-login-container] {line}")
            
            # Detect 2FA prompt
            # wrapper prints "enter 2fa code" or similar when it detects 2fa
            if '2fa' in line.lower() or 'two-factor' in line.lower() or 'verification code' in line.lower():
                two_fa = True
                emit_status({'phase': '2fa', 'message': 'Enter the 2FA code sent to your Apple device.'})
                
            # Success indicator
            if 'login success' in line.lower() or 'ready' in line.lower() or 'auth success' in line.lower() or 'sign in success' in line.lower():
                success = True
                break
                
            # Failure indicator
            if 'error' in line.lower() or 'failed' in line.lower():
                error_msg = line
                
        if success:
            emit_status({'phase': 'success', 'message': 'Apple Sign-In successful!'})
        elif two_fa:
            # The code wait is handled by checking active state. If success was not set
            # but 2fa was detected, wait up to 3 minutes for user input.
            start_wait = time.time()
            while time.time() - start_wait < 180:
                with _active_login_lock:
                    if not _active_login:
                        # Cancelled
                        break
                    if _active_login.get('two_fa_code'):
                        code = _active_login['two_fa_code']
                        _active_login['two_fa_code'] = None # consume
                        
                        emit_status({'phase': 'logging-in', 'message': 'Submitting 2FA code...'})
                        # Write code via exec inside container
                        try:
                            # Run exec printf into file
                            safe_code = re.sub(r'\D', '', code)[:8]
                            exec_res = login_container.exec_run(
                                ['sh', '-c', f'printf %s "{safe_code}" > "{WRAPPER_2FA_PATH}"']
                            )
                            print(f"[wrapper-login] 2FA write exit={exec_res.exit_code}")
                        except Exception as write_err:
                            print(f"Failed to write 2FA: {write_err}")
                time.sleep(1)
                # Check if process exited or logged success
                # (Simple check is if container exited)
                try:
                    login_container.reload()
                    state_info = login_container.attrs.get('State', {})
                    if state_info.get('Running') is False:
                        exit_code = state_info.get('ExitCode')
                        if exit_code == 0:
                            success = True
                        else:
                            error_msg = f"Login container exited with code {exit_code}"
                        break
                except Exception:
                    break
        else:
            emit_status({'phase': 'failed', 'error': error_msg or 'Sign-in failed'})
            
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
        # Recreate container in daemon mode
        binds = [spec['bind']] + [
            '/dev/null:/app/rootfs/dev/null',
            '/dev/urandom:/app/rootfs/dev/urandom',
            '/dev/random:/app/rootfs/dev/random',
            '/dev/zero:/app/rootfs/dev/zero'
        ]
        
        # Pull latest compose specifications
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
        _active_login['two_fa_code'] = code
        return True

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
