"""雪哨 v2 主程序（脱离本机版）。

职责：
1) 后台守护线程启动 Cookie 接收 HTTP 服务（供浏览器「Cookie 管家」推送 cookie）；
2) 主线程按 poll_interval_minutes 轮询雪球 -> 渲染 400x300 1-bit 图
   -> 推墨水屏（Zectrix）+ 手机（企业微信/Bark/自定义）。

单进程入口：python main.py（QNAP 上用 Task Scheduler 以 nohup 自启）。

v2.1：移植雪哨 Chrome 扩展的防风控策略——
  · 主取数改为「合并 home_timeline（~3 次请求）+ 轮转深扫（每轮 3 人）」，源头压低请求量；
  · 接入 xueqiu 模块的风控熔断（rl_trip/rl_reset/rl_is_blocked）：命中 567/429 立即熔断、
    指数退避、冷却期内跳过本轮、连续成功复位；
  · 抓取失败时墨水屏满屏显示错误，让用户立刻发现。
"""
import os
import json
import time
import random
import threading

import config
import cookies_store
import xueqiu
import eink
import server

STATE_FILE = os.environ.get('XW_STATE', 'state.json')
GROUP_CACHE_FILE = os.environ.get('XW_GROUP', 'group_cache.json')  # 特别关注成员缓存
DEEP_PER_CYCLE = int(os.environ.get('XW_DEEP', '5'))  # 每轮兜底深扫人数（轮转）
DEEP_BLOCK_COOLDOWN = int(os.environ.get('XW_DEEP_COOLDOWN', '600'))  # 深扫被风控后静默秒数
_deep_blocked_until = 0.0  # 深扫被风控时记录，静默期内不再发 user_timeline 请求（避免越锁越死）


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'seen_ids': [], 'deep_idx': 0}


def save_state(st):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        print('[state] save error', e)


# ---- 特别关注分组缓存 ----

def load_group_cache():
    """从文件加载上次拉取的特别关注成员列表（跨重启持久化）。"""
    if os.path.exists(GROUP_CACHE_FILE):
        try:
            with open(GROUP_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_group_cache(users):
    """保存特别关注成员列表到文件。"""
    try:
        with open(GROUP_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        print(f'[group] 已缓存 {len(users)} 人到 {GROUP_CACHE_FILE}')
    except Exception as e:
        print('[group] 缓存写入失败:', e)


def resolve_followed(cfg, cookie_header):
    """解析 followed_user_ids：
      - 若 config 里填了值 → 走原有的昵称→ID 解析
      - 若为空或 ['__auto__'] → 自动从「特别关注」分组拉取（优先用本地缓存）
    返回数字 ID 字符串列表。
    """
    raw = cfg.get('followed_user_ids') or []
    if raw and raw != ['__auto__']:
        # 有手动配置 → 走原有逻辑（支持昵称/数字混合）
        return xueqiu.resolve_user_ids(raw, cookie_header)

    # 自动模式：先尝试本地缓存
    cached = load_group_cache()
    if cached:
        print(f'[group] 使用本地缓存的特别关注成员（{len(cached)} 人，上次拉取）')
        return [u['id'] for u in cached if u.get('id')]

    # 无缓存 → 在线拉取
    print('[group] 正在从雪球拉取「特别关注」分组…')
    users = xueqiu.fetch_special_follow(cookie_header)
    if users:
        save_group_cache(users)
        names = ', '.join(f"{u['name']}({u['id']})" for u in users[:10])
        if len(users) > 10:
            names += f' … 等共 {len(users)} 人'
        print(f'[group] 拉取成功：{names}')
        return [u['id'] for u in users]
    else:
        print('[group] ⚠️ 拉取失败，本轮无监控对象（请在浏览器 Cookie 管家确认已导出有效 cookie）')
        return []


def push_phone(new_posts, cfg):
    ph = cfg.get('phone') or {}
    if not ph.get('enabled'):
        return
    typ = ph.get('type')
    webhook = ph.get('webhook')
    if not webhook:
        print('[phone] 已启用但未配置 webhook，跳过')
        return
    lines = [f"· {p.get('user', '?')}: {p.get('text', '')}" for p in new_posts[:10]]
    content = '雪哨新动态\n' + '\n'.join(lines)
    try:
        import requests
        if typ == 'wecom':
            requests.post(webhook, json={'msgtype': 'text', 'text': {'content': content}}, timeout=10)
        elif typ == 'bark':
            requests.post(webhook, json={'title': '雪哨新动态', 'body': content}, timeout=10)
        elif typ == 'http':
            # 自定义安卓 APP / 自建服务：把原始帖子 JSON 推过去
            requests.post(webhook, json={'posts': new_posts}, timeout=10)
        else:
            print('[phone] 未知类型', typ)
            return
        print('[phone] 已推送', len(new_posts), '条')
    except Exception as e:
        print('[phone] push error', e)


# ---- Cookie 文件导入（浏览器「快捷写入目录」经 NAS 同步落盘）----
_last_import_mtime = 0.0

def maybe_import_from_file(cfg):
    """若配置了 cookie_import_file（浏览器写出的 cookies-import.json 经同步到 NAS 的路径），
    且文件比上次导入新，则读入并存入加密 cookie 库。"""
    global _last_import_mtime
    path = cfg.get('cookie_import_file')
    if not path or not os.path.exists(path):
        return
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    if mtime <= _last_import_mtime:
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        cookies_map = (data.get('cookies') or {}) if isinstance(data, dict) else {}
        if not cookies_map:
            return
        cookies_store.save_cookies(cookies_map)
        _last_import_mtime = mtime
        print(f'[import] 已从 {path} 载入 {len(cookies_map)} 个站点的 cookie')
    except Exception as e:
        print('[import] 读取同步 cookie 文件失败:', e)


def _push_error(reason, cfg):
    """抓取失败时在墨水屏上渲染并推送一张满屏错误图，让用户能立刻发现异常。

    与正常摘要图同走 Zectrix 推送；渲染/推送任何异常都不抛出（避免掩盖原错误）。
    """
    zx = cfg.get('zectrix') or {}
    api_key = zx.get('api_key')
    mac = zx.get('device_mac')
    page_id = zx.get('page_id', '1')
    font_path = cfg.get('font_path', '') or None
    try:
        img = eink.render_error(reason, font_path=font_path)
        if img is None:
            print('[run] 错误图渲染不可用（字库缺失），仅记录日志')
            return
        if api_key and mac:
            code, txt = eink.push_image(api_key, mac, img, page_id)
            print(f'[run] 错误图推送 {code}: {str(txt)[:80]}')
        else:
            print('[run] 未配置 zectrix，跳过错误图推送（错误已在日志体现）')
    except Exception as e:
        print('[run] 错误图渲染/推送异常:', e)


def _deep_scan(cfg, cookie_header, followed, state):
    """兜底深扫：每轮只直查 DEEP_PER_CYCLE 个用户（轮转），请求量极小不会触发 WAF。
    合并流负责"即时"，深扫负责"绝不漏"，两者互补。用户之间随机睡 1~2 秒，杜绝突发。

    深扫是非致命的：命中风控（user_timeline 被限流）时记录静默期、跳过本轮剩余深扫，
    直接返回已收集的（可能为空），绝不向上抛——这样合并流已拿到的帖子仍能正常上屏，
    不会因深扫失败而显示错误图。静默期内连请求都不发，避免反复撞墙把风控锁死。
    """
    global _deep_blocked_until
    ids = [str(x) for x in followed if str(x).isdigit()]
    if not ids:
        return []
    # 静默期内不发 user_timeline 请求（当前多半仍被限流，发了也是 567）
    if time.time() < _deep_blocked_until:
        print('[deep] 仍在风控静默期，本轮深扫跳过')
        return []
    n = len(ids)
    idx = int(state.get('deep_idx', 0)) % n
    collected = []
    seen = set()
    for k in range(DEEP_PER_CYCLE):
        uid = ids[idx % n]
        idx = (idx + 1) % n
        try:
            for pg in (1, 2):
                for p in xueqiu.fetch_user_timeline(uid, cookie_header, page=pg):
                    if p['id'] and p['id'] not in seen:
                        seen.add(p['id'])
                        collected.append(p)
        except xueqiu.WafBlocked as e:
            # user_timeline 被限流：进入静默期，停止本轮深扫，返回已收集的
            _deep_blocked_until = time.time() + DEEP_BLOCK_COOLDOWN
            print(f'[deep] 深扫被风控拦截（{e}），进入静默期 {DEEP_BLOCK_COOLDOWN}s，本轮深扫停止')
            break
        except Exception as e:
            print(f'  [deep] 用户 {uid} 抓取失败: {e}')
        # 节流：用户之间随机睡 1~2s，避免突发请求触发风控
        if k < DEEP_PER_CYCLE - 1:
            time.sleep(1 + random.random())
    state['deep_idx'] = idx
    return collected


def run_once(cfg, state, first_run=False):
    zx = cfg.get('zectrix') or {}
    api_key = zx.get('api_key')
    mac = zx.get('device_mac')
    page_id = zx.get('page_id', '1')
    digest_count = int(cfg.get('digest_count', 6))
    font_path = cfg.get('font_path', '') or None

    # 先尝试从同步文件导入（浏览器快捷写入目录 -> NAS 同步落盘）
    maybe_import_from_file(cfg)

    # 熔断冷却期内直接跳过本轮（风控未解除，不再发请求，避免越锁越死）。
    # 上一轮推的错误图会一直留在墨水屏上，用户能看见。
    if xueqiu.rl_is_blocked():
        print('[rl] 熔断冷却中，跳过本轮（雪球风控未解除）')
        return

    cookies = cookies_store.load_cookies()
    xq = (cookies.get('xueqiu') or {}).get('header')
    if not xq:
        _push_error('未读到雪球 Cookie\n请在浏览器 Cookie 管家\n重新导出到 NAS', cfg)
        return

    try:
        followed = resolve_followed(cfg, xq)
    except xueqiu.WafBlocked as e:
        mins = xueqiu.rl_trip(str(e))
        _push_error('雪球风控拦截\n%s\n已暂停 %d 分钟\n稍后自动恢复' % (str(e), mins), cfg)
        return
    except Exception as e:
        _push_error('特别关注分组解析异常：\n%s' % e, cfg)
        return
    if not followed:
        detail = getattr(xueqiu, 'LAST_ERROR', '') or ''
        reason = '特别关注分组拉取为空'
        if detail:
            reason += '\n%s' % detail
        reason += '\n请重新导出 Cookie'
        _push_error(reason, cfg)
        return

    # 主取数：合并信息流（~3 次请求）+ 轮转深扫（每轮 3 人）
    try:
        merged = xueqiu.fetch_merged_timeline(xq, pages=3, followed_ids=followed)
        deep = _deep_scan(cfg, xq, followed, state)
        seen_ids_dedup = set()
        posts = []
        for p in merged + deep:
            pid = p.get('id')
            if pid and pid not in seen_ids_dedup:
                seen_ids_dedup.add(pid)
                posts.append(p)
        posts.sort(key=lambda x: x.get('time', 0), reverse=True)
        print(f'[run] 合并流 {len(merged)} 条 + 深扫 {len(deep)} 条 → 去重后 {len(posts)} 条')
    except xueqiu.WafBlocked as e:
        mins = xueqiu.rl_trip(str(e))
        _push_error('雪球风控拦截\n%s\n已暂停 %d 分钟\n稍后自动恢复' % (str(e), mins), cfg)
        return
    except Exception as e:
        _push_error('雪球抓取异常：\n%s' % e, cfg)
        return

    if not posts:
        detail = getattr(xueqiu, 'LAST_ERROR', '') or ''
        if detail:
            _push_error('本周期未抓到任何帖子\n%s' % detail, cfg)
        else:
            print('[run] 本周期无帖子（可能雪球暂无更新），保持上一屏不刷新')
        return

    # 本轮完整跑通（未被风控中断）→ 复位退避计数
    xueqiu.rl_reset()

    # 更新共享缓存，供手机 APP 的 GET /api/posts 读取（不触发新的抓取）
    server.set_recent_posts(posts)
    print(f'[run] 抓到 {len(posts)} 条，已更新缓存')

    # 墨水屏：始终展示最新 digest_count 条（即使以前看过也刷新）
    # 渲染/推送任何失败都不能影响抓取与手机推送，整段隔离。
    try:
        img = eink.render_digest(posts, count=digest_count, font_path=font_path)
        if img is None:
            print('[run] 渲染器不可用，跳过墨水屏')
        elif api_key and mac:
            code, txt = eink.push_image(api_key, mac, img, page_id)
            print(f'[run] 墨水屏推送 {code}: {str(txt)[:80]}')
        else:
            print('[run] 未配置 zectrix，跳过墨水屏推送')
    except Exception as e:
        print('[run] 墨水屏环节异常（不影响其他功能）:', e)

    # 手机：仅推送“新”帖子（按 id 去重，跨重启持久化）
    seen = set(state.get('seen_ids', []))
    if first_run:
        # 首次运行只“播种”已见 id，避免把历史帖子全推到手机
        for p in posts:
            if p.get('id'):
                seen.add(p['id'])
        state['seen_ids'] = list(seen)
        save_state(state)
        print('[run] 首次运行，已播种 seen_ids，手机推送从下一轮新帖开始')
        return

    new_posts = [p for p in posts if p.get('id') and p['id'] not in seen]
    if new_posts:
        push_phone(new_posts, cfg)
        for p in new_posts:
            if p.get('id'):
                seen.add(p['id'])
        if len(seen) > 500:
            seen = set(list(seen)[-500:])
        state['seen_ids'] = list(seen)
        save_state(state)


def main():
    cfg = config.load_config()
    port = int(cfg.get('server_port', 8080))
    interval = max(1, int(cfg.get('poll_interval_minutes', 10)))

    threading.Thread(target=server.start_receiver, args=(port,), daemon=True).start()
    print(f'[main] cookie 接收服务已启动: http://0.0.0.0:{port}/api/set-cookies')

    zx = cfg.get('zectrix') or {}
    if not (zx.get('api_key') and zx.get('device_mac')):
        print('[main] 警告：未配置 zectrix api_key/device_mac，墨水屏推送将跳过'
              '（仍可做 cookie 接收 + 手机推送）。')

    state_existed = os.path.exists(STATE_FILE)
    state = load_state()
    print(f'[main] 启动轮询，间隔 {interval} 分钟。Ctrl+C 退出。')
    while True:
        try:
            run_once(cfg, state, first_run=not state_existed)
            state_existed = True
        except Exception as e:
            print('[run] 异常:', e)
        time.sleep(interval * 60)


if __name__ == '__main__':
    main()
