"""Immutable local experiment evidence. Standard library only; no runtime imports."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from .history import _check_source_path


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _no_symlinks(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("symlink evidence paths are not permitted")


def stable_read(path: Path) -> bytes:
    """Detect changed input, rather than silently stitching two versions together."""
    _no_symlinks(path)
    before = path.stat()
    first = path.read_bytes()
    second = path.read_bytes()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns
    ) or first != second:
        raise ValueError("input changed during capture")
    return first


def _validate_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("bundle payload must be a mapping")
    run_id = payload.get("run_id", "")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ValueError("invalid run_id")
    for key in ("inputs", "proposal", "validated", "execution", "evidence_type"):
        if key not in payload:
            raise ValueError(f"missing evidence stage: {key}")
    if payload["evidence_type"] not in {"synthetic", "historical_capture", "prospective"}:
        raise ValueError("invalid evidence_type")
    for key in ("inputs", "proposal", "validated", "execution"):
        if payload[key] is not None and not isinstance(payload[key], dict):
            raise ValueError(f"evidence stage must be a mapping or explicit null: {key}")


def write_bundle(directory: Path, payload: dict[str, Any]) -> Path:
    """Publish complete content once; an identical retry is harmless, a rewrite fails.

    A content hash checks integrity, not a trustworthy publication timestamp or the
    truth of caller-supplied evidence. Unknown historical stages stay explicit nulls.
    """
    _validate_payload(payload)
    run_id = payload["run_id"]
    wire = canonical_bytes({"schema_version": 1, "payload_sha256": digest(canonical_bytes(payload)),
                            "payload": payload})
    _no_symlinks(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{run_id}.json"
    _no_symlinks(target)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(wire)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(temp_path, target)  # exclusive publication, never replace an existing run
        except FileExistsError:
            if stable_read(target) != wire:
                raise ValueError("run_id already exists with different evidence") from None
        return target
    finally:
        temp_path.unlink(missing_ok=True)


def read_bundle(path: Path) -> dict[str, Any]:
    document = json.loads(stable_read(path))
    if document.get("schema_version") != 1 or not isinstance(document.get("payload"), dict):
        raise ValueError("invalid bundle schema")
    payload = document["payload"]
    _validate_payload(payload)
    if digest(canonical_bytes(payload)) != document.get("payload_sha256"):
        raise ValueError("bundle integrity check failed")
    if path.stem != payload.get("run_id"):
        raise ValueError("bundle identity mismatch")
    return payload


def _selected_files(source: Path, cutoff: date) -> list[Path]:
    _check_source_path(source)
    selected: list[Path] = []
    for day_dir in sorted(source.iterdir()):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_dir.name):
            continue
        day = date.fromisoformat(day_dir.name)
        if day > cutoff:
            continue
        _no_symlinks(day_dir)
        if not day_dir.is_dir():
            raise ValueError("dated input is not a directory")
        for path in sorted(day_dir.iterdir()):
            # No recursive walk: never traverse archived runs or experimental arms.
            if path.name.endswith((".md", "_path.json", "_committee.json")):
                _check_source_path(path)
                _no_symlinks(path)
                if not path.is_file():
                    raise ValueError("expected an input file")
                selected.append(path)
    return selected


def freeze_history(source: Path, destination: Path, *, cutoff: str) -> dict[str, Any]:
    """Copy only allowlisted completed live files; private content never goes to git."""
    _no_symlinks(source)
    _check_source_path(source)
    _no_symlinks(destination)
    source, destination = source.resolve(), destination.resolve()
    if source.name != ".committee":
        raise ValueError("source must be the live .committee directory")
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("source and snapshot must not overlap")
    cutoff_date = date.fromisoformat(cutoff)
    selected = _selected_files(source, cutoff_date)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "files").mkdir()
    files: list[dict[str, Any]] = []
    for path in selected:
        contents = stable_read(path)
        relative = path.relative_to(source)
        target = destination / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as out:
            out.write(contents)
        files.append({"kind": "committee", "path": relative.as_posix(), "sha256": digest(contents), "size": len(contents)})
    declarations = source.parent / ".dreams" / "executions.jsonl"
    if declarations.exists():
        _check_source_path(declarations)
        contents = stable_read(declarations)
        target = destination / ".dreams" / "executions.jsonl"
        target.parent.mkdir(parents=True)
        with target.open("xb") as out:
            out.write(contents)
        files.append({"kind": "declarations", "path": "executions.jsonl",
                      "sha256": digest(contents), "size": len(contents)})
    manifest = {"schema_version": 1, "selection": "latest_live_only",
                "cutoff": cutoff, "files": files}
    (destination / "manifest.json").write_bytes(canonical_bytes(manifest))
    verify_history(source, destination)
    return manifest


def verify_history(source: Path, snapshot: Path) -> bool:
    manifest = json.loads(stable_read(snapshot / "manifest.json"))
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("selection") != "latest_live_only" or not isinstance(manifest.get("files"), list)):
        raise ValueError("invalid manifest schema")
    if not isinstance(manifest.get("cutoff"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", manifest["cutoff"]):
        raise ValueError("invalid manifest cutoff")
    seen = set()
    for item in manifest["files"]:
        if (not isinstance(item, dict) or item.get("kind") not in {"committee", "declarations"}
                or not isinstance(item.get("path"), str) or not item["path"]
                or type(item.get("size")) is not int or item["size"] < 0
                or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ValueError("invalid manifest entry")
        key = (item["kind"], item["path"])
        if key in seen:
            raise ValueError("duplicate manifest entry")
        seen.add(key)
    expected = {item["path"] for item in manifest["files"] if item["kind"] == "committee"}
    selected = {p.relative_to(source).as_posix()
                for p in _selected_files(source, date.fromisoformat(manifest["cutoff"]))}
    if selected != expected:
        raise ValueError("source inventory changed after capture")
    frozen_selected = {p.relative_to(snapshot / "files").as_posix()
                       for p in _selected_files(snapshot / "files", date.fromisoformat(manifest["cutoff"]))}
    if frozen_selected != expected:
        raise ValueError("snapshot inventory does not match manifest")
    ledger = source.parent / ".dreams" / "executions.jsonl"
    has_ledger = any(item["kind"] == "declarations" for item in manifest["files"])
    if ledger.exists() != has_ledger:
        raise ValueError("execution declaration inventory changed after capture")
    if (snapshot / ".dreams" / "executions.jsonl").exists() != has_ledger:
        raise ValueError("snapshot declaration inventory does not match manifest")
    for item in manifest["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid manifest path")
        if item["kind"] == "committee":
            original_path, frozen_path = source / relative, snapshot / "files" / relative
        elif item["kind"] == "declarations" and item["path"] == "executions.jsonl":
            original_path, frozen_path = ledger, snapshot / ".dreams" / relative
        else:
            raise ValueError("invalid manifest kind")
        original = stable_read(original_path)
        frozen = stable_read(frozen_path)
        if (digest(original) != item["sha256"] or digest(frozen) != item["sha256"]
                or len(original) != item["size"] or original != frozen):
            raise ValueError("source or frozen input failed its integrity check")
    return True
