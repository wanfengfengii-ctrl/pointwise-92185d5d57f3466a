#!/usr/bin/env python3
"""Prepare the persistent demo workspace used by the runtime checklist."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenario import prepare_demo  # noqa: E402


def main() -> int:
    info = prepare_demo()
    print(
        json.dumps(
            {
                "prepared": True,
                "workspace": str(info["dir"]),
                "ledger": str(info["db"]),
                "volume": str(info["volume_path"]),
                "next": "Run demo.py to return, remove, and reclaim offline.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
