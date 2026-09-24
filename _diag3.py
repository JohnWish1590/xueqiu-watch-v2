# -*- coding: utf-8 -*-
"""临时诊断3：列出已存 cookie 的「名字」（不打值，避免泄露），判断是否缺关键字段。"""
import cookies_store

ck = cookies_store.load_cookies()
header = (ck.get('xueqiu') or {}).get('header') or ''
names = []
for part in header.split(';'):
    kv = part.strip().split('=', 1)
    if kv and kv[0]:
        names.append(kv[0])

print('cookie 条数: %d' % len(names))
print('名字列表: %s' % ', '.join(names))
print('-' * 60)
KEY = ['xq_a_token', 'xqat', 'xq_r_token', 'u', 'device_id', 'xq_is_login',
       'acw_tc', 's', 'bid', 'Hm_lvt']
for k in KEY:
    print('%-14s %s' % (k, 'YES' if k in names else '-- missing'))

# 存储层元信息
meta = {k: v for k, v in ck.items() if k != 'xueqiu'}
print('-' * 60)
print('cookies_store 其他键: %s' % list(meta.keys()))
import json
print('xueqiu 键内字段: %s'
      % [k for k in (ck.get('xueqiu') or {}).keys()])
