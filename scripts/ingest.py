"""Scan the raw image folder and write or refresh data/manifest.csv.

Each image is identified by the SHA-256 of its bytes, so renaming or moving a file
does not change its ID. Columns a person fills in (week_ending, archive_ref, notes)
are kept when the manifest is regenerated.
"""

from common import load_config, path, read_csv, sha256_file, write_csv

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
FIELDS = ["image_id", "sha256", "filename", "week_ending", "archive_ref", "notes"]
HUMAN_FIELDS = ["week_ending", "archive_ref", "notes"]


def main():
    cfg = load_config()
    raw = path(cfg, "raw_images")
    manifest_path = path(cfg, "manifest")
    existing = {r["sha256"]: r for r in read_csv(manifest_path)}

    rows, seen = [], set()
    for p in sorted(raw.rglob("*")):
        if p.suffix.lower() not in IMAGE_EXTS or not p.is_file():
            continue
        digest = sha256_file(p)
        if digest in seen:
            print(f"duplicate image skipped: {p.relative_to(raw)}")
            continue
        seen.add(digest)
        old = existing.get(digest, {})
        row = {
            "image_id": digest[:16],
            "sha256": digest,
            "filename": str(p.relative_to(raw)),
        }
        for f in HUMAN_FIELDS:
            row[f] = old.get(f, "")
        rows.append(row)

    # Keep rows whose image has gone missing so hand-entered fields are never lost.
    for digest in sorted(set(existing) - seen):
        print(f"kept, but image no longer on disk: {existing[digest]['filename']}")
        rows.append(existing[digest])

    write_csv(manifest_path, rows, FIELDS)
    print(f"{len(rows)} images -> {manifest_path}")


if __name__ == "__main__":
    main()
