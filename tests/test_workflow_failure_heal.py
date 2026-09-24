#!/usr/bin/env python3
"""Focused contracts for review/autofix identical-failure fingerprints."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_PATH = REPO_ROOT / "scripts" / "workflow_failure_heal.py"
HEAD_SHA = "a" * 40
OLD_ENGINE_SHA = "b" * 40
CURRENT_ENGINE_SHA = "c" * 40
AUTHOR = "workflow-pat-user"


def _load_helper():
	spec = importlib.util.spec_from_file_location("workflow_failure_heal", HELPER_PATH)
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	return module


heal = _load_helper()


def _fingerprint(reason: str = "editor_empty_noop") -> str:
	return heal.autofix_failure_fingerprint(
		failure_reason=reason,
		evidence_text="::error::editor provider failed deterministically\n",
	)["fp"]


def _comment(
	run_id: int,
	*,
	degraded: bool = False,
	engine_sha: str | None = CURRENT_ENGINE_SHA,
	fingerprint: str | None = None,
) -> dict[str, object]:
	marker = heal.render_failure_marker(
		HEAD_SHA,
		"editor_empty_noop",
		fingerprint or _fingerprint(),
		degraded,
		str(run_id),
		engine_sha,
	)
	assert marker
	return {"id": run_id, "author_login": AUTHOR, "body": f"failure\n{marker}"}


def test_render_and_parse_failure_marker_add_engine_without_breaking_legacy() -> None:
	marker = heal.render_failure_marker(
		HEAD_SHA,
		"editor_empty_noop",
		_fingerprint(),
		False,
		"17",
		CURRENT_ENGINE_SHA.upper(),
	)
	assert f" engine={CURRENT_ENGINE_SHA} run=17 " in f" {marker} "
	parsed = heal.parse_failure_markers(
		[{"id": 17, "author_login": AUTHOR, "body": marker}],
		head_sha=HEAD_SHA,
		author_login=AUTHOR,
	)
	assert parsed[0]["engine"] == CURRENT_ENGINE_SHA

	legacy_marker = heal.render_failure_marker(HEAD_SHA, "editor_empty_noop", _fingerprint(), False, "18")
	assert " engine=" not in legacy_marker
	legacy = heal.parse_failure_markers(
		[{"id": 18, "author_login": AUTHOR, "body": legacy_marker}],
		head_sha=HEAD_SHA,
		author_login=AUTHOR,
	)
	assert legacy[0]["engine"] == ""
	assert " engine=" not in heal.render_failure_marker(
		HEAD_SHA,
		"editor_empty_noop",
		_fingerprint(),
		False,
		"19",
		"not-a-sha",
	)


def test_three_degraded_markers_remain_diagnostic_only() -> None:
	comments = [_comment(run_id, degraded=True) for run_id in range(1, 4)]
	result = heal.count_identical_failures(
		comments,
		head_sha=HEAD_SHA,
		author_login=AUTHOR,
		engine_sha=CURRENT_ENGINE_SHA,
	)
	assert result["count"] == 0
	assert result["fp"] == _fingerprint()
	assert result["cap_applied"] is False


def test_newest_degraded_marker_does_not_expose_older_deterministic_failures() -> None:
	comments = [_comment(run_id) for run_id in range(1, 4)]
	comments.append(_comment(4, degraded=True))
	result = heal.count_identical_failures(
		comments,
		head_sha=HEAD_SHA,
		author_login=AUTHOR,
		engine_sha=CURRENT_ENGINE_SHA,
	)
	assert result["count"] == 0


def test_same_engine_evidence_markers_cap_and_old_engine_markers_do_not() -> None:
	comments = [
		_comment(1, engine_sha=None),
		_comment(2, engine_sha=OLD_ENGINE_SHA),
		_comment(3),
		_comment(4),
		_comment(5),
	]
	result = heal.count_identical_failures(
		comments,
		head_sha=HEAD_SHA,
		author_login=AUTHOR,
		engine_sha=CURRENT_ENGINE_SHA,
	)
	assert result["count"] == 3


def test_cap_marker_is_engine_and_author_scoped() -> None:
	comments = [_comment(run_id) for run_id in range(1, 4)]
	comments.extend(
		[
			{
				"author_login": AUTHOR,
				"body": f"<!-- review-autofix-failure-cap:v1 head={HEAD_SHA} engine={OLD_ENGINE_SHA} fp={_fingerprint()} reason=editor_empty_noop count=3 -->",
			},
			{
				"author_login": "untrusted-user",
				"body": f"<!-- review-autofix-failure-cap:v1 head={HEAD_SHA} engine={CURRENT_ENGINE_SHA} fp={_fingerprint()} reason=editor_empty_noop count=3 -->",
			},
		]
	)
	result = heal.count_identical_failures(
		comments,
		head_sha=HEAD_SHA,
		author_login=AUTHOR,
		engine_sha=CURRENT_ENGINE_SHA,
	)
	assert result["count"] == 3
	assert result["cap_applied"] is False

	comments.append(
		{
			"author_login": AUTHOR,
			"body": f"<!-- review-autofix-failure-cap:v1 head={HEAD_SHA} engine={CURRENT_ENGINE_SHA} fp={_fingerprint()} reason=editor_empty_noop count=3 -->",
		}
	)
	assert heal.count_identical_failures(
		comments,
		head_sha=HEAD_SHA,
		author_login=AUTHOR,
		engine_sha=CURRENT_ENGINE_SHA,
	)["cap_applied"] is True


def test_engine_less_calls_preserve_legacy_counting() -> None:
	comments = [_comment(run_id, engine_sha=None) for run_id in range(1, 4)]
	result = heal.count_identical_failures(comments, head_sha=HEAD_SHA, author_login=AUTHOR)
	assert result["count"] == 3


def test_cli_accepts_optional_engine_sha(tmp_path: Path) -> None:
	evidence = tmp_path / "editor_attempt_1.err"
	evidence.write_text("::error::editor provider failed deterministically\n", encoding="utf-8")
	fingerprint_result = subprocess.run(
		[
			sys.executable,
			str(HELPER_PATH),
			"autofix-failure-fingerprint",
			"--failure-reason",
			"editor_empty_noop",
			"--evidence-file",
			str(evidence),
			"--head-sha",
			HEAD_SHA,
			"--run-id",
			"9",
			"--engine-sha",
			CURRENT_ENGINE_SHA,
		],
		check=True,
		capture_output=True,
		text=True,
	)
	assert f"engine={CURRENT_ENGINE_SHA}" in fingerprint_result.stdout

	comments_path = tmp_path / "comments.json"
	comments_path.write_text(json.dumps([_comment(run_id) for run_id in range(1, 4)]), encoding="utf-8")
	count_result = subprocess.run(
		[
			sys.executable,
			str(HELPER_PATH),
			"autofix-identical-failure-count",
			"--comments-json",
			str(comments_path),
			"--head-sha",
			HEAD_SHA,
			"--author-login",
			AUTHOR,
			"--engine-sha",
			CURRENT_ENGINE_SHA,
		],
		check=True,
		capture_output=True,
		text=True,
	)
	assert "count=3\n" in count_result.stdout
	assert "cap_applied=false\n" in count_result.stdout
