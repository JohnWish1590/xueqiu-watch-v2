# -*- coding: utf-8 -*-
"""把雪球帖子渲染成 400x300 1-bit 墨水屏图，并推送到极趣云（Zectrix）。

渲染后端：bitmapfont（纯 Python 点阵 + 手写 PNG 编码），零第三方依赖。
之所以不用 Pillow：QNAP ARMv7 + Python 3.12 无预编译 wheel 且机器上没有 gcc，
装不上。纯 Python 方案让 NAS 独立完成渲染，同时保证本机与 NAS 出图完全一致。

排版风格（像素级复刻「历史上的今天」墨水屏样式）：
  参考图特征：黑底标题栏 + 每条[小字日期/名字]+[大字内容]+[细分割线]，宽松间距。
  标题栏：宋体 18px — 黑底白字「雪球特别关注」+ 右侧时间
  内容：   宋体 16px — 每帖 1 行，超宽截断加省略号
  名字+时间：宋体 12px
  分割线：1px 细线
  内边距：8px（与参考图一致的留白）
  默认显示 5-6 条。

推送用 multipart/form-data 调 Zectrix Open API（优先 requests，缺失回退 urllib）。
注意：Zectrix 图片上传的字段名必须是 `images`（不是 `file`），否则服务端返回
{"code":500,"msg":"服务器内部异常"}。
"""
import time

import bitmapfont as bf

W, H = 400, 300

# ── 像素级参数（对齐「历史上的今天」参考图）──
TITLE_H = 30          # 标题栏黑底高度（参考图约 28-30px）
FONT_TITLE = 18       # 标题字号（宋体，font18.xwbf，实际格高=19）
FONT_CONTENT = 16     # 正文字号（宋体，font16.xwbf，实际格高=17）
FONT_META = 12        # 名字/时间字号（宋体，font12.xwbf，实际格高=13）
PAD = 8               # 左右内边距（参考图风格留白）
DIV_H = 1             # 分割线高度
GAP_META = 2          # 名字行->内容行 间距
GAP_CONTENT = 1       # 内容行->分割线 间距
GAP_ENTRY = 8         # 分割线->下一条目 间距（宽松，参考图风格）
TITLE_TOP_PAD = 8     # 标题栏下方到第一条目的间距


def _fmt_time(ts):
    """帖子时间戳（毫秒或秒）-> 'M月D日 H:MM'（无前导零，如 8月9日 16:19）。"""
    if not ts:
        return ''
    try:
        t = float(ts)
        if t > 1e11:      # 毫秒
            t /= 1000.0
        st = time.localtime(t)
        return '%d月%d日 %d:%02d' % (st.tm_mon, st.tm_mday, st.tm_hour, st.tm_min)
    except Exception:
        return ''


def render_digest(posts, count=6, font_path=None, title='雪球特别关注',
                  title_gray=0.0, font_tag=''):
    """渲染摘要图（像素级复刻「历史上的今天」风格）。返回 Canvas；字库缺失返回 None。

    title_gray：标题栏灰阶密度（黑点占比，0=纯黑底白字原版，>0=灰底白字）。
        0.95 几乎纯黑但带稀疏白点纹理，比纯黑(1.0)反射略弱。墨水屏 1-bit，
        灰=稀疏黑点；density 越大越深。推送 dither=false，灰阶需自绘。
    font_tag：字体标识。''=宋体(font12/16/18)，'fang'=仿宋，'kai'=楷体，'deng'=等线细。
        对应字库 fonts/font{size}_{tag}.xwbf（如 font18_fang.xwbf）。
    """
    def _load(size):
        return bf.load_font_variant(size, '_' + font_tag) if font_tag else bf.load_font(size)
    f_title = _load(FONT_TITLE)
    f_content = _load(FONT_CONTENT)
    f_meta = _load(FONT_META)
    if f_title is None or f_content is None or f_meta is None:
        if font_tag:
            miss = 'font18_%s.xwbf、font16_%s.xwbf、font12_%s.xwbf' % (font_tag, font_tag, font_tag)
        else:
            miss = 'font18.xwbf、font16.xwbf、font12.xwbf'
        print('[eink] 字库缺失（fonts/%s），跳过渲染。'
              '可在有 Pillow 的机器上运行 tools/gen_songs_fonts.py 生成。' % miss)
        return None

    cv = bf.Canvas(W, H)

    # ── 标题栏 ──
    if title_gray and title_gray > 0:
        # 深灰底（点阵抖动）+ 白字：比纯黑没那么镜面反光刺眼
        cv.fill_gray(0, 0, W, TITLE_H, title_gray)
        tval = 0
    else:
        # 纯黑底 + 白字（原版）
        cv.fill_rect(0, 0, W, TITLE_H, 1)
        tval = 0
    # 标题文字垂直居中于标题栏（用真实字高）
    title_y = (TITLE_H - f_title.height) // 2
    if title_y < 1:
        title_y = 1
    cv.draw_text(PAD, title_y, title, f_title, val=tval)
    # 右侧时间（用小号 meta 字体，与参考图"8月9日"位置一致）
    st = time.localtime()
    stamp = '%d月%d日 %d:%02d' % (st.tm_mon, st.tm_mday, st.tm_hour, st.tm_min)
    sw = f_meta.text_width(stamp)
    stamp_y = (TITLE_H - f_meta.height) // 2
    if stamp_y < 1:
        stamp_y = 1
    cv.draw_text(W - PAD - sw, stamp_y, stamp, f_meta, val=tval)

    items = posts[:count]
    if not items:
        cv.draw_text(PAD, TITLE_H + TITLE_TOP_PAD, '暂无新帖子，请检查 cookie 是否过期', f_meta)
        return cv

    # ── 条目区域（参考图风格：[名字+时间] / [内容] / ─── 分割线）──
    avail_w = W - PAD * 2
    y = TITLE_H + TITLE_TOP_PAD

    for p in items:
        # 预算本条目高度（用真实字高，避免裁切错位）
        block_h = f_meta.height + GAP_META + f_content.height + GAP_CONTENT + DIV_H + GAP_ENTRY
        if y + block_h > H - 2:   # 底部留 2px 安全边距
            break

        user = str(p.get('user') or '?')
        text = str(p.get('text') or '').strip()
        ts_str = _fmt_time(p.get('time'))

        # 第一行：作者名（左）+ 时间戳（右），小字号，不截断
        cv.draw_text(PAD, y, user, f_meta)
        tw = f_meta.text_width(ts_str)
        cv.draw_text(W - PAD - tw, y, ts_str, f_meta)
        y += f_meta.height + GAP_META

        # 第二行：内容（1 行，超宽截断加省略号）
        cv.draw_text_ellipsis(PAD, y, text, f_content, avail_w)
        y += f_content.height + GAP_CONTENT

        # 分割线（细线，贯穿内容宽度）
        cv.hline(PAD, y, avail_w, val=1)
        y += DIV_H + GAP_ENTRY

    return cv


def push_image(api_key, mac, img, page_id=1, timeout=15):
    """把画布推到 Zectrix 云。img 为 Canvas（或任何有 to_png() 的对象）。"""
    if img is None:
        print('[eink] 无图片可推送')
        return 0, 'skipped'
    if hasattr(img, 'to_png'):
        data = img.to_png()
    else:  # 兼容 Pillow Image
        import io
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        data = buf.getvalue()

    url = f'https://cloud.zectrix.com/open/v1/devices/{mac}/display/image'
    headers = {'X-API-Key': api_key}
    fields = {'dither': 'false', 'pageId': str(page_id)}
    try:
        import requests
        r = requests.post(url, headers=headers,
                          files={'images': ('eink.png', data, 'image/png')},
                          data=fields, timeout=timeout)
        return r.status_code, r.text
    except ImportError:
        return _push_urllib(url, headers, fields, data, timeout)


def _push_urllib(url, headers, fields, data, timeout):
    boundary = '----xueqiuwatchv2'
    body = bytearray()
    for k, v in fields.items():
        body += ('--%s\r\n' % boundary).encode()
        body += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode()
        body += ('%s\r\n' % v).encode()
    body += ('--%s\r\n' % boundary).encode()
    body += b'Content-Disposition: form-data; name="images"; filename="eink.png"\r\n'
    body += b'Content-Type: image/png\r\n\r\n'
    body += data
    body += ('\r\n--%s--\r\n' % boundary).encode()
    hdrs = dict(headers)
    hdrs['Content-Type'] = 'multipart/form-data; boundary=%s' % boundary
    import urllib.request
    req = urllib.request.Request(url, data=bytes(body), headers=hdrs, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'ignore')
    except Exception as e:
        return 0, str(e)


if __name__ == '__main__':
    now = time.time() * 1000
    sample = [
        {'user': 'PorcoRosso_dw', 'time': now - 3600e3,
         'text': '这判决认真的吗？'},
        {'user': '投资就是算好账', 'time': now - 7200e3,
         'text': '老美有制造业、有能源、有科技、还有货币霸权地位，所以回旋空间大'},
        {'user': '发财小民b', 'time': now - 9000e3,
         'text': '今天又亏了，但是长期看好的逻辑没变，仓位管理比择时更重要'},
        {'user': '门捷列夫学徒', 'time': now - 12000e3,
         'text': '光模块的问题在于可复制性太强，利润最终会沉淀到上游芯片'},
        {'user': '价投小韭菜日记', 'time': now - 15000e3,
         'text': '半导体周期走到哪个阶段了？详细拆解产业链上下游供需关系'},
        {'user': '量化交易老王', 'time': now - 18000e3,
         'text': '今天北向资金净流出80亿，主力资金大幅撤离科技板块'},
    ]
    cv = render_digest(sample, count=6)
    if cv:
        cv.save('preview.png')
        print('saved preview.png %dx%d 1-bit' % (cv.w, cv.h))
    else:
        print('ERROR: font missing')
