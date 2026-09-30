"""Shared utilities for the wildfire-wui-capstone pipeline.

Provides canonical paths, configuration loading, and logging setup so every
stage reads the same config and writes to the same locations.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Canonical paths
# ---------------------------------------------------------------------------
# Data location is overridable so a fresh clone can keep large artifacts off
# iCloud (or any small/synced volume) without hardcoding a machine path:
#   WILDFIRE_WUI_DATA_ROOT=/path/to/store          (env)
#   .data_root  file in the repo (first non-comment line; ~ and $HOME expand)
# Default remains <repo>/data. Never store /Users/<username>/... in source.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_ROOT_ENV = "WILDFIRE_WUI_DATA_ROOT"
CACHE_ENV = "WILDFIRE_WUI_CACHE"
DATA_ROOT_FILE = ".data_root"
DEFAULT_CACHE_NAME = "wildfire-wui-capstone"


def cache_root() -> Path:
    """External cache directory (created on demand; never a username path)."""
    raw = os.environ.get(CACHE_ENV)
    if raw:
        return Path(os.path.expandvars(raw)).expanduser().resolve()
    return (Path.home() / ".cache" / DEFAULT_CACHE_NAME).resolve()


def ensure_cache_layout() -> Path:
    """Create the portable cache tree used for optional offload of large files."""
    root = cache_root()
    for sub in ("raw", "interim", "processed", "processed/wui"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _resolve_data_dir() -> Path:
    raw = os.environ.get(DATA_ROOT_ENV)
    if raw:
        p = Path(os.path.expandvars(raw)).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    marker = PROJECT_ROOT / DATA_ROOT_FILE
    if marker.exists():
        for line in marker.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                p = Path(os.path.expandvars(line)).expanduser().resolve()
                p.mkdir(parents=True, exist_ok=True)
                return p
    return PROJECT_ROOT / "data"


DATA_DIR = _resolve_data_dir()
RAW_DIR = DATA_DIR / "raw"
INTERIM_DIR = DATA_DIR / "interim"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
FIGURES_DIR = OUTPUTS_DIR / "figures"
TABLES_DIR = OUTPUTS_DIR / "tables"
LOGS_DIR = OUTPUTS_DIR / "logs"

PROVENANCE_PATH = RAW_DIR / "provenance.json"

# Stage 2 WUI product directory (county WUI metrics, WUI fragments, places)
WUI_DIR = PROCESSED_DIR / "wui"


def stored_path(path: Path) -> str:
    """Serialize a filesystem path without embedding /Users/<username>/.

    Repo-relative paths are stored relative to PROJECT_ROOT. Paths under the
    user's home directory (including the default cache) are stored with a
    leading ``~``. Anything else is stored as an absolute path.
    """
    path = Path(path).resolve()
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        pass
    try:
        return str(Path("~") / path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def resolve_stored_path(value: str) -> Path:
    """Inverse of stored_path: relative, ``~``, or absolute."""
    p = Path(value)
    if value.startswith("~"):
        return p.expanduser().resolve()
    if p.is_absolute():
        return p
    return (PROJECT_ROOT / p).resolve()


def ensure_dirs() -> None:
    """Create all output directory tiers if missing."""
    ensure_cache_layout()
    for d in (
        RAW_DIR / "mtbs_perimeter",
        RAW_DIR / "census_counties",
        RAW_DIR / "silvis_wui",
        RAW_DIR / "nature_wui",
        RAW_DIR / "census_places",
        INTERIM_DIR,
        PROCESSED_DIR,
        WUI_DIR,
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


def load_stage2_config() -> dict:
    """Load the `stage2` block of config/analysis.yaml (WUI integration)."""
    cfg = load_analysis_config()
    try:
        return cfg["stage2"]
    except KeyError as exc:
        raise KeyError("config/analysis.yaml is missing the 'stage2' section") from exc


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