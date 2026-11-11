"""QR rendering for the phone deck link (owner request, 2026-11-08):
scanning beats typing the deck URL — token and all — into a phone.

The encoder is built in, right here — byte mode, error level M,
versions 1-10 (up to 213 bytes, far beyond a deck URL). The first
version of this module delegated to the third-party segno package
and returned None when segno was missing, which made the deck QR
window silently never appear on the owner's PC (2026-11-08: "there
is no qr code pop up window"). A built-in encoder removes that whole
failure class: the exact code tested is the exact code that runs on
every machine, with no install step in between.

qr_matrix still returns None for text that cannot be encoded at all
(empty, or longer than a version-10 code holds) so callers keep a
defined fallback — but callers must not treat None as silence: the
deck pop-up opens with the plain URL either way (forge/main.py).
Nothing here may raise into the UI.
"""
from __future__ import annotations

import sys

BORDER = 4  # quiet-zone modules on each side (QR spec minimum)

# Error-correction level M parameters, versions 1-10 (QR spec tables).
_ECC_PER_BLOCK = (0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
_NUM_BLOCKS = (0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
_ALIGNMENT = ((), (), (6, 18), (6, 22), (6, 26), (6, 30), (6, 34),
              (6, 22, 38), (6, 24, 42), (6, 26, 46), (6, 28, 50))
_REMAINDER_BITS = (0, 0, 7, 7, 7, 7, 7, 0, 0, 0, 0)

# Galois field GF(256) tables, primitive polynomial 0x11D.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(len(result)):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i in range(len(divisor)):
            result[i] ^= _gf_mul(divisor[i], factor)
    return result


def _num_raw_data_modules(version):
    result = (16 * version + 128) * version + 64
    if version >= 2:
        num_align = version // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if version >= 7:
            result -= 36
    return result


def _num_data_codewords(version):
    return (_num_raw_data_modules(version) // 8
            - _ECC_PER_BLOCK[version] * _NUM_BLOCKS[version])


class _Bits:
    def __init__(self):
        self.data = []

    def append(self, value, length):
        for i in range(length - 1, -1, -1):
            self.data.append((value >> i) & 1)

    def __len__(self):
        return len(self.data)


def _encode_codewords(payload: bytes, version: int) -> bytes:
    capacity = _num_data_codewords(version) * 8
    bb = _Bits()
    bb.append(0x4, 4)  # byte mode indicator
    bb.append(len(payload), 8 if version <= 9 else 16)
    for b in payload:
        bb.append(b, 8)
    # Terminator, byte alignment, pad bytes.
    bb.append(0, min(4, capacity - len(bb)))
    bb.append(0, -len(bb) % 8)
    pad = 0xEC
    while len(bb) < capacity:
        bb.append(pad, 8)
        pad ^= 0xEC ^ 0x11
    databytes = bytearray()
    for i in range(0, len(bb), 8):
        byte = 0
        for bit in bb.data[i:i + 8]:
            byte = (byte << 1) | bit
        databytes.append(byte)
    # Split into blocks, compute ECC, interleave.
    num_blocks = _NUM_BLOCKS[version]
    ecc_len = _ECC_PER_BLOCK[version]
    divisor = _rs_divisor(ecc_len)
    short_len = len(databytes) // num_blocks
    num_long = len(databytes) % num_blocks
    blocks = []
    eccs = []
    k = 0
    for i in range(num_blocks):
        take = short_len + (1 if i >= num_blocks - num_long else 0)
        block = bytes(databytes[k:k + take])
        k += take
        blocks.append(block)
        eccs.append(_rs_remainder(block, divisor))
    out = bytearray()
    for i in range(max(len(b) for b in blocks)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ecc_len):
        for ecc in eccs:
            out.append(ecc[i])
    return bytes(out)


def _format_bits(mask):
    data = mask  # error level M has format bits 0b00
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (((data << 10) | rem) ^ 0x5412) & 0x7FFF


def _version_bits(version):
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


class _QrBuilder:
    def __init__(self, version):
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.is_function = [[False] * self.size for _ in range(self.size)]

    def set_function(self, x, y, dark):
        if 0 <= x < self.size and 0 <= y < self.size:
            self.modules[y][x] = bool(dark)
            self.is_function[y][x] = True

    def draw_function_patterns(self):
        size = self.size
        for i in range(size):
            self.set_function(6, i, i % 2 == 0)
            self.set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    dist = max(abs(dx), abs(dy))
                    self.set_function(cx + dx, cy + dy,
                                      dist != 2 and dist != 4)
        align = _ALIGNMENT[self.version]
        for i, ax in enumerate(align):
            for j, ay in enumerate(align):
                if ((i == 0 and j == 0) or (i == 0 and j == len(align) - 1)
                        or (i == len(align) - 1 and j == 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_function(ax + dx, ay + dy,
                                          max(abs(dx), abs(dy)) != 1)
        self.draw_format_bits(0)  # placeholder; reserves the modules
        if self.version >= 7:
            bits = _version_bits(self.version)
            for i in range(18):
                bit = (bits >> i) & 1
                self.set_function(size - 11 + i % 3, i // 3, bit)
                self.set_function(i // 3, size - 11 + i % 3, bit)

    def draw_format_bits(self, mask):
        size = self.size
        bits = _format_bits(mask)
        for i in range(6):
            self.set_function(8, i, (bits >> i) & 1)
        self.set_function(8, 7, (bits >> 6) & 1)
        self.set_function(8, 8, (bits >> 7) & 1)
        self.set_function(7, 8, (bits >> 8) & 1)
        for i in range(9, 15):
            self.set_function(14 - i, 8, (bits >> i) & 1)
        for i in range(8):
            self.set_function(size - 1 - i, 8, (bits >> i) & 1)
        for i in range(8, 15):
            self.set_function(8, size - 15 + i, (bits >> i) & 1)
        self.set_function(8, size - 8, True)  # dark module

    def place_data(self, bits):
        size = self.size
        i = 0
        right = size - 1
        while right >= 1:
            if right <= 6:
                right -= 1
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.is_function[y][x]:
                        self.modules[y][x] = bool(
                            bits[i]) if i < len(bits) else False
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        size = self.size
        for y in range(size):
            for x in range(size):
                if self.is_function[y][x]:
                    continue
                if mask == 0:
                    flip = (y + x) % 2 == 0
                elif mask == 1:
                    flip = y % 2 == 0
                elif mask == 2:
                    flip = x % 3 == 0
                elif mask == 3:
                    flip = (y + x) % 3 == 0
                elif mask == 4:
                    flip = (y // 2 + x // 3) % 2 == 0
                elif mask == 5:
                    flip = (y * x) % 2 + (y * x) % 3 == 0
                elif mask == 6:
                    flip = ((y * x) % 2 + (y * x) % 3) % 2 == 0
                else:
                    flip = ((y + x) % 2 + (y * x) % 3) % 2 == 0
                if flip:
                    self.modules[y][x] = not self.modules[y][x]


def _penalty(modules):
    size = len(modules)
    score = 0

    def line_score(line):
        s = 0
        history = [0] * 7
        run_color = line[0]
        run_len = 0

        def close_run(color, length):
            nonlocal s
            if length >= 5:
                s += 3 + (length - 5)
            history.pop()
            history.insert(0, length)
            if color:  # dark run: check the finder-like pattern around it
                n = history[1]
                if (n > 0 and history[2] == n and history[3] == n * 3
                        and history[4] == n and history[5] == n):
                    if history[0] >= n * 4 and history[6] >= n:
                        s += 40
                    if history[6] >= n * 4 and history[0] >= n:
                        s += 40

        for cell in line:
            if cell == run_color:
                run_len += 1
            else:
                close_run(run_color, run_len)
                run_color = cell
                run_len = 1
        close_run(run_color, run_len)
        # Virtual light run past the edge (the finder-pattern border).
        history.pop()
        history.insert(0, size)
        n = history[1]
        if (run_color and n > 0 and history[2] == n and history[3] == n * 3
                and history[4] == n and history[5] == n):
            if history[0] >= n * 4 and history[6] >= n:
                s += 40
            if history[6] >= n * 4 and history[0] >= n:
                s += 40
        return s

    for y in range(size):
        score += line_score(modules[y])
    for x in range(size):
        score += line_score([modules[y][x] for y in range(size)])
    for y in range(size - 1):
        for x in range(size - 1):
            if (modules[y][x] == modules[y][x + 1]
                    == modules[y + 1][x] == modules[y + 1][x + 1]):
                score += 3
    dark = sum(sum(1 for cell in row if cell) for row in modules)
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    score += max(0, k) * 10
    return score


def _encode(text):
    payload = text.encode("utf-8")
    if not payload:
        return None
    version = None
    for candidate in range(1, 11):
        count_bits = 8 if candidate <= 9 else 16
        if 4 + count_bits + len(payload) * 8 <= _num_data_codewords(candidate) * 8:
            version = candidate
            break
    if version is None:
        return None
    codewords = _encode_codewords(payload, version)
    bits = []
    for byte in codewords:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    bits += [0] * _REMAINDER_BITS[version]
    best = None
    best_score = None
    for mask in range(8):
        builder = _QrBuilder(version)
        builder.draw_function_patterns()
        builder.place_data(bits)
        builder.apply_mask(mask)
        builder.draw_format_bits(mask)
        try:
            score = _penalty(builder.modules)
        except Exception:
            # Sweep R112: a mask whose penalty scoring fails is the
            # WORST candidate, never the best — the old fallback
            # (score 0 for the first mask) let a broken, unscored
            # matrix WIN mask selection.
            score = float("inf")
        if best_score is None or score < best_score:
            best = builder.modules
            best_score = score
    return best


def qr_matrix(text: str):
    """The QR module matrix for text as a list of rows of bools
    (True = dark module), quiet-zone border included — or None when
    the text cannot be encoded (empty or over 213 bytes). Encoded by
    the built-in encoder above: no third-party package involved."""
    try:
        body = _encode(text)
    except Exception as exc:
        # Sweep R113: an encoder failure stays a None return (the
        # caller's defined fallback) but must not be indistinguishable
        # from "text unencodable" — one stderr line keeps a regression
        # from presenting as the silent no-QR fallback.
        print(f"qr: encoding failed unexpectedly: {exc}", file=sys.stderr)
        return None
    if not body:
        return None
    width = len(body[0]) + 2 * BORDER
    blank = [False] * width
    out = [list(blank) for _ in range(BORDER)]
    for row in body:
        out.append([False] * BORDER + row + [False] * BORDER)
    out += [list(blank) for _ in range(BORDER)]
    return out


def qr_pixmap(text: str, scale: int = 5):
    """A QPixmap of the QR code for text — or None (see module
    docstring). Dark modules on a light card so phone cameras read
    it against the Forge's dark UI."""
    # Sweep R114: an unusable scale is "cannot render" (None), not a
    # null pixmap the caller mistakes for a real code.
    if not isinstance(scale, int) or scale < 1:
        return None
    matrix = qr_matrix(text)
    if matrix is None:
        return None
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPixmap, qRgb
        h = len(matrix); w = len(matrix[0])
        img = QImage(w, h, QImage.Format_RGB32)
        dark = qRgb(18, 18, 22); light = qRgb(246, 243, 236)
        for y, row in enumerate(matrix):
            for x, cell in enumerate(row):
                img.setPixel(x, y, dark if cell else light)
        return QPixmap.fromImage(img).scaled(
            w * scale, h * scale, Qt.IgnoreAspectRatio, Qt.FastTransformation)
    except Exception as exc:
        print(f"qr: rendering failed unexpectedly: {exc}", file=sys.stderr)
        return None
