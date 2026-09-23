# Bills of Mortality: age-at-death transcription

This pipeline turns scanned pages of the London weekly Bills of Mortality into CSVs of
deaths by age. Researchers then check the CSVs against the page images.

**What is AI and what is code:**

- **AI does one thing:** `transcribe.py` sends each page image to Claude, which reads
  the numbers. It runs Claude Code in the terminal (`claude -p`), so it uses the Claude
  subscription you are logged in with; no API key is needed. Each answer is saved once as a JSON file, and the files are committed to
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

You need Python 3.11 or newer, git, and [Claude Code](https://code.claude.com/docs/en/setup)
logged in to a Claude subscription whose plan includes the model set in `config.toml`.

```bash
git clone https://github.com/jmotis/bom-data-work.git
cd bom-data-work
python3 -m pip install --user -r requirements.txt   # installs pillow, the only extra package
claude          # first time only: type /login, sign in with your Claude account, then /exit
claude auth status   # should show "loggedIn": true
```

If `pip` stops with an "externally-managed-environment" error (common with Homebrew
Python on a Mac), add `--break-system-packages` to that command. With `--user` it still
installs only into your own account, not the system's Python.

`transcribe.py` removes `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from each
`claude` call, so an API key set in your terminal is never billed by mistake. It also
runs Claude Code in safe mode with no tools, so your own settings, plugins and
CLAUDE.md files can't affect the transcription.

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

Try `python3 scripts/transcribe.py --limit 5` first. Each page takes about 15–25
seconds. Images that already have a saved transcription are skipped, so the step can be
stopped with Ctrl-C and restarted. A page that fails is not saved and is tried again on
the next run. After three failures in a row the script stops, which usually means the
subscription's usage limit has been reached: wait for it to reset, then run it again.
Commit the new JSON files as you go.

Each JSON file in `data/transcriptions/a/` records:

- the model requested and the model that actually answered (the script warns if they
  differ), and the effort level
- checksums of the prompt and output format
- the Claude Code version, token usage and the time of the request

**Optional second pass:** `make check-run` runs a second, independent transcription.
Wherever the two disagree, the cell goes into the review queue. This catches most
one-off misreadings, but uses about twice as much of the subscription.

### 4. `make csv` (code only)

This writes four files to `data/output/`:

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
- Each number read matches the characters transcribed for it.
- Run A and run B agree, if run B exists.
- Week-to-week burial changes match the printed "Increased/Decreased" figure, where
  dates are filled in.

**Counts:** a blank or dashed cell is counted as 0, with `legibility = blank` so you can
tell it apart from a printed 0. An unreadable cell is left empty. An age category the
bill doesn't print at all (e.g. no "A Hundred" rows) is also left empty.

**Damaged zeros** often print as "c" or "o" (`6c` = 60). They are written as 0
everywhere, including `as_printed`. The AI is told to do this, and the code also
converts any that slip through.

### 5. Human review

Reviewers work through `review_queue.csv` with the image open. The file lists issues
first, but reviewers should look at every bill. **Don't edit the output CSVs.** Instead,
add rows to `data/corrections.csv`:

```csv
image_id,field,value,reviewer,date,note
f6758c00622366db,40_to_50,60,JO,2026-10-01,"damaged 6c, confirmed 60 at zoom"
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
checked values for sample pages. Pages are matched by SHA-256, so the ground-truth
images must be the exact files in `data/raw/`; the `note` on each page's first row
says where its values came from. Re-run it whenever you change the model, prompt
or effort, and report the result with the dataset. Add more hand-checked pages as the
project goes on; about one in fifty, chosen at random, is a reasonable target.

## Notes from the sample pages

Eight sample bills (1732–1751) are in the ground truth. What they show:

- **The source itself can be wrong.** On sample `5.jpg` (bread assize of 31 Dec 1751):
  - the ages add up to 385
  - buried males + females is 429
  - buried "in all" is 447

  This is confirmed at 4× zoom; it is the print, not a misreading. The AI is told
  **not** to adjust numbers to make totals agree, so a reviewer should mark such bills
  `source_discrepancy` rather than "fixing" them. The other seven samples balance
  exactly.
- Damaged zeros print as "c" (`6c` = 60, `3c` = 30); see **Damaged zeros** above.
- The centenarian rows vary between editions:
  - four bare "A Hundred" rows, sometimes with a count on the first (1732, 1736)
  - a misprinted "A Huudred" (1736)
  - "A Hundred 5 · 1" (1743), read as 105 years with 1 death, which the total confirms
  - "A Hundred and two" / "A Hundred and five" in braces with no counts (1747)
  - "A Hundred and one" / "A Hundred and three" with the count beside a brace (1751)
  - no "A Hundred" rows at all (1738)

  The code maps all of these to `age_100`, `age_101`, …, and flags labels it can't map.
- The burials line says either "Increased" or "Decreased"; a decrease is stored as a
  negative number.
- Samples `6.jpg`–`8.jpg` (1736, 1747, 1738) were read by Claude from images shared in
  chat and are marked "not yet hand-checked" in the ground truth. Every total balances,
  but someone should confirm them against the scans, and check that those images are
  byte-identical to the copies in `data/raw/` (otherwise their SHA-256 won't match).

## Settings

Everything adjustable is in `config.toml`: model, effort, per-page timeout, image size, run names and
paths. Changing the model or prompt changes future transcriptions only. Existing
JSONs are kept unless you run `transcribe.py --force`.
