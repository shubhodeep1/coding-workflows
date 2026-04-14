#!/usr/bin/env python3
"""AI label contract utilities for workflow phase transitions and repair."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class LabelContractError(ValueError):
    """Raised when label contract data is invalid."""


def _require_nonempty(value: str | None, field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise LabelContractError(f"{field_name} is required")
    return text


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_label_contract(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise LabelContractError("Label contract must be a JSON object")

    schema_version = payload.get("schema_version")
    if schema_version != "label_contract.v1":
        raise LabelContractError(f"Unsupported schema_version: {schema_version!r}")

    labels = payload.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise LabelContractError("labels must be a non-empty object")

    phase_groups = payload.get("phase_groups")
    if not isinstance(phase_groups, list) or not phase_groups:
        raise LabelContractError("phase_groups must be a non-empty array")

    for group in phase_groups:
        if not isinstance(group, dict):
            raise LabelContractError("Each phase group must be an object")
        members = group.get("members")
        if not isinstance(members, list) or len(members) < 2:
            raise LabelContractError("Each phase group must contain at least two members")
        for member in members:
            if not isinstance(member, str):
                raise LabelContractError(f"Phase group members must be strings: {member!r}")
            if member not in labels:
                raise LabelContractError(f"Phase group member not declared in labels: {member}")

        fallback = group.get("fallback")
        if fallback is not None:
            if not isinstance(fallback, str):
                raise LabelContractError(f"Phase group fallback must be a string: {fallback!r}")
            fallback_value = fallback.strip()
            if not fallback_value:
                raise LabelContractError("Phase group fallback cannot be empty")
            if fallback_value not in labels:
                raise LabelContractError(f"Phase group fallback not declared in labels: {fallback_value}")
            if fallback_value not in members:
                raise LabelContractError(f"Phase group fallback must be a member: {fallback_value}")

    return payload


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def cmd_contract_validate(args: argparse.Namespace) -> int:
    contract = load_label_contract(Path(args.contract_file).resolve())
    _print_json({"ok": True, "schema_version": contract["schema_version"], "labels": sorted(contract["labels"].keys())})
    return 0


def cmd_contract_get(args: argparse.Namespace) -> int:
    contract = load_label_contract(Path(args.contract_file).resolve())
    _print_json({"ok": True, "contract": contract})
    return 0


def cmd_resolve_phase(args: argparse.Namespace) -> int:
    contract = load_label_contract(Path(args.contract_file).resolve())
    phase = _require_nonempty(args.phase, "phase")

    labels = contract["labels"]
    if phase not in labels:
        raise LabelContractError(f"Unknown phase label in contract: {phase}")

    remove_labels: list[str] = []
    for group in contract["phase_groups"]:
        members = [str(item) for item in group.get("members") or []]
        if phase in members:
            remove_labels.extend([item for item in members if item != phase])

    remove_labels = sorted(set(remove_labels))
    _print_json({"ok": True, "add": [phase], "remove": remove_labels})
    return 0


def cmd_repair_labels(args: argparse.Namespace) -> int:
    contract = load_label_contract(Path(args.contract_file).resolve())
    issue_labels = sorted({item.strip() for item in (args.issue_labels or "").split(",") if item.strip()})
    issue_labels_set = set(issue_labels)
    known_labels = set(contract["labels"].keys())
    has_known_label = bool(issue_labels_set & known_labels)

    desired: list[str] = []
    remove: set[str] = set()

    for group in contract["phase_groups"]:
        members = [str(item) for item in group.get("members") or []]
        present = [label for label in members if label in issue_labels_set]
        if not present:
            # Only assign fallback when the issue is already in AI-managed states.
            # This avoids adding AI labels to unrelated open issues during repair sweeps.
            fallback = str(group.get("fallback") or "").strip()
            if has_known_label and fallback and fallback in known_labels:
                desired.append(fallback)
            continue
        if len(present) > 1:
            # Keep the most-advanced (last) phase in the group when multiple are present.
            # This avoids regressing issue state during repairs (e.g., keeping ai:done over ai:implementing).
            keep = present[-1]
            desired.append(keep)
            for label in present:
                if label != keep:
                    remove.add(label)
        else:
            desired.append(present[0])

    output = {
        "ok": True,
        "known_labels": sorted(known_labels),
        "issue_labels": issue_labels,
        "add": sorted(set(desired) - set(issue_labels)),
        "remove": sorted(remove),
    }
    _print_json(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI label policy helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("contract-validate", help="Validate label contract JSON")
    validate.add_argument("--contract-file", required=True)
    validate.set_defaults(func=cmd_contract_validate)

    get_contract = subparsers.add_parser("contract-get", help="Print label contract JSON")
    get_contract.add_argument("--contract-file", required=True)
    get_contract.set_defaults(func=cmd_contract_get)

    resolve_phase = subparsers.add_parser("resolve-phase", help="Resolve label add/remove set for a phase")
    resolve_phase.add_argument("--contract-file", required=True)
    resolve_phase.add_argument("--phase", required=True)
    resolve_phase.set_defaults(func=cmd_resolve_phase)

    repair = subparsers.add_parser("repair-labels", help="Repair issue labels against phase exclusivity rules")
    repair.add_argument("--contract-file", required=True)
    repair.add_argument("--issue-labels", required=True)
    repair.set_defaults(func=cmd_repair_labels)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except (LabelContractError, ValueError, json.JSONDecodeError) as exc:
        print(f"AI_LABELS_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
