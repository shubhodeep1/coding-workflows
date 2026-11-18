#!/usr/bin/env python3
"""Contract tests for scripts/release_ci_gate.sh (Refs #6532, re-issued from #6775).

Both stable release workflows run the gate on the exact commit they release
before any changelog push or tag write. The gate must pass only when the
newest github-actions `lint` check-run of a `.github/workflows/ci.yml` run
for that SHA completed with `success`, and fail closed on everything else.

These tests drive the real script with a fake ``gh`` on PATH that serves
synthetic fixtures (no real repositories, SHAs or tokens) and records every
call. The inline cases in ci.yml's "Release CI gate tests" step also check
that both release jobs call the gate in the right order; this file covers
the gate's own decisions.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release_ci_gate.sh"

SHA = "a" * 40
OTHER_SHA = "b" * 40
TOKEN_SENTINEL = "ghp_TESTSENTINEL0000"
CI_RUN = {"head_sha": SHA, "path": ".github/workflows/ci.yml", "check_suite_id": 100}

FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
fx="${RCG_TEST_FIXTURE_DIR:?}"
printf '%s\n' "$*" >> "${fx}/calls.log"
if [ -f "${fx}/fail" ]; then
  echo "stub gh: forced failure" >&2
  exit 1
fi
case "$*" in
  *actions/workflows/ci.yml/runs*) cat "${fx}/runs.json" ;;
  *check-runs*)
    if [ -f "${fx}/fail_checks" ]; then
      echo "stub gh: forced check-runs failure" >&2
      exit 1
    fi
    # Zero-based poll index: poll N (1-based) reads checks.<N-1>.json, else checks.json.
    n=0
    if [ -f "${fx}/counter" ]; then n="$(cat "${fx}/counter")"; fi
    echo "$((n + 1))" > "${fx}/counter"
    if [ -f "${fx}/checks.${n}.json" ]; then
      cat "${fx}/checks.${n}.json"
    else
      cat "${fx}/checks.json"
    fi
    ;;
  *) echo "stub gh: unexpected call: $*" >&2; exit 1 ;;
esac
"""


def _check(run_id=1, status="completed", conclusion="success", head=SHA, app="github-actions", suite=100, name="lint"):
	return {
		"id": run_id,
		"name": name,
		"head_sha": head,
		"status": status,
		"conclusion": conclusion,
		"app": {"slug": app},
		"check_suite": {"id": suite},
	}


def _pages(key, items):
	return json.dumps([{key: items}])


def _write_case(root, case, runs, checks, extra=None):
	case_dir = root / case
	case_dir.mkdir(parents=True)
	(case_dir / "runs.json").write_text(
		runs if isinstance(runs, str) else _pages("workflow_runs", runs), encoding="utf-8"
	)
	(case_dir / "checks.json").write_text(
		checks if isinstance(checks, str) else _pages("check_runs", checks), encoding="utf-8"
	)
	for name, body in (extra or {}).items():
		(case_dir / name).write_text(body, encoding="utf-8")
	return case_dir


class ReleaseCiGateTest(unittest.TestCase):
	def setUp(self) -> None:
		self._tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self._tmp.cleanup)
		self.root = Path(self._tmp.name)
		bin_dir = self.root / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(FAKE_GH, encoding="utf-8")
		gh.chmod(0o755)
		self.fx = self.root / "fx"
		self.fx.mkdir()
		env = dict(os.environ)
		for key in ("RELEASE_CI_GATE_CHECK_NAME", "RELEASE_CI_GATE_WORKFLOW_PATH"):
			env.pop(key, None)
		env.update(
			{
				"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
				"GH_RETRY_MAX_ATTEMPTS": "1",
				"RELEASE_CI_GATE_WAIT_SECS": "0",
				"RELEASE_CI_GATE_POLL_SECS": "1",
				"RUNNER_TEMP": str(self.root),
				"PYTHONDONTWRITEBYTECODE": "1",
				"GH_TOKEN": TOKEN_SENTINEL,
			}
		)
		self.env = env

	def _case(self, case, runs=None, checks=None, extra=None):
		return _write_case(
			self.fx,
			case,
			[CI_RUN] if runs is None else runs,
			[_check()] if checks is None else checks,
			extra,
		)

	def _run(self, case, *, repo="owner/repo", sha=SHA, args=None, **env_overrides):
		env = dict(self.env)
		env["RCG_TEST_FIXTURE_DIR"] = str(self.fx / case)
		env.update(env_overrides)
		argv = ["bash", str(SCRIPT)] + (list(args) if args is not None else [repo, sha])
		proc = subprocess.run(
			argv,
			cwd=REPO_ROOT,
			env=env,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
			timeout=60,
		)
		return proc.returncode, proc.stdout

	def _assert_gate(self, case, want_rc, pattern, **kwargs):
		rc, out = self._run(case, **kwargs)
		flat = out.replace("\n", " ")
		msg = f"case={case} rc={rc} (want {want_rc}) pattern=/{pattern}/\n{out}"
		self.assertEqual(rc, want_rc, msg)
		self.assertRegex(flat, pattern, msg)
		return out

	def _check_reads(self, case):
		counter = self.fx / case / "counter"
		return int(counter.read_text().strip()) if counter.exists() else 0

	def test_pass(self) -> None:
		self._case("success")
		self._assert_gate(
			"success",
			0,
			rf"RELEASE_CI_GATE repo=owner/repo sha={SHA} outcome=pass reason=success "
			r"check_run_id=1 status=completed conclusion=success waited_secs=",
		)

	def test_pass_with_ref_suffix_path(self) -> None:
		self._case("ref", runs=[dict(CI_RUN, path=".github/workflows/ci.yml@refs/heads/stable")])
		self._assert_gate("ref", 0, r"outcome=pass reason=success")

	def test_missing_check_run_times_out(self) -> None:
		self._case("missing", runs=[], checks=[])
		self._assert_gate("missing", 1, r"outcome=fail reason=wait_timeout .*last_state=missing")

	def test_pending_single_check_times_out(self) -> None:
		self._case("pending", checks=[_check(status="in_progress", conclusion=None)])
		self._assert_gate(
			"pending", 1, r"outcome=fail reason=wait_timeout .*status=in_progress .*last_state=pending"
		)

	def test_pending_then_timeout_after_polling(self) -> None:
		self._case("pending_poll", checks=[_check(status="queued", conclusion=None)])
		self._assert_gate(
			"pending_poll",
			1,
			r"outcome=waiting reason=pending.*outcome=fail reason=wait_timeout .*last_state=pending",
			# 3s, not 2s: waited_secs uses whole-second `date +%s`, so a slow
			# first poll could otherwise time out before logging `waiting`.
			RELEASE_CI_GATE_WAIT_SECS="3",
		)
		self.assertGreaterEqual(self._check_reads("pending_poll"), 2)

	def test_pending_then_success(self) -> None:
		self._case(
			"pending_then_success",
			extra={"checks.0.json": _pages("check_runs", [_check(status="queued", conclusion=None)])},
		)
		self._assert_gate(
			"pending_then_success",
			0,
			r"outcome=waiting reason=pending.*outcome=pass reason=success",
			RELEASE_CI_GATE_WAIT_SECS="5",
		)

	def test_failed_conclusions_fail_at_once(self) -> None:
		for conclusion in ("failure", "cancelled", "skipped", "timed_out", "neutral"):
			with self.subTest(conclusion=conclusion):
				case = f"concl_{conclusion}"
				self._case(case, checks=[_check(conclusion=conclusion)])
				# A long wait budget proves a completed failure does not poll.
				self._assert_gate(
					case,
					1,
					rf"outcome=fail reason=conclusion_{conclusion} ",
					RELEASE_CI_GATE_WAIT_SECS="30",
				)
				self.assertEqual(self._check_reads(case), 1)

	def test_wrong_sha(self) -> None:
		self._case("sha_mismatch", checks=[_check(head=OTHER_SHA)])
		self._assert_gate("sha_mismatch", 1, r"outcome=fail reason=sha_mismatch")

	def test_ci_run_for_other_sha_is_not_counted(self) -> None:
		self._case("ci_other_sha", runs=[dict(CI_RUN, head_sha=OTHER_SHA)])
		self._assert_gate("ci_other_sha", 1, r"outcome=fail reason=wait_timeout .*last_state=non_actions_app")

	def test_unrelated_same_name_check_is_not_counted(self) -> None:
		variants = {
			"other_app": ([CI_RUN], [_check(app="evil-app")]),
			"other_suite": ([CI_RUN], [_check(suite=999)]),
			"other_workflow": ([dict(CI_RUN, path=".github/workflows/lint.yml")], [_check()]),
		}
		for case, (runs, checks) in variants.items():
			with self.subTest(case=case):
				self._case(case, runs=runs, checks=checks)
				self._assert_gate(case, 1, r"outcome=fail reason=wait_timeout .*last_state=non_actions_app")

	def test_unreadable_output(self) -> None:
		variants = {
			"gh_fail": {"extra": {"fail": ""}},
			"checks_fail": {"extra": {"fail_checks": ""}},
			"malformed_json": {"checks": "not json"},
			"malformed_shape": {"checks": json.dumps({"check_runs": [_check()]})},
			"page_without_key": {"checks": json.dumps([{"total_count": 1}])},
			"non_object_item": {"checks": json.dumps([{"check_runs": ["lint"]}])},
			"malformed_runs": {"runs": "not json"},
		}
		for case, spec in variants.items():
			with self.subTest(case=case):
				self._case(case, runs=spec.get("runs"), checks=spec.get("checks"), extra=spec.get("extra"))
				self._assert_gate(case, 1, r"outcome=fail reason=api_unreadable")

	def test_newest_check_run_decides(self) -> None:
		self._case("newest_cancelled", checks=[_check(run_id=10), _check(run_id=11, conclusion="cancelled")])
		self._assert_gate("newest_cancelled", 1, r"outcome=fail reason=conclusion_cancelled check_run_id=11 ")
		self._case("newest_success", checks=[_check(run_id=10, conclusion="failure"), _check(run_id=11)])
		self._assert_gate("newest_success", 0, r"outcome=pass reason=success check_run_id=11 ")

	def test_invalid_input(self) -> None:
		variants = {
			"short_sha": ["owner/repo", SHA[:39]],
			"upper_sha": ["owner/repo", OTHER_SHA.upper()],
			"shell_repo": ["owner/repo;id", SHA],
			"traversal_repo": ["../..", SHA],
			"missing_arg": ["owner/repo"],
		}
		for case, args in variants.items():
			with self.subTest(case=case):
				self._case(case)
				self._assert_gate(case, 2, r"outcome=fail reason=invalid_input", args=args)
				self.assertFalse((self.fx / case / "calls.log").exists(), "gh must not be called")

	def test_invalid_wait_falls_back_with_warning(self) -> None:
		self._case("bad_wait")
		self._assert_gate(
			"bad_wait",
			0,
			r"::warning::RELEASE_CI_GATE RELEASE_CI_GATE_WAIT_SECS.*outcome=pass",
			RELEASE_CI_GATE_WAIT_SECS="abc",
		)

	def test_api_calls_are_pinned(self) -> None:
		self._case("calls")
		rc, out = self._run("calls")
		self.assertEqual(rc, 0, out)
		calls = (self.fx / "calls" / "calls.log").read_text(encoding="utf-8").splitlines()
		self.assertEqual(
			calls,
			[
				f"api --paginate --slurp repos/owner/repo/actions/workflows/ci.yml/runs?head_sha={SHA}&per_page=100",
				f"api --paginate --slurp repos/owner/repo/commits/{SHA}/check-runs?check_name=lint&filter=all&per_page=100",
			],
		)

	def test_failure_lines_are_error_annotations_and_token_never_logged(self) -> None:
		self._case("tok_fail", checks=[_check(conclusion="failure")])
		rc, fail_out = self._run("tok_fail")
		self.assertEqual(rc, 1, fail_out)
		self.assertIn("::error::RELEASE_CI_GATE ", fail_out)
		self._case("tok_pass")
		rc, pass_out = self._run("tok_pass")
		self.assertEqual(rc, 0, pass_out)
		self.assertNotIn("::error::", pass_out)
		for out in (fail_out, pass_out):
			self.assertNotIn(TOKEN_SENTINEL, out)

	def test_log_line_carries_only_sanitized_tokens(self) -> None:
		self._case("odd_conclusion", checks=[_check(conclusion="bad value;$(id)")])
		out = self._assert_gate("odd_conclusion", 1, r"outcome=fail reason=conclusion_bad_value__")
		self.assertNotIn("$(id)", out)
		self.assertIsNone(re.search(r"conclusion=[^ ]*[;$()]", out))


if __name__ == "__main__":
	unittest.main()
