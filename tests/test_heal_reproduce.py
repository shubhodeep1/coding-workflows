#!/usr/bin/env python3
"""Tests for scripts/heal_reproduce.sh (plan P2, tracking #8434).

The script runs from a temporary trusted support directory with the real gate
wrapper (scripts/prepush_test_gate_run.sh), the real gate module, a fake
`codex_isolated_exec.sh` (image / prepare / cleanup) and a fake `docker` on
PATH that hands back a queued gate result. One case per row of the P2 step 4
mapping table, plus the wall-time timeout, no merge-base, worktree mode and
input-validation cases. Every case checks that the worktree and the private
temp root are gone and that no token reached the container steps.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "heal_reproduce.sh"
WRAPPER = REPO_ROOT / "scripts" / "prepush_test_gate_run.sh"
MODULE = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "prepush_affected_tests.py"
IMAGE = "sha256:" + "b" * 64
TOKEN = "ghp_healreproducetoken000000000000000000"
FINAL_RE = re.compile(r"^HEAL_REPRODUCE outcome=(\S+) reason=(\S+) tests=(\d+)$", re.M)

FAKE_DOCKER = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "__WORK__/docker.log"
env >> "__WORK__/docker_env.log"
if [ "$1" = run ]; then
	if [ -f "__WORK__/docker_sleep" ]; then exec sleep 60; fi
	if [ -f "__WORK__/docker_fail" ]; then exit 1; fi
	results=""
	for arg in "$@"; do
		case "$arg" in
			type=bind,src=*,dst=/results) results="${arg#type=bind,src=}"; results="${results%,dst=/results}" ;;
		esac
	done
	cp "__WORK__/gate_result.json" "$results/result.json"
	printf '## Pre-push test gate\n' > "$results/summary.md"
fi
exit 0
'''

FAKE_EXEC = r'''#!/usr/bin/env bash
env >> "__WORK__/exec_env.log"
case "$1" in
	image) echo "building" >&2; echo "__IMAGE__" ;;
	prepare)
		root="$(mktemp -d "__WORK__/prep.XXXXXX")"
		mkdir -p "$root/venv/bin"
		echo "prepare $*" >> "__WORK__/exec.log"
		echo "$root"
		;;
	cleanup)
		echo "$3" >> "__WORK__/cleanup.log"
		rm -rf -- "$3"
		;;
	*) exit 2 ;;
esac
'''


def _git(cwd: Path, *args: str) -> str:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
	return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def _gate(status: str, ran: list | None = None, failures: list | None = None, inherited: list | None = None,
		unverified: list | None = None, reason: str | None = None) -> dict:
	result = {"schema": "prepush_test_gate.v1", "status": status, "ran": ran if ran is not None else ["tests/test_a.py"],
		"failures": failures or [], "inherited": inherited or [], "unverified": unverified or [],
		"changed_assertions": [], "selected": 1, "elapsed_secs": 1}
	if reason:
		result["reason"] = reason
	return result


def _fail(path: str = "tests/test_a.py", names: list | None = None) -> dict:
	return {"test": path, "file": path, "names": names if names is not None else [f"{path}::test_x"], "tail": "FAILED"}


class Env:
	def __init__(self, tmp_path: Path):
		self.tmp = tmp_path
		self.work = tmp_path / "work"
		self.work.mkdir()
		self.src = tmp_path / "src"
		(self.src / "lib").mkdir(parents=True)
		(self.src / "tests").mkdir()
		(self.src / "lib" / "a.py").write_text("X = 1\n", encoding="utf-8")
		(self.src / "tests" / "test_a.py").write_text("from lib import a\n\ndef test_x():\n\tassert a.X == 1\n", encoding="utf-8")
		(self.src / "tests" / "test_b.py").write_text("def test_y():\n\tassert True\n", encoding="utf-8")
		_git(self.src, "init", "-q")
		_git(self.src, "add", "-A")
		_git(self.src, "commit", "-q", "-m", "base")
		self.base = _git(self.src, "rev-parse", "HEAD")
		(self.src / "lib" / "a.py").write_text("X = 2\n", encoding="utf-8")
		_git(self.src, "commit", "-q", "-am", "break")
		self.failing = _git(self.src, "rev-parse", "HEAD")
		self.support = tmp_path / "support" / "scripts"
		self.support.mkdir(parents=True)
		shutil.copy2(SCRIPT, self.support / SCRIPT.name)
		shutil.copy2(WRAPPER, self.support / WRAPPER.name)
		shutil.copy2(REPO_ROOT / "scripts" / "review_untrusted_workspace.py", self.support / "review_untrusted_workspace.py")
		shutil.copy2(MODULE, self.support / "prepush_affected_tests.py")
		fake_exec = self.support / "codex_isolated_exec.sh"
		fake_exec.write_text(FAKE_EXEC.replace("__WORK__", str(self.work)).replace("__IMAGE__", IMAGE), encoding="utf-8")
		fake_exec.chmod(0o755)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		docker = bin_dir / "docker"
		docker.write_text(FAKE_DOCKER.replace("__WORK__", str(self.work)), encoding="utf-8")
		docker.chmod(0o755)
		self.runner_temp = tmp_path / "rt"
		self.runner_temp.mkdir()
		self.env = {k: v for k, v in os.environ.items()
			if not k.startswith(("GITHUB_", "GIT_", "PREPUSH_", "WORKFLOW_HEAL_", "RUNNER_"))}
		self.env.update({
			"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
			"RUNNER_TEMP": str(self.runner_temp),
			"TMPDIR": str(self.runner_temp),
			"GH_TOKEN": TOKEN,
			"GITHUB_TOKEN": TOKEN,
		})

	def gate(self, result: dict) -> None:
		(self.work / "gate_result.json").write_text(json.dumps(result), encoding="utf-8")

	def tests_file(self, *lines: str) -> Path:
		path = self.tmp / "tests.txt"
		path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
		return path

	def run(self, tests: Path, *extra: str, sha: str | None = "failing", baseline: str | None = None,
			repo: Path | None = None, env: dict | None = None) -> tuple[dict | None, str]:
		out = self.tmp / "out" / "repro.json"
		args = ["bash", str(self.support / SCRIPT.name), "--tests-file", str(tests), "--out", str(out),
			"--baseline", baseline or self.base]
		if sha:
			args += ["--sha", self.failing if sha == "failing" else sha]
		args += list(extra)
		run_env = dict(self.env)
		run_env.update(env or {})
		proc = subprocess.run(args, cwd=repo or self.src, env=run_env, capture_output=True, text=True, timeout=180)
		assert proc.returncode == 0, proc.stderr
		data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
		return data, proc.stderr

	def read(self, name: str) -> str:
		path = self.work / name
		return path.read_text(encoding="utf-8") if path.exists() else ""

	def assert_clean(self, stderr: str, repo: Path | None = None, extra_worktrees: int = 0) -> None:
		listing = _git(repo or self.src, "worktree", "list", "--porcelain")
		assert listing.count("worktree ") == 1 + extra_worktrees, listing
		assert "heal-repro." not in listing
		assert not list(self.runner_temp.glob("heal-repro*")), list(self.runner_temp.iterdir())
		assert not list(self.work.glob("prep.*")), "prepare root left behind"
		prepared = self.read("exec.log").count("\n")
		assert self.read("cleanup.log").count("\n") == prepared
		assert len(FINAL_RE.findall(stderr)) == 1, stderr
		for name in ("docker.log", "docker_env.log", "exec_env.log"):
			assert TOKEN not in self.read(name), name
		assert TOKEN not in stderr


@pytest.fixture()
def h(tmp_path: Path) -> Env:
	return Env(tmp_path)


def _final(stderr: str) -> tuple[str, str, int]:
	status, reason, count = FINAL_RE.findall(stderr)[-1]
	return status, reason, int(count)


def test_caused_failure_is_reproduced(h: Env) -> None:
	h.gate(_gate("fail", failures=[_fail()]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert data == {"schema": "heal_repro.v1", "status": "reproduced", "reason": "regression",
		"failing_at": h.failing, "passing_at": h.base,
		"tests": [{"test": "tests/test_a.py::test_x", "status": "reproduced", "reason": "regression"}]}
	assert _final(err) == ("reproduced", "regression", 1)
	assert "run " in h.read("docker.log")
	assert "--venv" not in h.read("docker.log")  # the wrapper mounts it; the flag never reaches docker
	assert "dst=/opt/codex-venv" in h.read("docker.log")
	h.assert_clean(err)


def test_inherited_failure_is_pre_existing(h: Env) -> None:
	h.gate(_gate("pass", inherited=[{"test": "tests/test_a.py", "file": "tests/test_a.py", "names": ["tests/test_a.py::test_x"]}]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert (data["status"], data["reason"]) == ("not_reproduced", "pre_existing")
	assert data["tests"][0]["reason"] == "pre_existing"
	h.assert_clean(err)


@pytest.mark.parametrize("row", ["passes_at_both", "passes_at_failing_fails_at_green"])
def test_passing_at_failing_sha_is_never_reproduced(h: Env, row: str) -> None:
	# The gate re-runs only failed units on the baseline, so both rows come
	# back as gate `pass` with the test in `ran`.
	h.gate(_gate("pass", ran=["tests/test_a.py"]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert (data["status"], data["reason"]) == ("not_reproduced", "passes_at_failing_sha")
	assert data["tests"] == [{"test": "tests/test_a.py::test_x", "status": "not_reproduced", "reason": "passes_at_failing_sha"}]
	h.assert_clean(err)


@pytest.mark.parametrize("reason", ["baseline_error", "baseline_timeout", "timeout", "pytest_unavailable"])
def test_unit_unverified_passes_gate_reason(h: Env, reason: str) -> None:
	h.gate(_gate("unverified", unverified=[{"item": "tests/test_a.py", "reason": reason}]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert (data["status"], data["reason"]) == ("unverified", reason)
	assert data["tests"][0] == {"test": "tests/test_a.py::test_x", "status": "unverified", "reason": reason}
	h.assert_clean(err)


def test_rejected_list_line_is_unverified(h: Env) -> None:
	h.gate(_gate("fail", failures=[_fail()],
		unverified=[{"item": "tests_file:line2", "reason": "tests_file_invalid_name"}]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x", "tests/test_b.py::bad;name"))
	assert (data["status"], data["reason"]) == ("unverified", "tests_file_invalid_name")
	assert [t["status"] for t in data["tests"]] == ["reproduced", "unverified"]
	h.assert_clean(err)


def test_mixed_list_is_not_reproduced(h: Env) -> None:
	h.gate(_gate("fail", ran=["tests/test_a.py", "tests/test_b.py"], failures=[_fail()]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x", "tests/test_b.py::test_y"))
	assert (data["status"], data["reason"]) == ("not_reproduced", "passes_at_failing_sha")
	assert [(t["status"], t["reason"]) for t in data["tests"]] == [
		("reproduced", "regression"), ("not_reproduced", "passes_at_failing_sha")]
	assert _final(err) == ("not_reproduced", "passes_at_failing_sha", 2)
	h.assert_clean(err)


def test_named_test_absent_from_failed_unit_names_passes(h: Env) -> None:
	h.gate(_gate("fail", failures=[_fail(names=["tests/test_a.py::test_other", "tests/test_a.py::test_p[1]"])]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x", "tests/test_a.py::test_p"))
	assert [(t["status"], t["reason"]) for t in data["tests"]] == [
		("not_reproduced", "passes_at_failing_sha"), ("reproduced", "regression")]
	assert data["status"] == "not_reproduced"
	h.assert_clean(err)


def test_whole_file_entry_in_failed_unit_is_reproduced(h: Env) -> None:
	h.gate(_gate("fail", failures=[_fail(names=[])]))
	data, err = h.run(h.tests_file("tests/test_a.py"))
	assert (data["status"], data["reason"]) == ("reproduced", "regression")
	h.assert_clean(err)


def test_wall_timeout_is_unverified_and_cleans_up(h: Env) -> None:
	h.gate(_gate("fail", failures=[_fail()]))
	(h.work / "docker_sleep").write_text("1", encoding="utf-8")
	start = time.monotonic()
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"), env={"WORKFLOW_HEAL_REPRODUCE_TIMEOUT_SECS": "5"})
	assert time.monotonic() - start < 60
	assert (data["status"], data["reason"]) == ("unverified", "timeout")
	assert data["tests"] == [{"test": "tests/test_a.py::test_x", "status": "unverified", "reason": "timeout"}]
	assert data["failing_at"] == h.failing and data["passing_at"] == h.base
	assert "phase=wall outcome=timeout secs=5" in err
	h.assert_clean(err)


def test_no_merge_base_skips_docker(h: Env) -> None:
	_git(h.src, "checkout", "-q", "--orphan", "unrelated")
	_git(h.src, "rm", "-rq", "--cached", ".")
	(h.src / "other.txt").write_text("x\n", encoding="utf-8")
	_git(h.src, "add", "other.txt")
	_git(h.src, "commit", "-q", "-m", "unrelated")
	unrelated = _git(h.src, "rev-parse", "HEAD")
	_git(h.src, "checkout", "-q", "-f", h.failing)
	h.gate(_gate("fail", failures=[_fail()]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"), baseline=unrelated)
	assert (data["status"], data["reason"]) == ("unverified", "no_merge_base")
	assert data["tests"][0]["reason"] == "no_merge_base"
	assert data["passing_at"] == unrelated
	assert "run " not in h.read("docker.log")
	# Both commits are local, so git treats the fetch as a no-op; the ladder still ran.
	assert "HEAL_REPRODUCE phase=fetch outcome=" in err
	h.assert_clean(err)


def test_gate_level_failure_marks_every_test(h: Env) -> None:
	(h.work / "docker_fail").write_text("1", encoding="utf-8")
	h.gate(_gate("fail", failures=[_fail()]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x", "tests/test_b.py"))
	assert (data["status"], data["reason"]) == ("unverified", "container_rc_1")
	assert {t["reason"] for t in data["tests"]} == {"container_rc_1"}
	h.assert_clean(err)


def test_gate_merge_base_item_maps_to_no_merge_base(h: Env) -> None:
	h.gate(_gate("unverified", ran=[], unverified=[{"item": "merge_base", "reason": "no_merge_base"}]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert (data["status"], data["reason"]) == ("unverified", "no_merge_base")
	h.assert_clean(err)


def test_invalid_timeout_minutes_falls_back(h: Env) -> None:
	h.gate(_gate("fail", failures=[_fail()]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"), env={"WORKFLOW_HEAL_REPRODUCE_TIMEOUT_MINUTES": "abc"})
	assert "phase=config outcome=config_invalid name=WORKFLOW_HEAL_REPRODUCE_TIMEOUT_MINUTES fallback=10" in err
	assert data["status"] == "reproduced"
	# 10 minutes less the 180 s reserve leaves a 7-minute gate (wall = 7*60+120);
	# floor division gives 6 once the inner pass's setup crosses a second.
	assert re.search(r"--timeout-minutes [67] ", h.read("docker.log"))
	h.assert_clean(err)


def test_worktree_mode_keeps_callers_tree(h: Env) -> None:
	_git(h.src, "checkout", "-q", h.base)
	tree = h.tmp / "caller-tree"
	_git(h.src, "worktree", "add", "-q", "--detach", str(tree), h.failing)
	h.gate(_gate("fail", failures=[_fail()]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"), "--worktree", str(tree), sha=None, repo=h.tmp)
	assert (data["status"], data["failing_at"]) == ("reproduced", h.failing)
	assert (tree / "lib" / "a.py").read_text(encoding="utf-8") == "X = 2\n"
	h.assert_clean(err, extra_worktrees=1)


def test_shallow_clone_is_deepened(h: Env) -> None:
	clone = h.tmp / "shallow"
	_git(h.tmp, "clone", "-q", "--depth", "1", "--no-local", f"file://{h.src}", str(clone))
	assert _git(clone, "rev-parse", "--is-shallow-repository") == "true"
	h.gate(_gate("fail", failures=[_fail()]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"), "--repo", str(clone))
	assert "phase=fetch outcome=ok depth=50" in err
	assert (data["status"], data["passing_at"]) == ("reproduced", h.base)
	h.assert_clean(err, repo=clone)


def test_empty_list_is_no_tests(h: Env) -> None:
	h.gate(_gate("fail", failures=[_fail()]))
	data, err = h.run(h.tests_file("# comment only", ""))
	assert (data["status"], data["reason"], data["tests"]) == ("unverified", "no_tests", [])
	assert _final(err) == ("unverified", "no_tests", 0)
	assert h.read("docker.log") == "" and h.read("exec.log") == ""
	h.assert_clean(err)


def test_symlinked_list_is_refused(h: Env) -> None:
	secret = h.tmp / "secret.txt"
	secret.write_text("tests/test_a.py\n", encoding="utf-8")
	link = h.tmp / "link.txt"
	link.symlink_to(secret)
	data, err = h.run(link)
	assert (data["status"], data["reason"]) == ("unverified", "tests_file_not_regular")
	assert h.read("docker.log") == ""
	h.assert_clean(err)


@pytest.mark.parametrize("extra,reason", [
	(["--worktree", "/nonexistent"], "usage_error"),
	(["--remote", "-upload-pack=x"], "invalid_remote"),
])
def test_bad_arguments_are_unverified(h: Env, extra: list, reason: str) -> None:
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"), *extra)
	assert (data["status"], data["reason"]) == ("unverified", reason)
	assert data["tests"][0]["reason"] == reason
	assert h.read("docker.log") == ""
	h.assert_clean(err)


def test_invalid_sha_is_refused(h: Env) -> None:
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"), sha="HEAD;id")
	assert (data["status"], data["reason"]) == ("unverified", "invalid_sha")
	h.assert_clean(err)


def test_whole_list_rejection_carries_gate_reason(h: Env) -> None:
	h.gate(_gate("unverified", ran=[], unverified=[{"item": "tests_file", "reason": "tests_file_unreadable"}]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert (data["status"], data["reason"]) == ("unverified", "tests_file_unreadable")
	assert data["tests"][0]["reason"] == "tests_file_unreadable"
	h.assert_clean(err)


def test_inherited_unit_keeps_pre_existing_for_unparsed_names(h: Env) -> None:
	# Inherited entries carry no tail, so a missing name is not proof of a pass.
	h.gate(_gate("pass", inherited=[{"test": "tests/test_a.py", "file": "tests/test_a.py",
		"names": ["tests/test_a.py::test_other"]}]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x", "tests/test_a.py::test_other"))
	assert [(t["status"], t["reason"]) for t in data["tests"]] == [
		("not_reproduced", "pre_existing"), ("not_reproduced", "pre_existing")]
	h.assert_clean(err)


def test_named_test_with_error_line_is_reproduced(h: Env) -> None:
	# pytest reports setup/teardown errors as `ERROR path::name`, which the gate's names miss.
	fail = _fail(names=["tests/test_a.py::test_other"])
	fail["tail"] = "FAILED tests/test_a.py::test_other - assert 0\nERROR tests/test_a.py::test_x - fixture\n"
	h.gate(_gate("fail", failures=[fail]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert (data["status"], data["reason"]) == ("reproduced", "regression")
	h.assert_clean(err)


@pytest.mark.parametrize("shape", ["full_tail", "name_cap", "no_tail"])
def test_named_test_absent_from_incomplete_names_is_unverified(h: Env, shape: str) -> None:
	fail = _fail(names=["tests/test_a.py::test_other"])
	if shape == "full_tail":
		fail["tail"] = "x" * 4096
	elif shape == "name_cap":
		fail["names"] = [f"tests/test_a.py::test_n{i}" for i in range(50)]
	else:
		fail.pop("tail")
	h.gate(_gate("fail", failures=[fail]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert (data["status"], data["reason"]) == ("unverified", "names_incomplete")
	h.assert_clean(err)


def test_bare_name_in_failed_script_unit_is_reproduced(h: Env) -> None:
	# Script-style and unittest units report bare names (FAIL: test_x (...)).
	fail = _fail(names=["test_x"])
	fail["tail"] = "FAIL: test_x (__main__.T)\n"
	h.gate(_gate("fail", failures=[fail]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert (data["status"], data["reason"]) == ("reproduced", "regression")
	h.assert_clean(err)


def test_error_text_mid_line_does_not_count(h: Env) -> None:
	fail = _fail(names=["tests/test_a.py::test_other"])
	fail["tail"] = "FAILED tests/test_a.py::test_other - assert 0\nlog: ERROR tests/test_a.py::test_x - noise\n"
	h.gate(_gate("fail", failures=[fail]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert (data["status"], data["reason"]) == ("not_reproduced", "passes_at_failing_sha")
	h.assert_clean(err)


def test_class_qualified_name_matching_only_a_bare_name_is_unverified(h: Env) -> None:
	# A bare test_x may belong to another class's failing test_x.
	fail = _fail(names=["test_x"])
	fail["tail"] = "FAIL: test_x (__main__.ClassA)\n"
	h.gate(_gate("fail", failures=[fail]))
	data, err = h.run(h.tests_file("tests/test_a.py::ClassB::test_x"))
	assert (data["status"], data["reason"]) == ("unverified", "names_ambiguous")
	h.assert_clean(err)


def test_bare_name_in_pytest_unit_is_unverified(h: Env) -> None:
	# In a pytest unit, real failures are full nodes; a bare name came from captured output.
	fail = _fail(names=["tests/test_a.py::test_other", "test_x"])
	fail["tail"] = "FAILED tests/test_a.py::test_other - assert 0\nFAIL: test_x (printed)\n"
	h.gate(_gate("fail", failures=[fail]))
	data, err = h.run(h.tests_file("tests/test_a.py::test_x"))
	assert (data["status"], data["reason"]) == ("unverified", "names_ambiguous")
	h.assert_clean(err)
