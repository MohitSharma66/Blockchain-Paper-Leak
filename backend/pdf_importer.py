"""Hybrid exam-PDF importer.

* Reads the PDF's own text layer when a page has one (fast, exact).
* Falls back to Tesseract OCR for a page that has (almost) no text layer, e.g. a scanned paper.
* Both sources are turned into the same positioned words, so the parsing below is identical.
* The caller picks two DIFFERENT identifiers: one for questions ("1.", "1)", "Q1") and one for
  options ("(1)", "1)", "(A)", "A.").
* Figures are cropped from the page (without the diagonal watermark) and attached to their question as
  images, so they can be printed. Questions whose text cannot be read reliably (broken formulas, missing
  options) also get a picture of the whole question, used for review and, if needed, printed as an image.
* The question / option identifiers can be "auto": they are detected from the document.

Output is always an unverified draft.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import sys
from difflib import SequenceMatcher

import pdfplumber
from PIL import Image

logging.getLogger("pdfminer").setLevel(logging.ERROR)

QUESTION_PATTERNS = {
    "1)": r"^(\d{1,3})\)\s*",
    "1.": r"^(\d{1,3})\.\s*",
    "Q1": r"^Q(?:uestion)?\s*(\d{1,3})\s*[:.)-]?\s*",
    "Question 1": r"^Question\s+(\d{1,3})\s*[:.)-]?\s*",
}
OPTION_PATTERNS = {
    "(1)": r"^\(([1-6])\)\s*",
    "1)": r"^([1-6])\)\s*",
    "1.": r"^([1-6])\.\s*",
    "(A)": r"^\(([A-F])\)\s*",
    "A.": r"^([A-F])\.\s*",
}
# the same markers, but matching one whole word (used to split side-by-side options that sit close together)
_OPTION_TOKENS = {
    "(1)": r"^\(([1-6])\)$",
    "1)": r"^([1-6])\)$",
    "1.": r"^([1-6])\.$",
    "(A)": r"^\(([A-F])\)$",
    "A.": r"^([A-F])\.$",
}

_CELL_GAP = 18.0          # points of empty space that separate two cells (e.g. option columns) on one line
_MIN_TEXT_WORDS = 15      # fewer upright words than this on a page -> treat the page as scanned
_RENDER_DPI = 200      # crops are rendered at this resolution (print quality)
_REVIEW_SCALE = 0.8    # pictures of whole questions are shrunk a little to keep the JSON small (still printable)
_HEADING = re.compile(r"^(?P<name>[A-Za-z][A-Za-z &/]{2,30}?)\s*[·•|.\-–—:]\s*\d+\s*Qs?\.?$")
_KEY_HEAD = re.compile(r"^\s*(?:answer\s*key|answers?|key)\s*:?\s*$", re.I)
_WATERMARK = re.compile(r"medic\s*neet", re.I)
_MATH_FONT = re.compile(r"katex|cmmi|cmsy|cmex|cmr\d|stix|mathjax|mathematica|symbolmt", re.I)
_FIGURE_WORDS = re.compile(r"\b(figure|diagram|pedigree|graph|image below|shown below|as shown|given circuit|given figure)\b", re.I)
_ARTIFACT = re.compile(r"(?<=[A-Za-z)\]])X[0-9+\u2212-]|\\ce\{|\\emph\{")
_PICTURE_OPTION = re.compile(r"\(?see (?:the )?(?:figure|image|diagram|picture)\)?", re.I)
# Signs that the text layer lost part of a formula (placeholder letters, detached sub/superscripts, lost
# exponents). Text like this cannot be repaired reliably, so the question is printed as a picture instead.
_DAMAGED = [
    ("a placeholder letter where a subscript/superscript was", re.compile(r"(?<=[A-Za-z)\]₀-₉⁰-⁹])X(?=[\]\)+\u22120-9\-\s]|$)")),
    ("detached subscript digits after a bracket",              re.compile(r"[\]\)][²³⁺⁻]*\s\d(?:\s\d)?\b")),
    ("a fraction or exponent split into loose characters",     re.compile(r"(?:\s[\d+\u2212-]){2,}$")),
    ("an option made only of loose digits",                    re.compile(r"^\d+(?:\s\d+)+$")),
    ("a lost superscript in an electron configuration",        re.compile(r"\b\d[spdf]\d{1,2}\b")),
    ("a lost exponent after a unit or power of ten",           re.compile(r"[A-Za-z0-9]\u2212\d")),
    ("repeated loose letters",                                 re.compile(r"\b(\w) \1 \1\b")),
]
_ARROW = "\ue000"   # private marker: a wide gap inside an option that is probably a (drawn) reaction arrow
_LIST_LINE = re.compile(r"^(?:[A-F]\.\s|(?:I|II|III|IV|V|VI)\.\s|List[\s-]|Statement\b|Assertion|Reason|Choose the)")

_SUB = str.maketrans("0123456789+-\u2212", "₀₁₂₃₄₅₆₇₈₉₊₋₋")
_SUP = str.maketrans("0123456789+-\u2212", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻")


# --------------------------------------------------------------------------- settings
class _Settings:
    def __init__(self, question_format, option_format, option_count):
        if question_format not in QUESTION_PATTERNS:
            raise ValueError(f"Unsupported question identifier. Choose one of: {', '.join(QUESTION_PATTERNS)}")
        if option_format not in OPTION_PATTERNS:
            raise ValueError(f"Unsupported option identifier. Choose one of: {', '.join(OPTION_PATTERNS)}")
        if question_format == option_format:
            raise ValueError("The question identifier and the option identifier must be different.")
        if option_count not in (2, 3, 4, 5, 6):
            raise ValueError("Options per question must be between 2 and 6.")
        self.question_format, self.option_format, self.n = question_format, option_format, option_count
        self.q_re = re.compile(QUESTION_PATTERNS[question_format], re.I)
        self.o_re = re.compile(OPTION_PATTERNS[option_format], re.I)
        self.o_tok = re.compile(_OPTION_TOKENS[option_format], re.I)


def _scale(page) -> float:
    """Thresholds are tuned for a 612 pt wide page; scanned PDFs can have any page size."""
    return page.width / 612.0


def _marker_index(token: str) -> int:
    return int(token) if token.isdigit() else ord(token.upper()) - 64


# --------------------------------------------------------------------------- text helpers
def _join(a: str, b: str) -> str:
    """Join two wrapped pieces; undo a hyphen break such as 'N-' + 'ethylethanamine'."""
    if not a:
        return b
    if a.endswith("-") and b[:1].islower():
        return a + b
    return a + " " + b


def _clean(s: str) -> str:
    s = _WATERMARK.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _fix_text(s: str) -> str:
    """Repair the artefacts a math-typeset PDF leaves in its text layer (NaX2SOX4 -> Na₂SO₄, \\ce{..})."""
    s = re.sub(r"\\ce\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\emph\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\^\{?(\d*[+\u2212-]|\d+)\}?", lambda m: m.group(1).translate(_SUP), s)   # Li^2+ -> Li²⁺
    s = re.sub(r"(?<=[A-Za-z)\]])X(\d*[+\u2212-])(?![\w(])", lambda m: m.group(1).translate(_SUP), s)
    s = re.sub(r"(?<=[A-Za-z)\]])X\s?(\d+)", lambda m: m.group(1).translate(_SUB), s)
    s = re.sub(r"(?<=[₀-₉⁰-⁹⁺⁻])\s(?=[A-Z])", "", s)  # 'Na₂ S' -> 'Na₂S'
    return s


_MATCH_Q = re.compile(r"\bList[\s-]?I\b.*\bList[\s-]?II\b", re.S)


def _break_lists(stem: str):
    """Some papers print 'Match List-I with List-II' as one run-on paragraph. Put each lettered item
    (and the closing instruction) on its own line. Returns (text, changed)."""
    if not _MATCH_Q.search(stem) or stem.count("\n") >= 3:
        return stem, False
    out = re.sub(r"[ ]+(?=[A-F]\.\s)", "\n", stem)
    out = re.sub(r"[ ]+(?=Choose the correct)", "\n", out)
    return out, out != stem


def _damage(stem: str, options) -> list:
    """Names of the damage signs found in a question's text (empty list: the text looks intact)."""
    found = []
    for name, rx in _DAMAGED:
        if rx.search(stem) or any(rx.search(o) for o in options):
            found.append(name)
    return found


def _is_heading(text: str) -> bool:
    return bool(_HEADING.match(text) or _KEY_HEAD.match(text))


# --------------------------------------------------------------------------- words (text layer or OCR)
_PLACEHOLDER = re.compile(r"(?<=[A-Za-z)\]])X")
_SCRIPT_WORD = re.compile(r"\d+[+−-]?|[+−-]")


def _attach_scripts(words):
    """Put sub/superscripts back into formulas.

    Math-typeset PDFs write 'Na2SO4' as the base word 'NaXSOX' (one placeholder X per script) plus
    separate small words '2' and '4' that sit inside the base word's box. When the number of small words
    equals the number of placeholders they are substituted in order (lower -> subscript, raised -> superscript).
    """
    used, out = set(), []
    for w in words:
        if id(w) in used:
            continue
        k = len(_PLACEHOLDER.findall(w["text"]))
        if k:
            h, cy = w["bottom"] - w["top"], (w["top"] + w["bottom"]) / 2
            cands = [s for s in words if s is not w and id(s) not in used and _SCRIPT_WORD.fullmatch(s["text"])
                     and w["x0"] - 1 <= s["x0"] and s["x1"] <= w["x1"] + 1
                     and abs((s["top"] + s["bottom"]) / 2 - cy) <= 8
                     and (s["bottom"] - s["top"]) < 0.85 * h]
            if len(cands) == k:
                cands.sort(key=lambda s: s["x0"])
                pieces = iter(cands)

                def sub(_m):
                    s = next(pieces)
                    lower = (s["top"] + s["bottom"]) / 2 > cy + 1
                    plain = s["text"]
                    return plain.translate(_SUB if lower and plain[-1:].isdigit() else _SUP)

                w = dict(w, text=_PLACEHOLDER.sub(sub, w["text"]))
                used.update(id(s) for s in cands)
        out.append(w)
    return [w for w in out if id(w) not in used]


def _text_words(page):
    """Upright words from the text layer. Rotated characters (diagonal watermarks) are dropped."""
    upright = page.filter(lambda o: o.get("object_type") != "char"
                          or (abs(o["matrix"][1]) < 0.01 and abs(o["matrix"][2]) < 0.01))
    return _attach_scripts(upright.extract_words(x_tolerance=1.5, y_tolerance=3))


_OCR_DPI = 300      # higher than 200 reads small exam print noticeably better
_OCR_PSM = 3        # Tesseract page-segmentation mode (default auto layout); words are regrouped by coordinates afterwards
_OCR_WHITEN = 190   # pixels brighter than this become white: removes faint watermarks / paper tint
_OCR_MIN_CONF = 30


_OCR_MARKER = re.compile(r"^[(\[{]\s*([1-6l|])[)\]}jJ](.*)$")


def _repair_ocr_markers(words, S):
    """Tesseract often reads '(1)' as '(1j', '[1)' or '(l)', and glues the next word to it. Only the '(1)' style."""
    if S.option_format != "(1)":
        return words
    out = []
    for w in words:
        m = _OCR_MARKER.match(w["text"])
        if not m:
            out.append(w)
            continue
        digit = m.group(1) if m.group(1).isdigit() else "1"
        rest = m.group(2).strip()
        cut = w["x0"] + (w["x1"] - w["x0"]) * (3.0 / max(len(w["text"]), 3))
        out.append(dict(w, text=f"({digit})", x1=min(w["x1"], cut)))
        if rest:
            out.append(dict(w, text=rest, x0=min(w["x1"], cut)))
    return out


def _ocr_words(page, dpi=None, psm=None, whiten=None):
    import pytesseract
    from pytesseract import Output
    dpi, psm = dpi or _OCR_DPI, psm or _OCR_PSM
    whiten = _OCR_WHITEN if whiten is None else whiten
    img = page.to_image(resolution=dpi).original.convert("L")
    if whiten:
        img = img.point(lambda p: 255 if p > whiten else p)
    d = pytesseract.image_to_data(img, config=f"--psm {psm}", output_type=Output.DICT)
    k = 72.0 / dpi
    words = []
    for i, raw in enumerate(d["text"]):
        t = raw.strip()
        if not t or float(d["conf"][i]) < _OCR_MIN_CONF:
            continue
        x0, top = d["left"][i] * k, d["top"][i] * k
        words.append({"text": t, "x0": x0, "x1": x0 + d["width"][i] * k,
                      "top": top, "bottom": top + d["height"][i] * k})
    return words


def _cell_lines(words, S, y_tol, gap=_CELL_GAP):
    """Words -> lines (top to bottom); each line is a list of cells (left to right).

    A new cell starts at a wide horizontal gap, or at an option marker that continues the sequence
    (so '(3) long text (4) text' is split even when the gap is small).
    """
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1]["top"] - w["top"]) <= y_tol:
            lines[-1]["words"].append(w)
        else:
            lines.append({"top": w["top"], "words": [w]})
    out = []
    for ln in lines:
        ws = sorted(ln["words"], key=lambda w: w["x0"])
        cells, last_marker = [], None
        for prev, w in zip([None] + ws[:-1], ws):
            m = S.o_tok.match(w["text"])
            marker = _marker_index(m.group(1)) if m else None
            new = (prev is None or w["x0"] - prev["x1"] > gap
                   or (marker is not None and last_marker is not None and marker == last_marker + 1))
            if new:
                cells.append({"x0": w["x0"], "x1": w["x1"], "parts": [w["text"]]})
            else:
                cells[-1]["parts"].append(w["text"])
                cells[-1]["x1"] = w["x1"]
            if marker is not None:
                last_marker = marker
        cells = [{"x0": c["x0"], "x1": c["x1"], "text": " ".join(c["parts"])} for c in cells]
        if cells and S.o_re.match(cells[0]["text"]):
            # a gap *inside* an option (e.g. around a reaction arrow) must not start a new cell:
            # only option markers start cells on a line that begins with an option marker
            merged = []
            for c in cells:
                if merged and not S.o_re.match(c["text"]):
                    sep = f" {_ARROW} " if c["x0"] - merged[-1]["x1"] > gap else " "
                    merged[-1]["text"] += sep + c["text"]
                    merged[-1]["x1"] = max(merged[-1]["x1"], c["x1"])
                else:
                    merged.append(dict(c))
            cells = merged
        out.append({"top": min(w["top"] for w in ws), "bottom": max(w["bottom"] for w in ws), "cells": cells})
    return out


def _linearize(lines, S, page_no, scale=1.0):
    """Cell lines -> plain rows.

    Options laid out as a grid   (1) text      (2) text
                                 (3) text      (4) text
    wrap *under their own column*, so reading straight across would glue a continuation onto the wrong
    option. A row whose first two cells both start with option markers is therefore split into one row
    per cell, and each following unmarked line is attached to the cell it lines up with (by x position).
    """
    rows, i = [], 0
    while i < len(lines):
        ln = lines[i]
        cells = ln["cells"]
        if len(cells) >= 2 and S.o_re.match(cells[0]["text"]) and S.o_re.match(cells[1]["text"]):
            cols = [dict(c, top=ln["top"], bottom=ln["bottom"]) for c in cells]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]["cells"]
                if any(S.o_re.match(c["text"]) or S.q_re.match(c["text"]) or _is_heading(c["text"]) for c in nxt):
                    break
                targets = [min(range(len(cols)), key=lambda k: abs(cols[k]["x0"] - c["x0"])) for c in nxt]
                if (any(abs(cols[t]["x0"] - c["x0"]) > 15 * scale for t, c in zip(targets, nxt))
                        or len(set(targets)) < len(targets)):
                    break
                for t, c in zip(targets, nxt):
                    cols[t]["text"] = _join(cols[t]["text"], c["text"])
                    cols[t]["x1"] = max(cols[t]["x1"], c["x1"])
                    cols[t]["bottom"] = lines[j]["bottom"]
                j += 1
            for c in cols:
                rows.append({"page": page_no, "top": c["top"], "bottom": c["bottom"],
                             "x0": c["x0"], "x1": c["x1"], "text": c["text"]})
            i = j
        else:
            text = cells[0]["text"]
            for c in cells[1:]:
                text += " " + c["text"]
            rows.append({"page": page_no, "top": ln["top"], "bottom": ln["bottom"],
                         "x0": cells[0]["x0"], "x1": cells[-1]["x1"], "text": text})
            i += 1
    return rows


# --------------------------------------------------------------------------- page images
class _Renderer:
    """Renders each page once and crops from it. Diagonal watermark text is removed before rendering
    (pdfium); if that is not possible the page is rendered as it is. Keeps only the last two pages."""

    def __init__(self, data, pdf):
        self.pdf, self.cache, self.dpi = pdf, {}, {}
        self.unstripped = 0   # pages rendered with the watermark still on them
        self.doc = None
        try:
            import pypdfium2 as pdfium
            self.doc = pdfium.PdfDocument(data)
        except Exception:
            self.doc = None

    def close(self):
        try:
            if self.doc is not None:
                self.doc.close()
        except Exception:
            pass

    @staticmethod
    def _strip_watermark(page):
        """Delete diagonal text (the watermark), also when it is nested inside form objects."""
        import pypdfium2.raw as raw
        doomed = []
        for obj in page.get_objects():
            if obj.type == raw.FPDF_PAGEOBJ_TEXT:
                m = obj.get_matrix()
                if abs(m.a) > 0.01 and abs(m.b) > 0.01:   # neither upright nor turned by 90 degrees
                    doomed.append(obj)
        obj = None
        for d in doomed:
            page.remove_obj(d)
            d.close()          # a removed object is no longer owned by the page, so it is closed here
        if doomed:
            page.gen_content()

    def _render(self, page_no):
        page = self.pdf.pages[page_no - 1]
        dpi = self.dpi[page_no] = _RENDER_DPI / _scale(page)  # same pixel size whatever the page's point size
        if self.doc is not None:
            p = None
            try:
                p = self.doc[page_no - 1]
                self._strip_watermark(p)
                bitmap = p.render(scale=dpi / 72.0)
                try:
                    return bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            except Exception:
                pass
            finally:
                if p is not None:
                    try:
                        p.close()   # children are closed before the document (avoids exit-time errors)
                    except Exception:
                        pass
        self.unstripped += 1
        return page.to_image(resolution=dpi).original.convert("RGB")

    def crop(self, page_no, x0, top, x1, bottom):
        if page_no not in self.cache:
            for k in [k for k in self.cache if k < page_no - 1]:
                del self.cache[k]
            self.cache[page_no] = self._render(page_no)
        k = self.dpi[page_no] / 72.0
        img = self.cache[page_no]
        box = (max(0, int(x0 * k)), max(0, int(top * k)), min(img.width, int(x1 * k)), min(img.height, int(bottom * k)))
        return img.crop(box)


def _stitch(parts):
    if len(parts) == 1:
        return parts[0]
    w = max(p.width for p in parts)
    out = Image.new("RGB", (w, sum(p.height for p in parts)), "white")
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height
    return out


def _data_uri(img, review=False):
    """PNG for figures/diagrams; JPEG when that is clearly smaller (photos) or for whole-question pictures.
    The resolution is stored in the file so the printed size is right."""
    img = img.convert("RGB")
    dpi = float(_RENDER_DPI)
    if review:
        img = img.resize((max(1, int(img.width * _REVIEW_SCALE)), max(1, int(img.height * _REVIEW_SCALE))), Image.LANCZOS)
        dpi *= _REVIEW_SCALE
    png = io.BytesIO()
    img.save(png, "PNG", optimize=True, dpi=(dpi, dpi))
    best, mime = png.getvalue(), "image/png"
    if review or len(best) > 150_000:
        jpg = io.BytesIO()
        img.save(jpg, "JPEG", quality=80 if review else 88, optimize=True, dpi=(dpi, dpi))
        if review or len(jpg.getvalue()) < 0.6 * len(best):
            best, mime = jpg.getvalue(), "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(best).decode()


def _figure_objects(page, top, limit, ocr_page, vector=True):
    """Images / drawn figures that start inside [top, limit) on this page. Separator rules are ignored."""
    page_area = page.width * page.height
    found = []
    if not ocr_page:  # on a scanned page the whole page is one big image
        for im in page.images:
            if im["top"] >= top - 2 and im["top"] < limit and (im["x1"] - im["x0"]) * (im["bottom"] - im["top"]) < 0.6 * page_area:
                found.append(im)
    if not found and not ocr_page and vector:
        vec = []
        for o in list(page.curves) + list(page.lines) + list(page.rects):
            w, h = o["x1"] - o["x0"], o["bottom"] - o["top"]
            if w > 0.6 * page.width and h < 3:
                continue  # horizontal separator rule
            if o["top"] >= top - 2 and o["top"] < limit and h < 0.5 * page.height:
                vec.append(o)
        if len(vec) >= 3:
            # fraction bars and underlines are drawn as thin lines too; a real drawing covers real area
            width = max(o["x1"] for o in vec) - min(o["x0"] for o in vec)
            height = max(o["bottom"] for o in vec) - min(o["top"] for o in vec)
            if width >= 60 * (page.width / 612.0) and height >= 50 * (page.width / 612.0):
                found = vec
    return found


# --------------------------------------------------------------------------- answer key
def _parse_key_text(text: str) -> dict:
    mapping = {c: i for i, c in enumerate("ABCDEF")} | {str(i + 1): i for i in range(6)}
    pat = re.compile(r"(?<![\w.])(?:Q(?:uestion)?\s*)?(\d{1,3})\s*[.):,;\-\t]\s*\(?\s*([A-Fa-f1-6])\s*\)?(?![\w])")
    found = {int(n): mapping[a.upper()] for n, a in pat.findall(text)}
    if len(found) < 5:  # bare "1 A" style, one pair per line
        for line in text.splitlines():
            m = re.match(r"^\s*(?:Q(?:uestion)?\s*)?(\d{1,3})\s+\(?\s*([A-Fa-f1-6])\s*\)?\s*$", line)
            if m:
                found[int(m.group(1))] = mapping[m.group(2).upper()]
    return found


def _apply_key(questions, found, option_count):
    applied, missing = 0, []
    for q in questions:
        if q["id"] in found and found[q["id"]] < (len(q.get("options") or []) or option_count):
            q["correct_index"] = found[q["id"]]
            applied += 1
        else:
            missing.append(q["id"])
    return {"applied": applied, "unmatched_question_ids": missing, "key_entries": len(found)}


def parse_answer_key(data: bytes, filename: str, questions: list[dict], option_count: int = 4) -> dict:
    """Read a text/CSV/PDF answer key and apply explicit question-number -> answer pairs only."""
    try:
        if filename.lower().endswith(".pdf"):
            parts = []
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages:
                    words = _text_words(page)
                    ocr_page = len(words) < _MIN_TEXT_WORDS
                    if ocr_page:
                        words = _ocr_words(page)
                    S = _Settings("Q1", "(A)", 4)  # settings are only used for line grouping here
                    sc = _scale(page)
                    for ln in _cell_lines(words, S, (4.0 if ocr_page else 3.0) * sc, _CELL_GAP * sc):
                        parts.append("  ".join(c["text"] for c in ln["cells"]))
            text = "\n".join(parts)
        else:
            text = data.decode("utf-8-sig", errors="replace")
    except Exception as exc:
        raise ValueError("Could not read the answer key.") from exc
    return _apply_key(questions, _parse_key_text(text), option_count)


# --------------------------------------------------------------------------- identifier detection
def _chain(ids) -> int:
    """Length of the longest run of consecutive numbers in ids."""
    best = run = 0
    prev = None
    for i in sorted(ids):
        run = run + 1 if prev is not None and i == prev + 1 else 1
        best, prev = max(best, run), i
    return best


def _detect_formats(page_lines, question_format, option_format):
    """Pick the question / option identifiers from the document itself.

    page_lines: per page, a list of lines; each line is the list of its cell texts (left to right).
    Questions: the format whose numbers form the longest 1, 2, 3, ... run at line starts.
    Options:   the format with the most '1' and '2' markers (other than the question format).
    """
    if question_format == "auto":
        scores = {}
        for fmt, pat in QUESTION_PATTERNS.items():
            rx, ids = re.compile(pat, re.I), set()
            for lines in page_lines:
                for cells in lines:
                    m = rx.match(cells[0])
                    if m:
                        ids.add(int(m.group(1)))
            scores[fmt] = _chain(ids)
        order = list(QUESTION_PATTERNS)
        question_format = max(order, key=lambda f: (scores[f], order.index(f)))
        if scores[question_format] < 2:
            raise ValueError("Could not detect how the questions are numbered. Choose the question identifier yourself.")
    if option_format == "auto":
        scores = {}
        for fmt, pat in OPTION_PATTERNS.items():
            if fmt == question_format:
                continue
            rx, first, second = re.compile(pat, re.I), 0, 0
            for lines in page_lines:
                for cells in lines:
                    for text in cells:
                        m = rx.match(text)
                        if m:
                            idx = _marker_index(m.group(1))
                            first += idx == 1
                            second += idx == 2
            scores[fmt] = min(first, second)
        order = list(OPTION_PATTERNS)
        option_format = max(scores, key=lambda f: (scores[f], -order.index(f)))
        if scores[option_format] < 2:
            raise ValueError("Could not detect how the options are labelled. Choose the option identifier yourself.")
    return question_format, option_format


# --------------------------------------------------------------------------- main parser
def parse_exam_pdf(data: bytes, filename: str = "paper.pdf", question_format: str = "auto",
                   option_format: str = "auto", option_count: int = 4, question_boundary: str = "line",
                   images_dir: str | None = None, images: str = "flagged", ocr: str = "auto",
                   embed_images: bool = True) -> dict:
    """
    question_format / option_format: one of the identifier names, or "auto" to detect.
    images: "flagged" (default) attaches figures, plus a picture of every question that needs review;
            "all" also attaches a picture of every question, "none" attaches nothing.
    ocr:    "auto" OCRs only pages without a usable text layer, "force" OCRs every page, "off" never.
    embed_images: put the pictures into the JSON as base64 data URIs (nothing is written to disk).
    images_dir: optionally also save the pictures as files there (debugging).
    """
    if question_boundary != "line":
        raise ValueError("Only line-start question boundaries are supported.")
    if images not in ("flagged", "all", "none") or ocr not in ("auto", "force", "off"):
        raise ValueError("images must be flagged/all/none and ocr must be auto/force/off.")
    if option_count not in (2, 3, 4, 5, 6):
        raise ValueError("Options per question must be between 2 and 6.")
    for name, value, table in (("question", question_format, QUESTION_PATTERNS), ("option", option_format, OPTION_PATTERNS)):
        if value != "auto" and value not in table:
            raise ValueError(f"Unsupported {name} identifier. Choose one of: auto, {', '.join(table)}")
    if "auto" not in (question_format, option_format) and question_format == option_format:
        raise ValueError("The question identifier and the option identifier must be different.")
    notes: list[str] = []

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if not pdf.pages:
            raise ValueError("The PDF has no pages.")

        # ---- 1. every page -> words with coordinates (text layer, or OCR when there is none) ----------
        page_mode, page_words = {}, {}
        for page_no, page in enumerate(pdf.pages, 1):
            words = [] if ocr == "force" else _text_words(page)
            use_ocr = ocr == "force" or (ocr == "auto" and len(words) < _MIN_TEXT_WORDS)
            if use_ocr:
                try:
                    words = _ocr_words(page)
                except Exception:
                    notes.append(f"Page {page_no} has no text layer and OCR is not available (install Tesseract + pytesseract).")
                    use_ocr = False
            page_mode[page_no], page_words[page_no] = ("ocr" if use_ocr else "text"), words

        # ---- 2. identifiers (detected when asked) ------------------------------------------------------
        detected = "auto" in (question_format, option_format)
        if detected:
            neutral = _Settings("1.", "(1)", option_count)
            page_lines = []
            for page_no, words in page_words.items():
                sc = _scale(pdf.pages[page_no - 1])
                lines = _cell_lines(words, neutral, (5.0 if page_mode[page_no] == "ocr" else 4.5) * sc, _CELL_GAP * sc)
                page_lines.append([[c["text"] for c in ln["cells"]] for ln in lines if ln["cells"]])
            question_format, option_format = _detect_formats(page_lines, question_format, option_format)
            notes.append(f"Detected identifiers: questions '{question_format}', options '{option_format}'.")
        S = _Settings(question_format, option_format, option_count)

        # ---- 3. words -> rows of text with coordinates -------------------------------------------------
        page_rows = {}
        for page_no, words in page_words.items():
            page = pdf.pages[page_no - 1]
            if page_mode[page_no] == "ocr":
                words = _repair_ocr_markers(words, S)
            # 4.5 pt keeps subscripts/superscripts on their line; text lines are >= 11 pt apart
            sc = _scale(page)
            page_rows[page_no] = _linearize(
                _cell_lines(words, S, (5.0 if page_mode[page_no] == "ocr" else 4.5) * sc, _CELL_GAP * sc),
                S, page_no, sc)
        del page_words

        # ---- 4. where each page's question numbers sit (left gutter) ---------------------------------
        gutter = {}
        for page_no, rows in page_rows.items():
            xs = [r["x0"] for r in rows if S.q_re.match(r["text"])]
            if xs:
                gutter[page_no] = min(xs) + 0.03 * pdf.pages[page_no - 1].width

        # ---- 5. walk the rows in reading order and cut them into question blocks ---------------------
        blocks, key_lines, bounds = [], [], {}
        current, subject, prev_id, in_key = None, None, None, False
        skipped, skipped_samples = 0, []
        for page_no in sorted(page_rows):
            for r in page_rows[page_no]:
                text = r["text"]
                if in_key:
                    key_lines.append(text)
                    continue
                if _KEY_HEAD.match(text):
                    in_key, current = True, None
                    bounds.setdefault(page_no, []).append(r["top"])
                    continue
                hm = _HEADING.match(text)
                if hm:
                    subject, current = _clean(hm.group("name")), None
                    bounds.setdefault(page_no, []).append(r["top"])
                    continue
                qm = S.q_re.match(text)
                if qm and r["x0"] <= gutter.get(page_no, 1e9):
                    qid = int(qm.group(1))
                    # numbers must keep increasing (a section may restart at 1); gaps are reported later
                    if 1 <= qid <= 500 and (prev_id is None or qid > prev_id or qid == 1):
                        current = {"id": qid, "subject": subject, "rows": [dict(r, text=text[qm.end():])]}
                        blocks.append(current)
                        prev_id = qid
                        bounds.setdefault(page_no, []).append(r["top"])
                        continue
                    if qid >= 1:
                        skipped += 1
                        if len(skipped_samples) < 3:
                            skipped_samples.append(f"p{page_no}: {text[:40]!r}")
                if current is not None:
                    current["rows"].append(r)
        if not blocks:
            raise ValueError(f"No questions matched the identifier '{question_format}'. "
                             "Check the question identifier you selected.")
        if skipped:
            notes.append(f"{skipped} line(s) looked like question numbers but broke the numbering sequence "
                         f"and were kept as text: {', '.join(skipped_samples)}.")

        # ---- 6. per page left/right content edges (for even crops) -----------------------------------
        edges = {}
        for page_no, rows in page_rows.items():
            if rows:
                pad = 6 * _scale(pdf.pages[page_no - 1])
                edges[page_no] = (min(r["x0"] for r in rows) - pad, max(r["x1"] for r in rows) + pad)

        # ---- 7. build each question -----------------------------------------------------------------
        renderer = _Renderer(data, pdf) if images != "none" else None
        out_dir = None
        if images != "none" and images_dir:
            out_dir = images_dir
            os.makedirs(out_dir, exist_ok=True)

        def save_file(img, name):
            if out_dir:
                img.save(os.path.join(out_dir, name))

        questions, seen_ids = [], {}
        try:
            for b in blocks:
                warn, info = [], []
                opts, current_opt, stem_lines = [], None, []
                for r in b["rows"]:
                    om = S.o_re.match(r["text"])
                    if om and _marker_index(om.group(1)) == len(opts) + (1 if current_opt is None else 2) \
                            and _marker_index(om.group(1)) <= S.n:
                        if current_opt is not None:
                            opts.append(current_opt)
                        current_opt = r["text"][om.end():]
                    elif om and _marker_index(om.group(1)) == 1 and current_opt is not None:
                        warn.append("option numbering restarts inside this question; a question number may have been missed")
                        current_opt = _join(current_opt, r["text"])
                    elif current_opt is not None:
                        current_opt = _join(current_opt, r["text"])
                    else:
                        stem_lines.append(r["text"])
                if current_opt is not None:
                    opts.append(current_opt)

                stem = ""
                for line in stem_lines:
                    stem = (stem + ("\n" if _LIST_LINE.match(line) and stem else " ") + line) if stem else line
                stem = "\n".join(_clean(_fix_text(s)) for s in stem.split("\n")).strip()
                stem, listed = _break_lists(stem)
                if listed:
                    warn.append("the lists are run together in the source PDF; line breaks were added, check them against the image")
                options = [_clean(_fix_text(o)) for o in opts]
                if any(_ARROW in o for o in options):
                    options = [o.replace(_ARROW, "→") for o in options]
                    warn.append("a wide gap inside an option was read as a reaction arrow (→); check against the image")

                if len(options) != S.n:
                    warn.append(f"detected {len(options)} of {S.n} options")
                options = (options + [""] * S.n)[:S.n]
                if any(not o for o in options[:len(opts)]):
                    warn.append("one or more options came out empty")
                filled = [o for o in options if o]
                picture_opts = bool(filled) and all(_PICTURE_OPTION.fullmatch(o) or re.fullmatch(r"\(?[A-F]\)?", o) for o in filled)
                dup_opts = not picture_opts and len(set(filled)) < len(filled)
                if dup_opts:
                    warn.append("two or more options are identical")
                if not stem:
                    warn.append("empty question text")
                if len(stem) > 1800:
                    warn.append("unusually long question text; possible missed boundary")
                if b["id"] in seen_ids:
                    warn.append(f"question number {b['id']} appears more than once")
                seen_ids[b["id"]] = True
                if _ARTIFACT.search(stem + " " + " ".join(options)):
                    warn.append("formula artefacts remain in the text")
                damage = [] if picture_opts else _damage(stem, options)
                if damage:
                    warn.append("the formula text is damaged (" + "; ".join(damage) + ")")
                # text that cannot be trusted at all: the whole question is printed as a picture instead
                text_unusable = dup_opts or len(filled) < S.n or not stem

                # ---- regions on the page(s) this question covers, figures, math, crops ----------------
                by_page = {}
                for r in b["rows"]:
                    seg = by_page.setdefault(r["page"], {"top": r["top"], "bottom": r["bottom"]})
                    seg["top"], seg["bottom"] = min(seg["top"], r["top"]), max(seg["bottom"], r["bottom"])
                has_figure, has_math, used_ocr = False, False, False
                segments, fig_boxes = [], []
                for page_no, seg in sorted(by_page.items()):
                    page = pdf.pages[page_no - 1]
                    used_ocr = used_ocr or page_mode[page_no] == "ocr"
                    later = [t for t in bounds.get(page_no, []) if t > seg["top"] + 1]
                    limit = min(later) if later else page.height
                    seg_math = page_mode[page_no] == "text" and any(
                        _MATH_FONT.search(c["fontname"]) for c in page.chars
                        if c["top"] >= seg["top"] - 1 and c["bottom"] <= min(limit, seg["bottom"] + 2))
                    # drawn lines inside typeset math (fraction bars) are not figures
                    figs = _figure_objects(page, seg["top"], limit, page_mode[page_no] == "ocr", vector=not seg_math)
                    x0, x1 = edges.get(page_no, (0, page.width))
                    bottom = seg["bottom"]
                    if figs:
                        has_figure = True
                        fx0, fx1 = min(f["x0"] for f in figs), max(f["x1"] for f in figs)
                        ftop, fbottom = min(f["top"] for f in figs), max(f["bottom"] for f in figs)
                        fig_boxes.append((page_no, fx0 - 3, ftop - 3, fx1 + 3, fbottom + 3))
                        bottom = max(bottom, fbottom)
                        x0, x1 = min(x0, fx0), max(x1, fx1)
                    if page_mode[page_no] == "text" and any(
                            _MATH_FONT.search(c["fontname"]) for c in page.chars
                            if c["top"] >= seg["top"] - 1 and c["bottom"] <= bottom + 1):
                        has_math = True
                    segments.append((page_no, x0, seg["top"] - 3, x1, bottom + 3))

                if has_figure:
                    info.append("figure attached as image")
                elif _FIGURE_WORDS.search(stem) and not picture_opts and not used_ocr:
                    warn.append("question mentions a figure/diagram but none was found in the PDF")
                if has_math:
                    warn.append("contains typeset formulas; the text may be incomplete or reordered, compare with the image")
                if used_ocr:
                    warn.append("text read by OCR; verify against the image")

                # ---- pictures ----------------------------------------------------------------------------
                figure_images, question_image = [], None
                render_as_image = False
                if renderer is not None:
                    for k, box in enumerate(fig_boxes):
                        fimg = renderer.crop(*box)
                        if fimg.width > 4 and fimg.height > 4:
                            figure_images.append(_data_uri(fimg) if embed_images else "")
                            save_file(fimg, f"q{b['id']}_fig{k + 1}.png")
                    if images == "all" or warn or has_math or used_ocr:
                        parts = [renderer.crop(*sg) for sg in segments]
                        qimg = _stitch([p for p in parts if p.width > 2 and p.height > 2])
                        render_as_image = text_unusable or (bool(damage) and not used_ocr)
                        if embed_images:
                            question_image = _data_uri(qimg, review=not render_as_image)
                        save_file(qimg, f"q{b['id']}.png")
                        if not embed_images:
                            question_image = None
                            render_as_image = False
                figure_images = [f for f in figure_images if f]
                if render_as_image:
                    info.append("the text could not be read reliably, so this question will be printed as a picture of the original")
                fixed_options = picture_opts or render_as_image
                if picture_opts:
                    info.append("the options refer to the figure; their order is fixed (not shuffled)")

                item = {
                    "id": b["id"], "subject": b["subject"], "question_text": stem, "options": options,
                    "correct_index": None, "fixed_options": fixed_options, "render_as_image": render_as_image,
                    "images": figure_images, "source": "ocr" if used_ocr else "text",
                    "needs_review": bool(warn), "review_issues": warn, "info": info,
                }
                if question_image:
                    item["question_image"] = question_image
                questions.append(item)
        finally:
            if renderer is not None:
                if renderer.unstripped:
                    notes.append(f"The watermark could not be removed from the pictures of {renderer.unstripped} page(s).")
                renderer.close()

    # ---- 8. paper-level checks ----------------------------------------------------------------------
    ids = [q["id"] for q in questions]
    gaps = sorted(set(range(1, max(ids) + 1)) - set(ids))
    if gaps:
        notes.append(f"Question numbers missing from the extraction: {gaps[:30]}{' ...' if len(gaps) > 30 else ''}.")
    squash = lambda s: re.sub(r"\W+", " ", s.lower()).strip()
    stems = [squash(q["question_text"]) for q in questions]
    optkeys = [sorted(squash(o) for o in q["options"]) for q in questions]
    whole = [stems[k] + " | " + " | ".join(optkeys[k]) for k in range(len(questions))]
    dup_pairs = []
    wordsets = [set(w.split()) for w in whole]
    for i in range(len(questions)):
        for j in range(i):
            if not (stems[i] and stems[j]):
                continue
            inter = len(wordsets[i] & wordsets[j])
            if inter < 0.4 * min(len(wordsets[i]), len(wordsets[j])):
                continue   # cheap word-overlap prefilter; the slow comparison only runs on plausible pairs
            same_options = optkeys[i] == optkeys[j] and any(optkeys[i])
            sm = SequenceMatcher(None, whole[i], whole[j])
            if sm.quick_ratio() > 0.6 and (sm.ratio() > 0.85 or (
                    same_options and SequenceMatcher(None, stems[i], stems[j]).ratio() > 0.55)):
                questions[i]["info"].append(f"looks like a repeat of question {questions[j]['id']}")
                dup_pairs.append((questions[j]["id"], questions[i]["id"]))
                break
    if dup_pairs:
        notes.append("Questions that look like repeats of an earlier one: "
                     + ", ".join(f"Q{b}~Q{a}" for a, b in dup_pairs) + ". This can be in the source paper itself.")

    key_info = None
    if key_lines:
        found = _parse_key_text("\n".join(key_lines))
        if found:
            key_info = _apply_key(questions, found, option_count)
            key_info["source"] = "Answer Key section inside the PDF"
            notes.append(f"Answers were read from the Answer Key section of this PDF ({key_info['applied']} applied); spot-check a few.")
    if key_info is None:
        notes.append("No answer key found: correct answers are empty (null). Upload an answer key to fill them in.")

    n_image_q = sum(q["render_as_image"] for q in questions)
    if n_image_q:
        notes.append(f"{n_image_q} question(s) will be printed as a picture of the original because their text could not be read reliably "
                     "(render_as_image). To print one as text instead, correct its text and set render_as_image to false.")
    ocr_pages = [p for p, m in page_mode.items() if m == "ocr"]
    if ocr_pages:
        notes.append(f"Pages read by OCR (no text layer): {ocr_pages}. Verify these questions carefully.")
    notes.insert(0, "Draft only. Check every question, option, formula and figure before use.")

    return {
        "filename": filename,
        "pages": len(page_mode),
        "identifier_settings": {"question_format": question_format, "question_boundary": question_boundary,
                                "option_format": option_format, "option_count": option_count,
                                "auto_detected": detected},
        "extraction": {"text_pages": sum(m == "text" for m in page_mode.values()), "ocr_pages": len(ocr_pages)},
        "answer_key": key_info,
        "paper": {"exam_name": re.sub(r"\.pdf$", "", os.path.basename(filename), flags=re.I),
                  "option_style": option_format, "questions": questions},
        "review_notes": notes,
    }


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract questions, options and answers from an exam PDF into JSON.")
    ap.add_argument("pdf")
    ap.add_argument("--q-format", default="auto", choices=["auto"] + list(QUESTION_PATTERNS), help="question identifier")
    ap.add_argument("--o-format", default="auto", choices=["auto"] + list(OPTION_PATTERNS), help="option identifier")
    ap.add_argument("--options", type=int, default=4)
    ap.add_argument("--key", help="answer key file (txt/csv/pdf), if it is not inside the paper")
    ap.add_argument("--out", default="import_out", help="output folder")
    ap.add_argument("--images", default="flagged", choices=["flagged", "all", "none"])
    ap.add_argument("--ocr", default="auto", choices=["auto", "force", "off"])
    ap.add_argument("--save-images", action="store_true", help="also save the pictures as files in <out>/images")
    ap.add_argument("--no-embed", action="store_true", help="do not embed pictures in the JSON")
    a = ap.parse_args(argv)

    with open(a.pdf, "rb") as f:
        data = f.read()
    res = parse_exam_pdf(data, os.path.basename(a.pdf), a.q_format, a.o_format, a.options,
                         images_dir=os.path.join(a.out, "images") if a.save_images else None,
                         images=a.images, ocr=a.ocr, embed_images=not a.no_embed)
    if a.key:
        with open(a.key, "rb") as f:
            res["answer_key"] = parse_answer_key(f.read(), os.path.basename(a.key), res["paper"]["questions"], a.options)
            res["answer_key"]["source"] = os.path.basename(a.key)
    os.makedirs(a.out, exist_ok=True)
    out_path = os.path.join(a.out, "paper.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    qs = res["paper"]["questions"]
    print(f"{len(qs)} questions | {sum(all(q['options']) for q in qs)} complete | "
          f"{sum(q['correct_index'] is not None for q in qs)} with answers | "
          f"{sum(bool(q['images']) for q in qs)} with figures | {sum(q['render_as_image'] for q in qs)} printed as image | "
          f"{sum(q['needs_review'] for q in qs)} need review")
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
