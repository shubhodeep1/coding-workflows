#!/usr/bin/env python3
"""Tests for scripts/lint_plan_archival_completeness.py (P6 from
docs/postmortems/2026-05-18-project-2734-stall.md).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "lint_plan_archival_completeness.py"


def _import_lint():
	spec = importlib.util.spec_from_file_location("lint_plan_archival", SCRIPT_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules["lint_plan_archival"] = module
	spec.loader.exec_module(module)
	return module


_PROJECT_2734_BODY = """## Project: Integration-sync resolver self-heal

**Total issues:** 9 | **Waves:** 7

### Wave 1
- [ ] **phase1-verifier-baseline-delta**: Phase 1A (priority 1)
- [ ] **phase6-subissue-test-runs-spike**: Phase 6 spike (priority 4)

### Wave 2
- [ ] **phase1-resolver-bootstrap-wiring**: Phase 1B (priority 1)

### Wave 3
- [ ] **phase2-retry-state-escalation**: Phase 2 (priority 2)

### Wave 4
- [ ] **phase3-tiered-verification**: Phase 3 (priority 2)

`ai:orchestrator-tracking`
"""

_FULLY_TICKED_BODY = _PROJECT_2734_BODY.replace("- [ ]", "- [x]")


def test_no_docs_completed_changes_is_silent_pass():
	# A PR with no archival changes never triggers the check.
	mod = _import_lint()
	violations, errors = mod.lint(
		pr_body="just a normal PR. Refs #2734.",
		added_files=["scripts/some_other_change.sh"],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		issue_fetcher=lambda r, n: {"labels": [], "body": ""},
	)
	assert violations == []
	assert errors == []


def test_archival_without_any_refs_is_violation():
	# The PR archives a plan but doesn't say which tracking issue it
	# belongs to — operator can't audit.
	mod = _import_lint()
	violations, errors = mod.lint(
		pr_body="Move the plan file. No refs at all.",
		added_files=["docs/completed/some-plan.md"],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		issue_fetcher=lambda r, n: {"labels": [], "body": ""},
	)
	assert len(violations) == 1
	assert "references no issues" in violations[0]
	assert "Refs #N" in violations[0]


def test_archival_with_url_form_tracking_ref_is_checked():
	# Full GitHub issue URLs must be harvested too; otherwise a PR body can
	# bypass the archival gate by swapping `Refs #2734` for the URL form.
	mod = _import_lint()
	def fetch(repo: str, n: int) -> dict | None:
		if n == 2734:
			return {"labels": ["ai:orchestrator-tracking"], "body": _PROJECT_2734_BODY}
		return {"labels": [], "body": ""}
	violations, errors = mod.lint(
		pr_body="Archive plan. Refs https://github.com/owner/repo/issues/2734\n",
		added_files=["docs/completed/integration-sync-resolver-self-heal-plan.md"],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		issue_fetcher=fetch,
	)
	assert errors == []
	assert len(violations) == 1
	assert "#2734" in violations[0]


def test_archival_with_unticked_tracking_issue_no_descope_fails():
	# THIS IS THE PROJECT-#2734 SCENARIO. The PR archives the plan but
	# the tracking issue's checkboxes are all unticked and the PR body
	# has no `## De-scoped phases` section. Must fail.
	mod = _import_lint()
	def fetch(repo: str, n: int) -> dict | None:
		if n == 2734:
			return {"labels": ["ai:orchestrator-tracking"], "body": _PROJECT_2734_BODY}
		return {"labels": [], "body": ""}
	violations, errors = mod.lint(
		pr_body=(
			"Squash merge of orchestrator project #2734.\n\n"
			"Refs #2734\n"
		),
		added_files=["docs/completed/integration-sync-resolver-self-heal-plan.md"],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		issue_fetcher=fetch,
	)
	assert errors == []
	assert len(violations) == 1
	v = violations[0]
	assert "#2734" in v
	assert "5 unchecked" in v  # the 5 checkboxes in our fixture body
	assert "De-scoped phases" in v
	assert "docs/completed/integration-sync-resolver-self-heal-plan.md" in v
	# The error must surface the names of unchecked items so the operator
	# can write the de-scope section without re-reading the issue.
	assert "phase1-verifier-baseline-delta" in v


def test_archival_with_all_ticked_tracking_issue_passes():
	# Scenario A: every checkbox is ticked. No de-scope section needed.
	mod = _import_lint()
	def fetch(repo: str, n: int) -> dict | None:
		if n == 2734:
			return {"labels": ["ai:orchestrator-tracking"], "body": _FULLY_TICKED_BODY}
		return None
	violations, errors = mod.lint(
		pr_body="Squash merge.\n\nRefs #2734\n",
		added_files=["docs/completed/integration-sync-resolver-self-heal-plan.md"],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		issue_fetcher=fetch,
	)
	assert violations == []
	assert errors == []


def test_archival_with_unticked_tracking_issue_but_descope_section_passes():
	# Scenario B: the PR body explicitly acknowledges the de-scoped
	# phases. Lint passes.
	mod = _import_lint()
	def fetch(repo: str, n: int) -> dict | None:
		if n == 2734:
			return {"labels": ["ai:orchestrator-tracking"], "body": _PROJECT_2734_BODY}
		return None
	pr_body = (
		"Squash merge of project #2734.\n\n"
		"Refs #2734\n\n"
		"## De-scoped phases\n\n"
		"- Phase 2: deferred to follow-up tracking issue (alert ladder).\n"
		"- Phase 3-5: descoped — alternative approach selected.\n"
	)
	violations, errors = mod.lint(
		pr_body=pr_body,
		added_files=["docs/completed/integration-sync-resolver-self-heal-plan.md"],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		issue_fetcher=fetch,
	)
	assert violations == []
	assert errors == []


def test_archival_with_empty_descope_section_still_fails():
	# The heading alone is not enough — the PR body must contain some
	# acknowledgement content under the de-scope section.
	mod = _import_lint()
	def fetch(repo: str, n: int) -> dict | None:
		if n == 2734:
			return {"labels": ["ai:orchestrator-tracking"], "body": _PROJECT_2734_BODY}
		return None
	violations, errors = mod.lint(
		pr_body="Refs #2734\n\n## De-scoped phases\n\n",
		added_files=["docs/completed/integration-sync-resolver-self-heal-plan.md"],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		issue_fetcher=fetch,
	)
	assert errors == []
	assert len(violations) == 1
	assert "non-empty explicit `## De-scoped phases` section" in violations[0]


def test_archival_with_blank_unchecked_checkbox_still_fails():
	# Blank checkbox items count as unchecked in the readiness gate; the
	# archival lint must classify them the same way.
	mod = _import_lint()
	def fetch(repo: str, n: int) -> dict | None:
		if n == 2734:
			return {
				"labels": ["ai:orchestrator-tracking"],
				"body": "## Project\n\n- [ ]\n- [x] done\n",
			}
		return None
	violations, errors = mod.lint(
		pr_body="Refs #2734\n",
		added_files=["docs/completed/plan.md"],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		issue_fetcher=fetch,
	)
	assert errors == []
	assert len(violations) == 1
	assert "<blank checkbox item>" in violations[0]


def test_archival_with_non_tracking_ref_is_silent_pass():
	# The PR archives a plan, references a non-tracking issue. Lint
	# doesn't fire — only tracking issues are in scope.
	mod = _import_lint()
	def fetch(repo: str, n: int) -> dict | None:
		return {"labels": ["bug"], "body": "no checkboxes here"}
	violations, errors = mod.lint(
		pr_body="archive. Refs #100\n",
		added_files=["docs/completed/some-plan.md"],
		repo="owner/repo",
		fail_open_on_lookup_error=False,
		issue_fetcher=fetch,
	)
	assert violations == []
	assert errors == []


def test_descope_heading_case_insensitive():
	# The de-scope heading match must be case-insensitive so contributors
	# don't trip on capitalisation. Both `## de-scoped phases` and
	# `### DE-SCOPED PHASES` should satisfy scenario B.
	mod = _import_lint()
	def fetch(repo: str, n: int) -> dict | None:
		return {"labels": ["ai:orchestrator-tracking"], "body": _PROJECT_2734_BODY}
	for heading in ("## de-scoped phases", "### DE-SCOPED PHASES", "## De Scoped Phases"):
		pr_body = f"Refs #2734\n\n{heading}\n\n- item\n"
		violations, _ = mod.lint(
			pr_body=pr_body,
			added_files=["docs/completed/plan.md"],
			repo="owner/repo",
			fail_open_on_lookup_error=False,
			issue_fetcher=fetch,
		)
		assert violations == [], f"heading {heading!r} should have satisfied scenario B"


def test_lookup_failure_with_fail_open_skips_violation():
	mod = _import_lint()
	def fetch(repo: str, n: int) -> dict | None:
		return None
	violations, errors = mod.lint(
		pr_body="Refs #2734\n",
		added_files=["docs/completed/plan.md"],
		repo="owner/repo",
		fail_open_on_lookup_error=True,
		issue_fetcher=fetch,
	)
	# With fail-open, no violation can be raised because we cannot
	# prove the issue is tracking-labeled.
	assert violations == []
	assert len(errors) == 1


def main() -> int:
	try:
		import sys as _sys
		_sys.stdout.reconfigure(line_buffering=True)
	except Exception:
		pass
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}", flush=True)
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}", flush=True)
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
