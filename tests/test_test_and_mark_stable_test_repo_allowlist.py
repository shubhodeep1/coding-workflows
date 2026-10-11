#!/usr/bin/env python3
"""Contract and behaviour checks for the release gate's smoke-test target
allowlist (security finding ``smoke-target-cross-repo-write``, issue #6547).

``test-and-mark-stable.yml`` used to accept any syntactically valid
``test_repo`` and then write to it with ``GH_PAT``. The ``source`` job's
``validate-test-repo`` step now fails closed unless the target is this
repository or is listed in ``.github/ai/smoke_test_repos.json`` on the
default branch."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
ALLOWLIST = REPO_ROOT / ".github" / "ai" / "smoke_test_repos.json"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SOURCE_REPO = "Owner/Source-Repo"
# GitHub-hosted runners ship jq; the step needs it only past the self/default checks.
requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _workflow() -> dict:
	return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step() -> dict:
	for step in _workflow()["jobs"]["source"]["steps"]:
		if step.get("id") == "validate-test-repo":
			return step
	raise AssertionError("validate-test-repo step not found in jobs.source")


def _needs(job: dict) -> list[str]:
	needs = job.get("needs") or []
	return [needs] if isinstance(needs, str) else list(needs)


def _reaches_source(jobs: dict, name: str, seen: set[str] | None = None) -> bool:
	seen = seen or set()
	if name in seen:
		return False
	seen.add(name)
	for dep in _needs(jobs[name]):
		if dep == "source" or _reaches_source(jobs, dep, seen):
			return True
	return False


def test_step_is_in_source_job_and_binds_values_through_env() -> None:
	step = _step()
	assert "${{" not in step["run"]
	env = step["env"]
	assert env["GH_TOKEN"] == "${{ github.token }}"
	assert env["TEST_REPO_INPUT"] == "${{ inputs.test_repo }}"
	assert env["SOURCE_REPO"] == "${{ github.repository }}"
	assert env["DEFAULT_BRANCH"] == "${{ github.event.repository.default_branch }}"
	assert env["SMOKE_TEST_REPO_ALLOWLIST_PATH"] == ".github/ai/smoke_test_repos.json"
	assert "GH_PAT" not in json.dumps(step)


def test_every_job_writing_to_test_repo_depends_on_source() -> None:
	jobs = _workflow()["jobs"]
	writers = [name for name, job in jobs.items() if "TEST_REPO" in (job.get("env") or {})]
	assert len(writers) >= 7, writers
	for name in writers:
		assert _reaches_source(jobs, name), f"{name} sets TEST_REPO but does not depend on source"


def test_allowlist_file_is_an_array_of_owner_repo_strings() -> None:
	data = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
	assert isinstance(data, list)
	for entry in data:
		assert isinstance(entry, str) and REPO_RE.match(entry), entry


def _run(test_repo: str, gh_body: str | None, gh_rc: int = 0) -> tuple[int, str, str]:
	"""Run the step script with a stub ``gh``. Returns (rc, stdout, gh calls)."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		calls = tmp / "gh_calls.txt"
		body = tmp / "body.json"
		body.write_text(gh_body or "", encoding="utf-8")
		gh = bin_dir / "gh"
		gh.write_text(
			"#!/usr/bin/env bash\n"
			f'printf "%s\\n" "$*" >> "{calls}"\n'
			f'cat "{body}"\n'
			f"exit {gh_rc}\n",
			encoding="utf-8",
		)
		gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
		env = {
			k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")
		}
		env.update(
			{
				"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
				"GH_TOKEN": "dummy",
				"TEST_REPO_INPUT": test_repo,
				"SOURCE_REPO": SOURCE_REPO,
				"DEFAULT_BRANCH": "main",
				"SMOKE_TEST_REPO_ALLOWLIST_PATH": ".github/ai/smoke_test_repos.json",
			}
		)
		proc = subprocess.run(
			["bash", "-c", _step()["run"]],
			env=env,
			capture_output=True,
			text=True,
			check=False,
		)
		gh_calls = calls.read_text(encoding="utf-8") if calls.exists() else ""
		return proc.returncode, proc.stdout + proc.stderr, gh_calls


def test_empty_input_is_allowed_without_api_call() -> None:
	rc, out, calls = _run("", None)
	assert rc == 0, out
	assert "outcome=allowed reason=default" in out
	assert calls == ""


def test_self_is_allowed_case_insensitively_without_api_call() -> None:
	for value in (SOURCE_REPO, SOURCE_REPO.lower(), SOURCE_REPO.upper()):
		rc, out, calls = _run(value, None)
		assert rc == 0, out
		assert "outcome=allowed reason=self" in out
		assert calls == ""


def test_invalid_format_is_rejected_without_api_call() -> None:
	for value in ("bad repo", "owner", "a/b/c", "owner/repo;rm"):
		rc, out, calls = _run(value, None)
		assert rc == 1, (value, out)
		assert "reason=invalid_format" in out
		assert calls == ""


@requires_jq
def test_listed_repo_is_allowed_and_read_from_default_branch() -> None:
	rc, out, calls = _run("Other/Smoke", json.dumps(["other/smoke"]))
	assert rc == 0, out
	assert "outcome=allowed reason=listed" in out
	assert "repos/Owner/Source-Repo/contents/.github/ai/smoke_test_repos.json?ref=main" in calls


@requires_jq
def test_unlisted_repo_is_rejected() -> None:
	rc, out, _ = _run("victim/private", json.dumps(["other/smoke"]))
	assert rc == 1, out
	assert "reason=not_listed" in out
	rc, out, _ = _run("victim/private", "[]")
	assert rc == 1, out
	assert "reason=not_listed" in out


@requires_jq
def test_unavailable_allowlist_fails_closed() -> None:
	rc, out, _ = _run("other/smoke", '{"message": "Not Found"}', gh_rc=1)
	assert rc == 1, out
	assert "reason=allowlist_unavailable" in out


@requires_jq
def test_malformed_allowlist_fails_closed() -> None:
	for body in ('{"repos": ["other/smoke"]}', '["other/smoke", 3]', '["other/smoke", "bad repo"]', "not json"):
		rc, out, _ = _run("other/smoke", body)
		assert rc == 1, (body, out)
		assert "reason=allowlist_malformed" in out


if __name__ == "__main__":
	raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
