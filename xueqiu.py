"""雪球 timeline 抓取：用浏览器导出的 Cookie 头调用雪球 API，解析成统一帖子结构。"""
import json
import re
import time
import urllib.request
import urllib.error
import urllib.parse

XQ_BASE = 'https://xueqiu.com'
# 关键：api.xueqiu.com 子域不挂阿里云 WAF，服务器/NAS 直连可用；
# 主域 xueqiu.com 的 /v4/statuses/* 会被 WAF JS 挑战拦截（返回 HTML 而非 JSON）。
XQ_API = 'https://api.xueqiu.com'
XQ_WWW = 'https://www.xueqiu.com'

# 双域兜底候选（与 Chrome 扩展 v1.5.17 的「同域重写 + 双域回退」思路一致）：
# 雪球会在裸域 / www / api 子域之间切换，任一可用即返回。
# 顺序：api 子域（无 WAF，首选）→ www → 裸域。
SPECIAL_BASES = [XQ_API, XQ_WWW, XQ_BASE]

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


class WafBlocked(Exception):
    """请求被阿里云 WAF 挑战页拦截（返回的不是 JSON）。"""


class XueqiuError(Exception):
    """雪球接口返回错误（消息里带 error_code，如 400016 = 登录态失效）。"""


LAST_ERROR = ''   # 最近一次请求失败的原因（含 error_code），供主程序在墨水屏上显示


def _err_detail(body):
    """从错误响应体里抽出 error_code + error_description，便于一眼定位。"""
    try:
        j = json.loads(body)
    except Exception:
        return ''
    if isinstance(j, dict) and j.get('error_code'):
        return '%s %s' % (j['error_code'], j.get('error_description') or '')
    return ''


def _is_waf(body):
    if not body:
        return False
    head = body.lstrip()[:1]
    if head in ('[', '{'):
        return False
    return ('aliyun_waf' in body) or ('renderData' in body) or head == '<'


def _req(url, cookie_header, timeout=12):
    global LAST_ERROR
    req = urllib.request.Request(url)
    req.add_header('Cookie', cookie_header)
    req.add_header('User-Agent', UA)
    req.add_header('Accept', 'application/json, text/plain, */*')
    req.add_header('Accept-Language', 'zh-CN,zh;q=0.9')
    req.add_header('Accept-Encoding', 'identity')
    req.add_header('Referer', 'https://%s/' % urllib.parse.urlparse(url).netloc)
    req.add_header('X-Requested-With', 'XMLHttpRequest')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode('utf-8', 'ignore')
        except Exception:
            raw = ''
        detail = _err_detail(raw)
        LAST_ERROR = 'HTTP %s%s' % (e.code, (' ' + detail) if detail else '')
        raise XueqiuError(LAST_ERROR)
    if _is_waf(body):
        LAST_ERROR = '被 WAF 挑战页拦截'
        raise WafBlocked(url)
    LAST_ERROR = ''
    return body


def _extract_list(data):
    try:
        d = json.loads(data)
    except Exception:
        return []
    if isinstance(d, list):
        return d
    for k in ('statuses', 'list', 'items', 'data'):
        v = d.get(k)
        if isinstance(v, list):
            return v
    return []


def _strip(html):
    if not html:
        return ''
    t = re.sub(r'<[^>]+>', '', html or '')
    return t.replace('&nbsp;', ' ').replace('\r', ' ').replace('\n', ' ').strip()


def _ts(s):
    if not s:
        return 0
    try:
        return int(s)
    except Exception:
        pass
    try:
        return time.mktime(time.strptime(s, '%a %b %d %H:%M:%S %z %Y'))
    except Exception:
        return 0


def fetch_user_timeline(user_id, cookie_header, page=1, count=10, since_id=None):
    since = f'&since_id={since_id}' if since_id else ''
    path = f'/v4/statuses/user_timeline.json?user_id={user_id}&page={page}&count={count}{since}'
    # 依次尝试：api 子域（无 WAF，首选）→ 主域 v4 → 主域旧版
    candidates = [
        XQ_API + path,
        XQ_BASE + path,
        f'{XQ_BASE}/statuses/user_timeline.json?user_id={user_id}&page={page}&count={count}{since}',
    ]
    posts = None
    last_err = None
    for u in candidates:
        try:
            posts = _extract_list(_req(u, cookie_header))
            break
        except WafBlocked:
            last_err = 'WAF'
            continue
        except Exception as e:
            last_err = e
            continue
    if posts is None:
        print('  [xueqiu] fetch error', user_id, last_err)
        return []
    out = []
    for p in posts:
        user = (p.get('user') or {}).get('screen_name', '')
        out.append({
            'id': p.get('id'),
            'user': user,
            'text': _strip(p.get('text', '')),
            'created_at': p.get('created_at', ''),
            'time': _ts(p.get('created_at')),
        })
    return out


def resolve_user_ids(entries, cookie_header):
    """把 followed_user_ids 里非数字的项当作雪球昵称，用 cookie 解析成数字 id。
    解析失败则保留原值（数字原样、昵称留待下轮 cookie 就绪后再试）。"""
    out = []
    for e in entries:
        s = str(e)
        if s.isdigit():
            out.append(s)
            continue
        try:
            import urllib.parse
            q = urllib.parse.quote(s)
            sub = f'/query/v1/search/user.json?q={q}&count=5'
            data = None
            for u in (XQ_API + sub, XQ_BASE + sub):
                try:
                    data = _req(u, cookie_header)
                    break
                except Exception:
                    continue
            if data is None:
                raise RuntimeError('search endpoints all failed')
            d = json.loads(data)
            users = d.get('list') or []
            hit = None
            for u in users:
                if u.get('screen_name') == s or s in (u.get('screen_name') or ''):
                    hit = u
                    break
            if not hit and users:
                hit = users[0]
            if hit and hit.get('id'):
                print(f'  [resolve] {s} -> {hit["id"]} ({hit.get("screen_name")})')
                out.append(str(hit['id']))
                continue
        except Exception as ex:
            print(f'  [resolve] 昵称 {s} 解析失败: {ex}')
        out.append(s)
    return out


def fetch_all(followed_ids, cookie_header):
    resolved = resolve_user_ids(followed_ids, cookie_header)
    posts = []
    seen = set()
    for uid in resolved:
        if not str(uid).isdigit():
            continue  # 没解析出的昵称本轮跳过
        for pg in (1, 2):
            for p in fetch_user_timeline(uid, cookie_header, page=pg):
                if p['id'] and p['id'] not in seen:
                    seen.add(p['id'])
                    posts.append(p)
    posts.sort(key=lambda x: x['time'], reverse=True)
    return posts


def fetch_group_members_all(gid, cookie_header):
    """分页拉取「特别关注」分组全部成员。

    雪球 friendships/groups/members.json 默认每页约 20 人，分组超过 20 人时
    只取第 1 页会漏掉后排成员（与雪哨 Chrome 扩展 v1.5.3 修复的同一 bug）。
    这里照搬扩展版 fetchGroupMembersAll 的逻辑：按 page 翻页，每页 20，
    到末页（空响应）或响应里的 maxPage 即停，最多 50 页（≈1000 人）。

    返回原始成员 dict 列表（未归一化）；失败/无成员返回 []。
    """
    all_users = []
    seen_ids = set()
    for page in range(1, 51):  # 最多 50 页
        raw = None
        for base in SPECIAL_BASES:
            try:
                raw = _req(
                    f'{base}/friendships/groups/members.json'
                    f'?gid={gid}&page={page}&count=20',
                    cookie_header,
                )
                break
            except WafBlocked:
                continue
            except Exception:
                continue
        if raw is None:
            print(f'  [special] members 第 {page} 页所有域名均失败')
            break
        try:
            d = json.loads(raw)
        except Exception as e:
            print(f'  [special] members 第 {page} 页响应非 JSON: {e}')
            break
        # 兼容多种响应形态
        if isinstance(d, list):
            users, meta = d, {}
        elif isinstance(d, dict):
            users, meta = d.get('users') or [], d
        else:
            users, meta = [], {}
        # 嵌套 {groups:[{users:[...]}]} 形态
        if not users and isinstance(meta, dict):
            for mg in (meta.get('groups') or []):
                if isinstance(mg.get('users'), list) and mg['users']:
                    users = mg['users']
                    break
        if not users:
            break  # 末页（空）→ 停止翻页
        for u in users:
            uid = str(u.get('id') or '')
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                all_users.append(u)
        # 命中 maxPage 即停
        max_page = meta.get('maxPage') if isinstance(meta, dict) else None
        if max_page and page >= int(max_page):
            break
    return all_users


def fetch_special_follow(cookie_header):
    """从雪球「特别关注」分组自动拉取成员列表。
    复用原版 Chrome 扩展 background.js getSpecialFollowUsers() 的完全相同逻辑：
      1) /friendships/groups.json → 找 special=true 或名含「特别关注」的分组
      2) 分组内嵌 users 则直接用，否则调 /friendships/groups/members.json?gid=xxx
    返回 [{id: '数字字符串', name: '昵称'}, ...]；失败返回 []。
    """
    raw = None
    last_err = None
    for base in SPECIAL_BASES:
        try:
            raw = _req(f'{base}/friendships/groups.json', cookie_header)
            break
        except WafBlocked:
            last_err = 'WAF@%s' % base
            continue
        except Exception as e:
            last_err = '%s: %s' % (base, e)
            continue
    if raw is None:
        print('  [special] groups.json 所有域名均失败：', last_err)
        return []
    try:
        data = json.loads(raw)
    except Exception as e:
        print('  [special] groups.json 响应非 JSON：', e)
        return []

    # 登录后返回顶层数组 [group, ...]，未登录返回 {error_code: 400016}
    groups = data if isinstance(data, list) else []
    if not groups and isinstance(data, dict):
        groups = data.get('groups') or []
        if data.get('error_code'):
            print('  [special] 未登录或登录态失效（error_code=%s）' % data['error_code'])
            return []

    # 定位「特别关注」分组
    target = None
    for g in groups:
        name = (g.get('name') or '').lower()
        if g.get('special') or '特别关注' in name or 'special' in name:
            target = g
            break
    if not target:
        print('  [special] 未找到「特别关注」分组（可用分组：%s）' %
              ', '.join((g.get('name') or '?') for g in groups))
        return []

    # 部分账号分组对象内嵌 users
    users = target.get('users')
    if isinstance(users, list) and users:
        return [_norm_user(u) for u in users]

    # 否则分页拉取全部成员（修复：members.json 默认每页 ~20 人，
    # 只取第 1 页会漏掉 >20 人的分组后排成员——与雪哨 Chrome 扩展 v1.5.3 修的同一 bug）
    gid = target.get('id')
    if not gid:
        print('  [special] 分组缺少 id 字段，无法拉成员')
        return []
    raw_users = fetch_group_members_all(gid, cookie_header)
    return [_norm_user(u) for u in raw_users]


def _norm_user(u):
    """统一成员格式：{id: 字符串数字ID, name: 昵称}"""
    return {
        'id': str(u.get('id') or ''),
        'name': u.get('screen_name') or u.get('name') or '',
    }


if __name__ == '__main__':
    # 仅作结构自测：需要有效 cookie 才能真抓
    print('xueqiu module loaded')
