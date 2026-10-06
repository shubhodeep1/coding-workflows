"""Orchestrator judge engine selection and credential-free sandbox contract."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"

# A stand-in for scripts/ai_engine.sh: the engine comes from FAKE_ENGINE, and
# claude_run records its role, files, labels and hints, then plays
# FAKE_CLAUDE_MODE (success writes "claude verdict", unavailable returns 75,
# crash returns 1).
FAKE_AI_ENGINE = r"""
ai_engine_for_role() {
  printf '%s|%s\n' "$1" "${AI_ENGINE_LABELS-unset}" >> "${CALLS}.resolve"
  echo "AI_ENGINE_SELECTED role=$1 engine=${FAKE_ENGINE}" >&2
  printf '%s\n' "${FAKE_ENGINE}"
}
ai_engine_fallback() {
  printf 'AI_ENGINE_FALLBACK role=%s reason=%s\n' "$1" "$2" >&2
}
claude_run() {
  printf '%s|%s|%s|%s|%s|%s|%s\n' "$1" "$(basename "$2")" "$(basename "$3")" "$4" "${AI_ENGINE_LABELS-unset}" "${AI_ENGINE_MODEL_HINT-}" "${AI_ENGINE_EFFORT_HINT-}" >> "${CALLS}.claude"
  case "${FAKE_CLAUDE_MODE}" in
    success) printf 'claude verdict\n' > "$3"; return 0 ;;
    unavailable) echo "AI_ENGINE_FALLBACK role=$1 reason=no_credential" >&2; return 75 ;;
    *) return 1 ;;
  esac
}
"""


def _helper_source() -> str:
	text = POLLER.read_text(encoding="utf-8")
	return "\n".join(
		re.search(rf"^{name}\(\)\n\{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL).group(0)
		for name in ("poller_claude_judge", "_poller_rb_judge_sandbox_attempt", "poller_judge_isolated", "poller_rb_judge_isolated")
	)


def _run_helper(
	tmp_path: Path,
	*,
	engine: str,
	claude_mode: str = "success",
	with_engine: bool = True,
	extra: str = "",
	tracking_labels: str | None = '["bug","ai:engine-claude"]',
	role: str = "WAVE_JUDGE",
	combined_mode: str = "false",
	log_file: str = "judge_log.txt",
) -> tuple[subprocess.CompletedProcess[str], Path]:
	scripts = tmp_path / "scripts"
	scripts.mkdir(parents=True, exist_ok=True)
	if with_engine:
		(scripts / "ai_engine.sh").write_text(FAKE_AI_ENGINE, encoding="utf-8")
	if role == "RB_JUDGE" and combined_mode == "true":
		subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
		(tmp_path / ".gitignore").write_text("rb-claude-untracked-*\nreview_sandbox_transfer_failed\nout.txt\ncalls.*\njudge_log.txt\nrb_judge_opencode.json\nrb_judge_isolation_reason\n", encoding="utf-8")
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("judge this\n", encoding="utf-8")
	calls = tmp_path / "calls"
	labels_line = f"TRACKING_LABELS='{tracking_labels}'\n" if tracking_labels is not None else "unset TRACKING_LABELS\n"
	script = (
		"set -euo pipefail\n"
		f"_POLLER_AI_ENGINE_SH={scripts / 'ai_engine.sh'}\n"
		+ _helper_source()
		+ labels_line
		+ f"MODEL_EDITOR=openai/gpt-6-sol\nMODEL_REASONING_EFFORT_JUDGE=xhigh\nRB_COMBINED_MODE={combined_mode}\n"
		+ "rc=0\n"
		+ f'poller_claude_judge {role} prompt.txt out.txt {log_file} ' + extra + ' || rc=$?\n'
		+ 'echo "rc=${rc}"\n'
	)
	env = dict(os.environ, CALLS=str(calls), FAKE_ENGINE=engine, FAKE_CLAUDE_MODE=claude_mode,
		GITHUB_WORKSPACE=str(tmp_path), RUNTIME_DIR=str(tmp_path), FAKE_SANDBOX_ROOT=str(tmp_path / "sandbox-root"))
	for inherited in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "WORKSPACE_PATH"):
		env.pop(inherited, None)
	proc = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
	return proc, calls


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8") if path.exists() else ""


def test_judge_on_claude_runs_in_sandbox_with_tracking_labels(tmp_path: Path) -> None:
	_stage_rb_support(tmp_path)
	proc, calls = _run_helper(tmp_path, engine="claude")
	assert "rc=0" in proc.stdout, proc.stderr
	assert _read(tmp_path / "out.txt") == "claude verdict\n"
	assert _read(Path(f"{calls}.sandbox")).splitlines() == ["prepare", "claude|WAVE_JUDGE|read|/dev/null", "cleanup"]
	assert _read(Path(f"{calls}.resolve")).splitlines() == ['WAVE_JUDGE|["bug","ai:engine-claude"]']
	assert "AI_ENGINE_SELECTED role=WAVE_JUDGE engine=claude" in _read(tmp_path / "judge_log.txt")


def test_judge_on_codex_uses_sandbox(tmp_path: Path) -> None:
	_stage_rb_support(tmp_path)
	proc, calls = _run_helper(tmp_path, engine="codex")
	assert "rc=0" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.sandbox")).splitlines() == ["prepare", f"codex|WAVE_JUDGE|read|{tmp_path}/poller_judge_opencode.json", "cleanup"]
	assert _read(Path(f"{calls}.claude")) == ""

def test_dev_null_suppresses_judge_selection_stderr(tmp_path: Path) -> None:
	_stage_rb_support(tmp_path)
	proc, _calls = _run_helper(tmp_path, engine="codex", log_file="/dev/null")
	assert "rc=0" in proc.stdout
	assert proc.stderr == ""

def test_claude_unavailable_retries_in_new_sandbox(tmp_path: Path) -> None:
	_stage_rb_support(tmp_path)
	proc, calls = _run_helper(tmp_path, engine="claude", claude_mode="unavailable")
	assert "rc=0" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.sandbox")).splitlines() == ["prepare", "claude|WAVE_JUDGE|read|/dev/null", "cleanup", "prepare", f"codex|WAVE_JUDGE|read|{tmp_path}/poller_judge_opencode.json", "cleanup"]


def test_claude_crash_is_not_a_fallback(tmp_path: Path) -> None:
	_stage_rb_support(tmp_path)
	proc, _calls = _run_helper(tmp_path, engine="claude", claude_mode="crash")
	assert "rc=1" in proc.stdout, proc.stderr


def test_missing_ai_engine_uses_isolated_opencode(tmp_path: Path) -> None:
	_stage_rb_support(tmp_path)
	proc, calls = _run_helper(tmp_path, engine="claude", with_engine=False)
	assert "rc=0" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.resolve")) == ""


def test_no_tracking_labels_means_an_explicit_empty_list(tmp_path: Path) -> None:
	_stage_rb_support(tmp_path)
	_proc, calls = _run_helper(tmp_path, engine="codex", tracking_labels=None)
	assert _read(Path(f"{calls}.resolve")).splitlines() == ["WAVE_JUDGE|[]"]


def test_model_hint_argument_overrides_model_editor(tmp_path: Path) -> None:
	_stage_rb_support(tmp_path)
	_proc, calls = _run_helper(tmp_path, engine="claude", extra="claude-sonnet-5-5")
	assert _read(Path(f"{calls}.config")) == ""
	assert _read(Path(f"{calls}.sandbox")).splitlines()[1].startswith("claude|WAVE_JUDGE|read|")


@pytest.mark.parametrize("role", ("WAVE_JUDGE", "STALL_JUDGE", "INTEGRATION_JUDGE", "SECURITY_JUDGE"))
def test_all_poller_judges_are_read_only_and_never_use_host_fallback(tmp_path: Path, role: str) -> None:
	support = _stage_rb_support(tmp_path)
	for engine, mode, expected in (("claude", "unavailable", 0), ("codex", "prepare_failed", 77)):
		proc, calls = _run_helper(tmp_path, engine=engine, claude_mode=mode, role=role)
		assert f"rc={expected}" in proc.stdout, proc.stderr
		assert all(f"|{role}|read|" in line for line in _read(Path(f"{calls}.sandbox")).splitlines() if "|" in line)
		if expected == 77:
			assert _read(tmp_path / "judge_isolation_reason").strip() == "sandbox_prepare_failed"
	(support / "review_untrusted_sandbox.sh").write_text(FAKE_RB_SANDBOX.replace(
		'# Poller judges (WAVE/STALL/INTEGRATION/SECURITY) are read-only sandbox roles; never transfer.',
		'# old version',
	), encoding="utf-8")
	proc, calls = _run_helper(tmp_path, engine="claude", role=role)
	assert "rc=77" in proc.stdout, proc.stderr
	assert _read(tmp_path / "judge_isolation_reason").strip() == "sandbox_helper_outdated"
	assert _read(Path(f"{calls}.claude")) == ""


FAKE_RB_SANDBOX = r"""#!/usr/bin/env bash
# Poller judges (WAVE/STALL/INTEGRATION/SECURITY) are read-only sandbox roles; never transfer.
# if [ "${rc}" -eq 0 ] && [ "${claude_access}" = write ]; then
case "$1" in
  prepare-ephemeral)
    [ "$FAKE_CLAUDE_MODE" != prepare_failed ] || exit 1
    printf 'prepare\n' >> "${CALLS}.sandbox"
    printf '%s\n' "$FAKE_SANDBOX_ROOT" ;;
  run)
    rc=0; claude_access="${9:-write}"
    # Arg 9 (read) applies to both engines; read-only roles never transfer edits back.
    if [ "${rc}" -eq 0 ] && [ "${claude_access}" = write ]; then :; fi
    case "${8:-}" in WAVE_JUDGE|STALL_JUDGE|INTEGRATION_JUDGE|SECURITY_JUDGE) [ "${claude_access}" = read ] || exit 2 ;; esac
    printf '%s|%s|%s|%s\n' "$7" "$8" "$9" "$6" >> "${CALLS}.sandbox"
    [ "$FAKE_CLAUDE_MODE" != outdated ] || exit 2
    [ "$FAKE_CLAUDE_MODE" != codex_unavailable ] || exit 75
    if [ "$7" = codex ]; then printf 'opencode verdict\n' > "$3"; exit 0; fi
    case "$FAKE_CLAUDE_MODE" in
      success) printf 'claude verdict\n' > "$3" ;;
      unavailable) exit 75 ;;
      transfer_failed) printf 'unusable verdict\n' > "$3"; : > "${RUNTIME_DIR}/review_sandbox_transfer_failed"; exit 1 ;;
      transfer_failed_dirty) printf 'unusable verdict\n' > "$3"; mkdir -p leaked; printf 'partial\n' > 'leaked/partial.py'; : > "${RUNTIME_DIR}/review_sandbox_transfer_failed"; exit 1 ;;
      transfer_failed_modified) printf 'unusable verdict\n' > "$3"; printf 'partial\n' > 'preexisting.py'; : > "${RUNTIME_DIR}/review_sandbox_transfer_failed"; exit 1 ;;
      transfer_failed_chmod) printf 'unusable verdict\n' > "$3"; chmod 755 preexisting.py; : > "${RUNTIME_DIR}/review_sandbox_transfer_failed"; exit 1 ;;
      cleanup_failed) printf 'claude verdict\n' > "$3"; printf 'partial\n' > 'new-file.py' ;;
      *) printf 'unusable verdict\n' > "$3"; exit 1 ;;
    esac ;;
  cleanup) printf 'cleanup\n' >> "${CALLS}.sandbox"; [ "$FAKE_CLAUDE_MODE" != cleanup_failed ] ;;
esac
"""

FAKE_RB_CONFIG = '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "${CALLS}.config"\nprintf "{}\\n" > "${RUNTIME_DIR}/rb_judge_opencode.json"\n'


def _stage_rb_support(tmp_path: Path) -> Path:
	support = tmp_path / ".codex-workflow-src" / "scripts"
	support.mkdir(parents=True, exist_ok=True)
	(support / "review_untrusted_sandbox.sh").write_text(FAKE_RB_SANDBOX, encoding="utf-8")
	(support / "write_opencode_config.sh").write_text(FAKE_RB_CONFIG, encoding="utf-8")
	return support


def test_poller_rb_judge_runs_only_in_isolated_sandbox(tmp_path: Path) -> None:
	_stage_rb_support(tmp_path)
	for combined, expected in (("false", "read"), ("true", "write")):
		proc, calls = _run_helper(tmp_path, engine="codex", role="RB_JUDGE", combined_mode=combined)
		assert "rc=0" in proc.stdout, proc.stderr
		assert _read(tmp_path / "out.txt") == "opencode verdict\n"
		assert _read(Path(f"{calls}.claude")) == ""
		assert _read(Path(f"{calls}.sandbox")).splitlines()[-3:] == [
			"prepare", f"codex|RB_JUDGE|{expected}|{tmp_path}/rb_judge_opencode.json", "cleanup",
		]
		assert "--role writer --model openai/gpt-6-sol" in _read(Path(f"{calls}.config"))


def test_poller_rb_judge_never_falls_back_to_host(tmp_path: Path) -> None:
	support = _stage_rb_support(tmp_path)
	for mode, expected in (("prepare_failed", 77), ("unavailable", 0), ("crash", 1), ("outdated", 77), ("transfer_failed", 76)):
		if mode == "prepare_failed":
			(tmp_path / "out.txt").write_text("stale verdict\n", encoding="utf-8")
		proc, calls = _run_helper(tmp_path, engine="claude", claude_mode=mode, role="RB_JUDGE", combined_mode="true")
		assert f"rc={expected}" in proc.stdout, proc.stderr
		if mode == "prepare_failed":
			assert _read(tmp_path / "rb_judge_isolation_reason").strip() == "sandbox_prepare_failed"
			assert "AI_ENGINE_FALLBACK role=RB_JUDGE reason=sandbox_prepare_failed" not in proc.stderr
			assert "reason=sandbox_prepare_failed" in proc.stderr
			assert "reason=sandbox_prepare_failed" in _read(tmp_path / "judge_log.txt")
			assert _read(tmp_path / "out.txt") == ""
		elif mode == "unavailable":
			assert _read(Path(f"{calls}.sandbox")).splitlines() == [
				"prepare", "claude|RB_JUDGE|write|/dev/null", "cleanup", "prepare",
				f"codex|RB_JUDGE|write|{tmp_path}/rb_judge_opencode.json", "cleanup",
			]
		else:
			assert _read(tmp_path / "out.txt") == ""
			assert _read(Path(f"{calls}.sandbox")).splitlines()[-1] == "cleanup"
		assert _read(Path(f"{calls}.claude")) == ""
	assert not (tmp_path / "review_sandbox_transfer_failed").exists()
	(support / "review_untrusted_sandbox.sh").unlink()
	proc, calls = _run_helper(tmp_path, engine="claude", role="RB_JUDGE")
	assert "rc=77" in proc.stdout, proc.stderr
	assert _read(tmp_path / "rb_judge_isolation_reason").strip() == "support_missing"
	assert _read(Path(f"{calls}.claude")) == ""
	_stage_rb_support(tmp_path)
	proc, _calls = _run_helper(tmp_path, engine="codex", claude_mode="codex_unavailable", role="RB_JUDGE")
	assert "rc=77" in proc.stdout, proc.stderr
	assert _read(tmp_path / "rb_judge_isolation_reason").strip() == "sandbox_helper_outdated"
	proc, calls = _run_helper(tmp_path, engine="codex", role="RB_JUDGE", with_engine=False)
	assert "rc=0" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.sandbox")).splitlines()[-2].startswith("codex|RB_JUDGE|read|")
	(support / "write_opencode_config.sh").unlink()
	proc, _calls = _run_helper(tmp_path, engine="codex", role="RB_JUDGE")
	assert "rc=77" in proc.stdout, proc.stderr
	assert _read(tmp_path / "rb_judge_isolation_reason").strip() == "opencode_config_failed"


def test_older_sandbox_with_only_claude_transfer_guard_is_rejected(tmp_path: Path) -> None:
	support = _stage_rb_support(tmp_path)
	sandbox = support / "review_untrusted_sandbox.sh"
	sandbox.write_text(FAKE_RB_SANDBOX.replace(
		'    if [ "${rc}" -eq 0 ] && [ "${claude_access}" = write ]; then :; fi',
		'    if [ "${rc}" -eq 0 ]; then :; fi',
	), encoding="utf-8")
	proc, calls = _run_helper(tmp_path, engine="codex", role="RB_JUDGE")
	assert "rc=77" in proc.stdout, proc.stderr
	assert _read(tmp_path / "rb_judge_isolation_reason").strip() == "sandbox_helper_outdated"
	assert _read(Path(f"{calls}.sandbox")) == ""


def test_rejected_poller_transfer_removes_only_new_untracked_files(tmp_path: Path) -> None:
	assert 'LC_ALL=C comm -z -13 <(LC_ALL=C sort -z "${rb_untracked_before_file}") <(LC_ALL=C sort -z "${rb_untracked_after_file}")' in _helper_source()
	_stage_rb_support(tmp_path)
	preexisting = tmp_path / "preexisting.py"
	preexisting.write_text("keep\n", encoding="utf-8")
	for mode in ("transfer_failed_dirty", "cleanup_failed"):
		proc, calls = _run_helper(tmp_path, engine="claude", claude_mode=mode, role="RB_JUDGE", combined_mode="true")
		if mode == "cleanup_failed":
			assert proc.returncode == 1 and "stopping the poller" in proc.stderr
			assert "rc=" not in proc.stdout
		else:
			assert "rc=76" in proc.stdout, proc.stderr
			assert _read(tmp_path / "out.txt") == ""
		assert not (tmp_path / "leaked" / "partial.py").exists()
		assert not (tmp_path / "new-file.py").exists()
		assert preexisting.read_text(encoding="utf-8") == "keep\n"
		assert _read(Path(f"{calls}.sandbox")).splitlines()[-1] == "cleanup"
		assert not (tmp_path / "review_sandbox_transfer_failed").exists()


@pytest.mark.parametrize("mode", ("transfer_failed_modified", "transfer_failed_chmod"))
def test_rejected_poller_transfer_stops_if_existing_untracked_file_was_modified(tmp_path: Path, mode: str) -> None:
	_stage_rb_support(tmp_path)
	preexisting = tmp_path / "preexisting.py"
	preexisting.write_text("keep\n", encoding="utf-8")
	proc, calls = _run_helper(tmp_path, engine="claude", claude_mode=mode, role="RB_JUDGE", combined_mode="true")
	assert proc.returncode == 1
	assert "stopping this poll tick" in proc.stderr
	assert "rc=" not in proc.stdout
	if mode == "transfer_failed_modified":
		assert preexisting.read_text(encoding="utf-8") == "partial\n"
	else:
		assert preexisting.stat().st_mode & 0o111
	assert _read(Path(f"{calls}.sandbox")).splitlines()[-1] == "cleanup"


def test_poller_rb_prepare_preserves_engine_selection_log(tmp_path: Path) -> None:
	support = _stage_rb_support(tmp_path)
	(support / "review_untrusted_sandbox.sh").write_text(
		FAKE_RB_SANDBOX.replace('  prepare-ephemeral)\n', '  prepare-ephemeral)\n    printf "sandbox preparation started\\n" >&2\n'),
		encoding="utf-8",
	)
	proc, _calls = _run_helper(tmp_path, engine="claude", role="RB_JUDGE", log_file="judge_log.txt")
	assert "rc=0" in proc.stdout, proc.stderr
	log = _read(tmp_path / "judge_log.txt")
	assert "AI_ENGINE_SELECTED role=RB_JUDGE engine=claude" in log
	assert "sandbox preparation started" in log


@pytest.mark.parametrize("comment_reply, expected_comment_id", [('{"id":123}', 123), ('{}', None)])
def test_isolation_deferral_escalates_once_per_head(tmp_path: Path, comment_reply: str, expected_comment_id: int | None) -> None:
	text = POLLER.read_text(encoding="utf-8")
	block = text.split('        if [ "${RB_JUDGE_ISOLATION_FAILED}" = true ]; then\n', 1)[1].split(
		'        echo "::warning::Review-blocked judge failed', 1,
	)[0]
	state = tmp_path / "state.json"
	state.write_text("{}", encoding="utf-8")
	(tmp_path / "rb_judge_isolation_reason").write_text("sandbox_prepare_failed\n", encoding="utf-8")
	script = (
		"set -euo pipefail\n"
		f'RUNTIME_DIR={tmp_path}\nSTATE_FILE={state}\n'
		'GITHUB_REPOSITORY=owner/repo\nrb_issue=10\nRB_PR=77\n'
		'RB_JUDGE_ISOLATION_MAX_FAILURES=2\nRB_ISOLATION_HEAD="$(printf "%040d" 1)"\n'
		'tg_notify() { :; }\nensure_label_exists() { :; }\n'
		'gh_retry() {\n'
		'  if [ "$1 $2" = "_safe_gh_jq repos/owner/repo/issues/10" ]; then echo "[]"; return; fi\n'
		'  printf "%s\\n" "$*" >> "$RUNTIME_DIR/remote_calls"\n'
		f'  if [ "$1 $2" = "gh api" ]; then printf "%s\\n" \'{comment_reply}\'; fi\n'
		'}\n'
		'RB_ISOLATION_REASON=sandbox_prepare_failed\n'
		'RB_ISOLATION_REASON="$(cat "${RUNTIME_DIR}/rb_judge_isolation_reason")"\n'
		'REVIEW_BLOCKED_STATE_CHANGED=false\n'
		'for tick in 1 2; do\nif [ "${RB_JUDGE_ISOLATION_FAILED:-true}" = true ]; then\n' + block + '\ndone\n'
	)
	env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "WORKSPACE_PATH")}
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	entry = json.loads(state.read_text(encoding="utf-8"))["review_blocked_isolation_state"]["10"]
	assert entry["count"] == 2 and entry["escalated"] is True and entry["owned_label"] is True
	assert entry["comment_id"] == expected_comment_id
	assert _read(tmp_path / "remote_calls").count("gh api ") == 1
	assert _read(tmp_path / "remote_calls").count("gh issue edit ") == 1


@pytest.mark.parametrize("owned_label", [False, True])
def test_new_head_does_not_bypass_uncleared_isolation_latch(tmp_path: Path, owned_label: bool) -> None:
	text = POLLER.read_text(encoding="utf-8")
	block = text.split('      RB_ISOLATION_HEAD="', 1)[1].split('      # Collect full PR context', 1)[0]
	state = tmp_path / "state.json"
	old_head, new_head = "a" * 40, "b" * 40
	state.write_text(json.dumps({"review_blocked_isolation_state": {"10": {
		"head_sha": old_head, "count": 3, "escalated": True,
		"comment_id": 123, "owned_label": owned_label,
	}}}), encoding="utf-8")
	script = (
		"set -euo pipefail\n"
		f'STATE_FILE={state}\nGITHUB_REPOSITORY=owner/repo\nrb_issue=10\nRB_PR=77\n'
		f'_rb_pr_json=\'{json.dumps({"head": {"sha": new_head}})}\'\n'
		'gh_retry() { if [ "$1" = _safe_gh_jq ]; then printf \'%s\\n\' \'{"labels":["ai:needs-human"]}\'; else return 1; fi; }\n'
		'for rb_issue in 10; do\n'
		'RB_ISOLATION_HEAD="' + block + 'printf "judge-ran\\n"\ndone\n'
	)
	env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "WORKSPACE_PATH")}
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "outcome=skip reason=latch_not_cleared" in result.stdout
	assert "judge-ran" not in result.stdout
	assert json.loads(state.read_text(encoding="utf-8"))["review_blocked_isolation_state"]["10"]["head_sha"] == old_head


@pytest.mark.parametrize("comment_seen", [True, False])
def test_lost_escalation_comment_response_does_not_retry_judge(tmp_path: Path, comment_seen: bool) -> None:
	text = POLLER.read_text(encoding="utf-8")
	block = text.split('      RB_ISOLATION_HEAD="', 1)[1].split('      # Collect full PR context', 1)[0]
	state = tmp_path / "state.json"
	head_sha = "a" * 40
	state.write_text(json.dumps({"review_blocked_isolation_state": {"10": {
		"head_sha": head_sha, "count": 3, "escalated": True,
		"comment_id": None, "owned_label": True, "reason": "sandbox_prepare_failed",
	}}}), encoding="utf-8")
	comments = [[{"id": 123, "user": {"id": 42}, "body": f"## Review-blocked judge isolation unavailable\n<!-- ai:rb-judge-isolation-escalated head={head_sha} reason=sandbox_prepare_failed -->"}]] if comment_seen else [[]]
	script = (
		"set -euo pipefail\n"
		f'STATE_FILE={state}\nGITHUB_REPOSITORY=owner/repo\nrb_issue=10\nRB_PR=77\n'
		f'_rb_pr_json=\'{json.dumps({"head": {"sha": head_sha}})}\'\n'
		'gh_retry() {\n'
		'  if [ "$1" = _safe_gh_jq ]; then\n'
		'    if [ "$2" = user ]; then printf "42\\n"; else printf \'%s\\n\' \'{"labels":["ai:needs-human"]}\'; fi\n'
		'  elif [ "$3" = --paginate ]; then printf \'%s\\n\' ' + "'" + json.dumps(comments) + "'" + ';\n'
		'  else printf "post\\n" >> remote_calls; printf \'%s\\n\' \'{"id":123}\'; fi\n'
		'}\n'
		'for rb_issue in 10; do\n'
		'RB_ISOLATION_HEAD="' + block + 'printf "judge-ran\\n"\ndone\n'
	)
	env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "WORKSPACE_PATH")}
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "outcome=skip reason=escalated" in result.stdout
	assert "judge-ran" not in result.stdout
	assert json.loads(state.read_text(encoding="utf-8"))["review_blocked_isolation_state"]["10"]["comment_id"] == 123
	assert _read(tmp_path / "remote_calls") == ("" if comment_seen else "post\n")


@pytest.mark.parametrize("comment_id, comment_actor, later_latch, should_resume", [
	(None, 42, False, True),
	(123, 42, False, True),
	(None, 99, False, False),
	(None, 42, True, False),
	(None, None, False, False),
])
def test_new_head_recovers_only_its_own_isolation_latch(
	tmp_path: Path, comment_id: int | None, comment_actor: int | None, later_latch: bool, should_resume: bool,
) -> None:
	text = POLLER.read_text(encoding="utf-8")
	block = text.split('      RB_ISOLATION_HEAD="', 1)[1].split('      # Collect full PR context', 1)[0]
	state = tmp_path / "state.json"
	old_head, new_head = "a" * 40, "b" * 40
	state.write_text(json.dumps({"review_blocked_isolation_state": {"10": {
		"head_sha": old_head, "count": 3, "escalated": True,
		"comment_id": comment_id, "owned_label": True,
	}}}), encoding="utf-8")
	comments = [[{"id": 12, "body": None}, *([{
		"id": 123, "user": {"id": comment_actor}, "created_at": "2026-10-05T10:01:00Z",
		"body": f"## Review-blocked judge isolation unavailable\n<!-- ai:rb-judge-isolation-escalated head={old_head} reason=sandbox_prepare_failed -->",
	}] if comment_actor is not None else [])]]
	events = [[{"event": "labeled", "label": {"name": "ai:needs-human"},
		"actor": {"id": 42}, "created_at": "2026-10-05T10:00:00Z"}]]
	if later_latch:
		events[0].append({"event": "labeled", "label": {"name": "ai:needs-human"},
			"actor": {"id": 99}, "created_at": "2026-10-05T10:02:00Z"})
	script = (
		"set -euo pipefail\n"
		f'STATE_FILE={state}\nGITHUB_REPOSITORY=owner/repo\nrb_issue=10\nRB_PR=77\n'
		f'_rb_pr_json=\'{json.dumps({"head": {"sha": new_head}})}\'\n'
		'gh_retry() {\n'
		'  if [ "$1" = _safe_gh_jq ]; then\n'
		'    if [ "$2" = user ]; then printf "42\\n"; else printf \'%s\\n\' \'{"labels":["ai:needs-human"]}\'; fi\n'
		'  elif [ "${3:-}" = --paginate ]; then\n'
		f'    case "$5" in */events?*) printf \'%s\\n\' \'{json.dumps(events)}\' ;; *) printf \'%s\\n\' \'{json.dumps(comments)}\' ;; esac\n'
		'  elif [ "$1 $2" = "gh issue" ]; then printf "removed\\n" >> remote_calls; fi\n'
		'}\n'
		'for rb_issue in 10; do\n'
		'RB_ISOLATION_HEAD="' + block + 'printf "judge-ran:%s\\n" "${RB_ISOLATION_ESCALATED}"\ndone\n'
	)
	env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "WORKSPACE_PATH")}
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert ("judge-ran:false" in result.stdout) is should_resume
	assert ("removed" in _read(tmp_path / "remote_calls")) is should_resume
	assert ("10" not in json.loads(state.read_text(encoding="utf-8")).get("review_blocked_isolation_state", {})) is should_resume


@pytest.mark.parametrize("escalated, expected_warning", [(True, False), (False, True)])
def test_isolation_escalation_does_not_promise_next_tick_retry(tmp_path: Path, escalated: bool, expected_warning: bool) -> None:
	text = POLLER.read_text(encoding="utf-8")
	notice = '        echo "::warning::Review-blocked judge failed for issue #${rb_issue}"' + text.split(
		'        echo "::warning::Review-blocked judge failed for issue #${rb_issue}"', 1,
	)[1].split('        # Reset worktree', 1)[0]
	script = (
		"set -euo pipefail\n"
		f'RB_ISOLATION_ESCALATED={str(escalated).lower()}\n'
		'RB_ISOLATION_REASON=sandbox_prepare_failed\nrb_issue=10\nRB_PR=77\n'
		'tg_notify() { printf "%s\\n" "$1" >> notifications; }\n'
		'_gh_url() { printf "https://example.invalid/%s" "$1"; }\n'
		+ notice
	)
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path,
		env={key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "WORKSPACE_PATH")},
		capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert ("Will retry next poll cycle" in _read(tmp_path / "notifications")) is expected_warning


# Each judge call site must never run a credentialed host agent.
SITES = {
	"SECURITY_JUDGE": (
		'poller_claude_judge SECURITY_JUDGE "${prompt_file}" "${output_file}" "${error_file}" "${effective_judge_model}" || security_judge_rc=$?',
	),
	"INTEGRATION_JUDGE": (
		'poller_claude_judge INTEGRATION_JUDGE "${prompt_file}" "${output_file}" "${RUNTIME_DIR}/integration_judge.log" "${MODEL_EDITOR:-openai/gpt-6-sol}" || integration_judge_rc=$?',
	),
	"STALL_JUDGE": (
		'poller_claude_judge STALL_JUDGE "${stall_judge_prompt_file}" "${stall_judge_output_file}" "${RUNTIME_DIR}/stall_judge.log" || stall_judge_rc=$?',
	),
	"WAVE_JUDGE": (
		'poller_claude_judge WAVE_JUDGE "${judge_effective_prompt_file}" "${JUDGE_OUTPUT_FILE}" "${RUNTIME_DIR}/judge_log.txt" || wave_judge_rc=$?',
	),
}


def test_each_judge_uses_sandbox_without_host_fallback() -> None:
	text = POLLER.read_text(encoding="utf-8")
	for role, (claude_call,) in SITES.items():
		assert text.count(claude_call) == 1, role
		start = text.index(claude_call)
		assert 'if [ "${' + role.lower() + '_rc}" -eq 75 ]; then' not in text[start:start + 700]
	assert 'poller_judge_isolated "${role}"' in text
	assert 'claude_run "${role}"' not in text
	assert len(re.findall(r"poller_claude_judge (?:SECURITY_JUDGE|INTEGRATION_JUDGE|STALL_JUDGE|WAVE_JUDGE|RB_JUDGE) ", text)) == len(SITES) + 2
	rb_block = text[text.index('      # Run the judge\n      RB_JUDGE_SUCCESS=false'):text.index('      # Parse judge output')]
	assert 'poller_claude_judge RB_JUDGE "${RB_JUDGE_PROMPT_FILE}" "${RB_JUDGE_OUTPUT_FILE}" "${RUNTIME_DIR}/rb_judge_${rb_issue}.log"' in rb_block
	assert 'pushd "${RB_COMBINED_WORKDIR}" >/dev/null ||' in rb_block
	assert 'poller_claude_judge RB_JUDGE ' in rb_block.split('pushd "${RB_COMBINED_WORKDIR}"', 1)[1].split('popd >/dev/null', 1)[0]
	assert 'pushd "${judge_wt}" >/dev/null;' in text
	assert 'if [ "${RB_JUDGE_ENGINE_RC}" -eq 77 ] || [ "${RB_JUDGE_ENGINE_RC}" -eq 75 ]; then\n            RB_JUDGE_ISOLATION_FAILED=true\n            break\n          fi' in rb_block
	assert 'if [ "${RB_JUDGE_ENGINE_RC}" -eq 76 ]; then\n            break\n          fi' in rb_block
	assert 'danger-full-access' not in rb_block and not re.search(r'\bcodex\s+.*\bexec\b', rb_block)
	assert '.review_blocked_isolation_state' in rb_block
	assert 'ai:rb-judge-isolation-escalated' in text
	assert text.index('outcome=skip reason=escalated') < text.index('RB_COMBINED_MODE="false"', text.index('Detected review-blocked issues'))
	assert 'RB_JUDGE_ISOLATION_MAX_FAILURES="${RB_JUDGE_ISOLATION_MAX_FAILURES:-3}"' in text
	assert 'JUDGE_ISOLATION_MAX_FAILURES="${JUDGE_ISOLATION_MAX_FAILURES:-3}"' in text
	assert 'SECURITY_PASS_JUDGE_OUTCOME=deferred' in text
	assert '_record_judge_isolation_failure WAVE_JUDGE "${STATE_FILE}"' in text
	assert '_record_judge_isolation_failure STALL_JUDGE "${_judge_state_file}"' in text
	assert '_record_judge_isolation_failure INTEGRATION_JUDGE "${STATE_FILE}"' in text
	assert '_record_judge_isolation_failure SECURITY_JUDGE "${STATE_FILE}"' in text


def test_integration_judge_does_not_charge_an_already_active_resolver() -> None:
	text = POLLER.read_text(encoding="utf-8")
	invocation = text.split("invoke_judge_for_integration_conflict() {", 1)[1].split("\n}\n", 1)[0]
	caller = text.split('invoke_judge_for_integration_conflict "${final_pr}" "${integration_branch}" "${default_branch}" || integration_judge_result=$?', 1)[1]
	assert 'if [ "${integration_dispatch_rc}" -eq 2 ]; then' in invocation
	assert 'return 3' in invocation.split('if [ "${integration_dispatch_rc}" -eq 2 ]; then', 1)[1].split("\n    fi", 1)[0]
	assert caller.index('if [ "${integration_judge_result}" -eq 3 ]; then') < caller.index('total_dispatches=$((total_dispatches + 1))')
	deferral = caller.split('    if [ "${integration_judge_result}" -eq 2 ]; then', 1)[0]
	program = ('set -euo pipefail\n'
		'invoke_judge_for_integration_conflict() { return 3; }\n'
		'check_deferral() {\nlocal final_pr=77 integration_branch=integration default_branch=main integration_judge_result=0\n'
		'invoke_judge_for_integration_conflict "${final_pr}" "${integration_branch}" "${default_branch}" || integration_judge_result=$?\n'
		+ deferral + '\nprintf "unexpected accounting\\n"\n}\ncheck_deferral\n')
	result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "Resolver already in flight" in result.stdout
	assert "unexpected accounting" not in result.stdout


def test_active_resolver_skips_integration_judge_before_model_call() -> None:
	text = POLLER.read_text(encoding="utf-8")
	gate = text.split('  if [ "${unresolved_ticks}" -ge "${effective_max_retries}" ]; then', 1)[1].split(
		'    invoke_judge_for_integration_conflict "${final_pr}"', 1,
	)[0]
	program = ('set -euo pipefail\n'
		+ '_CONFLICT_DISPATCH_TRACKER=/dev/null\n'
		+ '_has_active_autofix_run() { printf "active\\n"; return 0; }\n'
		+ 'check_gate() {\nlocal unresolved_ticks=3 effective_max_retries=1 final_pr=77 integration_branch=integration\n'
		+ 'if [ "${unresolved_ticks}" -ge "${effective_max_retries}" ]; then\n'
		+ gate + 'printf "judge-called\\n"\nfi\n}\ncheck_gate\n')
	result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "active" in result.stdout
	assert "judge-called" not in result.stdout
	assert gate.index('_has_active_autofix_run "${final_pr}"') < len(gate)


def test_cycle_local_dispatch_skips_integration_judge_before_run_is_visible(tmp_path: Path) -> None:
	text = POLLER.read_text(encoding="utf-8")
	gate = text.split('  if [ "${unresolved_ticks}" -ge "${effective_max_retries}" ]; then', 1)[1].split(
		'    invoke_judge_for_integration_conflict "${final_pr}"', 1,
	)[0]
	tracker = tmp_path / "dispatches"
	tracker.write_text("77\n", encoding="utf-8")
	program = ('set -euo pipefail\n'
		+ f'_CONFLICT_DISPATCH_TRACKER={tracker}\n'
		+ '_has_active_autofix_run() { printf "API queried\\n"; return 1; }\n'
		+ 'check_gate() {\nlocal unresolved_ticks=3 effective_max_retries=1 final_pr=77 integration_branch=integration\n'
		+ 'if [ "${unresolved_ticks}" -ge "${effective_max_retries}" ]; then\n'
		+ gate + 'printf "judge-called\\n"\nfi\n}\ncheck_gate\n')
	result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "judge-called" not in result.stdout
	assert "API queried" not in result.stdout


def test_unpublished_integration_guidance_defers_resolver_dispatch() -> None:
	text = POLLER.read_text(encoding="utf-8")
	invocation = text.split("invoke_judge_for_integration_conflict() {", 1)[1].split("\n}\n", 1)[0]
	assert 'post_issue_comment_json "${TRACKING_NUM}" "${integration_comment_body}"' in invocation
	assert 'if ! printf \'%s\' "${integration_comment_result}" | jq -e' in invocation
	assert invocation.index('return 4') < invocation.index('_dispatch_review_for_conflicts "${final_pr}"')
	caller = text.split('invoke_judge_for_integration_conflict "${final_pr}" "${integration_branch}" "${default_branch}" || integration_judge_result=$?', 1)[1]
	assert caller.index('if [ "${integration_judge_result}" -eq 4 ]; then') < caller.index('total_dispatches=$((total_dispatches + 1))')


def test_integration_guidance_is_head_and_base_bound_and_untrusted(tmp_path: Path) -> None:
	prepare = (REPO_ROOT / "scripts" / "review_conflict_prepare.sh").read_text(encoding="utf-8")
	resolver_prompt = (REPO_ROOT / "prompts" / "integration-sync-conflict-resolver.txt").read_text(encoding="utf-8")
	assert '{{JUDGE_GUIDANCE}}' in resolver_prompt
	assert 'UNTRUSTED INTEGRATION JUDGE GUIDANCE' in prepare
	assert 'INTEGRATION_JUDGE_GUIDANCE=""' in prepare
	assert "tpl=tpl.replace('{{JUDGE_GUIDANCE}}', os.environ.get('JUDGE_GUIDANCE',''))" in prepare
	assert 'INTEGRATION_JUDGE_GUIDANCE="$(jq -sr ' in prepare
	judge = POLLER.read_text(encoding="utf-8")
	assert 'integration-judge-guidance:v1 pr=${final_pr} head=$(git -C "${judge_wt}" rev-parse HEAD) base=$(git -C "${judge_wt}" rev-parse "refs/remotes/origin/${default_branch}")' in judge
	if shutil.which("jq") is None:
		pytest.skip("jq required to exercise the workflow's comment selector")

	git_env = {key: value for key, value in os.environ.items() if key not in ("GIT_DIR", "GIT_WORK_TREE", "BASH_ENV", "ENV")}
	subprocess.run(["git", "init", "-q", str(tmp_path)], env=git_env, check=True)
	subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
		"commit", "--allow-empty", "-qm", "initial"], env=git_env, check=True)
	head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, env=git_env, text=True).strip()
	subprocess.run(["git", "update-ref", "refs/remotes/origin/main", head_sha], cwd=tmp_path, env=git_env, check=True)
	selector = 'INTEGRATION_JUDGE_GUIDANCE="$(jq -sr ' + prepare.split('INTEGRATION_JUDGE_GUIDANCE="$(jq -sr ', 1)[1].split('    _state_payload=', 1)[0]
	marker = f"<!-- ai:integration-judge-guidance:v1 pr=77 head={head_sha} base={head_sha} -->"
	comment = "## Integration conflict diagnosis\n\nFinal PR #77: conflict\n\nResolver guidance: preserve both changes\n\n"
	for suffix, expected in ((marker, True), (marker.replace("pr=77", "pr=78"), False),
		(marker.replace(f"head={head_sha}", "head=" + "a" * 40), False),
		(marker.replace(f"base={head_sha}", "base=" + "b" * 40), False)):
		comments_file = tmp_path / "comments.json"
		comments_file.write_text(json.dumps([{"body": comment + suffix}]), encoding="utf-8")
		env = {**git_env, "PR_NUMBER": "77", "BASE_BRANCH": "main", "_ti_comments_raw": str(comments_file)}
		result = subprocess.run(["bash", "-c", 'set -euo pipefail\n' + selector + 'printf "%s" "$INTEGRATION_JUDGE_GUIDANCE"'],
			cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
		assert ("preserve both changes" in result.stdout) is expected


def test_judge_isolation_defers_escalates_once_and_resets_on_label_removal(tmp_path: Path) -> None:
	text = POLLER.read_text(encoding="utf-8")
	functions = "\n".join(re.search(rf"^{name}\(\)\n\{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL).group(0)
		for name in ("_judge_isolation_should_run", "_clear_judge_isolation_state", "_judge_isolation_checkpoint", "_record_judge_isolation_failure"))
	state = tmp_path / "state.json"
	state.write_text('{"judge_isolation_state":"invalid"}\n', encoding="utf-8")
	(tmp_path / "judge_isolation_reason").write_text("sandbox_prepare_failed\n", encoding="utf-8")
	script = ("set -euo pipefail\n" + functions + f'RUNTIME_DIR={tmp_path}\nTRACKING_NUM=12\n'
		+ f'JUDGE_ISOLATION_MAX_FAILURES=2\nSTATE_FILE={state}\nGITHUB_REPOSITORY=owner/repo\n'
		+ 'ensure_label_exists() { :; }\n'
		+ 'post_state_comment() { :; }\n'
		+ 'gh_retry() { printf "%s\\n" "$*" >> gh_calls; if [ "$1" = _safe_gh_jq ]; then printf \'{"labels":[]}\\n\'; elif [ "$1 $2" = "gh api" ]; then printf \'{"id":42}\\n\'; fi; }\n'
		+ 'tg_notify() { printf "%s\\n" "$*" >> alerts; }\n'
		+ '_record_judge_isolation_failure WAVE_JUDGE "$STATE_FILE" 12 \'[]\'\n'
		+ '_record_judge_isolation_failure WAVE_JUDGE "$STATE_FILE" 12 \'[]\'\n'
		+ 'if _judge_isolation_should_run WAVE_JUDGE "$STATE_FILE" 12 \'["ai:needs-human"]\'; then exit 8; fi\n'
		+ '_judge_isolation_should_run WAVE_JUDGE "$STATE_FILE" 12 \'[]\'\n'
		+ 'jq -e \'.judge_isolation_state.WAVE_JUDGE == null\' "$STATE_FILE"\n')
	env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "WORKSPACE_PATH")}
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert result.stdout.count("outcome=escalated") == 1
	assert "outcome=skip reason=escalated" in result.stdout
	assert "outcome=reset reason=label_cleared" in result.stdout
	assert _read(tmp_path / "gh_calls").count("gh issue edit") == 1
	assert _read(tmp_path / "gh_calls").count("gh api") == 1
	assert _read(tmp_path / "gh_calls").count("_safe_gh_jq repos/owner/repo/issues/12 --jq") == 1
	assert _read(tmp_path / "alerts").count("CRITICAL") == 1


@pytest.mark.parametrize("snapshot, live_labels, issue, escalated, expected_rc, reason, api_calls", [
	('[]', '{"labels":[]}', '12', True, 0, 'label_cleared', 1),
	('[]', '', '12', True, 1, 'labels_unavailable', 1),
	('[]', '{"labels":[{"name":"ai:needs-human"}]}', '12', True, 1, 'escalated', 1),
	('["ai:needs-human"]', '', '12', True, 1, 'escalated', 0),
	('null', '{"labels":[]}', '12', True, 0, 'label_cleared', 1),
	('[]', '{"number":12}', '12', True, 1, 'labels_unavailable', 1),
	('[]', '{"labels":null}', '12', True, 1, 'labels_unavailable', 1),
	('[]', '{"labels":{}}', '12', True, 1, 'labels_unavailable', 1),
	('[]', '{"labels":[{}]}', '12', True, 1, 'labels_unavailable', 1),
	('[]', '{"labels":[{"name":1}]}', '12', True, 1, 'labels_unavailable', 1),
	('[]', 'not json', '12', True, 1, 'labels_unavailable', 1),
	('[]', '{"labels":[]}', 'not-a-number', True, 1, 'labels_unavailable', 0),
	('[]', '', '12', False, 0, None, 0),
])
def test_judge_isolation_requires_live_label_removal(
	tmp_path: Path, snapshot: str, live_labels: str, issue: str, escalated: bool,
	expected_rc: int, reason: str | None, api_calls: int,
) -> None:
	text = POLLER.read_text(encoding="utf-8")
	functions = "\n".join(re.search(rf"^{name}\(\)\n\{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL).group(0)
		for name in ("_judge_isolation_should_run", "_clear_judge_isolation_state"))
	state = tmp_path / "state.json"
	state.write_text(json.dumps({"judge_isolation_state": {"WAVE_JUDGE": {"count": 3, "escalated": escalated}}}), encoding="utf-8")
	script = (
		"set -euo pipefail\n" + functions
		+ 'GITHUB_REPOSITORY=owner/repo\nJUDGE_ISOLATION_MAX_FAILURES=3\n'
		+ f'STATE_FILE={state}\n'
		+ 'gh_retry() { printf "%s\\n" "$*" >> gh_calls; '
		+ (f"printf '%s\\n' '{live_labels}' | jq \"$4\"" if live_labels else "return 1")
		+ '; }\n'
		+ f'rc=0; _judge_isolation_should_run WAVE_JUDGE "$STATE_FILE" {issue} \'{snapshot}\' || rc=$?\n'
		+ 'printf "rc=%s\\n" "$rc"\n'
	)
	env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "WORKSPACE_PATH")}
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert f"rc={expected_rc}" in result.stdout
	if reason is not None:
		assert f"reason={reason}" in result.stdout
	assert _read(tmp_path / "gh_calls").count("_safe_gh_jq") == api_calls
	assert ("WAVE_JUDGE" in json.loads(state.read_text(encoding="utf-8"))["judge_isolation_state"]) is (reason != "label_cleared")


def _poll_steps() -> list[dict]:
	data = yaml.safe_load(POLL_WORKFLOW.read_text(encoding="utf-8"))
	return data["jobs"]["poll"]["steps"]


def test_poll_job_stages_the_engine_and_fetches_the_pool_only_when_needed() -> None:
	steps = _poll_steps()
	names = [step.get("name") for step in steps]
	opencode = steps[names.index("Install OpenCode CLI for isolated review-blocked judge")]
	assert opencode["if"] == "steps.find_tracking.outputs.has_work == 'true'"
	assert opencode["continue-on-error"] is True
	assert opencode["uses"] == (
		"shubhodeep1/coding-workflows/.github/actions/install-opencode@28f5134003514b5cf31fb8ae52778c2be79d8fde"
	)
	assert opencode["with"]["opencode_version"] == "${{ vars.OPENCODE_VERSION || '1.18.23' }}"
	assert names.index("Install OpenCode CLI for isolated review-blocked judge") < names.index("Process each tracking issue")
	stage = steps[names.index("Stage workflow support files")]["run"]
	assert "security_dependency.py ai_engine.sh claude_engine.py claude_settings.json.tmpl codex_stall_guard.sh; do" in stage
	resolve = steps[names.index("Resolve AI engine")]
	assert resolve["id"] == "ai_engine"
	assert resolve["env"]["AI_ENGINE_LABELS"] == ""
	assert '--json number,title,labels' in steps[names.index("Find active tracking issues")]["run"]
	assert 'any(.[]; any(.labels[]?; .name == "ai:engine-claude") and (any(.labels[]?; .name == "ai:codex") | not))' in resolve["run"]
	assert 'any(.[]; all(.labels[]?; .name != "ai:codex"))' in resolve["run"]
	assert "for role in WAVE_JUDGE STALL_JUDGE INTEGRATION_JUDGE SECURITY_JUDGE RB_JUDGE; do" in resolve["run"]
	for name, uses in (
		("Install Claude Code CLI", "./.codex-workflow-src/.github/actions/install-claude"),
		("Resolve Claude credential", "./.codex-workflow-src/.github/actions/claude-pool-token"),
	):
		step = steps[names.index(name)]
		assert step["uses"] == uses
		assert step["continue-on-error"] is True
		assert step["if"] == "steps.find_tracking.outputs.has_work == 'true' && steps.ai_engine.outputs.any_claude == 'true'"
		assert names.index(name) < names.index("Process each tracking issue")
	process_env = steps[names.index("Process each tracking issue")]["env"]
	for role in ("WAVE_JUDGE", "STALL_JUDGE", "INTEGRATION_JUDGE", "SECURITY_JUDGE", "RB_JUDGE"):
		assert process_env[f"AI_ENGINE_{role}"] == f"${{{{ vars.AI_ENGINE_{role} || '' }}}}"
	assert process_env["CLAUDE_FIXER_ENABLED"] == "${{ vars.CLAUDE_FIXER_ENABLED || 'true' }}"
	assert process_env["RB_JUDGE_ISOLATION_MAX_FAILURES"] == "${{ vars.RB_JUDGE_ISOLATION_MAX_FAILURES || '3' }}"
	assert process_env["JUDGE_ISOLATION_MAX_FAILURES"] == "${{ vars.JUDGE_ISOLATION_MAX_FAILURES || '3' }}"


def test_poll_preflight_installs_for_label_even_with_global_codex(tmp_path: Path) -> None:
	steps = _poll_steps()
	preflight = next(step["run"] for step in steps if step.get("name") == "Resolve AI engine")
	issue_file = tmp_path / "tracking_issues.json"
	output_file = tmp_path / "github_output"
	env = {**os.environ, "RUNTIME_DIR": str(tmp_path), "GITHUB_OUTPUT": str(output_file), "AI_ENGINE": "codex", "AI_ENGINE_LABELS": ""}
	for role in ("WAVE_JUDGE", "STALL_JUDGE", "INTEGRATION_JUDGE", "SECURITY_JUDGE", "RB_JUDGE"):
		env[f"AI_ENGINE_{role}"] = ""
	for engine, labels, expected in (
		("codex", ["ai:engine-claude"], "true"),
		("codex", ["ai:codex"], "false"),
		("codex", ["ai:engine-claude", "ai:codex"], "false"),
		("claude", ["ai:codex"], "false"),
	):
		env["AI_ENGINE"] = engine
		issue_file.write_text(json.dumps([{"number": 1, "labels": [{"name": label} for label in labels]}]), encoding="utf-8")
		output_file.write_text("", encoding="utf-8")
		result = subprocess.run(["bash", "-c", preflight], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
		assert output_file.read_text(encoding="utf-8").strip() == f"any_claude={expected}"
	issue_file.write_text(json.dumps([
		{"number": 1, "labels": [{"name": "ai:codex"}]},
		{"number": 2, "labels": [{"name": "ai:engine-claude"}]},
	]), encoding="utf-8")
	env["AI_ENGINE"] = "codex"
	output_file.write_text("", encoding="utf-8")
	result = subprocess.run(["bash", "-c", preflight], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert output_file.read_text(encoding="utf-8").strip() == "any_claude=true"


def test_poll_preflight_fails_open_when_engine_helper_cannot_be_sourced(tmp_path: Path) -> None:
	preflight = next(step["run"] for step in _poll_steps() if step.get("name") == "Resolve AI engine")
	(tmp_path / "tracking_issues.json").write_text('[{"number":1,"labels":[]}]', encoding="utf-8")
	(tmp_path / "scripts").mkdir()
	(tmp_path / "scripts" / "ai_engine.sh").write_text("return 42\n", encoding="utf-8")
	output_file = tmp_path / "github_output"
	env = {**os.environ, "RUNTIME_DIR": str(tmp_path), "GITHUB_OUTPUT": str(output_file), "AI_ENGINE": "claude"}
	result = subprocess.run(["bash", "-c", preflight], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert output_file.read_text(encoding="utf-8").strip() == "any_claude=false"
	assert "Failed to source scripts/ai_engine.sh; continuing with codex defaults." in result.stderr


ORCHESTRATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate.yml"
DECOMPOSER_CODEX = (
	'timeout --signal=TERM --kill-after=30s -- "${ORCHESTRATE_DECOMPOSER_PER_ATTEMPT_TIMEOUT_SECS}" \\\n'
	'       codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${CODEX_PROMPT_FILE}" > "${CODEX_OUTPUT_FILE}" 2> >(tee -a "${RUNTIME_DIR}/codex_log.txt" >&2) || decomposer_rc=$?'
)


def _orchestrate_steps() -> dict[str, dict]:
	data = yaml.safe_load(ORCHESTRATE_WORKFLOW.read_text(encoding="utf-8"))
	return {step.get("name"): step for step in data["jobs"]["orchestrate"]["steps"]}


def _decomposer_engine_block() -> str:
	run = _orchestrate_steps()["Run Codex (decomposer)"]["run"]
	start = run.index("  decomposer_rc=75\n")
	end = run.index('  if [ "${decomposer_rc}" -eq 0 ]; then\n')
	return run[start:end]


def _run_decomposer_block(tmp_path: Path, engine: str, claude_mode: str) -> tuple[subprocess.CompletedProcess[str], Path]:
	scripts = tmp_path / "scripts"
	scripts.mkdir(parents=True, exist_ok=True)
	(scripts / "ai_engine.sh").write_text(FAKE_AI_ENGINE, encoding="utf-8")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	codex = bin_dir / "codex"
	codex.write_text('#!/usr/bin/env bash\nprintf "codex %s\\n" "$*" >> "${CALLS}.codex"\nprintf "codex plan\\n"\n', encoding="utf-8")
	codex.chmod(0o755)
	(tmp_path / "prompt.txt").write_text("decompose\n", encoding="utf-8")
	calls = tmp_path / "calls"
	script = (
		"set -euo pipefail\n"
		"CODEX_PROMPT_FILE=prompt.txt\nCODEX_OUTPUT_FILE=out.txt\nRUNTIME_DIR=.\n"
		"ORCHESTRATE_DECOMPOSER_PER_ATTEMPT_TIMEOUT_SECS=30\nMODEL_EDITOR=openai/gpt-6-sol\nMODEL_REASONING_EFFORT=high\n"
		f"ORCHESTRATE_ENGINE={engine}\n"
		+ _decomposer_engine_block()
		+ 'echo "rc=${decomposer_rc} engine=${ORCHESTRATE_ENGINE}"\n'
	)
	env = dict(os.environ, CALLS=str(calls), FAKE_ENGINE="claude", FAKE_CLAUDE_MODE=claude_mode, PATH=f"{bin_dir}:{os.environ['PATH']}")
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
		env.pop(inherited, None)
	proc = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
	return proc, calls


def test_decomposer_on_claude_runs_claude_run_only(tmp_path: Path) -> None:
	proc, calls = _run_decomposer_block(tmp_path, "claude", "success")
	assert "rc=0 engine=claude" in proc.stdout, proc.stderr
	assert _read(tmp_path / "out.txt") == "claude verdict\n"
	assert _read(Path(f"{calls}.claude")).split("|")[:3] == ["ORCHESTRATE", "prompt.txt", "out.txt"]
	assert _read(Path(f"{calls}.codex")) == ""


def test_decomposer_falls_back_to_codex_and_stays_there(tmp_path: Path) -> None:
	proc, calls = _run_decomposer_block(tmp_path, "claude", "unavailable")
	assert "rc=0 engine=codex" in proc.stdout, proc.stderr
	assert _read(tmp_path / "out.txt") == "codex plan\n"
	assert "exec --skip-git-repo-check --model openai/gpt-6-sol --sandbox danger-full-access" in _read(Path(f"{calls}.codex"))


def test_decomposer_claude_crash_keeps_its_status(tmp_path: Path) -> None:
	proc, calls = _run_decomposer_block(tmp_path, "claude", "crash")
	assert "rc=1 engine=claude" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.codex")) == ""


def test_decomposer_on_codex_never_touches_claude(tmp_path: Path) -> None:
	proc, calls = _run_decomposer_block(tmp_path, "codex", "success")
	assert "rc=0 engine=codex" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.claude")) == ""
	assert _read(tmp_path / "out.txt") == "codex plan\n"


def test_decomposer_codex_command_and_failure_status_are_kept() -> None:
	run = _orchestrate_steps()["Run Codex (decomposer)"]["run"]
	assert run.count(DECOMPOSER_CODEX) == 1
	assert 'rc="${decomposer_rc}"' in run
	assert "    rc=$?\n" not in run


def test_orchestrate_job_resolves_the_engine_from_the_engine_input() -> None:
	steps = _orchestrate_steps()
	names = list(steps)
	labels = "${{ inputs.engine == 'claude' && 'ai:engine-claude' || (inputs.engine == 'codex' && 'ai:codex' || '') }}"
	resolve = steps["Resolve AI engine"]
	assert resolve["env"]["AI_ENGINE_LABELS"] == labels
	assert 'engine="$(ai_engine_for_role ORCHESTRATE || echo codex)"' in resolve["run"]
	assert steps["Run Codex (decomposer)"]["env"]["AI_ENGINE_LABELS"] == labels
	assert steps["Run Codex (decomposer)"]["env"]["ORCHESTRATE_ENGINE"] == "${{ steps.ai_engine.outputs.engine || 'codex' }}"
	for name in ("Install Claude Code CLI", "Resolve Claude credential"):
		assert steps[name]["if"] == "steps.ai_engine.outputs.engine == 'claude'"
		assert names.index("Resolve AI engine") < names.index(name) < names.index("Run Codex (decomposer)")
	assert "write_codex_config.sh ai_engine.sh claude_engine.py claude_settings.json.tmpl; do" in steps["Stage workflow support files"]["run"]


def test_decomposer_preflight_fails_open_when_engine_helper_cannot_be_sourced(tmp_path: Path) -> None:
	preflight = _orchestrate_steps()["Resolve AI engine"]["run"]
	(tmp_path / "scripts").mkdir()
	(tmp_path / "scripts" / "ai_engine.sh").write_text("return 42\n", encoding="utf-8")
	output_file = tmp_path / "github_output"
	env = {**os.environ, "GITHUB_OUTPUT": str(output_file), "AI_ENGINE": "claude"}
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
		env.pop(inherited, None)
	result = subprocess.run(["bash", "-c", preflight], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert output_file.read_text(encoding="utf-8").strip() == "engine=codex"
	assert "Failed to source scripts/ai_engine.sh; continuing with codex defaults." in result.stderr
