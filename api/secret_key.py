import os
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
import secrets

_raw_key = None
_session_hmac_key = None
SESSION_HMAC_INFO = b'alacarte/session-hmac/v1'

def load_secrets_at_boot(config_dir):
    global _raw_key, _session_hmac_key
    if _raw_key is not None and _session_hmac_key is not None:
        return
        
    secret_file = os.path.join(config_dir, '.secret')
    if not os.path.exists(secret_file):
        # Generate random 32 bytes as 64-char hex string
        key_hex = secrets.token_hex(32)
        os.makedirs(config_dir, exist_ok=True)
        with open(secret_file, 'w', encoding='utf-8') as f:
            f.write(key_hex)
            
    with open(secret_file, 'r', encoding='utf-8') as f:
        hex_str = f.read().strip()
        
    if not re_matches_hex(hex_str):
        raise Exception(f"invalid secret key format in {secret_file}")
        
    _raw_key = bytes.fromhex(hex_str)
    
    hkdf_inst = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b'',
        info=SESSION_HMAC_INFO,
    )
    _session_hmac_key = hkdf_inst.derive(_raw_key)

def re_matches_hex(hex_str):
    import re
    return bool(re.match(r'^[0-9a-f]{64}$', hex_str, re.IGNORECASE))

def get_raw_key():
    if _raw_key is None:
        raise Exception('secret key not initialized')
    return _raw_key

def get_session_hmac_key():
    if _session_hmac_key is None:
        raise Exception('session HMAC key not initialized')
    return _session_hmac_key
