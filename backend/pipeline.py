"""
pipeline.py

Shuffle -> render PDF -> encrypt -> store, once per variant, from one API call.

What gets persisted, and where:
    encrypted blob   -> blob store (Cloudflare R2, or local folder fallback)
    AES key          -> keys.db, encrypted under the master key
    answer key       -> keys.db, encrypted under the master key
    record           -> board.db (variant, fingerprint, blob hash, object key)

No plaintext PDF, variant JSON or raw key is ever written to disk.
"""

import base64
import functools
import io
import logging
import math
import os
import hashlib
import json
import secrets
import random
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

import db
import keystore
from storage import get_blob_store

log = logging.getLogger(__name__)

MAX_VARIANTS = 500
MIN_OPTIONS, MAX_OPTIONS = 2, 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_FIGURES_PER_QUESTION = 6
IMAGE_DPI = 200          # resolution the importer crops at; sets the natural printed size of an image

# How the options are labelled on the printed paper. The importer sets the style of the source paper.
DEFAULT_OPTION_STYLE = "(A)"
OPTION_STYLES = ("(A)", "A.", "(1)", "1)", "1.")


def option_label(index: int, style: str = DEFAULT_OPTION_STYLE) -> str:
    n = str(index + 1)
    a = chr(ord("A") + index)
    return {"(A)": f"({a})", "A.": f"{a}.", "(1)": f"({n})", "1)": f"{n})", "1.": f"{n}."}[style]


# Unicode font: the built-in Helvetica cannot print subscripts, Greek letters, minus signs or arrows.
FONT_DIR = Path(__file__).parent / "fonts"
BODY_FONT, BOLD_FONT = "Helvetica", "Helvetica-Bold"
_GLYPHS = None


def _font_folders() -> list[Path]:
    """The bundled fonts first, then DejaVu copies that are commonly installed on the machine."""
    folders = [FONT_DIR, Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu"),
               Path("/usr/share/fonts/TTF"), Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]
    try:
        import matplotlib   # ships DejaVu Sans
        folders.append(Path(matplotlib.get_data_path()) / "fonts" / "ttf")
    except Exception:
        pass
    return folders


FONT_PROBLEM = ""


def _register_fonts() -> None:
    global BODY_FONT, BOLD_FONT, _GLYPHS, FONT_PROBLEM
    for folder in _font_folders():
        try:
            regular = TTFont("DejaVuSans", str(folder / "DejaVuSans.ttf"))
            bold = TTFont("DejaVuSans-Bold", str(folder / "DejaVuSans-Bold.ttf"))
        except Exception:
            continue
        pdfmetrics.registerFont(regular)
        pdfmetrics.registerFont(bold)
        pdfmetrics.registerFontFamily("DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold")
        BODY_FONT, BOLD_FONT = "DejaVuSans", "DejaVuSans-Bold"
        _GLYPHS = regular.face.charToGlyph
        FONT_PROBLEM = ""
        if folder != FONT_DIR:
            log.warning("The bundled fonts in %s were not found; using DejaVu Sans from %s.", FONT_DIR, folder)
        return
    FONT_PROBLEM = (f"The paper font could not be loaded: DejaVuSans.ttf and DejaVuSans-Bold.ttf are missing from "
                    f"{FONT_DIR}. Copy the 'fonts' folder that comes with the backend into that place and restart "
                    f"the server.")
    log.warning("%s Falling back to Helvetica: subscripts, Greek letters and minus signs will not print.", FONT_PROBLEM)


_register_fonts()


def _unprintable(text: str) -> list[str]:
    """Characters the paper font has no glyph for (they would print as blanks or wrong symbols)."""
    if _GLYPHS is None:
        return sorted({c for c in text if ord(c) > 255})
    return sorted({c for c in text if c not in "\n\t\r " and ord(c) not in _GLYPHS})


@functools.lru_cache(maxsize=512)
def _decode_image(uri: str) -> bytes:
    """Data URI -> image bytes; raises ValueError if it is not a usable PNG/JPEG."""
    if not isinstance(uri, str) or not uri.startswith(("data:image/png;base64,", "data:image/jpeg;base64,")):
        raise ValueError("images must be PNG or JPEG data URIs (data:image/png;base64,...).")
    try:
        raw = base64.b64decode(uri.split(",", 1)[1], validate=True)
    except Exception:
        raise ValueError("an image is not valid base64.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"an image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    try:
        Image.open(io.BytesIO(raw)).verify()
    except Exception:
        raise ValueError("an image could not be read as PNG or JPEG.")
    return raw


@functools.lru_cache(maxsize=512)
def _image_dpi(uri: str) -> float:
    """Resolution stored in the picture (the importer writes it); otherwise IMAGE_DPI."""
    try:
        dpi = Image.open(io.BytesIO(_decode_image(uri))).info.get("dpi")
        if dpi and float(dpi[0]) >= 30:
            return float(dpi[0])
    except Exception:
        pass
    return float(IMAGE_DPI)


def _is_fixed(q: dict) -> bool:
    """Questions whose option order must not change (the options refer to a figure, or are part of a picture)."""
    return q.get("fixed_options") is True or q.get("render_as_image") is True


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

def validate_paper(paper) -> None:
    """Raise ValueError with a clear message if the uploaded JSON is unusable."""
    if not isinstance(paper, dict):
        raise ValueError("Paper JSON must be an object with 'exam_name' and 'questions'.")
    if not isinstance(paper.get("exam_name"), str) or not paper["exam_name"].strip():
        raise ValueError("Paper JSON needs a non-empty 'exam_name' string.")
    style = paper.get("option_style", DEFAULT_OPTION_STYLE)
    if style not in OPTION_STYLES:
        raise ValueError(f"'option_style' must be one of {', '.join(OPTION_STYLES)}.")

    questions = paper.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("Paper JSON needs a non-empty 'questions' list.")

    seen_ids = set()
    for i, q in enumerate(questions, start=1):
        where = f"Question #{i}"
        if not isinstance(q, dict):
            raise ValueError(f"{where} must be an object.")
        for field in ("id", "question_text", "options", "correct_index"):
            if field not in q:
                raise ValueError(f"{where} is missing '{field}'.")
        where = f"Question #{i} (id {q['id']})"
        if q["id"] in seen_ids:
            raise ValueError(f"{where} reuses an id.")
        seen_ids.add(q["id"])

        for flag in ("fixed_options", "render_as_image"):
            if flag in q and not isinstance(q[flag], bool):
                raise ValueError(f"{where}: '{flag}' must be true or false.")
        as_image = q.get("render_as_image") is True
        fixed = _is_fixed(q)

        opts = q["options"]
        if not isinstance(opts, list) or not MIN_OPTIONS <= len(opts) <= MAX_OPTIONS:
            raise ValueError(f"{where} must have between {MIN_OPTIONS} and {MAX_OPTIONS} options.")

        figures = q.get("images", [])
        if not isinstance(figures, list) or len(figures) > MAX_FIGURES_PER_QUESTION:
            raise ValueError(f"{where}: 'images' must be a list of at most {MAX_FIGURES_PER_QUESTION} pictures.")
        try:
            for uri in figures:
                _decode_image(uri)
            if as_image:
                if not q.get("question_image"):
                    raise ValueError("it is marked render_as_image but has no 'question_image'.")
                _decode_image(q["question_image"])
        except ValueError as e:
            raise ValueError(f"{where}: {e}")

        if not as_image:
            if not isinstance(q["question_text"], str) or not q["question_text"].strip():
                raise ValueError(f"{where} has empty question_text.")
            if not all(isinstance(o, str) and o.strip() for o in opts):
                raise ValueError(f"{where} has an empty or non-text option. Fix the text, or set "
                                 f"render_as_image to true if the original has a 'question_image'.")
            bad = _unprintable(q["question_text"] + "".join(opts))
            if bad and FONT_PROBLEM:
                # not a problem with the question: the font itself is missing
                raise ValueError(FONT_PROBLEM + f" (Question {q['id']} needs it for: {' '.join(repr(c) for c in bad)}.)")
            if bad:
                raise ValueError(f"{where} contains characters the paper font cannot print: "
                                 f"{' '.join(repr(c) for c in bad)}. Replace them, or set render_as_image to true.")
        if not fixed and len(set(opts)) != len(opts):
            raise ValueError(f"{where} has duplicate options (shuffling would be ambiguous).")

        ci = q["correct_index"]
        if ci is None:
            raise ValueError(f"{where} has no correct answer yet (correct_index is empty). "
                             f"Upload the answer key or fill it in.")
        if not isinstance(ci, int) or isinstance(ci, bool) or not 0 <= ci < len(opts):
            raise ValueError(f"{where} has correct_index {ci!r}; it must be a whole number from 0 to {len(opts) - 1}.")


def variant_capacity(paper: dict) -> int:
    """How many different option orderings the paper can produce (each shuffled question multiplies it)."""
    cap = 1
    for q in paper["questions"]:
        if not _is_fixed(q):
            cap *= math.factorial(len(q["options"]))
    return cap


# ---------------------------------------------------------------------
# Shuffle + fingerprint
# ---------------------------------------------------------------------

def _shuffle_question(question: dict, rng: random.Random) -> dict:
    if _is_fixed(question):          # option order is part of the question: never shuffled
        return dict(question)
    order = list(range(len(question["options"])))
    rng.shuffle(order)
    return {**question,
            "options": [question["options"][i] for i in order],
            "correct_index": order.index(question["correct_index"])}


def _fingerprint(variant: dict) -> str:
    # Same scheme as generate_variants.py: hash of the option order across the paper.
    signature = "".join("".join(q["options"]) for q in variant["questions"])
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def _build_variant(paper: dict, variant_id: str, seed: int) -> dict:
    rng = random.Random(seed)
    return {
        "exam_name": paper["exam_name"],
        "option_style": paper.get("option_style", DEFAULT_OPTION_STYLE),
        "variant_id": variant_id,
        "questions": [_shuffle_question(q, rng) for q in paper["questions"]],
    }


# ---------------------------------------------------------------------
# PDF render + encrypt
# ---------------------------------------------------------------------

def _render_pdf_bytes(variant: dict) -> bytes:
    """
    Layout: exam title, then the questions in order. A question is
      * its text, then its figures (pictures), then its options, or
      * (render_as_image) one picture of the original question, which already contains the options.
    Text is set in DejaVu Sans so formulas (subscripts, Greek letters, minus signs, arrows) print correctly.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    margin = 20 * mm
    text_w = width - 2 * margin
    indent = 8 * mm
    style = variant.get("option_style", DEFAULT_OPTION_STYLE)
    state = {"y": height - margin}

    def ensure(h):
        if state["y"] - h < margin:
            c.showPage()
            state["y"] = height - margin

    def draw_text(text, font, size, x, max_w, leading):
        for line in simpleSplit(text, font, size, max_w):
            ensure(leading)
            c.setFont(font, size)
            c.drawString(x, state["y"], line)
            state["y"] -= leading

    def draw_image(uri, x, max_w, max_h):
        reader = ImageReader(io.BytesIO(_decode_image(uri)))
        iw, ih = reader.getSize()
        dpi = _image_dpi(uri)
        w, h = iw * 72.0 / dpi, ih * 72.0 / dpi                 # natural printed size
        scale = min(1.0, max_w / w, max_h / h)
        w, h = w * scale, h * scale
        ensure(h + 7 * mm)
        top = state["y"] + 2 * mm
        c.drawImage(reader, x, top - h, width=w, height=h)
        state["y"] = top - h - 5 * mm

    c.setFont(BOLD_FONT, 14)
    c.drawString(margin, state["y"], variant["exam_name"])
    state["y"] -= 10 * mm
    c.setFont(BODY_FONT, 9)
    c.drawString(margin, state["y"], f"Variant reference: {variant['variant_id']}")
    state["y"] -= 12 * mm

    last_subject = None
    for q in variant["questions"]:
        subject = q.get("subject")
        if subject and subject != last_subject:
            ensure(30 * mm)
            draw_text(str(subject), BOLD_FONT, 12, margin, text_w, 8 * mm)
            last_subject = subject
        ensure(28 * mm)   # do not strand a question's first line at the bottom of a page

        if q.get("render_as_image"):
            draw_image(q["question_image"], margin, text_w, 150 * mm)
            state["y"] -= 2 * mm
            continue

        draw_text(f"Q{q['id']}. {q['question_text']}", BOLD_FONT, 10.5, margin, text_w, 6 * mm)
        for uri in q.get("images", []):
            draw_image(uri, margin + indent, text_w - indent, 95 * mm)
        for i, text in enumerate(q["options"]):
            draw_text(f"{option_label(i, style)} {text}", BODY_FONT, 10, margin + indent, text_w - indent, 5.5 * mm)
        state["y"] -= 4 * mm

    c.save()
    return buf.getvalue()


def _online_payload(variant: dict) -> bytes:
    """
    The same paper as structured data, for the online exam server. It deliberately has NO answers.
    Each option carries `key` = its position in this variant; an answer sheet records that key, so
    it can be graded later against the board's answer key whatever order a candidate saw.
    """
    questions = []
    for q in variant["questions"]:
        item = {
            "id": q["id"],
            "subject": q.get("subject"),
            "fixed_options": _is_fixed(q),
        }
        if q.get("render_as_image"):
            item["question_image"] = q["question_image"]
            item["options"] = [{"key": i, "text": ""} for i in range(len(q["options"]))]
        else:
            item["question_text"] = q["question_text"]
            item["images"] = q.get("images", [])
            item["options"] = [{"key": i, "text": t} for i, t in enumerate(q["options"])]
        questions.append(item)
    payload = {
        "exam_name": variant["exam_name"],
        "option_style": variant.get("option_style", DEFAULT_OPTION_STYLE),
        "variant_id": variant["variant_id"],
        "questions": questions,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _encrypt(pdf_bytes: bytes, key: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, pdf_bytes, None)


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def _next_index() -> int:
    """Continue numbering after the highest existing center_NNN in the database."""
    highest = 0
    for center_id in db.list_all_centers():
        try:
            highest = max(highest, int(center_id.split("_")[-1]))
        except ValueError:
            pass
    return highest + 1


def generate_all(paper: dict, num_variants: int) -> list[dict]:
    """
    One variant per center (variant_N <-> center_NNN). Returns one summary
    dict per center. If anything fails part-way, the centers of this batch that
    were already created are rolled back, so no record ever points at a missing
    blob or key.
    """
    validate_paper(paper)
    if not isinstance(num_variants, int) or not 1 <= num_variants <= MAX_VARIANTS:
        raise ValueError(f"Number of variants must be between 1 and {MAX_VARIANTS}.")

    # Every variant must have its own option order; fixed questions cannot contribute to that.
    capacity = variant_capacity(paper)
    if num_variants > capacity:
        raise ValueError(
            f"This paper can only produce {capacity} distinct variant(s) (questions with a fixed option order "
            f"are not shuffled), but {num_variants} were requested."
        )

    # Fail fast on configuration problems, before creating anything.
    keystore.require_master_key()
    store = get_blob_store()
    store.check()

    start = _next_index()
    used_fingerprints = set()
    results = []
    created = []  # (center_id, variant_id, object_key) for rollback

    try:
        for offset in range(num_variants):
            n = start + offset
            variant_id = f"variant_{n}"
            center_id = f"center_{n:03d}"

            # Random (unpredictable) seed; retry until this ordering is unique in the batch.
            for _ in range(200):
                variant = _build_variant(paper, variant_id, secrets.randbits(64))
                fingerprint = _fingerprint(variant)
                if fingerprint not in used_fingerprints:
                    break
            else:
                raise ValueError("Could not generate enough distinct variants from this paper.")
            used_fingerprints.add(fingerprint)

            pdf_bytes = _render_pdf_bytes(variant)
            answers = [{"id": q["id"], "correct_index": q["correct_index"]} for q in variant["questions"]]

            key = AESGCM.generate_key(bit_length=256)
            blob = _encrypt(pdf_bytes, key)
            blob_hash = hashlib.sha256(blob).hexdigest()
            online_blob = _encrypt(_online_payload(variant), key)    # same key, fresh random nonce
            online_hash = hashlib.sha256(online_blob).hexdigest()
            online_key = f"online/{center_id}-{secrets.token_hex(4)}.enc"
            # Random suffix: a re-run never silently overwrites an older object in the bucket.
            object_key = f"blobs/{center_id}-{secrets.token_hex(4)}.enc"

            # Record goes in last, so it only exists once everything it points at does.
            store.put(object_key, blob)
            created.append((center_id, variant_id, object_key))
            store.put(online_key, online_blob)
            created.append((center_id, variant_id, online_key))
            keystore.store_key(center_id, key)
            keystore.store_answers(variant_id, answers)
            db.upsert_center_record(
                center_id=center_id,
                variant_id=variant_id,
                fingerprint=fingerprint,
                blob_hash=blob_hash,
                blob_object_key=object_key,
                online_blob_hash=online_hash,
                online_object_key=online_key,
            )

            results.append({
                "center_id": center_id,
                "variant_id": variant_id,
                "blob_hash": blob_hash,
                "fingerprint": fingerprint,
            })
    except Exception:
        for center_id, variant_id, object_key in created:
            # Each step on its own: a center appears twice in `created` (PDF blob + online blob).
            for step in (lambda: db.delete_center(center_id),
                         lambda: keystore.purge(center_id, variant_id),
                         lambda: store.delete(object_key)):
                try:
                    step()
                except Exception:
                    pass  # best effort; an orphaned object is harmless (it's ciphertext)
        raise

    return results
