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
import inspect
import json
import os
import re
import sys
import tempfile
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


def test_retry_state_tier_selection_from_consecutive_failure_count() -> None:
	ns = _retry_state_namespace()
	select_tier = ns["select_verification_tier"]
	assert select_tier(0, 2) == "strict"
	assert select_tier(1, 2) == "strict"
	assert select_tier(2, 2) == "ratio"
	assert select_tier(4, 2) == "count_only"
	assert select_tier(6, 2) == "warn_only"


def test_retry_state_tier_selection_resets_to_strict_on_head_change() -> None:
	ns = _retry_state_namespace()
	body = ns["upsert_retry_state_block"](
		"Initial PR body",
		{
			"schema_version": 1,
			"head_sha": "old-head",
			"consecutive_failure_count": 6,
		},
	)
	selection = ns["select_verification_tier_from_pr_payload"](
		_pr_payload(head_sha="new-head", body=body),
		2,
	)
	assert selection["tier"] == "strict"
	assert selection["reason"] == "retry_state_head_sha_mismatch"


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


def test_retry_state_downgrade_marker_emits_on_threshold_crossings(tmp_path: Path) -> None:
	_seed_repo_files(tmp_path)
	ns = _retry_state_namespace()
	for previous_count, expected_from, expected_to in (
		(1, "strict", "ratio"),
		(3, "ratio", "count_only"),
		(5, "count_only", "warn_only"),
	):
		first = _build_artifact(
			tmp_path=tmp_path,
			pr_payload=_pr_payload(head_sha=f"head-tier-{previous_count}"),
			threshold=2,
		)
		previous_state = dict(first["retry_state"])
		previous_state["consecutive_failure_count"] = previous_count
		previous_state["verification_tier"] = expected_from
		pr_payload = _pr_payload(
			head_sha=f"head-tier-{previous_count}",
			body=ns["upsert_retry_state_block"]("Initial PR body", previous_state),
		)
		second = _build_artifact(tmp_path=tmp_path, pr_payload=pr_payload, threshold=2)
		assert second["verification_tier"] == expected_to
		assert second["retry_state"]["verification_tier"] == expected_to
		assert second["tier_downgrade_marker"] == (
			f"::warning::FINGERPRINT_TIER_DOWNGRADED_V1 from={expected_from} to={expected_to} "
			f"reason=consecutive_failure_count_{previous_count + 1}_threshold_2"
		)


def test_retry_state_first_tier_threshold_does_not_escalate(tmp_path: Path) -> None:
	_seed_repo_files(tmp_path)
	ns = _retry_state_namespace()
	first = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=_pr_payload(head_sha="head-ratio"),
		threshold=2,
	)
	previous_state = dict(first["retry_state"])
	previous_state["consecutive_failure_count"] = 1
	pr_payload = _pr_payload(
		head_sha="head-ratio",
		body=ns["upsert_retry_state_block"]("Initial PR body", previous_state),
	)
	second = _build_artifact(tmp_path=tmp_path, pr_payload=pr_payload, threshold=2)
	assert second["consecutive_failure_count"] == 2
	assert second["verification_tier"] == "ratio"
	assert second["escalated"] is False
	assert second["retry_state"]["escalated"] is False


def test_retry_state_escape_threshold_sets_escalated_and_summary(tmp_path: Path) -> None:
	_seed_repo_files(tmp_path)
	ns = _retry_state_namespace()
	first = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=_pr_payload(head_sha="head-threshold"),
		threshold=2,
	)
	previous_state = dict(first["retry_state"])
	previous_state["consecutive_failure_count"] = 7
	previous_state["verification_tier"] = "warn_only"
	pr_payload = _pr_payload(
		head_sha="head-threshold",
		body=ns["upsert_retry_state_block"]("Initial PR body", previous_state),
	)
	second = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=pr_payload,
		threshold=2,
		pr_issue_comments=[{"id": 77, "body": "<!-- AUTOFIX_RESOLVER_ESCALATED_V1 -->\nold"}],
	)
	assert second["consecutive_failure_count"] == 8
	assert second["verification_tier"] == "warn_only"
	assert second["escalated"] is True
	assert second["existing_escalation_comment_id"] == 77
	assert "<!-- AUTOFIX_RESOLVER_ESCALATED_V1 -->" in second["summary_comment_body"]
	assert "escalation threshold 8" in second["summary_comment_body"]
	assert second["retry_state"]["escalated"] is True
	assert second["retry_state"]["escalation_threshold"] == 8


def test_resolve_script_uses_single_escalation_marker_literal_source() -> None:
	body = _resolve_script_text()
	marker = "<!-- AUTOFIX_RESOLVER_ESCALATED_V1 -->"
	assert f'ESCALATION_COMMENT_MARKER = "{marker}"' in body
	assert "RESOLVER_ESCALATION_COMMENT_MARKER=" not in body
	assert body.count(marker) == 1


def test_retry_state_artifact_treats_missing_baseline_as_regressed(tmp_path: Path) -> None:
	_seed_repo_files(tmp_path)
	artifact = _build_artifact(
		tmp_path=tmp_path,
		pr_payload=_pr_payload(head_sha="head-no-baseline"),
		baseline_state={"schema_version": 1, "fingerprints": {}},
	)
	assert artifact["ok"] is True
	assert artifact["retry_state"]["regressed_by_resolver_count"] == 2
	assert artifact["retry_state"]["pre_existing_drift_count"] == 0


def test_review_autofix_wires_escape_threshold_and_failure_comment_suppression() -> None:
	body = REVIEW_AUTOFIX.read_text(encoding="utf-8")
	resolve_body = RESOLVE_SCRIPT.read_text(encoding="utf-8")
	assert 'SUPPORT_SCRIPTS_DIR="${SUPPORT_SCRIPTS_DIR:-scripts}"' in resolve_body
	assert "RESOLVER_ESCAPE_THRESHOLD_N:" in body
	assert "vars.RESOLVER_ESCAPE_THRESHOLD_N || '5'" in body
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in body
	assert 'ensure_label_exists "ai:resolver-escalated" "${{ github.repository }}"' in body
	assert body.count("env.RESOLVER_ESCALATED != 'true'") >= 2
	assert "- name: Post partial finalize comment and persist runtime marker" in body
	assert "<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->" in body
	assert "partial_finalize.json" in body
	assert "env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED == 'true'" in body
	assert body.index("Post partial finalize comment and persist runtime marker") < body.index(
		"Post review-blocked comment on PR (workflow failure)"
	)
	assert "Install Codex CLI" not in body
	assert "Create Codex config" not in body
	assert 'opencode_run_cmd "$@"' in resolve_body
	assert 'writer\n    "${MODEL_EDITOR}"' in resolve_body
	assert '"${_current_reasoning_effort}"' in resolve_body
	assert "CODEX_THREAD_REUSE_ENABLED requested; OpenCode conflict resolver uses the fresh full-prompt path." in resolve_body


def test_resolve_script_baseline_fallback_and_comment_gate_contract() -> None:
	body = _resolve_script_text()
	assert "Resolver retry-state persistence continuing without baseline fingerprints state" in body
	assert re.search(
		r'RESOLVER_FP_BASELINE_STATE_FILE="\$\{_retry_state_baseline_file\}"\s+_build_resolver_retry_state_artifact',
		body,
	), "retry-state builder should receive an empty baseline-file env override when capture is unavailable"
	patch_branch_start = body.index('if [ -n "${_comment_id}" ] && [[ "${_comment_id}" =~ ^[0-9]+$ ]]; then')
	patch_branch_end = body.index('elif [ -s "${_comment_file}" ]; then', patch_branch_start)
	patch_branch = body[patch_branch_start:patch_branch_end]
	patch_call = 'if gh_retry gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/comments/${_comment_id}"'
	assert patch_call in patch_branch
	assert patch_branch.index("_comment_present=true") > patch_branch.index(patch_call)


def test_resolve_script_wires_tier_selection_and_verifier_args() -> None:
	body = _resolve_script_text()
	assert "_select_fingerprint_verification_tier" in body
	assert 'Integration fingerprint verification tier selected:' in body
	assert body.count('--verification-tier "${RESOLVER_FP_VERIFICATION_TIER}"') >= 2
	assert 'if [ "${RESOLVER_FP_EXIT}" -eq 1 ] || [ "${RESOLVER_FP_VERIFICATION_TIER:-strict}" = "warn_only" ]; then' in body
	assert "select_verification_tier_from_pr_payload" in body
	assert "FINGERPRINT_TIER_DOWNGRADED_V1" in body


def main() -> int:
	selected_names = list(sys.argv[1:])
	tests_by_name = {
		name: func
		for name, func in sorted(globals().items())
		if name.startswith("test_") and callable(func)
	}
	if selected_names:
		missing = [name for name in selected_names if name not in tests_by_name]
		for name in missing:
			print(f"  FAIL  {name}: unknown test name", flush=True)
		if missing:
			return 1
		test_funcs = [tests_by_name[name] for name in selected_names]
	else:
		test_funcs = list(tests_by_name.values())
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			params = list(inspect.signature(func).parameters)
			if not params:
				func()
			elif params == ["tmp_path"]:
				with tempfile.TemporaryDirectory(prefix="resolver-retry-state-") as td:
					func(Path(td))
			else:
				raise TypeError(f"unsupported test signature for {name}: {params}")
			print(f"  PASS  {name}", flush=True)
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}", flush=True)
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
