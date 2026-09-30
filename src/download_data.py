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
    load_analysis_config,
    load_datasets_config,
    setup_logging,
    stored_path,
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
def file_specs_from_meta(meta: dict) -> list[dict]:
    """Expand a dataset registry entry into concrete file specs.

    Supports:
    - `url`: single file
    - `url_pattern` + state FIPS list: one file per CONUS state (e.g. TIGER
      state place files); the FIPS list comes from config/analysis.yaml so
      the scope definition lives in exactly one place.
    - `additional_files`: auxiliary archives downloaded alongside the primary
    """
    specs: list[dict] = []
    if "url" in meta:
        specs.append({
            "url": meta["url"],
            # Zenodo API URLs end in '/content'; the documented archive name
            # is the canonical local filename for the primary file.
            "filename": meta.get("documented_file_name"),
            "expected_archive_member_suffix": meta.get(
                "expected_archive_member_suffix", ".shp"
            ),
            "auto_extract": meta.get("auto_extract", True),
            "role": "primary",
        })
    if "url_pattern" in meta:
        if "state_fips_source" in meta:
            conus_fips = load_analysis_config()["scope"]["conus_state_fips"]
            for fips in conus_fips:
                specs.append({
                    "url": meta["url_pattern"].format(state_fips=fips),
                    "filename": None,  # derived from the URL (tl_2025_XX_place.zip)
                    "expected_archive_member_suffix": meta.get(
                        "expected_archive_member_suffix", ".shp"
                    ),
                    "auto_extract": meta.get("auto_extract", True),
                    "role": "primary",
                })
    for extra in meta.get("additional_files", []):
        specs.append({
            "url": extra["url"],
            "filename": extra.get("documented_file_name"),
            "expected_archive_member_suffix": extra.get(
                "expected_archive_member_suffix", ".shp"
            ),
            "auto_extract": extra.get("auto_extract", True),
            "role": "auxiliary",
        })
    if not specs:
        raise ValueError("dataset entry defines no url / url_pattern / additional_files")
    return specs


def acquire_one_file(url: str, dest_dir: Path, expected_suffix: str,
                     force: bool = False, fname: str | None = None) -> dict:
    """Download (or reuse) one archive and return its provenance record.

    Raw data is immutable: downloads land as '<name>.zip.part' and are
    renamed atomically only after passing integrity checks; existing valid
    files are never re-downloaded; corrupt files are quarantined with a
    '.corrupt' suffix and re-fetched.

    `fname` overrides the URL-derived filename (needed where API URLs do not
    end in the archive name, e.g. Zenodo '/content' endpoints).
    """
    fname = fname or url.split("/")[-1].split("?")[0]
    dest = dest_dir / fname

    if dest.exists() and not force:
        ok, _ = validate_zip_archive(dest, expected_suffix)
        if ok:
            log.info("Reusing existing valid raw file: %s", dest)
            return {
                "url": url,
                "file_name": fname,
                "status": "cached",
                "retrieval_utc": utc_now_iso(),
                "size_bytes": dest.stat().st_size,
                "sha256": sha256_of_file(dest),
                "archive_valid": True,
            }
        log.warning("Existing file %s failed validation; quarantining for re-download.", dest)
        quarantine(dest)

    log.info("Downloading %s -> %s", url, dest)
    dest, _resumed = download_file(url, dest)

    ok, members = validate_zip_archive(dest, expected_suffix)
    record = {
        "url": url,
        "file_name": fname,
        "status": "downloaded",
        "retrieval_utc": utc_now_iso(),
        "size_bytes": dest.stat().st_size,
        "sha256": sha256_of_file(dest),
        "archive_valid": ok,
        "archive_members": members,
    }
    if not ok:
        quarantine(dest)
        raise RuntimeError(f"Downloaded archive failed validation: {dest}")
    return record


def acquire_dataset(key: str, meta: dict, force: bool = False) -> dict:
    """Acquire every file of a dataset and return its provenance record."""
    dest_dir = RAW_DIR / key
    specs = file_specs_from_meta(meta)
    default_suffix = meta.get("expected_archive_member_suffix", ".shp")

    record: dict = {
        "display_name": meta.get("display_name"),
        "provider": meta.get("provider"),
        "documentation_urls": meta.get("documentation_urls", []),
        "documented_file_name": meta.get("documented_file_name"),
        "n_files": len(specs),
    }
    for k in ("release_date", "boundaries_as_of", "renewal", "doi", "paper_doi",
              "layer_name", "crs_note", "methodology_citation", "class_domain"):
        if k in meta:
            record[k] = meta[k]

    file_records: dict[str, dict] = {}
    all_ok = True
    first_primary_seen = False
    for spec in specs:
        suffix = spec.get("expected_archive_member_suffix", default_suffix)
        try:
            frec = acquire_one_file(
                spec["url"], dest_dir, suffix, force=force,
                fname=spec.get("filename"),
            )
        except Exception as exc:  # noqa: BLE001 - record and continue
            log.error("FAILED to acquire %s: %s", spec["url"], exc)
            frec = {
                "url": spec["url"],
                "status": "failed",
                "error": str(exc),
                "attempted_utc": utc_now_iso(),
            }
            all_ok = False
        url_key = frec.get("file_name") or spec["url"].split("/")[-1]
        file_records[url_key] = frec
        # Upstream metadata for the first primary file only (best effort);
        # per-state pattern datasets share one host and release.
        if spec["role"] == "primary" and frec.get("status") != "failed" \
                and not first_primary_seen:
            frec.setdefault("upstream", head_info(spec["url"]))
            first_primary_seen = True

    record.update({
        "files": file_records,
        "archive_valid": all_ok,
        "retrieval_utc": utc_now_iso(),
    })
    # Keep the Stage-1 single-file contract: top-level url/sha256/size for the
    # first primary file so existing consumers keep working.
    primary = next((f for f in file_records.values() if f.get("status") != "failed"), None)
    if primary is not None:
        record.update({
            "url": primary["url"],
            "sha256": primary.get("sha256"),
            "size_bytes": primary.get("size_bytes"),
            "status": primary.get("status"),
        })
    else:
        record.update({"url": specs[0]["url"], "status": "failed"})

    if not all_ok:
        record["status"] = "failed"

    # Extraction is opt-out for very large archives processed directly from
    # the zip via GDAL /vsizip/ (disk-constrained environments).
    if meta.get("auto_extract", True) and record.get("status") != "failed":
        suffix = specs[0].get("expected_archive_member_suffix", default_suffix)
        # The primary archive's local filename (documented name when the
        # registry provides one; URL basename otherwise).
        primary_fname = specs[0].get("filename") or \
            specs[0]["url"].split("/")[-1].split("?")[0]
        archive = dest_dir / primary_fname
        try:
            record["extraction_dir"] = stored_path(
                extract_if_needed(dest_dir, archive, suffix)
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Extraction failed for %s: %s", key, exc)
            record["extraction_error"] = str(exc)
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
        help="Restrict to dataset key(s), comma-separated (default: all).",
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
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        unknown = [k for k in keys if k not in datasets]
        if unknown:
            log.error("Unknown dataset key(s): %s", unknown)
            return 2
    else:
        keys = list(datasets.keys())

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