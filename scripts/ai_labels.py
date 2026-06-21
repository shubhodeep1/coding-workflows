#!/usr/bin/env python3
"""AI label contract utilities for workflow phase transitions and repair."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class LabelContractError(ValueError):
    """Raised when label contract data is invalid."""


GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
GITHUB_API_MAX_ATTEMPTS = 3
GITHUB_API_BACKOFF_BASE_SECONDS = 1.0
GITHUB_API_BACKOFF_CAP_SECONDS = 8.0


def _require_nonempty(value: str | None, field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise LabelContractError(f"{field_name} is required")
    return text


def _require_repo_slug(value: str | None) -> str:
    repo_text = _require_nonempty(value, "repo")
    if repo_text.count("/") != 1:
        raise LabelContractError("repo must be in 'owner/name' format")

    owner_name, repo_name = repo_text.split("/", 1)
    if not owner_name or not repo_name:
        raise LabelContractError("repo must be in 'owner/name' format")
    if owner_name.strip() != owner_name or repo_name.strip() != repo_name:
        raise LabelContractError("repo must be in 'owner/name' format")

    return repo_text


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

    seen_group_names: set[str] = set()
    for group in phase_groups:
        if not isinstance(group, dict):
            raise LabelContractError("Each phase group must be an object")

        group_name = group.get("name")
        if not isinstance(group_name, str) or not group_name.strip():
            raise LabelContractError("Each phase group must define a non-empty name")
        group_name = group_name.strip()
        if group_name in seen_group_names:
            raise LabelContractError(f"Duplicate phase group name: {group_name}")
        seen_group_names.add(group_name)

        members = group.get("members")
        if not isinstance(members, list) or len(members) < 2:
            raise LabelContractError("Each phase group must contain at least two members")

        seen_members: set[str] = set()
        for member in members:
            if not isinstance(member, str):
                raise LabelContractError(f"Phase group members must be strings: {member!r}")
            if member not in labels:
                raise LabelContractError(f"Phase group member not declared in labels: {member}")
            if member in seen_members:
                raise LabelContractError(f"Duplicate member in phase group {group_name}: {member}")
            seen_members.add(member)

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


def _normalize_label_sync_metadata(label_name: str, metadata: Any) -> dict[str, str]:
    if not isinstance(metadata, dict):
        raise LabelContractError(f"Label metadata must be an object for {label_name}")

    color_value = metadata.get("color")
    if not isinstance(color_value, str) or not color_value.strip():
        raise LabelContractError(f"Label color must be a non-empty string for {label_name}")

    description_value = metadata.get("description")
    if not isinstance(description_value, str) or not description_value.strip():
        raise LabelContractError(f"Label description must be a non-empty string for {label_name}")

    return {
        "color": color_value.strip().lstrip("#").lower(),
        "description": description_value.strip(),
    }


def _label_sync_validation_error(metadata: dict[str, str]) -> str | None:
    description_text = metadata["description"]
    if len(description_text) > 100:
        return f"description exceeds GitHub's 100-character limit ({len(description_text)})"
    return None


def _label_sync_name_validation_error(label_name: str) -> str | None:
    if len(label_name) > 50:
        return f"label name exceeds GitHub's 50-character limit ({len(label_name)})"
    return None


def _github_token() -> str | None:
    token_text = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    return token_text or None


def _sanitize_log_detail(detail: str) -> str:
    return " ".join(detail.split())


def _github_retry_header_delay_seconds(headers: Any) -> float | None:
    if headers is None:
        return None

    retry_after_value = headers.get("Retry-After")
    if isinstance(retry_after_value, str):
        retry_after_text = retry_after_value.strip()
        if retry_after_text.isdigit():
            return float(retry_after_text)

    rate_limit_reset_value = headers.get("X-RateLimit-Reset")
    if isinstance(rate_limit_reset_value, str):
        rate_limit_reset_text = rate_limit_reset_value.strip()
        if rate_limit_reset_text.isdigit():
            reset_delay_seconds = float(int(rate_limit_reset_text) - int(time.time()))
            return max(0.0, reset_delay_seconds)

    return None


def _github_retry_delay_seconds(headers: Any, attempt_index: int) -> float:
    header_delay_seconds = _github_retry_header_delay_seconds(headers)
    exponential_delay_seconds = min(
        GITHUB_API_BACKOFF_CAP_SECONDS,
        GITHUB_API_BACKOFF_BASE_SECONDS * (2**attempt_index),
    )
    base_delay_seconds = exponential_delay_seconds
    if header_delay_seconds is not None:
        base_delay_seconds = max(base_delay_seconds, header_delay_seconds)

    jitter_seconds = random.uniform(0.0, 0.5)
    if header_delay_seconds is not None and header_delay_seconds > GITHUB_API_BACKOFF_CAP_SECONDS:
        return base_delay_seconds + jitter_seconds

    return min(
        GITHUB_API_BACKOFF_CAP_SECONDS,
        base_delay_seconds + jitter_seconds,
    )


def _github_api_request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    require_token: bool = False,
) -> Any:
    token_text = _github_token()
    if require_token and token_text is None:
        raise LabelContractError("GH_TOKEN / GITHUB_TOKEN is required unless --dry-run is set")

    request_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "coding-workflows-ai-labels",
    }
    if token_text:
        request_headers["Authorization"] = f"Bearer {token_text}"

    request_data: bytes | None = None
    if payload is not None:
        request_data = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        request_headers["Content-Type"] = "application/json; charset=utf-8"

    method_name = method.upper()
    request = urllib.request.Request(
        f"{GITHUB_API_BASE_URL}/{path.lstrip('/')}",
        data=request_data,
        headers=request_headers,
        method=method_name,
    )
    for attempt_index in range(GITHUB_API_MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in GITHUB_API_RETRY_STATUSES or attempt_index + 1 >= GITHUB_API_MAX_ATTEMPTS:
                raise
            time.sleep(_github_retry_delay_seconds(exc.headers, attempt_index))
            continue
        except (urllib.error.URLError, OSError):
            if attempt_index + 1 >= GITHUB_API_MAX_ATTEMPTS:
                raise
            time.sleep(_github_retry_delay_seconds(None, attempt_index))
            continue

        if not response_body:
            return None

        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            response_preview = _sanitize_log_detail(response_body.decode("utf-8", errors="replace"))
            if len(response_preview) > 160:
                response_preview = f"{response_preview[:157]}..."
            if not response_preview:
                response_preview = "<empty response body>"
            raise ValueError(
                f"GitHub API returned non-JSON response for {method_name} {path}: {response_preview}"
            ) from exc

    raise RuntimeError(f"GitHub API retry loop exhausted for {method_name} {path}")


def _github_label_path(repo: str, label_name: str) -> str:
    encoded_label_name = urllib.parse.quote(label_name, safe="")
    return f"repos/{repo}/labels/{encoded_label_name}"


def _label_sync_matches(existing_label: dict[str, Any], expected_label: dict[str, str]) -> bool:
    existing_color = str(existing_label.get("color") or "").strip().lstrip("#").lower()
    existing_description = str(existing_label.get("description") or "")
    return existing_color == expected_label["color"] and existing_description == expected_label["description"]


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    error_body = b""
    try:
        error_body = exc.read() or b""
    except (OSError, ValueError):
        error_body = b""

    if error_body:
        try:
            decoded_payload = json.loads(error_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded_text = error_body.decode("utf-8", errors="replace").strip()
            if decoded_text:
                return decoded_text
        else:
            if isinstance(decoded_payload, dict):
                payload_message = decoded_payload.get("message")
                if isinstance(payload_message, str) and payload_message.strip():
                    payload_errors = decoded_payload.get("errors")
                    if payload_errors is None:
                        return payload_message.strip()
                    return f"{payload_message.strip()}: {json.dumps(payload_errors, ensure_ascii=True, sort_keys=True)}"

    reason_text = exc.reason
    if isinstance(reason_text, str) and reason_text.strip():
        return reason_text.strip()
    return f"HTTP {exc.code}"


def _is_already_exists_error_message(message: str) -> bool:
    lowered_message = message.lower()
    return "already_exists" in lowered_message or "already exists" in lowered_message


def _log_label_sync(
    prefix: str,
    *,
    repo: str,
    label_name: str,
    action: str,
    dry_run: bool,
    status: int | None = None,
    detail: str | None = None,
) -> None:
    parts = [
        f"{prefix}:",
        f"repo={repo}",
        f"label={label_name}",
        f"action={action}",
        f"dry_run={'true' if dry_run else 'false'}",
    ]
    if status is not None:
        parts.append(f"status={status}")
    if detail:
        parts.append(f"detail={_sanitize_log_detail(detail)}")
    print(" ".join(parts), file=sys.stderr)


def _append_label_sync_error(
    errors: list[dict[str, Any]],
    *,
    repo: str,
    label_name: str,
    action: str,
    dry_run: bool,
    message: str,
    status: int | None = None,
) -> None:
    error_record: dict[str, Any] = {
        "label": label_name,
        "action": action,
        "message": _sanitize_log_detail(message),
    }
    if status is not None:
        error_record["status"] = status
    errors.append(error_record)
    _log_label_sync(
        "LABEL_SYNC_ERROR",
        repo=repo,
        label_name=label_name,
        action=action,
        dry_run=dry_run,
        status=status,
        detail=message,
    )


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
        "add": sorted(set(desired) - issue_labels_set),
        "remove": sorted(remove - set(desired)),
    }
    _print_json(output)
    return 0


def cmd_sync_labels(args: argparse.Namespace) -> int:
    contract = load_label_contract(Path(args.contract_file).resolve())
    repo = _require_repo_slug(args.repo)
    dry_run = bool(args.dry_run)
    if not dry_run and _github_token() is None:
        raise LabelContractError("GH_TOKEN / GITHUB_TOKEN is required unless --dry-run is set")

    label_items = sorted(contract["labels"].items())
    created_count = 0
    updated_count = 0
    unchanged_count = 0
    errors: list[dict[str, Any]] = []

    for label_name, raw_metadata in label_items:
        expected_metadata = _normalize_label_sync_metadata(label_name, raw_metadata)
        name_validation_error = _label_sync_name_validation_error(label_name)
        if name_validation_error is not None:
            _append_label_sync_error(
                errors,
                repo=repo,
                label_name=label_name,
                action="validate",
                dry_run=dry_run,
                message=name_validation_error,
            )
            continue
        label_path = _github_label_path(repo, label_name)

        try:
            existing_label = _github_api_request(label_path)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                _append_label_sync_error(
                    errors,
                    repo=repo,
                    label_name=label_name,
                    action="get",
                    dry_run=dry_run,
                    message=_http_error_message(exc),
                    status=exc.code,
                )
                continue
            existing_label = None
        except LabelContractError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _append_label_sync_error(
                errors,
                repo=repo,
                label_name=label_name,
                action="get",
                dry_run=dry_run,
                message=str(exc),
            )
            continue

        if existing_label is None:
            validation_error = _label_sync_validation_error(expected_metadata)
            if validation_error is not None:
                _append_label_sync_error(
                    errors,
                    repo=repo,
                    label_name=label_name,
                    action="create",
                    dry_run=dry_run,
                    message=validation_error,
                )
                continue

            if dry_run:
                created_count += 1
                _log_label_sync(
                    "LABEL_SYNC_CREATED",
                    repo=repo,
                    label_name=label_name,
                    action="create",
                    dry_run=True,
                )
                continue

            try:
                _github_api_request(
                    f"repos/{repo}/labels",
                    method="POST",
                    payload={"name": label_name, **expected_metadata},
                    require_token=True,
                )
            except urllib.error.HTTPError as exc:
                error_message = _http_error_message(exc)
                if exc.code == 422 and _is_already_exists_error_message(error_message):
                    try:
                        current_label = _github_api_request(label_path)
                    except LabelContractError:
                        raise
                    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
                        current_label = None
                    if isinstance(current_label, dict) and _label_sync_matches(current_label, expected_metadata):
                        created_count += 1
                        _log_label_sync(
                            "LABEL_SYNC_CREATED",
                            repo=repo,
                            label_name=label_name,
                            action="create",
                            dry_run=False,
                            detail="resolved already_exists conflict by re-reading label",
                        )
                        continue
                _append_label_sync_error(
                    errors,
                    repo=repo,
                    label_name=label_name,
                    action="create",
                    dry_run=False,
                    message=error_message,
                    status=exc.code,
                )
                continue
            except LabelContractError:
                raise
            except (urllib.error.URLError, OSError, ValueError) as exc:
                _append_label_sync_error(
                    errors,
                    repo=repo,
                    label_name=label_name,
                    action="create",
                    dry_run=False,
                    message=str(exc),
                )
                continue

            created_count += 1
            _log_label_sync(
                "LABEL_SYNC_CREATED",
                repo=repo,
                label_name=label_name,
                action="create",
                dry_run=False,
            )
            continue

        if not isinstance(existing_label, dict):
            _append_label_sync_error(
                errors,
                repo=repo,
                label_name=label_name,
                action="get",
                dry_run=dry_run,
                message="GitHub label response was not a JSON object",
            )
            continue

        if _label_sync_matches(existing_label, expected_metadata):
            unchanged_count += 1
            _log_label_sync(
                "LABEL_SYNC_UNCHANGED",
                repo=repo,
                label_name=label_name,
                action="get",
                dry_run=dry_run,
            )
            continue

        validation_error = _label_sync_validation_error(expected_metadata)
        if validation_error is not None:
            _append_label_sync_error(
                errors,
                repo=repo,
                label_name=label_name,
                action="update",
                dry_run=dry_run,
                message=validation_error,
            )
            continue

        if dry_run:
            updated_count += 1
            _log_label_sync(
                "LABEL_SYNC_UPDATED",
                repo=repo,
                label_name=label_name,
                action="update",
                dry_run=True,
            )
            continue

        try:
            _github_api_request(
                label_path,
                method="PATCH",
                payload=expected_metadata,
                require_token=True,
            )
        except urllib.error.HTTPError as exc:
            _append_label_sync_error(
                errors,
                repo=repo,
                label_name=label_name,
                action="update",
                dry_run=False,
                message=_http_error_message(exc),
                status=exc.code,
            )
            continue
        except LabelContractError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _append_label_sync_error(
                errors,
                repo=repo,
                label_name=label_name,
                action="update",
                dry_run=False,
                message=str(exc),
            )
            continue

        updated_count += 1
        _log_label_sync(
            "LABEL_SYNC_UPDATED",
            repo=repo,
            label_name=label_name,
            action="update",
            dry_run=False,
        )

    output = {
        "created": created_count,
        "updated": updated_count,
        "unchanged": unchanged_count,
        "errors": errors,
    }
    _print_json(output)
    return 1 if label_items and len(errors) == len(label_items) else 0


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

    sync = subparsers.add_parser("sync-labels", help="Create or update repo labels from the label contract")
    sync.add_argument("--contract-file", required=True)
    sync.add_argument("--repo", required=True)
    sync.add_argument("--dry-run", action="store_true")
    sync.set_defaults(func=cmd_sync_labels)

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
