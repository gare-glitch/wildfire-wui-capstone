"""Tests for downloader mechanics: archive validation, quarantine, checksums."""

import json
import zipfile

import pytest

import download_data as dl


def _make_zip(path, members: dict[str, bytes]) -> str:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def test_validate_zip_accepts_valid_shapefile_zip(tmp_path):
    z = tmp_path / "good.zip"
    _make_zip(
        z,
        {
            "mtbs_perims_DD83.shp": b"fake",
            "mtbs_perims_DD83.dbf": b"fake",
            "mtbs_perims_DD83.shx": b"fake",
        },
    )
    ok, members = dl.validate_zip_archive(z, ".shp")
    assert ok
    assert "mtbs_perims_DD83.shp" in members


def test_validate_zip_rejects_wrong_content(tmp_path):
    z = tmp_path / "no_shp.zip"
    _make_zip(z, {"readme.txt": b"hello"})
    ok, _ = dl.validate_zip_archive(z, ".shp")
    assert not ok


def test_validate_zip_rejects_corrupt_archive(tmp_path):
    z = tmp_path / "corrupt.zip"
    z.write_bytes(b"this is not a zip file at all" * 100)
    ok, _ = dl.validate_zip_archive(z, ".shp")
    assert not ok


def test_validate_zip_detects_crc_corruption(tmp_path):
    z = tmp_path / "crc_bad.zip"
    # Incompressible payload large enough that its deflate stream spans the
    # middle of the file, so a flipped byte must corrupt the data stream.
    import os

    payload = os.urandom(50_000)
    _make_zip(z, {"data.shp": payload})
    raw = bytearray(z.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    z.write_bytes(bytes(raw))
    ok, _ = dl.validate_zip_archive(z, ".shp")
    assert not ok


def test_sha256(tmp_path):
    import hashlib

    f = tmp_path / "blob.bin"
    payload = b"The quick brown fox jumps over the lazy dog"
    f.write_bytes(payload)
    assert dl.sha256_of_file(f) == hashlib.sha256(payload).hexdigest()


def test_quarantine_renames_without_deleting(tmp_path):
    f = tmp_path / "data.zip"
    f.write_bytes(b"original")
    dl.quarantine(f)
    assert not f.exists()
    assert (tmp_path / "data.zip.corrupt").read_bytes() == b"original"


def test_quarantine_avoids_overwrite(tmp_path):
    f = tmp_path / "data.zip"
    f.write_bytes(b"v1")
    dl.quarantine(f)
    f.write_bytes(b"v2")
    dl.quarantine(f)
    assert (tmp_path / "data.zip.corrupt").read_bytes() == b"v1"
    assert (tmp_path / "data.zip.corrupt.1").read_bytes() == b"v2"


def test_extract_if_needed_is_idempotent(tmp_path):
    z = tmp_path / "a.zip"
    _make_zip(z, {"nested/dir/layer.shp": b"x", "layer.dbf": b"y"})
    out1 = dl.extract_if_needed(tmp_path, z, ".shp")
    out2 = dl.extract_if_needed(tmp_path, z, ".shp")
    assert out1 == out2
    assert (out1 / "layer.shp").exists()


def test_head_info_handles_bad_url():
    info = dl.head_info("https://invalid.invalid-dataset-host-nonexistent.example/x.zip")
    assert info.get("http_status") is None
    assert "error" in info