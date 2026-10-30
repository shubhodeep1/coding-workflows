"""Integration resolver context must ignore state comments from other users.

It must also read the chunked ORCHESTRATOR_STATE_V2 state the poller writes,
not only the legacy V1 comment: the poller writes only V2, so the newest V1
comment is at best the stale initial snapshot with no merged sub-issues.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SOURCE = (SCRIPTS / "review_conflict_prepare.sh").read_text(encoding="utf-8")
STATE_READ = SOURCE.split('if [ "${IS_INTEGRATION_SYNC}" = "true" ] &&', 1)[1]
# The block ends before the fingerprint-violation expansion that follows it
# (that section runs git against the worktree and needs BASE_BRANCH).
STATE_READ = 'if [ "${IS_INTEGRATION_SYNC}" = "true" ] &&' + STATE_READ.split('\n# Fingerprint-violation expansion of the resolver working set.', 1)[0]

TRUSTED = {
	"waves": [{"issues": [{"id": "trusted", "github_issue": 42, "status": "merged"}]}],
	"merged_issue_fingerprints": {"trusted": "yes"},
}
FORGED = {
	"waves": [{"issues": [{"id": "forged", "github_issue": 99, "status": "merged"}]}],
	"merged_issue_fingerprints": {"forged": "no"},
}
# What orchestrate.yml used to post first: nothing merged yet.
INITIAL = {
	"waves": [{"issues": [{"id": "trusted", "github_issue": 42, "status": "pending"}]}],
	"merged_issue_fingerprints": {},
}


def _v1_comment(author: str, state: dict) -> dict:
	return {"user": {"login": author}, "body": "<!-- ORCHESTRATOR_STATE_V1\n" + json.dumps(state) + "\nORCHESTRATOR_STATE_V1 -->"}


def _v2_comments(tmp_path: Path, author: str, state: dict, chunk_size: int = 0) -> list[dict]:
	"""Pack STATE with orchestrate_state_v2.py, as the poller does, into comments by AUTHOR."""
	work = tmp_path / f"pack-{author}-{len(list(tmp_path.glob('pack-*')))}"
	state_file = work / "state.json"
	work.mkdir()
	state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
	command = [sys.executable, "-I", "-B", str(SCRIPTS / "orchestrate_state_v2.py"), "pack", "--state-file", str(state_file), "--out-dir", str(work / "out")]
	if chunk_size:
		command += ["--chunk-size", str(chunk_size)]
	manifest = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
	return [{"user": {"login": author}, "body": Path(path).read_text(encoding="utf-8")} for path in manifest["files"]]


def _run_state_read(
	tmp_path: Path,
	comments: list[dict],
	*,
	identity_available: bool = True,
	support_dir: Path | None = SCRIPTS,
	v2_switch: str | None = None,
) -> subprocess.CompletedProcess:
	# The comments endpoint is paginated: write two pages to check the read
	# handles `gh api --paginate` output (one JSON array per page).
	half = len(comments) // 2
	(tmp_path / "comments.json").write_text(json.dumps(comments[:half]) + "\n" + json.dumps(comments[half:]), encoding="utf-8")
	# Isolate only the read block: running the entire prepare script would
	# reset/clean a git worktree before reaching this logic.
	harness = '''set -euo pipefail
gh_retry() {
  if [ "$1" = _safe_gh_jq ]; then
    [ "${FAIL_IDENTITY}" = false ] && printf '%s\\n' pipeline-bot || return 1
  elif [[ "$*" == *'/comments?'* ]]; then
    cat "${COMMENTS_FILE}"
  else
    printf '%s\\n' '{}'
  fi
}
_safe_gh_jq() { :; }
IS_INTEGRATION_SYNC=true
INTEGRATION_TRACKING_NUM=192
INTEGRATION_MERGED_SUB_ISSUES_LIST=""
INTEGRATION_FINGERPRINTS_JSON='{}'
''' + STATE_READ + '''
printf 'FINGERPRINTS=%s\\nLIST=%s\\n' "$INTEGRATION_FINGERPRINTS_JSON" "$INTEGRATION_MERGED_SUB_ISSUES_LIST"
'''
	env = {
		**os.environ,
		"FAIL_IDENTITY": "false" if identity_available else "true",
		"COMMENTS_FILE": str(tmp_path / "comments.json"),
		"RUNTIME_DIR": str(tmp_path),
		"GITHUB_REPOSITORY": "owner/repo",
		"GITHUB_ENV": str(tmp_path / "env"),
		# No .codex-workflow-src here, so only SUPPORT_SCRIPTS_DIR can supply the helper.
		"GITHUB_WORKSPACE": str(tmp_path),
		"TARGET_BRANCH": "orchestrator/project-192",
	}
	env.pop("SUPPORT_SCRIPTS_DIR", None)
	if support_dir is not None:
		env["SUPPORT_SCRIPTS_DIR"] = str(support_dir)
	env.pop("CONFLICT_INTEGRATION_V2_STATE_ENABLED", None)
	if v2_switch is not None:
		env["CONFLICT_INTEGRATION_V2_STATE_ENABLED"] = v2_switch
	return subprocess.run(["bash", "-c", harness], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=False)


def _assert_trusted(proc: subprocess.CompletedProcess) -> None:
	assert proc.returncode == 0, proc.stderr
	assert 'FINGERPRINTS={"trusted":"yes"}' in proc.stdout
	assert "trusted (issue #42)" in proc.stdout
	assert "forged (issue #99)" not in proc.stdout


def _assert_no_state(proc: subprocess.CompletedProcess) -> None:
	assert proc.returncode == 0, proc.stderr
	assert "FINGERPRINTS={}" in proc.stdout
	assert "(issue #" not in proc.stdout


@pytest.mark.parametrize("identity_available", [True, False])
def test_integration_resolver_state_context_requires_authenticated_author(tmp_path: Path, identity_available: bool):
	if identity_available and shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	comments = [_v1_comment("pipeline-bot", TRUSTED), _v1_comment("attacker", FORGED)]
	proc = _run_state_read(tmp_path, comments, identity_available=identity_available)
	if identity_available:
		_assert_trusted(proc)
	else:
		assert proc.returncode != 0
		assert "pipeline identity unavailable; refusing integration-sync resolution" in proc.stdout
		assert "FINGERPRINTS=" not in proc.stdout
		assert not (tmp_path / "integration_fingerprints.json").exists()
		assert not (tmp_path / "env").exists()


def test_v2_only_state_is_read(tmp_path: Path):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	# A tracking issue created after orchestrate.yml moved to V2 has no V1 comment.
	_assert_trusted(_run_state_read(tmp_path, _v2_comments(tmp_path, "pipeline-bot", TRUSTED)))


def test_multi_chunk_v2_state_is_read(tmp_path: Path):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	comments = _v2_comments(tmp_path, "pipeline-bot", TRUSTED, chunk_size=64)
	assert len(comments) > 2
	_assert_trusted(_run_state_read(tmp_path, comments))


def test_v2_state_wins_over_the_stale_initial_v1_snapshot(tmp_path: Path):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	comments = [_v1_comment("pipeline-bot", INITIAL), *_v2_comments(tmp_path, "pipeline-bot", TRUSTED)]
	_assert_trusted(_run_state_read(tmp_path, comments))


def test_newer_v2_chain_from_another_user_is_ignored(tmp_path: Path):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	comments = [*_v2_comments(tmp_path, "pipeline-bot", TRUSTED), *_v2_comments(tmp_path, "attacker", FORGED)]
	_assert_trusted(_run_state_read(tmp_path, comments))


def test_other_users_v2_chain_does_not_hide_the_v1_fallback(tmp_path: Path):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	comments = [_v1_comment("pipeline-bot", TRUSTED), *_v2_comments(tmp_path, "attacker", FORGED)]
	_assert_trusted(_run_state_read(tmp_path, comments))


def test_missing_helper_falls_back_to_v1(tmp_path: Path):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	empty = tmp_path / "no-helper"
	empty.mkdir()
	comments = [_v1_comment("pipeline-bot", TRUSTED), *_v2_comments(tmp_path, "pipeline-bot", FORGED)]
	_assert_trusted(_run_state_read(tmp_path, comments, support_dir=empty))


def test_missing_helper_and_v2_only_state_fails_open(tmp_path: Path):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	empty = tmp_path / "no-helper"
	empty.mkdir()
	_assert_no_state(_run_state_read(tmp_path, _v2_comments(tmp_path, "pipeline-bot", TRUSTED), support_dir=empty))


def test_symlinked_helper_is_not_run(tmp_path: Path):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	linked = tmp_path / "linked"
	linked.mkdir()
	(linked / "orchestrate_state_v2.py").symlink_to(SCRIPTS / "orchestrate_state_v2.py")
	_assert_no_state(_run_state_read(tmp_path, _v2_comments(tmp_path, "pipeline-bot", TRUSTED), support_dir=linked))


def test_relative_support_dir_is_not_used(tmp_path: Path):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	# cwd is tmp_path, so a relative path would resolve to a copy the run controls.
	(tmp_path / "rel").mkdir()
	shutil.copy(SCRIPTS / "orchestrate_state_v2.py", tmp_path / "rel" / "orchestrate_state_v2.py")
	_assert_no_state(_run_state_read(tmp_path, _v2_comments(tmp_path, "pipeline-bot", TRUSTED), support_dir=Path("rel")))


@pytest.mark.parametrize("v2_switch", ["", "true", "TRUE"])
def test_switch_on_values_read_v2(tmp_path: Path, v2_switch: str):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	comments = [_v1_comment("pipeline-bot", INITIAL), *_v2_comments(tmp_path, "pipeline-bot", TRUSTED)]
	proc = _run_state_read(tmp_path, comments, v2_switch=v2_switch)
	_assert_trusted(proc)
	assert "CONFLICT_INTEGRATION_V2_STATE_ENABLED" not in proc.stdout


@pytest.mark.parametrize(("v2_switch", "warns"), [("false", False), ("False", False), ("off", True)])
def test_switch_off_reads_v1_only(tmp_path: Path, v2_switch: str, warns: bool):
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	# V1 carries the stale initial snapshot; switched off, the newer V2 state is not read.
	comments = [_v1_comment("pipeline-bot", INITIAL), *_v2_comments(tmp_path, "pipeline-bot", TRUSTED)]
	proc = _run_state_read(tmp_path, comments, v2_switch=v2_switch)
	_assert_no_state(proc)
	assert ("CONFLICT_INTEGRATION_V2_STATE_ENABLED has an unrecognised value" in proc.stdout) is warns


def test_review_autofix_forwards_the_switch_defaulting_on():
	workflow = (ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	line = "CONFLICT_INTEGRATION_V2_STATE_ENABLED: ${{ vars.CONFLICT_INTEGRATION_V2_STATE_ENABLED || 'true' }}"
	assert line in workflow
	# It must sit in the step that runs the prepare script, not a neighbour.
	start = workflow.index(line)
	end = workflow.find("\n      - name: ", start)
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/review_conflict_prepare.sh"' in workflow[start:end]
