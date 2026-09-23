# Bills of Mortality: age-at-death transcription

This pipeline turns scanned pages of the London weekly Bills of Mortality into CSVs of
deaths by age. Researchers then check the CSVs against the page images.

**What is AI and what is code:**

- **AI does one thing:** `transcribe.py` sends each page image to Claude, which reads
  the numbers. Each answer is saved once as a JSON file, and the files are committed to
  git. They are never regenerated unless you ask. They are the fixed record of what the
  AI read, and every later step works from them.
- **Everything else is ordinary code:** listing the files, mapping labels to
  categories, checking the arithmetic, applying human corrections and writing the
  CSVs. The same inputs always give the same CSVs.

## What is transcribed

From the "Diseases and Casualties" side of each bill:

| Section | Fields |
|---|---|
| Christned | males, females, in all |
| Buried | males, females, in all |
| "Increased/Decreased in the Burials this Week" | signed change (decrease is negative) |
| "Whereof have died" | the 12 standard age brackets, plus every "A Hundred ..." row |

The totals are included so the code can check the age counts against them.

## Setup (once)

```bash
cd bills-of-mortality
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # from console.anthropic.com
```

## Workflow

### 1. Get the images into `data/raw/`

This folder is not committed to git; images are identified by checksum instead.

- **From Google Drive:** use [rclone](https://rclone.org/drive/). Run
  `rclone config` once to connect your Google account, then:
  ```bash
  rclone copy "gdrive:Bills of Mortality" data/raw/ --progress
  rclone check "gdrive:Bills of Mortality" data/raw/   # confirms every file arrived intact
  ```
- **From a laptop folder:** `rsync -a ~/path/to/scans/ data/raw/`, or just copy the files.

JPEG, PNG and TIFF are accepted, and subfolders are fine.

### 2. `make manifest`

This writes `data/manifest.csv`, with one row per image. `image_id` is the first 16
characters of the file's SHA-256 checksum, so renaming a file doesn't break anything.

**Fill in `week_ending` (YYYY-MM-DD) by hand.** The date is printed on the other side
of the bill, not on this page. The assize-of-bread date near the bottom is *not* the
week. Once dates are filled in, the code also checks each bill's "Increased/Decreased"
figure against the previous week's burials. Hand-entered columns survive re-running
this step.

### 3. `make transcribe` (the AI step)

Try `python scripts/transcribe.py --limit 5` first. Images that already have a saved
transcription are skipped, so the step can be stopped and restarted. Each JSON file in
`data/transcriptions/a/` records:

- the model and effort level
- checksums of the prompt and output format
- the SDK version and the time of the request

**Optional second pass:** `make check-run` runs a second, independent transcription.
Wherever the two disagree, the cell goes into the review queue. This catches most
one-off misreadings for roughly twice the cost.

### 4. `make csv` (code only)

This writes three files to `data/output/`:

| File | Contents |
|---|---|
| `bills.csv` | One row per bill: totals, `age_sum`, one column per age category, `review_status`, `issue_count`. |
| `ages_long.csv` | One row per bill per age category, ready for R, pandas or Excel pivot tables. |
| `review_queue.csv` | One row per problem the code found (see below). |
| `run_info.json` | The git commit and settings that produced the files. |

**Checks (all code):**

- Males plus females equals "in all", for both christenings and burials.
- The age counts add up to "Buried in all".
- All 12 standard age brackets are present.
- Every age label maps to a known category.
- Any cell the transcriber marked uncertain or illegible is listed.
- Run A and run B agree, if run B exists.
- Week-to-week burial changes match the printed "Increased/Decreased" figure, where
  dates are filled in.

**Counts:** a blank or dashed cell is counted as 0, with `legibility = blank` so you can
tell it apart from a printed 0. An unreadable cell is left empty.

### 5. Human review

Reviewers work through `review_queue.csv` with the image open. The file lists issues
first, but reviewers should look at every bill. **Don't edit the output CSVs.** Instead,
add rows to `data/corrections.csv`:

```csv
image_id,field,value,reviewer,date,note
f6758c00622366db,40_to_50,60,JO,2026-10-01,printed "6c"; damaged zero
9c2f6382e72c5ecb,review_status,source_discrepancy,JO,2026-10-01,print itself does not add up
e15c7f34290cdc82,review_status,verified,KK,2026-10-01,
```

- `field` is any column name from `bills.csv` (e.g. `under_2`, `age_101`,
  `buried_in_all`). Use `review_status` to mark a whole bill `verified`,
  `source_discrepancy`, or whatever labels the team agrees on.
- `value` is a whole number, or `blank`.

Then run `make csv` again. Corrected cells show `source = corrected by <reviewer>`.
Every human decision is recorded, can be replayed, and is under version control.

### 6. Measure accuracy

`make evaluate` compares a run against `ground_truth/age_tables.csv`, which holds
hand-checked values for sample pages. Re-run it whenever you change the model, prompt
or effort, and report the result with the dataset. Add more hand-checked pages as the
project goes on; about one in fifty, chosen at random, is a reasonable target.

## Notes from the five sample pages

- **The source itself can be wrong.** On sample `5.jpg` (bread assize of 31 Dec 1751):
  - the ages add up to 385
  - buried males + females is 429
  - buried "in all" is 447

  This is confirmed at 4× zoom; it is the print, not a misreading. The AI is told
  **not** to adjust numbers to make totals agree, so a reviewer should mark such bills
  `source_discrepancy` rather than "fixing" them.
- Damaged zeros print as "c" (`6c` = 60, `3c` = 30).
- The centenarian rows vary between editions:
  - four bare "A Hundred" rows (1732)
  - "A Hundred 5 · 1" (1743), read as 105 years with 1 death, which the total confirms
  - "A Hundred and one" / "A Hundred and three" with the count beside a brace (1751)

  The code maps these to `age_100`, `age_101`, …, and flags labels it can't map.

## Settings

Everything adjustable is in `config.toml`: model, effort, image size, run names and
paths. Changing the model or prompt changes future transcriptions only. Existing
JSONs are kept unless you run `transcribe.py --force`.
