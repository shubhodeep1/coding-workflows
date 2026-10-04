#!/usr/bin/env python3
"""Security contract tests for the reusable check-failure triage workflow."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "check_failure_triage.yml"


def _workflow() -> dict:
	return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _step(job: dict, *, step_id: str | None = None, name: str | None = None) -> dict:
	for candidate in job["steps"]:
		if step_id is not None and candidate.get("id") == step_id:
			return candidate
		if name is not None and candidate.get("name") == name:
			return candidate
	raise AssertionError(f"workflow step not found: id={step_id!r}, name={name!r}")


def _write_executable(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(0o755)


def _run_prerequisite(
	*,
	pr_number: str = "17",
	check_run_id: str = "29",
	check_name: str = "CI / lint",
	check_conclusion: str = "failure",
	head_sha: str = "a" * 40,
	details_url: str = "https://github.com/owner/repo/actions/runs/1",
	gh_mode: str = "same_repo",
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], int]:
	job = _workflow()["jobs"]["derive_check_name_key"]
	script = _step(job, step_id="hash_check_name")["run"]
	temp_dir = tempfile.TemporaryDirectory(prefix="check-triage-security-")
	temp_path = Path(temp_dir.name)
	bin_dir = temp_path / "bin"
	bin_dir.mkdir()
	call_count_path = temp_path / "gh-call-count"
	output_path = temp_path / "github-output"
	_write_executable(
		bin_dir / "gh",
		"""#!/usr/bin/env bash
set -euo pipefail
count=0
if [ -f "${MOCK_GH_CALL_COUNT}" ]; then
  count="$(cat "${MOCK_GH_CALL_COUNT}")"
fi
printf '%s' "$((count + 1))" > "${MOCK_GH_CALL_COUNT}"
case "${MOCK_GH_MODE}" in
  same_repo) printf '%s\n' '{"head":{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","repo":{"full_name":"owner/repo"}}}' ;;
  fork) printf '%s\n' '{"head":{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","repo":{"full_name":"fork/repo"}}}' ;;
  missing_repo) printf '%s\n' '{"head":{"repo":null}}' ;;
  permanent) printf '%s\n' 'gh: Not Found (HTTP 404)' >&2; exit 1 ;;
  transient) printf '%s\n' 'gh: upstream failure (HTTP 500)' >&2; exit 1 ;;
  *) exit 2 ;;
esac
""",
	)
	_write_executable(bin_dir / "sleep", "#!/usr/bin/env bash\nexit 0\n")
	env = os.environ.copy()
	env.pop("BASH_ENV", None)
	env.pop("ENV", None)
	env.update(
		{
			"CHECK_CONCLUSION": check_conclusion,
			"CHECK_NAME": check_name,
			"CHECK_RUN_ID": check_run_id,
			"DETAILS_URL": details_url,
			"GH_TOKEN": "minimal-token",
			"GITHUB_OUTPUT": str(output_path),
			"HEAD_SHA": head_sha,
			"MOCK_GH_CALL_COUNT": str(call_count_path),
			"MOCK_GH_MODE": gh_mode,
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"PR_NUMBER": pr_number,
			"REPOSITORY": "owner/repo",
		}
	)
	proc = subprocess.run(
		["bash", "--noprofile", "--norc", "-c", script],
		cwd=REPO_ROOT,
		env=env,
		capture_output=True,
		text=True,
		encoding="utf-8",
	)
	outputs: dict[str, str] = {}
	if output_path.exists():
		for line in output_path.read_text(encoding="utf-8").splitlines():
			key, value = line.split("=", 1)
			outputs[key] = value
	call_count = int(call_count_path.read_text(encoding="utf-8")) if call_count_path.exists() else 0
	temp_dir.cleanup()
	return proc, outputs, call_count


class CheckFailureTriageWorkflowSecurityTests(unittest.TestCase):
	def test_publisher_rejects_bad_artifacts_and_moved_pr(self) -> None:
		with tempfile.TemporaryDirectory(prefix="triage-publish-") as directory:
			root = Path(directory)
			(root / "scripts").mkdir()
			(root / "scripts/workflow_failure_heal.py").write_text(
				"def redact_secrets(text): return text\n", encoding="utf-8"
			)
			bin_dir = root / "bin"
			bin_dir.mkdir()
			artifact = root / "diagnosis.md"
			writes = root / "writes"
			_write_executable(bin_dir / "gh", """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *'/pulls/17'* ]]; then
  printf '{"state":"open","head":{"ref":"%s","sha":"%s","repo":{"full_name":"owner/repo"}}}\n' "${MOCK_HEAD_REF:-feature}" "${MOCK_HEAD_SHA}"
elif [[ "$*" == *'/issues/42'* ]]; then
  echo '{"body":"<!-- check-failure-triage:gen=3 -->"}'
elif [[ "$*" == *'--paginate'* ]]; then
  if [ -n "${MOCK_DUPLICATE_FP:-}" ]; then
    printf '[{"number":45,"body":"fp=%s"}]\n' "${MOCK_DUPLICATE_FP}"
  else
    printf '[]\n'
  fi
elif [[ "$*" == *'label create'* || "$*" == *'issue create'* || "$*" == *'issue edit'* ]]; then
  printf '%s\n' "$*" >> "${MOCK_WRITES}"
  if [[ "$*" == *'issue create'* ]]; then printf 'https://github.com/owner/repo/issues/42\n'; fi
else
  exit 1
fi
""")
			env = os.environ.copy()
			env.pop("BASH_ENV", None)
			env.pop("ENV", None)
			env.update({
				"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
				"MOCK_HEAD_SHA": "a" * 40, "MOCK_WRITES": str(writes),
				"GITHUB_REPOSITORY": "owner/repo", "RUNTIME_DIR": str(root / "runtime"),
				"CHECK_TRIAGE_PR_NUMBER": "17", "CHECK_TRIAGE_CHECK_NAME": "CI / lint",
				"CHECK_TRIAGE_CHECK_CONCLUSION": "failure",
				"CHECK_TRIAGE_HEAD_SHA": "a" * 40,
				"CHECK_TRIAGE_DIAG_FILE": str(artifact),
			})
			def publish() -> subprocess.CompletedProcess[str]:
				return subprocess.run(
					["bash", str(REPO_ROOT / "scripts/check_failure_triage.sh"), "publish"],
					cwd=root, env=env, capture_output=True, text=True,
				)
			for kind in ("missing", "empty", "directory", "symlink", "hardlink", "oversize", "malformed", "non_utf8"):
				with self.subTest(kind=kind):
					if artifact.is_symlink() or artifact.is_file():
						artifact.unlink()
					elif artifact.is_dir():
						artifact.rmdir()
					if kind == "directory":
						artifact.mkdir()
					elif kind == "symlink":
						artifact.symlink_to(root / "runtime/pr_payload.json")
					elif kind == "hardlink":
						other = root / "second-link.md"
						other.write_text("## Summary\n\nBad link.\n", encoding="utf-8")
						os.link(other, artifact)
					elif kind == "non_utf8":
						artifact.write_bytes(b"## Summary\n\xff\n")
					elif kind != "missing":
						artifact.write_text({
							"empty": "", "oversize": "## Summary\n" + "X" * 65536,
							"malformed": "unstructured output",
						}[kind], encoding="utf-8")
					self.assertNotEqual(publish().returncode, 0)
					self.assertFalse(writes.exists(), kind)
			if artifact.is_dir():
				artifact.rmdir()
			elif artifact.is_symlink() or artifact.is_file():
				artifact.unlink()
			artifact.write_text("## Summary\n\nValid diagnosis.\n", encoding="utf-8")
			env["MOCK_HEAD_SHA"] = "b" * 40
			self.assertNotEqual(publish().returncode, 0)
			self.assertFalse(writes.exists())
			env["MOCK_HEAD_SHA"] = "a" * 40
			result = publish()
			self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
			self.assertIn("issue create", writes.read_text(encoding="utf-8"))
			writes.unlink()
			env["MOCK_DUPLICATE_FP"] = hashlib.sha256(
				b"owner/repo|pr=17|check=CI / lint"
			).hexdigest()
			self.assertEqual(publish().returncode, 0)
			self.assertFalse(writes.exists(), "duplicate issue must suppress publication")
			env.pop("MOCK_DUPLICATE_FP")
			env["MOCK_HEAD_REF"] = "ai/issue-42"
			self.assertEqual(publish().returncode, 0)
			self.assertIn("issue edit", writes.read_text(encoding="utf-8"))
			self.assertNotIn("issue create", writes.read_text(encoding="utf-8"))

	def test_workflow_contract_gates_secrets_behind_minimal_prerequisite(self) -> None:
		workflow = _workflow()
		jobs = workflow["jobs"]
		derive_job = jobs["derive_check_name_key"]
		triage_job = jobs["triage"]

		self.assertEqual(derive_job["permissions"], {"pull-requests": "read"})
		self.assertEqual(triage_job["permissions"]["contents"], "read")
		self.assertEqual(triage_job["permissions"]["actions"], "read")
		self.assertEqual(_workflow()["jobs"]["publish_triage"]["needs"], ["derive_check_name_key", "triage"])
		self.assertNotIn("GH_TOKEN", _step(triage_job, name="Run check-failure triage")["env"])
		self.assertIn("GH_TOKEN", _step(_workflow()["jobs"]["publish_triage"], name="Publish verified triage")["env"])
		self.assertEqual(_step(triage_job, name="Checkout PR head (failing branch)")["with"]["persist-credentials"], False)
		self.assertEqual(_step(triage_job, name="Checkout workflow support source")["with"]["persist-credentials"], False)
		self.assertEqual(_step(_workflow()["jobs"]["publish_triage"], name="Checkout trusted workflow support")["with"]["persist-credentials"], False)
		self.assertIn("clarify_isolated_run.sh", _step(triage_job, name="Stage workflow support files")["run"])
		self.assertEqual(
			derive_job["outputs"]["check_name_key"],
			"${{ steps.hash_check_name.outputs.check_name_key }}",
		)
		self.assertEqual(
			derive_job["outputs"]["same_repo"],
			"${{ steps.hash_check_name.outputs.same_repo }}",
		)
		self.assertEqual(
			" ".join(triage_job["if"].split()),
			"!contains(fromJson('[\"false\"]'), vars.CHECK_FAILURE_TRIAGE_ENABLED) && "
			"inputs.pr_number != '' && "
			"contains(fromJson('[\"failure\",\"timed_out\"]'), inputs.check_conclusion) && "
			"!contains(inputs.check_name, 'Check Failure Triage') && "
			"needs.derive_check_name_key.result == 'success' && "
			"needs.derive_check_name_key.outputs.same_repo == 'true'",
		)
		self.assertEqual(
			triage_job["concurrency"]["group"],
			"ai-check-triage-${{ github.repository }}-${{ inputs.pr_number }}-"
			"${{ needs.derive_check_name_key.outputs.check_name_key || inputs.check_run_id || "
			"inputs.check_name || github.run_id }}",
		)

		for job in jobs.values():
			for step in job.get("steps", []):
				self.assertNotIn("${{ inputs.", step.get("run", ""), step.get("name", "unnamed"))

		workflow_inputs = workflow["on"]["workflow_call"]["inputs"]
		self.assertEqual(
			list(workflow_inputs),
			["pr_number", "check_run_id", "check_name", "check_conclusion", "head_sha", "details_url"],
		)
		run_env = _step(triage_job, name="Run check-failure triage")["env"]
		self.assertEqual(
			set(run_env),
			{
				"CHECK_TRIAGE_PR_NUMBER",
				"CHECK_TRIAGE_CHECK_RUN_ID",
				"CHECK_TRIAGE_CHECK_NAME",
				"CHECK_TRIAGE_CHECK_CONCLUSION",
				"CHECK_TRIAGE_HEAD_SHA",
				"CHECK_TRIAGE_DETAILS_URL",
			},
		)

	def test_valid_same_repo_and_empty_optional_values_continue(self) -> None:
		for check_run_id, head_sha in (("29", "a" * 40), ("", "")):
			with self.subTest(check_run_id=check_run_id, head_sha=head_sha):
				proc, outputs, calls = _run_prerequisite(
					check_run_id=check_run_id,
					head_sha=head_sha,
				)
				self.assertEqual(proc.returncode, 0, proc.stderr)
				self.assertEqual(outputs["same_repo"], "true")
				self.assertEqual(outputs["verified_head_sha"], "a" * 40)
				self.assertEqual(calls, 1)

	def test_fork_is_rejected_without_failing_prerequisite(self) -> None:
		proc, outputs, calls = _run_prerequisite(gh_mode="fork")
		self.assertEqual(proc.returncode, 0, proc.stderr)
		self.assertEqual(outputs["same_repo"], "false")
		self.assertEqual(calls, 1)
		self.assertIn("Skipping check-failure triage for fork PR #17 (head repo: fork/repo).", proc.stdout)

	def test_invalid_inputs_never_reach_github_api(self) -> None:
		invalid_cases = (
			{"pr_number": "0"},
			{"pr_number": "1; echo unsafe"},
			{"check_run_id": "not-numeric"},
			{"check_run_id": "0"},
			{"check_conclusion": "cancelled"},
			{"head_sha": "abc123"},
			{"head_sha": "g" * 40},
		)
		for overrides in invalid_cases:
			with self.subTest(overrides=overrides):
				proc, _, calls = _run_prerequisite(**overrides)
				self.assertNotEqual(proc.returncode, 0)
				self.assertEqual(calls, 0)

	def test_stale_pr_head_is_rejected_before_checkout(self) -> None:
		proc, _, calls = _run_prerequisite(head_sha="b" * 40)
		self.assertNotEqual(proc.returncode, 0)
		self.assertEqual(calls, 1)

	def test_isolated_diagnosis_and_artifact_contract(self) -> None:
		workflow = _workflow()
		diagnose = workflow["jobs"]["triage"]
		publisher = workflow["jobs"]["publish_triage"]
		self.assertIn("bash scripts/check_failure_triage.sh diagnose", _step(diagnose, name="Run check-failure triage")["run"])
		self.assertIn("bash scripts/check_failure_triage.sh publish", _step(publisher, name="Publish verified triage")["run"])
		self.assertNotIn("secrets.GH_PAT", str(diagnose))
		self.assertNotIn("OPENROUTER_API_KEY", str(publisher))
		self.assertEqual(_step(diagnose, name="Upload bounded diagnosis")["with"]["retention-days"], 1)
		script = (REPO_ROOT / "scripts/check_failure_triage.sh").read_text()
		self.assertIn('bash scripts/clarify_isolated_run.sh "${PROMPT_FILE}" "${DIAG_FILE}"', script)
		self.assertNotIn("--sandbox danger-full-access", script)
		self.assertIn("invalid_diagnosis_artifact", script)
		isolation = (REPO_ROOT / "scripts/clarify_isolated_run.sh").read_text()
		for boundary in ("--network none", "--read-only", "--cap-drop ALL", '".git"', "CHECK_TRIAGE"):
			self.assertIn(boundary, isolation)
		for wrapper in (
			REPO_ROOT / ".github/workflows/internal-check-failure-triage.yml",
			REPO_ROOT / "workflow-templates/ai-check-failure-triage.yml",
		):
			permissions = yaml.safe_load(wrapper.read_text(encoding="utf-8"))["permissions"]
			self.assertEqual(permissions["checks"], "read")
			self.assertEqual(permissions["actions"], "read")

	def test_isolation_failure_produces_only_inconclusive_diagnosis(self) -> None:
		with tempfile.TemporaryDirectory(prefix="triage-isolation-") as directory:
			root = Path(directory)
			(root / "scripts").mkdir()
			(root / "prompts").mkdir()
			(root / "prompts/mode-check-failure-triage.txt").write_text("Trusted triage prompt\n")
			(root / "scripts/workflow_failure_heal.py").write_text("def redact_secrets(text): return text\n")
			_write_executable(root / "scripts/clarify_isolated_run.sh", "#!/bin/bash\nexit 1\n")
			bin_dir = root / "bin"
			bin_dir.mkdir()
			marker = root / "host-codex-invoked"
			_write_executable(bin_dir / "codex", f"#!/bin/bash\ntouch {marker}\n")
			_write_executable(bin_dir / "gh", """#!/bin/bash
if [[ "$*" == *'/pulls/17'* ]]; then
  echo '{"state":"open","head":{"ref":"feature","repo":{"full_name":"owner/repo"}}}'
else
  exit 1
fi
""")
			env = os.environ.copy()
			env.pop("BASH_ENV", None)
			env.pop("ENV", None)
			env.update({
				"GITHUB_REPOSITORY": "owner/repo", "RUNTIME_DIR": str(root / "runtime"),
				"CHECK_TRIAGE_PR_NUMBER": "17", "CHECK_TRIAGE_CHECK_NAME": "CI / lint",
				"CHECK_TRIAGE_CHECK_CONCLUSION": "failure", "CHECK_TRIAGE_HEAD_SHA": "a" * 40,
				"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			})
			result = subprocess.run(
				["bash", str(REPO_ROOT / "scripts/check_failure_triage.sh"), "diagnose"],
				cwd=root, env=env, capture_output=True, text=True,
			)
			self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
			self.assertFalse(marker.exists())
			self.assertIn("Automated diagnosis failed", (root / "runtime/diagnosis.md").read_text())

	def test_api_failures_are_classified_before_retry(self) -> None:
		permanent_proc, _, permanent_calls = _run_prerequisite(gh_mode="permanent")
		self.assertNotEqual(permanent_proc.returncode, 0)
		self.assertEqual(permanent_calls, 1)

		transient_proc, _, transient_calls = _run_prerequisite(gh_mode="transient")
		self.assertNotEqual(transient_proc.returncode, 0)
		self.assertEqual(transient_calls, 3)

		missing_repo_proc, _, missing_repo_calls = _run_prerequisite(gh_mode="missing_repo")
		self.assertNotEqual(missing_repo_proc.returncode, 0)
		self.assertEqual(missing_repo_calls, 1)

	def test_shell_metacharacters_are_hashed_as_literal_data(self) -> None:
		with tempfile.TemporaryDirectory(prefix="check-triage-marker-") as marker_dir:
			marker = Path(marker_dir) / "executed"
			check_name = f"quote' backtick` $(touch {marker});\n::error::payload"
			details_url = f"https://example.invalid/$(touch {marker})\n::warning::payload"
			proc, outputs, calls = _run_prerequisite(
				check_name=check_name,
				details_url=details_url,
			)
			self.assertEqual(proc.returncode, 0, proc.stderr)
			self.assertEqual(calls, 1)
			self.assertFalse(marker.exists())
			self.assertEqual(outputs["check_name_key"], hashlib.sha256(check_name.encode()).hexdigest())

	def test_trigger_log_sanitizes_metacharacters_to_one_line(self) -> None:
		workflow = _workflow()
		script = _step(workflow["jobs"]["triage"], name="Log trigger context")["run"]
		with tempfile.TemporaryDirectory(prefix="check-triage-log-") as temp_dir:
			marker_path = Path(temp_dir) / "executed"
			check_name = f"bad' `touch {marker_path}` $(touch {marker_path})\n::error::forged"
			env = os.environ.copy()
			env.pop("BASH_ENV", None)
			env.pop("ENV", None)
			env.update(
				{
					"CHECK_CONCLUSION": "failure",
					"CHECK_NAME": check_name,
					"GITHUB_REPOSITORY": "owner/repo",
					"HEAD_SHA": "a" * 40,
					"PR_NUMBER": "17",
				}
			)
			proc = subprocess.run(
				["bash", "--noprofile", "--norc", "-c", script],
				cwd=REPO_ROOT,
				env=env,
				capture_output=True,
				text=True,
				encoding="utf-8",
			)
			self.assertEqual(proc.returncode, 0, proc.stderr)
			self.assertFalse(marker_path.exists())
			self.assertEqual(len(proc.stdout.splitlines()), 3)
			self.assertIn("$(touch", proc.stdout.splitlines()[1])

	def test_failure_notification_sanitizes_payload_without_evaluation(self) -> None:
		workflow = _workflow()
		script = _step(workflow["jobs"]["triage"], name="Notify on triage workflow failure")["run"]
		with tempfile.TemporaryDirectory(prefix="check-triage-notify-") as temp_dir:
			temp_path = Path(temp_dir)
			(temp_path / "scripts").mkdir()
			capture_path = temp_path / "telegram-message"
			marker_path = temp_path / "executed"
			(temp_path / "scripts" / "tg_helpers.sh").write_text(
				'tg_send_msg() { printf "%s\\0%s" "$1" "$2" > "${TG_CAPTURE}"; }\n',
				encoding="utf-8",
			)
			check_name = f"bad' `touch {marker_path}` $(touch {marker_path})\n::error::forged"
			env = os.environ.copy()
			env.pop("BASH_ENV", None)
			env.pop("ENV", None)
			env.update(
				{
					"CHECK_NAME": check_name,
					"GITHUB_REPOSITORY": "owner/repo",
					"GITHUB_RUN_ID": "123",
					"PR_NUMBER": "17",
					"TG_CAPTURE": str(capture_path),
				}
			)
			proc = subprocess.run(
				["bash", "--noprofile", "--norc", "-c", script],
				cwd=temp_path,
				env=env,
				capture_output=True,
				text=True,
				encoding="utf-8",
			)
			self.assertEqual(proc.returncode, 0, proc.stderr)
			self.assertFalse(marker_path.exists())
			message, level = capture_path.read_bytes().split(b"\0", 1)
			self.assertEqual(level, b"CRITICAL")
			self.assertIn(b"$(touch", message)
			self.assertNotIn(b"\n::error::forged", message)


if __name__ == "__main__":
	unittest.main()
