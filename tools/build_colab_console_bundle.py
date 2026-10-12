#!/usr/bin/env python
"""Build the small, reproducible Creator Console source bundle used by Colab.

Generated media, credentials, local environments, project data and Git history
are excluded by construction: only the two Python packages and the Colab
requirements file are eligible. The archive carries per-file SHA-256 hashes so
the notebook can reject a damaged extraction before executing it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / ".local_runtime" / "creator_console_colab.zip"
#: `creator_console` imports `infrastructure` (the object-store adapter is chosen
#: by the composition root in `app.py`), and `infrastructure` in turn is what a
#: future backend swap replaces, so both packages must ship or the extracted
#: bundle fails to import. `application` carries the ports those adapters satisfy.
INCLUDE_ROOTS = ("creator_console", "local_pipeline", "application", "infrastructure")
INCLUDE_SUFFIXES = {".py", ".html", ".css", ".js"}
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def source_files() -> list[Path]:
    files: list[Path] = []
    for root_name in INCLUDE_ROOTS:
        root = PROJECT_ROOT / root_name
        for path in root.rglob("*"):
            if (path.is_file() and path.suffix.lower() in INCLUDE_SUFFIXES
                    and "__pycache__" not in path.parts):
                files.append(path)
    files.append(PROJECT_ROOT / "requirements-colab.txt")
    return sorted(files, key=lambda path: path.relative_to(PROJECT_ROOT).as_posix())


def build(output: Path) -> dict:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    records = []
    payloads: list[tuple[str, bytes]] = []
    for path in source_files():
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        payload = path.read_bytes()
        payloads.append((relative, payload))
        records.append({
            "path": relative,
            "size": len(payload),
            "sha256": sha256_bytes(payload),
        })
    manifest = {
        "schema_version": 1,
        "artifact_type": "creator_console_colab_source_bundle",
        "files": records,
    }
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        for relative, payload in payloads:
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
        info = zipfile.ZipInfo("bundle_manifest.json", FIXED_ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, manifest_bytes)

    result = {
        "path": str(output),
        "size": output.stat().st_size,
        "sha256": sha256_bytes(output.read_bytes()),
        "file_count": len(records),
    }
    output.with_suffix(output.suffix + ".sha256").write_text(
        result["sha256"] + "\n", encoding="ascii")
    return result


def verify(bundle: Path) -> dict:
    bundle = bundle.resolve()
    with zipfile.ZipFile(bundle, "r") as archive:
        manifest = json.loads(archive.read("bundle_manifest.json"))
        names = set(archive.namelist())
        for record in manifest.get("files") or []:
            path = str(record.get("path") or "")
            if not path or path not in names or Path(path).is_absolute() \
                    or ".." in Path(path).parts:
                raise ValueError(f"unsafe or missing bundle entry: {path!r}")
            payload = archive.read(path)
            if len(payload) != int(record["size"]):
                raise ValueError(f"bundle size mismatch: {path}")
            if sha256_bytes(payload) != record["sha256"]:
                raise ValueError(f"bundle hash mismatch: {path}")
    return {
        "path": str(bundle), "file_count": len(manifest.get("files") or []),
        "sha256": sha256_bytes(bundle.read_bytes()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = verify(args.output) if args.verify else build(args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
