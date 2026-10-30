"""雪球 timeline 抓取：用浏览器导出的 Cookie 头调用雪球 API，解析成统一帖子结构。

v2.5.0 防风控策略（针对 v2.4.0「仍被风控」的复盘）：
  1) 默认只走「合并 home_timeline」（每轮 ~3 次请求）。v2.4.0 验证过：这 3 次请求能
     覆盖特别关注成员的新帖（他们在你关注流里），无需再逐个 user_timeline。
  2) user_timeline 是直接暴露「爬虫特征」的端点（v2.3.0 用它 N 人×2 页 → 账号被 567
     锁定，连 home_timeline 都被拦）。v2.5.0 曾因此整体关闭深扫；v2.5.1 折中恢复为
     保守开启：每轮只查 1 人、仅合并流不足时才发请求，从源头把请求量压到极低。
  3) 熔断退避拉长到 8 小时（首触 60 分钟）：风控惩罚窗口以小时计，短退避只会反复撞墙
     把标记锁死。冷却期内整轮跳过，连续成功才复位。
  4) 翻页间 +0.6~1.6s 随机停顿；主循环轮询间隔 ±30% 抖动——消除「固定节拍」这种机器
     特征。
"""
import json
import re
import time
import os
import random
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
    """请求被雪球风控/访问提示页拦截（HTTP 567/429/503 或返回 HTML 而非 JSON）。

    上层捕获后应触发熔断并停止本轮，不要再试其他域/用户——回退只会再撞一次墙。
    """


class XueqiuError(Exception):
    """雪球接口返回错误（消息里带 error_code，如 400016 = 登录态失效）。"""


LAST_ERROR = ''   # 最近一次请求失败的原因（含 error_code），供主程序在墨水屏上显示


# ─────────────────────────────────────────────────────────────────────────────
# 风控熔断（防止高频轮询触发「访问被阻断」/ 567）
# 策略：识别阻断 → 立即熔断（停止本轮 + 指数退避）→ 冷却期内跳过轮询 → 连续成功才复位。
# 状态持久化到文件，跨进程重启仍有效（NAS 进程偶尔会被 Task Scheduler 重启）。
# ─────────────────────────────────────────────────────────────────────────────
_RL_FILE = os.environ.get(
    'XW_RL',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rl_state.json'),
)
_rl_state = None


def rl_load():
    global _rl_state
    if _rl_state is not None:
        return _rl_state
    try:
        with open(_RL_FILE, 'r', encoding='utf-8') as f:
            _rl_state = json.load(f)
    except Exception:
        _rl_state = {'blockedUntil': 0, 'retryCount': 0}
    return _rl_state


def rl_save():
    global _rl_state
    try:
        with open(_RL_FILE, 'w', encoding='utf-8') as f:
            json.dump(_rl_state, f)
    except Exception:
        pass


def rl_is_blocked():
    """是否处于熔断冷却期（冷却期内主程序应跳过本轮，不再发请求）。"""
    s = rl_load()
    return (time.time() * 1000) < s.get('blockedUntil', 0)


def rl_backoff_min(retry_count):
    """退避时长（分钟）。

    风控惩罚窗口以「小时」计（账号/会话级 567 拦截，不是几分钟内能解的），
    所以退避必须足够长：第 1 次熔断暂停 60 分钟，之后一律 480 分钟（8 小时），
    直到连续成功才复位。绝不再用 5~30 分钟的短退避去反复撞墙——那只会把
    风控标记越锁越死（v2.4.0 的教训）。
    """
    if int(retry_count) <= 1:
        return 60
    return 480


def rl_trip(reason):
    """触发熔断：retryCount 递增，冷却期指数增长，并持久化。返回冷却分钟数。"""
    s = rl_load()
    s['retryCount'] = int(s.get('retryCount', 0)) + 1
    mins = rl_backoff_min(s['retryCount'])
    s['blockedUntil'] = int(time.time() * 1000) + mins * 60 * 1000
    rl_save()
    print('[rl] 熔断触发：%s → 暂停 %d 分钟（第 %d 次）' % (reason, mins, s['retryCount']))
    return mins


def rl_reset():
    """完整跑通一轮（未被风控中断）→ 复位退避计数。"""
    s = rl_load()
    if int(s.get('retryCount', 0)) == 0 and int(s.get('blockedUntil', 0)) == 0:
        return
    s['retryCount'] = 0
    s['blockedUntil'] = 0
    rl_save()
    print('[rl] 熔断解除：连续成功，重置退避计数')


def _err_detail(body):
    """从错误响应体里抽出 error_code + error_description，便于一眼定位。"""
    try:
        j = json.loads(body)
    except Exception:
        return ''
    if isinstance(j, dict) and j.get('error_code'):
        return '%s %s' % (j['error_code'], j.get('error_description') or '')
    return ''


def looks_like_block(text):
    """阻断页特征（雪球 WAF 页面关键词，中英文都覆盖；只看开头 2KB 足够）。"""
    if not text or not isinstance(text, str):
        return False
    head = text[:2000]
    return bool(re.search(
        r'访问被阻断|安全威胁|被阻断|访问已被拦截|访问提示|很抱歉|'
        r'has been blocked|access denied|blocked by|forbidden',
        head, re.I))


def _is_waf(body):
    """返回的不是 JSON（HTML 风控页）。"""
    if not body:
        return False
    head = body.lstrip()[:1]
    if head in ('[', '{'):
        return False
    return ('aliyun_waf' in body) or ('renderData' in body) or head == '<'


def _req(url, cookie_header, timeout=12):
    """统一请求入口。风控/限流（567/429/503/访问提示页）一律抛 WafBlocked，由上层熔断。"""
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
        # 429/503/567 或响应体是阻断页 → 风控拦截，直接熔断
        if e.code in (429, 503, 567) or looks_like_block(raw):
            LAST_ERROR = 'HTTP %s 风控拦截' % e.code
            raise WafBlocked('HTTP %s' % e.code)
        detail = _err_detail(raw)
        LAST_ERROR = 'HTTP %s%s' % (e.code, (' ' + detail) if detail else '')
        raise XueqiuError(LAST_ERROR)
    if _is_waf(body) or looks_like_block(body):
        LAST_ERROR = '被风控/访问提示页拦截'
        raise WafBlocked(url)
    LAST_ERROR = ''
    return body


def _extract_list(data):
    """从接口响应里抽出帖子数组，兼容多种返回形态。"""
    try:
        d = json.loads(data)
    except Exception:
        return []
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ('statuses', 'list', 'items', 'home_timeline'):
            v = d.get(k)
            if isinstance(v, list):
                return v
        sub = d.get('data')
        if isinstance(sub, dict):
            for k in ('statuses', 'list', 'items', 'home_timeline'):
                v = sub.get(k)
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


def _norm_post(p):
    """统一帖子格式：{id, user(昵称), user_id, text, created_at, time}。"""
    u = p.get('user') or {}
    uid = str(u.get('id') if isinstance(u, dict) else p.get('user_id') or '')
    name = (u.get('screen_name') or u.get('name') or '') if isinstance(u, dict) else ''
    return {
        'id': p.get('id'),
        'user': name,
        'user_id': uid,
        'text': _strip(p.get('text', '')),
        'created_at': p.get('created_at', ''),
        'time': _ts(p.get('created_at')),
    }


def fetch_user_timeline(user_id, cookie_header, page=1, count=10, since_id=None):
    """直查单用户时间线（兜底深扫用）。命中风控时抛 WafBlocked（向上熔断，不再试其他域）。"""
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
            raise  # 风控 → 直接向上抛，触发熔断
        except Exception as e:
            last_err = e
            continue
    if posts is None:
        print('  [xueqiu] fetch error', user_id, last_err)
        return []
    out = []
    for p in posts:
        out.append(_norm_post(p))
    return out


def fetch_merged_timeline(cookie_header, pages=3, followed_ids=None):
    """合并信息流取数（WAF 根治：把 N 个请求压成 1~pages 个）。

    拉 home_timeline（你关注的人发的帖都在，特别关注是其子集），本地按 user_id 过滤出
    特别关注的新帖。请求量降 ~N 倍，从源头让 WAF 触发不了。命中风控抛 WafBlocked。
    """
    idset = set(str(x) for x in (followed_ids or []))
    out = []
    seen = set()
    for p in range(1, pages + 1):
        variants = [
            XQ_API + f'/v4/statuses/home_timeline.json?page={p}&count=20',
            XQ_BASE + f'/v4/statuses/home_timeline.json?page={p}&count=20',
            XQ_API + f'/statuses/home_timeline.json?page={p}&count=20',
        ]
        items = None
        for u in variants:
            try:
                items = _extract_list(_req(u, cookie_header))
                break
            except WafBlocked:
                raise  # 风控 → 向上抛，触发熔断
            except Exception:
                continue
        if not items:
            break  # 两个 variant 都没拿到 → 末页/异常
        for post in items:
            uid = str((post.get('user') or {}).get('id') or post.get('user_id') or '')
            if idset and uid not in idset:
                continue  # 非特别关注成员，跳过
            pid = post.get('id')
            if pid and pid not in seen:
                seen.add(pid)
                out.append(_norm_post(post))
        if len(items) < 20:
            break  # 不足一页 → 已是末页
        # 翻页间加 0.6~1.6s 随机停顿，避免「固定间隔翻页」这种机器特征触发风控
        if p < pages:
            time.sleep(0.6 + random.random())
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
                except WafBlocked:
                    raise
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
    """兼容旧接口：直查每个用户时间线（深扫兜底场景仍可用，但主流程改用合并流）。"""
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
                raise  # 风控直接向上抛
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
            raise  # 风控直接向上抛
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
