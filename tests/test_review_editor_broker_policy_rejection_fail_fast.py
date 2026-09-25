"""Contract test: the review editor retry loop fails fast on broker policy rejections.

PR #4077 review runs 34663517732 and 34654303940 (2026-09-11/12): the model
provider broker answered HTTP 429 (`broker_rejection`) on the editor's fifth
model call. Because one broker instance serves the whole retry loop, attempts
2 and 3 — including the MODEL_EDITOR_FALLBACK switch, whose log line blamed
"capacity-limited" provider saturation — were rejected on their very first
request. Each round burned ~18 minutes of runner time for a decision the
broker had already made deterministically.

`scripts/review_apply_fixes.sh` now snapshots
`model_provider_broker_policy_rejection_count` before every attempt and,
when a failed attempt recorded new 4xx rejections, logs
`EDITOR_BROKER_POLICY_REJECTION ...`, emits `broker_policy_rejection` before
the final attempt or preserves `attempt_failed` on the final attempt, and
breaks out of the loop. The partial-finalize fallback summary is unchanged
(still `recoverable_failure`), so the workflow-side sentinels stay in lockstep.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
CODEX_HELPERS = REPO_ROOT / "scripts" / "codex_helpers.sh"
BROKER = REPO_ROOT / "scripts" / "model_provider_broker.py"


def _text(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _editor_loop(text: str) -> str:
	start = text.index('while [ "${attempt}" -le "${editor_max_attempts}" ]; do')
	end = text.index("# If PR was closed/merged during editor execution, exit cleanly", start)
	return text[start:end]


def test_broker_exposes_rejections_file_flag() -> None:
	broker = _text(BROKER)
	assert 'parser.add_argument(\n\t\t"--rejections-file",' in broker
	assert "def record_rejection(self, status: int, message: str, path: str) -> None:" in broker
	assert 'rejection_path = self.path.split("?", 1)[0].split("#", 1)[0]' in broker
	assert 'self.server.record_rejection(status, message, rejection_path)' in broker
	assert 'MODEL_PROVIDER_BROKER_REJECT status={status} path={path} message={json.dumps(message)}' in broker
	assert "MAX_REJECTIONS_PER_STATUS_CLASS = 100" in broker
	assert "self.rejections_recorded_by_status_class = {4: 0, 5: 0}" in broker
	assert broker.index("self.server.record_rejection(status, message, rejection_path)") < broker.index("self.wfile.write(payload)")
	# The record is appended 0600 and never includes tokens.
	assert "os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600" in broker
	assert '"status": int(status), "path": path, "message": message' in broker


def test_codex_helpers_pass_rejections_file_and_expose_count_helpers() -> None:
	helpers = _text(CODEX_HELPERS)
	assert 'rejections_file="${MODEL_PROVIDER_BROKER_REJECTIONS_FILE:-${broker_runtime_dir}/model-provider-broker-rejections.jsonl}"' in helpers
	assert '--rejections-file "${rejections_file}" \\' in helpers
	assert "export MODEL_PROVIDER_BROKER_PID_FILE MODEL_PROVIDER_BROKER_READY_FILE MODEL_PROVIDER_BROKER_AGENT_HOME MODEL_PROVIDER_BROKER_REJECTIONS_FILE" in helpers
	assert "model_provider_broker_policy_rejection_count()" in helpers
	assert "model_provider_broker_last_policy_rejection()" in helpers
	# Only deterministic 4xx policy rejections count; relayed 502s do not.
	assert """grep -cE '"status":[[:space:]]*4[0-9]{2}([^0-9]|$)' -- "${rejections_file}\"""" in helpers
	# stop() cleans the file and the env var like the ready/pid files: both the
	# rm -f and the unset are unconditional (unlike the deferred-access-restore
	# vars below), so MODEL_PROVIDER_BROKER_REJECTIONS_FILE rides in the same
	# unconditional unset line as PID_FILE/READY_FILE rather than alongside
	# ACL_CAPTURED (which is only unset when access restoration is not deferred).
	assert '[ -z "${MODEL_PROVIDER_BROKER_REJECTIONS_FILE:-}" ] || rm -f -- "${MODEL_PROVIDER_BROKER_REJECTIONS_FILE}"' in helpers
	assert "unset MODEL_PROVIDER_BROKER_BASE_URL MODEL_PROVIDER_BROKER_TOKEN MODEL_PROVIDER_BROKER_PID_FILE MODEL_PROVIDER_BROKER_READY_FILE MODEL_PROVIDER_BROKER_REJECTIONS_FILE" in helpers


def test_editor_loop_snapshots_rejections_before_each_attempt() -> None:
	loop = _editor_loop(_text(REVIEW_APPLY_FIXES))
	snapshot = loop.index('attempt_broker_rejections_before="$(model_provider_broker_policy_rejection_count 2>/dev/null || echo 0)"')
	tmp_files = loop.index('tmp_output="$(mktemp)"')
	assert snapshot < tmp_files, "the snapshot must be taken before the attempt starts"
	# Guarded so an older codex_helpers.sh (no helper) keeps the legacy behaviour.
	assert 'if command -v model_provider_broker_policy_rejection_count >/dev/null 2>&1; then' in loop
	assert 'attempt_broker_rejections_before="0"' in loop


def test_editor_loop_breaks_on_new_policy_rejections_after_a_failed_attempt() -> None:
	text = _text(REVIEW_APPLY_FIXES)
	loop = _editor_loop(text)
	block_start = loop.index("# ── Broker policy-rejection short-circuit ──")
	refusal_start = loop.index("# ── Safety-policy refusal short-circuit ──")
	assert block_start < refusal_start, "broker rejections are checked before the refusal short-circuit"
	block = loop[block_start:refusal_start]
	assert '[ "${cmd_rc}" -ne 0 ] \\' in block
	assert '[ "${attempt_broker_rejections_after}" -gt "${attempt_broker_rejections_before}" ] 2>/dev/null; then' in block
	assert "EDITOR_BROKER_POLICY_REJECTION attempt=${attempt} model=${EDITOR_ATTEMPT_MODEL} rc=${cmd_rc} new_rejections=" in block
	assert re.search(
		r'if \[ "\$\{attempt\}" -lt "\$\{editor_max_attempts\}" \]; then\n'
		r'\s+opencode_emit_failure_alert review_apply_fixes writer "\$\{EDITOR_ATTEMPT_MODEL\}" "\$\{cmd_rc\}" broker_policy_rejection \|\| true\n'
		r'\s+else\n'
		r'\s+opencode_emit_failure_alert review_apply_fixes writer "\$\{EDITOR_ATTEMPT_MODEL\}" "\$\{cmd_rc\}" attempt_failed \|\| true\n'
		r'\s+fi\n',
		block,
	)
	assert re.search(r'rm -f "\$\{tmp_output\}" "\$\{tmp_err\}" "\$\{attempt_prompt_file_cleanup_path\}"\n\s+break\n', block)
	# The loop never sets a new partial-finalize reason, so the fallback summary
	# and the workflow's `recoverable_failure` sentinel stay in lockstep.
	assert "editor_partial_finalize_reason" not in block
	assert 'editor_partial_finalize_reason="recoverable_failure"' in text


def test_successful_attempts_ignore_rejection_noise() -> None:
	"""A successful attempt (rc=0) that happened to see a rejection keeps its
	output: the check is gated on cmd_rc != 0 so a rejected side request
	cannot discard a valid editor summary."""
	loop = _editor_loop(_text(REVIEW_APPLY_FIXES))
	block = loop[loop.index("# ── Broker policy-rejection short-circuit ──"):loop.index("# ── Safety-policy refusal short-circuit ──")]
	condition = block[block.index("if [ \"${cmd_rc}\" -ne 0 ]"):block.index("; then", block.index("if [ \"${cmd_rc}\" -ne 0 ]"))]
	assert "-ne 0" in condition and "-gt" in condition
