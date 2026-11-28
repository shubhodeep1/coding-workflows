#!/usr/bin/env python3
"""Stall recovery sends an issue with an open implementation PR to that PR (#9809).

implement.yml stops at `existing_pr` while an issue has an open implementation
PR, so the `/approved` that the `auto_approve` and `retrigger_implement` arms
of ``execute_stall_recovery_action`` post does nothing (#9733).
``_stall_route_open_impl_pr`` dispatches the PR's review instead, behind
UNBLOCK_EXISTING_PR_ROUTING_ENABLED (default true).

Same extract-and-stub pattern as ``test_retrigger_inflight_direct_fallback.py``:
the helpers are pulled out of the production script with awk and run with
stubbed ``_resolve_issue_implementation_pr`` / ``_dispatch_review_for_conflicts``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"

_EXTRACT_FN = r"""
extract_fn() {
	local fn="$1"
	awk -v fn="${fn}" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__POLLER__"
}
"""

# STUB_RESOLVE_RC / STUB_PR_JSON drive the resolver; STUB_DISPATCH_RC the
# dispatch. Calls are recorded in calls.log.
_STUBS = r"""
_resolve_issue_implementation_pr() {
  echo "resolve $1" >> calls.log
  STALL_IMPL_PR_NUM=""
  STALL_IMPL_PR_JSON=""
  if [ "${STUB_RESOLVE_RC:-0}" -ne 0 ]; then
    return "${STUB_RESOLVE_RC}"
  fi
  STALL_IMPL_PR_NUM="${STUB_PR_NUM:-42}"
  STALL_IMPL_PR_JSON="${STUB_PR_JSON:-}"
  return 0
}
_dispatch_review_for_conflicts() {
  echo "dispatch $1 $2" >> calls.log
  return "${STUB_DISPATCH_RC:-0}"
}
add_healing_note() { echo "note $1" >> calls.log; }
tg_notify() { :; }
_gh_url() { printf 'https://github.com/owner/repo/%s' "$1"; }
"""


def _helpers(tmp_path: Path) -> Path:
	extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
	script = textwrap.dedent(
		"""
		set -euo pipefail
		{extractor}
		: > helpers.sh
		extract_fn '_existing_pr_routing_enabled' >> helpers.sh
		extract_fn '_stall_route_open_impl_pr' >> helpers.sh
		"""
	).format(extractor=extractor)
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	helpers = tmp_path / "helpers.sh"
	text = helpers.read_text(encoding="utf-8")
	assert "_existing_pr_routing_enabled() {" in text
	assert "_stall_route_open_impl_pr() {" in text
	return helpers


def _route(tmp_path: Path, env: dict[str, str], unset_flag: bool = False) -> tuple[subprocess.CompletedProcess, list[str]]:
	helpers = _helpers(tmp_path)
	(tmp_path / "calls.log").write_text("", encoding="utf-8")
	script = f"""
set -uo pipefail
{_STUBS}
source "{helpers}"
STALL_HEALING_CHANGED=false
rc=0
_stall_route_open_impl_pr 9733 ai:implementing auto_approve || rc=$?
echo "rc=${{rc}} healing=${{STALL_HEALING_CHANGED}}"
"""
	full_env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV", "UNBLOCK_EXISTING_PR_ROUTING_ENABLED")}
	full_env.update(env)
	if unset_flag:
		full_env.pop("UNBLOCK_EXISTING_PR_ROUTING_ENABLED", None)
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=full_env, capture_output=True, text=True, check=False)
	calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
	return result, calls


OPEN_PR = json.dumps({"number": 42, "state": "open", "head": {"ref": "ai/issue-9733"}})


def test_open_implementation_pr_dispatches_review_instead_of_approved(tmp_path: Path) -> None:
	result, calls = _route(tmp_path, {"STUB_PR_JSON": OPEN_PR}, unset_flag=True)
	assert result.returncode == 0, result.stderr
	assert "rc=0 healing=true" in result.stdout
	assert "STALL_RECOVERY issue=9733 reason=open_impl_pr pr=42 phase=ai:implementing action=dispatch_review override_from=auto_approve" in result.stdout
	assert "resolve 9733" in calls
	assert "dispatch 42 ai/issue-9733" in calls
	assert any(call.startswith("note Issue #9733:") for call in calls)


@pytest.mark.parametrize("value", ["true", "TRUE", ""])
def test_flag_on_values_route(tmp_path: Path, value: str) -> None:
	result, calls = _route(tmp_path, {"STUB_PR_JSON": OPEN_PR, "UNBLOCK_EXISTING_PR_ROUTING_ENABLED": value})
	assert "rc=0" in result.stdout
	assert "dispatch 42 ai/issue-9733" in calls


def test_flag_off_keeps_approved(tmp_path: Path) -> None:
	result, calls = _route(tmp_path, {"STUB_PR_JSON": OPEN_PR, "UNBLOCK_EXISTING_PR_ROUTING_ENABLED": "false"})
	assert "rc=1 healing=false" in result.stdout
	assert calls == []
	assert "::warning::" not in result.stdout + result.stderr


def test_invalid_flag_is_off_with_warning(tmp_path: Path) -> None:
	result, calls = _route(tmp_path, {"STUB_PR_JSON": OPEN_PR, "UNBLOCK_EXISTING_PR_ROUTING_ENABLED": "maybe"})
	assert "rc=1 healing=false" in result.stdout
	assert calls == []
	assert "::warning::UNBLOCK_EXISTING_PR_ROUTING_ENABLED is not true or false" in result.stderr


def test_no_open_implementation_pr_keeps_approved(tmp_path: Path) -> None:
	result, calls = _route(tmp_path, {"STUB_RESOLVE_RC": "1"})
	assert "rc=1 healing=false" in result.stdout
	assert not any(call.startswith("dispatch") for call in calls)
	closed = json.dumps({"number": 42, "state": "closed", "head": {"ref": "ai/issue-9733"}})
	result, calls = _route(tmp_path, {"STUB_PR_JSON": closed})
	assert "rc=1 healing=false" in result.stdout
	assert not any(call.startswith("dispatch") for call in calls)
	# A resolver success without a usable PR number is not a PR either.
	result, calls = _route(tmp_path, {"STUB_PR_JSON": OPEN_PR, "STUB_PR_NUM": "not-a-number"})
	assert "rc=1" in result.stdout
	assert not any(call.startswith("dispatch") for call in calls)


@pytest.mark.parametrize(
	("dispatch_rc", "route_rc", "reason"),
	[("2", "2", "open_impl_pr_review_active"), ("1", "3", "open_impl_pr_dispatch_failed")],
)
def test_dispatch_outcomes_skip_without_approved(tmp_path: Path, dispatch_rc: str, route_rc: str, reason: str) -> None:
	result, calls = _route(tmp_path, {"STUB_PR_JSON": OPEN_PR, "STUB_DISPATCH_RC": dispatch_rc})
	assert f"rc={route_rc} healing=false" in result.stdout
	assert f"STALL_SKIP issue=9733 reason={reason} pr=42 phase=ai:implementing action=auto_approve" in result.stdout
	assert "dispatch 42 ai/issue-9733" in calls


def _arm(text: str, name: str) -> str:
	start = text.index("execute_stall_recovery_action() {")
	arm_start = text.index(f"\n    {name})\n", start)
	arm_end = text.index("\n      ;;\n", arm_start)
	return text[arm_start:arm_end]


@pytest.mark.parametrize("arm", ["auto_approve", "retrigger_implement"])
def test_stall_arms_route_before_posting_approved(arm: str) -> None:
	text = POLLER_SCRIPT.read_text(encoding="utf-8")
	body = _arm(text, arm)
	route = body.index('_stall_route_open_impl_pr "${issue_num}" "${phase}" "${action}"')
	# The /approved comment heredoc, not the comments that mention it.
	assert route < body.index("cat <<'STALL_EOF'\n/approved")
	# After the no-op cap and after every existing hold (the staged-support
	# latch skip and the plan gate), so an open PR never bypasses them.
	assert body.index("MAX_IMPL_NOOP_REISSUES") < route
	assert body.index("_plan_auto_approval_gate") < route
	if arm == "auto_approve":
		assert body.index("staged_support_latch_release_incomplete") < route
	assert re.search(r'0\)\n\s+STALL_RECOVERY_SHOULD_INCREMENT="true"\n\s+STALL_RECOVERY_EFFECTIVE_ACTION="dispatch_review"\n\s+return 0', body[route:])
	assert "2|3) return 1 ;;" in body[route:]


def _standalone_arm(text: str, name: str) -> str:
	start = text.index("run_standalone_stall_recovery() {")
	arm_start = text.index(f"\n      {name})\n", start)
	arm_end = text.index("\n        ;;\n", arm_start)
	return text[arm_start:arm_end]


@pytest.mark.parametrize(("arm", "rc_var"), [("auto_approve", "_std_auto_approve_route_rc"), ("retrigger_implement", "_std_retrigger_implement_route_rc")])
def test_standalone_stall_arms_route_before_posting_approved(arm: str, rc_var: str) -> None:
	text = POLLER_SCRIPT.read_text(encoding="utf-8")
	body = _standalone_arm(text, arm)
	route = body.index(f'_stall_route_open_impl_pr "${{issue_num}}" "${{phase}}" "${{action}}" || {rc_var}=$?')
	# After the plan gate (its heal hold, stale-close check and protected-path
	# hold run first), before the /approved comment heredoc.
	assert body.index("_plan_auto_approval_gate") < route
	assert route < body.index("cat <<'STALL_EOF'\n/approved")
	routed = body[route:]
	assert re.search(rf'if \[ "\$\{{{rc_var}\}}" -eq 0 \]; then\n\s+STALL_RECOVERY_SHOULD_INCREMENT="true"\n\s+STALL_RECOVERY_EFFECTIVE_ACTION="dispatch_review"\n\s+took_action="true"', routed)
	# Only rc 1 (flag off, no open PR, inconclusive lookup) reaches /approved.
	if arm == "auto_approve":
		# Reset per issue so a held gate never inherits an earlier issue's rc.
		assert body.index(f'local {rc_var}="gate_held"') < body.index("_plan_auto_approval_gate")
		assert routed.index(f'if [ "${{{rc_var}}}" = "1" ]; then') < routed.index("cat <<'STALL_EOF'\n/approved")
	else:
		# The route runs only when the gate passed.
		assert body.index('if [ "${_std_rti_gate_ok}" = "true" ]; then') < route
		assert f'if [ "${{{rc_var}}}" -ne 1 ]; then\n            _std_rti_gate_ok="false"' in routed
		assert routed.index('_std_rti_gate_ok="false"') < routed.index('if [ "${_std_rti_gate_ok}" = "true" ]; then')


def test_poller_defaults_flag_on_and_workflow_forwards_it() -> None:
	text = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert 'UNBLOCK_EXISTING_PR_ROUTING_ENABLED="${UNBLOCK_EXISTING_PR_ROUTING_ENABLED:-true}"' in text
	workflow = (REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
	line = "UNBLOCK_EXISTING_PR_ROUTING_ENABLED: ${{ vars.UNBLOCK_EXISTING_PR_ROUTING_ENABLED || 'true' }}"
	assert workflow.count(line) == workflow.count("ENABLE_STALL_MERGED_PR_GUARD: ${{") == 2
