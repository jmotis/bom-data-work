"""Shared, deterministic helpers: config, age categories, label mapping, CSV I/O."""

import csv
import hashlib
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The twelve age brackets printed on every weekly bill, in print order.
STANDARD_AGES = [
    ("under_2", "under two"),
    ("2_to_5", "two and five"),
    ("5_to_10", "five and ten"),
    ("10_to_20", "ten and twenty"),
    ("20_to_30", "twenty and thirty"),
    ("30_to_40", "thirty and forty"),
    ("40_to_50", "forty and fifty"),
    ("50_to_60", "fifty and sixty"),
    ("60_to_70", "sixty and seventy"),
    ("70_to_80", "seventy and eighty"),
    ("80_to_90", "eighty and ninety"),
    ("90_to_100", "ninety and a hundred"),
]
STANDARD_KEYS = [key for key, _ in STANDARD_AGES]

# Totals transcribed alongside the age table so the ages can be checked against them.
TOTAL_FIELDS = [
    "christened_males", "christened_females", "christened_in_all",
    "buried_males", "buried_females", "buried_in_all",
    "burials_change",
]

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def load_config():
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def path(cfg, key):
    return ROOT / cfg["paths"][key]


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_label(label):
    """Lowercase, long s -> s, '&' -> 'and', keep only letters, digits and single spaces."""
    s = label.replace("ſ", "s").replace("&", " and ").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def age_key_for_label(label):
    """Map a printed age label to a canonical category key, or None if unrecognized."""
    s = normalize_label(label)
    s = re.sub(r"^(between|of age|age) ", "", s)
    s = re.sub(r" (years )?of age$", "", s)
    s = s.replace("years ", "")
    for key, phrase in STANDARD_AGES:
        if s == phrase or s == phrase.replace(" a hundred", " hundred"):
            return key
    m = re.fullmatch(r"a hundred(?: and (\w+))?", s)
    if m:
        extra = m.group(1)
        if extra is None:
            return "age_100"
        if extra.isdigit():
            return f"age_{100 + int(extra)}"
        if extra in NUMBER_WORDS:
            return f"age_{100 + NUMBER_WORDS[extra]}"
    return None


def age_sort_order(key):
    if key in STANDARD_KEYS:
        return STANDARD_KEYS.index(key)
    if key.startswith("age_"):
        return int(key[4:])
    return 999


def read_csv(p):
    if not Path(p).exists():
        return []
    with open(p, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(p, rows, fieldnames):
    """UTF-8 with BOM so Excel shows accented characters correctly."""
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_transcription(cfg, run, image_id):
    p = path(cfg, "transcriptions") / run / f"{image_id}.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def cells_from_transcription(t):
    """Flatten one transcription into {field: cell}; cell has value, as_printed, legibility, label.

    Returns (cells, unrecognized_labels, duplicate_labels).
    """
    cells, unrecognized, duplicates = {}, [], []
    for group in ("christened", "buried"):
        for part in ("males", "females", "in_all"):
            c = t[group][part]
            cells[f"{group}_{part}"] = {**c, "label": f"{group} {part}"}
    change = t["burials_change"]
    value = change["value"]
    if value is not None and change["direction"] == "decreased":
        value = -value
    cells["burials_change"] = {
        "value": value, "as_printed": change["as_printed"],
        "legibility": change["legibility"], "label": change["direction"],
    }
    for row in t["age_rows"]:
        key = age_key_for_label(row["printed_label"])
        cell = {k: row[k] for k in ("value", "as_printed", "legibility")}
        cell["label"] = row["printed_label"]
        if key is None:
            unrecognized.append(row["printed_label"])
        elif key not in cells or _is_blank(cells[key]):
            cells[key] = cell
        elif not _is_blank(cell):
            # Some bills print several bare "A Hundred" rows; blank repeats are harmless.
            duplicates.append(row["printed_label"])
    return cells, unrecognized, duplicates


def _is_blank(cell):
    return cell["value"] is None and cell["legibility"] == "blank"
