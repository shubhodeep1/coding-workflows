"""Stall recovery keeps ``ai:check-triage`` when it re-issues a triage issue.

btc_sweeper #209 (``ai:check-triage``) stalled and was re-issued as #223
without the label. The copied body still carried the check-failure-triage
markers, so ``scripts/resolve_integration_ref.sh`` refused #223 as
``label_missing`` on every clarify and plan run. Both ``close_and_reissue``
cases in ``scripts/orchestrate_poll_process.sh`` (the managed one in
``execute_stall_recovery_action``, which the stall judge also reaches for
standalone issues, and the standalone one in ``run_standalone_stall_recovery``)
must carry the label over.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER_TEXT = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")

TRIAGE_BODY = (
	"<!-- check-failure-triage:fp=" + "a" * 64 + " -->\n"
	"<!-- check-failure-triage:gen=1 -->\n"
	"<!-- check-failure-triage:root=" + "a" * 64 + " -->\n"
	"<!-- check-failure-triage:pr=32 -->\n\n"
	"## Automated CI failure triage\n"
)


def _case_body(mode: str) -> str:
	if mode == "managed":
		start = POLLER_TEXT.index("    close_and_reissue)\n      # Belt-and-braces ancestor-chain")
		stop = POLLER_TEXT.index("    skip)\n", start)
	else:
		start = POLLER_TEXT.index("      close_and_reissue)\n        # Belt-and-braces ancestor-chain")
		stop = POLLER_TEXT.index("      skip)\n", start)
	return POLLER_TEXT[start:stop]


def _run(tmp_path: Path, mode: str, source_labels: list[str], labels_json: str = "[]") -> list[str]:
	issue_json = json.dumps({
		"title": "CI failure: check on PR #32",
		"body": TRIAGE_BODY,
		"heal": "ai:workflow-heal" in source_labels,
		"managed": "ai:orchestrator-managed" in source_labels,
		"check_triage": "ai:check-triage" in source_labels,
	})
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
gh_retry() {
  if [ "$1" = _safe_gh_jq ]; then
    printf '%s\\n' "$ORIG_ISSUE_JSON"
  elif [ "$1" = gh ] && [ "$2" = issue ] && [ "$3" = create ]; then
    printf '%s\\n' "$@" > "$CAPTURED_CREATE_ARGS_FILE"
    printf 'https://github.com/o/r/issues/99\\n'
  fi
}
reissue_case() {
  local issue_num=7 phase=ai:ready-to-merge recovery_count=2 local_id='' stall_minutes=78
  local elapsed_minutes=78 labels_json="$SOURCE_LABELS_JSON" updated_state='{}' state_comment_id=''
  local STALL_REISSUE_EXTRA_GUIDANCE_ISSUE='' took_action=''
  local MAX_IMPL_NOOP_REISSUES=2 GITHUB_REPOSITORY=o/r WAVE_IDX=0
  for REISSUE_TEST_PASS in 1; do
    case close_and_reissue in
""" + _case_body(mode) + """
    esac
  done
}
STALL_RECOVERY_SHOULD_INCREMENT=''
reissue_case
"""
	args_file = tmp_path / "create-args.txt"
	result = subprocess.run(
		["bash", "-c", script], capture_output=True, text=True, check=False,
		env={**os.environ, "ORIG_ISSUE_JSON": issue_json, "SOURCE_LABELS_JSON": labels_json,
			"CAPTURED_CREATE_ARGS_FILE": str(args_file)},
	)
	assert result.returncode == 0, result.stderr + result.stdout
	if not args_file.exists():
		return []
	return args_file.read_text(encoding="utf-8").splitlines()


def _labels(args: list[str]) -> list[str]:
	return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--label"]


needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required for issue JSON parsing")


@needs_jq
@pytest.mark.parametrize("mode", ["managed", "standalone"])
def test_reissue_of_a_check_triage_issue_keeps_the_label(tmp_path: Path, mode: str) -> None:
	# A managed source issue carries ai:orchestrator-managed; a standalone one does not.
	source = ["ai:check-triage", "ai:orchestrator-managed"] if mode == "managed" else ["ai:check-triage"]
	args = _run(tmp_path, mode, source)
	labels = _labels(args)
	assert labels.count("ai:check-triage") == 1, args
	assert "ai:clarification" in labels
	assert ("ai:orchestrator-managed" in labels) == (mode == "managed")


@needs_jq
@pytest.mark.parametrize("mode", ["managed", "standalone"])
def test_reissue_of_an_ordinary_issue_gets_no_check_triage_label(tmp_path: Path, mode: str) -> None:
	args = _run(tmp_path, mode, [])
	assert args, "the re-issue should still be created"
	assert "ai:check-triage" not in _labels(args)


@needs_jq
def test_standalone_reissue_falls_back_to_the_cycle_labels(tmp_path: Path) -> None:
	# The source read can come back without the label flag (older payload);
	# the standalone loop's cached labels still carry it over.
	args = _run(tmp_path, "standalone", [], labels_json='["ai:check-triage","ai:ready-to-merge"]')
	assert _labels(args).count("ai:check-triage") == 1, args


def test_both_reissue_reads_request_the_check_triage_flag() -> None:
	for mode in ("managed", "standalone"):
		body = _case_body(mode)
		assert 'check_triage: ([.labels[]?.name] | index("ai:check-triage") != null)' in body, mode
		assert '${_check_triage_reissue_label_args[@]+"${_check_triage_reissue_label_args[@]}"}' in body, mode
