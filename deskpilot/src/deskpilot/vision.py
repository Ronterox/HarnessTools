"""Pixel fallbacks: OCR (text + click coords, cheap) and downscaled screenshots (images, pricier)."""
from __future__ import annotations

import difflib
import io
import subprocess

from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageOps

from . import xutil

# Claude bills images at roughly (width * height) / 750 tokens.
TOKENS_PER_PIXEL = 1 / 750


def grab(rect: tuple[int, int, int, int]) -> Image.Image:
    x, y, w, h = rect
    return ImageGrab.grab(bbox=(x, y, x + w, y + h), xdisplay=xutil.current_display()).convert("RGB")


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
    out = ocr_lines(rect, min_conf=min_conf, upscale=upscale)
    if find:
        out = match(out, find)
        if not out:
            return f"no OCR text matching {find!r} (OCR can miss tiny/stylized text; try screenshot)"
    return "\n".join(f"{cx},{cy}  {text}" for cx, cy, text in out) or "(no text recognized)"


def match(lines: list[tuple[int, int, str]], find: str) -> list[tuple[int, int, str]]:
    """Lines containing find (case-insensitive), the closest to an exact match first."""
    fl = find.lower().strip()
    hits = [t for t in lines if fl in t[2].lower()]
    if hits:
        return sorted(hits, key=lambda t: (t[2].lower().strip() != fl, len(t[2])))
    # OCR misreads small UI text ("git" -> "gil"): fall back to the closest same-length stretch.
    scored = []
    for t in lines:
        s, n = t[2].lower(), len(fl)
        best = max((difflib.SequenceMatcher(None, fl, s[i:i + n]).ratio()
                    for i in range(max(1, len(s) - n + 1))), default=0)
        if best >= 0.8:
            scored.append((-best, len(t[2]), t))
    return [t for _, _, t in sorted(scored)]


def ocr_lines(rect, *, min_conf: int = 40, upscale: float = 2.0) -> list[tuple[int, int, str]]:
    """OCR'd text lines as (x, y, text), x,y = line center in screen coords, in reading order."""
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
            boxes = [(x0 + wd[0] / upscale, y0 + wd[1] / upscale, wd[2] / upscale, wd[3] / upscale, wd[4])
                     for wd in g]
            out.append((cy, cx, text, boxes))
    out.sort(key=lambda t: (t[0] // 8, t[1]))  # reading order, tolerant to small y jitter
    global _last_words
    _last_words = {(cx, cy, text): boxes for cy, cx, text, boxes in out}
    return [(cx, cy, text) for cy, cx, text, _ in out]


_last_words: dict = {}  # (x, y, text) of each line from the last ocr_lines() -> its word boxes


def _norm(w: str) -> str:
    return "".join(ch for ch in w.lower() if ch.isalnum())


def _labels(words: list) -> list[list]:
    """An OCR line split into labels: a gap wider than the text height starts a new one (a button
    row "Cancel   Don't Save   Save" is one OCR line but three labels)."""
    out, cur = [], []
    for w in words:
        if cur and w[0] - (cur[-1][0] + cur[-1][2]) > max(cur[-1][3], w[3]):
            out.append(cur)
            cur = []
        cur.append(w)
    return out + [cur] if cur else out


def _misread(a: str, b: str) -> bool:
    """b looks like an OCR misread of a ("gil status" for "git status"): 5+ chars, the same length
    (a few chars of slack only for long text, since misreads swap characters rather than add them),
    80% similar. "Delete" never matches "Deleted"."""
    if len(a) < 5 or abs(len(a) - len(b)) > (len(a) // 8 if len(a) >= 8 else 0):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _hit(span: list) -> tuple[int, int, str]:
    return (int(sum(w[0] + w[2] / 2 for w in span) / len(span)),
            int(sum(w[1] + w[3] / 2 for w in span) / len(span)), " ".join(w[4] for w in span))


def locate(lines: list[tuple[int, int, str]], find: str) -> tuple[int, int, str]:
    """Where to click for `find` among OCR lines from the last ocr_lines(). Rules, safest first:
    - a label that IS the text (whole words, case/punctuation-insensitive): "Save" hits the Save
      button, never "Don't Save" or "Save As" (they're other labels);
    - a multi-word phrase inside a longer label ("git status" in "$ git status --short"), only
      when it occurs exactly once; a single word never matches inside a longer label;
    - an OCR misread of either (see _misread), only when it is the single candidate.
    The click point is the center of the matched words. Raises when nothing or several match."""
    want = [_norm(w) for w in find.split() if _norm(w)]
    if not want:
        raise RuntimeError("find= needs some letters or digits")
    n, phrase = len(want), " ".join(want)
    whole, part, fuzzy, inside = [], [], [], []
    for line in lines:
        for label in _labels(_last_words.get(line) or []):
            norms = [_norm(w[4]) for w in label]
            joined = " ".join(norms)
            if norms == want:
                whole.append(_hit(label))
            elif _misread(phrase, joined):
                fuzzy.append(_hit(label))
            else:
                for i in range(len(label) - n + 1):
                    got = norms[i:i + n]
                    if got == want:
                        (part if n >= 2 else inside).append((_hit(label[i:i + n]), " ".join(w[4] for w in label)))
                    elif n >= 2 and _misread(phrase, " ".join(got)):
                        fuzzy.append(_hit(label[i:i + n]))

    def many(kind, hits):
        return RuntimeError(f"{find!r} matches {len(hits)} {kind}: "
                            + ", ".join(f"{h[2]!r} at {h[0]},{h[1]}" for h in hits[:6])
                            + " — nothing sent; click the one you mean by x,y")
    if len(whole) == 1:
        return whole[0]
    if whole:
        raise many("labels", whole)
    if len(part) == 1:
        return part[0][0]
    if part:
        raise many("places", [p[0] for p in part])
    if len(fuzzy) == 1:
        return fuzzy[0]
    if fuzzy:
        raise many("near-matches (OCR misreads?)", fuzzy)
    if inside:
        raise RuntimeError(f"{find!r} only appears inside longer labels: "
                           + ", ".join(repr(lab) for _, lab in inside[:6])
                           + " — nothing sent; give the whole label or click by x,y")
    raise RuntimeError(f"no text matching {find!r} found by OCR; nothing sent — try read_screen")


def strip(images: list[Image.Image], labels: list[str], *, max_side: int = 1400,
          quality: int = 70) -> tuple[bytes, str]:
    """Several snapshots side by side (wrapping into rows) in one JPEG: one image's token cost for a
    whole sequence. Each frame is labeled with its step."""
    n = len(images)
    cols = n if n <= 4 else (3 if n <= 9 else 4)
    rows = (n + cols - 1) // cols
    fw = max(i.width for i in images); fh = max(i.height for i in images)
    gap, lab = 6, 22
    sheet = Image.new("RGB", (cols * fw + (cols - 1) * gap, rows * (fh + lab) + (rows - 1) * gap), (40, 40, 40))
    d = ImageDraw.Draw(sheet)
    font = _font(16)
    for k, (img, label) in enumerate(zip(images, labels)):
        cx, cy = (k % cols) * (fw + gap), (k // cols) * (fh + lab + gap)
        d.text((cx + 4, cy + 2), label, fill=(255, 220, 120), font=font)
        sheet.paste(img, (cx, cy + lab))
    scale = min(1.0, max_side / max(sheet.size))
    if scale < 1:
        sheet = sheet.resize((int(sheet.width * scale), int(sheet.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=quality)
    tok = int(sheet.width * sheet.height * TOKENS_PER_PIXEL)
    return buf.getvalue(), f"{n} frames, {sheet.width}x{sheet.height} (~{tok} tokens), left to right, top to bottom"
