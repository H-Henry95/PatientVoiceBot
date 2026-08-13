.PHONY: install serve tunnel call batch transcribe analyze

install:
	pip install -r requirements.txt

# Run the media-stream server (leave this running in its own terminal).
serve:
	uvicorn src.server:app --host 0.0.0.0 --port $${PORT:-5050}

# Convenience: open an ngrok tunnel to the server (separate terminal).
tunnel:
	ngrok http $${PORT:-5050}

# Place a single call:  make call SCENARIO=weekend_trap
call:
	python -m scripts.run_call --scenario $(SCENARIO)

# Place the full default batch (14 calls).
batch:
	python -m scripts.run_batch

# Re-transcribe a recording from audio (verification):
#   make transcribe REC=recordings/xxxxx.mp3
transcribe:
	python -m src.transcribe $(REC)

# Draft candidate bugs from transcripts:
#   make analyze                     (whole folder)
#   make analyze T=transcripts/x.txt (one file)
analyze:
	python -m src.analyze $${T:-transcripts/}
