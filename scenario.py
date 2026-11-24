#!/usr/bin/env python3
"""Shared fixtures for local verification and demonstration."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATION = ROOT / "station.py"


def line_digest(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def make_k_volume(directory: Path, volume: Any = "V", block: Any = "K") -> tuple[Path, list[list[Any]]]:
    directory.mkdir(parents=True, exist_ok=True)
    values = [7, None, 7, 31]
    line = json.dumps(values, separators=(",", ":"))
    volume_path = directory / f"{volume}-{block}.jsonl"
    volume_path.write_text(line + "\n", encoding="utf-8")
    manifest = [[block, len(values), line_digest(line)]]
    return volume_path, manifest


def run_station(db: Path | str, operation: str, params: Any, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-I",
        "-B",
        str(STATION),
        str(db),
        operation,
        json.dumps(params, ensure_ascii=False),
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise AssertionError(
            f"station {operation} failed ({result.returncode})\n"
            f"params={json.dumps(params, ensure_ascii=False)}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def json_output(result: subprocess.CompletedProcess[str]) -> Any:
    return json.loads(result.stdout)


def prepare_k_scenario(
    directory: Path,
    *,
    issue_a: bool = True,
    independent_c: bool = False,
) -> dict[str, Any]:
    """Create the canonical K/A/B/C scenario before return."""
    directory.mkdir(parents=True, exist_ok=True)
    volume_path, manifest = make_k_volume(directory)
    db = directory / "ledger.json"

    run_station(db, "接卷", ["V", str(volume_path), manifest])
    run_station(db, "冻结", ["V", "A", [["K", 0, 1], ["K", 1, 0]], False])
    run_station(db, "冻结", ["V", "B", [["K", 1, 0], ["K", 2, 1]], True])
    run_station(db, "冻结", ["V", "C", [["K", 3, 0]], independent_c])

    receipt_a = None
    if issue_a:
        receipt_a = json_output(run_station(db, "签发", ["ISS-A", "A"]))
    return {"dir": directory, "db": db, "volume_path": volume_path, "manifest": manifest, "receipt_a": receipt_a}


def prepare_demo() -> dict[str, Any]:
    demo_dir = ROOT / "demo-workspace"
    if demo_dir.exists():
        shutil.rmtree(demo_dir)
    return prepare_k_scenario(demo_dir, issue_a=True)


if __name__ == "__main__":
    info = prepare_demo()
    print(
        json.dumps(
            {
                "prepared": True,
                "workspace": str(info["dir"]),
                "ledger": str(info["db"]),
                "volume": str(info["volume_path"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
