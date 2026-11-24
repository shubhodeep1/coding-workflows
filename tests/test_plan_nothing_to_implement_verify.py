#!/usr/bin/env python3
"""Nothing-to-implement planning block verify (issue #10015, Phase 8a, Refs #9650).

A plan run that ends in the planner's "work is already on the planning ref;
nothing to implement" block runs the plan's ACCEPTANCE tests (or the tests the
issue body names) on the planning ref through heal_reproduce.sh, and closes
the issue as completed when every listed test passes. These tests run
scripts/plan_nothing_to_implement_verify.sh against a stub `gh` and a stub
reproduce script (no Docker, no network), unit-test the acceptance_check.py
helpers, and check the plan.yml wiring, including the real body of
"Handle blocked planning output".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:  # CI installs PyYAML; the wiring tests skip without it.
	import yaml  # type: ignore
except ImportError:  # pragma: no cover
	yaml = None


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
SCRIPT = SCRIPTS / "plan_nothing_to_implement_verify.sh"
HELPER = SCRIPTS / "acceptance_check.py"
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
VERIFY_STEP = "Verify nothing-to-implement block"
BLOCKED_STEP = "Handle blocked planning output"
REASON = "work is already on the planning ref; nothing to implement"

sys.path.insert(0, str(SCRIPTS))
import acceptance_check  # noqa: E402

STUB_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_LOG}"
for arg in "$@"; do
	case "${arg}" in
		body=*) printf '%s' "${arg#body=}" > "${STUB_BODY}" ;;
	esac
done
if [ -n "${STUB_GH_FAIL:-}" ]; then
	case "$*" in
		*"${STUB_GH_FAIL}"*) exit 1 ;;
	esac
fi
exit 0
"""

STUB_REPRODUCE = """#!/usr/bin/env bash
printf 'args=%s\\n' "$*" >> "${STUB_REPRO_LOG}"
printf 'timeout_secs=%s\\n' "${WORKFLOW_HEAL_REPRODUCE_TIMEOUT_SECS:-}" >> "${STUB_REPRO_LOG}"
tests_file=""
out=""
while [ $# -gt 0 ]; do
	case "$1" in
		--tests-file) tests_file="$2"; shift 2 ;;
		--out) out="$2"; shift 2 ;;
		*) shift ;;
	esac
done
cat "${tests_file}" >> "${STUB_REPRO_LOG}"
if [ -n "${STUB_REPRO_RC:-}" ]; then
	exit "${STUB_REPRO_RC}"
fi
if [ -n "${STUB_REPRO_JSON:-}" ]; then
	printf '%s' "${STUB_REPRO_JSON}" > "${out}"
fi
exit 0
"""

STUB_GH_HELPERS = "gh_retry() { \"$@\"; }\n"


def _passing(tests: list[str]) -> str:
	return json.dumps({
		"schema": "heal_repro.v1", "status": "not_reproduced", "reason": "passes_at_failing_sha",
		"failing_at": "", "passing_at": "",
		"tests": [{"test": t, "status": "not_reproduced", "reason": "passes_at_failing_sha"} for t in tests],
	})


def _result(tests: list[tuple[str, str, str]], reason: str = "pre_existing") -> str:
	return json.dumps({
		"schema": "heal_repro.v1", "status": "not_reproduced", "reason": reason,
		"failing_at": "", "passing_at": "",
		"tests": [{"test": t, "status": s, "reason": r} for t, s, r in tests],
	})


def _clean_git_env() -> dict:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update({
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
		"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
	})
	return env


def _plan_with_acceptance(*targets: str) -> str:
	lines = ["Implementation Plan", "", "## Files likely to change", "- `scripts/x.py`", "", "## Acceptance", ""]
	lines += [f"ACCEPTANCE: test {t}" for t in targets]
	lines += ["ACCEPTANCE: post-merge none: already delivered", "", "BLOCKED: " + REASON]
	return "\n".join(lines) + "\n"


class ScriptHarness:
	def __init__(self, root: Path) -> None:
		self.root = root
		self.repo = root / "repo"
		(self.repo / "tests").mkdir(parents=True)
		for name in ("test_ok.py", "test_other.py", "test_third.py"):
			(self.repo / "tests" / name).write_text("def test_x():\n\tassert True\n", encoding="utf-8")
		env = _clean_git_env()
		for cmd in (["init", "-q"], ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"]):
			subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, env=env, capture_output=True)
		self.head = subprocess.run(
			["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, env=env, capture_output=True, text=True,
		).stdout.strip()
		self.scripts = root / "scripts"
		self.scripts.mkdir()
		shutil.copy2(SCRIPT, self.scripts / SCRIPT.name)
		(self.scripts / "gh_helpers.sh").write_text(STUB_GH_HELPERS, encoding="utf-8")
		self.bin = root / "bin"
		self.bin.mkdir()
		gh = self.bin / "gh"
		gh.write_text(STUB_GH, encoding="utf-8")
		gh.chmod(0o755)
		self.reproduce = root / "reproduce.sh"
		self.reproduce.write_text(STUB_REPRODUCE, encoding="utf-8")
		self.reproduce.chmod(0o755)
		self.runner_temp = root / "runner_temp"
		self.runner_temp.mkdir()

	def run(self, *, reason: str = REASON, plan: str = "", issue_body: str = "", enabled: str | None = None,
			repro_json: str | None = None, repro_rc: str = "", gh_fail: str = "", reproduce: Path | None = None) -> dict:
		plan_file = self.root / "plan.txt"
		plan_file.write_text(plan, encoding="utf-8")
		issue_file = self.root / "issue.json"
		issue_file.write_text(json.dumps({"number": 42, "body": issue_body}), encoding="utf-8")
		output = self.root / "github_output"
		for name in ("github_output", "gh.log", "body.md", "repro.log"):
			(self.root / name).unlink(missing_ok=True)
		env = _clean_git_env()
		env.pop("PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED", None)
		env.update({
			"PATH": f"{self.bin}:{os.environ['PATH']}",
			"ISSUE_NUMBER": "42",
			"BLOCKED_REASON": reason,
			"CODEX_OUTPUT_FILE": str(plan_file),
			"ISSUE_META_FILE": str(issue_file),
			"REPOSITORY": "o/r",
			"REPO_DIR": str(self.repo),
			"RUN_URL": "https://github.com/o/r/actions/runs/1",
			"PLAN_PROGRESS_COMMENT_ID": "777",
			"GITHUB_OUTPUT": str(output),
			"RUNNER_TEMP": str(self.runner_temp),
			"PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ACCEPTANCE_HELPER": str(HELPER),
			"PLAN_NOTHING_TO_IMPLEMENT_VERIFY_REPRODUCE_SCRIPT": str(reproduce or self.reproduce),
			"STUB_LOG": str(self.root / "gh.log"),
			"STUB_BODY": str(self.root / "body.md"),
			"STUB_REPRO_LOG": str(self.root / "repro.log"),
			"STUB_REPRO_JSON": repro_json or "",
			"STUB_REPRO_RC": repro_rc,
			"STUB_GH_FAIL": gh_fail,
			"PYTHONDONTWRITEBYTECODE": "1",
		})
		if enabled is not None:
			env["PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED"] = enabled
		result = subprocess.run(
			["bash", str(self.scripts / SCRIPT.name)], cwd=self.root, env=env, capture_output=True, text=True, timeout=120,
		)

		def read(name: str) -> str:
			path = self.root / name
			return path.read_text(encoding="utf-8") if path.exists() else ""

		outputs = {}
		for line in read("github_output").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value
		return {
			"rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "outputs": outputs,
			"gh": read("gh.log"), "body": read("body.md"), "repro": read("repro.log"),
		}


class PlanNothingToImplementVerifyScriptTests(unittest.TestCase):
	def setUp(self) -> None:
		self._tmp = tempfile.TemporaryDirectory()
		self.h = ScriptHarness(Path(self._tmp.name))

	def tearDown(self) -> None:
		self._tmp.cleanup()

	def _log_line(self, stdout: str) -> str:
		lines = [line for line in stdout.splitlines() if line.startswith("PLAN_NOTHING_TO_IMPLEMENT ")]
		self.assertEqual(len(lines), 1, stdout)
		return lines[0]

	def test_passing_tests_close_the_issue(self) -> None:
		r = self.h.run(plan=_plan_with_acceptance("tests/test_ok.py"), repro_json=_passing(["tests/test_ok.py"]))
		self.assertEqual(r["rc"], 0, r["stderr"])
		self.assertEqual(self._log_line(r["stdout"]), "PLAN_NOTHING_TO_IMPLEMENT issue=42 outcome=closed reason=passed tests=1")
		self.assertEqual(r["outputs"]["outcome"], "closed")
		self.assertEqual(r["outputs"]["note"], "Verify: 1 test(s) passed on the planning ref; issue closed as completed.")
		self.assertIn("<!-- ai:implement-success-noop:v1 issue=42 -->", r["body"])
		self.assertIn("Already implemented — verified.", r["body"])
		self.assertIn("- `tests/test_ok.py`", r["body"])
		self.assertIn("Run: https://github.com/o/r/actions/runs/1", r["body"])
		gh_lines = r["gh"].splitlines()
		self.assertTrue(gh_lines[0].startswith("api repos/o/r/issues/42/comments"), gh_lines)
		self.assertTrue(any(line.startswith("label create ai:closed") for line in gh_lines), gh_lines)
		edit = next(line for line in gh_lines if line.startswith("issue edit 42"))
		self.assertIn("--remove-label ai:blocked", edit)
		self.assertIn("--remove-label ai:planning", edit)
		self.assertIn("--add-label ai:closed", edit)
		self.assertIn("issue close 42 --repo o/r --reason completed", r["gh"])
		self.assertIn("api -X DELETE repos/o/r/issues/comments/777", r["gh"])
		# Order: comment, labels, close.
		order = [i for i, line in enumerate(gh_lines) if line.startswith(("api repos", "issue edit", "issue close"))]
		kinds = [gh_lines[i].split()[1] if gh_lines[i].startswith("issue") else "comment" for i in order]
		self.assertEqual(kinds[0], "comment")
		self.assertLess(kinds.index("edit"), kinds.index("close"))
		# Planning HEAD is both tested commit and baseline; the 600 s cap is set.
		self.assertIn(f"--sha {self.h.head} --baseline {self.h.head}", r["repro"])
		self.assertIn(f"--repo {self.h.repo}", r["repro"])
		self.assertIn("timeout_secs=600", r["repro"])
		self.assertEqual(list(self.h.runner_temp.iterdir()), [])

	def test_incident_9472_shape_closes(self) -> None:
		plan = _plan_with_acceptance("tests/test_ok.py::test_x", "tests/test_other.py")
		r = self.h.run(plan=plan, repro_json=_passing(["tests/test_ok.py::test_x", "tests/test_other.py"]))
		self.assertIn("outcome=closed reason=passed tests=2", r["stdout"])
		self.assertIn("tests/test_ok.py::test_x\ntests/test_other.py\n", r["repro"])

	def test_failing_test_keeps_the_block(self) -> None:
		for status, reason in (("not_reproduced", "pre_existing"), ("reproduced", "regression")):
			with self.subTest(reason=reason):
				r = self.h.run(
					plan=_plan_with_acceptance("tests/test_ok.py"),
					repro_json=_result([("tests/test_ok.py", status, reason)]),
				)
				self.assertEqual(self._log_line(r["stdout"]), "PLAN_NOTHING_TO_IMPLEMENT issue=42 outcome=blocked reason=failed tests=1")
				self.assertEqual(r["outputs"]["outcome"], "blocked")
				self.assertEqual(
					r["outputs"]["note"],
					"Verify: 1 listed test(s) did not all pass on the planning ref (failed); the block stays.",
				)
				self.assertEqual(r["gh"], "")

	def test_unverified_result_is_failed_with_detail(self) -> None:
		r = self.h.run(
			plan=_plan_with_acceptance("tests/test_ok.py"),
			repro_json=_result([("tests/test_ok.py", "unverified", "image_unavailable")], reason="image_unavailable"),
		)
		self.assertIn("outcome=blocked reason=failed tests=1 detail=image_unavailable", r["stdout"])
		self.assertEqual(r["gh"], "")

	def test_one_failing_test_among_passing_keeps_the_block(self) -> None:
		r = self.h.run(
			plan=_plan_with_acceptance("tests/test_ok.py", "tests/test_other.py"),
			repro_json=_result([
				("tests/test_ok.py", "not_reproduced", "passes_at_failing_sha"),
				("tests/test_other.py", "not_reproduced", "pre_existing"),
			]),
		)
		self.assertIn("outcome=blocked reason=failed tests=2", r["stdout"])
		self.assertEqual(r["gh"], "")

	def test_count_mismatch_is_failed(self) -> None:
		r = self.h.run(
			plan=_plan_with_acceptance("tests/test_ok.py", "tests/test_other.py"),
			repro_json=_passing(["tests/test_ok.py"]),
		)
		self.assertIn("outcome=blocked reason=failed tests=2 detail=count_mismatch", r["stdout"])
		self.assertEqual(r["gh"], "")

	def test_missing_or_invalid_result_is_failed(self) -> None:
		for repro in ("", "not json", json.dumps({"schema": "other"})):
			with self.subTest(repro=repro):
				r = self.h.run(plan=_plan_with_acceptance("tests/test_ok.py"), repro_json=repro)
				self.assertIn("outcome=blocked reason=failed tests=1", r["stdout"])
				self.assertEqual(r["gh"], "")

	def test_no_tests_keeps_the_block_without_running_the_gate(self) -> None:
		r = self.h.run(plan="BLOCKED: " + REASON + "\n", issue_body="Nothing named here; tests/missing.py does not exist.")
		self.assertEqual(self._log_line(r["stdout"]), "PLAN_NOTHING_TO_IMPLEMENT issue=42 outcome=blocked reason=no_tests tests=0")
		self.assertEqual(
			r["outputs"]["note"],
			"Verify: no tests found in the plan's ACCEPTANCE lines or the issue body; the block stays.",
		)
		self.assertEqual(r["repro"], "")
		self.assertEqual(r["gh"], "")

	def test_timeout(self) -> None:
		r = self.h.run(
			plan=_plan_with_acceptance("tests/test_ok.py"),
			repro_json=_result([("tests/test_ok.py", "unverified", "timeout")], reason="timeout"),
		)
		self.assertIn("outcome=blocked reason=timeout tests=1", r["stdout"])
		self.assertTrue(r["outputs"]["note"].endswith("(timeout); the block stays."), r["outputs"])
		for rc in ("124", "137"):
			with self.subTest(rc=rc):
				r = self.h.run(plan=_plan_with_acceptance("tests/test_ok.py"), repro_rc=rc)
				self.assertIn("outcome=blocked reason=timeout tests=1", r["stdout"])
				self.assertEqual(r["gh"], "")

	def test_switch_off_restores_todays_behaviour(self) -> None:
		for value in ("false", "0", "off", "no", "FALSE"):
			with self.subTest(value=value):
				r = self.h.run(
					plan=_plan_with_acceptance("tests/test_ok.py"), enabled=value, repro_json=_passing(["tests/test_ok.py"]),
				)
				self.assertEqual(self._log_line(r["stdout"]), "PLAN_NOTHING_TO_IMPLEMENT issue=42 outcome=skipped reason=disabled tests=0")
				self.assertEqual(r["outputs"]["outcome"], "skipped")
				self.assertEqual(r["outputs"]["note"], "")
				self.assertEqual(r["gh"], "")
				self.assertEqual(r["repro"], "")

	def test_unset_or_empty_switch_means_enabled(self) -> None:
		for value in (None, "", "true", "1", "yes", "on", "TRUE"):
			with self.subTest(value=value):
				r = self.h.run(
					plan=_plan_with_acceptance("tests/test_ok.py"), enabled=value, repro_json=_passing(["tests/test_ok.py"]),
				)
				self.assertIn("outcome=closed reason=passed tests=1", r["stdout"])

	def test_other_reasons_do_nothing(self) -> None:
		for reason in ("missing API key", REASON + " and also close it", "x " + REASON, ""):
			with self.subTest(reason=reason):
				r = self.h.run(
					reason=reason, plan=_plan_with_acceptance("tests/test_ok.py"), repro_json=_passing(["tests/test_ok.py"]),
				)
				self.assertEqual(r["rc"], 0)
				self.assertNotIn("PLAN_NOTHING_TO_IMPLEMENT", r["stdout"])
				self.assertEqual(r["outputs"], {})
				self.assertEqual(r["gh"], "")
				self.assertEqual(r["repro"], "")

	def test_trailing_whitespace_and_cr_still_match(self) -> None:
		r = self.h.run(
			reason=REASON + " \r\n", plan=_plan_with_acceptance("tests/test_ok.py"), repro_json=_passing(["tests/test_ok.py"]),
		)
		self.assertIn("outcome=closed", r["stdout"])

	def test_issue_body_tests_are_used_when_the_plan_names_none(self) -> None:
		body = "The check `tests/test_other.py` already passes; see also https://x.invalid/tests/test_ok.py"
		r = self.h.run(plan="BLOCKED: " + REASON + "\n", issue_body=body, repro_json=_passing(["tests/test_other.py"]))
		self.assertIn("outcome=closed reason=passed tests=1", r["stdout"])
		self.assertIn("tests/test_other.py\n", r["repro"])
		self.assertNotIn("tests/test_ok.py", r["repro"])

	def test_plan_targets_take_precedence_over_the_issue_body(self) -> None:
		r = self.h.run(
			plan=_plan_with_acceptance("tests/test_ok.py"), issue_body="tests/test_other.py",
			repro_json=_passing(["tests/test_ok.py"]),
		)
		self.assertIn("outcome=closed reason=passed tests=1", r["stdout"])
		self.assertIn("tests/test_ok.py\n", r["repro"])
		self.assertNotIn("tests/test_other.py", r["repro"])

	def test_comment_failure_keeps_the_block(self) -> None:
		r = self.h.run(
			plan=_plan_with_acceptance("tests/test_ok.py"), repro_json=_passing(["tests/test_ok.py"]), gh_fail="/comments",
		)
		self.assertIn("outcome=blocked reason=passed tests=1 detail=comment_failed", r["stdout"])
		self.assertEqual(r["outputs"]["outcome"], "blocked")
		self.assertNotIn("issue close", r["gh"])
		self.assertNotIn("issue edit", r["gh"])

	def test_close_failure_keeps_the_block_after_the_marker(self) -> None:
		r = self.h.run(
			plan=_plan_with_acceptance("tests/test_ok.py"), repro_json=_passing(["tests/test_ok.py"]), gh_fail="issue close",
		)
		self.assertIn("outcome=blocked reason=passed tests=1 detail=close_failed", r["stdout"])
		self.assertEqual(r["outputs"]["outcome"], "blocked")
		self.assertIn("<!-- ai:implement-success-noop:v1 issue=42 -->", r["body"])
		# The open issue must not keep the terminal ai:closed label.
		gh_lines = r["gh"].splitlines()
		close_at = next(i for i, line in enumerate(gh_lines) if line.startswith("issue close 42"))
		self.assertIn("issue edit 42 --repo o/r --remove-label ai:closed", gh_lines[close_at + 1:])

	def test_missing_reproduce_script_is_failed(self) -> None:
		r = self.h.run(
			plan=_plan_with_acceptance("tests/test_ok.py"), reproduce=self.h.root / "absent.sh",
		)
		self.assertIn("outcome=blocked reason=failed tests=1 detail=support_missing", r["stdout"])
		self.assertEqual(r["gh"], "")

	def test_test_names_with_backticks_are_sanitized(self) -> None:
		target = "tests/test_ok.py::test_`x`"
		# acceptance_check refuses backticks in names, so the name never
		# reaches the comment; the comment code span stays intact.
		r = self.h.run(plan=_plan_with_acceptance(target), repro_json=_passing([target]))
		self.assertIn("outcome=blocked reason=no_tests", r["stdout"])
		self.assertEqual(r["gh"], "")

	def test_comment_list_strips_backticks_from_targets(self) -> None:
		# Even if a target with a backtick reached the tests file, the comment
		# renders it without one.
		script = (self.h.scripts / SCRIPT.name).read_text(encoding="utf-8")
		self.assertIn("tr -d '`\\000-\\037\\177'", script)

	def test_script_defaults_every_variable(self) -> None:
		text = SCRIPT.read_text(encoding="utf-8")
		self.assertIn('"${PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED:-true}"', text)
		self.assertIn('${PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ACCEPTANCE_HELPER:-${pnti_dir}/acceptance_check.py}', text)
		self.assertIn('${PLAN_NOTHING_TO_IMPLEMENT_VERIFY_REPRODUCE_SCRIPT:-${pnti_dir}/heal_reproduce.sh}', text)
		self.assertIn('${REPOSITORY:-${GITHUB_REPOSITORY:-}}', text)
		self.assertIn('${REPO_DIR:-${GITHUB_WORKSPACE:-$PWD}}', text)
		self.assertIn('PNTI_REASON_LITERAL="' + REASON + '"', text)
		unblock = (SCRIPTS / "unblock_judge.sh").read_text(encoding="utf-8")
		self.assertIn(REASON, unblock)


class ExtractIssueTestPathsTests(unittest.TestCase):
	def setUp(self) -> None:
		self._tmp = tempfile.TemporaryDirectory()
		self.root = Path(self._tmp.name)
		(self.root / "tests").mkdir()
		for i in range(12):
			(self.root / "tests" / f"test_{i}.py").write_text("", encoding="utf-8")
		(self.root / "outside.py").write_text("", encoding="utf-8")

	def tearDown(self) -> None:
		self._tmp.cleanup()

	def test_existing_paths_in_order_without_duplicates(self) -> None:
		text = "Run tests/test_2.py and `tests/test_1.py::test_a`, then tests/test_2.py again."
		self.assertEqual(acceptance_check.extract_issue_test_paths(text, self.root), ["tests/test_2.py", "tests/test_1.py"])

	def test_missing_symlinked_and_traversal_paths_are_dropped(self) -> None:
		(self.root / "tests" / "test_link.py").symlink_to(self.root / "outside.py")
		text = "tests/test_missing.py tests/test_link.py tests/../outside.py tests/./test_1.py tests/test_0.py"
		self.assertEqual(acceptance_check.extract_issue_test_paths(text, self.root), ["tests/test_0.py"])

	def test_cap_is_ten(self) -> None:
		text = " ".join(f"tests/test_{i}.py" for i in range(12))
		paths = acceptance_check.extract_issue_test_paths(text, self.root)
		self.assertEqual(len(paths), 10)
		self.assertEqual(paths[0], "tests/test_0.py")

	def test_no_match_inside_urls_or_longer_paths(self) -> None:
		text = (
			"https://github.com/o/r/blob/main/tests/test_1.py foo/tests/test_2.py "
			"tests/test_3.pyc tests/test_4.py.bak xtests/test_5.py"
		)
		self.assertEqual(acceptance_check.extract_issue_test_paths(text, self.root), [])

	def test_non_string_input(self) -> None:
		self.assertEqual(acceptance_check.extract_issue_test_paths(None, self.root), [])

	def test_verify_targets_cli(self) -> None:
		plan = self.root / "plan.txt"
		plan.write_text(_plan_with_acceptance("tests/test_1.py", "tests/test_missing.py", "tests/test_1.py"), encoding="utf-8")
		issue = self.root / "issue.json"
		issue.write_text(json.dumps({"body": "tests/test_2.py"}), encoding="utf-8")
		out = self.root / "out.json"
		rc = acceptance_check.main([
			"verify-targets", "--plan-file", str(plan), "--issue-json", str(issue),
			"--repo-root", str(self.root), "--out", str(out),
		])
		self.assertEqual(rc, 0)
		self.assertEqual(
			json.loads(out.read_text(encoding="utf-8")),
			{"schema": "plan_verify_targets.v1", "source": "plan", "tests": ["tests/test_1.py"]},
		)
		plan.write_text("no acceptance\n", encoding="utf-8")
		acceptance_check.main([
			"verify-targets", "--plan-file", str(plan), "--issue-json", str(issue),
			"--repo-root", str(self.root), "--out", str(out),
		])
		self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["source"], "issue")
		issue.write_text("not json", encoding="utf-8")
		acceptance_check.main([
			"verify-targets", "--plan-file", str(plan), "--issue-json", str(issue),
			"--repo-root", str(self.root), "--out", str(out),
		])
		self.assertEqual(
			json.loads(out.read_text(encoding="utf-8")), {"schema": "plan_verify_targets.v1", "source": "none", "tests": []},
		)
		rc = acceptance_check.main([
			"verify-targets", "--plan-file", str(self.root / "absent.txt"), "--issue-json", str(issue),
			"--repo-root", str(self.root), "--out", str(out),
		])
		self.assertEqual(rc, 2)


def _steps() -> list[dict]:
	workflow = yaml.safe_load(PLAN_WF.read_text(encoding="utf-8"))
	return workflow["jobs"]["plan"]["steps"]


def _step(name: str) -> dict:
	for step in _steps():
		if step.get("name") == name:
			return step
	raise AssertionError(f"step not found: {name!r}")


def _render(script: str) -> str:
	replacements = {
		"${{ github.repository }}": "o/r",
		"${{ github.server_url }}": "https://github.com",
		"${{ github.run_id }}": "123",
	}
	for expression, value in replacements.items():
		script = script.replace(expression, value)
	leftover = re.findall(r"\$\{\{[^}]*\}\}", script)
	assert not leftover, leftover
	return script


@unittest.skipIf(yaml is None, "PyYAML not installed")
class PlanWorkflowWiringTests(unittest.TestCase):
	def test_verify_step_wiring(self) -> None:
		names = [step.get("name") for step in _steps()]
		self.assertLess(names.index("Validate done-check (acceptance)"), names.index(VERIFY_STEP))
		self.assertEqual(names.index(VERIFY_STEP) + 1, names.index(BLOCKED_STEP))
		step = _step(VERIFY_STEP)
		self.assertEqual(step["id"], "nothing_to_implement_verify")
		self.assertEqual(step["if"], "env.SKIP_PLAN != 'true' && steps.parse_plan.outputs.blocked == 'true'")
		self.assertIs(step["continue-on-error"], True)
		self.assertEqual(
			step["env"]["PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED"],
			"${{ vars.PLAN_NOTHING_TO_IMPLEMENT_VERIFY_ENABLED || 'true' }}",
		)
		self.assertEqual(step["env"]["BLOCKED_REASON"], "${{ steps.parse_plan.outputs.blocked_reason }}")
		self.assertIn(".codex-workflow-src/scripts/plan_nothing_to_implement_verify.sh", step["run"])
		self.assertNotIn("${{", step["run"])

	def test_blocked_step_skips_on_close_and_reads_the_note(self) -> None:
		step = _step(BLOCKED_STEP)
		self.assertIn("steps.nothing_to_implement_verify.outputs.outcome != 'closed'", step["if"])
		self.assertEqual(
			step["env"]["PLAN_NOTHING_TO_IMPLEMENT_VERIFY_NOTE"], "${{ steps.nothing_to_implement_verify.outputs.note }}",
		)

	def _run_verify_step(self, reason: str) -> str:
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			(root / "step.sh").write_text(_render(_step(VERIFY_STEP)["run"]), encoding="utf-8")
			env = {**os.environ, "GITHUB_WORKSPACE": str(root), "BLOCKED_REASON": reason, "ISSUE_NUMBER": "42"}
			result = subprocess.run(["bash", str(root / "step.sh")], cwd=root, env=env, capture_output=True, text=True, timeout=60)
			self.assertEqual(result.returncode, 0, result.stderr)
			return result.stdout

	def test_verify_step_without_the_helper_skips(self) -> None:
		self.assertEqual(
			self._run_verify_step(REASON).strip(),
			"PLAN_NOTHING_TO_IMPLEMENT issue=42 outcome=skipped reason=helper_absent tests=0",
		)
		self.assertEqual(self._run_verify_step("missing API key"), "")

	def _run_blocked_step(self, reason: str, note: str | None) -> str:
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			scripts_dir = root / "scripts"
			scripts_dir.mkdir()
			(scripts_dir / "gh_helpers.sh").write_text(STUB_GH_HELPERS, encoding="utf-8")
			(scripts_dir / "label_helpers.sh").write_text("ensure_label_exists() { :; }\n", encoding="utf-8")
			(scripts_dir / "tg_helpers.sh").write_text("tg_send_tracked() { :; }\n", encoding="utf-8")
			bin_dir = root / "bin"
			bin_dir.mkdir()
			gh = bin_dir / "gh"
			gh.write_text(STUB_GH, encoding="utf-8")
			gh.chmod(0o755)
			(root / "step.sh").write_text(_render(_step(BLOCKED_STEP)["run"]), encoding="utf-8")
			env = {
				**os.environ,
				"PATH": f"{bin_dir}:{os.environ['PATH']}",
				"BLOCKED_REASON": reason,
				"ISSUE_NUMBER": "42",
				"ISSUE_URL": "https://github.com/o/r/issues/42",
				"PLAN_COMMENT_FILE": str(root / "comment.md"),
				"PLAN_PROGRESS_COMMENT_ID": "",
				"STUB_LOG": str(root / "gh.log"),
				"STUB_BODY": str(root / "body.md"),
				"STUB_GH_FAIL": "",
			}
			env.pop("PLAN_NOTHING_TO_IMPLEMENT_VERIFY_NOTE", None)
			if note is not None:
				env["PLAN_NOTHING_TO_IMPLEMENT_VERIFY_NOTE"] = note
			result = subprocess.run(["bash", str(root / "step.sh")], cwd=root, env=env, capture_output=True, text=True, timeout=60)
			self.assertEqual(result.returncode, 0, result.stderr)
			return (root / "body.md").read_text(encoding="utf-8")

	def test_blocked_comment_carries_the_verify_note(self) -> None:
		note = "Verify: 1 listed test(s) did not all pass on the planning ref (failed); the block stays."
		body = self._run_blocked_step(REASON, note)
		lines = body.splitlines()
		self.assertEqual(lines[0], "Planning blocked: human input required.")
		self.assertEqual(lines[2], f"Reason: {REASON}")
		self.assertEqual(lines[4], note)
		self.assertEqual(lines[5], "")

	def test_blocked_comment_without_a_note_is_unchanged(self) -> None:
		expected = (
			"Planning blocked: human input required.\n\n"
			f"Reason: {REASON}\n\n"
			"Auto-answer has been paused to avoid unsatisfiable clarification loops.\n"
			"Provide the required input, then reply with /answer.\n\n"
			"Run: https://github.com/o/r/actions/runs/123"
		)
		self.assertEqual(self._run_blocked_step(REASON, None), expected)
		self.assertEqual(self._run_blocked_step(REASON, ""), expected)


if __name__ == "__main__":
	unittest.main()
