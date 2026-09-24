# -*- coding: utf-8 -*-
"""临时诊断2：①探公开接口证明网络/请求头没问题（区分「鉴权失败」与「被墙/网络」）
②打印 xw.log 末尾，确认错误图是否已推送。
"""
import urllib.request
import urllib.error

import xueqiu

PUBLIC = '/v5/stock/quote.json?symbol=SH600519&extend=detail'
print('=== 公开接口（无需登录）===')
for b in (xueqiu.XQ_API, xueqiu.XQ_WWW, xueqiu.XQ_BASE):
    url = b + PUBLIC
    req = urllib.request.Request(url)
    req.add_header('User-Agent', xueqiu.UA)
    req.add_header('Accept', 'application/json, text/plain, */*')
    req.add_header('Referer', b + '/')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode('utf-8', 'ignore')
            print('%-26s -> OK %s | %s' % (b, r.status, body[:150]))
    except urllib.error.HTTPError as e:
        print('%-26s -> HTTP %s | %s'
              % (b, e.code, e.read().decode('utf-8', 'ignore')[:150]))
    except Exception as e:
        print('%-26s -> ERR %s | %s' % (b, type(e).__name__, str(e)[:150]))

print('-' * 70)
print('=== xw.log 末尾 40 行 ===')
try:
    with open('xw.log', 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    for ln in lines[-40:]:
        print(ln.rstrip())
except Exception as e:
    print('read log failed:', e)
