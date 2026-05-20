#!/usr/bin/env python3
"""Contract tests for resolver retry-state persistence + escape valve.

The integration-sync resolver now persists a single hidden
AUTOFIX_RESOLVER_RETRY_STATE_V1 block in the PR body after a
fingerprint-verifier failure. The state is keyed by the current PR
head SHA plus a stable sha256 over the sorted union of regressed and
pre-existing-drift fp_keys, so only identical-signature failures on the
same head count toward RESOLVER_ESCAPE_THRESHOLD_N.

The implementation lives inside scripts/review_conflict_resolve.sh as an
embedded Python helper (source-of-truth for the JSON block + signature
logic) and the workflow suppresses the generic PR failure comment when
that helper already posted the dedicated escalation summary.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
REVIEW_AUTOFIX = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


def _resolve_script_text() -> str:
	return RESOLVE_SCRIPT.read_text(encoding="utf-8")


def _retry_state_namespace() -> dict[str, object]:
	body = _resolve_script_text()
	match = re.search(
		r"# AUTOFIX_RESOLVER_RETRY_STATE_PY_BEGIN\n(.*?)\n# AUTOFIX_RESOLVER_RETRY_STATE_PY_END",
		body,
		flags=re.S,
	)
	assert match, "review_conflict_resolve.sh missing embedded retry-state Python helper"
	namespace: dict[str, object] = {"__name__": "resolver_retry_state_test"}
	exec(match.group(1), namespace)
	return namespace


@contextlib.contextmanager
def _pushd(path: Path):
	prev = Path.cwd()
	os.chdir(path)
	try:
		yield
	finally:
		os.chdir(prev)


def _seed_repo_files(tmp_path: Path) -> None:
	(tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
	(tmp_path / "scripts" / "example_a.py").write_text("resolver output\n", encoding="utf-8")
	(tmp_path / "scripts" / "example_b.py").write_text("resolver output\n", encoding="utf-8")


def _sample_fingerprints(*, reverse_order: bool = False, variant: str = "base") -> dict[str, object]:
	entries = [
		(
			"1500",
			{
				"issue": 1500,
				"pr": 2600,
				"must_contain": [{"file": "scripts/example_a.py", "regex": "EXPECTED_A"}],
				"must_not_contain": [],
				"must_not_exist": [],
			},
		),
		(
			"1501",
			{
				"issue": 1501,
				"pr": 2601,
				"must_contain": [{"file": "scripts/example_b.py", "regex": "EXPECTED_B" if variant == "base" else "EXPECTED_C"}],
				"must_not_contain": [],
				"must_not_exist": [],
			},
		),
	]
	if reverse_order:
		entries.reverse()
	return dict(entries)


def _sample_baseline_state(*, variant: str = "base") -> dict[str, object]:
	regex_b = "EXPECTED_B" if variant == "base" else "EXPECTED_C"
	return {
		"schema_version": 1,
		"fingerprints": {
			"1500": {
				"issue": 1500,
				"pr": 2600,
				"must_contain": [{
					"path": "scripts/example_a.py",
					"regex": "EXPECTED_A",
					"fp_key": ["scripts/example_a.py", "EXPECTED_A"],
					"satisfied": True,
				}],
				"must_not_contain": [],
				"must_not_exist": [],
			},
			"1501": {
				"issue": 1501,
				"pr": 2601,
				"must_contain": [{
					"path": "scripts/example_b.py",
					"regex": regex_b,
					"fp_key": ["scripts/example_b.py", regex_b],
					"satisfied": False,
				}],
				"must_not_contain": [],
				"must_not_exist": [],
			},
		},
	}


def _pr_payload(*, head_sha: str, body: str = "") -> dict[str, object]:
	return {
		"body": body,
		"head": {"sha": head_sha},
	}


def _build_artifact(
	*,
	tmp_path: Path,
	pr_payload: dict[str, object],
	pr_issue_comments: list[dict[str, object]] | None = None,
	fingerprints: dict[str, object] | None = None,
	baseline_state: dict[str, object] | None = None,
	threshold: int = 5,
	max_items: int = 10,
) -> dict[str, object]:
	ns = _retry_state_namespace()
	verifier_module = ns["load_verifier_module"](str(REPO_ROOT / "scripts"))
	with _pushd(tmp_path):
		return ns["build_resolver_retry_state_artifact"](
			pr_payload=pr_payload,
			pr_issue_comments=pr_issue_comments or [],
			fingerprints=fingerprints or _sample_fingerprints(),
			baseline_state=baseline_state or _sample_baseline_state(),
			threshold=threshold,
			repository="owner/repo",
			pr_number="123",
			run_url="https://github.com/owner/repo/actions/runs/1",
			verifier_module=verifier_module,
			max_items=max_items,
		)


def test_retry_state_signature_is_order_stable(tmp_path: Path) -> None:
	_seed_repo_files(tmp_path)
	artifact_a = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=_pr_payload(head_sha="head-a"),
		fingerprints=_sample_fingerprints(reverse_order=False),
	)
	artifact_b = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=_pr_payload(head_sha="head-a"),
		fingerprints=_sample_fingerprints(reverse_order=True),
	)
	assert artifact_a["ok"] is True
	assert artifact_b["ok"] is True
	assert artifact_a["failure_signature_sha256"] == artifact_b["failure_signature_sha256"]


def test_retry_state_identical_signature_increments(tmp_path: Path) -> None:
	_seed_repo_files(tmp_path)
	ns = _retry_state_namespace()
	first = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=_pr_payload(head_sha="head-b"),
	)
	previous_state = dict(first["retry_state"])
	previous_state["consecutive_failure_count"] = 2
	pr_payload = _pr_payload(
		head_sha="head-b",
		body=ns["upsert_retry_state_block"]("Initial PR body", previous_state),
	)
	second = _build_artifact(tmp_path=tmp_path, pr_payload=pr_payload)
	assert second["consecutive_failure_count"] == 3
	assert second["retry_state"]["consecutive_failure_count"] == 3


def test_retry_state_resets_on_head_sha_change(tmp_path: Path) -> None:
	_seed_repo_files(tmp_path)
	ns = _retry_state_namespace()
	first = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=_pr_payload(head_sha="old-head"),
	)
	previous_state = dict(first["retry_state"])
	previous_state["consecutive_failure_count"] = 4
	pr_payload = _pr_payload(
		head_sha="new-head",
		body=ns["upsert_retry_state_block"]("Initial PR body", previous_state),
	)
	second = _build_artifact(tmp_path=tmp_path, pr_payload=pr_payload)
	assert second["consecutive_failure_count"] == 1
	assert second["retry_state"]["head_sha"] == "new-head"


def test_retry_state_resets_on_signature_change(tmp_path: Path) -> None:
	_seed_repo_files(tmp_path)
	ns = _retry_state_namespace()
	first = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=_pr_payload(head_sha="same-head"),
		fingerprints=_sample_fingerprints(variant="base"),
		baseline_state=_sample_baseline_state(variant="base"),
	)
	previous_state = dict(first["retry_state"])
	previous_state["consecutive_failure_count"] = 4
	pr_payload = _pr_payload(
		head_sha="same-head",
		body=ns["upsert_retry_state_block"]("Initial PR body", previous_state),
	)
	second = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=pr_payload,
		fingerprints=_sample_fingerprints(variant="changed"),
		baseline_state=_sample_baseline_state(variant="changed"),
	)
	assert second["consecutive_failure_count"] == 1
	assert second["failure_signature_sha256"] != first["failure_signature_sha256"]


def test_retry_state_escape_threshold_sets_escalated_and_summary(tmp_path: Path) -> None:
	_seed_repo_files(tmp_path)
	ns = _retry_state_namespace()
	first = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=_pr_payload(head_sha="head-threshold"),
	)
	previous_state = dict(first["retry_state"])
	previous_state["consecutive_failure_count"] = 4
	pr_payload = _pr_payload(
		head_sha="head-threshold",
		body=ns["upsert_retry_state_block"]("Initial PR body", previous_state),
	)
	second = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=pr_payload,
		pr_issue_comments=[{"id": 77, "body": "<!-- AUTOFIX_RESOLVER_ESCALATED_V1 -->\nold"}],
	)
	assert second["consecutive_failure_count"] == 5
	assert second["escalated"] is True
	assert second["existing_escalation_comment_id"] == 77
	assert "<!-- AUTOFIX_RESOLVER_ESCALATED_V1 -->" in second["summary_comment_body"]
	assert second["retry_state"]["escalated"] is True


def test_review_autofix_wires_escape_threshold_and_failure_comment_suppression() -> None:
	body = REVIEW_AUTOFIX.read_text(encoding="utf-8")
	assert "RESOLVER_ESCAPE_THRESHOLD_N:" in body
	assert "vars.RESOLVER_ESCAPE_THRESHOLD_N || '5'" in body
	assert 'ensure_label_exists "ai:resolver-escalated" "${{ github.repository }}"' in body
	assert "env.RESOLVER_ESCALATED != 'true'" in body
