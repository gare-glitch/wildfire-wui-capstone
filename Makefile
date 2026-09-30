.PHONY: stage1 stage2 env test clean

# One-command Stage 1 reproduction.
stage1:
	.venv/bin/python run_pipeline.py

# One-command Stage 2 (WUI integration) reproduction; requires Stage 1 done.
stage2:
	.venv/bin/python run_pipeline.py --stage-set 2

# Set up the Python environment from scratch.
env:
	python3.12 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

test:
	.venv/bin/python -m pytest tests/ -q

clean:
	rm -rf data/interim/* data/processed/* outputs/figures/*.png outputs/logs/*
	touch data/interim/.gitkeep data/processed/.gitkeep