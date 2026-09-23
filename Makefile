# Each step reads the previous step's files. `make all` rebuilds from the images.
PY ?= python3

.PHONY: all manifest transcribe check-run csv evaluate

all: manifest transcribe csv

manifest:     ## scan data/raw and update data/manifest.csv
	$(PY) scripts/ingest.py

transcribe:   ## AI step (claude -p): transcribe any image without a saved JSON (run "a")
	$(PY) scripts/transcribe.py

check-run:    ## optional: independent second transcription (run "b") for cross-checking
	$(PY) scripts/transcribe.py --run b

csv:          ## code only: apply corrections, run checks, write data/output/*.csv
	$(PY) scripts/build_csv.py

evaluate:     ## accuracy against ground_truth/age_tables.csv
	$(PY) scripts/evaluate.py
