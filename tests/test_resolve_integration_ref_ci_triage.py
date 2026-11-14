#!/usr/bin/env python3
"""CI-triage PR-branch routing in scripts/resolve_integration_ref.sh.

Each fixture under tests/fixtures/integration_ref_resolver_ci_triage/ runs
through the resolver's own mocked-``gh`` self-test harness
(``--self-test-case``), which asserts stdout, exit code and, for refusals,
exactly one ``::error::CI_TRIAGE_PR_BRANCH_ROUTING ... reason=<token>`` line.
These fixtures stay out of tests/fixtures/integration_ref_resolver/ so the
shell/Python parity test there is unaffected: the Python resolver has no
workflow callers and does not implement this routing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVER = REPO_ROOT / "scripts" / "resolve_integration_ref.sh"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "integration_ref_resolver_ci_triage"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))
REFUSAL_REASONS = {
	"label_missing",
	"marker_invalid",
	"author_untrusted",
	"not_an_issue",
	"pr_association_mismatch",
	"pr_lookup_failed",
	"pr_not_open",
	"pr_cross_repository",
	"head_ref_invalid",
	"branch_missing",
	"issue_read_failed",
	"issue_parse_failed",
	"issue_json_invalid",
	"branch_lookup_failed",
	"base_ref_invalid",
}


def _run_case(fixture: Path) -> subprocess.CompletedProcess[str]:
	env = dict(os.environ)
	# An outer value must not change the result: the harness sets the flag
	# from the fixture only.
	env["CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED"] = "true"
	return subprocess.run(
		["bash", str(RESOLVER), "--self-test-case", str(fixture)],
		cwd=REPO_ROOT,
		env=env,
		capture_output=True,
		text=True,
		timeout=120,
		check=False,
	)


@pytest.mark.parametrize("fixture", FIXTURES, ids=[path.stem for path in FIXTURES])
def test_ci_triage_fixture(fixture: Path) -> None:
	result = _run_case(fixture)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "self-test passed" in result.stdout


def test_fixture_set_covers_every_refusal_reason_and_the_happy_paths() -> None:
	reasons = set()
	routed = []
	routed_by_default = []
	flag_off = []
	for fixture in FIXTURES:
		data = json.loads(fixture.read_text(encoding="utf-8"))
		if data.get("expected_exit_code") == 3:
			assert data.get("expected_error_reason"), f"{fixture.name} must name its refusal reason"
			reasons.add(data["expected_error_reason"])
		# The self-test harness turns routing off for a fixture without the
		# key; a JSON null unsets it, which exercises the resolver's default.
		flag = (data.get("env") or {}).get("CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED", "false")
		if flag is not None and flag.lower() != "true":
			flag_off.append(fixture.name)
			assert data.get("expected_exit_code") == 0 and data.get("expected_stdout") == "", fixture.name
		elif data.get("expected_stdout"):
			routed.append(fixture.name)
			if flag is None:
				routed_by_default.append(fixture.name)
	assert REFUSAL_REASONS <= reasons, sorted(REFUSAL_REASONS - reasons)
	assert flag_off, "need a flag-off fixture"
	assert routed, "need a routed fixture"
	assert routed_by_default, "need a fixture that routes with the flag unset"


def test_refusal_exit_code_is_three_and_default_is_on() -> None:
	text = RESOLVER.read_text(encoding="utf-8")
	assert "CI_TRIAGE_REFUSED_RC=3" in text
	assert '"${CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED:-true}"' in text
	assert '"${CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED:-false}"' not in text


def test_unrecognised_flag_value_warns_and_stays_off() -> None:
	result = _run_case(FIXTURE_DIR / "flag_unrecognised_value_is_off.json")
	assert result.returncode == 0, result.stderr + result.stdout
	assert "self-test passed" in result.stdout


def test_ci_triage_fixtures_stay_out_of_the_parity_directory() -> None:
	parity_dir = REPO_ROOT / "tests" / "fixtures" / "integration_ref_resolver"
	names = {path.name for path in parity_dir.glob("*.json")}
	assert not names & {path.name for path in FIXTURES}


TRACKING_LINE_PATTERN = r'^\s*(?:-\s*)?(?:\*\*Tracking issue:\*\*|Tracking issue:)\s*#(\d+)\s*$'


def test_managed_child_copied_header_fixtures_are_present() -> None:
	# Issue #7513 (#7511 / #7512): an ai:orchestrator-managed child with a
	# copied triage header, no ai:check-triage label and a tracking line
	# resolves through its tracking issue; without the tracking line it is
	# still refused as label_missing.
	skip_path = FIXTURE_DIR / "managed_child_copied_header_without_label_skips.json"
	refuse_path = FIXTURE_DIR / "managed_child_without_tracking_line_label_missing.json"
	skip = json.loads(skip_path.read_text(encoding="utf-8"))
	refuse = json.loads(refuse_path.read_text(encoding="utf-8"))
	tracking = re.compile(TRACKING_LINE_PATTERN, re.MULTILINE)

	skip_issue = skip["issues"]["101"]
	assert skip["expected_exit_code"] == 0
	assert skip["expected_stdout"]
	assert "expected_error_reason" not in skip
	assert {"name": "ai:orchestrator-managed"} in skip_issue["labels"]
	assert {"name": "ai:check-triage"} not in skip_issue["labels"]
	assert tracking.search(skip_issue["body"])

	refuse_issue = refuse["issues"]["101"]
	assert refuse["expected_exit_code"] == 3
	assert refuse["expected_error_reason"] == "label_missing"
	assert {"name": "ai:orchestrator-managed"} in refuse_issue["labels"]
	assert not tracking.search(refuse_issue["body"])

	for path in (skip_path, refuse_path):
		result = _run_case(path)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "self-test passed" in result.stdout


def test_tracking_line_pattern_stays_in_sync_with_extract_tracking_issue() -> None:
	text = RESOLVER.read_text(encoding="utf-8")
	assert text.count(TRACKING_LINE_PATTERN) == 2


# Issue #8671 / #8673: a label-less issue that only mentions
# check-failure-triage markers in prose is skipped (ordinary issue) while
# CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED is on (the default); a label-less body
# whose first line is the canonical header is still refused as label_missing.
PROSE_MARKER_PATTERN = re.compile(r"<!--\s*check-failure-triage:")
PROSE_MARKER_NON_PR_PATTERN = re.compile(r"<!--\s*check-failure-triage:(?!pr=)")
FP_HEADER_PATTERN = re.compile(r"<!-- check-failure-triage:fp=[0-9a-f]{64} -->")
PROSE_MARKER_FIXTURES = {
	"prose_marker_mid_body_without_label_skips": (0, None),
	"prose_marker_header_first_line_without_label_refuses": (3, "label_missing"),
	"prose_marker_skip_flag_false_refuses": (3, "label_missing"),
	"prose_marker_skip_flag_unrecognised_refuses": (3, "label_missing"),
	"prose_marker_body_with_label_refuses_marker_invalid": (3, "marker_invalid"),
	"prose_marker_managed_child_without_tracking_line_refuses": (3, "label_missing"),
	"prose_marker_header_behind_zero_width_joiner_refuses": (3, "label_missing"),
	"prose_marker_header_behind_interleaved_invisibles_refuses": (3, "label_missing"),
}
# Fixtures whose canonical fp= header sits behind leading invisible characters
# (and whitespace); the resolver must strip them and still refuse.
INVISIBLE_PREFIX_STEMS = {
	"prose_marker_header_behind_zero_width_joiner_refuses",
	"prose_marker_header_behind_interleaved_invisibles_refuses",
}
INVISIBLE_PREFIX_PATTERN = re.compile(r"^[\s\ufeff\u200b-\u200f\u202a-\u202e\u2060-\u2064\u180e\u00ad\u034f]+")


def test_invisible_prefix_pattern_stays_in_sync_with_resolver() -> None:
	# The shape test above copies the resolver's strip class; fail if they drift.
	text = RESOLVER.read_text(encoding="utf-8")
	assert text.count('r"' + INVISIBLE_PREFIX_PATTERN.pattern + '"') == 1


def test_prose_marker_skip_default_is_on() -> None:
	text = RESOLVER.read_text(encoding="utf-8")
	assert '"${CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED:-true}"' in text
	assert '"${CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED:-false}"' not in text


@pytest.mark.parametrize("stem", sorted(PROSE_MARKER_FIXTURES))
def test_prose_marker_skip_fixtures_shape_and_run(stem: str) -> None:
	path = FIXTURE_DIR / f"{stem}.json"
	data = json.loads(path.read_text(encoding="utf-8"))
	expected_rc, expected_reason = PROSE_MARKER_FIXTURES[stem]
	assert data["expected_exit_code"] == expected_rc
	assert data.get("expected_error_reason") == expected_reason
	assert data["expected_stdout"] == ""
	issue = data["issues"]["101"]
	body = issue["body"]
	labels = {label["name"] for label in issue["labels"]}
	first_line = body.split("\n", 1)[0]
	env = data.get("env") or {}
	if stem == "prose_marker_header_first_line_without_label_refuses":
		assert "ai:check-triage" not in labels
		assert FP_HEADER_PATTERN.fullmatch(first_line)
		assert env.get("CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED") is None
	elif stem in INVISIBLE_PREFIX_STEMS:
		# The raw first line does not start with the marker, but after the
		# leading invisibles are dropped the canonical header is first.
		assert "ai:check-triage" not in labels
		assert not labels
		assert not PROSE_MARKER_PATTERN.match(body)
		assert INVISIBLE_PREFIX_PATTERN.match(body)
		stripped_first = INVISIBLE_PREFIX_PATTERN.sub("", body).split("\n", 1)[0]
		assert FP_HEADER_PATTERN.fullmatch(stripped_first)
		assert env.get("CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED") is None
	else:
		assert not PROSE_MARKER_PATTERN.match(first_line)
		assert PROSE_MARKER_NON_PR_PATTERN.search(body)
		if stem == "prose_marker_body_with_label_refuses_marker_invalid":
			assert "ai:check-triage" in labels
			assert "CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED" not in env
		else:
			assert "ai:check-triage" not in labels
	if stem == "prose_marker_mid_body_without_label_skips":
		assert "CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED" not in env
	if stem == "prose_marker_managed_child_without_tracking_line_refuses":
		# A managed child with no tracking line stays fail closed even when its
		# markers sit only in prose.
		assert "ai:orchestrator-managed" in labels
		assert "Tracking issue" not in body
		assert "CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED" not in env
	if stem == "prose_marker_skip_flag_false_refuses":
		assert env["CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED"] == "false"
	if stem == "prose_marker_skip_flag_unrecognised_refuses":
		assert env["CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED"] == "maybe"
		assert "CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED has an unrecognised value" in data["expected_warning"]
	result = _run_case(path)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "self-test passed" in result.stdout


def test_prose_marker_skip_outer_value_does_not_leak() -> None:
	# The harness unsets the flag for a fixture without the key, so an outer
	# "false" must not turn the default-on skip off.
	env = dict(os.environ)
	env["CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED"] = "false"
	result = subprocess.run(
		["bash", str(RESOLVER), "--self-test-case", str(FIXTURE_DIR / "prose_marker_mid_body_without_label_skips.json")],
		cwd=REPO_ROOT,
		env=env,
		capture_output=True,
		text=True,
		timeout=120,
		check=False,
	)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "self-test passed" in result.stdout


# Issue #9170: an unblock-judge CI-failure reissue (lone `pr=` marker on line
# 1, "Re-issued by the unblock judge from #N." on line 2, no label) whose
# owning PR merged into a non-default branch of this repository resolves to
# that base branch. A PR that did not merge keeps the default branch; any
# failure on the merged path refuses (exit 3) instead of falling back.
REISSUE_FIXTURES = {
	"reissue_lone_pr_marker_without_label_skips": (0, None, ""),
	"reissue_closed_unmerged_pr_keeps_default": (0, None, ""),
	"reissue_merged_pr_routes_to_base_branch": (0, None, "orchestrator/project-768"),
	# The deleted base branch's own lookup worked and found no merge into the
	# default branch (an abandoned branch).
	"reissue_merged_pr_base_branch_missing_refuses": (3, "branch_missing", ""),
	# The helper's closed-PR lookup failed: not an abandoned branch.
	"reissue_merged_pr_base_branch_lookup_failed_refuses": (3, "branch_lookup_failed", ""),
	# The deleted base branch was itself merged into the default branch
	# (finalize uses --delete-branch): keep the default branch.
	"reissue_merged_pr_base_branch_merged_into_default_keeps_default": (0, None, ""),
	"reissue_merged_pr_cross_repository_refuses": (3, "pr_cross_repository", ""),
	"reissue_pr_lookup_failed_refuses": (3, "pr_lookup_failed", ""),
	"reissue_merged_pr_base_ref_invalid_refuses": (3, "base_ref_invalid", ""),
}
REISSUE_LINE_TWO = re.compile(r"Re-issued by the unblock judge from #[1-9][0-9]*\.")
ROUTED_LOG_LITERAL = 'echo "CI_TRIAGE_PR_BRANCH_ROUTING issue=${ISSUE} pr=${pr} outcome=routed branch=${ref}" >&2'


@pytest.mark.parametrize("stem", sorted(REISSUE_FIXTURES))
def test_reissue_fixtures_shape_and_run(stem: str) -> None:
	path = FIXTURE_DIR / f"{stem}.json"
	data = json.loads(path.read_text(encoding="utf-8"))
	expected_rc, expected_reason, expected_stdout = REISSUE_FIXTURES[stem]
	assert data["expected_exit_code"] == expected_rc
	assert data.get("expected_error_reason") == expected_reason
	assert data["expected_stdout"] == expected_stdout
	issue = data["issues"]["101"]
	assert issue["labels"] == []
	assert issue["author_association"] in {"OWNER", "MEMBER", "COLLABORATOR"}
	lines = issue["body"].split("\n")
	assert re.fullmatch(r"<!-- check-failure-triage:pr=[1-9][0-9]* -->", lines[0])
	assert REISSUE_LINE_TWO.fullmatch(lines[1])
	pulls = data.get("pulls") or {}
	if stem == "reissue_pr_lookup_failed_refuses":
		assert not pulls
	else:
		pull = next(iter(pulls.values()))
		merged_expected = stem.startswith("reissue_merged_pr_")
		assert pull["merged"] is merged_expected
	result = _run_case(path)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "self-test passed" in result.stdout


def _reissue_fixture(name: str, *, pull: dict | None, branch_exists: dict, stdout: str, rc: int, reason: str | None = None, body: str | None = None, labels: list | None = None, author: str = "OWNER") -> dict:
	data: dict = {
		"name": name,
		"env": {"CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED": "true"},
		"issues": {
			"101": {
				"body": body if body is not None else "<!-- check-failure-triage:pr=6786 -->\nRe-issued by the unblock judge from #7.\n\nFix the failing check.\n",
				"labels": labels or [],
				"author_association": author,
				"user": {"login": "owner"},
			}
		},
		"branch_exists": branch_exists,
		"expected_stdout": stdout,
		"expected_exit_code": rc,
	}
	if pull is not None:
		data["pulls"] = {"6786": pull}
	if reason:
		data["expected_error_reason"] = reason
	return data


def _merged_pull(base_ref: str = "orchestrator/project-768", default_branch: str | None = "main") -> dict:
	base_repo: dict = {"full_name": "owner/repo"}
	if default_branch is not None:
		base_repo["default_branch"] = default_branch
	return {
		"number": 6786,
		"state": "closed",
		"merged": True,
		"head": {"ref": "ai/issue-6700", "repo": {"full_name": "owner/repo"}},
		"base": {"ref": base_ref, "repo": base_repo},
	}


EXISTS = {"orchestrator/project-768": True, "main": True}
REISSUE_EDGE_CASES = {
	"merged_into_default_branch_keeps_default": dict(pull=_merged_pull(base_ref="main"), branch_exists=EXISTS, stdout="", rc=0),
	"untrusted_author_keeps_default": dict(pull=_merged_pull(), branch_exists=EXISTS, stdout="", rc=0, author="NONE"),
	"managed_label_keeps_default": dict(pull=_merged_pull(), branch_exists=EXISTS, stdout="", rc=0, labels=[{"name": "ai:orchestrator-managed"}]),
	"tracking_line_keeps_default": dict(pull=None, branch_exists=EXISTS, stdout="", rc=0, body="<!-- check-failure-triage:pr=6786 -->\nRe-issued by the unblock judge from #7.\n\n- Tracking issue: #900\n"),
	"line_two_mismatch_keeps_default": dict(pull=_merged_pull(), branch_exists=EXISTS, stdout="", rc=0, body="<!-- check-failure-triage:pr=6786 -->\nPlease look at this.\n"),
	"marker_not_first_line_keeps_default": dict(pull=_merged_pull(), branch_exists=EXISTS, stdout="", rc=0, body="Hello\n<!-- check-failure-triage:pr=6786 -->\nRe-issued by the unblock judge from #7.\n"),
	"base_ref_leading_dash_refuses": dict(pull=_merged_pull(base_ref="-bad"), branch_exists=EXISTS, stdout="", rc=3, reason="base_ref_invalid"),
	"base_ref_dotdot_refuses": dict(pull=_merged_pull(base_ref="a/../b"), branch_exists=EXISTS, stdout="", rc=3, reason="base_ref_invalid"),
	"base_ref_head_refuses": dict(pull=_merged_pull(base_ref="HEAD"), branch_exists=EXISTS, stdout="", rc=3, reason="base_ref_invalid"),
	"missing_default_branch_refuses": dict(pull=_merged_pull(default_branch=None), branch_exists=EXISTS, stdout="", rc=3, reason="pr_lookup_failed"),
	"pr_number_mismatch_refuses": dict(pull={**_merged_pull(), "number": 1}, branch_exists=EXISTS, stdout="", rc=3, reason="pr_association_mismatch"),
	"routing_flag_off_keeps_default": dict(pull=_merged_pull(), branch_exists=EXISTS, stdout="", rc=0),
}


@pytest.mark.parametrize("case", sorted(REISSUE_EDGE_CASES))
def test_reissue_edge_cases(tmp_path: Path, case: str) -> None:
	# Edge cases run from tmp_path; the committed fixture
	# reissue_merged_pr_base_ref_invalid_refuses.json keeps base_ref_invalid in
	# REFUSAL_REASONS and the full --self-test.
	data = _reissue_fixture(case, **REISSUE_EDGE_CASES[case])
	if case == "routing_flag_off_keeps_default":
		data["env"]["CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED"] = "false"
	path = tmp_path / f"{case}.json"
	path.write_text(json.dumps(data), encoding="utf-8")
	result = _run_case(path)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "self-test passed" in result.stdout


def test_routed_log_literal_is_shared_by_both_routing_paths() -> None:
	# plan.yml's routed detection (and the regex in
	# test_plan_check_triage_branch_guard.py) match this exact line.
	text = RESOLVER.read_text(encoding="utf-8")
	assert text.count(ROUTED_LOG_LITERAL) == 2
	routed = re.findall(r'echo "(CI_TRIAGE_PR_BRANCH_ROUTING [^"]*outcome=routed[^"]*)" >&2', text)
	assert len(routed) == 2 and routed[0] == routed[1]


def test_reissue_verdict_is_routed_through_the_triage_parser() -> None:
	text = RESOLVER.read_text(encoding="utf-8")
	assert "resolve_ci_reissue_base_branch() {" in text
	assert 'print("refuse base_ref_invalid")' in text
	assert 'resolve_ci_reissue_base_branch "${pr}"' in text


def test_reissue_helper_switch_is_pinned_against_caller_value() -> None:
	# The resolver pins RETARGET_MERGED_BASE_ENABLED=true on the helper call,
	# so a caller exporting false (implement.yml) agrees with plan.yml.
	fixture = FIXTURE_DIR / "reissue_merged_pr_base_branch_merged_into_default_keeps_default.json"
	env = {**os.environ, "CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED": "true", "RETARGET_MERGED_BASE_ENABLED": "false"}
	result = subprocess.run(["bash", str(RESOLVER), "--self-test-case", str(fixture)],
		cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "self-test passed" in result.stdout
