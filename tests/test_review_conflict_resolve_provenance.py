#!/usr/bin/env python3
"""Contract tests for the resolver provenance check (issue #9842).

Phase 3 of docs/plans/pipeline-human-wait-elimination-plan.md: after the
conflict resolver's output passes the marker and fingerprint gates,
scripts/review_conflict_resolve.sh checks that every resolved region comes
from "ours" or "theirs". A region whose lines match neither side fails soft
validation, retries once with a prompt naming the hunks, and escalates on the
next failure. RESOLVER_PROVENANCE_CHECK_ENABLED=false restores the previous
behaviour.

The helper functions run against real temporary git repositories with an
in-progress conflicting merge.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
STAGE_SCRIPT = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
PRELUDE = REPO_ROOT / "prompts" / "conflict-resolver-retry-provenance-prelude.txt"

_GIT_PINNING_ENV = (
	"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
	"GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "BASH_ENV", "ENV",
	"RESOLVER_PROVENANCE_CHECK_ENABLED", "RESOLVER_PROVENANCE_RETRY_MAX",
	"RESOLVER_PROVENANCE_REPORT_FILE",
)

BASE_CFG = "name: ci\n  A: 1\n  ZOMBIE_RUN_REAPER_ENABLED: 'false'\nfooter\n"
OURS_CFG = "name: ci\n  A: 2\n  ZOMBIE_RUN_REAPER_ENABLED: 'false'\nfooter\n"
THEIRS_CFG = "name: ci\n  A: 3\n  ZOMBIE_RUN_REAPER_ENABLED: 'false'\nfooter\n"


def _src() -> str:
	return RESOLVE_SCRIPT.read_text(encoding="utf-8")


def _function(name: str) -> str:
	match = re.search(rf"^{re.escape(name)}\(\)\n\{{\n.*?\n\}}\n", _src(), re.M | re.S)
	assert match, f"function {name} not found"
	return match.group(0)


def _build_retry_prompt_source() -> str:
	src = _src()
	start = src.index("_build_retry_prompt() {")
	return src[start:src.index("\n}\n", start) + 2]


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
	env = {key: value for key, value in os.environ.items() if key not in _GIT_PINNING_ENV}
	env.update({
		"GIT_CONFIG_GLOBAL": os.devnull,
		"GIT_CONFIG_NOSYSTEM": "1",
		"GIT_AUTHOR_NAME": "t",
		"GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t",
		"GIT_COMMITTER_EMAIL": "t@example.invalid",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	env.update(extra or {})
	return env


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
	return subprocess.run(["git", *args], cwd=repo, env=_env(), capture_output=True, check=check)


def _apply(repo: Path, files: dict[str, object]) -> None:
	for name, content in files.items():
		path = repo / name
		if content is None:
			if path.exists():
				_git(repo, "rm", "-q", "--", name)
			continue
		data = content.encode("utf-8") if isinstance(content, str) else content
		path.write_bytes(data)
		_git(repo, "add", "--", name)


def _conflict_repo(tmp: Path, base: dict, ours: dict, theirs: dict) -> Path:
	if shutil.which("git") is None or shutil.which("bash") is None:
		pytest.skip("git/bash not available")
	repo = tmp / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_apply(repo, base or {"seed.txt": "seed\n"})
	_git(repo, "commit", "-q", "-m", "base")
	_git(repo, "checkout", "-q", "-b", "theirs")
	_apply(repo, theirs)
	_git(repo, "commit", "-q", "-m", "theirs")
	_git(repo, "checkout", "-q", "main")
	_apply(repo, ours)
	_git(repo, "commit", "-q", "-m", "ours")
	merge = _git(repo, "merge", "-q", "--no-edit", "theirs", check=False)
	assert merge.returncode != 0, "fixture merge was expected to conflict"
	return repo


def _run_check(
	repo: Path,
	allowlist: list[str],
	resolutions: dict[str, object],
	*,
	paired: str = "",
	env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
	runtime = repo.parent / "runtime"
	runtime.mkdir(exist_ok=True)
	index_file = runtime / "unmerged.z"
	index_file.write_bytes(_git(repo, "ls-files", "-u", "-z").stdout)
	(runtime / "allow.txt").write_text("".join(f"{p}\n" for p in allowlist), encoding="utf-8")
	(runtime / "paired.txt").write_text(paired, encoding="utf-8")
	for name, content in resolutions.items():
		path = repo / name
		if isinstance(content, tuple) and content[0] == "symlink":
			if path.exists() or path.is_symlink():
				path.unlink()
			os.symlink(content[1], path)
			continue
		path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
	report = runtime / "report.txt"
	program = (
		"set -euo pipefail\n"
		f"RESOLVER_UNMERGED_INDEX_FILE='{index_file}'\n"
		f"RESOLVER_ALLOWLIST_FILE='{runtime / 'allow.txt'}'\n"
		f"RESOLVER_PAIRED_LIVE_FILE='{runtime / 'paired.txt'}'\n"
		f"RESOLVER_PROVENANCE_STAGES_DIR='{runtime / 'stages'}'\n"
		f"RESOLVER_PROVENANCE_REPORT_FILE='{report}'\n"
		+ _function("_capture_resolution_provenance_stages")
		+ _function("_verify_resolution_provenance")
		+ "_capture_resolution_provenance_stages\n"
		+ "printf 'READY=%s\\n' \"${_resolver_provenance_ready}\"\n"
		+ "rc=0\n_verify_resolution_provenance || rc=$?\n"
		+ "printf 'RC=%s HUNKS=%s\\n' \"${rc}\" \"${_provenance_hunk_count}\"\n"
	)
	proc = subprocess.run(["bash", "-c", program], cwd=repo, env=_env(env), capture_output=True, text=True)
	assert proc.returncode == 0, proc.stderr
	return proc, report


def _result(proc: subprocess.CompletedProcess[str]) -> tuple[int, int]:
	match = re.search(r"^RC=(\d+) HUNKS=(\d+)$", proc.stdout, re.M)
	assert match, proc.stdout
	return int(match.group(1)), int(match.group(2))


def _cfg_repo(tmp_path: Path) -> Path:
	repo = _conflict_repo(tmp_path, {"cfg.yml": BASE_CFG}, {"cfg.yml": OURS_CFG}, {"cfg.yml": THEIRS_CFG})
	# The check pins a two-sided merge style; a repository-level diff3 setting
	# must not turn base-only lines into unchanged context.
	_git(repo, "config", "merge.conflictStyle", "diff3")
	return repo


@pytest.mark.parametrize(
	"resolution",
	[
		"name: ci\n  A: 2\n  A: 3\n  ZOMBIE_RUN_REAPER_ENABLED: 'false'\nfooter\n",
		OURS_CFG,
		THEIRS_CFG,
		"name: ci\n    A:   2  \n  ZOMBIE_RUN_REAPER_ENABLED: 'false'\nfooter\n",
	],
	ids=["union", "ours", "theirs", "whitespace"],
)
def test_resolution_from_either_side_passes(tmp_path, resolution):
	repo = _cfg_repo(tmp_path)
	proc, report = _run_check(repo, ["cfg.yml"], {"cfg.yml": resolution})
	assert "READY=1" in proc.stdout
	assert _result(proc) == (0, 0)
	assert "Resolver provenance: path=cfg.yml outcome=ok hunks=0" in proc.stdout
	assert not report.exists()


def test_df30f80a_replay_flipped_flag_is_foreign(tmp_path):
	repo = _cfg_repo(tmp_path)
	resolution = "name: ci\n  A: 2\n  ZOMBIE_RUN_REAPER_ENABLED: 'true'\nfooter\n"
	proc, report = _run_check(repo, ["cfg.yml"], {"cfg.yml": resolution})
	assert _result(proc) == (1, 1)
	assert "Resolver provenance: path=cfg.yml outcome=foreign_hunks hunks=1" in proc.stdout
	text = report.read_text(encoding="utf-8")
	assert "--- cfg.yml lines 3..3 ---" in text
	assert "+  ZOMBIE_RUN_REAPER_ENABLED: 'true'" in text
	# Hunk text never reaches the log.
	assert "'true'" not in proc.stdout


@pytest.mark.parametrize("value", ["  A: 4", "  A: 1"], ids=["invented", "base_only"])
def test_invented_or_base_only_line_is_foreign(tmp_path, value):
	repo = _cfg_repo(tmp_path)
	resolution = f"name: ci\n{value}\n  ZOMBIE_RUN_REAPER_ENABLED: 'false'\nfooter\n"
	proc, _report = _run_check(repo, ["cfg.yml"], {"cfg.yml": resolution})
	assert _result(proc) == (1, 1)


def test_binary_file_is_skipped(tmp_path):
	repo = _conflict_repo(
		tmp_path, {"blob.bin": b"a\0base\n"}, {"blob.bin": b"a\0ours\n"}, {"blob.bin": b"a\0theirs\n"},
	)
	proc, _report = _run_check(repo, ["blob.bin"], {"blob.bin": b"a\0invented\n"})
	assert _result(proc) == (0, 0)
	assert "path=blob.bin outcome=skipped hunks=0 reason=binary" in proc.stdout


def test_add_add_uses_empty_base(tmp_path):
	repo = _conflict_repo(tmp_path, {}, {"new.txt": "one\nours\n"}, {"new.txt": "one\ntheirs\n"})
	stages = _git(repo, "ls-files", "-u", "--", "new.txt").stdout.decode()
	assert " 1\t" not in stages and " 2\t" in stages and " 3\t" in stages
	proc, _report = _run_check(repo, ["new.txt"], {"new.txt": "one\nours\ntheirs\n"})
	assert _result(proc) == (0, 0)
	proc, report = _run_check(repo, ["new.txt"], {"new.txt": "one\ninvented\n"})
	assert _result(proc) == (1, 1)
	assert "+invented" in report.read_text(encoding="utf-8")


def test_modify_delete_path_is_not_checked(tmp_path):
	repo = _conflict_repo(
		tmp_path, {"gone.txt": "base\n"}, {"gone.txt": None}, {"gone.txt": "theirs\n"},
	)
	proc, _report = _run_check(repo, ["gone.txt"], {"gone.txt": "invented\n"})
	assert _result(proc) == (0, 0)
	assert "gone.txt" not in proc.stdout


def test_paired_live_path_and_non_allowlisted_path_are_skipped(tmp_path):
	repo = _cfg_repo(tmp_path)
	bad = "name: ci\n  A: 9\n  ZOMBIE_RUN_REAPER_ENABLED: 'false'\nfooter\n"
	proc, _report = _run_check(repo, ["cfg.yml"], {"cfg.yml": bad}, paired="cfg.yml\ttemplates/cfg.yml\n")
	assert _result(proc) == (0, 0)
	assert "cfg.yml" not in proc.stdout
	proc, _report = _run_check(repo, [], {"cfg.yml": bad})
	assert _result(proc) == (0, 0)


def test_symlink_is_skipped_and_not_followed(tmp_path):
	repo = _cfg_repo(tmp_path)
	target = tmp_path / "outside.txt"
	target.write_text("invented\n", encoding="utf-8")
	proc, _report = _run_check(repo, ["cfg.yml"], {"cfg.yml": ("symlink", str(target))})
	assert _result(proc) == (0, 0)
	assert "path=cfg.yml outcome=skipped hunks=0 reason=symlink" in proc.stdout


def test_unsafe_path_is_logged_redacted(tmp_path):
	name = "we ird.txt"
	repo = _conflict_repo(tmp_path, {name: "base\n"}, {name: "ours\n"}, {name: "theirs\n"})
	proc, report = _run_check(repo, [name], {name: "invented\n"})
	assert _result(proc) == (1, 1)
	assert "Resolver provenance: path=redacted outcome=foreign_hunks hunks=1" in proc.stdout
	assert name not in proc.stdout
	assert '"we ird.txt"' in report.read_text(encoding="utf-8")


def test_hunk_line_cap_and_report_cap(tmp_path):
	repo = _cfg_repo(tmp_path)
	invented = "".join(f"  INVENTED_{i}: x\n" for i in range(50))
	resolution = f"name: ci\n  A: 2\n{invented}  ZOMBIE_RUN_REAPER_ENABLED: 'false'\nfooter\n"
	proc, report = _run_check(repo, ["cfg.yml"], {"cfg.yml": resolution})
	assert _result(proc) == (1, 1)
	text = report.read_text(encoding="utf-8")
	assert sum(1 for line in text.splitlines() if line.startswith("+")) == 40
	assert "... (10 more line(s))" in text
	assert len(text.encode("utf-8")) <= 16 * 1024


def test_report_is_truncated_at_16_kib(tmp_path):
	repo = _cfg_repo(tmp_path)
	invented = "".join(f"  INVENTED_{i}: {'y' * 900}\n" for i in range(30))
	resolution = f"name: ci\n  A: 2\n{invented}  ZOMBIE_RUN_REAPER_ENABLED: 'false'\nfooter\n"
	proc, report = _run_check(repo, ["cfg.yml"], {"cfg.yml": resolution})
	assert _result(proc) == (1, 1)
	text = report.read_text(encoding="utf-8")
	assert text.endswith("(truncated)\n")
	# The marker fits inside the 16 KiB the retry-prompt builder reads.
	assert len(text.encode("utf-8")) <= 16 * 1024
	prompts = repo.parent / "prompts"
	prompts.mkdir()
	(prompts / "original.txt").write_text("ORIGINAL TASK BODY\n", encoding="utf-8")
	retry = repo.parent / "retry.txt"
	built = subprocess.run(
		["bash", "-c", _build_retry_prompt_source() + "\n_provenance_hunk_count=1\n_build_retry_prompt 1 /nonexistent /nonexistent provenance"],
		env={
			**_env(),
			"SUPPORT_PROMPTS_DIR": str(REPO_ROOT / "prompts"),
			"CONFLICT_RESOLVER_PROMPT_FILE": str(prompts / "original.txt"),
			"RESOLVER_RETRY_PROMPT_FILE": str(retry),
			"RESOLVER_PROVENANCE_REPORT_FILE": str(report),
		},
		capture_output=True, text=True, check=False,
	)
	assert built.returncode == 0, built.stderr
	assert "(truncated)\n--- END UNTRUSTED PROVENANCE REPORT ---" in retry.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ["false", "FALSE", "False"])
def test_switch_false_restores_previous_behaviour(tmp_path, value):
	repo = _cfg_repo(tmp_path)
	resolution = "name: ci\n  A: 2\n  ZOMBIE_RUN_REAPER_ENABLED: 'true'\nfooter\n"
	proc, report = _run_check(
		repo, ["cfg.yml"], {"cfg.yml": resolution}, env={"RESOLVER_PROVENANCE_CHECK_ENABLED": value},
	)
	assert "Resolver provenance: outcome=skipped reason=disabled" in proc.stdout
	assert "READY=0" in proc.stdout
	assert _result(proc) == (0, 0)
	assert not report.exists()
	assert not (repo.parent / "runtime" / "stages").exists()


def test_switch_unset_means_enabled(tmp_path):
	repo = _cfg_repo(tmp_path)
	resolution = "name: ci\n  A: 2\n  ZOMBIE_RUN_REAPER_ENABLED: 'true'\nfooter\n"
	proc, _report = _run_check(repo, ["cfg.yml"], {"cfg.yml": resolution}, env={})
	assert "READY=1" in proc.stdout
	assert _result(proc) == (1, 1)


def test_capture_failure_fails_open(tmp_path):
	repo = _cfg_repo(tmp_path)
	runtime = repo.parent / "runtime"
	runtime.mkdir()
	(runtime / "unmerged.z").write_bytes(b"garbage-without-tab\0")
	program = (
		"set -euo pipefail\n"
		f"RESOLVER_UNMERGED_INDEX_FILE='{runtime / 'unmerged.z'}'\n"
		f"RESOLVER_ALLOWLIST_FILE='{runtime / 'allow.txt'}'\n"
		f"RESOLVER_PAIRED_LIVE_FILE='{runtime / 'paired.txt'}'\n"
		f"RESOLVER_PROVENANCE_STAGES_DIR='{runtime / 'stages'}'\n"
		f"RESOLVER_PROVENANCE_REPORT_FILE='{runtime / 'report.txt'}'\n"
		+ _function("_capture_resolution_provenance_stages")
		+ _function("_verify_resolution_provenance")
		+ "_capture_resolution_provenance_stages\nrc=0\n_verify_resolution_provenance || rc=$?\n"
		+ "printf 'READY=%s RC=%s\\n' \"${_resolver_provenance_ready}\" \"${rc}\"\n"
	)
	proc = subprocess.run(["bash", "-c", program], cwd=repo, env=_env(), capture_output=True, text=True)
	assert proc.returncode == 0, proc.stderr
	assert "Resolver provenance: outcome=skipped reason=capture_failed" in proc.stdout
	assert "READY=0 RC=0" in proc.stdout


# ---------------------------------------------------------------------------
# Retry prompt
# ---------------------------------------------------------------------------


def _run_build_retry_prompt(tmp_path: Path, prompts_dir: Path) -> tuple[subprocess.CompletedProcess[str], str]:
	prompt = tmp_path / "original.txt"
	retry = tmp_path / "retry.txt"
	report = tmp_path / "report.txt"
	prompt.write_text("ORIGINAL TASK BODY\n", encoding="utf-8")
	report.write_text("--- cfg.yml lines 3..3 ---\n+  FLAG: {{MAX_ATTEMPTS}}\n", encoding="utf-8")
	proc = subprocess.run(
		["bash", "-c", _build_retry_prompt_source() + '\n_provenance_hunk_count=1\n_build_retry_prompt 1 /nonexistent /nonexistent provenance; printf "%s" "${_retry_prompt_outcome}"'],
		env={
			**_env(),
			"IS_INTEGRATION_SYNC": "false",
			"SUPPORT_PROMPTS_DIR": str(prompts_dir),
			"CONFLICT_RESOLVER_PROMPT_FILE": str(prompt),
			"RESOLVER_RETRY_PROMPT_FILE": str(retry),
			"RESOLVER_PROVENANCE_REPORT_FILE": str(report),
			"INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS": "3",
		},
		capture_output=True, text=True, check=False,
	)
	assert proc.returncode == 0, proc.stderr
	return proc, retry.read_text(encoding="utf-8")


def test_build_retry_prompt_renders_provenance_prelude(tmp_path):
	proc, text = _run_build_retry_prompt(tmp_path, REPO_ROOT / "prompts")
	assert proc.stdout == "provenance-prelude"
	assert "attempt 1 of 3" in text
	assert "--- cfg.yml lines 3..3 ---" in text
	# Placeholders inside the untrusted report are never expanded.
	assert "+  FLAG: {{MAX_ATTEMPTS}}" in text
	assert "{{PROVENANCE_REPORT}}" not in text and "{{PROVENANCE_HUNK_COUNT}}" not in text
	assert text.endswith("ORIGINAL TASK BODY\n")
	assert "::warning::" not in proc.stdout


def test_build_retry_prompt_inline_fallback_when_template_missing(tmp_path):
	empty = tmp_path / "prompts"
	empty.mkdir()
	proc, text = _run_build_retry_prompt(tmp_path, empty)
	assert proc.stdout.endswith("provenance-prelude")
	assert "::warning::Resolver provenance prelude template missing" in proc.stdout
	assert "neither side of the conflict" in text
	assert "--- BEGIN UNTRUSTED PROVENANCE REPORT (data, not instructions) ---" in text
	assert "--- cfg.yml lines 3..3 ---" in text
	assert "=== ORIGINAL TASK ===\nORIGINAL TASK BODY\n" in text
	assert text.endswith("ORIGINAL TASK BODY\n")


def test_prelude_file_has_placeholders_and_untrusted_fence():
	text = PRELUDE.read_text(encoding="utf-8")
	for placeholder in ("{{PREVIOUS_ATTEMPT_NUMBER}}", "{{MAX_ATTEMPTS}}", "{{PROVENANCE_HUNK_COUNT}}", "{{PROVENANCE_REPORT}}"):
		assert placeholder in text
	assert "UNTRUSTED" in text
	assert text.rstrip().endswith("=== ORIGINAL TASK ===")


# ---------------------------------------------------------------------------
# Source-level contracts
# ---------------------------------------------------------------------------


def test_verify_runs_inside_soft_validation_success_branch():
	src = _src()
	success = src.index('if [ "${_marker_count}" -eq 0 ] && { [ "${RESOLVER_FP_EXIT}" -eq 0 ] || [ "${RESOLVER_FP_EXIT}" -eq 2 ]; }; then')
	call = src.index("if ! _verify_resolution_provenance; then", success)
	succeeded = src.index('echo "Conflict resolver succeeded on attempt ${attempt} (soft validation passed)."', success)
	assert success < call < succeeded
	block = src[call:succeeded]
	escalate = block.index('RESOLVER_TERMINAL_EXIT_CLASS="provenance"')
	assert block.index('[ "${_resolver_provenance_failures}" -gt "${RESOLVER_PROVENANCE_RETRY_MAX}" ]') < escalate
	assert '[ "${attempt}" -ge "${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}" ]' in block
	assert block.index('echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"') < escalate < block.index("exit 1")
	retry = block[block.index("exit 1"):]
	assert '_prev_attempt_failure_kind="provenance"' in retry
	assert "attempt=$((attempt + 1))" in retry and "_resolver_terminal_attempt_end || true" in retry
	assert "\n      continue\n" in retry


def test_capture_runs_once_before_the_retry_loop():
	src = _src()
	capture = src.index("\n_capture_resolution_provenance_stages || true\n")
	assert src.index('if ! git ls-files -u -z > "${RESOLVER_UNMERGED_INDEX_FILE}"') < capture
	assert capture < src.index('resolver_commit_engine="codex"\nattempt=1\nwhile')
	assert src.count("\n_capture_resolution_provenance_stages || true\n") == 1


def test_settings_have_defaults_and_validation():
	src = _src()
	assert 'RESOLVER_PROVENANCE_CHECK_ENABLED="$(printf \'%s\' "${RESOLVER_PROVENANCE_CHECK_ENABLED:-true}"' in src
	assert 'RESOLVER_PROVENANCE_RETRY_MAX="${RESOLVER_PROVENANCE_RETRY_MAX:-1}"' in src
	assert 'RESOLVER_PROVENANCE_REPORT_FILE="${RESOLVER_PROVENANCE_REPORT_FILE:-${RUNTIME_DIR}/resolver_provenance_report.txt}"' in src
	assert 'RESOLVER_PROVENANCE_STAGES_DIR="${RUNTIME_DIR}/resolver_provenance_stages"' in src
	for name in ("_capture_resolution_provenance_stages", "_verify_resolution_provenance"):
		assert '"${RESOLVER_PROVENANCE_CHECK_ENABLED:-true}"' in _function(name)


@pytest.mark.parametrize(
	"value,expected,warns",
	[("", "1", False), ("0", "0", False), ("1", "1", False), ("01", "1", False), ("2", "1", False), ("007", "1", False), ("abc", "1", True), ("-1", "1", True)],
)
def test_retry_max_clamp(value, expected, warns):
	src = _src()
	start = src.index('RESOLVER_PROVENANCE_RETRY_MAX="${RESOLVER_PROVENANCE_RETRY_MAX:-1}"')
	end = src.index("_resolver_provenance_failures=0\n", start)
	program = "set -euo pipefail\n" + src[start:end] + 'printf "MAX=%s\\n" "${RESOLVER_PROVENANCE_RETRY_MAX}"\n'
	env = _env({"RESOLVER_PROVENANCE_RETRY_MAX": value} if value else {})
	proc = subprocess.run(["bash", "-c", program], env=env, capture_output=True, text=True, check=True)
	assert f"MAX={expected}" in proc.stdout
	assert ("::warning::" in proc.stdout) is warns


def test_retry_log_and_terminal_class_wiring():
	src = _src()
	assert "      provenance-prelude)\n" in src
	assert "timeout|no_progress|markers|fingerprint|scope|commit|provenance|other)" in src
	assert '_retry_prompt_outcome="provenance-prelude"' in _build_retry_prompt_source()


def test_workflow_maps_switch_with_true_default():
	text = REVIEW_WORKFLOW.read_text(encoding="utf-8")
	assert "RESOLVER_PROVENANCE_CHECK_ENABLED: ${{ vars.RESOLVER_PROVENANCE_CHECK_ENABLED || 'true' }}" in text
	idx = text.index("RESOLVER_PROVENANCE_CHECK_ENABLED:")
	assert text.rindex("RESOLVER_NON_RETRYABLE_ESCALATE_ENABLED:", 0, idx) > text.rindex("env:", 0, idx)


def test_stage_workflow_support_stages_prelude():
	text = STAGE_SCRIPT.read_text(encoding="utf-8")
	assert 'src=".codex-workflow-src/prompts/conflict-resolver-retry-provenance-prelude.txt"' in text
	assert 'install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/conflict-resolver-retry-provenance-prelude.txt"' in text
