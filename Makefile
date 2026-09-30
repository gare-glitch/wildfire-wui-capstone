.PHONY: stage1 env test clean

# One-command Stage 1 reproduction.
stage1:
	.venv/bin/python run_pipeline.py

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