#!/usr/bin/env python3
"""Contract regression checks. Exits non-zero on the first failed requirement."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenario import json_output, make_k_volume, prepare_k_scenario, run_station  # noqa: E402


def expect_fail(db: Path, op: str, params: Any, label: str) -> None:
    before = db.read_bytes() if db.exists() else None
    result = run_station(db, op, params, check=False)
    if result.returncode == 0:
        raise AssertionError(f"{label}: expected failure but got {result.stdout}")
    after = db.read_bytes() if db.exists() else None
    if before != after:
        raise AssertionError(f"{label}: failed operation changed the ledger")


def check_canonical_offline_story() -> None:
    with tempfile.TemporaryDirectory(prefix="pointwise-verify-") as td:
        root = Path(td)
        info = prepare_k_scenario(root, issue_a=True)
        db = info["db"]

        # Issue idempotency / conflict for the first class of order numbers.
        receipt_a_2 = json_output(run_station(db, "签发", ["ISS-A", "A"]))
        if receipt_a_2 != info["receipt_a"]:
            raise AssertionError("same issue parameters did not return the same receipt")
        expect_fail(db, "签发", ["ISS-A", "B"], "issue order parameter conflict")

        # A simulated failed return is not accounted and leaves the volume readable.
        before = db.read_bytes()
        failed = run_station(db, "归还", ["V", "RET-FAIL", True], check=False)
        if failed.returncode == 0:
            raise AssertionError("simulated pre-commit failure unexpectedly succeeded")
        if db.read_bytes() != before:
            raise AssertionError("simulated failed return changed the ledger")
        probe = json_output(run_station(db, "冻结", ["V", "PROBE-INDEP", [["K", 0, 0]], True]))
        if not probe.get("prepared_local_material"):
            raise AssertionError("volume was not readable after simulated return failure")

        # Real return gives a permit; same params replay, different volume conflicts.
        permit = json_output(run_station(db, "归还", ["V", "RET-1"]))
        if not permit.get("permit_id") or permit.get("volume") != "V":
            raise AssertionError("return did not give an unmount permit")
        permit_replay = json_output(run_station(db, "归还", ["V", "RET-1"]))
        if permit_replay != permit:
            raise AssertionError("same return parameters did not return the same permit")
        verified = json_output(run_station(db, "核验", [permit]))
        if not verified.get("verified") or verified.get("volume") != "V":
            raise AssertionError("return permit did not verify")
        expect_fail(db, "核验", ["V", "RET-MISSING"], "unknown return verification")
        expect_fail(db, "归还", ["OTHER", "RET-1"], "return order volume conflict")
        expect_fail(db, "归还", ["V", "RET-OTHER", True], "second simulated failure")

        # Physically remove the original volume and reopen via fresh subprocesses.
        moved = root / "removed-volume.jsonl"
        shutil.move(str(info["volume_path"]), moved)

        got_a = json_output(run_station(db, "领取", [info["receipt_a"]]))
        expected_a = [
            "A",
            [
                ["V", "K", 0, 8],
                ["V", "K", 1, None],
            ],
        ]
        if got_a != [expected_a, 8]:
            raise AssertionError(f"A offline read mismatch: {json.dumps(got_a, ensure_ascii=False)}")

        # Candidate forms accepted by the public contract.
        run_station(db, "领取", [info["receipt_a"], got_a])
        run_station(db, "领取", [info["receipt_a"], expected_a])
        run_station(db, "领取", [info["receipt_a"], [["K", 1, None], ["K", 0, 8]]])
        run_station(db, "领取", [info["receipt_a"], [8, None]])
        expect_fail(db, "领取", [info["receipt_a"], [7, None]], "candidate value mismatch")
        expect_fail(db, "领取", [info["receipt_a"], [8]], "candidate missing cell")
        expect_fail(db, "领取", [info["receipt_a"], [["K", 1, 8], ["K", 0, 8]]], "candidate wrong value/binding")
        expect_fail(db, "领取", [info["receipt_a"], [["K", 9, 8], ["K", 1, None]]], "candidate out of receipt binding")
        expect_fail(db, "领取", [info["receipt_a"], ["B", [["V", "K", 0, 8], ["V", "K", 1, None]]]], "candidate revision binding")

        # B was independently prepared, so it can be first signed and read offline.
        receipt_b = json_output(run_station(db, "签发", ["ISS-B", "B"]))
        got_b = json_output(run_station(db, "领取", [receipt_b]))
        expected_b = ["B", [["V", "K", 1, None], ["V", "K", 2, 8]]]
        if got_b != [expected_b, 8]:
            raise AssertionError(f"B offline read mismatch: {json.dumps(got_b, ensure_ascii=False)}")

        # C was neither signed nor independent before permit effectiveness.
        expect_fail(db, "签发", ["ISS-C", "C"], "C first sign after return/removal")

    # If C was signed first, its signed revised value remains readable.
    with tempfile.TemporaryDirectory(prefix="pointwise-cfirst-") as td:
        root = Path(td)
        info = prepare_k_scenario(root, issue_a=False)
        db = info["db"]
        receipt_c = json_output(run_station(db, "签发", ["ISS-C", "C"]))
        run_station(db, "归还", ["V", "RET-C"])
        shutil.move(str(info["volume_path"]), root / "gone.jsonl")
        got_c = json_output(run_station(db, "领取", [receipt_c]))
        if got_c != [["C", [["V", "K", 3, 31]]], 31]:
            raise AssertionError(f"C signed-first read mismatch: {got_c}")


def check_receive_and_freeze_rejections() -> None:
    with tempfile.TemporaryDirectory(prefix="pointwise-invalid-") as td:
        root = Path(td)
        volume_path, manifest = make_k_volume(root)
        db = root / "ledger.json"

        bad_manifest = json.loads(json.dumps(manifest))
        bad_manifest[0][2] = "0" * 64
        expect_fail(db, "接卷", ["BAD", str(volume_path), bad_manifest], "bad line SHA256")
        if db.exists():
            raise AssertionError("failed receive created a ledger account file")

        run_station(db, "接卷", ["V", str(volume_path), manifest])
        expect_fail(db, "冻结", ["V", "BAD1", [["K", 4, 0]], False], "cell out of bounds")
        expect_fail(db, "冻结", ["V", "BAD2", [["K", 0, 0], ["K", 0, 1]], False], "duplicate cell")
        expect_fail(db, "冻结", ["V", "BAD3", [["NOPE", 0, 0]], False], "unknown block")
        expect_fail(db, "冻结", ["V", "BAD4", [["K", True, 0]], False], "boolean cell")
        expect_fail(db, "冻结", ["V", "BAD5", [["K", 0, False]], False], "boolean delta")

        # A failed freeze must not leave a revision that can later be issued.
        expect_fail(db, "签发", ["ISS-BAD", "BAD1"], "issue rejected freeze")


def check_concurrent_issue_and_return() -> None:
    with tempfile.TemporaryDirectory(prefix="pointwise-concurrent-") as td:
        root = Path(td)
        volume_path, manifest = make_k_volume(root)
        db = root / "ledger.json"
        run_station(db, "接卷", ["V", str(volume_path), manifest])

        # Every one of these has independent local material, so every issue is
        # allowed regardless of whether return commits first.
        for i in range(12):
            cell = i % 4
            run_station(
                db,
                "冻结",
                ["V", f"R{i}", [[ "K", cell, 0 ]], True],
            )

        def worker(i: int):
            if i == 0:
                return run_station(db, "归还", ["V", "RET-CONCURRENT"], check=False)
            return run_station(db, "签发", [f"ISS-{i}", f"R{i - 1}"], check=False)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(13)))

        if results[0].returncode != 0:
            raise AssertionError("concurrent return failed")
        for i, result in enumerate(results[1:], start=1):
            if result.returncode != 0:
                raise AssertionError(f"concurrent issue {i} failed: {result.stderr}")
            receipt = json.loads(result.stdout)
            collected = json_output(run_station(db, "领取", [receipt]))
            if not isinstance(collected, list) or len(collected) != 2:
                raise AssertionError("successful concurrent issue is not collectible")

        # Ledger itself must still be valid JSON and support idempotent replay.
        replay = json_output(run_station(db, "归还", ["V", "RET-CONCURRENT"]))
        if not replay.get("permit_id"):
            raise AssertionError("concurrent return replay failed")


def check_concurrent_nonindependent_race() -> None:
    with tempfile.TemporaryDirectory(prefix="pointwise-race-") as td:
        root = Path(td)
        count = 16
        values = list(range(count))
        line = json.dumps(values, separators=(",", ":"))
        volume_path = root / "race.jsonl"
        volume_path.write_text(line + "\n", encoding="utf-8")
        digest = hashlib.sha256(line.encode()).hexdigest()
        db = root / "ledger.json"
        run_station(db, "接卷", ["V", str(volume_path), [["K", count, digest]]])
        for i in range(count):
            run_station(db, "冻结", ["V", f"N{i}", [[ "K", i, 0 ]], False])

        def issue(i: int):
            return run_station(db, "签发", [f"RACE-ISS-{i}", f"N{i}"], check=False)

        with ThreadPoolExecutor(max_workers=9) as pool:
            issue_futures = [pool.submit(issue, i) for i in range(count)]
            return_future = pool.submit(run_station, db, "归还", ["V", "RACE-RET"], False)
            results = [f.result() for f in issue_futures]
            return_result = return_future.result()

        if return_result.returncode != 0:
            raise AssertionError(f"race return failed: {return_result.stderr}")
        successes = 0
        for i, result in enumerate(results):
            if result.returncode == 0:
                successes += 1
                receipt = json.loads(result.stdout)
                collected = json_output(run_station(db, "领取", [receipt]))
                if collected != [[f"N{i}", [["V", "K", i, i]]], i]:
                    raise AssertionError(f"successful racing issue {i} not collectible: {collected}")
            else:
                error = json.loads(result.stderr)
                if error.get("code") != "first_sign_without_independent_material":
                    raise AssertionError(f"unexpected racing issue failure: {result.stderr}")
        if successes == 0:
            raise AssertionError("race test unexpectedly had no successful issues")


def main() -> int:
    check_receive_and_freeze_rejections()
    check_canonical_offline_story()
    check_concurrent_issue_and_return()
    check_concurrent_nonindependent_race()
    print("verify: OK - receive, freeze rejection, return/permit, offline collect, candidates, idempotency, concurrency")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
