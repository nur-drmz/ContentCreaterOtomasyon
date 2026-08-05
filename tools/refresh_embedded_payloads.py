#!/usr/bin/env python
"""Refreshes the compressed Base85 + SHA-256 `local_pipeline` payloads embedded in each
notebook's bootstrap cell, in place.

WHY THIS EXISTS
---------------
`00_youtube_automation_master.ipynb` must work when it is the ONLY file
uploaded to a fresh Colab session, so every notebook that needs a
`local_pipeline` module carries a self-contained, hash-verified compressed snapshot
of that module as a last-resort fallback (after: direct import -> Drive/ancestor
copy -> `PROJECT_REPO_URL` clone).

Those snapshots go stale the moment you edit a file under `local_pipeline/`.
`verify_local.py` fails loudly when that happens (see its
"Embedded local_pipeline payload hash synchronization" section). This script is
the fix for that failure:

    python tools/refresh_embedded_payloads.py

It is idempotent and surgical: it rewrites ONLY the `_LOCAL_PIPELINE_PAYLOADS`
literal and the `_LOCAL_PIPELINE_MODULE_NAMES` list inside each bootstrap cell,
leaving every other cell -- and the bootstrap loader logic itself -- untouched.
Pass --check to verify without writing (exit 1 if anything is out of sync).
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import sys
import zlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_PIPELINE_DIR = PROJECT_ROOT / "local_pipeline"
NOTEBOOK_DIR = PROJECT_ROOT / "experiments" / "colab"

PAYLOAD_START_MARKER = "_LOCAL_PIPELINE_PAYLOADS = {"
PAYLOAD_END_MARKER = "}\n"


def module_source_and_hash(module_name: str) -> tuple[bytes, str]:
    module_path = LOCAL_PIPELINE_DIR / f"{module_name}.py"
    if not module_path.is_file():
        raise SystemExit(f"HATA: kanonik modul bulunamadi: {module_path}")
    source_bytes = module_path.read_text(encoding="utf-8").encode("utf-8")
    return source_bytes, hashlib.sha256(source_bytes).hexdigest()


def render_payload_literal(module_names: list[str]) -> str:
    """Renders compact zlib+Base85 payloads for the notebook fallback."""
    lines = ["_LOCAL_PIPELINE_PAYLOADS = {"]
    for name in module_names:
        source_bytes, digest = module_source_and_hash(name)
        compressed = base64.b85encode(zlib.compress(source_bytes, level=9)).decode("ascii")
        lines.append(f"    {name!r}: {{")
        lines.append(f"        'sha256': {digest!r},")
        lines.append("        'encoding': 'zlib+b85',")
        lines.append("        'data': (")
        for i in range(0, len(compressed), 100):
            lines.append(f"            {compressed[i:i + 100]!r}")
        lines.append("        ),")
        lines.append("    },")
    lines.append("}")
    return "\n".join(lines)


def find_bootstrap_cell(nb: dict) -> int | None:
    for idx, cell in enumerate(nb["cells"]):
        if cell["cell_type"] != "code":
            continue
        if PAYLOAD_START_MARKER in "".join(cell["source"]):
            return idx
    return None


def parse_embedded(source: str) -> tuple[list[str], dict]:
    """Extracts (_LOCAL_PIPELINE_MODULE_NAMES, _LOCAL_PIPELINE_PAYLOADS) from a
    bootstrap cell via AST -- never by executing it."""
    tree = ast.parse(source)
    names, payloads = None, None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_LOCAL_PIPELINE_MODULE_NAMES" in targets:
            names = ast.literal_eval(node.value)
        elif "_LOCAL_PIPELINE_PAYLOADS" in targets:
            payloads = ast.literal_eval(node.value)
    return names, payloads


def replace_payload_literal(source: str, new_literal: str) -> str:
    """Swaps the existing `_LOCAL_PIPELINE_PAYLOADS = {...}` block for
    new_literal, matching braces at column 0 so nested dict braces are safe."""
    start = source.index(PAYLOAD_START_MARKER)
    # The literal ends at the first line that is exactly "}" at column 0.
    rest = source[start:]
    end_rel = None
    offset = 0
    for line in rest.splitlines(keepends=True):
        if line.rstrip("\n") == "}" and offset > 0:
            end_rel = offset + len(line.rstrip("\n"))
            break
        offset += len(line)
    if end_rel is None:
        raise SystemExit("HATA: _LOCAL_PIPELINE_PAYLOADS literalinin sonu bulunamadi.")
    return source[:start] + new_literal + rest[end_rel:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="sadece dogrula, yazma (senkron disi varsa exit 1)")
    args = parser.parse_args()

    notebooks = sorted(NOTEBOOK_DIR.glob("*.ipynb"))
    if not notebooks:
        raise SystemExit(f"HATA: notebook bulunamadi: {NOTEBOOK_DIR}")

    out_of_sync, refreshed, skipped = [], [], []

    for nb_path in notebooks:
        nb = json.loads(nb_path.read_text(encoding="utf-8"))
        cell_idx = find_bootstrap_cell(nb)
        if cell_idx is None:
            skipped.append(nb_path.name)
            continue

        source = "".join(nb["cells"][cell_idx]["source"])
        module_names, payloads = parse_embedded(source)
        if not module_names:
            raise SystemExit(f"HATA: {nb_path.name}: _LOCAL_PIPELINE_MODULE_NAMES okunamadi.")

        stale = []
        for name in module_names:
            _, canonical = module_source_and_hash(name)
            entry = (payloads or {}).get(name, {})
            recorded = entry.get("sha256")
            if recorded != canonical or entry.get("encoding") != "zlib+b85":
                stale.append(f"{name} ({str(recorded)[:12]}... -> {canonical[:12]}...)")

        if not stale:
            print(f"  {nb_path.name}: senkron ({', '.join(module_names)})")
            continue

        out_of_sync.append(nb_path.name)
        if args.check:
            print(f"  {nb_path.name}: SENKRON DISI -> {'; '.join(stale)}")
            continue

        new_source = replace_payload_literal(source, render_payload_literal(list(module_names)))
        nb["cells"][cell_idx]["source"] = new_source.splitlines(keepends=True)
        nb_path.write_text(
            json.dumps(nb, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
        )
        refreshed.append(nb_path.name)
        print(f"  {nb_path.name}: YENILENDI -> {'; '.join(stale)}")

    print()
    if skipped:
        print(f"Atlandi (gomulu payload yok): {', '.join(skipped)}")
    if args.check:
        if out_of_sync:
            print(f"SENKRON DISI ({len(out_of_sync)}): {', '.join(out_of_sync)}")
            print("Duzeltme: python tools/refresh_embedded_payloads.py")
            return 1
        print("TUM GOMULU PAYLOADLAR SENKRON")
        return 0
    if refreshed:
        print(f"Yenilendi ({len(refreshed)}): {', '.join(refreshed)}")
        print("Simdi 'python verify_local.py' calistirip dogrula.")
    else:
        print("Yenilenecek bir sey yoktu; tum payloadlar zaten senkron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
