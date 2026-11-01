"""
generate_variants.py

Takes base_paper.json and produces N option-shuffled variants of the same
exam paper. Every variant has identical questions and identical *content* of
options, but each question's option ORDER is shuffled differently per variant.

This is the "watermark" — invisible to a student (they still see 4 sensible
options, just in a different order), but it lets you trace a leaked photo/copy
of the paper back to the specific variant (and therefore center) it came from.

Usage:
    python generate_variants.py --num-variants 4
"""

import json
import random
import argparse
import hashlib
from pathlib import Path

BASE_PAPER_PATH = Path(__file__).parent / "base_paper.json"
VARIANTS_DIR = Path(__file__).parent / "variants"


def load_base_paper(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def shuffle_question_options(question: dict, rng: random.Random) -> dict:
    """
    Shuffle a single question's options, keeping track of which new position
    holds the correct answer (since correct_index shifts along with the shuffle).
    """
    options = question["options"]
    correct_option_text = options[question["correct_index"]]

    # Create a shuffled copy of the options
    shuffled_options = options.copy()
    rng.shuffle(shuffled_options)

    # Find where the correct answer landed after shuffling
    new_correct_index = shuffled_options.index(correct_option_text)

    return {
        **question,
        "options": shuffled_options,
        "correct_index": new_correct_index,
    }


def generate_variant(base_paper: dict, variant_id: str, seed: int) -> dict:
    """
    Generate one variant of the paper using a deterministic seed.
    Deterministic seeding means variants are reproducible for debugging/grading,
    but each variant_id maps to a distinct, fixed shuffle pattern.
    """
    rng = random.Random(seed)

    shuffled_questions = [
        shuffle_question_options(q, rng) for q in base_paper["questions"]
    ]

    variant = {
        "exam_name": base_paper["exam_name"],
        "variant_id": variant_id,
        "questions": shuffled_questions,
    }
    return variant


def compute_variant_fingerprint(variant: dict) -> str:
    """
    Compute a fingerprint (hash) of just the option ORDERING across all
    questions. This is what you'd compare against scraped/leaked content
    to identify which variant (and thus which center) it came from —
    without needing to compare full question text.
    """
    ordering_signature = ""
    for q in variant["questions"]:
        ordering_signature += "".join(q["options"])

    return hashlib.sha256(ordering_signature.encode("utf-8")).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Generate option-shuffled paper variants.")
    parser.add_argument(
        "--num-variants", type=int, default=4,
        help="Number of distinct variants to generate (default: 4)"
    )
    args = parser.parse_args()

    VARIANTS_DIR.mkdir(exist_ok=True)

    base_paper = load_base_paper(BASE_PAPER_PATH)

    fingerprint_registry = {}

    for i in range(1, args.num_variants + 1):
        variant_id = f"variant_{i}"
        # Seed derived from variant index -> deterministic & reproducible
        seed = 1000 + i

        variant = generate_variant(base_paper, variant_id, seed)
        fingerprint = compute_variant_fingerprint(variant)
        variant["fingerprint"] = fingerprint

        output_path = VARIANTS_DIR / f"{variant_id}.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(variant, f, indent=2)

        fingerprint_registry[variant_id] = fingerprint

        print(f"Generated {variant_id} -> {output_path.name} (fingerprint: {fingerprint[:12]}...)")

    # Save a lookup table mapping fingerprints back to variant IDs.
    # This is what the leak-detection matching script will use to identify
    # which variant a scraped/leaked paper matches.
    registry_path = VARIANTS_DIR / "fingerprint_registry.json"
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(fingerprint_registry, f, indent=2)

    print(f"\nFingerprint registry written to {registry_path}")


if __name__ == "__main__":
    main()
