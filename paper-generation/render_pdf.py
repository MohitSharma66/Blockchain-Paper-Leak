"""
render_pdf.py

Renders a variant JSON (question_text + options) into a print-ready PDF using
ONE shared template. Every variant uses the identical layout/numbering, so
printed papers look structurally identical across centers regardless of
which option-order variant they contain -- the watermark is invisible.

Usage:
    python render_pdf.py --variant variants/variant_1.json --out variants/variant_1.pdf
"""

import json
import argparse
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import simpleSplit

OPTION_LABELS = ["A", "B", "C", "D"]


def render_variant_to_pdf(variant: dict, output_path: Path):
    c = canvas.Canvas(str(output_path), pagesize=A4)
    width, height = A4
    margin = 20 * mm
    y = height - margin

    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y, variant["exam_name"])
    y -= 10 * mm

    c.setFont("Helvetica", 9)
    c.drawString(margin, y, f"Variant reference: {variant['variant_id']}")
    y -= 12 * mm

    c.setFont("Helvetica", 11)

    for q in variant["questions"]:
        # Wrap question text to fit page width
        lines = simpleSplit(f"Q{q['id']}. {q['question_text']}", "Helvetica-Bold", 11, width - 2 * margin)
        c.setFont("Helvetica-Bold", 11)
        for line in lines:
            if y < margin:
                c.showPage()
                y = height - margin
                c.setFont("Helvetica-Bold", 11)
            c.drawString(margin, y, line)
            y -= 6 * mm

        c.setFont("Helvetica", 10)
        for label, option_text in zip(OPTION_LABELS, q["options"]):
            option_lines = simpleSplit(f"({label}) {option_text}", "Helvetica", 10, width - 2 * margin - 8 * mm)
            for i, line in enumerate(option_lines):
                if y < margin:
                    c.showPage()
                    y = height - margin
                    c.setFont("Helvetica", 10)
                prefix = line if i > 0 else line
                c.drawString(margin + 8 * mm, y, prefix)
                y -= 5.5 * mm

        y -= 4 * mm  # spacing between questions

    c.save()


def main():
    parser = argparse.ArgumentParser(description="Render a variant JSON into a print-ready PDF.")
    parser.add_argument("--variant", required=True, help="Path to variant_N.json")
    parser.add_argument("--out", required=True, help="Output PDF path")
    args = parser.parse_args()

    with open(args.variant, "r", encoding="utf-8") as f:
        variant = json.load(f)

    render_variant_to_pdf(variant, Path(args.out))
    print(f"Rendered {args.variant} -> {args.out}")


if __name__ == "__main__":
    main()
