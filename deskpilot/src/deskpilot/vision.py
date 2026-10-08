"""Pixel fallbacks: OCR (text + click coords, cheap) and downscaled screenshots (images, pricier)."""
from __future__ import annotations

import io
import subprocess

from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageOps

# Claude bills images at roughly (width * height) / 750 tokens.
TOKENS_PER_PIXEL = 1 / 750


def grab(rect: tuple[int, int, int, int]) -> Image.Image:
    x, y, w, h = rect
    return ImageGrab.grab(bbox=(x, y, x + w, y + h), xdisplay=None).convert("RGB")


def _font(size: int):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def screenshot(rect, *, max_side=1024, gray=False, grid=0, quality=70) -> tuple[bytes, str]:
    """Returns (jpeg bytes, note). Grid lines/labels are in absolute SCREEN coordinates."""
    x0, y0, w, h = rect
    img = grab(rect)
    scale = min(1.0, max_side / max(w, h))
    if scale < 1:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    if gray:
        img = ImageOps.grayscale(img).convert("RGB")
    if grid:
        d = ImageDraw.Draw(img)
        f = _font(max(10, int(13 * max(scale, 0.75))))
        first_x = (x0 // grid + 1) * grid
        for gx in range(first_x, x0 + w, grid):
            px = int((gx - x0) * scale)
            d.line([(px, 0), (px, img.height)], fill=(255, 0, 255), width=1)
            d.text((px + 2, 2), str(gx), fill=(255, 0, 255), font=f, stroke_width=2, stroke_fill=(0, 0, 0))
        first_y = (y0 // grid + 1) * grid
        for gy in range(first_y, y0 + h, grid):
            py = int((gy - y0) * scale)
            d.line([(0, py), (img.width, py)], fill=(0, 255, 255), width=1)
            d.text((2, py + 2), str(gy), fill=(0, 255, 255), font=f, stroke_width=2, stroke_fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    toks = int(img.width * img.height * TOKENS_PER_PIXEL)
    note = (f"image {img.width}x{img.height} (~{toks} tok) of screen region x={x0} y={y0} w={w} h={h}; "
            f"screen_x = {x0} + img_x / {scale:.3f}, screen_y = {y0} + img_y / {scale:.3f}")
    return buf.getvalue(), note


def ocr(rect, *, find: str | None = None, min_conf: int = 40, upscale: float = 2.0) -> str:
    """OCR a screen region with tesseract; returns lines as 'x,y  text' (x,y = line center, screen coords)."""
    x0, y0, w, h = rect
    img = ImageOps.grayscale(grab(rect))
    # Tesseract wants dark text on light background; invert dark themes.
    if sum(img.resize((32, 32)).getdata()) / 1024 < 110:
        img = ImageOps.invert(img)
    if upscale != 1:
        img = img.resize((int(w * upscale), int(h * upscale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "11", "tsv"], input=buf.getvalue(),
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("tesseract failed: " + r.stderr.decode(errors="replace")[-300:])
    rows = r.stdout.decode(errors="replace").splitlines()[1:]
    lines: dict[tuple, list] = {}
    for row in rows:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            conf = float(c[10])
        except ValueError:
            continue
        if conf < min_conf:
            continue
        key = (int(c[2]), int(c[3]), int(c[4]))  # block, paragraph, line
        lx, ly, lw, lh = (int(v) for v in c[6:10])
        lines.setdefault(key, []).append((lx, ly, lw, lh, c[11]))
    out = []
    for words in lines.values():
        # Split a tesseract "line" where there are big horizontal gaps (separate UI elements).
        words.sort()
        groups, cur = [], [words[0]]
        for wd in words[1:]:
            prev = cur[-1]
            if wd[0] - (prev[0] + prev[2]) > 3 * max(prev[3], wd[3]):
                groups.append(cur); cur = [wd]
            else:
                cur.append(wd)
        groups.append(cur)
        for g in groups:
            text = " ".join(wd[4] for wd in g)
            gx1 = min(wd[0] for wd in g); gy1 = min(wd[1] for wd in g)
            gx2 = max(wd[0] + wd[2] for wd in g); gy2 = max(wd[1] + wd[3] for wd in g)
            cx = x0 + int((gx1 + gx2) / 2 / upscale); cy = y0 + int((gy1 + gy2) / 2 / upscale)
            out.append((cy, cx, text))
    out.sort(key=lambda t: (t[0] // 8, t[1]))  # reading order, tolerant to small y jitter
    if find:
        fl = find.lower()
        out = [t for t in out if fl in t[2].lower()]
        if not out:
            return f"no OCR text matching {find!r} (OCR can miss tiny/stylized text; try screenshot)"
    return "\n".join(f"{cx},{cy}  {text}" for cy, cx, text in out) or "(no text recognized)"
