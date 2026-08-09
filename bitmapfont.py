# -*- coding: utf-8 -*-
"""纯 Python 1-bit 点阵渲染引擎（零第三方依赖）。

存在的理由：QNAP ARMv7 + Python 3.12 装不上 Pillow（PyPI 无预编译 wheel，
机器上也没有 gcc）。本模块用预生成的 .xwbf 点阵字库 + 手写 PNG 编码器，
让 NAS 完全独立地把文字渲染成墨水屏需要的 400x300 1-bit PNG。

字库由 tools/gen_bitmap_font.py 在有 Pillow 的机器上一次性生成。
"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, 'fonts')


class BitmapFont:
    """读取 .xwbf 点阵字库。"""

    def __init__(self, path):
        with open(path, 'rb') as f:
            blob = f.read()
        magic, ver, w, h, half_bytes, full_bytes, n_half, n_full = \
            struct.unpack_from('<4sBBBBBII', blob, 0)
        if magic != b'XWBF':
            raise ValueError('bad font file: %s' % path)
        self.size = w            # 名义字号 = 字宽
        self.width = w
        self.height = h          # 真实格高 = 字库行数（= asc+desc，可能 > size）
        self.half_w = w // 2
        self.full_w = w
        self.half_bytes = half_bytes
        self.full_bytes = full_bytes
        self.half_row = (self.half_w + 7) // 8
        self.full_row = (self.full_w + 7) // 8

        off = struct.calcsize('<4sBBBBBII')
        half_cps = struct.unpack_from('<%dI' % n_half, blob, off)
        off += n_half * 4
        self._half_blob = blob[off:off + n_half * half_bytes]
        off += n_half * half_bytes
        full_cps = struct.unpack_from('<%dI' % n_full, blob, off)
        off += n_full * 4
        self._full_blob = blob[off:off + n_full * full_bytes]

        # codepoint -> 数据块内序号
        self._half_idx = {cp: i for i, cp in enumerate(half_cps)}
        self._full_idx = {cp: i for i, cp in enumerate(full_cps)}

    def glyph(self, ch):
        """返回 (width, row_bytes, data)；字库中没有则返回 None。"""
        cp = ord(ch)
        i = self._half_idx.get(cp)
        if i is not None:
            s = i * self.half_bytes
            return self.half_w, self.half_row, self._half_blob[s:s + self.half_bytes]
        i = self._full_idx.get(cp)
        if i is not None:
            s = i * self.full_bytes
            return self.full_w, self.full_row, self._full_blob[s:s + self.full_bytes]
        return None

    def char_width(self, ch):
        cp = ord(ch)
        if cp in self._half_idx:
            return self.half_w
        return self.full_w

    def text_width(self, text):
        return sum(self.char_width(c) for c in text)


_FONT_CACHE = {}


def load_font(size):
    """按字号加载字库（带进程内缓存）。找不到返回 None。"""
    size = int(size)
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    path = os.path.join(FONT_DIR, 'font%d.xwbf' % size)
    if not os.path.exists(path):
        return None
    f = BitmapFont(path)
    _FONT_CACHE[size] = f
    return f


def load_font_variant(size, variant=''):
    """加载带字重后缀的字库，如 load_font_variant(36,'b') -> font36b.xwbf。

    带进程内缓存，key = (size, variant)。
    """
    size = int(size)
    key = (size, variant) if variant else size
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = os.path.join(FONT_DIR, 'font%d%s.xwbf' % (size, variant))
    if not os.path.exists(path):
        return None
    f = BitmapFont(path)
    _FONT_CACHE[key] = f
    return f


class Canvas:
    """1 字节/像素的简单画布：0 = 白（背景），1 = 黑（墨点）。"""

    def __init__(self, width, height):
        self.w = width
        self.h = height
        self.buf = bytearray(width * height)  # 全 0 = 全白

    # ---- 基础绘制 ----

    def fill_rect(self, x, y, w, h, val=1):
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes([val]) * (x1 - x0)
        for yy in range(y0, y1):
            base = yy * self.w
            self.buf[base + x0:base + x1] = row

    def invert_rect(self, x, y, w, h):
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        for yy in range(y0, y1):
            base = yy * self.w
            for xx in range(x0, x1):
                self.buf[base + xx] ^= 1

    def hline(self, x, y, w, val=1):
        self.fill_rect(x, y, w, 1, val)

    # Bayer 4x4 有序抖动矩阵（值 0..15），用于模拟 1-bit 屏幕上的灰阶
    _BAYER4 = [0, 8, 2, 10,
               12, 4, 14, 6,
               3, 11, 1, 9,
               15, 7, 13, 5]

    def fill_gray(self, x, y, w, h, density=0.4):
        """用有序抖动把 (x,y,w,h) 区域填充成目标灰阶。

        density = 黑点占比（0=纯白 … 1=纯黑）。墨水屏是 1-bit，所谓“灰”
        就是稀疏黑点；density 越大越深。推送走 dither=false，所以灰阶必须
        在画布里自己抖出来，不能依赖设备端抖动。
        """
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        thr = density * 16.0
        b = self._BAYER4
        buf = self.buf
        W = self.w
        for yy in range(y0, y1):
            base = yy * W
            ry = yy & 3
            for xx in range(x0, x1):
                if b[ry * 4 + (xx & 3)] < thr:
                    buf[base + xx] = 1

    def vline(self, x, y, h, val=1):
        self.fill_rect(x, y, 1, h, val)

    def rect(self, x, y, w, h, val=1):
        self.hline(x, y, w, val)
        self.hline(x, y + h - 1, w, val)
        self.vline(x, y, h, val)
        self.vline(x + w - 1, y, h, val)

    # ---- 文字 ----

    def draw_char(self, x, y, ch, font, val=1):
        g = font.glyph(ch)
        if g is None:
            # 缺字：画一个空心方框占位
            self.rect(x + 1, y + 1, font.full_w - 2, font.height - 2, val)
            return font.full_w
        gw, row_bytes, data = g
        H, W = font.height, self.w
        for gy in range(H):
            py = y + gy
            if py < 0 or py >= self.h:
                continue
            base = py * W
            rb = gy * row_bytes
            for gx in range(gw):
                if data[rb + (gx >> 3)] & (0x80 >> (gx & 7)):
                    px = x + gx
                    if 0 <= px < W:
                        self.buf[base + px] = val
        return gw

    def draw_text(self, x, y, text, font, val=1, max_x=None):
        """从 (x, y) 左上角开始横排。max_x 为右边界（超出即停）。返回结束 x。"""
        limit = self.w if max_x is None else min(self.w, max_x)
        cx = x
        for ch in text:
            cw = font.char_width(ch)
            if cx + cw > limit:
                break
            self.draw_char(cx, y, ch, font, val)
            cx += cw
        return cx

    def draw_text_ellipsis(self, x, y, text, font, max_w, val=1):
        """超宽自动截断并追加省略号。返回实际绘制的字符数。"""
        ell = '…'
        ell_w = font.char_width(ell)
        cx, used = x, 0
        limit = x + max_w
        for i, ch in enumerate(text):
            cw = font.char_width(ch)
            remain = len(text) - i
            # 还放得下 && (是最后一个字 或 放完还能塞省略号)
            if cx + cw <= limit and (remain == 1 or cx + cw + ell_w <= limit):
                self.draw_char(cx, y, ch, font, val)
                cx += cw
                used += 1
            else:
                if cx + ell_w <= limit:
                    self.draw_char(cx, y, ell, font, val)
                break
        return used

    # ---- 导出 ----

    def to_png(self):
        """编码成 1-bit 灰度 PNG（bit 0 = 黑）。返回 bytes。"""
        W, H = self.w, self.h
        row_bytes = (W + 7) // 8
        raw = bytearray()
        buf = self.buf
        for y in range(H):
            raw.append(0)  # filter type 0 (None)
            base = y * W
            line = bytearray(row_bytes)
            for x in range(W):
                if not buf[base + x]:      # 0 = 白 -> PNG bit 1
                    line[x >> 3] |= 0x80 >> (x & 7)
            raw += line

        def chunk(tag, data):
            c = struct.pack('>I', len(data)) + tag + data
            return c + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)

        ihdr = struct.pack('>IIBBBBB', W, H, 1, 0, 0, 0, 0)
        return (b'\x89PNG\r\n\x1a\n'
                + chunk(b'IHDR', ihdr)
                + chunk(b'IDAT', zlib.compress(bytes(raw), 9))
                + chunk(b'IEND', b''))

    def save(self, path):
        with open(path, 'wb') as f:
            f.write(self.to_png())


def wrap_text(text, font, max_w):
    """按像素宽度折行，返回行列表。"""
    lines, cur, cur_w = [], '', 0
    for ch in text:
        if ch == '\n':
            lines.append(cur)
            cur, cur_w = '', 0
            continue
        cw = font.char_width(ch)
        if cur_w + cw > max_w:
            lines.append(cur)
            cur, cur_w = ch, cw
        else:
            cur += ch
            cur_w += cw
    if cur:
        lines.append(cur)
    return lines
