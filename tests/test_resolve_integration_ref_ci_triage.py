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
	flag_off = []
	for fixture in FIXTURES:
		data = json.loads(fixture.read_text(encoding="utf-8"))
		if data.get("expected_exit_code") == 3:
			assert data.get("expected_error_reason"), f"{fixture.name} must name its refusal reason"
			reasons.add(data["expected_error_reason"])
		flag = (data.get("env") or {}).get("CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED", "false")
		if flag.lower() != "true":
			flag_off.append(fixture.name)
			assert data.get("expected_exit_code") == 0 and data.get("expected_stdout") == "", fixture.name
		elif data.get("expected_stdout"):
			routed.append(fixture.name)
	assert REFUSAL_REASONS <= reasons, sorted(REFUSAL_REASONS - reasons)
	assert flag_off, "need a default-off fixture"
	assert routed, "need a routed fixture"


def test_refusal_exit_code_is_three_and_default_is_off() -> None:
	text = RESOLVER.read_text(encoding="utf-8")
	assert "CI_TRIAGE_REFUSED_RC=3" in text
	assert '"${CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED:-false}"' in text


def test_ci_triage_fixtures_stay_out_of_the_parity_directory() -> None:
	parity_dir = REPO_ROOT / "tests" / "fixtures" / "integration_ref_resolver"
	names = {path.name for path in parity_dir.glob("*.json")}
	assert not names & {path.name for path in FIXTURES}
