"""Send each bill image to Claude and save its transcription as JSON.

This is the only AI step. It runs Claude Code in the terminal ("claude -p"), so it
uses the Claude subscription you are logged in with, not an API key. Its outputs are
saved once, per image, per run, and committed to git; every later step reads these
files and is ordinary code. An image is not re-sent if its JSON already exists (use
--force to redo it).

Usage:
    python scripts/transcribe.py              # primary run ("a")
    python scripts/transcribe.py --run b      # independent second run for cross-checking
    python scripts/transcribe.py --limit 5    # try a few images first
"""

import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from PIL import Image

from common import load_config, path, read_csv, sha256_text

SYSTEM_PROMPT = "You transcribe historical printed documents accurately."

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
- as_printed: only the characters of the number itself (no label, no trailing \
full stop), with damaged zeros written as 0, e.g. "60", "—" for a dash, "" for \
nothing printed.
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
placement, a number you could read two ways). Empty string if nothing. Do not add \
up the figures or comment on whether totals agree; that is checked separately.
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

# Removed from the environment of each "claude" call so it always uses the logged-in
# subscription. If either is set, Claude Code would bill that key instead.
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# Stop the run after this many failures in a row (e.g. a subscription usage limit).
MAX_CONSECUTIVE_FAILURES = 3


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


def claude_env():
    return {k: v for k, v in os.environ.items() if k not in API_KEY_VARS}


def check_claude():
    """Exit with a clear message unless Claude Code is installed and logged in."""
    if shutil.which("claude") is None:
        sys.exit("Claude Code is not installed: see https://code.claude.com/docs/en/setup")
    out = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, env=claude_env())
    try:
        status = json.loads(out.stdout)
    except json.JSONDecodeError:
        sys.exit(f"could not read 'claude auth status':\n{out.stdout}{out.stderr}")
    if not status.get("loggedIn"):
        sys.exit("Claude Code is not logged in. Run 'claude', then /login with your Claude account.")
    version = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()
    return version, status.get("authMethod", "unknown")


def transcribe(cfg, image_path):
    t = cfg["transcription"]
    message = {"type": "user", "message": {"role": "user", "content": [
        image_block(image_path, t["max_long_edge_px"]), {"type": "text", "text": PROMPT},
    ]}}
    cmd = [
        "claude", "-p",
        "--model", t["model"],
        "--effort", t["effort"],
        "--system-prompt", SYSTEM_PROMPT,
        "--json-schema", json.dumps(SCHEMA),
        "--tools", "",               # no tools: Claude only looks at the image
        "--safe-mode",               # ignore the user's own CLAUDE.md, plugins, hooks and MCP servers
        "--no-session-persistence",
        "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose",
    ]
    try:
        proc = subprocess.run(cmd, input=json.dumps(message) + "\n", capture_output=True, text=True,
                              env=claude_env(), timeout=t["timeout_seconds"])
    except subprocess.TimeoutExpired:
        return {"error": f"no answer after {t['timeout_seconds']} seconds"}

    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None:
        return {"error": f"claude exited with code {proc.returncode}: {proc.stderr.strip()[:500]}"}
    if result.get("is_error") or result.get("structured_output") is None:
        return {"error": f"{result.get('subtype')}: {str(result.get('result', ''))[:500]}"}
    served = [e["message"]["model"] for e in events if e.get("type") == "assistant" and "model" in e.get("message", {})]
    return {
        "model_served": served[-1] if served else "unknown",
        "usage": result.get("usage"),
        "duration_ms": result.get("duration_ms"),
        "transcription": result["structured_output"],
    }


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
    cli_version, auth_method = check_claude()
    print(f"Claude Code {cli_version}, logged in ({auth_method})")

    done = failures = 0
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
        record = transcribe(cfg, image_path)
        if "error" in record:
            # Nothing is saved, so the next run tries this image again.
            print(f"  not saved: {record['error']}")
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"stopping after {failures} failures in a row (usage limit reached?); run again later")
                break
            continue
        failures = 0
        if record["model_served"] != cfg["transcription"]["model"]:
            print(f"  note: answered by {record['model_served']}, not {cfg['transcription']['model']}")
        record = {
            "image_id": row["image_id"],
            "sha256": row["sha256"],
            "filename": row["filename"],
            "run": run,
            "model_requested": cfg["transcription"]["model"],
            "effort": cfg["transcription"]["effort"],
            "prompt_sha256": sha256_text(SYSTEM_PROMPT + "\n" + PROMPT),
            "schema_sha256": sha256_text(json.dumps(SCHEMA, sort_keys=True)),
            "claude_code_version": cli_version,
            "transcribed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **record,
        }
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        done += 1
    print(f"{done} new transcriptions in {out_dir}")


if __name__ == "__main__":
    main()
