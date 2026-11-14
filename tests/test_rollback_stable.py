#!/usr/bin/env python3
"""Tests for scripts/rollback_stable.sh and .github/workflows/rollback-stable.yml (#9342).

Each test builds a local bare `origin` with annotated vX.Y.Z tags, a `stable`
branch and lightweight `stable` / `v1` tags (the shape the release job
writes), clones it, and runs the real script in the clone with a fake `gh`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "rollback_stable.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "rollback-stable.yml"
REAL_GIT = shutil.which("git") or "/usr/bin/git"

BOTH_ASSETS = [{"name": "sync-manifest.json", "state": "uploaded"}, {"name": "release-manifest.v1.json", "state": "uploaded"}]

# Fake gh: releases, workflow runs, run cancels, consumer dispatches, and the
# hold file (stable_hold.sh's contents API). Every hold PUT appends to EVENTS.
MOCK_GH = r'''#!/usr/bin/env python3
import base64, json, os, sys
from pathlib import Path
state_path = Path(os.environ["MOCK_GH_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def save():
    state_path.write_text(json.dumps(state))
def respond(payload):
    save(); sys.stdout.write(json.dumps(payload)); sys.exit(0)
def fail(msg):
    save(); sys.stderr.write(msg + "\n"); sys.exit(1)
method, path, rest, i = "GET", None, args[1:], 0
fields = []
while i < len(rest):
    a = rest[i]
    if a in ("-X", "--method"):
        method = rest[i + 1]; i += 2; continue
    if a in ("-f", "-F"):
        fields.append(rest[i + 1]); i += 2; continue
    if a in ("--input", "-H"):
        i += 2; continue
    if path is None and not a.startswith("-"):
        path = a
    i += 1
body = json.loads(sys.stdin.read() or "null") if "--input" in args else None
if path.startswith("repos/owner/repo/releases/tags/"):
    if state.get("release_error"):
        fail("HTTP 503: Service Unavailable")
    tag = path.rsplit("/", 1)[1]
    rel = state["releases"].get(tag)
    if rel is None:
        fail("gh: Not Found (HTTP 404)")
    respond(rel)
if "/actions/workflows/" in path and "/runs" in path:
    wf = path.split("/actions/workflows/")[1].split("/")[0]
    seq = state.get("runs", {}).get(wf) or [[]]
    ticks = state.setdefault("run_ticks", {})
    n = ticks.get(wf, 0); ticks[wf] = n + 1
    respond({"workflow_runs": seq[min(n, len(seq) - 1)]})
if path.startswith("repos/owner/repo/actions/runs/") and path.endswith("/cancel"):
    state.setdefault("cancels", []).append(path.split("/")[5])
    if state.get("cancel_fail_once", 0) > 0:
        state["cancel_fail_once"] -= 1
        fail("HTTP 502: Bad Gateway")
    respond({})
if path.endswith("/dispatches"):
    repo = "/".join(path.split("/")[1:3])
    state.setdefault("dispatches", []).append({"repo": repo, "fields": fields})
    if repo in state.get("dispatch_fail", []):
        fail("HTTP 404: Not Found")
    respond({})
if path.startswith("repos/owner/repo/contents/stable-hold.json?ref="):
    if state.get("hold_read_error"):
        fail("HTTP 503: Service Unavailable")
    if state.get("hold_file") is None:
        fail("gh: Not Found (HTTP 404)")
    enc = base64.b64encode(state["hold_file"].encode()).decode()
    respond({"sha": state["hold_sha"], "encoding": "base64", "content": enc})
if path == "repos/owner/repo/git/ref/heads/ai/stable-hold":
    respond({"object": {"sha": "c" * 40}})
if path == "repos/owner/repo/contents/stable-hold.json" and method == "PUT":
    with open(os.environ["EVENTS"], "a") as ev:
        ev.write("hold_put\n")
    state.setdefault("hold_puts", []).append(body)
    if state.get("hold_put_fail"):
        fail("HTTP 403: Resource not accessible by integration")
    state["hold_file"] = base64.b64decode(body["content"]).decode()
    state["hold_sha"] = str(len(state["hold_puts"]) % 10) * 40
    respond({})
fail("unexpected gh call " + " ".join(args))
'''

GIT_WRAPPER = f'''#!/usr/bin/env bash
if [ "${{1:-}}" = "push" ]; then
  printf 'push %s\\n' "$*" >> "${{EVENTS}}"
fi
exec {REAL_GIT} "$@"
'''


def _git(cwd: Path, *args: str, env: dict[str, str]) -> str:
	return subprocess.run([REAL_GIT, *args], cwd=str(cwd), env=env, check=True, text=True, capture_output=True).stdout.strip()


def _base_env(tmp: Path) -> dict[str, str]:
	env = os.environ.copy()
	for key in list(env):
		if key.startswith("GIT_") or key.startswith("TG_") or key.startswith("STABLE_") or key in ("BASH_ENV", "ENV"):
			env.pop(key)
	env.update(
		{
			"HOME": str(tmp / "home"),
			"GIT_CONFIG_NOSYSTEM": "1",
			"GIT_AUTHOR_NAME": "t",
			"GIT_AUTHOR_EMAIL": "t@example.com",
			"GIT_COMMITTER_NAME": "t",
			"GIT_COMMITTER_EMAIL": "t@example.com",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	(tmp / "home").mkdir(exist_ok=True)
	return env


class Fixture:
	"""origin: v0.9.0 < v1.44.0 < v1.45.0 < v1.46.0 on `stable` (stable, v1 tags
	at v1.46.0); v1.45.5 on a side branch; v1.47.0 on main ahead of stable."""

	def __init__(self, tmp: Path) -> None:
		self.tmp = tmp
		self.env = _base_env(tmp)
		self.origin = tmp / "origin.git"
		seed = tmp / "seed"
		_git(tmp, "init", "--bare", "-b", "main", str(self.origin), env=self.env)
		_git(tmp, "init", "-b", "main", str(seed), env=self.env)
		self.sha: dict[str, str] = {}
		for version in ("v0.9.0", "v1.44.0", "v1.45.0", "v1.46.0"):
			_git(seed, "commit", "--allow-empty", "-m", version, env=self.env)
			_git(seed, "tag", "-a", version, "-m", f"Release {version}", env=self.env)
			self.sha[version] = _git(seed, "rev-parse", "HEAD", env=self.env)
		_git(seed, "branch", "stable", env=self.env)
		_git(seed, "tag", "stable", "refs/tags/v1.46.0", env=self.env)
		_git(seed, "tag", "v1", "refs/tags/v1.46.0", env=self.env)
		_git(seed, "commit", "--allow-empty", "-m", "v1.47.0", env=self.env)
		_git(seed, "tag", "-a", "v1.47.0", "-m", "Release v1.47.0", env=self.env)
		_git(seed, "checkout", "-q", "-b", "side", self.sha["v1.45.0"], env=self.env)
		_git(seed, "commit", "--allow-empty", "-m", "v1.45.5", env=self.env)
		_git(seed, "tag", "-a", "v1.45.5", "-m", "Release v1.45.5", env=self.env)
		_git(seed, "remote", "add", "origin", str(self.origin), env=self.env)
		_git(seed, "push", "-q", "origin", "refs/heads/main:refs/heads/main", "refs/heads/stable:refs/heads/stable", "refs/heads/side:refs/heads/side", "--tags", env=self.env)
		self.consumers = tmp / "consumer_repos.json"
		self.consumers.write_text(json.dumps(["owner/consumer-a", "bad repo", "../escape", "owner/consumer-b"]), encoding="utf-8")
		self.state: dict = {"releases": {v: {"tag_name": v, "draft": False, "prerelease": False, "assets": list(BOTH_ASSETS)} for v in ("v0.9.0", "v1.44.0", "v1.45.0", "v1.45.5", "v1.46.0", "v1.47.0")}}
		self.runs = 0

	def remote_peel(self, tag: str) -> str:
		return _git(self.origin, "rev-parse", f"refs/tags/{tag}^{{commit}}", env=self.env)

	def set_remote_tag(self, tag: str, version: str) -> None:
		obj = _git(self.origin, "rev-parse", f"refs/tags/{version}", env=self.env)
		_git(self.origin, "update-ref", f"refs/tags/{tag}", obj, env=self.env)

	def run(self, env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict, list[str]]:
		self.runs += 1
		work = self.tmp / f"run{self.runs}"
		bin_dir = self.tmp / f"bin{self.runs}"
		bin_dir.mkdir()
		subprocess.run([REAL_GIT, "clone", "-q", str(self.origin), str(work)], env=self.env, check=True)
		(bin_dir / "gh").write_text(MOCK_GH, encoding="utf-8")
		(bin_dir / "gh").chmod(0o755)
		(bin_dir / "git").write_text(GIT_WRAPPER, encoding="utf-8")
		(bin_dir / "git").chmod(0o755)
		state_file = self.tmp / "state.json"
		state_file.write_text(json.dumps(self.state), encoding="utf-8")
		events = self.tmp / f"events{self.runs}.txt"
		events.write_text("", encoding="utf-8")
		summary = self.tmp / f"summary{self.runs}.md"
		run_env = dict(self.env)
		run_env.update(
			{
				"PATH": f"{bin_dir}:{run_env['PATH']}",
				"MOCK_GH_STATE": str(state_file),
				"EVENTS": str(events),
				"GITHUB_REPOSITORY": "owner/repo",
				"GITHUB_REF": "refs/heads/main",
				"GITHUB_RUN_ID": "4242",
				"GITHUB_SERVER_URL": "https://github.com",
				"GITHUB_STEP_SUMMARY": str(summary),
				"GH_TOKEN": "x",
				"GH_RETRY_MAX_ATTEMPTS": "1",
				"STABLE_ROLLBACK_REASON": "release regression",
				"STABLE_ROLLBACK_CONSUMER_FILE": str(self.consumers),
				"STABLE_ROLLBACK_CANCEL_POLL_SECS": "1",
				"STABLE_ROLLBACK_CANCEL_WAIT_SECS": "5",
			}
		)
		if env:
			run_env.update(env)
		proc = subprocess.run(["bash", str(SCRIPT)], cwd=str(work), env=run_env, text=True, capture_output=True, timeout=120)
		final = json.loads(state_file.read_text(encoding="utf-8"))
		# Carry the hold (and nothing per-run) into the next run.
		for key in ("hold_file", "hold_sha"):
			if key in final:
				self.state[key] = final[key]
		final["summary"] = summary.read_text(encoding="utf-8") if summary.exists() else ""
		return proc, final, events.read_text(encoding="utf-8").splitlines()


@pytest.fixture()
def fx(tmp_path: Path) -> Fixture:
	return Fixture(tmp_path)


def _assert_untouched(fx: Fixture) -> None:
	assert fx.remote_peel("stable") == fx.sha["v1.46.0"]
	assert fx.remote_peel("v1") == fx.sha["v1.46.0"]


def _hold_doc(state: dict) -> dict:
	return json.loads(state["hold_file"])


def test_disabled_skips_without_calls(fx: Fixture) -> None:
	proc, state, events = fx.run({"STABLE_ROLLBACK_ENABLED": "false"})
	assert proc.returncode == 0, proc.stderr
	assert "STABLE_ROLLBACK outcome=skipped reason=disabled" in proc.stdout
	assert not state.get("calls") and not events
	_assert_untouched(fx)


def test_bad_inputs_are_refused(fx: Fixture) -> None:
	cases = [
		({"GITHUB_REF": "refs/heads/feature"}, "bad_ref"),
		({"STABLE_ROLLBACK_REASON": "  \n "}, "bad_reason"),
		({"STABLE_ROLLBACK_REASON": "x" * 201}, "bad_reason"),
		({"STABLE_ROLLBACK_TARGET_VERSION": "1.45.0"}, "bad_target"),
		({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0; rm -rf /"}, "bad_target"),
	]
	for env, reason in cases:
		proc, state, events = fx.run(env)
		assert proc.returncode == 1, (env, proc.stdout)
		assert f"STABLE_ROLLBACK outcome=refused reason={reason}" in proc.stdout, (env, proc.stdout)
		assert not state.get("hold_puts") and not events
	_assert_untouched(fx)


def test_default_target_skips_release_without_manifest(fx: Fixture) -> None:
	fx.state["releases"]["v1.45.0"]["assets"] = [{"name": "release-manifest.v1.json", "state": "uploaded"}]
	proc, state, events = fx.run()
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "STABLE_ROLLBACK outcome=rolled_back from=v1.46.0 to=v1.44.0 consumers_dispatched=2/2" in proc.stdout
	assert "Skipping v1.45.5: not_on_stable." in proc.stdout
	assert "Skipping v1.45.0: release_assets_missing." in proc.stdout
	assert fx.remote_peel("stable") == fx.sha["v1.44.0"]
	assert fx.remote_peel("v1") == fx.sha["v1.44.0"]
	# The hold is written before any tag push.
	assert events[0] == "hold_put"
	assert [e.split()[-1] for e in events if e.startswith("push")] == ["refs/tags/stable", "refs/tags/v1"]
	assert events.index("hold_put") < next(i for i, e in enumerate(events) if e.startswith("push"))
	doc = _hold_doc(state)
	assert doc["active"] is True and doc["from_version"] == "v1.46.0" and doc["to_version"] == "v1.44.0"
	assert doc["run_url"] == "https://github.com/owner/repo/actions/runs/4242"
	assert doc["reason"] == "release regression"
	assert [d["repo"] for d in state["dispatches"]] == ["owner/consumer-a", "owner/consumer-b"]
	for dispatch in state["dispatches"]:
		assert "event_type=coding-workflows-stable-released" in dispatch["fields"]
		assert "client_payload[version]=v1.44.0" in dispatch["fields"]
		assert f"client_payload[sha]={fx.sha['v1.44.0']}" in dispatch["fields"]
	assert "owner/consumer-a: dispatched" in state["summary"]


def test_explicit_target_rolls_back_and_tags_peel(fx: Fixture) -> None:
	proc, state, _ = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "outcome=rolled_back from=v1.46.0 to=v1.45.0" in proc.stdout
	assert fx.remote_peel("stable") == fx.sha["v1.45.0"]
	assert fx.remote_peel("v1") == fx.sha["v1.45.0"]
	# Lightweight tags pointing at the version's annotated tag object.
	v_obj = _git(fx.origin, "rev-parse", "refs/tags/v1.45.0", env=fx.env)
	assert _git(fx.origin, "rev-parse", "refs/tags/stable", env=fx.env) == v_obj
	assert _git(fx.origin, "rev-parse", "refs/heads/stable", env=fx.env) == fx.sha["v1.46.0"], "the stable branch never moves"


@pytest.mark.parametrize(
	"target,reason",
	[("v1.47.0", "target_not_lower"), ("v0.9.0", "major_mismatch"), ("v1.45.5", "not_on_stable"), ("v1.43.0", "target_not_found")],
)
def test_ineligible_targets_are_refused(fx: Fixture, target: str, reason: str) -> None:
	proc, state, events = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": target})
	assert proc.returncode == 1
	assert f"STABLE_ROLLBACK outcome=refused reason={reason} to={target}" in proc.stdout
	assert not state.get("hold_puts") and not events and not state.get("dispatches")
	_assert_untouched(fx)


def test_target_without_release_manifest_is_refused(fx: Fixture) -> None:
	fx.state["releases"]["v1.45.0"]["assets"] = [{"name": "sync-manifest.json", "state": "uploaded"}]
	proc, state, events = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 1
	assert "outcome=refused reason=release_assets_missing to=v1.45.0" in proc.stdout
	_assert_untouched(fx)


def test_release_lookup_error_stops_instead_of_walking_further_back(fx: Fixture) -> None:
	fx.state["release_error"] = True
	proc, state, events = fx.run()
	assert proc.returncode == 1
	assert "outcome=refused reason=release_lookup_failed candidate=v1.45.0" in proc.stdout
	_assert_untouched(fx)


def test_hold_failure_leaves_tags_untouched(fx: Fixture) -> None:
	fx.state["hold_put_fail"] = True
	proc, state, events = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 1
	assert "STABLE_ROLLBACK outcome=hold_failed from=v1.46.0 to=v1.45.0" in proc.stdout
	assert events == ["hold_put"]
	assert not state.get("dispatches") and not state.get("cancels")
	_assert_untouched(fx)


def test_in_flight_release_runs_are_cancelled_and_drained(fx: Fixture) -> None:
	active = {"id": 901, "status": "in_progress"}
	fx.state["runs"] = {
		"test-and-mark-stable.yml": [[active], [active], []],
		"promote-main-to-stable.yml": [[{"id": 902, "status": "queued"}], []],
		"mark-stable.yml": [[{"id": 903, "status": "completed"}]],
	}
	proc, state, events = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert sorted(state["cancels"]) == ["901", "902"]
	assert "Waiting for 1 cancelled release run(s) to stop." in proc.stdout
	assert fx.remote_peel("stable") == fx.sha["v1.45.0"]
	assert events[0] == "hold_put"


def test_release_still_running_keeps_tags_and_hold(fx: Fixture) -> None:
	fx.state["runs"] = {"mark-stable.yml": [[{"id": 905, "status": "in_progress"}]]}
	proc, state, events = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0", "STABLE_ROLLBACK_CANCEL_WAIT_SECS": "1"})
	assert proc.returncode == 1
	assert "STABLE_ROLLBACK outcome=release_still_running active_runs=1" in proc.stdout
	assert _hold_doc(state)["active"] is True
	assert not [e for e in events if e.startswith("push")]
	_assert_untouched(fx)


def test_rerun_is_idempotent(fx: Fixture) -> None:
	proc, _, _ = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	proc, state, events = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "STABLE_ROLLBACK outcome=already_rolled_back from=v1.45.0 to=v1.45.0 consumers_dispatched=2/2" in proc.stdout
	assert "STABLE_HOLD outcome=unchanged to=v1.45.0" in proc.stdout
	assert events == [], "no hold write and no tag push on a re-run"
	assert not state.get("cancels")
	assert len(state["dispatches"]) == 2


def test_blank_target_after_rollback_does_not_roll_back_further(fx: Fixture) -> None:
	proc, _, _ = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	proc, state, events = fx.run()
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "outcome=already_rolled_back from=v1.45.0 to=v1.45.0" in proc.stdout
	assert fx.remote_peel("stable") == fx.sha["v1.45.0"]


def test_partial_run_pushes_only_the_missing_tag(fx: Fixture) -> None:
	# A previous run wrote the hold and moved `stable`, then failed on `v1`.
	fx.set_remote_tag("stable", "v1.45.0")
	fx.state["hold_file"] = json.dumps({"schema": "stable_hold.v1", "active": True, "from_version": "v1.46.0", "to_version": "v1.45.0"})
	fx.state["hold_sha"] = "b" * 40
	proc, state, events = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "outcome=rolled_back" in proc.stdout
	assert [e.split()[-1] for e in events if e.startswith("push")] == ["refs/tags/v1"]
	assert fx.remote_peel("v1") == fx.sha["v1.45.0"]


def test_explicit_current_version_without_hold_is_refused(fx: Fixture) -> None:
	# Nothing was rolled back, so no hold may be written for the current version.
	proc, state, events = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.46.0"})
	assert proc.returncode == 1
	assert "STABLE_ROLLBACK outcome=refused reason=target_not_lower to=v1.46.0" in proc.stdout
	assert not state.get("hold_puts") and not events and not state.get("dispatches")
	_assert_untouched(fx)


def test_explicit_current_version_with_unreadable_hold_is_hold_unknown(fx: Fixture) -> None:
	fx.state["hold_read_error"] = True
	proc, state, events = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.46.0"})
	assert proc.returncode == 1
	assert "STABLE_ROLLBACK outcome=refused reason=hold_unknown to=v1.46.0" in proc.stdout
	assert not state.get("hold_puts") and not events
	_assert_untouched(fx)


def test_failed_cancel_is_retried_on_the_next_poll(fx: Fixture) -> None:
	active = {"id": 907, "status": "in_progress"}
	fx.state["cancel_fail_once"] = 1
	fx.state["runs"] = {"mark-stable.yml": [[active], [active], []]}
	proc, state, _ = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert state["cancels"] == ["907", "907"]
	assert fx.remote_peel("stable") == fx.sha["v1.45.0"]


def test_release_run_started_during_drain_is_cancelled(fx: Fixture) -> None:
	fx.state["runs"] = {"mark-stable.yml": [[], [{"id": 906, "status": "queued"}], []]}
	proc, state, _ = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert state["cancels"] == ["906"]
	assert fx.remote_peel("stable") == fx.sha["v1.45.0"]


def test_dry_run_changes_nothing(fx: Fixture) -> None:
	proc, state, events = fx.run({"STABLE_ROLLBACK_DRY_RUN": "true"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert f"STABLE_ROLLBACK outcome=dry_run from=v1.46.0 to=v1.45.0 target_sha={fx.sha['v1.45.0']} planned=hold,cancel_release_runs,tag:stable,tag:v1,notify_consumers" in proc.stdout
	assert events == []
	assert not state.get("dispatches") and not state.get("cancels") and not state.get("hold_puts")
	_assert_untouched(fx)
	assert "STABLE_ROLLBACK outcome=dry_run" in state["summary"]


def test_failed_consumer_dispatch_only_warns(fx: Fixture) -> None:
	fx.state["dispatch_fail"] = ["owner/consumer-b"]
	proc, state, _ = fx.run({"STABLE_ROLLBACK_TARGET_VERSION": "v1.45.0"})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "consumers_dispatched=1/2" in proc.stdout
	assert "owner/consumer-b: dispatch failed" in state["summary"]


def test_workflow_contract_text() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	assert "\t" not in text
	run_body = text.split("run: |", 1)[1]
	assert "${{" not in run_body, "dispatch inputs reach the script only through env:"
	assert "bash scripts/rollback_stable.sh" in run_body
	assert "STABLE_ROLLBACK_ENABLED: ${{ vars.STABLE_ROLLBACK_ENABLED || 'true' }}" in text
	assert "STABLE_HOLD_TOKEN: ${{ github.token }}" in text


def test_workflow_contract_structure() -> None:
	yaml = pytest.importorskip("yaml")
	wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	on = wf["on"]
	assert set(on) == {"workflow_dispatch"}
	inputs = on["workflow_dispatch"]["inputs"]
	assert inputs["target_version"]["required"] is False and inputs["target_version"]["default"] == ""
	assert inputs["reason"]["required"] is True
	assert inputs["dry_run"]["type"] == "boolean" and inputs["dry_run"]["default"] is False
	assert wf["permissions"] == {"contents": "write", "actions": "write"}
	assert wf["concurrency"] == {"group": "stable-rollback", "cancel-in-progress": False}
	steps = wf["jobs"]["rollback"]["steps"]
	assert steps[0]["with"]["fetch-depth"] == 0
	run_step = next(s for s in steps if "run" in s)
	assert run_step["env"]["STABLE_ROLLBACK_TARGET_VERSION"] == "${{ inputs.target_version }}"
	assert run_step["env"]["STABLE_ROLLBACK_REASON"] == "${{ inputs.reason }}"
	assert run_step["env"]["STABLE_ROLLBACK_DRY_RUN"] == "${{ inputs.dry_run }}"

