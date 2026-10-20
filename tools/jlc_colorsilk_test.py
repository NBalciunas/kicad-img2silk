"""
Experimental: swap the top colour-silkscreen image in an EasyEDA Pro Gerber zip.

Takes a genuine EasyEDA export (with "Generate colorful silkscreen" ticked), builds a
new top-side colour SVG containing IMAGE stretched over the board, encrypts it the
same way EasyEDA Pro 3.2.149 does, and writes a copy of the zip with only
Fabrication_ColorfulTopSilkscreen.FCTS replaced. Everything else stays untouched.

File format (from EasyEDA's pcb.js):
    RSA-OAEP-SHA256(aes_key) || RSA-OAEP-SHA256(aes_iv) || AES-GCM(key, iv, svg)
with a random 16-byte key and IV and JLCPCB's RSA-2048 public key below.

Usage:
    py -m pip install cryptography
    py tools/jlc_colorsilk_test.py BASE.zip IMAGE.png OUT.zip
The plain SVG is written next to OUT.zip for preview in a browser.
"""
import base64
import os
import re
import struct
import sys
import zipfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

JLC_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzPtuUqJecaR/wWtctGT8
QuVslmDH3Ut3s8c1Ls4A+M9rwpeLjgDUqfcrSrTHBrl5k/dOeJEWMeNF7STWS5jo
WZE0H60cvf2bhormC9S6CRwq4Lw0ua0YQMo66R/qCtLVa5w6WkaPCz4b0xaHWtej
JH49C0T67rU2DkepXuMPpwNCflMU+WgEQioZEldUTD6gYpu2U5GrW4AE0AQiIo+j
e7tgN8PlBMbMaEfu0LokZyth1ugfuLAgyogWnedAegQmPZzAUe36Sni94AsDlhxm
mjFl+WQZzD3MclbEY6KQB5XL8zCR/J6pCUUwfHantLxY/gQi0XJG5hWWtDyH/fR2
lwIDAQAB
-----END PUBLIC KEY-----"""

TOP_NAME = "Fabrication_ColorfulTopSilkscreen.FCTS"
MARK_NAME = "Fabrication_ColorfulBoardOutlineMark.FCBM"
OUTLINE_NAME = "Gerber_BoardOutlineLayer.GKO"
UNIT_MM = 0.254       # EasyEDA internal unit = 10 mil
OUTLINE_INSET = 0.2   # EasyEDA clips the print 0.2 mm inside the board edge
BOARD_COLOR = "#FFFFFF"


def encrypt(plain):
    pub = serialization.load_pem_public_key(JLC_PUBLIC_KEY)
    oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    key, iv = os.urandom(16), os.urandom(16)
    return pub.encrypt(key, oaep) + pub.encrypt(iv, oaep) + AESGCM(key).encrypt(iv, plain, None)


def gerber_xy(text):
    """All X/Y coordinate pairs (mm) in a FSLAX45Y45 Gerber string."""
    return [(int(x) / 1e5, int(y) / 1e5) for x, y in re.findall(r"X(-?\d+)Y(-?\d+)", text)]


def bbox(points):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit("IMAGE must be a PNG")
    return struct.unpack(">II", data[16:24])


def parse_base(zf):
    mark = zf.read(MARK_NAME).decode()
    outline = zf.read(OUTLINE_NAME).decode()

    # Single-board rectangle: the "Rect" block inside the step-and-repeat section.
    rect = re.search(r"G04 Rect Start\*(.*?)G04 Rect End", mark, re.S)
    board = bbox(gerber_xy(rect.group(1)))

    # Alignment marks: flashed (D03) points in the colorScreenSilk_Mark block.
    marks_block = re.search(r"colorScreenSilk_Mark Start\*(.*?)colorScreenSilk_Mark End", mark, re.S).group(1)
    marks = [(int(x) / 1e5, int(y) / 1e5) for x, y in re.findall(r"X(-?\d+)Y(-?\d+)D03", marks_block)]

    sr = re.search(r"%SRX(\d+)Y(\d+)I([\d.]+)J([\d.]+)\*%", mark)
    step = (int(sr.group(1)), int(sr.group(2)), float(sr.group(3)), float(sr.group(4))) if sr else None

    panel = bbox(gerber_xy(outline)) if step else board
    return board, panel, marks, step


def build_svg(board, panel, marks, step, png):
    u = lambda mm: mm / UNIT_MM
    img_w, img_h = png_size(png)

    pminx, pminy, pmaxx, pmaxy = map(u, panel)
    pw, ph = pmaxx - pminx, pmaxy - pminy
    view = f"{pminx} {-pmaxy} {pw} {ph}"

    bminx, bminy, bmaxx, bmaxy = map(u, board)
    ins = u(OUTLINE_INSET)
    clip = (f"M{bminx + ins} {-(bmaxy - ins)} L{bmaxx - ins} {-(bmaxy - ins)} "
            f"L{bmaxx - ins} {-(bminy + ins)} L{bminx + ins} {-(bminy + ins)} Z")
    background = f"M{bminx} {-bmaxy} L{bmaxx} {-bmaxy} L{bmaxx} {-bminy} L{bminx} {-bminy} Z"

    sx, sy = (bmaxx - bminx) / img_w, (bmaxy - bminy) / img_h
    href = "data:image/png;base64," + base64.b64encode(png).decode()
    image = (f'<image width = "{img_w}" height ="{img_h}" preserveAspectRatio="none" xlink:href="{href}" '
             f'transform="matrix({sx} 0 0 {sy} {bminx} {-bmaxy})"/>')

    mark_attr = " ".join(f"{u(x)} {u(y)}" for x, y in marks)
    svg = (f'<?xml version="1.0" encoding="UTF-8" standalone="no"?><svg\n'
           f'        width="{pw * UNIT_MM}mm"\n        height="{ph * UNIT_MM}mm"\n'
           f'\t\tboardBox="{view}"\n        viewBox="{view}"\n        version="1.1"\n'
           f'\t\teda-version="2.4(2026-05-08)"\n\t\tmark-points="{mark_attr}"\n'
           '        xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"\n'
           '        xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"\n'
           '        xmlns:xlink="http://www.w3.org/1999/xlink"\n'
           '        xmlns="http://www.w3.org/2000/svg"\n'
           '        xmlns:svg="http://www.w3.org/2000/svg">')
    svg += (f"<defs><clipPath id='clipPath0'><path d=\"{clip}\" id=\"outline0\" stroke=\"none\"  "
            f"style=\"fill-opacity:1;fill-rule:nonzero;fill:block;\"/></clipPath></defs>")
    body = (f"<g clip-path='url(#clipPath0)' transform=\"scale(1 1) translate(0 0)\">"
            f'<path d="{background}" fill="{BOARD_COLOR}" stroke="none" stroke-width="0" id ="background" />'
            f"{image}</g>")

    if not step:
        return svg + body + "</svg>"

    cols, rows, dx, dy = step
    uses = "".join(
        f'<use xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'xlink:href="#multiSilk" x="{pminx + c * u(dx)}" y="{-pmaxy - r * u(dy)}" width="{pw}" height="{ph}" />'
        for r in range(rows) for c in range(cols))
    return svg + f'<defs><symbol id="multiSilk" viewBox="{view}">{body}</symbol></defs>{uses}</svg>'


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    base_zip, image_path, out_zip = sys.argv[1:]

    with zipfile.ZipFile(base_zip) as zf:
        if TOP_NAME not in zf.namelist():
            sys.exit(f"{base_zip} has no {TOP_NAME}; export it with colourful silkscreen enabled")
        board, panel, marks, step = parse_base(zf)
        svg = build_svg(board, panel, marks, step, open(image_path, "rb").read()).encode()
        blob = encrypt(svg)
        assert len(blob) == 512 + len(svg) + 16

        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as out:
            for info in zf.infolist():
                out.writestr(info, blob if info.filename == TOP_NAME else zf.read(info))

    svg_path = os.path.splitext(out_zip)[0] + "_top.svg"
    open(svg_path, "wb").write(svg)
    print(f"board   {board[2] - board[0]:.3f} x {board[3] - board[1]:.3f} mm")
    print(f"panel   {panel[2] - panel[0]:.3f} x {panel[3] - panel[1]:.3f} mm, step {step}")
    print(f"marks   {marks}")
    print(f"wrote   {out_zip} ({TOP_NAME}: {len(blob)} bytes)")
    print(f"preview {svg_path}")


if __name__ == "__main__":
    main()
