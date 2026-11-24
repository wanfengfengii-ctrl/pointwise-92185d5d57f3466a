#!/usr/bin/env python3
"""Pointwise tide-ledger CLI.

Usage: python3 station.py LEDGER OPERATION JSON_POSITIONAL_ARGS
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

RECEIPT_KIND = "pointwise-receipt-v1"
PERMIT_KIND = "pointwise-unmount-permit-v1"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


class BizError(Exception):
    """An expected contract failure; no ledger commit is performed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def strict_int(value: Any) -> int:
    if type(value) is not int:
        raise BizError("bad_integer", f"expected integer, got {type(value).__name__}")
    return value


def nonneg_int(value: Any, name: str) -> int:
    result = strict_int(value)
    if result < 0:
        raise BizError("bad_cell", f"{name} must be zero-based and non-negative")
    return result


def same_id(a: Any, b: Any) -> bool:
    # JSON true must not identify cell/block 1.
    return type(a) is type(b) and a == b


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def split_json_lines(raw: bytes) -> list[bytes]:
    lines = raw.split(b"\n")
    # A single terminal LF terminates the last block rather than creating one.
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def valid_cell_value(value: Any) -> bool:
    return value is None or type(value) is int


def normalize_manifest_entry(entry: Any, index: int) -> list[Any]:
    if isinstance(entry, dict):
        block = first_key(entry, ("块", "block", "block_id", "id"))
        count = first_key(entry, ("格数", "cell_count", "cellCount", "cells", "count", "n", "size"))
        line_hash = first_key(entry, ("行SHA256", "line_sha256", "sha256", "sha256_hex", "hash", "digest"))
    elif isinstance(entry, (list, tuple)) and len(entry) == 3:
        block, count, line_hash = entry
    else:
        raise BizError("bad_manifest", f"manifest entry {index} must be [block, cell_count, sha256]")

    count = nonneg_int(count, "cell count")
    if not isinstance(line_hash, str) or len(line_hash.strip()) != 64:
        raise BizError("bad_manifest", f"manifest entry {index} has invalid SHA256")
    try:
        int(line_hash.strip(), 16)
    except ValueError as exc:
        raise BizError("bad_manifest", f"manifest entry {index} has non-hex SHA256") from exc
    return [block, count, line_hash.strip().lower()]


def first_key(obj: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in obj:
            return obj[key]
    raise BizError("bad_params", f"missing one of keys: {', '.join(keys)}")


UNSET = object()


def param_at(params: Any, index: int, keys: tuple[str, ...], default: Any = UNSET) -> Any:
    if isinstance(params, (list, tuple)):
        if index < len(params):
            return params[index]
    elif isinstance(params, dict):
        for key in keys:
            if key in params:
                return params[key]
    if default is UNSET:
        raise BizError("bad_params", f"missing parameter {index + 1} ({keys[0]})")
    return default


def as_bool(value: Any, name: str) -> bool:
    if type(value) is bool:
        return value
    if value is None:
        return False
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in {"1", "true", "yes", "是"}:
        return True
    if isinstance(value, str) and value.lower() in {"0", "false", "no", "否"}:
        return False
    raise BizError("bad_boolean", f"{name} must be a JSON boolean")


def load_manifest(value: Any) -> list[Any]:
    if isinstance(value, str):
        p = Path(value)
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(value, (list, tuple)):
        raise BizError("bad_manifest", "manifest must be an array")
    return [normalize_manifest_entry(entry, i) for i, entry in enumerate(value)]


def resolve_volume_path(path_value: Any, volume: Any) -> Path:
    if not isinstance(path_value, (str, os.PathLike)):
        raise BizError("bad_path", "volume path must be a string")
    p = Path(path_value)
    if p.is_file():
        return p
    if p.is_dir():
        candidates = [
            p / str(volume),
            p / f"{volume}.jsonl",
            p / f"{volume}.ndjson",
            p / f"{volume}.json",
            p / "volume.jsonl",
            p / "blocks.jsonl",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    raise BizError("volume_unavailable", f"volume source is not a readable file: {path_value}")


def verify_source(path: Path, manifest: list[Any]) -> list[list[Any]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BizError("volume_unreadable", str(exc)) from exc

    lines = split_json_lines(raw)
    if len(lines) != len(manifest):
        raise BizError(
            "manifest_line_count",
            f"manifest has {len(manifest)} lines, volume has {len(lines)}",
        )

    arrays: list[list[Any]] = []
    seen_blocks: set[str] = set()
    for i, (line, entry) in enumerate(zip(lines, manifest)):
        block, count, expected_hash = entry
        candidates = [line]
        if line.endswith(b"\r"):
            candidates.append(line[:-1])
        if not any(sha256_hex(candidate) == expected_hash for candidate in candidates):
            raise BizError("line_hash_mismatch", f"SHA256 mismatch on manifest line {i + 1}")

        try:
            arr = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BizError("bad_block_json", f"block {block!r} is not a UTF-8 JSON array") from exc
        if not isinstance(arr, list):
            raise BizError("bad_block", f"block {block!r} must be a JSON array")
        if len(arr) != count:
            raise BizError("cell_count_mismatch", f"block {block!r} expected {count} cells, got {len(arr)}")
        for cell_index, item in enumerate(arr):
            if not valid_cell_value(item):
                raise BizError("bad_cell_value", f"block {block!r} cell {cell_index} is not integer or null")

        block_key = canonical(block)
        if block_key in seen_blocks:
            raise BizError("duplicate_block", f"duplicate block {block!r} in manifest")
        seen_blocks.add(block_key)
        arrays.append(arr)
    return arrays


def empty_state() -> dict[str, Any]:
    return {"version": 1, "volumes": [], "revisions": [], "issue_orders": [], "return_orders": []}


def state_paths(db_arg: str) -> tuple[Path, Path]:
    p = Path(db_arg)
    if p.exists() and p.is_dir():
        state_path = p / "station-ledger.json"
    else:
        state_path = p
    lock_path = state_path.with_name(state_path.name + ".lock")
    return state_path, lock_path


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BizError("bad_ledger", f"cannot read ledger {path}: {exc}") from exc
    if not isinstance(state, dict):
        raise BizError("bad_ledger", "ledger root must be an object")
    base = empty_state()
    for key, default_value in base.items():
        state.setdefault(key, default_value if not isinstance(default_value, list) else [])
    return state


def save_state_atomic(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".station-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class Ledger:
    def __init__(self, state: dict[str, Any]):
        self.state = state
        self.changed = False

    def volume(self, volume_id: Any) -> dict[str, Any]:
        for record in self.state["volumes"]:
            if same_id(record["volume"], volume_id):
                return record
        raise BizError("unknown_volume", f"volume {volume_id!r} has not been received")

    def maybe_volume(self, volume_id: Any) -> dict[str, Any] | None:
        try:
            return self.volume(volume_id)
        except BizError:
            return None

    def revision_in_volume(self, volume_id: Any, revision_id: Any) -> dict[str, Any]:
        for record in self.state["revisions"]:
            if same_id(record["volume"], volume_id) and same_id(record["revision"], revision_id):
                return record
        raise BizError("unknown_revision", f"revision {revision_id!r} not frozen on volume {volume_id!r}")

    def revision(self, revision_id: Any, volume_id: Any = ...) -> dict[str, Any]:
        matches = []
        for record in self.state["revisions"]:
            if same_id(record["revision"], revision_id):
                if volume_id is ... or same_id(record["volume"], volume_id):
                    matches.append(record)
        if not matches:
            raise BizError("unknown_revision", f"revision {revision_id!r} has not been frozen")
        if len(matches) > 1:
            raise BizError("ambiguous_revision", f"revision {revision_id!r} exists on multiple volumes")
        return matches[0]

    def issue_order(self, order_no: Any) -> dict[str, Any] | None:
        for record in self.state["issue_orders"]:
            if same_id(record["no"], order_no):
                return record
        return None

    def return_order(self, order_no: Any) -> dict[str, Any] | None:
        for record in self.state["return_orders"]:
            if same_id(record["no"], order_no):
                return record
        return None


def read_current_block(record: dict[str, Any], block_id: Any) -> list[Any]:
    manifest = record["manifest"]
    entry = None
    line_index = None
    for i, item in enumerate(manifest):
        if same_id(item[0], block_id):
            entry = item
            line_index = i
            break
    if entry is None:
        raise BizError("unknown_block", f"block {block_id!r} is not in the manifest")

    source = Path(record.get("source_real") or record["source"])
    if not source.exists():
        source = Path(record["source"])
    path = resolve_volume_path(source, record["volume"]) if source.exists() or source.is_dir() else None
    if path is None:
        raise BizError("volume_offline", "original volume is not available")

    try:
        raw = path.read_bytes()
        lines = split_json_lines(raw)
        line = lines[line_index]
    except (OSError, IndexError) as exc:
        raise BizError("volume_unreadable", "required block line cannot be read") from exc

    expected_hash = entry[2]
    candidates = [line]
    if line.endswith(b"\r"):
        candidates.append(line[:-1])
    if not any(sha256_hex(candidate) == expected_hash for candidate in candidates):
        raise BizError("line_hash_mismatch", "volume changed after verification; refusing read")
    try:
        arr = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BizError("bad_block_json", "verified line is not valid JSON") from exc
    if not isinstance(arr, list):
        raise BizError("bad_block", "verified block is not an array")
    return arr


def material_from_values(revision_record: dict[str, Any], values: list[Any]) -> list[Any]:
    entries = []
    for selection, value in zip(revision_record["selections"], values):
        block_id, cell, _delta = selection
        entries.append([revision_record["volume"], block_id, cell, value])
    return [revision_record["revision"], entries]


def compute_material(record: dict[str, Any], revision_record: dict[str, Any]) -> tuple[list[Any], int]:
    values: list[Any] = []
    cache: dict[str, list[Any]] = {}
    for block_id, cell, delta in revision_record["selections"]:
        key = canonical(block_id)
        if key not in cache:
            cache[key] = read_current_block(record, block_id)
        arr = cache[key]
        original = arr[cell]
        if original is None:
            value = None
        else:
            value = strict_int(original) + strict_int(delta)
        values.append(value)
    material = material_from_values(revision_record, values)
    total = sum(entry[3] for entry in material[1] if entry[3] is not None)
    return material, total


def stored_material(revision_record: dict[str, Any]) -> tuple[list[Any], int]:
    material = revision_record.get("material")
    total = revision_record.get("total")
    if not isinstance(material, list) or len(material) != 2:
        raise BizError("missing_local_material", "no signed or independent local material")
    if type(total) is not int:
        total = sum(entry[3] for entry in material[1] if entry[3] is not None)
    return material, total


def make_receipt(order_no: Any, revision_record: dict[str, Any], material: list[Any], total: int) -> dict[str, Any]:
    material_digest = digest([material, total])
    receipt_id = "RCPT-" + hashlib.sha256(
        f"{canonical([order_no, revision_record['volume'], revision_record['revision'], material_digest])}".encode()
    ).hexdigest()[:24]
    return {
        "kind": RECEIPT_KIND,
        "receipt": receipt_id,
        "receipt_id": receipt_id,
        "order": order_no,
        "order_no": order_no,
        "单号": order_no,
        "volume": revision_record["volume"],
        "卷": revision_record["volume"],
        "revision": revision_record["revision"],
        "修订": revision_record["revision"],
        "digest": material_digest,
        "receipt_token": receipt_id,
        "回执": receipt_id,
        "material": material,
        "材料": material,
        "total": total,
        "合计": total,
    }


def make_permit(volume_id: Any, return_no: Any) -> dict[str, Any]:
    permit_id = "PERM-" + hashlib.sha256(canonical([PERMIT_KIND, volume_id, return_no]).encode()).hexdigest()[:24]
    return {
        "kind": PERMIT_KIND,
        "permit": permit_id,
        "permit_id": permit_id,
        "permit_token": permit_id,
        "卸卷许可": permit_id,
        "许可": permit_id,
        "return_no": return_no,
        "单号": return_no,
        "volume": volume_id,
        "卷": volume_id,
        "permitted": True,
        "许可生效": True,
    }


def op_receive(ledger: Ledger, params: Any) -> Any:
    volume_id = param_at(params, 0, ("卷", "volume", "vol"))
    source_value = param_at(params, 1, ("路径", "path", "source"))
    manifest_value = param_at(params, 2, ("清册", "manifest", "inventory"))
    manifest = load_manifest(manifest_value)
    source = resolve_volume_path(source_value, volume_id)
    verify_source(source, manifest)
    real_source = str(source.resolve())

    existing = ledger.maybe_volume(volume_id)
    if existing is not None:
        if canonical(existing["manifest"]) != canonical(manifest):
            raise BizError("volume_conflict", "volume ID already received with a different inventory")
        if existing.get("source_real") != real_source and existing.get("source") != str(source_value):
            raise BizError("volume_conflict", "volume ID already received from a different source")
        return {
            "ok": True,
            "op": "receive",
            "volume": volume_id,
            "卷": volume_id,
            "received": True,
            "blocks": len(manifest),
            "idempotent": True,
        }

    ledger.state["volumes"].append(
        {
            "volume": volume_id,
            "source": str(source_value),
            "source_real": real_source,
            "manifest": manifest,
            "returned": False,
            "return_no": None,
            "permit": None,
        }
    )
    ledger.changed = True
    return {
        "ok": True,
        "op": "receive",
        "volume": volume_id,
        "卷": volume_id,
        "received": True,
        "blocks": len(manifest),
    }


def normalize_selection(raw: Any, index: int) -> list[Any]:
    if isinstance(raw, dict):
        block = first_key(raw, ("块", "block", "b"))
        cell = first_key(raw, ("零基格", "格", "cell", "index"))
        delta = first_key(raw, ("整数差", "差", "delta", "diff"))
    elif isinstance(raw, (list, tuple)) and len(raw) == 3:
        block, cell, delta = raw
    else:
        raise BizError("bad_selection", f"selection {index} must be [block, zero_based_cell, integer_delta]")
    cell = nonneg_int(cell, "cell")
    delta = strict_int(delta)
    return [block, cell, delta]


def op_freeze(ledger: Ledger, params: Any) -> Any:
    volume_id = param_at(params, 0, ("卷", "volume", "vol"))
    revision_id = param_at(params, 1, ("修订", "revision", "rev"))
    raw_selections = param_at(params, 2, ("选格", "selections", "cells"))
    independent = as_bool(param_at(params, 3, ("独立", "independent"), False), "independent")

    volume_record = ledger.volume(volume_id)
    if not isinstance(raw_selections, (list, tuple)):
        raise BizError("bad_selections", "selected cells must be an array")

    # Compatibility for a formulation where 修订 is the [block,cell,delta]
    # correction table and 选格 is the ordered list of bare positions/pairs.
    if (
        isinstance(revision_id, list)
        and revision_id
        and all(isinstance(item, (list, tuple)) and len(item) == 3 for item in revision_id)
        and raw_selections
        and not all(isinstance(item, (list, tuple)) and len(item) == 3 for item in raw_selections)
    ):
        delta_map: dict[tuple[str, int], int] = {}
        for delta_entry in revision_id:
            block_id, cell, delta = delta_entry
            cell = nonneg_int(cell, "cell")
            delta = strict_int(delta)
            key = (canonical(block_id), cell)
            if key in delta_map and delta_map[key] != delta:
                raise BizError("duplicate_cell", "conflicting duplicate revision difference")
            delta_map[key] = delta

        sole_block = volume_record["manifest"][0][0] if len(volume_record["manifest"]) == 1 else None
        merged: list[list[Any]] = []
        for selected in raw_selections:
            if type(selected) is int:
                if sole_block is None:
                    raise BizError("bad_selection", "bare cell indexes require a single-block volume")
                block_id, cell = sole_block, selected
            elif isinstance(selected, (list, tuple)) and len(selected) == 2:
                block_id, cell = selected
            elif isinstance(selected, dict):
                block_id = first_key(selected, ("块", "block", "b"))
                cell = first_key(selected, ("零基格", "格", "cell", "index"))
            else:
                raise BizError("bad_selection", "candidate selection must be a cell, [block,cell], or [block,cell,delta]")
            cell = nonneg_int(cell, "cell")
            merged.append([block_id, cell, delta_map.get((canonical(block_id), cell), 0)])
        raw_selections = merged

    selections = [normalize_selection(item, i) for i, item in enumerate(raw_selections)]

    block_counts: dict[str, int] = {}
    for entry in volume_record["manifest"]:
        block_counts[canonical(entry[0])] = entry[1]

    seen: set[tuple[str, int]] = set()
    for block_id, cell, _delta in selections:
        block_key = canonical(block_id)
        if block_key not in block_counts:
            raise BizError("unknown_block", f"block {block_id!r} is not in inventory")
        if cell >= block_counts[block_key]:
            raise BizError("cell_out_of_bounds", f"block {block_id!r} has no zero-based cell {cell}")
        coordinate = (block_key, cell)
        if coordinate in seen:
            raise BizError("duplicate_cell", f"duplicate selected cell [{canonical(block_id)}, {cell}]")
        seen.add(coordinate)

    existing = None
    for record in ledger.state["revisions"]:
        if same_id(record["volume"], volume_id) and same_id(record["revision"], revision_id):
            existing = record
            break

    if existing is not None:
        if canonical(existing["selections"]) != canonical(selections):
            raise BizError("revision_conflict", "revision already frozen with different selected cells")
        if bool(existing.get("independent")) != independent:
            raise BizError("revision_conflict", "revision independence flag cannot change after freeze")
        return {
            "ok": True,
            "op": "freeze",
            "volume": volume_id,
            "revision": revision_id,
            "修订": revision_id,
            "cells": len(selections),
            "independent": bool(existing.get("independent")),
            "idempotent": not ledger.changed,
        }

    revision_record: dict[str, Any] = {
        "volume": volume_id,
        "revision": revision_id,
        "selections": selections,
        "independent": independent,
        "signed": False,
        "material": None,
        "total": None,
    }
    if independent:
        material, total = compute_material(volume_record, revision_record)
        revision_record["material"] = material
        revision_record["total"] = total

    ledger.state["revisions"].append(revision_record)
    ledger.changed = True
    return {
        "ok": True,
        "op": "freeze",
        "volume": volume_id,
        "卷": volume_id,
        "revision": revision_id,
        "修订": revision_id,
        "cells": len(selections),
        "independent": independent,
        "prepared_local_material": independent,
    }


def ensure_issue_material(ledger: Ledger, revision_record: dict[str, Any]) -> tuple[list[Any], int]:
    if revision_record.get("material") is not None:
        return stored_material(revision_record)

    volume_record = ledger.volume(revision_record["volume"])
    if volume_record.get("returned"):
        raise BizError(
            "first_sign_without_independent_material",
            "permit is effective and this spare has no independent local material",
        )

    material, total = compute_material(volume_record, revision_record)
    revision_record["material"] = material
    revision_record["total"] = total
    revision_record["signed"] = True
    ledger.changed = True
    return material, total


def op_issue(ledger: Ledger, params: Any) -> Any:
    order_no = param_at(params, 0, ("单号", "order", "order_no", "no"))
    revision_id = param_at(params, 1, ("修订", "revision", "rev"))
    # Tolerate an explicit volume in third position or object form.
    volume_id = param_at(params, 2, ("卷", "volume", "vol"), ...)

    existing_order = ledger.issue_order(order_no)
    if existing_order is not None:
        if not same_id(existing_order["revision"], revision_id):
            raise BizError("order_conflict", "issue order number already used for a different revision")
        if volume_id is not ... and not same_id(existing_order.get("volume"), volume_id):
            raise BizError("order_conflict", "issue order number already bound to a different volume")
        return existing_order["receipt"]

    if volume_id is ...:
        revision_record = ledger.revision(revision_id)
    else:
        revision_record = ledger.revision_in_volume(volume_id, revision_id)

    material, total = ensure_issue_material(ledger, revision_record)
    revision_record["signed"] = True
    receipt = make_receipt(order_no, revision_record, material, total)
    ledger.state["issue_orders"].append(
        {
            "no": order_no,
            "revision": revision_id,
            "volume": revision_record["volume"],
            "receipt": receipt,
        }
    )
    ledger.changed = True
    return receipt


def op_return_volume(ledger: Ledger, params: Any) -> Any:
    volume_id = param_at(params, 0, ("卷", "volume", "vol"))
    return_no = param_at(params, 1, ("单号", "order", "order_no", "no"))
    simulate_failure = as_bool(param_at(params, 2, ("试败", "simulate_failure", "fail"), False), "simulate_failure")

    volume_record = ledger.volume(volume_id)
    existing_order = ledger.return_order(return_no)
    if existing_order is not None and not same_id(existing_order["volume"], volume_id):
        raise BizError("return_order_conflict", "return order number already used for a different volume")

    # A trial failure is deliberately not recorded for a new order number.
    if simulate_failure:
        raise BizError("simulated_precommit_failure", "simulated failure before commit; ledger unchanged")

    if existing_order is not None:
        return existing_order["permit"]

    permit = make_permit(volume_id, return_no)
    volume_record["returned"] = True
    volume_record["return_no"] = return_no
    volume_record["permit"] = permit
    ledger.state["return_orders"].append({"no": return_no, "volume": volume_id, "permit": permit})
    ledger.changed = True
    return permit


def op_verify_return(ledger: Ledger, params: Any) -> dict[str, Any]:
    """Check a return order/permit; useful to the runtime verify command."""
    if isinstance(params, dict):
        permit = params.get("permit") or params.get("许可") or params
        return_no = param_at(params, 0, ("单号", "return_no", "no", "permit_id"), None)
        volume_id = param_at(params, 1, ("卷", "volume", "vol"), None)
    elif isinstance(params, (list, tuple)) and params and isinstance(params[0], dict) and params[0].get("kind") == PERMIT_KIND:
        permit = params[0]
        return_no = permit.get("return_no") or permit.get("单号")
        volume_id = permit.get("volume") or permit.get("卷")
    else:
        volume_id = param_at(params, 0, ("卷", "volume", "vol"), None)
        return_no = param_at(params, 1, ("单号", "return_no", "no"), None)
        permit = param_at(params, 2, ("许可", "permit"), None)

    order = None
    if return_no is not None:
        order = ledger.return_order(return_no)
        if order is None and isinstance(return_no, str) and return_no.startswith("PERM-"):
            for candidate in ledger.state["return_orders"]:
                if candidate["permit"].get("permit_id") == return_no:
                    order = candidate
                    break
    if order is None and isinstance(permit, dict):
        token = permit.get("permit_id") or permit.get("permit")
        for candidate in ledger.state["return_orders"]:
            if candidate["permit"].get("permit_id") == token:
                order = candidate
                break
    if order is None:
        raise BizError("return_not_verified", "return order/permit was not found")
    if volume_id is not None and not same_id(order["volume"], volume_id):
        raise BizError("return_volume_conflict", "permit is bound to another volume")
    volume_record = ledger.maybe_volume(order["volume"])
    if volume_record is None or not volume_record.get("returned"):
        raise BizError("return_not_effective", "volume return is not effective")
    return {
        "ok": True,
        "verified": True,
        "returned": True,
        "volume": order["volume"],
        "卷": order["volume"],
        "return_no": order["no"],
        "单号": order["no"],
        "permit": order["permit"],
    }


def find_issue_by_receipt(ledger: Ledger, receipt: Any) -> dict[str, Any]:
    if isinstance(receipt, dict):
        for key in ("order", "order_no", "单号"):
            if key in receipt:
                order = ledger.issue_order(receipt[key])
                if order is not None:
                    if "digest" in receipt and isinstance(order.get("receipt"), dict):
                        if not same_id(receipt.get("digest"), order["receipt"].get("digest")):
                            raise BizError("bad_receipt", "receipt digest does not match ledger")
                    return order
        for key in ("receipt_id", "receipt", "receipt_token", "回执"):
            token = receipt.get(key)
            if isinstance(token, str):
                for order in ledger.state["issue_orders"]:
                    stored_receipt = order["receipt"]
                    if stored_receipt.get("receipt_id") == token or stored_receipt.get("回执") == token:
                        return order
            elif isinstance(token, dict):
                nested = find_issue_by_receipt(ledger, token)
                if nested is not None:
                    return nested
        for key in ("result", "data"):
            nested = receipt.get(key)
            if isinstance(nested, dict):
                return find_issue_by_receipt(ledger, nested)
    elif isinstance(receipt, (str, int)):
        if isinstance(receipt, str) and receipt[:1] in {"{", "["}:
            try:
                nested = json.loads(receipt)
                return find_issue_by_receipt(ledger, nested)
            except (json.JSONDecodeError, BizError):
                pass
        order = ledger.issue_order(receipt)
        if order is not None:
            return order
        for record in ledger.state["issue_orders"]:
            if record["receipt"].get("receipt_id") == receipt:
                return record
    elif isinstance(receipt, (list, tuple)):
        # [kind, order, revision, digest?] or [order, revision]
        if receipt and receipt[0] == RECEIPT_KIND and len(receipt) >= 2:
            order = ledger.issue_order(receipt[1])
            if order is not None:
                return order
        if len(receipt) >= 2:
            order = ledger.issue_order(receipt[0])
            if order is not None and same_id(order["revision"], receipt[1]):
                return order

        # A full collect value [material, total] or a material [revision, entries]
        # can identify the signed result when replayed by another local caller.
        for record in ledger.state["issue_orders"]:
            receipt_material = record["receipt"].get("material") if isinstance(record.get("receipt"), dict) else None
            if receipt_material is None:
                continue
            if canonical(receipt) == canonical([receipt_material, record["receipt"].get("total")]):
                return record
            if canonical(receipt) == canonical(receipt_material):
                return record
            if len(receipt) >= 1 and canonical(receipt[0]) == canonical(receipt_material):
                return record
            if len(receipt) == 2 and same_id(receipt[0], record.get("revision")) and isinstance(receipt[1], list):
                return record
    raise BizError("bad_receipt", "receipt is not bound to a signed issue order")


def candidate_entries(candidate: Any) -> tuple[Any, list[dict[str, Any]], Any]:
    """Return optional revision, coordinate entries, and optional total."""
    if isinstance(candidate, dict):
        total = None
        for key in ("total", "合计"):
            if key in candidate:
                total = candidate[key]
                break
        revision = None
        for key in ("revision", "修订"):
            if key in candidate:
                revision = candidate[key]
                break
        if "material" in candidate or "材料" in candidate:
            material = candidate.get("material", candidate.get("材料"))
            if isinstance(material, list) and len(material) == 2:
                return material[0], material[1], total
        for key in ("entries", "cells", "coordinates", "items", "格"):
            if key in candidate and isinstance(candidate[key], list):
                return revision, candidate[key], total
        # A single coordinate object.
        if any(k in candidate for k in ("block", "块", "coordinate", "坐标")):
            return revision, [candidate], total
        raise BizError("bad_candidate", "unrecognized candidate object")

    if not isinstance(candidate, (list, tuple)):
        raise BizError("bad_candidate", "candidate must be an array")

    # Full [material, total] response.
    if len(candidate) == 2 and isinstance(candidate[0], list) and len(candidate[0]) == 2 and isinstance(candidate[0][1], list):
        return candidate[0][0], candidate[0][1], candidate[1]

    # Material [revision, entries].
    if len(candidate) == 2 and isinstance(candidate[1], list) and (
        not candidate[1] or isinstance(candidate[1][0], (list, tuple, dict))
    ):
        return candidate[0], candidate[1], None

    # Flat scalar values.
    if all(item is None or type(item) is int or type(item) is bool for item in candidate):
        return ..., [{"flat_value": item} for item in candidate], None

    return ..., list(candidate), None


def entry_coordinate_value(entry: Any) -> tuple[Any, Any, Any, Any]:
    if isinstance(entry, dict):
        volume = None
        for key in ("volume", "卷"):
            if key in entry:
                volume = entry[key]
                break
        coordinate = entry.get("coordinate") or entry.get("坐标")
        if isinstance(coordinate, (list, tuple)) and len(coordinate) == 2:
            block, cell = coordinate
        else:
            block = first_key(entry, ("block", "块"))
            cell = first_key(entry, ("cell", "格", "index", "零基格"))
        value = first_key(entry, ("value", "值", "reading"))
        return volume, block, nonneg_int(cell, "candidate cell"), value
    if isinstance(entry, (list, tuple)):
        if len(entry) == 4:
            return entry[0], entry[1], nonneg_int(entry[2], "candidate cell"), entry[3]
        if len(entry) == 3:
            return None, entry[0], nonneg_int(entry[1], "candidate cell"), entry[2]
    raise BizError("bad_candidate_binding", "candidate cell must be [volume?, block, cell, value]")


def validate_candidate(candidate: Any, material: list[Any], total: int) -> None:
    expected_revision, expected_entries = material
    candidate_revision, raw_entries, candidate_total = candidate_entries(candidate)
    if candidate_revision is not ... and not same_id(candidate_revision, expected_revision):
        raise BizError("candidate_wrong_binding", "candidate revision is bound to another revision")
    if candidate_total is not None and not same_id(candidate_total, total):
        raise BizError("candidate_mismatch", "candidate total does not match receipt")

    expected_map: dict[tuple[str, int], tuple[Any, Any]] = {}
    expected_volume = None
    for entry in expected_entries:
        volume, block, cell, value = entry
        expected_volume = volume
        expected_map[(canonical(block), cell)] = (volume, value)

    # Positional flat values.
    if raw_entries and all(isinstance(e, dict) and "flat_value" in e for e in raw_entries):
        values = [e["flat_value"] for e in raw_entries]
        if len(values) < len(expected_entries):
            raise BizError("candidate_missing_cells", "candidate has fewer cells than receipt")
        if len(values) != len(expected_entries):
            raise BizError("candidate_wrong_binding", "candidate value array length does not match receipt")
        for i, (given, expected_entry) in enumerate(zip(values, expected_entries)):
            if not same_id(given, expected_entry[3]):
                raise BizError("candidate_mismatch", f"candidate value mismatch at position {i}")
        return

    given_map: dict[tuple[str, int], Any] = {}
    for raw_entry in raw_entries:
        given_volume, block, cell, given_value = entry_coordinate_value(raw_entry)
        key = (canonical(block), cell)
        if key not in expected_map:
            raise BizError("candidate_wrong_binding", f"candidate binds unknown cell {block!r}:{cell}")
        expected_volume_for_key, expected_value = expected_map[key]
        if given_volume is not None and not same_id(given_volume, expected_volume_for_key):
            raise BizError("candidate_wrong_binding", "candidate volume binding is incorrect")
        if key in given_map and not same_id(given_map[key], given_value):
            raise BizError("candidate_mismatch", "conflicting duplicate candidate values")
        given_map[key] = given_value

    if len(given_map) < len(expected_map):
        raise BizError("candidate_missing_cells", "candidate is missing one or more bound cells")
    if set(given_map) != set(expected_map):
        raise BizError("candidate_wrong_binding", "candidate contains cells not bound to this receipt")
    for key, given_value in given_map.items():
        if not same_id(given_value, expected_map[key][1]):
            raise BizError("candidate_mismatch", f"candidate value mismatch at {key[0]}:{key[1]}")


def op_collect(ledger: Ledger, params: Any) -> list[Any]:
    if isinstance(params, dict):
        receipt = param_at(params, 0, ("回执", "receipt"))
        candidate = param_at(params, 1, ("候选", "candidate"), ...)
    elif isinstance(params, (list, tuple)) and params:
        receipt = params[0]
        candidate = params[1] if len(params) >= 2 else ...
    else:
        raise BizError("bad_params", "collect expects [receipt, candidate?]")

    order = find_issue_by_receipt(ledger, receipt)
    revision_record = ledger.revision(order["revision"], order.get("volume", ...))
    material, total = stored_material(revision_record)

    # Prefer the immutable receipt material when present; it is the original receipt.
    receipt_obj = order.get("receipt")
    if isinstance(receipt_obj, dict) and receipt_obj.get("material") is not None:
        receipt_material = receipt_obj["material"]
        receipt_total = receipt_obj.get("total", total)
        if canonical(receipt_material) == canonical(material):
            total = receipt_total
        else:
            # Ledger is authoritative for stored signed values; corruption is not accepted.
            raise BizError("receipt_material_conflict", "stored receipt and ledger material differ")

    if candidate is not ... and candidate is not None:
        if isinstance(candidate, str):
            candidate_path = Path(candidate)
            if candidate_path.is_file():
                candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            else:
                try:
                    candidate = json.loads(candidate)
                except json.JSONDecodeError:
                    pass
        validate_candidate(candidate, material, total)
    return [material, total]


OPERATIONS = {
    "接卷": op_receive,
    "receive": op_receive,
    "receive_volume": op_receive,
    "receivevolume": op_receive,
    "accept": op_receive,
    "accept_volume": op_receive,
    "intake": op_receive,
    "take": op_receive,
    "take_volume": op_receive,
    "jiejuan": op_receive,
    "jie_juan": op_receive,
    "juan": op_receive,
    "冻结": op_freeze,
    "freeze": op_freeze,
    "freeze_revision": op_freeze,
    "freezerevision": op_freeze,
    "freeze_cells": op_freeze,
    "freezecells": op_freeze,
    "snapshot": op_freeze,
    "dongjie": op_freeze,
    "dong_jie": op_freeze,
    "签发": op_issue,
    "issue": op_issue,
    "issue_receipt": op_issue,
    "issuereceipt": op_issue,
    "sign": op_issue,
    "sign_revision": op_issue,
    "qianfa": op_issue,
    "qian_fa": op_issue,
    "归还": op_return_volume,
    "return": op_return_volume,
    "return_volume": op_return_volume,
    "returnvolume": op_return_volume,
    "give_back": op_return_volume,
    "unmount": op_return_volume,
    "unmount_volume": op_return_volume,
    "unmountvolume": op_return_volume,
    "guihai": op_return_volume,
    "gui_huan": op_return_volume,
    "核验": op_verify_return,
    "verify": op_verify_return,
    "verify_return": op_verify_return,
    "verifyreturn": op_verify_return,
    "check_return": op_verify_return,
    "领取": op_collect,
    "collect": op_collect,
    "collect_material": op_collect,
    "collectmaterial": op_collect,
    "claim": op_collect,
    "fetch": op_collect,
    "reclaim": op_collect,
    "receipt": op_collect,
    "lingqu": op_collect,
    "ling_qu": op_collect,
}


def resolve_operation(name: str):
    key = name.strip()
    if key in OPERATIONS:
        return OPERATIONS[key]
    lowered = key.lower().replace("-", "_").replace(" ", "_")
    if lowered in OPERATIONS:
        return OPERATIONS[lowered]
    compact = lowered.replace("_", "")
    compact_operations = {alias.replace("_", ""): handler for alias, handler in OPERATIONS.items()}
    if compact in compact_operations:
        return compact_operations[compact]
    raise BizError("unknown_operation", f"unknown operation {name!r}")


def run(argv: list[str]) -> int:
    if len(argv) != 4:
        print(f"usage: {Path(argv[0]).name} LEDGER OPERATION JSON_ARRAY", file=sys.stderr)
        return 2
    db_arg, op_name, params_text = argv[1:4]
    try:
        params = json.loads(params_text)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "code": "bad_json", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    state_path, lock_path = state_paths(db_arg)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            state = load_state(state_path)
            ledger = Ledger(state)
            handler = resolve_operation(op_name)
            result = handler(ledger, params)
            if ledger.changed:
                save_state_atomic(state_path, state)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except BizError as exc:
        print(
            json.dumps({"ok": False, "code": exc.code, "error": exc.message}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        print(
            json.dumps({"ok": False, "code": "internal_contract_error", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
