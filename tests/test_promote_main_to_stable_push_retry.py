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



# --- Stable rollback hold gate (issue #10076) -------------------------------

HOLD_GATE_STEP_ID = "stable_hold_gate"
HOLD_SKIP_IF = "if: steps.stable_hold_gate.outputs.skip != 'true'"
HOLD_CONTENTS_CALL = "repos/o/r/contents/stable-hold.json?ref=ai%2Fstable-hold"

FAKE_HOLD_GIT = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state = os.environ["FAKE_GIT_STATE"]
with open(os.path.join(state, "git_calls.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
if args[:1] == ["show"] and len(args) == 2 and args[1].startswith("origin/main:scripts/"):
    if os.environ.get("FAKE_GIT_SHOW_FAIL") == "1":
        sys.stderr.write("fatal: path does not exist in 'origin/main'\n")
        sys.exit(128)
    rel = args[1][len("origin/main:"):]
    path = os.path.join(os.environ["FAKE_REPO_ROOT"], rel)
    if not os.path.isfile(path):
        sys.stderr.write("fatal: path does not exist in 'origin/main'\n")
        sys.exit(128)
    with open(path) as fh:
        sys.stdout.write(fh.read())
    sys.exit(0)
sys.exit(0)
"""

FAKE_HOLD_GH = r"""#!/usr/bin/env python3
import base64, json, os, sys
args = sys.argv[1:]
state = os.environ["FAKE_GIT_STATE"]
with open(os.path.join(state, "gh_calls.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
hold = os.environ.get("FAKE_HOLD", "inactive")
if args[:1] == ["api"] and any("/contents/stable-hold.json" in a for a in args):
    if hold == "inactive":
        sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
    if hold == "unknown":
        sys.stderr.write("HTTP 503: Service Unavailable\n"); sys.exit(1)
    doc = {"schema": "stable_hold.v1", "active": True, "to_version": "v1.45.0", "from_version": "v1.46.0"}
    if hold == "malformed":
        doc["schema"] = "other"
    if hold == "cleared":
        doc["active"] = False
    print(json.dumps({"sha": "a" * 40, "encoding": "base64", "content": base64.b64encode(json.dumps(doc).encode()).decode()}))
    sys.exit(0)
sys.stderr.write("unexpected gh call\n")
sys.exit(1)
"""


def _promote_job_step_blocks() -> list[list[str]]:
	"""Split the `promote` job's steps into line blocks (one per `- name:`)."""
	lines = WORKFLOW.read_text().splitlines()
	start = next(i for i, line in enumerate(lines) if line.rstrip() == "  promote:")
	steps_idx = next(
		i for i in range(start, len(lines)) if lines[i].strip() == "steps:"
	)
	blocks: list[list[str]] = []
	current: list[str] | None = None
	step_indent = None
	for line in lines[steps_idx + 1 :]:
		if line.strip() and len(line) - len(line.lstrip()) <= 4:
			break
		if line.lstrip().startswith("- name:"):
			indent = len(line) - len(line.lstrip())
			if step_indent is None:
				step_indent = indent
			if indent == step_indent:
				current = [line]
				blocks.append(current)
				continue
		if current is not None:
			current.append(line)
	return blocks


def _step_name(block: list[str]) -> str:
	return block[0].split("- name:", 1)[1].strip()


def _step_block_by_id(step_id: str) -> list[str]:
	for block in _promote_job_step_blocks():
		if any(line.strip() == f"id: {step_id}" for line in block):
			return block
	raise AssertionError(f"no promote step with id {step_id}")


def _step_block_script(block: list[str]) -> str:
	run_idx = next(i for i, line in enumerate(block) if line.strip() == "run: |")
	body = block[run_idx + 1 :]
	indent = min(
		len(body_line) - len(body_line.lstrip()) for body_line in body if body_line.strip()
	)
	return (
		"\n".join(body_line[indent:] if body_line.strip() else "" for body_line in body)
		+ "\n"
	)


def _step_block_env(block: list[str]) -> dict:
	env = {}
	in_env = False
	for line in block:
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


class PromoteStableHoldGateTest(unittest.TestCase):
	def _run_gate(self, hold="inactive", extra_env=None):
		tmp = Path(tempfile.mkdtemp())
		state = tmp / "state"
		state.mkdir()
		bindir = tmp / "bin"
		bindir.mkdir()
		for name, body in (("git", FAKE_HOLD_GIT), ("gh", FAKE_HOLD_GH), ("sleep", FAKE_SLEEP)):
			path = bindir / name
			path.write_text(body)
			path.chmod(path.stat().st_mode | stat.S_IEXEC)
		script = tmp / "gate.sh"
		script.write_text(_step_block_script(_step_block_by_id(HOLD_GATE_STEP_ID)))
		output = tmp / "github_output"
		output.write_text("")
		summary = tmp / "step_summary"
		summary.write_text("")
		env = dict(os.environ)
		for key in (
			"PROMOTE_STABLE_HOLD_GATE_ENABLED",
			"GH_READ_TOKEN",
			"STABLE_HOLD_BRANCH",
			"STABLE_HOLD_FILE",
		):
			env.pop(key, None)
		env.update(
			{
				"PATH": f"{bindir}:{env.get('PATH', '')}",
				"FAKE_GIT_STATE": str(state),
				"FAKE_REPO_ROOT": str(REPO_ROOT),
				"FAKE_HOLD": hold,
				"GH_RETRY_MAX_ATTEMPTS": "1",
				"GITHUB_REPOSITORY": "o/r",
				"GH_TOKEN": "x",
				"GITHUB_OUTPUT": str(output),
				"GITHUB_STEP_SUMMARY": str(summary),
				"TMPDIR": str(tmp),
				"PYTHONDONTWRITEBYTECODE": "1",
			}
		)
		env.update(extra_env or {})
		proc = subprocess.run(
			["bash", str(script)],
			env=env,
			capture_output=True,
			text=True,
			timeout=120,
			cwd=str(tmp),
		)

		def _calls(name):
			path = state / name
			if not path.exists():
				return []
			return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

		return proc, _calls("gh_calls.jsonl"), output.read_text(), summary.read_text()

	def _hold_calls(self, gh_calls):
		return [c for c in gh_calls if any("/contents/stable-hold.json" in a for a in c)]

	def test_no_hold_branch_404_promotes(self):
		proc, gh_calls, out, summary = self._run_gate("inactive")
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("skip=false", out)
		self.assertNotIn("skip=true", out)
		self.assertNotIn("PROMOTE_SKIPPED", proc.stdout)
		hold_calls = self._hold_calls(gh_calls)
		self.assertEqual(len(hold_calls), 1, gh_calls)
		self.assertIn(HOLD_CONTENTS_CALL, hold_calls[0])

	def test_cleared_hold_promotes(self):
		proc, gh_calls, out, summary = self._run_gate("cleared")
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("skip=false", out)
		self.assertNotIn("PROMOTE_SKIPPED", proc.stdout)

	def test_hold_read_503_skips_fail_closed(self):
		proc, gh_calls, out, summary = self._run_gate("unknown")
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("PROMOTE_SKIPPED reason=stable_hold_unknown", proc.stdout)
		self.assertIn("::warning::", proc.stdout)
		self.assertIn("skip=true", out)
		self.assertNotIn("skip=false", out)
		self.assertIn("Promotion skipped", summary)

	def test_active_hold_skips(self):
		proc, gh_calls, out, summary = self._run_gate("active")
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("PROMOTE_SKIPPED reason=stable_hold_active to=v1.45.0", proc.stdout)
		self.assertIn("::warning::A stable rollback hold is active", proc.stdout)
		self.assertIn("skip=true", out)
		self.assertNotIn("skip=false", out)
		self.assertIn("v1.45.0", summary)

	def test_malformed_hold_skips_as_unknown(self):
		proc, gh_calls, out, summary = self._run_gate("malformed")
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("PROMOTE_SKIPPED reason=stable_hold_unknown", proc.stdout)
		self.assertIn("skip=true", out)

	def test_reader_missing_on_main_skips_without_gh(self):
		proc, gh_calls, out, summary = self._run_gate(
			"inactive", {"FAKE_GIT_SHOW_FAIL": "1"}
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("PROMOTE_SKIPPED reason=stable_hold_unknown", proc.stdout)
		self.assertIn("skip=true", out)
		self.assertEqual(gh_calls, [])

	def test_gate_disabled_promotes_without_reading(self):
		for value in ("false", "FALSE"):
			proc, gh_calls, out, summary = self._run_gate(
				"active", {"PROMOTE_STABLE_HOLD_GATE_ENABLED": value}
			)
			self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
			self.assertIn("skip=false", out)
			self.assertNotIn("PROMOTE_SKIPPED", proc.stdout)
			self.assertEqual(gh_calls, [])

	def test_unrecognised_switch_value_keeps_gate_on(self):
		proc, gh_calls, out, summary = self._run_gate(
			"active", {"PROMOTE_STABLE_HOLD_GATE_ENABLED": "flase"}
		)
		self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
		self.assertIn("PROMOTE_SKIPPED reason=stable_hold_active to=v1.45.0", proc.stdout)
		self.assertIn("skip=true", out)

	def test_static_contract(self):
		blocks = _promote_job_step_blocks()
		names = [_step_name(block) for block in blocks]
		gate_idx = next(
			i
			for i, block in enumerate(blocks)
			if any(line.strip() == f"id: {HOLD_GATE_STEP_ID}" for line in block)
		)
		ff_idx = next(
			i for i, block in enumerate(blocks) if any(line.strip() == "id: ff" for line in block)
		)
		self.assertEqual(names[gate_idx], "Check stable rollback hold")
		self.assertEqual(names[gate_idx - 1], "Validate stable is fast-forwardable to main")
		self.assertEqual(ff_idx, gate_idx + 1)
		env = _step_block_env(blocks[gate_idx])
		self.assertEqual(env.get("GH_TOKEN"), "${{ github.token }}")
		self.assertEqual(
			env.get("PROMOTE_STABLE_HOLD_GATE_ENABLED"),
			"${{ vars.PROMOTE_STABLE_HOLD_GATE_ENABLED || 'true' }}",
		)
		for name in (
			"Fast-forward stable to main",
			"Resolve version (compute next minor if not provided)",
			"Dispatch test-and-mark-stable on stable",
			"Summary",
		):
			block = blocks[names.index(name)]
			self.assertIn(HOLD_SKIP_IF, [line.strip() for line in block], name)
		gate_script = _step_block_script(blocks[gate_idx])
		self.assertIn("origin/main:scripts/stable_hold.sh", gate_script)
		self.assertNotIn("${{", gate_script)
		for block in blocks:
			self.assertNotIn("rollback_stable.sh", "\n".join(block))


if __name__ == "__main__":
	unittest.main()
