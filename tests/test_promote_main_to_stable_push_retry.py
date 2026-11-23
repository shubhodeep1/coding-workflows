"""Transient push retry in promote-main-to-stable.yml's `ff` step (issue #8822).

Run 41819783609 lost a pinned promotion to one HTTP 500 on the single
`git push origin HEAD:refs/heads/stable`. These tests run the shipped step
body against a fake `git` and `sleep`.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "promote-main-to-stable.yml"

BEFORE = "1" * 40
TARGET = "2" * 40
MAIN_TIP = "3" * 40
OLD = BEFORE

SERVER_ERROR_OUTPUT = (
	"remote: Internal Server Error        \n"
	"remote: Request ID F403:4A281:5F8DA9:743DA7:6AEB14E0        \n"
	"To https://github.com/shubhodeep1/coding-workflows\n"
	" ! [remote rejected]         HEAD -> stable (Internal Server Error)\n"
	"error: failed to push some refs to 'https://github.com/shubhodeep1/coding-workflows'\n"
)
REMOTE_FAILED_OUTPUT = (
	"To https://github.com/shubhodeep1/coding-workflows\n"
	" ! [remote rejected]         HEAD -> stable (failed)\n"
	"error: failed to push some refs\n"
)
RETRY_LINE = "PROMOTE_STABLE_PUSH_RETRY"

FAKE_GIT = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state = os.environ["FAKE_GIT_STATE"]
with open(os.path.join(state, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")

def pop(name):
    path = os.path.join(state, name + ".json")
    with open(path) as fh:
        seq = json.load(fh)
    item = seq[0] if len(seq) == 1 else seq.pop(0)
    with open(path, "w") as fh:
        json.dump(seq, fh)
    return item

if args[:2] == ["ls-remote", "--exit-code"]:
    sys.exit(0)
if args[:1] == ["ls-remote"]:
    value = pop("ls_remote")
    if value == "FAIL":
        sys.exit(128)
    print(value + "\trefs/heads/stable")
    sys.exit(0)
if args[:1] == ["rev-parse"]:
    if args[1:] == ["HEAD"]:
        print(os.environ["FAKE_BEFORE"])
    else:
        print(os.environ["FAKE_MAIN_TIP"])
    sys.exit(0)
if args[:1] == ["push"]:
    rc, out = pop("push")
    sys.stdout.write(out)
    sys.exit(rc)
sys.exit(0)
"""

FAKE_SLEEP = """#!/usr/bin/env bash
echo "$1" >> "${FAKE_GIT_STATE}/sleeps.txt"
"""


def _ff_step_lines() -> list[str]:
	lines = WORKFLOW.read_text().splitlines()
	start = next(i for i, line in enumerate(lines) if line.strip() == "id: ff")
	# Back up to the "- name:" line that opens this step.
	while not lines[start].lstrip().startswith("- name:"):
		start -= 1
	indent = len(lines[start]) - len(lines[start].lstrip())
	end = start + 1
	while end < len(lines):
		line = lines[end]
		if line.strip() and len(line) - len(line.lstrip()) <= indent:
			break
		end += 1
	return lines[start:end]


def _ff_step_script() -> str:
	step = _ff_step_lines()
	run_idx = next(i for i, line in enumerate(step) if line.strip() == "run: |")
	body = step[run_idx + 1 :]
	indent = min(
		len(body_line) - len(body_line.lstrip()) for body_line in body if body_line.strip()
	)
	return (
		"\n".join(body_line[indent:] if body_line.strip() else "" for body_line in body)
		+ "\n"
	)


def _ff_step_env() -> dict:
	env = {}
	in_env = False
	for line in _ff_step_lines():
		stripped = line.strip()
		if stripped == "env:":
			in_env = True
			continue
		if in_env:
			if stripped.startswith("run:"):
				break
			key, _, value = stripped.partition(": ")
			env[key] = value
	return env


class PromoteStablePushRetryTest(unittest.TestCase):
	def _run(self, pushes, ls_remote, extra_env=None):
		tmp = Path(tempfile.mkdtemp())
		state = tmp / "state"
		state.mkdir()
		bindir = tmp / "bin"
		bindir.mkdir()
		for name, body in (("git", FAKE_GIT), ("sleep", FAKE_SLEEP)):
			path = bindir / name
			path.write_text(body)
			path.chmod(path.stat().st_mode | stat.S_IEXEC)
		(state / "push.json").write_text(json.dumps(pushes))
		(state / "ls_remote.json").write_text(json.dumps(ls_remote))
		script = tmp / "step.sh"
		script.write_text(_ff_step_script())
		output = tmp / "github_output"
		output.write_text("")
		env = dict(os.environ)
		env.update(
			{
				"PATH": f"{bindir}:{env.get('PATH', '')}",
				"FAKE_GIT_STATE": str(state),
				"FAKE_BEFORE": BEFORE,
				"FAKE_MAIN_TIP": MAIN_TIP,
				"TARGET_SHA_INPUT": TARGET,
				"GITHUB_OUTPUT": str(output),
				"TMPDIR": str(tmp),
			}
		)
		env.pop("PROMOTE_STABLE_PUSH_TRANSIENT_RETRY_ENABLED", None)
		env.update(extra_env or {})
		proc = subprocess.run(
			["bash", str(script)],
			env=env,
			capture_output=True,
			text=True,
			timeout=120,
		)
		calls = [
			json.loads(line)
			for line in (state / "calls.jsonl").read_text().splitlines()
			if line.strip()
		]
		pushes_made = [c for c in calls if c[:1] == ["push"]]
		for push in pushes_made:
			self.assertFalse(any(a.startswith("--force") for a in push), push)
			self.assertEqual(push, ["push", "origin", "HEAD:refs/heads/stable"])
		sleeps_path = state / "sleeps.txt"
		sleeps = sleeps_path.read_text().split() if sleeps_path.exists() else []
		return proc, pushes_made, sleeps, output.read_text()

	def test_server_error_then_success(self):
		proc, pushes, sleeps, out = self._run(
			[[1, SERVER_ERROR_OUTPUT], [0, ""]], [OLD, TARGET]
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(len(pushes), 2)
		self.assertIn("moved=true", out)
		self.assertIn(f"sha={TARGET}", out)
		self.assertEqual(sleeps, ["5"])
		self.assertIn(
			"PROMOTE_STABLE_PUSH_RETRY attempt=1/2 class=server_error outcome=retry",
			proc.stdout,
		)

	def test_server_error_but_push_landed(self):
		proc, pushes, sleeps, out = self._run([[1, SERVER_ERROR_OUTPUT]], [TARGET])
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(len(pushes), 1)
		self.assertIn("outcome=landed", proc.stdout)
		self.assertIn("moved=true", out)

	def test_gives_up_after_two_retries(self):
		proc, pushes, sleeps, out = self._run([[1, REMOTE_FAILED_OUTPUT]], [OLD])
		self.assertNotEqual(proc.returncode, 0)
		self.assertEqual(len(pushes), 3)
		self.assertEqual(sleeps, ["5", "10"])
		self.assertIn(
			"PROMOTE_STABLE_PUSH_RETRY attempt=2/2 class=remote_rejected_failed outcome=gave_up",
			proc.stdout,
		)
		self.assertNotIn("moved=true", out)

	def test_ls_remote_failure_is_logged_and_retried(self):
		proc, pushes, sleeps, out = self._run(
			[[1, SERVER_ERROR_OUTPUT], [0, ""]], ["FAIL", TARGET]
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(len(pushes), 2)
		self.assertIn("git ls-remote origin refs/heads/stable failed (exit 128)", proc.stdout)
		self.assertIn("class=server_error outcome=retry", proc.stdout)
		self.assertIn("moved=true", out)

	def test_last_attempt_landed(self):
		proc, pushes, sleeps, out = self._run(
			[[1, SERVER_ERROR_OUTPUT]], [OLD, OLD, TARGET]
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertEqual(len(pushes), 3)
		self.assertIn("attempt=2/2 class=server_error outcome=landed", proc.stdout)
		self.assertIn("moved=true", out)

	def test_rule_rejections_are_not_retried(self):
		for text in (
			"remote: error: GH013: Repository rule violations found\n"
			" ! [remote rejected] HEAD -> stable (Internal Server Error)\n",
			"remote: error: GH006: Protected branch update failed\n",
		):
			proc, pushes, sleeps, out = self._run([[1, text]], [OLD])
			self.assertEqual(proc.returncode, 1)
			self.assertEqual(len(pushes), 1)
			self.assertNotIn(RETRY_LINE, proc.stdout)
			self.assertEqual(sleeps, [])

	def test_non_fast_forward_is_not_retried(self):
		proc, pushes, sleeps, out = self._run(
			[[1, " ! [rejected]        HEAD -> stable (non-fast-forward)\n"]], [OLD]
		)
		self.assertEqual(proc.returncode, 1)
		self.assertEqual(len(pushes), 1)
		self.assertNotIn(RETRY_LINE, proc.stdout)

	def test_kill_switch_disables_retry(self):
		proc, pushes, sleeps, out = self._run(
			[[1, SERVER_ERROR_OUTPUT], [0, ""]],
			[OLD, TARGET],
			{"PROMOTE_STABLE_PUSH_TRANSIENT_RETRY_ENABLED": "FALSE"},
		)
		self.assertEqual(proc.returncode, 1)
		self.assertEqual(len(pushes), 1)
		self.assertNotIn(RETRY_LINE, proc.stdout)

	def test_static_contract(self):
		env = _ff_step_env()
		self.assertEqual(
			env.get("PROMOTE_STABLE_PUSH_TRANSIENT_RETRY_ENABLED"),
			"${{ vars.PROMOTE_STABLE_PUSH_TRANSIENT_RETRY_ENABLED || 'true' }}",
		)
		script = _ff_step_script()
		self.assertIn("${PROMOTE_STABLE_PUSH_TRANSIENT_RETRY_ENABLED:-true}", script)
		order = [
			"GH006|GH013|protected branch",
			"(fetch first|non-fast-forward)",
			"Internal Server Error|The requested URL returned error",
			"remote rejected.*\\(failed\\)",
			"RPC failed|unexpected disconnect",
		]
		positions = [script.index(token) for token in order]
		self.assertEqual(positions, sorted(positions))
		for line in script.splitlines():
			if line.strip().startswith("#"):
				continue
			self.assertNotIn("--force", line)
			self.assertNotIn("git rebase", line)
		self.assertIn("Post-push verification failed", script)
		self.assertIn("Refusing to dispatch the release on stale state", script)


if __name__ == "__main__":
	unittest.main()
