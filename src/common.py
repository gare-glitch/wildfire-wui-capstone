"""Shared utilities for the wildfire-wui-capstone pipeline.

Provides canonical paths, configuration loading, and logging setup so every
stage reads the same config and writes to the same locations.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Canonical paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
INTERIM_DIR = DATA_DIR / "interim"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
FIGURES_DIR = OUTPUTS_DIR / "figures"
TABLES_DIR = OUTPUTS_DIR / "tables"
LOGS_DIR = OUTPUTS_DIR / "logs"

PROVENANCE_PATH = RAW_DIR / "provenance.json"


def ensure_dirs() -> None:
    """Create all output directory tiers if missing."""
    for d in (
        RAW_DIR / "mtbs",
        RAW_DIR / "census",
        INTERIM_DIR,
        PROCESSED_DIR,
        FIGURES_DIR,
        TABLES_DIR,
        LOGS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_datasets_config() -> dict:
    """Load config/datasets.yaml (authoritative source registry)."""
    return load_yaml(CONFIG_DIR / "datasets.yaml")


def load_analysis_config() -> dict:
    """Load config/analysis.yaml (analysis parameters)."""
    return load_yaml(CONFIG_DIR / "analysis.yaml")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def setup_logging(
    log_filename: str | None = None,
    level_console: int = logging.INFO,
    level_file: int = logging.DEBUG,
) -> logging.Logger:
    """Configure the 'pipeline' logger with console + optional file handlers.

    Args:
        log_filename: base name of log file; written to outputs/logs/ with a
            UTC timestamp prefix. Pass None for console-only logging.
        level_console / level_file: logging levels for each handler.

    Returns:
        The configured 'pipeline' logger.
    """
    logger = logging.getLogger("pipeline")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(level_console)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_filename is not None:
        ensure_dirs()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        logfile = LOGS_DIR / f"{stamp}_{log_filename}"
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setLevel(level_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.info("Logging to %s", logfile)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Child logger under the 'pipeline' namespace."""
    return logging.getLogger(f"pipeline.{name}")