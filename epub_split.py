#!/usr/bin/env nix-shell
#!nix-shell -i python3 -p python3
"""
epub_split.py - extract plain text from an EPUB, one file per chapter.

Writes ch_0001.txt, ch_0002.txt, ... into an output directory, following the
reading order defined by the book's spine (and, when useful, its table of
contents).

Standard library only - no ebooklib, no BeautifulSoup, no pip.

Usage:
    ./epub_split.py book.epub
    ./epub_split.py book.epub -o out/ --split toc --wrap 80
"""

import argparse
import posixpath
import re
import sys
import textwrap
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

CONTAINER = "META-INF/container.xml"
HTML_TYPES = ("application/xhtml+xml", "text/html")
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
MIME_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/svg+xml": ".svg", "image/webp": ".webp",
    "image/tiff": ".tif",
}

# Tags that end a line of text.
LINE_TAGS = {
    "br", "td", "th", "dt", "dd", "li", "caption", "option", "header", "footer",
}
# Void elements: HTMLParser reports <br/> as start+end, so only break once.
VOID_TAGS = {"br", "hr", "img", "meta", "link", "area", "col", "input", "source"}
# Tags that end a paragraph (blank line between).
PARA_TAGS = {
    "p", "div", "tr", "blockquote", "section", "article", "aside",
    "figure", "figcaption", "pre", "hr", "table", "ul", "ol", "dl", "main",
    "address", "form",
} | HEADINGS


# --------------------------------------------------------------------------- #
# XML / text helpers
# --------------------------------------------------------------------------- #

def strip_ns(root):
    """Drop XML namespaces so we can query with plain tag names."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        for key in list(el.attrib):
            if "}" in key:
                el.attrib[key.split("}", 1)[1]] = el.attrib.pop(key)
    return root


def decode(data: bytes) -> str:
    m = re.match(rb"\s*<\?xml[^>]*encoding=['\"]([\w\-]+)['\"]", data[:200])
    candidates = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
    if m:
        candidates.insert(0, m.group(1).decode("ascii", "ignore"))
    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def normalize(text: str, wrap: int = 0) -> str:
    """Collapse whitespace, keep at most one blank line between paragraphs."""
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\r", "\n")
    out, blank = [], 0
    for raw in text.split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if line:
            out.append(textwrap.fill(line, wrap) if wrap else line)
            blank = 0
        else:
            blank += 1
            if blank == 1 and out:
                out.append("")
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# HTML parsing
# --------------------------------------------------------------------------- #

class DocParser(HTMLParser):
    """Collect text fragments, anchor positions and a title from one document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []            # text fragments, in order
        self.anchors = {}          # element id -> index into self.parts
        self._skip = 0
        self._in_title = False
        self._title_buf = []
        self._in_heading = 0
        self._head_buf = []
        self.title = None
        self.heading = None

    def _break(self, want):
        """Ensure `want` newlines precede the next text, without stacking up."""
        have = 0
        for part in reversed(self.parts):
            if part.strip():
                break
            have += part.count("\n")
            if have >= want:
                return
        self.parts.append("\n" * (want - have))

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
            return

        anchor = attrs.get("id") or (attrs.get("name") if tag == "a" else None)
        if anchor and anchor not in self.anchors:
            self.anchors[anchor] = len(self.parts)

        if tag == "br":
            self._break(1)
        elif tag in PARA_TAGS:
            self._break(2)
        elif tag in LINE_TAGS:
            self._break(1)

        if tag in HEADINGS:
            self._in_heading += 1
        if tag == "img" and attrs.get("alt"):
            self.parts.append(f"[{attrs['alt'].strip()}]")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
            self.title = self.title or normalize("".join(self._title_buf))
            return
        if tag in HEADINGS:
            self._in_heading = max(0, self._in_heading - 1)
            if self.heading is None:
                text = normalize("".join(self._head_buf))
                if text:
                    self.heading = text.replace("\n", " ")
            self._head_buf.clear()
        if tag in PARA_TAGS:
            self._break(2)
        elif tag in LINE_TAGS:
            self._break(1)

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self._title_buf.append(data)
            return
        self.parts.append(data)
        if self._in_heading and self.heading is None:
            self._head_buf.append(data)


class NavParser(HTMLParser):
    """Pull (href, label) pairs out of an EPUB 3 navigation document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._nav_depth = 0
        self._toc_nav = False
        self._href = None
        self._buf = []
        self._saw_toc = False

    def handle_starttag(self, tag, attrs):
        tag, attrs = tag.lower(), dict(attrs)
        if tag == "nav":
            self._nav_depth += 1
            kind = attrs.get("epub:type") or attrs.get("type") or ""
            if "toc" in kind or (not self._saw_toc and not kind):
                self._toc_nav = True
                self._saw_toc = True
        elif tag == "a" and attrs.get("href") and (self._toc_nav or not self._saw_toc):
            self._href = attrs["href"]
            self._buf = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "nav":
            self._nav_depth = max(0, self._nav_depth - 1)
            if self._nav_depth == 0:
                self._toc_nav = False
        elif tag == "a" and self._href is not None:
            self.links.append((self._href, normalize("".join(self._buf)).replace("\n", " ")))
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)


class ImgParser(HTMLParser):
    """Collect image sources from a document, including SVG <image> wrappers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.srcs = []

    def handle_starttag(self, tag, attrs):
        tag, attrs = tag.lower(), dict(attrs)
        if tag == "img":
            src = attrs.get("src")
        elif tag == "image":
            src = attrs.get("xlink:href") or attrs.get("href")
        else:
            return
        if src:
            self.srcs.append(src)


# --------------------------------------------------------------------------- #
# EPUB container
# --------------------------------------------------------------------------- #

class Epub:
    def __init__(self, path):
        self.zf = zipfile.ZipFile(path)
        self._names = {n.lower(): n for n in self.zf.namelist()}
        self.opf_path = self._find_opf()
        self.opf_dir = posixpath.dirname(self.opf_path)
        opf = strip_ns(ET.fromstring(self.zf.read(self.opf_path)))
        self.opf = opf
        self.guide = [(r.get("type") or "", r.get("href") or "")
                      for r in opf.iterfind("guide/reference")]

        self.manifest = {}
        for item in opf.iterfind("manifest/item"):
            self.manifest[item.get("id")] = {
                "href": item.get("href", ""),
                "type": (item.get("media-type") or "").lower(),
                "props": item.get("properties") or "",
            }

        spine = opf.find("spine")
        self.spine = [i.get("idref") for i in spine.iterfind("itemref")] if spine is not None else []
        self.ncx_id = spine.get("toc") if spine is not None else None
        self.title = (opf.findtext("metadata/title") or "").strip()

    # -- zip helpers ------------------------------------------------------- #

    def _find_opf(self):
        if CONTAINER.lower() not in self._names:
            raise SystemExit("Not a valid EPUB: META-INF/container.xml is missing.")
        root = strip_ns(ET.fromstring(self.zf.read(self._names[CONTAINER.lower()])))
        rf = root.find(".//rootfile")
        if rf is None or not rf.get("full-path"):
            raise SystemExit("Not a valid EPUB: no rootfile in container.xml.")
        return rf.get("full-path")

    def resolve(self, href, base=None):
        href = unquote(href.split("#", 1)[0])
        base = self.opf_dir if base is None else base
        path = posixpath.normpath(posixpath.join(base, href)) if base else posixpath.normpath(href)
        return path.lstrip("/")

    def exists(self, path):
        return path.lower() in self._names

    def raw(self, path):
        real = self._names.get(path.lower())
        return None if real is None else self.zf.read(real)

    def read(self, path):
        data = self.raw(path)
        return None if data is None else decode(data)

    # -- structure --------------------------------------------------------- #

    def documents(self):
        """Spine documents as (path, manifest_entry), nav document excluded."""
        docs, seen = [], set()
        for idref in self.spine:
            item = self.manifest.get(idref)
            if not item or "nav" in item["props"].split():
                continue
            if item["type"] and item["type"] not in HTML_TYPES:
                continue
            path = self.resolve(item["href"])
            if path in seen:
                continue
            seen.add(path)
            docs.append((path, item))
        return docs

    def _image_in(self, doc_path):
        """First existing image referenced by an XHTML/SVG document."""
        text = self.read(doc_path)
        if not text:
            return None
        p = ImgParser()
        try:
            p.feed(text)
        except Exception:
            return None
        base = posixpath.dirname(doc_path)
        for src in p.srcs:
            cand = self.resolve(src, base)
            if self.exists(cand):
                return cand
        return None

    def cover_image(self):
        """Locate the cover image, trying each convention publishers use."""
        # 1. EPUB 3: manifest item flagged properties="cover-image".
        for item in self.manifest.values():
            if "cover-image" in item["props"].split():
                path = self.resolve(item["href"])
                if self.exists(path):
                    return path

        # 2. EPUB 2: <meta name="cover" content="item-id"/> in the metadata.
        for meta in self.opf.iterfind("metadata/meta"):
            if (meta.get("name") or "").lower() != "cover":
                continue
            item = self.manifest.get(meta.get("content") or "")
            if not item:
                continue
            path = self.resolve(item["href"])
            if not self.exists(path):
                continue
            if item["type"].startswith("image/"):
                return path
            hit = self._image_in(path)       # the id pointed at a cover page
            if hit:
                return hit

        # 3. A cover *page*: guide reference, or the opening spine document.
        pages = [self.resolve(h) for t, h in self.guide if "cover" in t.lower()]
        pages += [p for p, _ in self.documents()[:2]]
        for page in pages:
            if self.exists(page):
                hit = self._image_in(page)
                if hit:
                    return hit

        # 4. Last resort: an image in the manifest that is simply named "cover".
        for item in self.manifest.values():
            if item["type"].startswith("image/") and "cover" in item["href"].lower():
                path = self.resolve(item["href"])
                if self.exists(path):
                    return path
        return None

    def toc(self):
        """Table of contents as an ordered list of (doc_path, fragment, label)."""
        entries = self._toc_from_nav() or self._toc_from_ncx()
        out, seen = [], set()
        for href, label in entries:
            base, _, frag = href.partition("#")
            if not base:
                continue
            key = (self.resolve(base, self._toc_base), frag)
            if key in seen:
                continue
            seen.add(key)
            out.append((key[0], frag, label))
        return out

    def _toc_from_nav(self):
        for item in self.manifest.values():
            if "nav" in item["props"].split():
                path = self.resolve(item["href"])
                text = self.read(path)
                if not text:
                    return []
                self._toc_base = posixpath.dirname(path)
                p = NavParser()
                p.feed(text)
                return p.links
        return []

    def _toc_from_ncx(self):
        item = self.manifest.get(self.ncx_id)
        if not item:
            item = next((i for i in self.manifest.values()
                         if i["type"] == "application/x-dtbncx+xml"), None)
        if not item:
            return []
        path = self.resolve(item["href"])
        raw = self.read(path)
        if not raw:
            return []
        self._toc_base = posixpath.dirname(path)
        try:
            ncx = strip_ns(ET.fromstring(raw))
        except ET.ParseError:
            return []
        links = []
        for pt in ncx.iterfind(".//navPoint"):
            content = pt.find("content")
            if content is None or not content.get("src"):
                continue
            label = normalize("".join(pt.find("navLabel").itertext())) if pt.find("navLabel") is not None else ""
            links.append((content.get("src"), label.replace("\n", " ")))
        return links


# --------------------------------------------------------------------------- #
# Chapter building
# --------------------------------------------------------------------------- #

def build_chapters(book, split, min_chars, wrap):
    docs = book.documents()
    toc_by_doc = {}
    for path, frag, label in book.toc():
        toc_by_doc.setdefault(path, []).append((frag, label))

    parsed = {}
    for path, _ in docs:
        text = book.read(path)
        if text is None:
            print(f"warning: missing document {path}", file=sys.stderr)
            continue
        p = DocParser()
        try:
            p.feed(text)
            p.close()
        except Exception as exc:                      # malformed markup
            print(f"warning: could not parse {path}: {exc}", file=sys.stderr)
            continue
        parsed[path] = p

    def cut_points(path):
        """Resolved (part_index, label) split points inside one document."""
        p = parsed[path]
        cuts = {}
        for frag, label in toc_by_doc.get(path, []):
            idx = 0 if not frag else p.anchors.get(frag)
            if idx is None:
                continue
            cuts.setdefault(idx, label)
        return sorted(cuts.items())

    if split == "auto":
        split = "toc" if any(len(cut_points(p)) > 1 for p in parsed) else "spine"

    chapters = []
    for path, _ in docs:
        p = parsed.get(path)
        if p is None:
            continue
        fallback = p.heading or (toc_by_doc.get(path, [("", "")])[0][1]) or p.title or ""
        cuts = cut_points(path) if split == "toc" else []

        if not cuts:
            segments = [(0, len(p.parts), fallback)]
        else:
            if cuts[0][0] > 0:
                cuts.insert(0, (0, fallback))
            bounds = [c[0] for c in cuts] + [len(p.parts)]
            segments = [(bounds[i], bounds[i + 1], cuts[i][1]) for i in range(len(cuts))]

        for start, end, label in segments:
            body = normalize("".join(p.parts[start:end]), wrap)
            if len(body) < min_chars:
                continue
            title = label or body.split("\n", 1)[0][:60]
            chapters.append((title, body, path))
    return chapters, split


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="Split an EPUB into one plain-text file per chapter.")
    ap.add_argument("epub", type=Path, help="path to the .epub file")
    ap.add_argument("-o", "--outdir", type=Path,
                    help="output directory (default: <bookname>_chapters)")
    ap.add_argument("--split", choices=("auto", "spine", "toc"), default="auto",
                    help="auto: use the table of contents only when a single "
                         "document holds several chapters (default)")
    ap.add_argument("--min-chars", type=int, default=1, metavar="N",
                    help="skip sections shorter than N characters (default: 1)")
    ap.add_argument("--wrap", type=int, default=0, metavar="COLS",
                    help="hard-wrap paragraphs at COLS columns (default: none)")
    ap.add_argument("--cover", action="store_true",
                    help="also extract the cover image as cover.<ext>")
    ap.add_argument("--cover-only", action="store_true",
                    help="extract only the cover image, no text files")
    ap.add_argument("--index", action="store_true",
                    help="also write index.txt listing files and titles")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if not args.epub.is_file():
        raise SystemExit(f"No such file: {args.epub}")

    try:
        book = Epub(args.epub)
    except zipfile.BadZipFile:
        raise SystemExit(f"{args.epub} is not a zip archive - is it really an EPUB?")

    outdir = args.outdir or Path(f"{args.epub.stem}_chapters")
    outdir.mkdir(parents=True, exist_ok=True)

    if args.cover or args.cover_only:
        src = book.cover_image()
        if src is None:
            print("warning: no cover image found in this EPUB", file=sys.stderr)
        else:
            item = next((i for i in book.manifest.values()
                         if book.resolve(i["href"]) == src), None)
            ext = posixpath.splitext(src)[1].lower()
            if ext not in MIME_EXT.values():
                ext = MIME_EXT.get(item["type"] if item else "", ".img")
            dest = outdir / f"cover{ext}"
            dest.write_bytes(book.raw(src))
            if not args.quiet:
                print(f"{dest.name}\t{dest.stat().st_size:>7} bytes\t(from {src})")
        if args.cover_only:
            return

    chapters, mode = build_chapters(book, args.split, args.min_chars, args.wrap)
    if not chapters:
        raise SystemExit("No text content found in this EPUB.")

    lines = []
    for n, (title, body, src) in enumerate(chapters, 1):
        name = f"ch_{n:04d}.txt"
        (outdir / name).write_text(body + "\n", encoding="utf-8")
        lines.append(f"{name}\t{len(body):>7} chars\t{title}")

    if args.index:
        header = f"{book.title or args.epub.stem}\n{'=' * 60}\n"
        (outdir / "index.txt").write_text(header + "\n".join(lines) + "\n",
                                          encoding="utf-8")
    if not args.quiet:
        print("\n".join(lines))
        print(f"\n{len(chapters)} files written to {outdir}/ (split mode: {mode})")


if __name__ == "__main__":
    main()
