#!/usr/bin/env python3
"""Inventory and reconcile a model-writable worktree without Git-ignore gaps."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA = "post_agent_workspace_manifest.v1"
REPORT_SCHEMA = "post_agent_workspace_report.v1"


class GuardError(RuntimeError):
    pass


def _resolved_workspace(value: str) -> Path:
    workspace = Path(value).resolve(strict=True)
    if not workspace.is_dir():
        raise GuardError("workspace is not a directory")
    return workspace


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GuardError("manifest contains an unsafe path")
    return path


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    original_mode: int | None = None
    try:
        try:
            handle = path.open("rb")
        except PermissionError:
            original_mode = stat.S_IMODE(path.lstat().st_mode)
            os.chmod(path, original_mode | stat.S_IRUSR, follow_symlinks=False)
            handle = path.open("rb")
        with handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        if original_mode is not None:
            os.chmod(path, original_mode, follow_symlinks=False)
    return digest.hexdigest()


def _entry(path: Path, relative: str) -> dict[str, Any]:
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    row: dict[str, Any] = {"path": relative, "mode": mode}
    if stat.S_ISREG(metadata.st_mode):
        row.update(type="file", size=metadata.st_size, sha256=_digest(path))
    elif stat.S_ISDIR(metadata.st_mode):
        row.update(type="directory")
    elif stat.S_ISLNK(metadata.st_mode):
        row.update(type="symlink", target=os.readlink(path))
    else:
        row.update(type="special")
    return row


def _git_metadata_paths(workspace: Path) -> set[Path]:
    paths: set[Path] = set()
    for query in ("--absolute-git-dir", "--git-common-dir"):
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", query],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            candidate = Path(result.stdout.strip())
            if not candidate.is_absolute():
                candidate = workspace / candidate
            paths.add(candidate.resolve(strict=False))
    return paths


def _inventory(workspace: Path) -> list[dict[str, Any]]:
    metadata_paths = _git_metadata_paths(workspace)
    rows: list[dict[str, Any]] = []

    def walk(directory: Path) -> None:
        original_mode: int | None = None
        try:
            try:
                children = list(os.scandir(directory))
            except PermissionError:
                original_mode = stat.S_IMODE(directory.lstat().st_mode)
                os.chmod(directory, original_mode | stat.S_IRUSR | stat.S_IXUSR, follow_symlinks=False)
                children = list(os.scandir(directory))
            children.sort(key=lambda item: os.fsencode(item.name))
            for child in children:
                child_path = Path(child.path)
                lexical_child = Path(os.path.abspath(child.path))
                if not child.is_symlink() and lexical_child in metadata_paths:
                    continue
                relative = child_path.relative_to(workspace).as_posix()
                _safe_relative(relative)
                try:
                    row = _entry(child_path, relative)
                except OSError as error:
                    raise GuardError(f"cannot inspect workspace object: {relative}") from error
                rows.append(row)
                if row["type"] == "directory":
                    walk(child_path)
        except OSError as error:
            raise GuardError(f"cannot scan workspace: {error.strerror or error}") from error
        finally:
            if original_mode is not None:
                os.chmod(directory, original_mode, follow_symlinks=False)

    walk(workspace)
    return rows


def _workspace_identity(workspace: Path) -> dict[str, Any]:
    metadata = workspace.stat()
    return {"path": str(workspace), "device": metadata.st_dev, "inode": metadata.st_ino}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def snapshot(args: argparse.Namespace) -> int:
    workspace = _resolved_workspace(args.workspace)
    manifest_path = Path(args.manifest).resolve(strict=False)
    if manifest_path == workspace or workspace in manifest_path.parents:
        raise GuardError("manifest must be outside the workspace")
    payload = {
        "schema_version": SCHEMA,
        "workspace": _workspace_identity(workspace),
        "entries": _inventory(workspace),
    }
    _write_json(manifest_path, payload)
    print(f"POST_AGENT_WORKSPACE_GUARD_OK action=snapshot entries={len(payload['entries'])}")
    return 0


def _load_manifest(path: Path, workspace: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GuardError("manifest is unreadable or malformed") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
        raise GuardError("manifest schema is invalid")
    if payload.get("workspace") != _workspace_identity(workspace):
        raise GuardError("manifest workspace identity does not match")
    rows = payload.get("entries")
    if not isinstance(rows, list):
        raise GuardError("manifest entries are invalid")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise GuardError("manifest entry is invalid")
        path = row["path"]
        _safe_relative(path)
        if path in result:
            raise GuardError("manifest contains duplicate paths")
        result[path] = row
    return result


def _is_ignored(workspace: Path, relative: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(workspace), "check-ignore", "--no-index", "-q", "--", relative],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise GuardError("git ignore classification failed")
    return result.returncode == 0


def _changed_rows(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for relative in sorted(set(before) | set(after), key=os.fsencode):
        old = before.get(relative)
        new = after.get(relative)
        if old == new:
            continue
        if old is None:
            change = "created"
        elif new is None:
            change = "deleted"
        elif old.get("type") != new.get("type"):
            change = "type-changed"
        elif old.get("mode") != new.get("mode"):
            change = "mode-changed"
        else:
            change = "modified"
        changes.append({"path": relative, "change": change})
    return changes


def _top_level_created_paths(changes: list[dict[str, str]]) -> list[str]:
    created = {row["path"] for row in changes if row["change"] == "created"}
    roots: list[str] = []
    for relative in sorted(created, key=lambda value: (value.count("/"), os.fsencode(value))):
        parents = PurePosixPath(relative).parents
        if any(parent.as_posix() in created for parent in parents if parent.as_posix() != "."):
            continue
        roots.append(relative)
    return roots


def _move_to_quarantine(workspace: Path, quarantine: Path, relative: str) -> None:
    relative_path = _safe_relative(relative)
    source = workspace / relative_path
    restored_ancestors: list[tuple[Path, int]] = []
    ancestor = workspace
    try:
        for part in relative_path.parts[:-1]:
            ancestor /= part
            metadata = ancestor.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                break
            original_mode = stat.S_IMODE(metadata.st_mode)
            required_mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
            if original_mode & required_mode != required_mode:
                os.chmod(ancestor, original_mode | required_mode, follow_symlinks=False)
                restored_ancestors.append((ancestor, original_mode))
        if not source.exists() and not source.is_symlink():
            return
        destination = quarantine / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise GuardError("quarantine destination already exists")
        shutil.move(str(source), str(destination))
    finally:
        for restored_ancestor, original_mode in reversed(restored_ancestors):
            os.chmod(restored_ancestor, original_mode, follow_symlinks=False)


def _remove_empty_directories(root: Path) -> None:
    for candidate in sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if candidate.is_dir() and not candidate.is_symlink():
            try:
                candidate.rmdir()
            except OSError:
                pass


def reconcile(args: argparse.Namespace) -> int:
    workspace = _resolved_workspace(args.workspace)
    manifest_path = Path(args.manifest).resolve(strict=True)
    quarantine = Path(args.quarantine_dir).resolve(strict=False)
    if quarantine == workspace or workspace in quarantine.parents:
        raise GuardError("quarantine must be outside the workspace")
    quarantine.mkdir(parents=True, exist_ok=True)
    _remove_empty_directories(quarantine)
    if any(quarantine.iterdir()):
        raise GuardError("quarantine directory is not empty")
    before = _load_manifest(manifest_path, workspace)
    after_rows = _inventory(workspace)
    after = {row["path"]: row for row in after_rows}
    changes = _changed_rows(before, after)
    rejected: list[dict[str, str]] = []
    for row in changes:
        relative = row["path"]
        current = after.get(relative)
        previous = before.get(relative)
        reason = ""
        path_parts = _safe_relative(relative).parts
        if path_parts[-1] in {"sitecustomize.py", "usercustomize.py"} or path_parts[-1].endswith(".pth"):
            reason = "python-startup-path"
        elif any(
            part.startswith(".") and not (index == 0 and part == ".github")
            for index, part in enumerate(path_parts)
        ):
            reason = "hidden-path"
        elif _is_ignored(workspace, relative):
            reason = "ignored-path"
        elif (
            row["change"] == "created"
            and current is not None
            and current.get("type") == "directory"
            and current["mode"] & (stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            != (stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        ):
            os.chmod(
                workspace / _safe_relative(relative),
                current["mode"] | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                follow_symlinks=False,
            )
            reason = "directory-mode-changed"
        elif (
            row["change"] == "mode-changed"
            and current is not None
            and previous is not None
            and current.get("type") == previous.get("type") == "directory"
        ):
            os.chmod(workspace / _safe_relative(relative), previous["mode"], follow_symlinks=False)
            reason = "directory-mode-changed"
        elif any(
            entry is not None and entry.get("type") in {"symlink", "special"}
            for entry in (current, previous)
        ):
            reason = "unsupported-object"
        if reason:
            rejected.append({**row, "reason": reason})

    created_roots = _top_level_created_paths(changes)
    rejected_created_roots = _top_level_created_paths(
        [row for row in rejected if row["reason"] != "directory-mode-changed"]
    )
    restored = [
        relative
        for relative in created_roots
        if not any(relative == root or relative.startswith(f"{root}/") for root in rejected_created_roots)
    ]
    quarantined: list[str] = []
    for relative in rejected_created_roots:
        _move_to_quarantine(workspace, quarantine, relative)
        quarantined.append(relative)
    rejected_created_set = set(rejected_created_roots)
    for row in rejected:
        relative = row["path"]
        if row["reason"] == "directory-mode-changed":
            continue
        if row["change"] == "created" and any(
            relative == root or relative.startswith(f"{root}/") for root in rejected_created_set
        ):
            continue
        _move_to_quarantine(workspace, quarantine, relative)
        quarantined.append(relative)
    _remove_empty_directories(quarantine)

    changed_paths = sorted(
        {
            row["path"]
            for row in changes
            if row["change"] == "type-changed"
            or (
                before.get(row["path"], {}).get("type") != "directory"
                and after.get(row["path"], {}).get("type") != "directory"
            )
        },
        key=os.fsencode,
    )
    Path(args.changed_paths_out).write_text(
        "".join(f"{path}\n" for path in changed_paths), encoding="utf-8"
    )
    report = {
        "schema_version": REPORT_SCHEMA,
        "workspace": _workspace_identity(workspace),
        "changes": changes,
        "changed_paths": changed_paths,
        "quarantined": sorted(set(quarantined), key=os.fsencode),
        "restored": restored,
        "rejected": rejected,
    }
    _write_json(Path(args.report).resolve(strict=False), report)
    if rejected:
        paths = ",".join(row["path"] for row in rejected[:20])
        print(
            f"POST_AGENT_WORKSPACE_GUARD_BLOCKED rejected={len(rejected)} paths={paths}",
            file=sys.stderr,
        )
        return 20
    print(
        "POST_AGENT_WORKSPACE_GUARD_OK "
        f"action=reconcile changed={len(changed_paths)} restored={len(restored)}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--workspace", required=True)
    snapshot_parser.add_argument("--manifest", required=True)
    snapshot_parser.set_defaults(handler=snapshot)
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--workspace", required=True)
    reconcile_parser.add_argument("--manifest", required=True)
    reconcile_parser.add_argument("--quarantine-dir", required=True)
    reconcile_parser.add_argument("--changed-paths-out", required=True)
    reconcile_parser.add_argument("--report", required=True)
    reconcile_parser.set_defaults(handler=reconcile)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.handler(args)
    except (GuardError, OSError) as error:
        print(f"POST_AGENT_WORKSPACE_GUARD_BLOCKED error={error}", file=sys.stderr)
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
