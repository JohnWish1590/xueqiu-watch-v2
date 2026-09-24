# -*- coding: utf-8 -*-
"""临时诊断：定位 user_timeline 返回 HTTP 400 的真因。

在 NAS 上运行（需真实 cookie）。对比矩阵：
  base = api / www / bare
  referer = https://xueqiu.com/ (旧) vs https://<base host>/ (新，跟随域名)
  path = /v4/statuses/user_timeline.json vs 旧版 /statuses/user_timeline.json
并额外探测 groups.json（判断 cookie 是否有效）。
"""
import urllib.request
import urllib.error
import json

import cookies_store
import xueqiu

ck = cookies_store.load_cookies()
xq = (ck.get('xueqiu') or {}).get('header') or ''
print('cookie_len=%d' % len(xq))
print('cookie_has_token=%s' % ('xq_a_token' in xq))
print('-' * 70)

UID = '9650668145'  # 管我财（特别关注成员之一）
BASES = [xueqiu.XQ_API, xueqiu.XQ_WWW, xueqiu.XQ_BASE]
V4 = '/v4/statuses/user_timeline.json?user_id=%s&page=1&count=10' % UID
OLD = '/statuses/user_timeline.json?user_id=%s&page=1&count=10' % UID


def probe(url, referer):
    req = urllib.request.Request(url)
    req.add_header('Cookie', xq)
    req.add_header('User-Agent', xueqiu.UA)
    req.add_header('Accept', 'application/json, text/plain, */*')
    req.add_header('Accept-Language', 'zh-CN,zh;q=0.9')
    req.add_header('Accept-Encoding', 'identity')
    req.add_header('Referer', referer)
    req.add_header('X-Requested-With', 'XMLHttpRequest')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode('utf-8', 'ignore')
            return 'OK %s' % r.status, body[:220]
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8', 'ignore')
        except Exception:
            body = '<no body>'
        return 'HTTP %s' % e.code, body[:220]
    except Exception as e:
        return 'ERR %s' % type(e).__name__, str(e)[:220]


print('=== user_timeline 矩阵 ===')
for b in BASES:
    for label, path in (('v4', V4), ('old', OLD)):
        for rlabel, ref in (('ref_bare', 'https://xueqiu.com/'),
                            ('ref_same', b + '/')):
            st, body = probe(b + path, ref)
            print('%-28s %-4s %-9s -> %s | %s'
                  % (b, label, rlabel, st, body.replace('\n', ' ')))

print('-' * 70)
print('=== groups.json（判断 cookie 是否有效）===')
for b in BASES:
    st, body = probe(b + '/friendships/groups.json', b + '/')
    print('%-28s -> %s | %s' % (b, st, body.replace('\n', ' ')[:180]))
