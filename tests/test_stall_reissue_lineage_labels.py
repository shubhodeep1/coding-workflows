"""Stall-recovery close_and_reissue keeps a replacement routable.

Regression for shubhodeep1/btc_sweeper#223 (re-issued from #209): standalone
stall recovery's ``run_stall_judge`` step calls ``invoke_stall_judge`` with an
empty ``local_id``, and a ``close_and_reissue`` verdict runs the managed arm of
``execute_stall_recovery_action``. That arm always added
``ai:orchestrator-managed`` and never carried ``ai:check-triage``. The
replacement of a standalone check-failure-triage issue therefore had the
triage header, the managed label, no triage label and no tracking line, and
``scripts/resolve_integration_ref.sh`` refused it with ``label_missing`` on
every clarify run (also coding-workflows#7511 and #7515).

Each test runs the real case block from ``scripts/orchestrate_poll_process.sh``
with stubbed ``gh``, captures the labels of the replacement issue, and feeds
those labels plus the replacement body to the resolver's own CI-triage parser.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER_TEXT = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
RESOLVER_TEXT = (REPO_ROOT / "scripts" / "resolve_integration_ref.sh").read_text(encoding="utf-8")

TRIAGE_BODY = """<!-- check-failure-triage:fp={fp} -->
<!-- check-failure-triage:gen=1 -->
<!-- check-failure-triage:root={fp} -->
<!-- check-failure-triage:pr=32 -->

## Automated CI failure triage (generation 1 of max 3)

- **Repository:** `o/r`
- **Pull request:** https://github.com/o/r/pull/32 (`codex/print-address-count-before-utxo-check`)
- **Failing check:** `Codex PR Self-Healing Semantic Agent` (conclusion: `failure`)
""".format(fp="6" * 64)

PLAIN_BODY = "Delete the legacy wrapper.\n"
SECURITY_FIX_BODY = "Fix the finding.\n\n- Tracking issue: #40\n- Local ID: `security-pass-fix-cycle-1`\n"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required for issue JSON parsing")


def _case_body(recovery_mode: str) -> str:
	if recovery_mode == "managed":
		start = POLLER_TEXT.index("    close_and_reissue)\n      # Belt-and-braces ancestor-chain")
		stop = POLLER_TEXT.index("    skip)\n", start)
	else:
		start = POLLER_TEXT.index("      close_and_reissue)\n        # Belt-and-braces ancestor-chain")
		stop = POLLER_TEXT.index("      skip)\n", start)
	return POLLER_TEXT[start:stop]


def _reissue(tmp_path: Path, recovery_mode: str, labels: list[str], body: str, local_id: str = "") -> tuple[list[str], str]:
	"""Run one close_and_reissue block; return (created labels, created body)."""
	issue_json = json.dumps({"number": 209, "labels": [{"name": name} for name in labels], "title": "CI failure", "body": body})
	script = """set -euo pipefail
count_noop_ancestors() { printf '0\\n'; }
ensure_label_exists() { :; }
close_linked_pr() { :; }
surface_reissue_closed_without_pr() { :; }
tg_notify() { :; }
tg_notify_issue() { :; }
_gh_url() { :; }
engine_label_create_args() { :; }
write_standalone_state_json() { :; }
_stall_retired_conflict_reissue_body() { printf '%s' "$1"; }
gh_retry() {
  if [ "$1" = _safe_gh_jq ]; then
    # Apply the call's own --jq projection to the stored issue payload.
    printf '%s' "$ORIG_ISSUE_JSON" | jq -c "$4"
  elif [ "$1" = gh ] && [ "$2" = issue ] && [ "$3" = create ]; then
    shift 3
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --label) printf '%s\\n' "$2" >> "$CAPTURED_LABELS_FILE"; shift 2 ;;
        --body) printf '%s' "$2" > "$CAPTURED_BODY_FILE"; shift 2 ;;
        *) shift ;;
      esac
    done
    printf 'https://github.com/o/r/issues/223\\n'
  fi
}
reissue_case() {
  local issue_num=209 phase=ai:ready-to-merge recovery_count=2 local_id="$TEST_LOCAL_ID" stall_minutes=78
  local elapsed_minutes=78 labels_json="$SOURCE_LABELS_JSON" updated_state='{}' state_comment_id=''
  local STALL_REISSUE_EXTRA_GUIDANCE_ISSUE='' STALL_REISSUE_EXTRA_GUIDANCE=''
  local STALL_RETIRED_CONFLICT_CLOSE_MESSAGE='' STALL_RETIRED_CONFLICT_BASE=''
  local took_action='' STATE_FILE="$TEST_STATE_FILE"
  local MAX_IMPL_NOOP_REISSUES=2 GITHUB_REPOSITORY=o/r WAVE_IDX=0
  for REISSUE_TEST_PASS in 1; do
    case close_and_reissue in
""" + _case_body(recovery_mode) + """
    esac
  done
}
STALL_RECOVERY_SHOULD_INCREMENT=''
reissue_case
"""
	labels_file = tmp_path / "labels.txt"
	body_file = tmp_path / "body.txt"
	state_file = tmp_path / "state.json"
	state_file.write_text(json.dumps({"issue_number_map": {}, "waves": [{"issues": [{"id": local_id or "x"}]}]}), encoding="utf-8")
	result = subprocess.run(
		["bash", "-c", script], capture_output=True, text=True, check=False,
		env={**os.environ, "ORIG_ISSUE_JSON": issue_json, "SOURCE_LABELS_JSON": json.dumps(labels),
			"TEST_LOCAL_ID": local_id, "TEST_STATE_FILE": str(state_file),
			"CAPTURED_LABELS_FILE": str(labels_file), "CAPTURED_BODY_FILE": str(body_file)},
	)
	assert result.returncode == 0, result.stderr + result.stdout
	assert body_file.exists(), "the replacement issue was not created:\n" + result.stdout + result.stderr
	return labels_file.read_text(encoding="utf-8").split(), body_file.read_text(encoding="utf-8")


def _resolver_verdict(labels: list[str], body: str) -> str:
	match = re.search(r"^CI_TRIAGE_PARSE_PY='\n(.*?)\n'$", RESOLVER_TEXT, re.S | re.M)
	assert match, "could not locate CI_TRIAGE_PARSE_PY in resolve_integration_ref.sh"
	issue = {"labels": [{"name": name} for name in labels], "body": body, "author_association": "OWNER", "pull_request": None}
	result = subprocess.run(
		["python3", "-I", "-c", match.group(1), "o/r"], input=json.dumps(issue),
		capture_output=True, text=True, check=True,
	)
	return result.stdout.strip()


@pytest.mark.parametrize("recovery_mode", ["managed", "standalone"])
def test_standalone_triage_reissue_keeps_check_triage_and_routes(tmp_path: Path, recovery_mode: str) -> None:
	labels, body = _reissue(tmp_path, recovery_mode, ["ai:check-triage", "ai:ready-to-merge"], TRIAGE_BODY)
	assert "ai:check-triage" in labels
	assert "ai:orchestrator-managed" not in labels
	assert "ai:clarification" in labels
	assert body.startswith(TRIAGE_BODY) and "Re-issued from #209" in body
	# The btc_sweeper#223 shape was refused here with label_missing.
	assert _resolver_verdict(labels, body) == "verify 32 codex/print-address-count-before-utxo-check"


def test_managed_arm_no_longer_marks_a_standalone_issue_managed(tmp_path: Path) -> None:
	labels, body = _reissue(tmp_path, "managed", ["ai:clarification"], PLAIN_BODY)
	assert labels == ["ai:clarification"]
	assert _resolver_verdict(labels, body) == "skip"


def test_managed_arm_keeps_managed_label_for_a_wave_item(tmp_path: Path) -> None:
	labels, _body = _reissue(tmp_path, "managed", ["ai:clarification"], PLAIN_BODY, local_id="item-3")
	assert "ai:orchestrator-managed" in labels


def test_managed_arm_keeps_managed_label_for_a_non_wave_managed_issue(tmp_path: Path) -> None:
	# A security-pass fix issue has no local_id but is managed; its successor
	# lookup lists open ai:orchestrator-managed issues.
	labels, body = _reissue(tmp_path, "managed", ["ai:orchestrator-managed", "ai:implementing"], SECURITY_FIX_BODY)
	assert "ai:orchestrator-managed" in labels
	assert "- Tracking issue: #40" in body
