# epub_split

Split an EPUB into plain-text files, one per chapter (`ch_0001.txt`,
`ch_0002.txt`, ...). It can also pull out the cover image.

It uses only the Python standard library, so there is nothing to `pip install`.

## Requirements

- Python 3, or Nix with `nix-shell`.

The script has a `nix-shell` shebang, so on NixOS you can run it directly and
Nix will provide Python 3:

```sh
chmod +x epub_split.py
./epub_split.py book.epub
```

Without Nix, call Python yourself:

```sh
python3 epub_split.py book.epub
```

## Quick start

```sh
./epub_split.py book.epub
```

This creates `book_chapters/` next to where you run it and prints a summary:

```
ch_0001.txt	   1834 chars	Title Page
ch_0002.txt	  24410 chars	Chapter 1: The Beginning
...

42 files written to book_chapters/ (split mode: spine)
```

Each file holds the chapter's plain text. Whitespace is cleaned up and
paragraphs are separated by a single blank line. Image `alt` text appears
inline as `[alt text]`.

## Options

| Option | Default | What it does |
|---|---|---|
| `epub` | (required) | Path to the `.epub` file. |
| `-o`, `--outdir DIR` | `<bookname>_chapters` | Where to write the output. Created if missing. |
| `--split {auto,spine,toc}` | `auto` | How to decide where chapters begin. See below. |
| `--min-chars N` | `1` | Skip sections shorter than N characters. Use this to drop empty pages and short separator pages. |
| `--wrap COLS` | `0` (off) | Hard-wrap paragraphs at COLS columns. |
| `--cover` | off | Also save the cover image as `cover.<ext>`. |
| `--cover-only` | off | Save only the cover image and write no text files. |
| `--index` | off | Also write `index.txt` with the book title and a list of files and chapter titles. |
| `-q`, `--quiet` | off | Print nothing on success. Warnings still go to stderr. |

### Split modes

- **`spine`**: one output file per document in the book's reading order (the
  spine). This works well for most EPUBs, which store each chapter in its own
  XHTML file.
- **`toc`**: split at the table-of-contents entries (EPUB 3 nav or EPUB 2
  NCX). If one XHTML file holds several chapters, it is cut at each TOC anchor.
- **`auto`** (default): uses `toc` if any single document contains more than
  one TOC entry, and `spine` otherwise. The mode it picked is shown in the
  last line of output.

Chapter titles come from the TOC label, then the first heading in the
document, then the document's `<title>`, and finally the first line of text.

## Examples

```sh
# Write to a custom folder, wrap at 80 columns, and add an index
./epub_split.py book.epub -o out/ --wrap 80 --index

# Force splitting by table of contents and skip tiny sections
./epub_split.py book.epub --split toc --min-chars 200

# Save just the cover
./epub_split.py book.epub --cover-only -o covers/

# Process every EPUB in a folder without printing anything
for f in *.epub; do ./epub_split.py "$f" -q; done
```

## Cover detection

With `--cover` or `--cover-only`, the script tries these in order:

1. A manifest item with `properties="cover-image"` (EPUB 3).
2. `<meta name="cover" content="...">` in the metadata (EPUB 2). If that
   points at a page instead of an image, the first image on that page is used.
3. A cover page from the `<guide>`, or the first two spine documents.
4. Any manifest image whose path contains `cover`.

If none of these finds an image, it prints a warning and continues.

## Errors and warnings

- `Not a valid EPUB: ...`: the file has no `META-INF/container.xml` or no
  rootfile.
- `... is not a zip archive`: the file is not an EPUB (or it is damaged).
- `No text content found in this EPUB.`: nothing was left after parsing,
  possibly because `--min-chars` is set too high.
- `warning: missing document` / `could not parse`: one spine document was
  skipped, and the rest of the book is still written.

## Limitations

- DRM-protected EPUBs are not supported.
- The output is plain text only, so formatting, footnote links, and tables are
  flattened.
- Running it again into the same folder overwrites existing `ch_*.txt` files,
  but files from an earlier run that produced more chapters are left in place.
