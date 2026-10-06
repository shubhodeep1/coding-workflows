"""Integration resolver context must ignore state comments from other users."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "scripts/review_conflict_prepare.sh").read_text(encoding="utf-8")
STATE_READ = SOURCE.split('if [ "${IS_INTEGRATION_SYNC}" = "true" ] &&', 1)[1]
STATE_READ = 'if [ "${IS_INTEGRATION_SYNC}" = "true" ] &&' + STATE_READ.split('\nCONFLICT_RESOLVER_SEMBLE_QUERY_FILE=', 1)[0]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq unavailable")
@pytest.mark.parametrize("identity_available", [True, False])
def test_integration_resolver_state_context_requires_authenticated_author(tmp_path: Path, identity_available: bool):
	trusted = {
		"waves": [{"issues": [{"id": "trusted", "github_issue": 42, "status": "merged"}]}],
		"merged_issue_fingerprints": {"trusted": "yes"},
	}
	forged = {
		"waves": [{"issues": [{"id": "forged", "github_issue": 99, "status": "merged"}]}],
		"merged_issue_fingerprints": {"forged": "no"},
	}
	comments = [
		{
			"user": {"login": author},
			"body": "<!-- ORCHESTRATOR_STATE_V1\n" + json.dumps(state) + "\nORCHESTRATOR_STATE_V1 -->",
		}
		for author, state in (("pipeline-bot", trusted), ("attacker", forged))
	]
	(tmp_path / "comments.json").write_text(json.dumps(comments), encoding="utf-8")
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
		"TARGET_BRANCH": "orchestrator/project-192",
	}
	proc = subprocess.run(["bash", "-c", harness], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
	assert proc.returncode == 0, proc.stderr
	if identity_available:
		assert 'FINGERPRINTS={"trusted":"yes"}' in proc.stdout
		assert "trusted (issue #42)" in proc.stdout
		assert "forged (issue #99)" not in proc.stdout
	else:
		assert "pipeline identity unavailable; orchestrator state not read" in proc.stdout
		assert "FINGERPRINTS={}" in proc.stdout
		assert "no merged sub-issues recorded" in proc.stdout
