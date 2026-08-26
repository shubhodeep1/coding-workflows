#!/usr/bin/env python3
"""Runtime tests for _pr_json_is_issue_implementation_pr.

Background (the issue #3817 / PR #3825 incident): the poller resolves an
issue's "linked PR" via ``_issue_cross_ref_pr_number_last``, which returns the
LAST PR that cross-references the issue — including PRs that merely mention it.
PR #3825 (an unrelated fix whose body said "Refs #3817", the §19-correct
non-closing reference) was merged, became issue #3817's most recent
cross-reference, and the poller adopted it as the issue's implementation PR:
``reconcile_managed_issue_labels`` forced ``ai:merged`` and
``close_merged_issues_sweep`` closed the issue with its scope never
implemented.

The fix gates merged-state adoption on ``_pr_json_is_issue_implementation_pr``:
a candidate PR counts as the issue's implementation PR only when its head
branch is the orchestrator convention ``ai/issue-<n>`` or its body carries a
GitHub closing-keyword reference to the issue (``_pr_json_closes_issue``).

Uses the same function-extraction-plus-source pattern as
``test_retrigger_inflight_direct_fallback.py``: pull the helper definitions out
of the production script with awk and exercise them directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	full_env = os.environ.copy()
	full_env.pop("BASH_ENV", None)
	full_env.pop("ENV", None)
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=full_env,
		capture_output=True,
		text=True,
	)


_EXTRACT_FN = r"""
extract_fn() {
	local fn="$1"
	awk -v fn="${fn}" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__POLLER__"
}
"""


def _guard_rc(issue_num: str, pr_json: str) -> int:
	"""Source the extracted helpers and return the guard's exit code."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
		bootstrap = textwrap.dedent(f"""
		set -euo pipefail
		{extractor}
		: > helpers.sh
		extract_fn '_pr_json_closes_issue' >> helpers.sh
		extract_fn '_pr_json_is_issue_implementation_pr' >> helpers.sh
		""")
		r = _run_bash(bootstrap, cwd=tmp)
		assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
		body = (tmp / "helpers.sh").read_text(encoding="utf-8")
		assert "_pr_json_closes_issue()" in body, "missing _pr_json_closes_issue in helpers.sh"
		assert "_pr_json_is_issue_implementation_pr()" in body, "missing guard in helpers.sh"
		pr_json_file = tmp / "pr.json"
		pr_json_file.write_text(pr_json, encoding="utf-8")
		script = textwrap.dedent(f"""
		set -uo pipefail
		source helpers.sh
		_pr_json_is_issue_implementation_pr '{issue_num}' "$(cat pr.json)"
		""")
		r = _run_bash(script, cwd=tmp)
		return r.returncode


def _pr(head_ref: str, body: str) -> str:
	return json.dumps({
		"number": 3825,
		"state": "closed",
		"merged_at": "2026-08-25T15:18:27Z",
		"body": body,
		"head": {"ref": head_ref, "sha": "0" * 40},
		"base": {"ref": "main"},
	})


def test_conventional_head_branch_is_accepted():
	"""ai/issue-<n> head branch is the orchestrator implement convention."""
	assert _guard_rc("3816", _pr("ai/issue-3816", "Automated implementation.")) == 0


def test_closing_hash_reference_is_accepted():
	"""A non-conventional branch with a closing-keyword #N body reference
	(e.g. a claude-branch or validation-fix PR) still qualifies."""
	assert _guard_rc("3817", _pr("claude/some-fix-branch", "Closes #3817")) == 0


def test_closing_url_reference_is_accepted():
	"""The implement pipeline writes URL-form closers ("Closes https://.../issues/N")."""
	body = "Automated implementation. Closes https://github.com/shubhodeep1/coding-workflows/issues/3816\nRefs #3810\n"
	assert _guard_rc("3816", _pr("claude/other-branch", body)) == 0


def test_refs_mention_only_pr_is_rejected():
	"""The incident case: a merged PR whose body only says "Refs #3817" is a
	mention, not the issue's implementation PR, and must be rejected."""
	body = "Raise default ai-memory push-retry budget.\n\nRefs #3817\n"
	assert _guard_rc("3817", _pr("claude/ai-planning-workflow-failure-fwufit", body)) == 1


def test_closing_reference_to_other_issue_is_rejected():
	"""A closer aimed at a different issue number must not qualify."""
	assert _guard_rc("3817", _pr("claude/branch", "Closes #3816")) == 1


def test_head_branch_for_other_issue_is_rejected():
	"""ai/issue-<other> is some other issue's implementation branch."""
	assert _guard_rc("3817", _pr("ai/issue-3816", "Automated implementation.")) == 1


def test_empty_or_invalid_pr_json_is_rejected():
	"""Adopting merged state is the destructive act, so unverifiable
	candidates map to rejection (unlike the fail-closed skip-guards)."""
	assert _guard_rc("3817", "") == 1
	assert _guard_rc("3817", "{}") == 1
	assert _guard_rc("not-a-number", _pr("ai/issue-3817", "Closes #3817")) == 1


# ---------------------------------------------------------------------------
# Direct-invocation entrypoint
#
# `.github/workflows/*.yml` runs this test as `python3 tests/<file>.py` from
# explicit allowlists (no pytest discovery). Without this block, the file would
# import successfully and exit 0 without running any test_* functions.
# ---------------------------------------------------------------------------


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
