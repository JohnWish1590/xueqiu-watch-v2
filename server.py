"""Cookie 接收 + 帖子只读服务（纯标准库 http.server，零额外依赖）。

随 main.py 在后台守护线程启动，端口取自 config.server_port（默认 8080）。

接口：
    POST /api/set-cookies   接收「Cookie 管家」导出的 cookie 并加密存储
                           ★ 仅允许内网/回环来源（经 Cloudflare 隧道时，
                             隧道 ingress 应只放行 /api/posts，故不会暴露此接口）
    GET  /api/posts        返回最近抓到的帖子（手机 APP 轮询用，可公开）
                           ?limit=20 限制条数
    GET  /api/health       健康检查
"""
import json
import os
import time
import ipaddress
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cookies_store
import xueqiu
import main as main_module  # 复用 resolve_followed / save_group_cache 等

# 主线程每轮轮询后写入；本模块 HTTP 处理读取它返回给 APP。
recent_posts = []


def set_recent_posts(posts):
    global recent_posts
    # 只保留最近 100 条，避免内存无限增长
    recent_posts = list(posts)[:100]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_private(self):
        """判断请求来源是否为内网/回环（公网来源返回 False）。"""
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except Exception:
            return False

    def do_GET(self):
        path = self.path.split('?', 1)[0].rstrip('/')
        if path in ('', '/api/health', '/health'):
            self._send(200, {
                'ok': True,
                'service': 'xueqiu-watch-v2',
                'has_cookies': bool(cookies_store.load_cookies()),
                'posts': len(recent_posts),
            })
        elif path == '/api/posts':
            # 公开只读接口（经隧道暴露给手机）
            limit = 20
            if '?' in self.path:
                try:
                    limit = int(parse_qs(self.path.split('?', 1)[1]).get('limit', [20])[0])
                except Exception:
                    pass
            limit = max(1, min(limit, 100))
            out = [{
                'id': p.get('id'),
                'user': p.get('user', ''),
                'text': p.get('text', ''),
                'created_at': p.get('created_at', ''),
                'time': p.get('time', 0),
            } for p in recent_posts[:limit]]
            self._send(200, {
                'posts': out,
                'count': len(out),
                'updated_at': int(time.time()),
            })
        elif path == '/api/refresh-group':
            # ★ 仅允许内网来源（跟 set-cookies 一样，涉及 cookie 操作）
            if not self._client_private():
                self._send(403, {'ok': False, 'error': 'refresh-group only allowed from private/LAN network'})
                return
            cookies = cookies_store.load_cookies()
            xq = (cookies.get('xueqiu') or {}).get('header')
            if not xq:
                self._send(400, {'ok': False, 'error': 'no xueqiu cookie — please export from Cookie Manager first'})
                return
            users = xueqiu.fetch_special_follow(xq)
            if not users:
                self._send(200, {'ok': True, 'users': [], 'error': '未找到特别关注分组或分组为空（可能 cookie 过期）'})
                return
            main_module.save_group_cache(users)
            self._send(200, {
                'ok': True,
                'count': len(users),
                'users': users,
                'message': f'已刷新并缓存 {len(users)} 人',
            })
        else:
            self._send(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        if self.path.split('?', 1)[0].rstrip('/') != '/api/set-cookies':
            self._send(404, {'ok': False, 'error': 'not found'})
            return
        # ★ 仅允许内网/回环来源写入 cookie
        if not self._client_private():
            self._send(403, {'ok': False, 'error': 'set-cookies only allowed from private/LAN network'})
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            payload = json.loads(raw.decode('utf-8', 'ignore'))
        except Exception as e:
            self._send(400, {'ok': False, 'error': f'bad json: {e}'})
            return
        cookies = payload.get('cookies')
        if not isinstance(cookies, dict) or not cookies:
            self._send(400, {'ok': False, 'error': 'missing cookies map'})
            return
        try:
            cookies_store.save_cookies(cookies)
            self._send(200, {'ok': True, 'sites': list(cookies.keys())})
        except Exception as e:
            self._send(500, {'ok': False, 'error': str(e)})

    def log_message(self, fmt, *args):
        print('[server]', fmt % args)


def start_receiver(port=8080):
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'[server] 监听 0.0.0.0:{port}  (POST /api/set-cookies, GET /api/posts, GET /api/refresh-group)')
    srv.serve_forever()
    return srv


if __name__ == '__main__':
    start_receiver(int(os.environ.get('XW_PORT', 8080)))
