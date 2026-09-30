"""Optional offload of large pipeline artifacts to a portable cache.

A fresh clone writes to ``<repo>/data`` by default. On iCloud-backed or
disk-constrained machines, either:

  export WILDFIRE_WUI_DATA_ROOT="$HOME/.cache/wildfire-wui-capstone"
  python run_pipeline.py

so ALL data tiers live in the cache from the start, or keep the in-repo
``data/`` layout and run this script to move files above a size threshold
into ``$WILDFIRE_WUI_CACHE`` (default ``~/.cache/wildfire-wui-capstone``),
replacing each with a symlink. The cache path is derived from ``Path.home()``
— never from a hardcoded ``/Users/<username>/...``.

Idempotent: already-offloaded symlinks are left alone.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    DATA_DIR,
    PROJECT_ROOT,
    cache_root,
    ensure_cache_layout,
    get_logger,
    setup_logging,
)

log = get_logger("offload_data")

DEFAULT_MIN_BYTES = 50 * 1024 * 1024  # 50 MiB
SKIP_NAMES = {".gitkeep", ".DS_Store", "provenance.json"}


def _cache_dest_for(repo_file: Path) -> Path:
    """Map a file under DATA_DIR to the parallel cache path."""
    rel = repo_file.resolve().relative_to(DATA_DIR.resolve())
    return cache_root() / rel


def offload_file(src: Path, dest: Path) -> str:
    """Move src to dest and replace src with a symlink. Return a status verb."""
    if src.is_symlink():
        return "already_symlink"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        src.unlink()
        src.symlink_to(dest)
        return "relinked"
    shutil.move(str(src), str(dest))
    src.symlink_to(dest)
    return "moved"


def iter_offload_candidates(min_bytes: int) -> list[Path]:
    files: list[Path] = []
    if not DATA_DIR.exists():
        return files
    for dirpath, dirnames, filenames in os.walk(DATA_DIR):
        dirnames[:] = [d for d in dirnames if d not in {".git"}]
        for name in filenames:
            if name in SKIP_NAMES:
                continue
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            if p.stat().st_size >= min_bytes:
                files.append(p)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-bytes", type=int, default=DEFAULT_MIN_BYTES,
        help="Only offload files at least this large (default 50 MiB).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List candidates without moving anything.",
    )
    args = parser.parse_args(argv)

    ensure_cache_layout()
    candidates = iter_offload_candidates(args.min_bytes)
    log.info(
        "Cache root: %s  DATA_DIR: %s  candidates: %d",
        cache_root(), DATA_DIR, len(candidates),
    )
    if DATA_DIR.resolve() == cache_root():
        log.info("DATA_DIR is already the cache; nothing to offload.")
        return 0
    for src in candidates:
        dest = _cache_dest_for(src)
        rel = src.relative_to(PROJECT_ROOT)
        if args.dry_run:
            log.info("[dry-run] %s -> %s (%.1f MiB)", rel, dest, src.stat().st_size / 1e6)
            continue
        verb = offload_file(src, dest)
        log.info("%s %s -> %s", verb, rel, dest)
    return 0


if __name__ == "__main__":
    setup_logging("offload_data.log")
    sys.exit(main())
