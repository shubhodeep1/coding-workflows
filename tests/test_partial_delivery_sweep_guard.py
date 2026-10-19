#!/usr/bin/env python3
"""The poller's close sweep leaves a partially delivered issue open (#7657).

`_pr_json_is_partial_delivery` (scripts/orchestrate_poll_process.sh) is read
from the shipped script and run against PR JSON shapes: it returns 0 only for
a bound partial-delivery PR, 1 otherwise and 2 when the pipeline login cannot
be read. `close_merged_issues_sweep` must skip the close and the ai:merged
backfill for such a PR.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _function(name: str) -> str:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index(f"\n{name}() {{\n") + 1
	end = text.index("\n}\n", start) + 3
	return text[start:end]


def _pr(body: str, *, head_repo: str = "o/r", head_ref: str = "ai/issue-7", login: str = "Pipeline-Bot") -> str:
	return json.dumps({"body": body, "head": {"ref": head_ref, "repo": {"full_name": head_repo}}, "user": {"login": login}})


MARKER = "<!-- ai:partial-delivery:v1 issue=7 -->"
BODY = f"Automated implementation. Refs #7\n\n{MARKER}\n### Deferred scope (not delivered by this PR)\n\n- rest\n"


def _rc(pr_json: str, *, login: str = "pipeline-bot", switch: str | None = None, repo: str = "o/r") -> int:
	assert shutil.which("jq") is not None, "jq is required for the sweep guard test"
	script = (
		"set -euo pipefail\n"
		f"unblock_trusted_login() {{ printf '%s' {json.dumps(login)}; }}\n"
		+ _function("_pr_json_is_partial_delivery")
		+ '\nrc=0; _pr_json_is_partial_delivery 7 "${PR_JSON}" || rc=$?; echo "rc=${rc}"\n'
	)
	env = {"PATH": os.environ["PATH"], "PR_JSON": pr_json, "GITHUB_REPOSITORY": repo}
	if switch is not None:
		env["PARTIAL_DELIVERY_TRACKING_ENABLED"] = switch
	result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True)
	return int(result.stdout.strip().rsplit("rc=", 1)[1])


def test_bound_partial_pr_is_detected() -> None:
	assert _rc(_pr(BODY)) == 0
	assert _rc(_pr(BODY.replace("\n", "\r\n"))) == 0


def test_unbound_or_unmarked_prs_are_not_partial() -> None:
	assert _rc(_pr("Automated implementation. Closes https://x/issues/7\n")) == 1
	assert _rc(_pr(BODY, head_repo="fork/r")) == 1
	assert _rc(_pr(BODY, head_ref="ai/issue-8")) == 1
	assert _rc(_pr(BODY, login="maintainer")) == 1
	assert _rc(_pr(BODY.replace("issue=7", "issue=8"))) == 1
	assert _rc(_pr(BODY + MARKER + "\n")) == 1
	assert _rc(_pr(BODY), switch="false") == 1


def test_unavailable_login_defers() -> None:
	assert _rc(_pr(BODY), login="") == 2


def test_missing_repository_defers_only_a_marked_pr() -> None:
	assert _rc(_pr(BODY), repo="") == 2
	assert _rc(_pr("Automated implementation. Closes https://x/issues/7\n"), repo="") == 1


def test_sweep_skips_close_and_backfill_for_a_partial_delivery() -> None:
	text = POLLER.read_text(encoding="utf-8")
	sweep = text[text.index("\nclose_merged_issues_sweep() {"):]
	sweep = sweep[: sweep.index("\n}\n")]
	guard = sweep.index('_pr_json_is_partial_delivery "${issue_num}" "${_sweep_candidate_pr_json}"')
	assert sweep.index("_pr_json_base_is_target_merge") < guard < sweep.index("accepted reason=${_TARGET_MERGE_REASON}")
	skip = sweep.index('if [ -n "${sweep_partial_delivery_reason}" ]; then')
	assert skip < sweep.index('--add-label "ai:merged"')
	assert "outcome=skip reason=partial_delivery" in sweep
	# A partial candidate does not stop the loop: a later full-delivery PR
	# for the same issue still closes it and clears the partial reason.
	partial_log = sweep.index("outcome=skip reason=partial_delivery")
	assert sweep[partial_log:].split("\n", 3)[2].strip() == "continue"
	accepted = sweep.index('merged_pr_num="${_sweep_candidate_pr}"')
	assert 'sweep_partial_delivery_reason=""' in sweep[accepted:accepted + 200]
	assert 'if [ "${_sweep_is_managed}" != "true" ]; then' in sweep[: guard]
	poll = (REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
	assert "PARTIAL_DELIVERY_TRACKING_ENABLED: ${{ vars.PARTIAL_DELIVERY_TRACKING_ENABLED || 'true' }}" in poll


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"OK: {len(tests)} partial-delivery sweep guard tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
