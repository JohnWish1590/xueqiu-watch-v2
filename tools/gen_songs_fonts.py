# -*- coding: utf-8 -*-
"""生成墨水屏点阵字库（12/16/18 三档）。

支持多款中文字体，按 tag 区分文件名：
    fonts/font12.xwbf      — 宋体（tag=''，默认）
    fonts/font12_fang.xwbf — 仿宋（tag='fang'）
    fonts/font12_kai.xwbf  — 楷体（tag='kai'）
    fonts/font12_deng.xwbf — 等线-细（tag='deng'）

用法：
    python tools/gen_songs_fonts.py            # 生成全部（song/fang/kai/deng）
    python tools/gen_songs_fonts.py fang kai  # 只生成指定字体
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), 'fonts')

# 每款字体的字体文件 + 光栅化阈值（细笔画字体阈值要低，否则细笔被吃）
FONTS = {
    'song': {'path': r'C:\Windows\Fonts\simsun.ttc',  'thr': 96},
    'fang': {'path': r'C:\Windows\Fonts\simfang.ttf', 'thr': 96},
    'kai':  {'path': r'C:\Windows\Fonts\simkai.ttf',  'thr': 84},
    'deng': {'path': r'C:\Windows\Fonts\Deng.ttf',    'thr': 92},
    'hira': {'path': r'C:\Users\user\Desktop\hiragino-sans-gb-w3-maisfontes.0d44\hiragino-sans-gb-w3.otf',
             'thr': 88},   # W3 极细，阈值压低防细笔被吃
}


def build_charset():
    chars = set()
    for c in range(0x20, 0x7F):
        chars.add(chr(c))
    for hi in range(0x81, 0xFF):
        for lo in range(0x40, 0xFF):
            if lo == 0x7F:
                continue
            try:
                ch = bytes([hi, lo]).decode('gbk')
            except UnicodeDecodeError:
                continue
            if len(ch) == 1 and ord(ch) > 0x7F:
                chars.add(ch)
    return chars


def is_halfwidth(ch):
    return ord(ch) < 0x80


def rasterize(font, ch, cell_w, cell_h, threshold):
    """cell_h 传入字体的真实自然高度（asc+desc），baseline=asc，上下零裁切。"""
    asc, desc = font.getmetrics()
    baseline_y = asc
    img = Image.new('L', (cell_w, cell_h), 0)
    d = ImageDraw.Draw(img)
    try:
        d.text((0, baseline_y), ch, font=font, fill=255, anchor='ls')
    except Exception:
        d.text((0, baseline_y), ch, font=font, fill=255)

    px = img.load()
    row_bytes = (cell_w + 7) // 8
    out = bytearray(row_bytes * cell_h)
    for y in range(cell_h):
        base = y * row_bytes
        for x in range(cell_w):
            if px[x, y] > threshold:
                out[base + (x >> 3)] |= 0x80 >> (x & 7)
    return bytes(out)


def gen_font(size, font_path, threshold, tag=''):
    import struct
    font = ImageFont.truetype(font_path, size)
    asc, desc = font.getmetrics()
    cell_h = asc + desc          # 真实自然高度（比 size 大 1-2px），保证上下不裁
    cell_full = size             # 全角字宽 = size
    cell_half = size // 2        # 半角字宽 = size/2
    row_full = (cell_full + 7) // 8
    row_half = (cell_half + 7) // 8
    full_bytes = row_full * cell_h
    half_bytes = row_half * cell_h

    chars = build_charset()
    halfs, fulls = [], []
    for ch in sorted(chars):
        if is_halfwidth(ch):
            halfs.append(ch)
        else:
            fulls.append(ch)

    name = os.path.basename(font_path)
    print(f'  [{size}px] {name} tag={tag!r} | cell_h={cell_h} (asc={asc}+desc={desc}) | '
          f'半角 {len(halfs)} / 全角 {len(fulls)} | 光栅化中…')

    half_cps = struct.pack('<%dI' % len(halfs), *[ord(c) for c in halfs])
    half_data = bytearray()
    for ch in halfs:
        half_data += rasterize(font, ch, cell_half, cell_h, threshold)

    full_cps = struct.pack('<%dI' % len(fulls), *[ord(c) for c in fulls])
    full_data = bytearray()
    for i, ch in enumerate(fulls):
        full_data += rasterize(font, ch, cell_full, cell_h, threshold)
        if i and i % 5000 == 0:
            print(f'     …{i}/{len(fulls)}')

    header = struct.pack('<4sBBBBBII', b'XWBF', 1, size, cell_h,
                         half_bytes, full_bytes, len(halfs), len(fulls))
    blob = header + half_cps + bytes(half_data) + full_cps + bytes(full_data)

    os.makedirs(OUT_DIR, exist_ok=True)
    suffix = ('_' + tag) if tag else ''
    out_path = os.path.join(OUT_DIR, f'font{size}{suffix}.xwbf')
    with open(out_path, 'wb') as f:
        f.write(blob)
    print(f'  -> {out_path}  ({len(blob) / 1024:.0f} KB)')
    return out_path


def main():
    tags = sys.argv[1:] or list(FONTS.keys())
    for tag in tags:
        if tag not in FONTS:
            print('未知字体 tag:', tag, '| 可选:', list(FONTS.keys()))
            continue
        spec = FONTS[tag]
        if not os.path.exists(spec['path']):
            print(f'错误：找不到 {spec["path"]}')
            continue
        print(f'==== 字体={tag} ({spec["path"]}) ====')
        for sz in (12, 16, 18):
            gen_font(sz, spec['path'], spec['thr'], tag)
    print('完成。')


if __name__ == '__main__':
    main()
