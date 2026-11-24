"""Pure Python 3.9 stdlib ISO/IEC 18004 QR Code Model 2 SVG generator.

Supports Byte mode (UTF-8) for Versions 1..10 with Error Correction Level M
(automatically falling back to Level L for payloads between 213 and 271 bytes).
Used to render NapCat QQ login QR codes directly in the local Web UI with zero
external dependencies or network requests.
"""

from typing import List, Optional, Tuple

# GF(256) with primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D)
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]

# Alignment pattern center coordinates for Versions 1..10
_ALIGNMENT_COORDS = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

# (version, level) -> (ec_cw_per_block, g1_blocks, g1_data_cw, g2_blocks, g2_data_cw)
_EC_TABLE = {
    # Level M (format bits 00)
    (1, "M"): (10, 1, 16, 0, 0),
    (2, "M"): (16, 1, 28, 0, 0),
    (3, "M"): (26, 1, 44, 0, 0),
    (4, "M"): (18, 2, 32, 0, 0),
    (5, "M"): (24, 2, 43, 0, 0),
    (6, "M"): (16, 4, 27, 0, 0),
    (7, "M"): (18, 4, 31, 0, 0),
    (8, "M"): (22, 2, 38, 2, 39),
    (9, "M"): (22, 3, 36, 2, 37),
    (10, "M"): (26, 4, 43, 1, 44),
    # Level L (format bits 01)
    (1, "L"): (7, 1, 19, 0, 0),
    (2, "L"): (10, 1, 34, 0, 0),
    (3, "L"): (15, 1, 55, 0, 0),
    (4, "L"): (20, 1, 80, 0, 0),
    (5, "L"): (26, 1, 108, 0, 0),
    (6, "L"): (18, 2, 68, 0, 0),
    (7, "L"): (20, 2, 78, 0, 0),
    (8, "L"): (24, 2, 97, 0, 0),
    (9, "L"): (30, 2, 116, 0, 0),
    (10, "L"): (18, 2, 68, 2, 69),
}

_FORMAT_EC_BITS = {"M": 0, "L": 1, "H": 2, "Q": 3}


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(degree: int) -> List[int]:
    poly = [1]
    for i in range(degree):
        root = _GF_EXP[i]
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, root)
        poly = nxt
    return poly


def _rs_remainder(data: List[int], ec_len: int) -> List[int]:
    gen = _rs_generator(ec_len)
    rem = [0] * ec_len
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        if factor:
            for i in range(ec_len):
                rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


def _select_version(payload_len: int, preferred_level: str = "M") -> Tuple[int, str]:
    levels = [preferred_level] + (["L"] if preferred_level != "L" else [])
    for lvl in levels:
        for ver in range(1, 11):
            _, g1_b, g1_d, g2_b, g2_d = _EC_TABLE[(ver, lvl)]
            total_data_cw = g1_b * g1_d + g2_b * g2_d
            count_bits = 8 if ver <= 9 else 16
            # 4-bit mode indicator + count_bits + payload bytes
            needed_bits = 4 + count_bits + payload_len * 8
            if needed_bits <= total_data_cw * 8:
                return ver, lvl
    raise ValueError(f"QR payload too long ({payload_len} bytes; max 271 bytes)")


def _encode_data_codewords(payload: bytes, version: int, level: str) -> List[int]:
    _, g1_b, g1_d, g2_b, g2_d = _EC_TABLE[(version, level)]
    total_data_cw = g1_b * g1_d + g2_b * g2_d
    total_data_bits = total_data_cw * 8

    bits: List[int] = []

    def push_bits(val: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((val >> shift) & 1)

    # Byte mode indicator = 0100
    push_bits(0b0100, 4)
    count_bits = 8 if version <= 9 else 16
    push_bits(len(payload), count_bits)
    for b in payload:
        push_bits(b, 8)

    # Terminator (up to 4 zero bits)
    term_len = min(4, total_data_bits - len(bits))
    bits.extend([0] * term_len)
    # Pad to byte boundary
    while len(bits) % 8 != 0:
        bits.append(0)

    codewords: List[int] = []
    for i in range(0, len(bits), 8):
        byte = 0
        for bit in bits[i : i + 8]:
            byte = (byte << 1) | bit
        codewords.append(byte)

    # Pad codewords alternating 0xEC, 0x11
    pad_bytes = (0xEC, 0x11)
    idx = 0
    while len(codewords) < total_data_cw:
        codewords.append(pad_bytes[idx & 1])
        idx += 1
    return codewords


def _interleave_blocks(data_cw: List[int], version: int, level: str) -> List[int]:
    ec_len, g1_b, g1_d, g2_b, g2_d = _EC_TABLE[(version, level)]
    data_blocks: List[List[int]] = []
    ec_blocks: List[List[int]] = []
    offset = 0
    for _ in range(g1_b):
        blk = data_cw[offset : offset + g1_d]
        offset += g1_d
        data_blocks.append(blk)
        ec_blocks.append(_rs_remainder(blk, ec_len))
    for _ in range(g2_b):
        blk = data_cw[offset : offset + g2_d]
        offset += g2_d
        data_blocks.append(blk)
        ec_blocks.append(_rs_remainder(blk, ec_len))

    out: List[int] = []
    max_data = max(len(b) for b in data_blocks)
    for i in range(max_data):
        for blk in data_blocks:
            if i < len(blk):
                out.append(blk[i])
    for i in range(ec_len):
        for blk in ec_blocks:
            out.append(blk[i])
    return out


def _format_bits(level: str, mask: int) -> int:
    data = (_FORMAT_EC_BITS[level] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


def _build_base_matrix(version: int) -> Tuple[List[List[int]], List[List[bool]]]:
    size = 17 + 4 * version
    modules = [[0] * size for _ in range(size)]
    is_func = [[False] * size for _ in range(size)]

    def set_func(r: int, c: int, val: int) -> None:
        if 0 <= r < size and 0 <= c < size:
            modules[r][c] = val
            is_func[r][c] = True

    # Timing patterns (placed first so finder/alignment overwrite cleanly)
    for i in range(size):
        bit = 1 if i % 2 == 0 else 0
        set_func(6, i, bit)
        set_func(i, 6, bit)

    # Finder patterns + 1-module white separator
    def draw_finder(cr: int, cc: int) -> None:
        for dr in range(-4, 5):
            for dc in range(-4, 5):
                r, c = cr + dr, cc + dc
                if 0 <= r < size and 0 <= c < size:
                    dist = max(abs(dr), abs(dc))
                    val = 1 if (dist != 2 and dist != 4) else 0
                    set_func(r, c, val)

    draw_finder(3, 3)
    draw_finder(3, size - 4)
    draw_finder(size - 4, 3)

    # Alignment patterns
    coords = _ALIGNMENT_COORDS[version]
    for r0 in coords:
        for c0 in coords:
            # Skip positions overlapping the 3 finder patterns
            if (r0 == 6 and c0 == 6) or (r0 == 6 and c0 == coords[-1]) or (r0 == coords[-1] and c0 == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    val = 1 if max(abs(dr), abs(dc)) != 1 else 0
                    set_func(r0 + dr, c0 + dc, val)

    # Reserve format info areas
    for i in range(9):
        if i != 6:
            set_func(8, i, 0)
            set_func(i, 8, 0)
    for i in range(8):
        set_func(8, size - 1 - i, 0)
        set_func(size - 1 - i, 8, 0)
    # Dark module (always 1)
    set_func(size - 8, 8, 1)

    # Version info areas for version >= 7
    if version >= 7:
        vbits = _version_bits(version)
        for i in range(18):
            bit = (vbits >> i) & 1
            a = size - 11 + (i % 3)
            b = i // 3
            set_func(a, b, bit)
            set_func(b, a, bit)

    return modules, is_func


def _place_codewords(modules: List[List[int]], is_func: List[List[bool]], codewords: List[int]) -> None:
    size = len(modules)
    bit_idx = 0
    total_bits = len(codewords) * 8

    col = size - 1
    upward = True
    while col >= 1:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for dc in (0, 1):
                c = col - dc
                if not is_func[r][c]:
                    bit = 0
                    if bit_idx < total_bits:
                        bit = (codewords[bit_idx >> 3] >> (7 - (bit_idx & 7))) & 1
                        bit_idx += 1
                    modules[r][c] = bit
        col -= 2
        upward = not upward


def _apply_format(modules: List[List[int]], level: str, mask: int) -> None:
    size = len(modules)
    fbits = _format_bits(level, mask)

    # Around top-left finder
    for i in range(6):
        modules[8][i] = (fbits >> (14 - i)) & 1
    modules[8][7] = (fbits >> 8) & 1
    modules[8][8] = (fbits >> 7) & 1
    modules[7][8] = (fbits >> 6) & 1
    for i in range(6):
        modules[5 - i][8] = (fbits >> (5 - i)) & 1

    # Bottom-left & top-right
    for i in range(7):
        modules[size - 1 - i][8] = (fbits >> (14 - i)) & 1
    for i in range(8):
        modules[8][size - 8 + i] = (fbits >> (7 - i)) & 1


def _mask_condition(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return ((r // 2) + (c // 3)) % 2 == 0
    if mask == 5:
        return ((r * c) % 2) + ((r * c) % 3) == 0
    if mask == 6:
        return (((r * c) % 2) + ((r * c) % 3)) % 2 == 0
    return (((r + c) % 2) + ((r * c) % 3)) % 2 == 0


def _penalty_score(modules: List[List[int]]) -> int:
    size = len(modules)
    score = 0

    # Rule 1: Adjacent modules in row/col of same color (5+ -> 3 + (len-5))
    for r in range(size):
        run_color = modules[r][0]
        run_len = 1
        for c in range(1, size):
            if modules[r][c] == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    score += 3 + (run_len - 5)
                run_color = modules[r][c]
                run_len = 1
        if run_len >= 5:
            score += 3 + (run_len - 5)

    for c in range(size):
        run_color = modules[0][c]
        run_len = 1
        for r in range(1, size):
            if modules[r][c] == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    score += 3 + (run_len - 5)
                run_color = modules[r][c]
                run_len = 1
        if run_len >= 5:
            score += 3 + (run_len - 5)

    # Rule 2: 2x2 blocks of same color
    for r in range(size - 1):
        row0 = modules[r]
        row1 = modules[r + 1]
        for c in range(size - 1):
            v = row0[c]
            if v == row0[c + 1] == row1[c] == row1[c + 1]:
                score += 3

    # Rule 3: Finder-like 1:1:3:1:1 patterns with 4 light modules on either side
    p1 = (1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0)
    p2 = (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1)
    for r in range(size):
        row = modules[r]
        for c in range(size - 10):
            seg = tuple(row[c : c + 11])
            if seg == p1 or seg == p2:
                score += 40
    for c in range(size):
        for r in range(size - 10):
            seg = tuple(modules[r + k][c] for k in range(11))
            if seg == p1 or seg == p2:
                score += 40

    # Rule 4: Balance of dark vs light modules
    dark = sum(sum(row) for row in modules)
    total = size * size
    k = abs(dark * 20 - total * 10) // total
    score += k * 10

    return score


def encode_qr_matrix(text: str, level: str = "M") -> List[List[int]]:
    """Encode a UTF-8 string into a QR code 2D matrix of 0/1 ints."""
    payload = str(text or "").encode("utf-8")
    if not payload:
        raise ValueError("QR content must not be empty")
    version, used_level = _select_version(len(payload), preferred_level=level)
    data_cw = _encode_data_codewords(payload, version, used_level)
    final_cw = _interleave_blocks(data_cw, version, used_level)

    base_mod, is_func = _build_base_matrix(version)
    _place_codewords(base_mod, is_func, final_cw)

    size = len(base_mod)
    best_matrix: Optional[List[List[int]]] = None
    best_score = 10**9

    for mask in range(8):
        cand = [row[:] for row in base_mod]
        for r in range(size):
            for c in range(size):
                if not is_func[r][c] and _mask_condition(mask, r, c):
                    cand[r][c] ^= 1
        _apply_format(cand, used_level, mask)
        sc = _penalty_score(cand)
        if sc < best_score:
            best_score = sc
            best_matrix = cand

    assert best_matrix is not None
    return best_matrix


def generate_qr_svg(text: str, border: int = 3, size_px: int = 220, level: str = "M") -> str:
    """Return a clean, crisp SVG string representing the QR code for `text`."""
    matrix = encode_qr_matrix(text, level=level)
    n = len(matrix)
    vb = n + border * 2

    # Combine horizontal runs into compact SVG path commands for minimal size & crisp rendering
    path_parts: List[str] = []
    for r, row in enumerate(matrix):
        y = r + border
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                width = c - start
                path_parts.append(f"M{start + border},{y}h{width}v1h-{width}z")
            else:
                c += 1

    path_d = "".join(path_parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb} {vb}" '
        f'width="{int(size_px)}" height="{int(size_px)}" shape-rendering="crispEdges" '
        f'role="img" aria-label="QQ 登录二维码">'
        f'<rect width="{vb}" height="{vb}" fill="#ffffff"/>'
        f'<path d="{path_d}" fill="#111827"/>'
        f"</svg>"
    )
