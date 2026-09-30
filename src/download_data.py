"""Reproducible downloader for authoritative raw datasets.

Reads the authoritative source registry (config/datasets.yaml), downloads
each dataset to data/raw/<key>/, validates the archive, and records full
provenance (URL, retrieval date, size, SHA-256, upstream last-modified) in
data/raw/provenance.json.

Design rules (AGENTS.md):
- Raw data is immutable: downloads land as '<name>.zip.part' and are renamed
  atomically only after passing integrity checks; existing valid files are
  never re-downloaded; corrupt files are quarantined with a '.corrupt' suffix
  and re-fetched.
- Archives are validated (ZIP CRC test + expected member types) before any
  downstream stage may consume them.

CLI:
    python src/download_data.py [--only KEY] [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PROVENANCE_PATH,
    RAW_DIR,
    ensure_dirs,
    get_logger,
    load_datasets_config,
    setup_logging,
    utc_now_iso,
)

CHUNK_SIZE = 1024 * 1024  # 1 MiB
DOWNLOAD_TIMEOUT = 60  # per-request connect/read timeout (seconds)
MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 5.0

log = get_logger("download")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_zip_archive(path: Path, expected_member_suffix: str) -> tuple[bool, list[str]]:
    """Return (is_valid, member_names).

    A valid archive passes the ZIP CRC test and contains at least one member
    matching the expected suffix (e.g. '.shp' for shapefile distributions).
    """
    if not zipfile.is_zipfile(path):
        return False, []
    members: list[str] = []
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()  # CRC-checks every member; returns first bad name
        if bad is not None:
            log.error("Archive %s has corrupt member: %s", path.name, bad)
            return False, []
        members = [m for m in zf.namelist() if not m.endswith("/")]
        if not any(m.lower().endswith(expected_member_suffix.lower()) for m in members):
            log.error(
                "Archive %s contains no member ending in %r. Members: %s",
                path.name,
                expected_member_suffix,
                members[:20],
            )
            return False, members
    return True, members


def extract_archive(path: Path, dest: Path, expected_member_suffix: str) -> Path:
    """Extract an already-validated archive into dest/<stem>/ .

    Returns the directory containing the extracted members. Extraction is a
    read-only operation on the raw archive; the original zip is untouched.
    """
    with zipfile.ZipFile(path) as zf:
        zf.extractall(dest)
    shp_members = [
        m for m in zf.namelist()
        if m.lower().endswith(expected_member_suffix.lower())
    ]
    # Return the directory that directly contains the first matching member.
    first = shp_members[0] if shp_members else zf.namelist()[0]
    return (dest / first).resolve().parent


def download_file(url: str, dest: Path) -> tuple[Path, bool]:
    """Stream `url` to `dest` with resume support.

    Returns (final_path, resumed_flag). Uses HTTP Range to resume an existing
    '.part' file so interrupted runs are cheap to re-run.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    resumed = part.exists() and part.stat().st_size > 0
    mode = "ab" if resumed else "wb"
    existing = part.stat().st_size if resumed else 0
    headers = {"Range": f"bytes={existing}-"} if resumed else {}

    last_err: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
                allow_redirects=True,
            ) as resp:
                if resumed and resp.status_code != 206:
                    # Server ignored the Range header: restart from scratch.
                    log.warning("Server did not honor Range; restarting download.")
                    mode, existing, resumed, headers = "wb", 0, False, {}
                    resp.raise_for_status()
                else:
                    resp.raise_for_status()

                total = int(resp.headers.get("content-length", 0)) + existing
                done = existing
                with open(part, mode) as fh:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            fh.write(chunk)
                            done += len(chunk)
                            if done % (CHUNK_SIZE * 256) < CHUNK_SIZE:
                                pct = f"{100 * done / total:5.1f}%" if total else "?"
                                log.info("  ... %s %s (%s / %s bytes)",
                                         dest.name, pct, f"{done:,}", f"{total:,}")
            part.rename(dest)
            return dest, resumed
        except Exception as exc:  # noqa: BLE001 - retry on any transport error
            last_err = exc
            log.warning("Attempt %s/%s failed: %s", attempt, MAX_ATTEMPTS, exc)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"Download failed after {MAX_ATTEMPTS} attempts: {url}") from last_err


def head_info(url: str) -> dict:
    """Fetch upstream metadata (last-modified, content-length) via HEAD."""
    try:
        resp = requests.head(url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
        return {
            "http_status": resp.status_code,
            "http_last_modified": resp.headers.get("last-modified"),
            "http_content_length": resp.headers.get("content-length"),
            "http_content_type": resp.headers.get("content-type"),
        }
    except requests.RequestException as exc:
        log.warning("HEAD request failed for %s: %s", url, exc)
        return {"http_status": None, "error": str(exc)}


# ---------------------------------------------------------------------------
# Main per-dataset routine
# ---------------------------------------------------------------------------
def acquire_dataset(key: str, meta: dict, force: bool = False) -> dict:
    """Download (or reuse) one dataset and return its provenance record."""
    url = meta["url"]
    fname = url.split("/")[-1].split("?")[0]
    dest_dir = RAW_DIR / key
    dest = dest_dir / fname
    expected_suffix = meta.get("expected_archive_member_suffix", ".shp")

    record: dict = {
        "display_name": meta.get("display_name"),
        "provider": meta.get("provider"),
        "url": url,
        "documentation_urls": meta.get("documentation_urls", []),
        "documented_file_name": meta.get("documented_file_name"),
    }
    for k in ("release_date", "boundaries_as_of", "renewal"):
        if k in meta:
            record[k] = meta[k]

    # Reuse existing raw file if it validates. Never overwrite raw data.
    if dest.exists() and not force:
        ok, _ = validate_zip_archive(dest, expected_suffix)
        if ok:
            log.info("Reusing existing valid raw file: %s", dest)
            record.update({
                "status": "cached",
                "retrieval_utc": utc_now_iso(),
                "size_bytes": dest.stat().st_size,
                "sha256": sha256_of_file(dest),
                "archive_valid": True,
            })
            record.update(head_info(url))
            record["extraction_dir"] = str(extract_if_needed(dest_dir, dest, expected_suffix))
            return record
        log.warning("Existing file %s failed validation; quarantining for re-download.", dest)
        quarantine(dest)

    log.info("Downloading %s -> %s", url, dest)
    dest, _resumed = download_file(url, dest)

    ok, members = validate_zip_archive(dest, expected_suffix)
    record.update(head_info(url))
    record.update({
        "status": "downloaded",
        "retrieval_utc": utc_now_iso(),
        "size_bytes": dest.stat().st_size,
        "sha256": sha256_of_file(dest),
        "archive_valid": ok,
        "archive_members": members,
    })
    if not ok:
        quarantine(dest)
        raise RuntimeError(f"Downloaded archive failed validation: {dest}")

    record["extraction_dir"] = str(extract_if_needed(dest_dir, dest, expected_suffix))
    return record


def quarantine(dest: Path) -> None:
    """Rename a corrupt raw file with a .corrupt suffix (never delete)."""
    if dest.exists():
        corrupt = dest.with_suffix(dest.suffix + ".corrupt")
        i = 1
        while corrupt.exists():
            corrupt = dest.with_suffix(dest.suffix + f".corrupt.{i}")
            i += 1
        dest.rename(corrupt)
        log.warning("Quarantined corrupt file to %s", corrupt)


def extract_if_needed(dest_dir: Path, archive: Path, expected_suffix: str) -> Path:
    """Extract the validated archive if not already extracted; return path.

    Idempotent: if the expected member already exists on disk with a size
    matching the archive member, skip re-extraction.
    """
    with zipfile.ZipFile(archive) as zf:
        member = next(
            (m for m in zf.namelist() if m.lower().endswith(expected_suffix.lower())),
            None,
        )
        if member is None:
            raise RuntimeError(f"No {expected_suffix} member in {archive}")
        out_path = dest_dir / "extracted" / member
        info = zf.getinfo(member)
        if out_path.exists() and out_path.stat().st_size == info.file_size:
            return out_path.resolve().parent
        zf.extractall(dest_dir / "extracted")
        return out_path.resolve().parent


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=list(load_datasets_config().keys()),
        help="Restrict to a single dataset key (default: all).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if a valid local copy exists.",
    )
    args = parser.parse_args(argv)

    setup_logging("download.log")
    ensure_dirs()

    datasets = load_datasets_config()
    keys = [args.only] if args.only else list(datasets.keys())

    provenance: dict = {}
    if PROVENANCE_PATH.exists():
        with open(PROVENANCE_PATH, "r", encoding="utf-8") as fh:
            provenance = json.load(fh)

    all_ok = True
    for key in keys:
        try:
            provenance.setdefault("datasets", {})[key] = acquire_dataset(
                key, datasets[key], force=args.force
            )
        except Exception as exc:  # noqa: BLE001 - record and continue
            log.error("FAILED to acquire %s: %s", key, exc)
            provenance.setdefault("datasets", {})[key] = {
                "url": datasets[key]["url"],
                "status": "failed",
                "error": str(exc),
                "attempted_utc": utc_now_iso(),
            }
            all_ok = False

    provenance["generated_utc"] = utc_now_iso()
    with open(PROVENANCE_PATH, "w", encoding="utf-8") as fh:
        json.dump(provenance, fh, indent=2, sort_keys=True)
    log.info("Provenance manifest written to %s", PROVENANCE_PATH)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())