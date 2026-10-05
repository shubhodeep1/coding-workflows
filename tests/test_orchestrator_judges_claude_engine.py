"""Phase 5c (replace-claude-sessions plan): the orchestrator judges on Claude.

scripts/orchestrate_poll_process.sh runs the wave, stall, integration,
security-pass and review-blocked judges through ``poller_claude_judge``. On
Claude the judge goes through ``claude_run``; other roles keep their host
codex fallback, but RB_JUDGE uses only the isolated review sandbox.
These tests run the helper against a stand-in ai_engine.sh and pin each call
site's codex command (G4).
"""

from __future__ import annotations

import json
import os
import re
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
		for name in ("poller_claude_judge", "_poller_rb_judge_sandbox_attempt", "poller_rb_judge_isolated")
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
	proc = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
	return proc, calls


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8") if path.exists() else ""


def test_judge_on_claude_runs_claude_run_with_the_tracking_labels(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, engine="claude")
	assert "rc=0" in proc.stdout, proc.stderr
	assert _read(tmp_path / "out.txt") == "claude verdict\n"
	assert _read(Path(f"{calls}.claude")).splitlines() == [
		f'WAVE_JUDGE|prompt.txt|out.txt|{tmp_path}|["bug","ai:engine-claude"]|openai/gpt-6-sol|xhigh',
	]
	assert _read(Path(f"{calls}.resolve")).splitlines() == ['WAVE_JUDGE|["bug","ai:engine-claude"]']
	assert "AI_ENGINE_SELECTED role=WAVE_JUDGE engine=claude" in _read(tmp_path / "judge_log.txt")


def test_judge_on_codex_returns_75_without_running_claude(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, engine="codex")
	assert "rc=75" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.claude")) == ""


def test_dev_null_suppresses_judge_selection_stderr(tmp_path: Path) -> None:
	proc, _calls = _run_helper(tmp_path, engine="codex", log_file="/dev/null")
	assert "rc=75" in proc.stdout
	assert proc.stderr == ""


def test_claude_unavailable_returns_75_so_codex_runs(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, engine="claude", claude_mode="unavailable")
	assert "rc=75" in proc.stdout, proc.stderr
	assert len(_read(Path(f"{calls}.claude")).splitlines()) == 1


def test_claude_crash_is_not_a_fallback(tmp_path: Path) -> None:
	proc, _calls = _run_helper(tmp_path, engine="claude", claude_mode="crash")
	assert "rc=1" in proc.stdout, proc.stderr


def test_missing_ai_engine_returns_75(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, engine="claude", with_engine=False)
	assert "rc=75" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.resolve")) == ""


def test_no_tracking_labels_means_an_explicit_empty_list(tmp_path: Path) -> None:
	_proc, calls = _run_helper(tmp_path, engine="codex", tracking_labels=None)
	assert _read(Path(f"{calls}.resolve")).splitlines() == ["WAVE_JUDGE|[]"]


def test_model_hint_argument_overrides_model_editor(tmp_path: Path) -> None:
	_proc, calls = _run_helper(tmp_path, engine="claude", extra="claude-sonnet-5-5")
	assert _read(Path(f"{calls}.claude")).split("|")[5] == "claude-sonnet-5-5"


FAKE_RB_SANDBOX = r"""#!/usr/bin/env bash
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
		proc, calls = _run_helper(tmp_path, engine="claude", claude_mode=mode, role="RB_JUDGE", combined_mode="true")
		assert f"rc={expected}" in proc.stdout, proc.stderr
		if mode == "prepare_failed":
			assert _read(tmp_path / "rb_judge_isolation_reason").strip() == "sandbox_prepare_failed"
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
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert ("Will retry next poll cycle" in _read(tmp_path / "notifications")) is expected_warning


# Each judge call site: the role, then the unchanged codex command that runs
# only on exit 75.
SITES = {
	"SECURITY_JUDGE": (
		'poller_claude_judge SECURITY_JUDGE "${prompt_file}" "${output_file}" "${error_file}" "${effective_judge_model}" || security_judge_rc=$?',
		'if [ "${security_judge_rc}" -eq 75 ]; then',
		'-- codex --ask-for-approval never -c model_verbosity=low exec --skip-git-repo-check \\\n            --model "${effective_judge_model}" --sandbox read-only < "${prompt_file}" || true',
	),
	"INTEGRATION_JUDGE": (
		'poller_claude_judge INTEGRATION_JUDGE "${prompt_file}" "${output_file}" "${RUNTIME_DIR}/integration_judge.log" "${MODEL_EDITOR:-openai/gpt-6-sol}" || integration_judge_rc=$?',
		'if [ "${integration_judge_rc}" -eq 75 ]; then',
		'cat "${prompt_file}" | codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR:-openai/gpt-6-sol}" --sandbox danger-full-access > "${output_file}" 2>> "${RUNTIME_DIR}/integration_judge.log" || integration_judge_rc=$?',
	),
	"STALL_JUDGE": (
		'poller_claude_judge STALL_JUDGE "${stall_judge_prompt_file}" "${stall_judge_output_file}" "${RUNTIME_DIR}/stall_judge.log" || stall_judge_rc=$?',
		'if [ "${stall_judge_rc}" -eq 75 ]; then',
		'codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${stall_judge_prompt_file}" > "${stall_judge_output_file}" 2>> "${RUNTIME_DIR}/stall_judge.log" || true',
	),
	"WAVE_JUDGE": (
		'poller_claude_judge WAVE_JUDGE "${judge_effective_prompt_file}" "${JUDGE_OUTPUT_FILE}" "${RUNTIME_DIR}/judge_log.txt" || wave_judge_rc=$?',
		'if [ "${wave_judge_rc}" -eq 75 ]; then',
		'cat "${judge_effective_prompt_file}" | codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access > "${JUDGE_OUTPUT_FILE}" 2> >(tee -a "${RUNTIME_DIR}/judge_log.txt" >&2) || true',
	),
}


def test_each_judge_tries_claude_then_runs_the_unchanged_codex_command() -> None:
	text = POLLER.read_text(encoding="utf-8")
	for role, (claude_call, gate, codex_call) in SITES.items():
		assert text.count(claude_call) == 1, role
		start = text.index(claude_call)
		assert text.index(gate, start) > start, role
		assert text.index(codex_call, start) > text.index(gate, start), role
		assert text.count(codex_call) == 1, role
	assert len(re.findall(r"^\s+poller_claude_judge [A-Z_]+ ", text, re.MULTILINE)) == len(SITES) + 1
	assert '[ "${RB_JUDGE_ENGINE_RC}" -ne 76 ] || break' in text
	rb_block = text[text.index('      # Run the judge\n      RB_JUDGE_SUCCESS=false'):text.index('      # Parse judge output')]
	assert 'poller_claude_judge RB_JUDGE "${RB_JUDGE_PROMPT_FILE}" "${RB_JUDGE_OUTPUT_FILE}" "${RUNTIME_DIR}/rb_judge_${rb_issue}.log"' in rb_block
	assert 'RB_JUDGE_ENGINE_RC}" -eq 77' in rb_block
	assert 'danger-full-access' not in rb_block and not re.search(r'\bcodex\s+.*\bexec\b', rb_block)
	assert '.review_blocked_isolation_state' in rb_block
	assert 'ai:rb-judge-isolation-escalated' in text
	assert text.index('outcome=skip reason=escalated') < text.index('RB_COMBINED_MODE="false"', text.index('Detected review-blocked issues'))
	assert 'RB_JUDGE_ISOLATION_MAX_FAILURES="${RB_JUDGE_ISOLATION_MAX_FAILURES:-3}"' in text


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
	assert 'any(.[]; any(.labels[]?; .name == "ai:engine-claude"))' in resolve["run"]
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


def test_poll_preflight_installs_for_label_even_with_global_codex(tmp_path: Path) -> None:
	steps = _poll_steps()
	preflight = next(step["run"] for step in steps if step.get("name") == "Resolve AI engine")
	issue_file = tmp_path / "tracking_issues.json"
	output_file = tmp_path / "github_output"
	env = {**os.environ, "RUNTIME_DIR": str(tmp_path), "GITHUB_OUTPUT": str(output_file), "AI_ENGINE": "codex", "AI_ENGINE_LABELS": ""}
	for role in ("WAVE_JUDGE", "STALL_JUDGE", "INTEGRATION_JUDGE", "SECURITY_JUDGE", "RB_JUDGE"):
		env[f"AI_ENGINE_{role}"] = ""
	for labels, expected in ((["ai:engine-claude"], "true"), (["ai:codex"], "false")):
		issue_file.write_text(json.dumps([{"number": 1, "labels": [{"name": label} for label in labels]}]), encoding="utf-8")
		output_file.write_text("", encoding="utf-8")
		result = subprocess.run(["bash", "-c", preflight], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
		assert output_file.read_text(encoding="utf-8").strip() == f"any_claude={expected}"


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
