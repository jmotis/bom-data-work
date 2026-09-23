"""Score a transcription run against hand-checked ground truth.

ground_truth/age_tables.csv lists, per image (by SHA-256), the correct value for each
field; "blank" means nothing printed. Run this whenever the prompt, model or effort
changes, and report the accuracy with the dataset.

Usage: python scripts/evaluate.py [--run a]
"""

import argparse

from build_csv import count
from common import cells_from_transcription, load_config, load_transcription, path, read_csv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    args = ap.parse_args()
    cfg = load_config()
    run = args.run or cfg["transcription"]["primary_run"]
    ids = {r["sha256"]: r["image_id"] for r in read_csv(path(cfg, "manifest"))}

    total = right = 0
    missing_images = set()
    for g in read_csv(path(cfg, "ground_truth")):
        image_id = ids.get(g["sha256"])
        rec = load_transcription(cfg, run, image_id) if image_id else None
        if rec is None:
            missing_images.add(g["sample_file"])
            continue
        cells = cells_from_transcription(rec["transcription"])[0]
        expected = 0 if g["value"] == "blank" else int(g["value"])
        got = count(cells[g["field"]]) if g["field"] in cells else None
        total += 1
        if got == expected:
            right += 1
        else:
            print(f"  {g['sample_file']:<12} {g['field']:<20} expected {expected!s:>5}  got {got!s:>5}")

    for f in sorted(missing_images):
        print(f"  no run-{run} transcription for ground-truth image {f}")
    if total:
        print(f"run {run}: {right}/{total} fields correct ({100 * right / total:.1f}%)")


if __name__ == "__main__":
    main()
