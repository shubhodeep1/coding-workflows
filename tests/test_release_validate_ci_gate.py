#!/usr/bin/env python3
"""Contract tests for the release validate-job CI gate and tagging switch.

Security finding #6548 (Refs #7029): the `validate` jobs of both stable
release workflows used to read the legacy combined commit status, which never
contains GitHub Actions check-runs. They now call the bounded release CI gate
(`scripts/release_ci_gate.sh`). `gate_only` runs of test-and-mark-stable.yml
only warn. Both `release` jobs carry a default-on `RELEASE_TAGGING_ENABLED`
kill switch that skips every tag/release/dispatch/changelog write.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
MARK = ".github/workflows/mark-stable.yml"
TAMS = ".github/workflows/test-and-mark-stable.yml"
VALIDATE_STEPS = {
	MARK: "Verify CI passed on stable",
	TAMS: "Verify CI passed on source branch",
}
GATE = "Require CI / lint success on the tested commit"
RESOLVER = "Resolve release tagging switch"
ASSEMBLE = "Assemble changelog fragments"
WRITE_STEPS = (
	"Tag version and update stable pointer",
	"Create GitHub Release",
	"Notify consumer repos via repository_dispatch",
)
EXPR_OPEN = "$" + "{" + "{"
SWITCH_ENV = EXPR_OPEN + " vars.RELEASE_TAGGING_ENABLED || 'true' }}"


def _load(wf: str) -> dict:
	return yaml.safe_load((REPO_ROOT / wf).read_text(encoding="utf-8"))


def _step(job: dict, name: str) -> dict:
	for step in job["steps"]:
		if step.get("name") == name:
			return step
	raise AssertionError(f"step {name!r} not found")


def _names(job: dict) -> list:
	return [step.get("name") for step in job["steps"]]


def test_validate_jobs_have_no_status_api_reads() -> None:
	for wf in (MARK, TAMS):
		job = _load(wf)["jobs"]["validate"]
		for step in job["steps"]:
			run = step.get("run") or ""
			assert not ("/commits/" in run and "/status" in run), f"{wf}: {step.get('name')} still reads the combined status"


def test_validate_steps_call_release_ci_gate() -> None:
	for wf, name in VALIDATE_STEPS.items():
		job = _load(wf)["jobs"]["validate"]
		step = _step(job, name)
		run = step.get("run") or ""
		assert "scripts/release_ci_gate.sh" in run, f"{wf}: {name} must call release_ci_gate.sh"
		assert EXPR_OPEN not in run, f"{wf}: {name} run body must not contain workflow expressions"
		env = step.get("env") or {}
		for key in ("GH_TOKEN", "GATE_REPO", "RELEASE_CI_GATE_WAIT_SECS", "RELEASE_CI_GATE_POLL_SECS"):
			assert key in env, f"{wf}: {name} env must set {key}"
		assert step.get("if") is None, f"{wf}: {name} must not be conditional"
		assert not step.get("continue-on-error"), f"{wf}: {name} must not continue on error"
		perms = job.get("permissions") or {}
		for key, value in (("contents", "read"), ("checks", "read"), ("actions", "read")):
			assert perms.get(key) == value, f"{wf}: validate permissions must include {key}: {value}, got {perms}"
		assert int(job.get("timeout-minutes", 0)) >= 35, f"{wf}: validate timeout must cover the gate wait"
	assert "GATE_ONLY" in (_step(_load(TAMS)["jobs"]["validate"], VALIDATE_STEPS[TAMS]).get("env") or {})
	assert "GATE_ONLY" not in (_step(_load(MARK)["jobs"]["validate"], VALIDATE_STEPS[MARK]).get("env") or {})


def test_release_jobs_carry_default_on_tagging_switch() -> None:
	for wf in (MARK, TAMS):
		job = _load(wf)["jobs"]["release"]
		names = _names(job)
		assert RESOLVER in names, f"{wf}: release job has no {RESOLVER!r} step"
		resolver = _step(job, RESOLVER)
		assert resolver.get("id") == "release_tagging"
		assert resolver.get("if") is None
		assert (resolver.get("env") or {}).get("RELEASE_TAGGING_ENABLED") == SWITCH_ENV
		assert EXPR_OPEN not in (resolver.get("run") or "")
		assert names.index(GATE) < names.index(RESOLVER) < names.index(ASSEMBLE), f"{wf}: resolver must sit between the gate and {ASSEMBLE!r}"
		for name in WRITE_STEPS:
			cond = str(_step(job, name).get("if") or "")
			assert "steps.release_tagging.outputs.enabled == 'true'" in cond, f"{wf}: {name} must be gated on the switch"
			assert "!inputs.dry_run" in cond, f"{wf}: {name} must keep the dry-run guard"
		assemble = _step(job, ASSEMBLE)
		run = assemble.get("run") or ""
		assert "RELEASE_TAGGING" in (assemble.get("env") or {})
		guard = run.find('"${RELEASE_TAGGING:-}" != "true"')
		commit = run.find("git commit")
		assert 0 <= guard < commit, f"{wf}: {ASSEMBLE} must check RELEASE_TAGGING before committing"


def _run_body(body: str, env_extra: dict, *, gate_rc: int | None) -> subprocess.CompletedProcess:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		subprocess.run(["git", "init", "-q", str(root)], check=True)
		git_env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
			GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
		for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
			git_env.pop(key, None)
		(root / "f").write_text("x\n")
		subprocess.run(["git", "-C", str(root), "add", "f"], check=True, env=git_env)
		subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=git_env)
		seen = root / "seen_wait"
		if gate_rc is not None:
			(root / "scripts").mkdir()
			(root / "scripts" / "release_ci_gate.sh").write_text(
				"#!/usr/bin/env bash\n"
				f"printf '%s' \"${{RELEASE_CI_GATE_WAIT_SECS:-unset}}\" > '{seen}'\n"
				f"if [ {gate_rc} -ne 0 ]; then echo '::error::RELEASE_CI_GATE repo=x outcome=fail reason=wait_timeout' >&2; fi\n"
				f"exit {gate_rc}\n"
			)
		out_file = root / "gh_output"
		env = dict(git_env, SOURCE_BRANCH="stable", GATE_REPO="o/r", GH_TOKEN="dummy",
			RELEASE_CI_GATE_WAIT_SECS="1800", RELEASE_CI_GATE_POLL_SECS="30",
			GITHUB_OUTPUT=str(out_file), GITHUB_STEP_SUMMARY=str(root / "summary"))
		env.update(env_extra)
		for key, value in list(env_extra.items()):
			if value is None:
				env.pop(key, None)
		proc = subprocess.run(["bash", "-c", body], cwd=root, env=env, capture_output=True, text=True)
		proc.seen_wait = seen.read_text() if seen.exists() else None  # type: ignore[attr-defined]
		proc.gh_output = out_file.read_text() if out_file.exists() else ""  # type: ignore[attr-defined]
		return proc


def _validate_body(wf: str) -> str:
	return _step(_load(wf)["jobs"]["validate"], VALIDATE_STEPS[wf])["run"]


def test_validate_bodies_fail_closed_for_real_releases() -> None:
	for wf, extra in ((MARK, {}), (TAMS, {"GATE_ONLY": "false"})):
		body = _validate_body(wf)
		assert _run_body(body, extra, gate_rc=0).returncode == 0, f"{wf}: passing gate must pass"
		failed = _run_body(body, extra, gate_rc=1)
		assert failed.returncode != 0, f"{wf}: failing gate must fail the step"
		assert failed.seen_wait == "1800"  # type: ignore[attr-defined]
		assert _run_body(body, extra, gate_rc=None).returncode != 0, f"{wf}: missing gate script must fail closed"


def test_gate_only_is_warning_only() -> None:
	body = _validate_body(TAMS)
	failed = _run_body(body, {"GATE_ONLY": "true"}, gate_rc=1)
	combined = failed.stdout + failed.stderr
	assert failed.returncode == 0, combined
	assert "::warning::" in combined
	assert "::error::" not in combined
	assert failed.seen_wait == "0"  # type: ignore[attr-defined]
	missing = _run_body(body, {"GATE_ONLY": "true"}, gate_rc=None)
	assert missing.returncode == 0
	assert "::warning::" in missing.stdout + missing.stderr
	assert _run_body(body, {"GATE_ONLY": "TRUE"}, gate_rc=0).returncode == 0


def test_tagging_switch_resolution() -> None:
	for wf in (MARK, TAMS):
		body = _step(_load(wf)["jobs"]["release"], RESOLVER)["run"]
		for value in (None, "", "true", " TRUE "):
			proc = _run_body(body, {"RELEASE_TAGGING_ENABLED": value}, gate_rc=0)
			assert proc.returncode == 0, proc.stderr
			assert "enabled=true" in proc.gh_output, (wf, value, proc.gh_output)  # type: ignore[attr-defined]
		for value in ("false", "0", "no", "off", "$(id)"):
			proc = _run_body(body, {"RELEASE_TAGGING_ENABLED": value}, gate_rc=0)
			assert proc.returncode == 0, proc.stderr
			assert "enabled=false" in proc.gh_output, (wf, value, proc.gh_output)  # type: ignore[attr-defined]
			assert "::warning::" in proc.stdout
			assert "$(" not in proc.stdout


def main() -> None:
	tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
	for test in tests:
		test()
		print(f"PASS {test.__name__}")
	print(f"OK: {len(tests)} release validate-gate contract tests passed")


if __name__ == "__main__":
	main()
