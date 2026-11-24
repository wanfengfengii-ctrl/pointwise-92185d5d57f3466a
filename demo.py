#!/usr/bin/env python3
"""Human-readable demo: return the volume, remove it, then reclaim offline."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenario import prepare_demo, run_station  # noqa: E402


def show(title: str, argv: list[str], result_stdout: str | None = None) -> None:
    print(f"\n== {title} ==")
    print("argv: " + json.dumps(argv, ensure_ascii=False))
    if result_stdout is not None:
        try:
            print(json.dumps(json.loads(result_stdout), ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            print(result_stdout)


def main() -> int:
    info = prepare_demo()
    db = info["db"]
    volume_path = info["volume_path"]

    print("Prepared K = [7, null, 7, 31]")
    print("A -> cells 0,1; B -> cells 1,2 independent; C -> cell 3")
    print(f"ledger: {db}")

    receipt_a = info["receipt_a"]
    show("A already signed; receipt", ["station.py", str(db), "签发", '["ISS-A","A"]'], json.dumps(receipt_a, ensure_ascii=False))

    permit_result = run_station(db, "归还", ["V", "RETURN-DEMO"])
    permit = json.loads(permit_result.stdout)
    show("Return V and receive unmount permit", ["station.py", str(db), "归还", '["V","RETURN-DEMO"]'], permit_result.stdout)

    removed = info["dir"] / "original-volume-removed.jsonl"
    shutil.move(str(volume_path), removed)
    print(f"\nOriginal volume moved to: {removed}")
    print("Every following station.py call is a fresh process ('reopen').")

    a_result = run_station(db, "领取", [receipt_a])
    show("Reclaim A without the volume", ["station.py", str(db), "领取", "[receipt-a]"], a_result.stdout)

    issue_b = run_station(db, "签发", ["ISS-B", "B"])
    receipt_b = json.loads(issue_b.stdout)
    show("First sign B from its independent local material", ["station.py", str(db), "签发", '["ISS-B","B"]'], issue_b.stdout)
    b_result = run_station(db, "领取", [receipt_b])
    show("Reclaim B", ["station.py", str(db), "领取", "[receipt-b]"], b_result.stdout)

    c_failed = run_station(db, "签发", ["ISS-C", "C"], check=False)
    print("\n== C first sign is rejected after permit effectiveness ==")
    print("argv: " + json.dumps(["station.py", str(db), "签发", '["ISS-C","C"]'], ensure_ascii=False))
    print(f"exit code: {c_failed.returncode}")
    print(c_failed.stderr.strip())
    if c_failed.returncode == 0:
        return 1

    # Signed-first C remains available; show that invariant in a fresh fixture.
    from scenario import prepare_k_scenario

    c_dir = info["dir"] / "c-signed-first"
    c_info = prepare_k_scenario(c_dir, issue_a=False)
    c_db = c_info["db"]
    receipt_c = json.loads(run_station(c_db, "签发", ["ISS-C", "C"]).stdout)
    run_station(c_db, "归还", ["V", "RETURN-C"])
    shutil.move(str(c_info["volume_path"]), c_dir / "gone.jsonl")
    c_result = run_station(c_db, "领取", [receipt_c])
    show("If C was signed first, 31 remains reclaimable", ["station.py", str(c_db), "领取", "[receipt-c]"], c_result.stdout)

    # Final assertions on the main story.
    assert json.loads(a_result.stdout) == [["A", [["V", "K", 0, 8], ["V", "K", 1, None]]], 8]
    assert json.loads(b_result.stdout) == [["B", [["V", "K", 1, None], ["V", "K", 2, 8]]], 8]
    assert json.loads(c_result.stdout) == [["C", [["V", "K", 3, 31]]], 31]
    assert permit["permitted"] is True
    print("\ndemo: OK - unmount permit, physical volume removal, and offline reclaim all verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
