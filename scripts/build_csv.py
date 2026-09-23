"""Turn saved transcriptions + human corrections into CSVs, and check them.

No AI here: the same transcription files and corrections always produce the same
CSVs. Outputs (in data/output/):
    bills.csv         one row per bill: totals, every age category, check results
    ages_long.csv     one row per bill per age category (for analysis)
    review_queue.csv  one row per problem found, for the people checking the bills
"""

import json
import re
import subprocess
from datetime import date

from common import (
    STANDARD_KEYS, TOTAL_FIELDS, age_sort_order, cells_from_transcription, load_config,
    load_transcription, path, read_csv, write_csv,
)

CELL_FIELDS = ["value", "as_printed", "legibility", "label"]


def git_commit():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True)
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], capture_output=True, text=True).stdout
        return out.stdout.strip() + ("-modified" if dirty.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def count(cell):
    """Numeric count for analysis: printed number, 0 for a blank/dash cell, None if unreadable."""
    if cell["value"] is not None:
        return cell["value"]
    return 0 if cell["legibility"] == "blank" else None


def valid_field(field):
    return field in TOTAL_FIELDS or field in STANDARD_KEYS or (field.startswith("age_") and field[4:].isdigit())


def apply_corrections(cells, corrections, issues):
    status = "unreviewed"
    for c in corrections:
        field, value = c["field"].strip(), c["value"].strip()
        if field == "review_status":
            status = value
            continue
        if not valid_field(field):
            issues.append((field, f"correction names unknown field '{field}'"))
            continue
        is_number = re.fullmatch(r"-?\d+", value) is not None
        if value not in ("", "blank") and not is_number:
            issues.append((field, f"correction value '{value}' is not a whole number or 'blank'"))
            continue
        old = cells.get(field, {"as_printed": "", "label": ""})
        cells[field] = {
            "value": int(value) if is_number else None,
            "as_printed": old["as_printed"],
            "legibility": "blank" if value == "blank" else "corrected",
            "label": old["label"],
            "source": f"corrected by {c.get('reviewer', '').strip() or 'unknown'}",
        }
    return status


def check_bill(cells, issues):
    def total(field):
        return count(cells[field]) if field in cells else None

    for group in ("christened", "buried"):
        m, f, a = total(f"{group}_males"), total(f"{group}_females"), total(f"{group}_in_all")
        if None in (m, f, a):
            issues.append((f"{group}_in_all", f"{group} totals incomplete, cannot check"))
        elif m + f != a:
            issues.append((f"{group}_in_all", f"{group}: males {m} + females {f} = {m + f}, but 'in all' is {a}"))

    ages = [k for k in cells if k in STANDARD_KEYS or k.startswith("age_")]
    unreadable = [k for k in ages if count(cells[k]) is None]
    buried = total("buried_in_all")
    age_sum = sum(count(cells[k]) or 0 for k in ages)
    if unreadable:
        issues.append(("age_sum", f"cannot check age total; unreadable: {', '.join(unreadable)}"))
    elif buried is not None and age_sum != buried:
        issues.append(("age_sum", f"ages add up to {age_sum}, but buried 'in all' is {buried} (difference {buried - age_sum:+d})"))
    return age_sum


def compare_runs(primary, check, issues):
    for field in sorted(set(primary) | set(check), key=lambda k: (k not in TOTAL_FIELDS, age_sort_order(k), k)):
        if primary.get(field, {}).get("source", "").startswith("corrected"):
            continue
        a = primary.get(field, {}).get("value")
        b = check.get(field, {}).get("value")
        if a != b:
            issues.append((field, f"second transcription disagrees: run A read {a}, run B read {b}"))


def main():
    cfg = load_config()
    t = cfg["transcription"]
    manifest = read_csv(path(cfg, "manifest"))
    corrections = read_csv(path(cfg, "corrections"))
    out_dir = path(cfg, "output")

    bills, long_rows, queue = [], [], []
    for row in manifest:
        rec = load_transcription(cfg, t["primary_run"], row["image_id"])
        if rec is None:
            continue
        tr = rec["transcription"]
        issues = []
        cells, unrecognized, duplicates = cells_from_transcription(tr)
        for label in unrecognized:
            issues.append(("age_label", f"age label not recognized: '{label}'"))
        for label in duplicates:
            issues.append(("age_label", f"age label appears twice: '{label}'"))

        if tr["transcriber_notes"].strip():
            issues.append(("notes", tr["transcriber_notes"].strip()))

        mine = [c for c in corrections if c["image_id"].strip() == row["image_id"]]
        status = apply_corrections(cells, mine, issues)
        check = load_transcription(cfg, t["check_run"], row["image_id"])
        if check is not None:
            compare_runs(cells, cells_from_transcription(check["transcription"])[0], issues)
        for key in STANDARD_KEYS:
            if key not in cells:
                cells[key] = {"value": None, "as_printed": "", "legibility": "missing", "label": ""}
                issues.append((key, "standard age row missing from transcription"))
        for field, cell in cells.items():
            cell.setdefault("source", "ai")
            if cell["legibility"] in ("uncertain", "illegible"):
                issues.append((field, f"transcriber marked {cell['legibility']}: '{cell['as_printed']}'"))
        age_sum = check_bill(cells, issues)

        bills.append({
            "image_id": row["image_id"], "filename": row["filename"], "week_ending": row["week_ending"],
            "review_status": status, "issue_count": len(issues),
            **{f: count(cells[f]) if f in cells else None for f in TOTAL_FIELDS},
            "age_sum": age_sum,
            **{k: count(c) for k, c in cells.items() if k not in TOTAL_FIELDS},
            "transcriber_notes": tr["transcriber_notes"],
            "model_served": rec["model_served"], "transcribed_at": rec["transcribed_at"],
            "_cells": cells,
        })
        for field, msg in issues:
            cell = cells.get(field, {})
            queue.append({
                "image_id": row["image_id"], "filename": row["filename"], "week_ending": row["week_ending"],
                "review_status": status, "field": field, "issue": msg,
                **{f"{k}": cell.get(k, "") for k in CELL_FIELDS},
            })

    check_consecutive_weeks(bills, queue, cfg["checks"]["week_length_days"])

    age_keys = sorted({k for b in bills for k in b["_cells"] if k not in TOTAL_FIELDS}, key=age_sort_order)
    for b in bills:
        for k in age_keys:
            c = b["_cells"].get(k)
            if c is None:
                continue
            long_rows.append({
                "image_id": b["image_id"], "filename": b["filename"], "week_ending": b["week_ending"],
                "age_category": k, "sort_order": age_sort_order(k), "printed_label": c["label"],
                "count": count(c), "as_printed": c["as_printed"], "legibility": c["legibility"],
                "source": c["source"], "review_status": b["review_status"],
            })

    head = ["image_id", "filename", "week_ending", "review_status", "issue_count"]
    write_csv(out_dir / "bills.csv", bills,
              head + TOTAL_FIELDS + ["age_sum"] + age_keys + ["transcriber_notes", "model_served", "transcribed_at"])
    write_csv(out_dir / "ages_long.csv", long_rows,
              ["image_id", "filename", "week_ending", "age_category", "sort_order", "printed_label",
               "count", "as_printed", "legibility", "source", "review_status"])
    queue.sort(key=lambda q: (q["review_status"] != "unreviewed", q["filename"]))
    write_csv(out_dir / "review_queue.csv", queue,
              ["image_id", "filename", "week_ending", "review_status", "field", "issue"] + CELL_FIELDS)
    (out_dir / "run_info.json").write_text(json.dumps({
        "pipeline_commit": git_commit(), "primary_run": t["primary_run"], "check_run": t["check_run"],
        "bills": len(bills), "bills_with_issues": sum(1 for b in bills if b["issue_count"]),
        "corrections_applied": len(corrections),
    }, indent=2) + "\n")
    print(f"{len(bills)} bills, {len(queue)} issues -> {out_dir}")


def check_consecutive_weeks(bills, queue, week_days):
    """A bill's 'Increased/Decreased' figure should equal the change from the previous week's burials."""
    dated = []
    for b in bills:
        if not b["week_ending"]:
            continue
        try:
            dated.append((date.fromisoformat(b["week_ending"]), b))
        except ValueError:
            queue.append({"image_id": b["image_id"], "filename": b["filename"], "week_ending": b["week_ending"],
                          "review_status": b["review_status"], "field": "week_ending",
                          "issue": "week_ending is not a YYYY-MM-DD date"})
    dated.sort(key=lambda x: x[0])
    for (d0, prev), (d1, cur) in zip(dated, dated[1:]):
        if (d1 - d0).days != week_days:
            continue
        stated, now, before = cur["burials_change"], cur["buried_in_all"], prev["buried_in_all"]
        if None in (stated, now, before):
            continue
        if now - before != stated:
            queue.append({
                "image_id": cur["image_id"], "filename": cur["filename"], "week_ending": cur["week_ending"],
                "review_status": cur["review_status"], "field": "burials_change",
                "issue": f"bill says change of {stated:+d}, but burials went {before} -> {now} ({now - before:+d}) "
                         f"since {prev['filename']}",
            })
            cur["issue_count"] += 1


if __name__ == "__main__":
    main()
