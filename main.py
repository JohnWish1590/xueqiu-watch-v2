"""雪哨 v2 主程序（脱离本机版）。

职责：
1) 后台守护线程启动 Cookie 接收 HTTP 服务（供浏览器「Cookie 管家」推送 cookie）；
2) 主线程按 poll_interval_minutes 轮询雪球 -> 渲染 400x300 1-bit 图
   -> 推墨水屏（Zectrix）+ 手机（企业微信/Bark/自定义）。

单进程入口：python main.py（QNAP 上用 Task Scheduler 以 nohup 自启）。
"""
import os
import json
import time
import threading

import config
import cookies_store
import xueqiu
import eink
import server

STATE_FILE = os.environ.get('XW_STATE', 'state.json')
GROUP_CACHE_FILE = os.environ.get('XW_GROUP', 'group_cache.json')  # 特别关注成员缓存


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'seen_ids': []}


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


def run_once(cfg, state, first_run=False):
    zx = cfg.get('zectrix') or {}
    api_key = zx.get('api_key')
    mac = zx.get('device_mac')
    page_id = zx.get('page_id', '1')
    digest_count = int(cfg.get('digest_count', 6))
    font_path = cfg.get('font_path', '') or None

    # 先尝试从同步文件导入（浏览器快捷写入目录 -> NAS 同步落盘）
    maybe_import_from_file(cfg)

    cookies = cookies_store.load_cookies()
    xq = (cookies.get('xueqiu') or {}).get('header')
    if not xq:
        _push_error('未读到雪球 Cookie\n请在浏览器 Cookie 管家\n重新导出到 NAS', cfg)
        return

    try:
        followed = resolve_followed(cfg, xq)
    except Exception as e:
        _push_error('特别关注分组解析异常：\n%s' % e, cfg)
        return
    if not followed:
        _push_error('特别关注分组为空\nCookie 可能已失效\n请重新导出', cfg)
        return

    try:
        posts = xueqiu.fetch_all(followed, xq)
    except Exception as e:
        _push_error('雪球抓取异常：\n%s' % e, cfg)
        return
    if not posts:
        _push_error('本周期未抓到任何帖子\n接口可能被拦截\n或 Cookie 已失效', cfg)
        return

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
