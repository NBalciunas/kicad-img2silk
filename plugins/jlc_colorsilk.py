"""
JLCPCB / EasyEDA Pro multi-color silkscreen export.

This module makes the same files as EasyEDA Pro 3.2.149 (pcb.js):
  * The .FCTS, .FCBS and .FCBO files contain an encrypted SVG:
        RSA-OAEP-SHA256(aes_key) || RSA-OAEP-SHA256(aes_iv) || AES-128-GCM(svg)
    The AES key and the IV are random 16-byte values. The RSA key is the
    JLCPCB RSA-2048 public key. The KiCad Python does not include a crypto
    library. Thus, this module does AES-GCM and OAEP with hashlib only.
  * The .FCBM file is a Gerber file. It contains the board shape, three
    alignment marks (wYe), the EasyEDA header and the EasyEDA hash aperture
    (addHashApertureToGerber). The hash marks the design as imported from
    another tool.
Add these files to the normal Gerber zip file.
Units: EasyEDA uses a unit of 10 mil. In the SVG, the y axis points down, as in
KiCad. In the Gerber file and in the mark points, the y axis points up.
"""
import base64
import datetime
import hashlib
import os
import random
import re
from decimal import ROUND_HALF_UP, Decimal

try:
    import pcbnew
except ImportError:
    pcbnew = None

EDA_VERSION = "3.2.149"
SVG_EDA_VERSION = "2.4(2026-05-08)"
OBJECT_CONFIGS = ("Pad,Via,Track,Text,Image,Dimension,BoardOutline,BoardCutout,"
                  "CopperFilled,SolidRegion,FPCStiffener,Line,PlaneZone,"
                  "ComponentProperty,ComponentSilkscreen,TearDrop")
IMPORTED = "494d"     # EasyEDA hash prefix for designs from other tools
UNIT_MM = 0.254
BOARD_COLOR = "#FFFFFF"
SILK_COLOR = "#000000"
OUTLINE_INSET_MM = 0.2
MARK_INSET_MM = 3 * UNIT_MM
_APERTURE_MAX = 8191

# JLCPCB RSA-2048 public key (modulus and exponent) from the EasyEDA Pro pcb.js file.
_JLC_E = 65537
_JLC_N = int(
    "ccfb6e52a25e71a47fc16b5cb464fc42e56c9660c7dd4b77b3c7352ece00f8cf"
    "6bc2978b8e00d4a9f72b4ab4c706b97993f74e78911631e345ed24d64b98e859"
    "91341fad1cbdfd9b868ae60bd4ba091c2ae0bc34b9ad1840ca3ae91fea0ad2d5"
    "6b9c3a5a468f0b3e1bd316875ad7a3247e3d0b44faeeb5360e47a95ee30fa703"
    "427e5314f96804422a191257544c3ea0629bb65391ab5b8004d00422228fa37b"
    "bb6037c3e504c6cc6847eed0ba24672b61d6e81fb8b020ca88169de7407a0426"
    "3d9cc051edfa4a78bde00b03961c669a3165f96419cc3dcc7256c463a2900795"
    "cbf33091fc9ea90945307c76a7b4bc58fe0422d17246e61596b43c87fdf47697", 16)


def _xt(b):
    return ((b << 1) ^ (0x1b if b & 0x80 else 0)) & 0xff


def _make_tables():
    sbox = [0x63] * 256
    p = q = 1
    while True:
        p ^= _xt(p)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xff
        if q & 0x80:
            q ^= 0x09
        x = q
        for r in (1, 2, 3, 4):
            x ^= ((q << r) | (q >> (8 - r))) & 0xff
        sbox[p] = x ^ 0x63
        if p == 1:
            break
    t0 = [(_xt(s) << 24) | (s << 16) | (s << 8) | (_xt(s) ^ s) for s in sbox]
    ror = lambda w, n: ((w >> n) | (w << (32 - n))) & 0xffffffff
    return (sbox, t0, [ror(w, 8) for w in t0], [ror(w, 16) for w in t0],
            [ror(w, 24) for w in t0])


_S, _T0, _T1, _T2, _T3 = _make_tables()


def _expand_key(key):
    w = [int.from_bytes(key[i:i + 4], "big") for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = w[-1]
        if i % 4 == 0:
            t = ((_S[(t >> 16) & 255] << 24) | (_S[(t >> 8) & 255] << 16)
                 | (_S[t & 255] << 8) | _S[t >> 24]) ^ (rcon << 24)
            rcon = _xt(rcon)
        w.append(w[-4] ^ t)
    return w


def _aes_block(rk, block):
    """Encrypt one 128-bit block with AES-128. The block and the result are integers."""
    T0, T1, T2, T3, S = _T0, _T1, _T2, _T3, _S
    s0 = (block >> 96) ^ rk[0]
    s1 = ((block >> 64) & 0xffffffff) ^ rk[1]
    s2 = ((block >> 32) & 0xffffffff) ^ rk[2]
    s3 = (block & 0xffffffff) ^ rk[3]
    for k in range(4, 40, 4):
        s0, s1, s2, s3 = (
            T0[s0 >> 24] ^ T1[(s1 >> 16) & 255] ^ T2[(s2 >> 8) & 255] ^ T3[s3 & 255] ^ rk[k],
            T0[s1 >> 24] ^ T1[(s2 >> 16) & 255] ^ T2[(s3 >> 8) & 255] ^ T3[s0 & 255] ^ rk[k + 1],
            T0[s2 >> 24] ^ T1[(s3 >> 16) & 255] ^ T2[(s0 >> 8) & 255] ^ T3[s1 & 255] ^ rk[k + 2],
            T0[s3 >> 24] ^ T1[(s0 >> 16) & 255] ^ T2[(s1 >> 8) & 255] ^ T3[s2 & 255] ^ rk[k + 3])
    o = 0
    for a, b, c, d, r in ((s0, s1, s2, s3, rk[40]), (s1, s2, s3, s0, rk[41]),
                          (s2, s3, s0, s1, rk[42]), (s3, s0, s1, s2, rk[43])):
        o = (o << 32) | (((S[a >> 24] << 24) | (S[(b >> 16) & 255] << 16)
                          | (S[(c >> 8) & 255] << 8) | S[d & 255]) ^ r)
    return o


def _ghash_table(h):
    """Make a 4096-entry table. With this table, X*H in GF(2^128) needs only 16 lookups
    (one lookup for each byte of X)."""
    basis = []
    v = h
    for _ in range(128):
        basis.append(v)
        v = (v >> 1) ^ (0xe1 << 120 if v & 1 else 0)
    table = [0] * 4096
    for j in range(16):
        for b in range(1, 256):
            low = b & -b
            table[j * 256 + b] = table[j * 256 + (b ^ low)] ^ basis[8 * j + 8 - low.bit_length()]
    return table


def _ghash(table, data, acc=0):
    data += bytes(-len(data) % 16)
    for i in range(0, len(data), 16):
        x = (acc ^ int.from_bytes(data[i:i + 16], "big")).to_bytes(16, "big")
        acc = 0
        for j in range(16):
            acc ^= table[j * 256 + x[j]]
    return acc


def aes_gcm_encrypt(key, iv, plain):
    """Encrypt with AES-128-GCM without AAD. Return the ciphertext followed by the
    16-byte tag (WebCrypto layout)."""
    rk = _expand_key(key)
    table = _ghash_table(_aes_block(rk, 0))
    if len(iv) == 12:
        j0 = (int.from_bytes(iv, "big") << 32) | 1
    else:
        j0 = _ghash(table, iv + bytes(8) + (len(iv) * 8).to_bytes(8, "big"))
    hi, ctr = j0 & ~0xffffffff, j0 & 0xffffffff
    blocks = (len(plain) + 15) // 16
    stream = b"".join(_aes_block(rk, hi | ((ctr + i) & 0xffffffff)).to_bytes(16, "big")
                      for i in range(1, blocks + 1))[:len(plain)]
    cipher = (int.from_bytes(plain, "big") ^ int.from_bytes(stream, "big")).to_bytes(
        len(plain), "big") if plain else b""
    s = _ghash(table, cipher)
    s = _ghash(table, (len(cipher) * 8).to_bytes(16, "big"), s)
    return cipher + (s ^ _aes_block(rk, j0)).to_bytes(16, "big")


def _mgf1(seed, length):
    out = b"".join(hashlib.sha256(seed + c.to_bytes(4, "big")).digest()
                   for c in range((length + 31) // 32))
    return out[:length]


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def rsa_oaep_encrypt(msg, n=_JLC_N, e=_JLC_E, seed=None):
    """Encrypt with RSA-OAEP. Use SHA-256 for the hash and for MGF1. Use an empty label."""
    k = (n.bit_length() + 7) // 8
    db = hashlib.sha256(b"").digest() + bytes(k - len(msg) - 66) + b"\x01" + msg
    seed = seed or os.urandom(32)
    masked_db = _xor(db, _mgf1(seed, k - 33))
    em = b"\x00" + _xor(seed, _mgf1(masked_db, 32)) + masked_db
    return pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")


def encrypt(plain):
    """Put the SVG bytes into the .FCTS/.FCBS format."""
    key, iv = os.urandom(16), os.urandom(16)
    return rsa_oaep_encrypt(key) + rsa_oaep_encrypt(iv) + aes_gcm_encrypt(key, iv, plain)


def _js_fixed2(s):
    """Round as (+s).toFixed(2) in JavaScript. Round half up on the exact double value."""
    return str(Decimal(float(s)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def add_hash_aperture(text, prefix=IMPORTED, rnd=random):
    """Add the EasyEDA hash aperture (port of addHashApertureToGerber).
    The aperture selects must have the G54Dnn format."""
    lines = text.split("\n")
    ad_lines, ad_nums, found = [], [], False
    for g, line in enumerate(lines):
        if (g > 200 and not re.match(r"%AD|%AM", line)) or g > 200 + _APERTURE_MAX * 2:
            break
        m = re.match(r"%ADD(\d{2,4})\D", line)
        if m:
            ad_lines.append(line)
            ad_nums.append(int(m.group(1)))
            found = True
        elif found:
            break
    n = len(ad_nums)
    pick = n if n <= 5 else min(5 + int(rnd.random() * 5), n - 1)
    line = ad_lines[pick] if pick < n else None
    h = ad_nums[pick] if pick < n else None
    if line is None:
        if n == 0:
            h = 10
        else:
            h = ad_nums[-1] + 1
            if h >= _APERTURE_MAX:
                h = ad_nums[-2] + 1 if n >= 2 else 10
    u = re.sub(r"^(%ADD|G54D)(\d{2,4})(?=\D)",
               lambda m: m.group(0) if int(m.group(2)) < h or int(m.group(2)) == _APERTURE_MAX
               else m.group(1) + str(int(m.group(2)) + 1), text, flags=re.M)
    if line is None or not re.search(r",([\d.]+)", line):
        line = "%%ADD%dC,%.4f*%%" % (h, rnd.random())
    digest = hashlib.md5((prefix + u).encode()).hexdigest()
    tail = str(int(digest[-2:], 16) % 100 + 100)[-2:]

    def size(m):
        t = _js_fixed2(m.group(1)) + tail
        return "," + ("0.0100" if float(t) == 0 else t)
    d = re.sub(r",([\d.]+)", size, line, count=1)
    nxt = re.compile(r"^%%ADD%d(?=\D)" % (h + 1), re.M)
    if nxt.search(u):
        return nxt.sub(lambda m: d + "\n" + m.group(0), u, count=1)
    parts = u.split("\n")
    mo = 0
    for g, p in enumerate(parts):
        if not mo:
            if p.startswith("%MO"):
                mo = g
        elif re.match(r"%LP|G", p):
            while g > 0 and not parts[g - 1]:
                g -= 1
            parts.insert(g, d)
            return "\n".join(parts)
    return u


def check_hash_aperture(text, prefix=IMPORTED):
    """Find the aperture lines with size digits that agree with the hash of the remaining file."""
    lines = text.split("\n")
    hits = []
    for i, line in enumerate(lines):
        m = re.match(r"%ADD\d{2,4}[A-Za-z]*,(\d+\.\d{4})(?!\d)", line)
        if m:
            digest = hashlib.md5((prefix + "\n".join(lines[:i] + lines[i + 1:])).encode()).hexdigest()
            if str(int(digest[-2:], 16) % 100 + 100)[-2:] == m.group(1)[-2:]:
                hits.append(line)
    return hits


def _n(v):
    s = "%.4f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _chains(ps):
    """Get each outline and each hole of a SHAPE_POLY_SET as a list of (x, y) points in mm."""
    out = []
    for i in range(ps.OutlineCount()):
        for c in [ps.COutline(i)] + [ps.Hole(i, k) for k in range(ps.HoleCount(i))]:
            out.append([(pcbnew.ToMM(c.CPoint(j).x), pcbnew.ToMM(c.CPoint(j).y))
                        for j in range(c.PointCount())])
    return out


def _path(ps):
    return " ".join("M" + "L".join("%s %s" % (_n(x / UNIT_MM), _n(y / UNIT_MM)) for x, y in pts) + "Z"
                    for pts in _chains(ps) if pts)


def _inflated(ps, mm):
    out = pcbnew.SHAPE_POLY_SET(ps)
    out.Inflate(pcbnew.FromMM(mm), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, pcbnew.FromMM(0.005))
    return out


def _layer_polys(board, layer):
    ps = pcbnew.SHAPE_POLY_SET()
    board.ConvertBrdLayerToPolygonalContours(layer, ps)
    merged = pcbnew.SHAPE_POLY_SET()
    merged.BooleanAdd(ps)
    return merged


def board_outline(board):
    ps = pcbnew.SHAPE_POLY_SET()
    try:
        ok = board.GetBoardPolygonOutlines(ps)
    except TypeError:  # KiCad 10 added the aInferOutlineIfNecessary parameter.
        ok = board.GetBoardPolygonOutlines(ps, False)
    if not ok or ps.OutlineCount() == 0:
        raise ValueError("The board needs a closed outline on Edge.Cuts.")
    return ps


def pick_marks(pts):
    """Find three different points: bottom-left, top-left and top-right (y axis up).
    The EasyEDA function wYe has the same purpose. But its loop gives a different
    result for a different point order. Thus, this function selects the points by key."""
    c = min(pts, key=lambda p: (p[0], p[1]))
    h = min((p for p in pts if p != c), key=lambda p: (p[0], -p[1]))
    u = max((p for p in pts if p not in (c, h)), key=lambda p: (p[0], p[1]))
    if h[1] < c[1]:  # Only one point is at the left side. Use the highest point.
        h = max((p for p in pts if p != c), key=lambda p: (p[1], -p[0]))
        u = max((p for p in pts if p not in (c, h)), key=lambda p: (p[0], p[1]))
    return [c, h, u]


def _marks(outline):
    """Get the mark points in EasyEDA units with the y axis up.
    The SVG mark-points attribute uses this format."""
    return pick_marks([(round(x / UNIT_MM, 4), round(-y / UNIT_MM, 4))
                       for chain in _chains(_inflated(outline, -MARK_INSET_MM)) for x, y in chain])


def _header(layer, stamp, layer_cfg):
    return "".join("G04 %s*\n" % c for c in (
        "Layer: " + layer,
        "EasyEDA Pro v%s, %s" % (EDA_VERSION, stamp),
        "Gerber Generator version 0.3",
        "Scale: 100 percent, Rotated: No, Reflected: No",
        "Dimensions in millimeters",
        "Leading zeros omitted, absolute positions, 4 integers and 5 decimals",
        "Generated by one-click",
        "Object Configs: " + OBJECT_CONFIGS,
        "Layer Configs: " + layer_cfg,
        "Copper Areas: 0")) + "%FSLAX45Y45*%\n%MOMM*%\n"


def _gxy(x, y):
    """Change KiCad mm to Gerber 4.5 coordinates (y axis up)."""
    return "X%dY%d" % (round(x * 1e5), round(-y * 1e5))


def build_fcbm(outline, marks, stamp, layer_cfg):
    body = ["G04 colorScreenSilk_board Start*", "G04 PolygonModel Start*", "G54D10*"]
    for pts in _chains(outline):
        body.append("G01%sD02*" % _gxy(*pts[0]))
        body += ["G01%sD01*" % _gxy(*p) for p in pts[1:] + pts[:1]]
    body += ["G04 PolygonModel End*", "G04 colorScreenSilk_board End*", "",
             "G04 colorScreenSilk_Mark Start*", "G54D11*"]
    body += ["G01X%dY%dD03*" % (round(x * UNIT_MM * 1e5), round(y * UNIT_MM * 1e5)) for x, y in marks]
    body += ["G04 colorScreenSilk_Mark End*", "", "", "M02*", "", ""]
    return (_header("Fabrication_ColorfulBoardOutlineMark", stamp, layer_cfg)
            + "%%ADD10C,0.254*%%\n%%ADD11C,%s*%%\nG75*\n\n" % _n(2 * UNIT_MM) + "\n".join(body))


def _svg_head(box, marks):
    x, y, w, h = (_n(v) for v in box)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="no"?><svg\n'
            '        width="%smm"\n        height="%smm"\n'
            '\t\tboardBox="%s %s %s %s"\n        viewBox="%s %s %s %s"\n        version="1.1"\n'
            '\t\teda-version="%s"\n\t\tmark-points="%s"\n'
            '        xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"\n'
            '        xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"\n'
            '        xmlns:xlink="http://www.w3.org/1999/xlink"\n'
            '        xmlns="http://www.w3.org/2000/svg"\n'
            '        xmlns:svg="http://www.w3.org/2000/svg">'
            % (_n(box[2] * UNIT_MM), _n(box[3] * UNIT_MM), x, y, w, h, x, y, w, h,
               SVG_EDA_VERSION, " ".join("%s %s" % (_n(a), _n(b)) for a, b in marks)))


def _side(board, outline, back):
    """Get the printable area and the silkscreen of one side.
    The printable area is the inset outline without the mask openings."""
    printable = _inflated(outline, -OUTLINE_INSET_MM)
    printable.BooleanSubtract(_layer_polys(board, pcbnew.B_Mask if back else pcbnew.F_Mask))
    return printable, _layer_polys(board, pcbnew.B_SilkS if back else pcbnew.F_SilkS)


def build_svg(outline, marks, printable, silk, images, back):
    """Make the color silkscreen SVG for one side.
    Each image is (png_bytes, w_px, h_px, x, y, w, h in mm, back)."""
    bb = outline.BBox()
    box = (pcbnew.ToMM(bb.GetX()) / UNIT_MM, pcbnew.ToMM(bb.GetY()) / UNIT_MM,
           pcbnew.ToMM(bb.GetWidth()) / UNIT_MM, pcbnew.ToMM(bb.GetHeight()) / UNIT_MM)
    x, y, w, h = box
    svg = _svg_head(box, marks)
    svg += ("<defs><clipPath id='clipPath0'><path d=\"%s\" id=\"outline0\" stroke=\"none\" "
            "clip-rule=\"evenodd\" style=\"fill-opacity:1;fill-rule:evenodd;fill:block;\"/>"
            "</clipPath></defs>" % _path(printable))
    svg += "<g clip-path='url(#clipPath0)' transform=\"%s\">" % (
        "scale(-1 1) translate(%s 0)" % _n(-(x * 2 + w)) if back else "scale(1 1) translate(0 0)")
    svg += ('<path d="M%s %sL%s %sL%s %sL%s %sZ" fill="%s" stroke="none" stroke-width="0" id ="background" />'
            % (_n(x), _n(y), _n(x + w), _n(y), _n(x + w), _n(y + h), _n(x), _n(y + h), BOARD_COLOR))
    for png, w_px, h_px, ix, iy, iw, ih, iback in images:
        if iback != back:
            continue
        svg += ('<image width = "%d" height ="%d" preserveAspectRatio="none" '
                'xlink:href="data:image/png;base64,%s" transform="matrix(%s 0 0 %s %s %s)"/>'
                % (w_px, h_px, base64.b64encode(png).decode(),
                   _n(iw / UNIT_MM / w_px), _n(ih / UNIT_MM / h_px), _n(ix / UNIT_MM), _n(iy / UNIT_MM)))
    if silk.OutlineCount():
        svg += ('<path d="%s" id="silk" stroke="none" layerid="%d" '
                'style="fill-opacity:1;fill-rule:evenodd;fill:%s"/>' % (_path(silk), 4 if back else 3, SILK_COLOR))
    return svg + "</g></svg>"


def render_png(path, outline, printable, silk, images, back):
    """Make a PNG preview of the build_svg print, as seen from that side.
    The printer does not print the grey areas."""
    import io
    import wx
    bb = outline.BBox()
    bx, by, bw, bh = (pcbnew.ToMM(v) for v in (bb.GetX(), bb.GetY(), bb.GetWidth(), bb.GetHeight()))
    k = min(20.0, 4000.0 / max(bw, bh))
    px = lambda x: ((bx + bw - x) if back else (x - bx)) * k
    w, h = max(1, int(round(bw * k))), max(1, int(round(bh * k)))
    bmp = wx.Bitmap(w, h)
    dc = wx.MemoryDC(bmp)
    gc = wx.GraphicsContext.Create(dc)

    def fill(chains, colour, frame=False):
        p = gc.CreatePath()
        if frame:
            p.AddRectangle(0, 0, w, h)
        for pts in chains:
            p.MoveToPoint(px(pts[0][0]), (pts[0][1] - by) * k)
            for x, y in pts[1:]:
                p.AddLineToPoint(px(x), (y - by) * k)
            p.CloseSubpath()
        gc.SetBrush(wx.Brush(wx.Colour(colour)))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.FillPath(p, wx.ODDEVEN_RULE)

    fill([], BOARD_COLOR, frame=True)
    for png, _, _, ix, iy, iw, ih, iback in images:
        if iback == back:
            img = wx.Image(io.BytesIO(png))
            gc.DrawBitmap(wx.Bitmap(img.Mirror() if back else img),
                          px(ix + iw if back else ix), (iy - by) * k, iw * k, ih * k)
    fill(_chains(silk), SILK_COLOR)
    fill(_chains(printable), "#A0A0A0", frame=True)
    del gc
    dc.SelectObject(wx.NullBitmap)
    bmp.SaveFile(path, wx.BITMAP_TYPE_PNG)


def build_fcbo(outline):
    bb = outline.BBox()
    x, y, w, h = (pcbnew.ToMM(v) / UNIT_MM for v in (bb.GetX(), bb.GetY(), bb.GetWidth(), bb.GetHeight()))
    svg = ('<?xml version="1.0" encoding="UTF-8" standalone="no"?><svg\n'
           '        width="%smm"\n        height="%smm"\n        viewBox="%s %s %s %s"\n        version="1.1"\n'
           '        xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"\n'
           '        xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"\n'
           '        xmlns:xlink="http://www.w3.org/1999/xlink"\n'
           '        xmlns="http://www.w3.org/2000/svg"\n'
           '        xmlns:svg="http://www.w3.org/2000/svg">'
           % (_n(w * UNIT_MM), _n(h * UNIT_MM), _n(x), _n(y), _n(w), _n(h)))
    for pts in _chains(outline):
        d = "M" + "L".join("%s %s" % (_n(px / UNIT_MM), _n(py / UNIT_MM)) for px, py in pts + pts[:1])
        svg += '<path d="%s" fill="none" stroke="green" stroke-width="2" id ="boardOutline" />' % d
    return svg + "</svg>"


def _layer_cfg(board):
    inner = board.GetCopperLayerCount() - 2
    return ",".join(map(str, list(range(1, 12)) + [13, 14] + list(range(15, 15 + inner)) + [56]))


def export_files(board, folder, images):
    """Write the four color silkscreen files into a new folder.
    Write the PNG previews into folder/preview.
    Add the four files to the Gerber zip file with the normal Gerber files."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    outline = board_outline(board)
    marks = _marks(outline)
    sides = [_side(board, outline, back) for back in (False, True)]
    svgs = [build_svg(outline, marks, printable, silk, images, back).encode()
            for back, (printable, silk) in zip((False, True), sides)]
    files = (("Fabrication_ColorfulTopSilkscreen.FCTS", encrypt(svgs[0])),
             ("Fabrication_ColorfulBottomSilkscreen.FCBS", encrypt(svgs[1])),
             ("Fabrication_ColorfulBoardOutlineLayer.FCBO", encrypt(build_fcbo(outline).encode())),
             ("Fabrication_ColorfulBoardOutlineMark.FCBM",
              add_hash_aperture(build_fcbm(outline, marks, stamp, _layer_cfg(board))).encode()))
    os.makedirs(os.path.join(folder, "preview"))
    for name, data in files:
        with open(os.path.join(folder, name), "wb") as f:
            f.write(data)
    for back, (printable, silk) in zip((False, True), sides):
        render_png(os.path.join(folder, "preview", "bottom.png" if back else "top.png"),
                   outline, printable, silk, images, back)


if __name__ == "__main__":
    h = bytes.fromhex
    # Test vector from FIPS-197, appendix C.1.
    assert _aes_block(_expand_key(h("000102030405060708090a0b0c0d0e0f")),
                      0x00112233445566778899aabbccddeeff) == 0x69c4e0d86a7b0430d8cdb78070b4c55a
    # Test cases 1 and 2 from the GCM specification.
    assert aes_gcm_encrypt(bytes(16), bytes(12), b"") == h("58e2fccefa7e3061367f1d57a4e7455a")
    assert aes_gcm_encrypt(bytes(16), bytes(12), bytes(16)) == h(
        "0388dace60b6a392f328c2b971b2fe78ab6e47d42cec13bdf53a67b21257bddf")
    # OAEP padding: with e=1, the "ciphertext" is the encoded message.
    # Remove the padding and compare the result.
    em = rsa_oaep_encrypt(b"hello", e=1, seed=bytes(range(32)))
    assert em[0] == 0 and len(em) == 256
    seed = _xor(em[1:33], _mgf1(em[33:], 32))
    db = _xor(em[33:], _mgf1(seed, 223))
    assert seed == bytes(range(32))
    assert db[:32] == hashlib.sha256(b"").digest()
    assert db[32:].lstrip(b"\x00") == b"\x01hello"
    assert len(encrypt(b"x" * 100)) == 512 + 100 + 16
    # Hash aperture: make sure that each file has one correct hash line.
    # Test the append branch and the renumber branch.
    for count in (2, 9):
        aps = "".join("%%ADD%dC,0.%d*%%\n" % (10 + i, i + 1) for i in range(count))
        sel = "".join("G54D%d*\nX0Y0D03*\n" % (10 + i) for i in range(count))
        g = "G04 x*\n%FSLAX45Y45*%\n%MOMM*%\n" + aps + "G75*\n" + sel + "M02*\n"
        for seed in range(5):
            out = add_hash_aperture(g, rnd=random.Random(seed))
            assert len(check_hash_aperture(out)) == 1, (count, seed)
            assert len(re.findall(r"^%ADD", out, re.M)) == count + 1
    assert _js_fixed2("0.125") == "0.13" and _js_fixed2("0.5") == "0.50"
    # Marks on a rectangle (y axis up): bottom-left, top-left and top-right.
    rect = [(0, 0), (10, 0), (10, 5), (0, 5), (5, 0), (0, 2)]
    for i in range(len(rect)):
        for pts in (rect[i:] + rect[:i], (rect[i:] + rect[:i])[::-1]):
            assert pick_marks(pts) == [(0, 0), (0, 5), (10, 5)], i
    diamond = [(0, 0), (5, 5), (10, 0), (5, -5)]  # Only one point is at the left side.
    assert pick_marks(diamond) == [(0, 0), (5, 5), (10, 0)]
    print("self-check ok")
