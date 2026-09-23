"""Send each bill image to Claude and save its transcription as JSON.

This is the only AI step. Its outputs are saved once, per image, per run, and
committed to git; every later step reads these files and is ordinary code. An image
is not re-sent if its JSON already exists (use --force to redo it).

Usage:
    python scripts/transcribe.py              # primary run ("a")
    python scripts/transcribe.py --run b      # independent second run for cross-checking
    python scripts/transcribe.py --limit 5    # try a few images first
"""

import argparse
import base64
import io
import json
from datetime import datetime, timezone

import anthropic
from PIL import Image

from common import load_config, path, read_csv, sha256_text

PROMPT = """\
This image is one page of an eighteenth-century London weekly Bill of Mortality \
("The Diseases and Casualties this Week"). Transcribe ONLY these parts:

1. "Christned": Males, Females, In all.
2. "Buried": Males, Females, In all.
3. The line "Increased/Decreased in the Burials this Week N".
4. The age table that follows "Whereof have died": every row, in the order printed \
(reading down each column, left column first), including every "A Hundred ..." row \
even when it has no number.

Rules:
- Transcribe exactly what is printed. Do NOT change any number to make totals add \
up. The printed totals sometimes genuinely disagree, and those disagreements are \
evidence the researchers need to see.
- Zeros are often damaged and print like "c" or "o" (e.g. "6c" or "3o"). Write every \
damaged zero as 0, in both as_printed and value ("6c" becomes "60").
- as_printed: the characters as they appear, with damaged zeros written as 0, e.g. \
"60", "—" for a dash, "" for nothing printed.
- value: your best reading as an integer, or null if the cell is blank, a dash, or \
unreadable.
- legibility: "clear"; "uncertain" if you are not sure of every digit; "illegible" \
if you cannot read it; "blank" if nothing or only a dash is printed.
- printed_label: the row label as printed (keep misprints such as "A Huudred"), with \
the long s written as "s", without the leader dashes. Join a label split over two lines (e.g. "A Hundred" / "and one" \
becomes "A Hundred and one").
- If one number sits beside a brace covering several lines, give it to the row \
whose label the brace joins, and say so in transcriber_notes.
- burials_change.direction: "increased", "decreased", or "not_printed".
- transcriber_notes: anything a checker should look at (damage, ambiguous \
placement, a number you could read two ways). Empty string if nothing.
"""

CELL = {
    "as_printed": {"type": "string"},
    "value": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    "legibility": {"type": "string", "enum": ["clear", "uncertain", "illegible", "blank"]},
}


def cell_schema():
    return {"type": "object", "properties": CELL, "required": list(CELL), "additionalProperties": False}


def group_schema():
    parts = ["males", "females", "in_all"]
    return {
        "type": "object",
        "properties": {p: cell_schema() for p in parts},
        "required": parts,
        "additionalProperties": False,
    }


SCHEMA = {
    "type": "object",
    "properties": {
        "christened": group_schema(),
        "buried": group_schema(),
        "burials_change": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["increased", "decreased", "not_printed"]},
                **CELL,
            },
            "required": ["direction", *CELL],
            "additionalProperties": False,
        },
        "age_rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"printed_label": {"type": "string"}, **CELL},
                "required": ["printed_label", *CELL],
                "additionalProperties": False,
            },
        },
        "transcriber_notes": {"type": "string"},
    },
    "required": ["christened", "buried", "burials_change", "age_rows", "transcriber_notes"],
    "additionalProperties": False,
}

MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def image_block(p, max_edge):
    """Send the original bytes when possible; otherwise downscale / convert to PNG."""
    with Image.open(p) as im:
        media_type = MEDIA_TYPES.get(p.suffix.lower())
        if media_type and max(im.size) <= max_edge:
            data = p.read_bytes()
        else:
            im = im.convert("L") if im.mode not in ("L", "RGB") else im
            im.thumbnail((max_edge, max_edge), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            data, media_type = buf.getvalue(), "image/png"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode()},
    }


def transcribe(client, cfg, image_path):
    t = cfg["transcription"]
    response = client.beta.messages.create(
        model=t["model"],
        max_tokens=t["max_tokens"],
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": t["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": [image_block(image_path, t["max_long_edge_px"]), {"type": "text", "text": PROMPT}],
        }],
    )
    record = {
        "stop_reason": response.stop_reason,
        "model_served": response.model,
        "usage": response.usage.model_dump(mode="json"),
    }
    if response.stop_reason != "end_turn":
        record["error"] = f"stop_reason={response.stop_reason}"
        return record
    text = next(b.text for b in response.content if b.type == "text")
    record["transcription"] = json.loads(text)
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="run name (default: primary_run in config.toml)")
    ap.add_argument("--limit", type=int, help="stop after this many new transcriptions")
    ap.add_argument("--force", action="store_true", help="redo images that already have a transcription")
    args = ap.parse_args()

    cfg = load_config()
    run = args.run or cfg["transcription"]["primary_run"]
    out_dir = path(cfg, "transcriptions") / run
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = path(cfg, "raw_images")
    client = anthropic.Anthropic()

    done = 0
    for row in read_csv(path(cfg, "manifest")):
        out = out_dir / f"{row['image_id']}.json"
        image_path = raw / row["filename"]
        if out.exists() and not args.force:
            continue
        if not image_path.exists():
            print(f"missing image, skipped: {row['filename']}")
            continue
        if args.limit is not None and done >= args.limit:
            break
        print(f"transcribing {row['filename']} ...", flush=True)
        try:
            record = transcribe(client, cfg, image_path)
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            # The SDK has already retried rate limits and server errors; leave this one for the next run.
            print(f"  API error, will retry next run: {e}")
            continue
        if "error" in record:
            print(f"  not saved: {record['error']}")
            continue
        record = {
            "image_id": row["image_id"],
            "sha256": row["sha256"],
            "filename": row["filename"],
            "run": run,
            "model_requested": cfg["transcription"]["model"],
            "effort": cfg["transcription"]["effort"],
            "prompt_sha256": sha256_text(PROMPT),
            "schema_sha256": sha256_text(json.dumps(SCHEMA, sort_keys=True)),
            "sdk_version": anthropic.__version__,
            "transcribed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **record,
        }
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        done += 1
    print(f"{done} new transcriptions in {out_dir}")


if __name__ == "__main__":
    main()
