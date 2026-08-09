"""加密存储多站 Cookie（雪球 / 微博 / 其他）。
优先用 cryptography.Fernet 做真正加密；若环境装不上 cryptography，自动降级为
base64 轻量混淆并打警告（个人 NAS 本地使用可接受，但不如加密安全）。
"""
import os
import json
import base64
import threading

try:
    from cryptography.fernet import Fernet
    _HAVE_CRYPTO = True
except Exception:
    _HAVE_CRYPTO = False

_KEY_FILE = os.environ.get('XW_KEY_FILE', 'secret.key')
_STORE_FILE = os.environ.get('XW_COOKIE_STORE', 'cookies.json.enc')
_lock = threading.Lock()


def _get_key():
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, 'rb') as f:
            return f.read()
    if _HAVE_CRYPTO:
        k = Fernet.generate_key()
    else:
        k = base64.b64encode(b'xueqiu-watch-v2-local-key-pad-1234567890ab')
        print('[warn] cryptography 未安装，cookie 仅做轻量混淆而非加密存储。建议 pip install cryptography。')
    with open(_KEY_FILE, 'wb') as f:
        f.write(k)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except Exception:
        pass
    return k


def _cipher():
    if _HAVE_CRYPTO:
        return Fernet(_get_key())
    return None


def save_cookies(cookies_map: dict):
    """cookies_map: {site_key: {domain, header}}"""
    data = json.dumps({'cookies': cookies_map}, ensure_ascii=False).encode('utf-8')
    with _lock:
        if _HAVE_CRYPTO:
            tok = _cipher().encrypt(data)
        else:
            tok = base64.b64encode(data)
        with open(_STORE_FILE, 'wb') as f:
            f.write(tok)


def load_cookies():
    if not os.path.exists(_STORE_FILE):
        return {}
    with _lock:
        with open(_STORE_FILE, 'rb') as f:
            tok = f.read()
    try:
        if _HAVE_CRYPTO:
            data = _cipher().decrypt(tok)
        else:
            data = base64.b64decode(tok)
        return json.loads(data.decode('utf-8')).get('cookies', {})
    except Exception as e:
        print('[warn] 读取 cookie 失败:', e)
        return {}


if __name__ == '__main__':
    save_cookies({'xueqiu': {'domain': 'xueqiu.com', 'header': 'a=1; b=2'}})
    print(load_cookies())
