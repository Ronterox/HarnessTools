"""Thumbnails of the images a timeline shows (SCHEMA.md "v6 additions"), stored with the diffs sidecar (diffs.py).

thumb(rel, text, images) -> a `data:` URI (str) or None, for a file whose `fmt` is `image` or `svg`:
- svg: the snapshot's text itself, when it is at most SVG_MAX bytes (`data:image/svg+xml;base64,...`). A reader
  draws it through <img> (never inlined into the page), where its scripts do not run.
- raster (png, jpeg, gif, webp, ico, bmp): the snapshot holds a binary file as git's bytes decoded with 'replace'
  (replay.py), so its bytes are lost there. GitImages finds them back: the git object whose bytes decode to exactly
  that text (any path, any commit of the repository: a seed or a reconciled commit put the text there); none, or
  several that differ, is no thumbnail. The disk is never read. Pillow downscales them to THUMB px on the long side,
  as PNG; without Pillow, an original of at most RAW_MAX bytes is used as it is.
  Bounds: git blobs over BLOB_MAX bytes are never read (their size is asked first); an image over MAX_PIXELS is
  never decoded (its size is in its header, read lazily), and a JPEG is decoded at a reduced scale (draft). So a
  decompression bomb (a few KB of PNG for 9000 x 9000 pixels) costs nothing.
  Known limit: replay's 'replace' decoding maps every invalid byte to U+FFFD, so two different committed images can
  decode to the same text (e.g. differing only in one invalid byte); such an image gets no thumbnail.
A thumbnail is made once per image content (memoised by the bytes' sha1).
"""
import base64, hashlib, io, os, subprocess, threading

THUMB, SVG_MAX, RAW_MAX = 64, 64 * 1024, 24 * 1024
BLOB_MAX, MAX_PIXELS = 8 * 1024 * 1024, 16_000_000
RASTER = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp',
          'ico': 'image/x-icon', 'bmp': 'image/bmp'}
try:
    from PIL import Image
except ImportError:          # no Pillow: small originals only
    Image = None


def _key(text):
    return hashlib.sha1(text.encode('utf-8', 'surrogatepass')).digest()


class GitImages:
    """The bytes of the raster images in a repository's history, found by the text replay decodes them to. The
    index ({sha1 of that text: {blob sha}} over every image blob reachable from any ref; the blobs are read one at a
    time to key them, then dropped) is built on the first lookup and again when a lookup misses and the refs moved
    since (live mode: a commit made meanwhile); a hit reads its one blob again."""

    def __init__(self, root):
        self.root, self.refs, self.by_text, self.done = root, None, {}, set()

    def _git(self, *args, data=None):
        return subprocess.run(['git', '-C', self.root, *args], input=data, capture_output=True)

    def _index(self):
        refs = self._git('for-each-ref', '--format=%(objectname)').stdout + self._git('rev-parse', 'HEAD').stdout
        if refs == self.refs:
            return
        self.refs = refs
        objs = self._git('rev-list', '--all', '--objects').stdout.decode('utf-8', 'replace').splitlines()
        want = [line.split(' ', 1)[0] for line in objs
                if ' ' in line and line.rsplit('.', 1)[-1].lower() in RASTER and line.split(' ', 1)[0] not in self.done]
        want = list(dict.fromkeys(want))
        if not want:
            return
        self.done.update(want)
        sizes = self._git('cat-file', '--batch-check', data=''.join(s + '\n' for s in want).encode()).stdout.split(b'\n')
        small = [h[0].decode() for h in (line.split() for line in sizes) if len(h) == 3 and int(h[2]) <= BLOB_MAX]
        p = subprocess.Popen(['git', '-C', self.root, 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        feed = threading.Thread(target=lambda: (p.stdin.write(''.join(s + '\n' for s in small).encode()), p.stdin.close()))
        feed.start()
        for sha in small:                                    # one blob in memory at a time
            head = p.stdout.readline().split()
            if len(head) != 3:
                continue
            data = p.stdout.read(int(head[2]))
            p.stdout.read(1)
            self.by_text.setdefault(_key(data.decode('utf-8', 'replace')), set()).add(sha)
        feed.join()
        p.stdout.close()
        p.wait()

    def bytes_for(self, text):
        """The bytes of the one image git holds that decodes to text, else None."""
        k = _key(text)
        if k not in self.by_text:
            self._index()
        hits = self.by_text.get(k, set())
        return self._git('cat-file', 'blob', next(iter(hits))).stdout if len(hits) == 1 else None


_memo = {}


def thumb(rel, text, images):
    """The thumbnail data URI of image file rel (snapshot text `text`; images: a GitImages or None), or None."""
    ext = rel.rsplit('.', 1)[-1].lower()
    if ext == 'svg':
        raw = text.encode('utf-8', 'surrogateescape') if '\x00' not in text else b''
        return 'data:image/svg+xml;base64,' + base64.b64encode(raw).decode() if raw and len(raw) <= SVG_MAX else None
    if ext not in RASTER or images is None:
        return None
    data = images.bytes_for(text)
    if data is None:
        return None
    h = hashlib.sha1(data).digest()
    if h not in _memo:
        _memo[h] = _make(data, RASTER[ext])
    return _memo[h]


def _make(data, mime):
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as im:           # (lazy: the header only, so far)
                if im.width * im.height > MAX_PIXELS:
                    return None
                im.seek(0)
                im.draft('RGB', (THUMB * 2, THUMB * 2))          # a JPEG decodes at a reduced scale
                im.thumbnail((THUMB, THUMB))
                buf = io.BytesIO()
                im.convert('RGBA').save(buf, 'PNG')
            return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
        except Exception:      # not an image Pillow reads
            return None
    return f'data:{mime};base64,' + base64.b64encode(data).decode() if len(data) <= RAW_MAX else None


def images_for(root):
    """A GitImages for root when it is in a git repository, else None."""
    if not root or not os.path.isdir(root):
        return None
    r = subprocess.run(['git', '-C', root, 'rev-parse', '--is-inside-work-tree'], capture_output=True)
    return GitImages(root) if r.returncode == 0 else None
