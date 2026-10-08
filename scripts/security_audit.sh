#!/usr/bin/env bash
# security_audit.sh — Run the default-branch OWASP Top 10 + STRIDE audit.

set -euo pipefail

security_audit_require_cmd() {
	local cmd_name="${1:?command name required}"
	command -v "${cmd_name}" >/dev/null 2>&1 || {
		echo "${cmd_name} is required but not installed" >&2
		exit 1
	}
}

security_audit_flag_enabled() {
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

security_audit_sanitize_log_value() {
	local sanitized_value
	sanitized_value="$(
		printf '%s' "${1:-}" \
			| tr '\n\r\t' '   ' \
			| LC_ALL=C tr -cd '[:print:]' \
			| tr -s ' ' \
			| sed -E 's#([[:alpha:]][[:alnum:]+.-]*://)[^/@[:space:]]+@#\1[redacted]@#g; s#([Aa]uthorization:[[:space:]]*)(([Bb]earer|[Bb]asic|[Tt]oken)[[:space:]]+)?[^[:space:]]+#\1[redacted]#g; s#([Bb]earer[[:space:]]+)[^[:space:]]+#\1[redacted]#g; s#(github_pat_|gh[pousr]_)[[:alnum:]_]+#\1[redacted]#g; s#(sk-or-)[[:alnum:]_-]+#\1[redacted]#g; s/^ //;s/ $//'
	)"
	if [ -z "${sanitized_value}" ]; then
		sanitized_value="(empty)"
	fi
	printf '%q' "${sanitized_value}"
}

security_audit_emit_failure() {
	local failure_phase="${1:?failure phase required}"
	local failure_path="${2:?failure path required}"
	local failure_reason="${3:?failure reason required}"
	local failure_provider="${4:-}"
	local failure_cwd
	failure_cwd="$(pwd -P 2>/dev/null || printf '%s' '.')"
	printf 'security-audit: phase=%s cwd=%s path=%s error=%s%s\n' \
		"$(security_audit_sanitize_log_value "${failure_phase}")" \
		"$(security_audit_sanitize_log_value "${failure_cwd}")" \
		"$(security_audit_sanitize_log_value "${failure_path}")" \
		"$(security_audit_sanitize_log_value "${failure_reason}")" \
		"${failure_provider:+ provider=${failure_provider}}" >&2
}

security_audit_emit_path_diagnostic() {
	local diagnostic_file="${1:?diagnostic file required}"
	local path_diagnostic
	if [ "${2:-}" = "sanitized-tail" ]; then
		# The Codex path receives only already-redacted, published lines.
		path_diagnostic="$(LC_ALL=C grep -E '(No\\ such\\ file\\ or\\ directory|os\\ error\\ 2|ENOENT)' "${diagnostic_file}" 2>/dev/null | tail -n 20 || true)"
		if [ -n "${path_diagnostic}" ]; then
			printf 'security-audit: captured_path_error=%s\n' "${path_diagnostic}" >&2
		fi
		return 0
	fi
	path_diagnostic="$(LC_ALL=C grep -E '(No such file or directory|os error 2|ENOENT)' "${diagnostic_file}" 2>/dev/null | tail -n 20 || true)"
	if [ -n "${path_diagnostic}" ]; then
		printf 'security-audit: captured_path_error=%s\n' \
			"$(security_audit_sanitize_log_value "${path_diagnostic}")" >&2
	fi
}

security_audit_emit_codex_stderr_tail() {
	local stderr_path="${1:?stderr path required}"
	local prompt_path="${2:?prompt path required}"
	local tail_path="${3:?tail path required}"
	local masked_path="${tail_path}.masked"
	local rendered_path="${tail_path}.rendered"
	local stderr_line rendered_line
	: > "${tail_path}"
	# Read only the final 64 KiB plus one byte; discard a cut first line rather
	# than publishing a fragment of a prompt or credential. Filter prompt/config
	# echoes and mask secrets before calling the shared log sanitizer.
	if ! python3 - "${stderr_path}" "${prompt_path}" > "${masked_path}" 2>/dev/null <<'PY'
import os
import re
import sys
from pathlib import Path

stderr_path = Path(sys.argv[1])
prompt_path = Path(sys.argv[2])
with stderr_path.open("rb") as stderr_file:
	stderr_file.seek(0, 2)
	length = stderr_file.tell()
	stderr_file.seek(max(0, length - 65537))
	content = stderr_file.read()
if length > 65537:
	content = content.partition(b"\n")[2]

prompt_lines = set()
with prompt_path.open(encoding="utf-8", errors="replace") as prompt_file:
	for prompt_line in prompt_file:
		if prompt_line.strip():
			prompt_lines.add(prompt_line.strip())

secret_values = []
for name, value in os.environ.items():
	if value and re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|(?:^|_)PAT(?:_|$))", name, re.I):
		secret_values.extend(part for part in value.splitlines() if part)

lines = []
for raw_line in content.decode("utf-8", errors="replace").splitlines():
	line = raw_line.strip()
	if not line or line in prompt_lines or any(
		len(prompt_line) >= 24 and prompt_line in line for prompt_line in prompt_lines
	):
		continue
	if re.search(r"config\.toml|model_verbosity|OPENROUTER_API_KEY|=== (?:BEGIN|END) UNTRUSTED|(?:^|\s)(?:model|api_?key|base_url|model_provider)\s*=", line, re.I):
		continue
	line = re.sub(r"(?i)(?:sk-[a-z0-9_-]{3,}|(?:gh[pousr]_|github_pat_)[a-z0-9_]+)", "[redacted]", line)
	line = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", line)
	line = re.sub(r"\b[0-9a-fA-F]{32,}\b|\b[A-Za-z0-9_+/=-]{40,}\b", "[redacted]", line)
	for value in sorted(set(secret_values), key=len, reverse=True):
		if len(value) >= 8:
			line = line.replace(value, "[redacted]")
		else:
			line = re.sub(r"(?<![\w])" + re.escape(value) + r"(?![\w])", "[redacted]", line)
	if line.strip():
		lines.append(line)

for line in lines[-40:]:
	print(line)
PY
	then
		: > "${masked_path}"
	fi
	: > "${rendered_path}"
	while IFS= read -r stderr_line; do
		rendered_line="$(security_audit_sanitize_log_value "${stderr_line}")"
		# Prefix untrusted text so it cannot become an Actions workflow command.
		printf 'security-audit: codex-stderr: %s\n' "${rendered_line}" >> "${rendered_path}"
	done < "${masked_path}"
	# Cap *rendered* bytes without cutting a line through a redacted token.
	local provider_class="unknown"
	provider_class="$(python3 - "${rendered_path}" "${tail_path}" 2>/dev/null <<'PY'
import re
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
kept = []
total = 0
for line in reversed(lines[-40:]):
	size = len((line + "\n").encode("utf-8"))
	if total + size > 4096:
		break
	kept.append(line)
	total += size
kept.reverse()
Path(sys.argv[2]).write_text("".join(line + "\n" for line in kept), encoding="utf-8")

provider = "unknown"
status = re.compile(r"\b(?:http(?:/\d+(?:\.\d+)?)?|status|statuscode|code|error|errorcode)\s*[:=/-]?\s*(?:error\s+)?(402|401|429|5\d\d)\b", re.I)
for line in kept:
	visible = line.replace("\\ ", " ")
	if re.search(r"payment required|insufficient credits", visible, re.I):
		provider = "402"
	elif re.search(r"rate limit", visible, re.I):
		provider = "429"
	else:
		match = status.search(visible)
		if match:
			provider = "5xx" if match.group(1).startswith("5") else match.group(1)
print(provider)
PY
	)" || provider_class="unknown"
	printf 'security-audit: codex-stderr-tail begin\n' >&2
	if [ -s "${tail_path}" ]; then
		cat "${tail_path}" >&2
	fi
	printf 'security-audit: codex-stderr-tail end\n' >&2
	printf '%s' "${provider_class}"
}

security_audit_require_file() {
	local required_phase="${1:?required phase required}"
	local required_path="${2:?required path required}"
	if [ ! -f "${required_path}" ] || [ ! -r "${required_path}" ]; then
		security_audit_emit_failure "${required_phase}" "${required_path}" "required readable file is unavailable"
		return 1
	fi
}

security_audit_require_directory() {
	local required_phase="${1:?required phase required}"
	local required_path="${2:?required path required}"
	if [ ! -d "${required_path}" ]; then
		security_audit_emit_failure "${required_phase}" "${required_path}" "required directory is unavailable"
		return 1
	fi
}

security_audit_require_writable_destination() {
	local required_phase="${1:?required phase required}"
	local required_path="${2:?required path required}"
	local required_parent
	required_parent="$(dirname -- "${required_path}")"
	if [ ! -d "${required_parent}" ]; then
		security_audit_emit_failure "${required_phase}" "${required_path}" "destination parent directory is unavailable"
		return 1
	fi
	if { [ -e "${required_path}" ] && { [ ! -f "${required_path}" ] || [ ! -w "${required_path}" ]; }; } \
			|| { [ ! -e "${required_path}" ] && [ ! -w "${required_parent}" ]; }; then
		security_audit_emit_failure "${required_phase}" "${required_path}" "destination is not writable"
		return 1
	fi
	# `-w` answers from permission bits alone, so it reports "writable" for
	# root on read-only or pseudo filesystems (e.g. /sys) where the write
	# itself is refused. Prove writability with a real write so a bad
	# destination fails at preflight instead of after the model run.
	if [ -e "${required_path}" ]; then
		if ! : >> "${required_path}" 2>/dev/null; then
			security_audit_emit_failure "${required_phase}" "${required_path}" "destination is not writable"
			return 1
		fi
	else
		local writable_probe_path
		if ! writable_probe_path="$(mktemp -p "${required_parent}" ".security-audit-writable-probe.XXXXXX" 2>/dev/null)"; then
			security_audit_emit_failure "${required_phase}" "${required_path}" "destination is not writable"
			return 1
		fi
		if ! rm -f -- "${writable_probe_path}" 2>/dev/null; then
			security_audit_emit_failure "${required_phase}" "${required_path}" "destination is not writable"
			return 1
		fi
	fi
}

security_audit_append_prompt_context() {
	echo || return 1
	echo "Current UTC date: $(date -u +%F)" || return 1
	if [ "${AUDIT_SCOPE_MODE}" = "incremental" ]; then
		if [ -n "${SECURITY_AUDIT_DIFF_BASE}" ]; then
			echo "Audit scope override: INCREMENTAL — explicit diff range ${AUDIT_SCOPE_BASE_SHA}..${AUDIT_SCOPE_HEAD_SHA}; the checked-out HEAD may be a non-default branch." || return 1
			if [ -n "${SECURITY_AUDIT_DIFF_SINCE}" ]; then
				echo "Delta re-audit: an earlier audit already covered this range up to commit ${AUDIT_SCOPE_SINCE_SHA}. Only range files changed since that commit, plus files cited by previously reported findings and files written by recent fix cycles, are in scope." || return 1
				echo "Files in scope (every finding MUST cite one of these files):" || return 1
			else
				echo "Files changed in the explicit range (every finding MUST cite one of these files):" || return 1
			fi
		else
			echo "Audit scope: INCREMENTAL — commits ${AUDIT_SCOPE_BASE_SHA}..${AUDIT_SCOPE_HEAD_SHA} on the default branch." || return 1
			echo "Files changed since the last audited commit (every finding MUST cite one of these files):" || return 1
		fi
		sed 's/^/- /' "${CHANGED_FILES_FILE}" || return 1
		echo "You may read any file in the repository to trace cross-file impact (callers, configuration, trust boundaries), but only emit findings whose cited file appears in the changed list above; findings citing unchanged files are dropped by the post-filter." || return 1
	else
		if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "findings-json" ]; then
			echo "Audit scope override: repository checkout at HEAD; do not assume the checked-out branch is the default branch." || return 1
		else
			echo "Audit scope: repository checkout at default-branch HEAD." || return 1
		fi
	fi
	if [ -s "${OVERSIZED_PROMPT_FILE}" ]; then
		echo || return 1
		cat "${OVERSIZED_PROMPT_FILE}" || return 1
	fi
	if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "findings-json" ]; then
		echo || return 1
		echo "Project-pass security and money-handling lens:" || return 1
		cat "${SECURITY_AUDIT_MONEY_LENS_FILE}" || return 1
		if [ -s "${PRIOR_FINDINGS_PROMPT_FILE}" ]; then
			echo || return 1
			echo "Previously reported findings for this project (earlier fix cycles; their fixes have merged into the audited head):" || return 1
			echo "=== BEGIN UNTRUSTED PRIOR FINDINGS ===" || return 1
			cat "${PRIOR_FINDINGS_PROMPT_FILE}" || return 1
			echo "=== END UNTRUSTED PRIOR FINDINGS ===" || return 1
			echo "Rules for previously reported findings:" || return 1
			echo "- Verify each one against the current code. If it is still exploitable, re-emit it with the SAME finding_id and the current line number." || return 1
			echo "- If it is resolved, omit it. Never report a resolved finding again under a new finding_id." || return 1
			echo "- Report every remaining instance of the same defect class in the scoped files (sibling code paths, other venues, adapters, handlers) as its own finding. The fix loop converges only when a class is cleared, not one line." || return 1
		fi
		if [ -s "${FIX_CYCLE_DIFFS_PROMPT_FILE}" ]; then
			echo || return 1
			echo "Newly introduced code since the last audit (written by the merged security-fix PR of the previous fix cycle and by any sync merges; the cycle before it is carried over once more). Shown as unified diff hunks per fix cycle:" || return 1
			echo "=== BEGIN UNTRUSTED FIX-CYCLE CODE ===" || return 1
			cat "${FIX_CYCLE_DIFFS_PROMPT_FILE}" || return 1
			echo "=== END UNTRUSTED FIX-CYCLE CODE ===" || return 1
			echo "Rules for newly introduced code:" || return 1
			echo "- Audit this code as FRESH attack surface for NEW defect classes. A fix written under time pressure is the most likely place for a hole that no earlier finding describes; a finding here needs its own new finding_id." || return 1
			echo "- Pay particular attention to readiness and state-transition predicates (is-settled, is-final, can-claim, may-refund, ready-to-pay), money-state transitions (debit, credit, payout, settlement, refund, pool distribution), and idempotency fences (unique keys, tombstones, TTLs, once-only guards) that the fix added or changed. Check that every predicate is complete, that every transition is atomic and authorised, and that every fence actually blocks the retry it exists for." || return 1
			echo "- Do not limit yourself to the previously reported finding ids or their defect classes. The rules for previously reported findings still apply to those findings; this section adds to them, it does not replace them." || return 1
			echo "- The hunks are for orientation only. Read the current file for surrounding context before concluding, and cite the current line number in the checked-out file." || return 1
		fi
		if [ -s "${WAIVED_FINDINGS_PROMPT_FILE}" ]; then
			echo || return 1
			echo "Accepted findings for this project (reviewed and waived as known risks; they are tracked in non-blocking follow-up issues):" || return 1
			echo "=== BEGIN UNTRUSTED ACCEPTED FINDINGS ===" || return 1
			cat "${WAIVED_FINDINGS_PROMPT_FILE}" || return 1
			echo "=== END UNTRUSTED ACCEPTED FINDINGS ===" || return 1
			echo "Rules for accepted findings:" || return 1
			echo "- Never re-report the same accepted exploit under any finding_id. Report a different exploit even when its file, category and line are near an accepted finding; an acceptance covers only its documented scenario." || return 1
			echo "- An acceptance covers one location. Other locations in the scoped files remain in scope." || return 1
		fi
		if [ -n "${SECURITY_AUDIT_PROJECT_SPEC_PATH}" ]; then
			echo || return 1
			echo "=== BEGIN UNTRUSTED PROJECT SPECIFICATION ===" || return 1
			cat "${SECURITY_AUDIT_PROJECT_SPEC_PATH}" || return 1
			echo || return 1
			echo "=== END UNTRUSTED PROJECT SPECIFICATION ===" || return 1
		fi
	fi
}

SECURITY_AUDIT_ENABLED="${SECURITY_AUDIT_ENABLED:-true}"
if ! security_audit_flag_enabled "${SECURITY_AUDIT_ENABLED}"; then
	echo "security-audit: SECURITY_AUDIT_ENABLED=${SECURITY_AUDIT_ENABLED}; skipping."
	exit 0
fi

SECURITY_AUDIT_OUTPUT_MODE="${SECURITY_AUDIT_OUTPUT_MODE:-issues}"
case "${SECURITY_AUDIT_OUTPUT_MODE}" in
	issues|findings-json)
		;;
	*)
		echo "SECURITY_AUDIT_OUTPUT_MODE must be issues or findings-json" >&2
		exit 1
		;;
esac

if [ "$#" -gt 1 ]; then
	echo "security_audit.sh accepts at most one project-spec file" >&2
	exit 1
fi
SECURITY_AUDIT_PROJECT_SPEC_PATH="${1:-}"
if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "issues" ] && [ -n "${SECURITY_AUDIT_PROJECT_SPEC_PATH}" ]; then
	echo "the project-spec file is only valid in findings-json mode" >&2
	exit 1
fi

SECURITY_AUDIT_FINDINGS_OUT="${SECURITY_AUDIT_FINDINGS_OUT:-}"
SECURITY_AUDIT_DIFF_BASE="${SECURITY_AUDIT_DIFF_BASE:-}"
SECURITY_AUDIT_DIFF_HEAD="${SECURITY_AUDIT_DIFF_HEAD:-}"
if { [ -n "${SECURITY_AUDIT_DIFF_BASE}" ] && [ -z "${SECURITY_AUDIT_DIFF_HEAD}" ]; } \
		|| { [ -z "${SECURITY_AUDIT_DIFF_BASE}" ] && [ -n "${SECURITY_AUDIT_DIFF_HEAD}" ]; }; then
	echo "SECURITY_AUDIT_DIFF_BASE and SECURITY_AUDIT_DIFF_HEAD must be supplied together" >&2
	exit 1
fi

# Optional delta narrowing for the explicit range: when set, only files that
# also changed in SECURITY_AUDIT_DIFF_SINCE..SECURITY_AUDIT_DIFF_HEAD stay in
# scope, so a re-audit after a merged fix looks at the fix (plus any prior
# findings, below) instead of re-sampling the whole project range.  The
# explicit range remains the scope contract: a file changed only by commits
# that were already on the base side of the range never enters scope.
SECURITY_AUDIT_DIFF_SINCE="${SECURITY_AUDIT_DIFF_SINCE:-}"
if [ -n "${SECURITY_AUDIT_DIFF_SINCE}" ] && [ -z "${SECURITY_AUDIT_DIFF_BASE}" ]; then
	echo "SECURITY_AUDIT_DIFF_SINCE requires SECURITY_AUDIT_DIFF_BASE and SECURITY_AUDIT_DIFF_HEAD" >&2
	exit 1
fi
# Optional JSON array of findings reported by earlier audits of the same
# project (findings-json mode only).  Their files are added to the audit
# scope and the list is appended to the prompt so the model verifies each one
# against the current code and reports remaining instances of the same class
# instead of re-discovering the project from scratch.
SECURITY_AUDIT_PRIOR_FINDINGS="${SECURITY_AUDIT_PRIOR_FINDINGS:-}"
if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "issues" ] && [ -n "${SECURITY_AUDIT_PRIOR_FINDINGS}" ]; then
	echo "SECURITY_AUDIT_PRIOR_FINDINGS is only valid in findings-json mode" >&2
	exit 1
fi
# Optional JSON array of findings that an operator (`/security-pass-waive`) or
# the orchestrator's security-pass exhaustion judge accepted as known risks
# for the audited project (findings-json mode only).  They are appended to the
# prompt as accepted findings the model must not report again, and the
# post-filter drops any re-report deterministically: an exact `finding_id`
# match, or the same file, category, severity and exploit scenario within
# SECURITY_AUDIT_WAIVER_LINE_WINDOW lines of the waived line (model-generated
# ids drift between runs and fix commits move lines).  Counted as
# `suppressed_waived`.  Malformed input fails closed like prior findings.
SECURITY_AUDIT_WAIVED_FINDINGS="${SECURITY_AUDIT_WAIVED_FINDINGS:-}"
if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "issues" ] && [ -n "${SECURITY_AUDIT_WAIVED_FINDINGS}" ]; then
	echo "SECURITY_AUDIT_WAIVED_FINDINGS is only valid in findings-json mode" >&2
	exit 1
fi
SECURITY_AUDIT_WAIVER_LINE_WINDOW="${SECURITY_AUDIT_WAIVER_LINE_WINDOW:-40}"
if ! [[ "${SECURITY_AUDIT_WAIVER_LINE_WINDOW}" =~ ^[0-9]+$ ]]; then
	echo "SECURITY_AUDIT_WAIVER_LINE_WINDOW must be a non-negative integer" >&2
	exit 1
fi
# Optional JSON array of fix-cycle diff entries (findings-json mode only),
# produced by the orchestrator from the local checkout:
#   [{"cycle": <int>, "since_sha": "<sha>", "head_sha": "<sha>", "files": [..]}]
# Each entry describes the code one security-fix cycle wrote (the commits
# between the head audited before the fix merged and the head audited after
# it).  Its files join the audit scope and its added/modified hunks are
# appended to the prompt as newly introduced code the model must audit as
# fresh attack surface for NEW defect classes -- not only for the previously
# reported finding ids.  Without this, re-audits verified the old findings
# and looked for more of the same class, so tele-funtoken-msg-scoring#4281
# exhausted its budget on a readiness predicate that cycle 1's fix created
# and cycles 2 and 3 never audited as new code.  Hunks are capped by
# SECURITY_AUDIT_FIX_DIFF_MAX_LINES / SECURITY_AUDIT_FIX_DIFF_MAX_BYTES;
# over the cap the remaining files are listed by name only.  Everything here
# fails OPEN: a missing or malformed file, an unresolvable SHA, or a git
# failure logs a warning and the audit runs exactly as it did before.
SECURITY_AUDIT_FIX_CYCLE_DIFFS="${SECURITY_AUDIT_FIX_CYCLE_DIFFS:-}"
if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "issues" ] && [ -n "${SECURITY_AUDIT_FIX_CYCLE_DIFFS}" ]; then
	echo "SECURITY_AUDIT_FIX_CYCLE_DIFFS is only valid in findings-json mode" >&2
	exit 1
fi
SECURITY_AUDIT_FIX_DIFF_MAX_LINES="${SECURITY_AUDIT_FIX_DIFF_MAX_LINES:-1200}"
if ! [[ "${SECURITY_AUDIT_FIX_DIFF_MAX_LINES}" =~ ^[0-9]+$ ]]; then
	echo "::warning::security-audit: SECURITY_AUDIT_FIX_DIFF_MAX_LINES must be a non-negative integer; defaulting to 1200"
	SECURITY_AUDIT_FIX_DIFF_MAX_LINES="1200"
fi
SECURITY_AUDIT_FIX_DIFF_MAX_BYTES="${SECURITY_AUDIT_FIX_DIFF_MAX_BYTES:-96000}"
if ! [[ "${SECURITY_AUDIT_FIX_DIFF_MAX_BYTES}" =~ ^[0-9]+$ ]]; then
	echo "::warning::security-audit: SECURITY_AUDIT_FIX_DIFF_MAX_BYTES must be a non-negative integer; defaulting to 96000"
	SECURITY_AUDIT_FIX_DIFF_MAX_BYTES="96000"
fi
# Line ownership (plan item 4b, decision D4; findings-json mode with an
# explicit SECURITY_AUDIT_DIFF_BASE..SECURITY_AUDIT_DIFF_HEAD range only).
# `project` (default) runs `git blame` over each finding's cited line at the
# head and tags the finding `"advisory": true` when that line was not written
# by a commit in base..head (it predates the project), `"advisory": false`
# otherwise.  Any blame failure tags the finding blocking.  A pre-project
# line still stays blocking when the project could have removed the control
# that protected it (finding security-pass-deleted-guard-advisory): the cited
# file lost or binary-changed lines in base..head (`deleted_lines_in_file`),
# the project added lines within SECURITY_AUDIT_LINE_OWNERSHIP_HUNK_WINDOW
# lines of it (`changed_hunk_within_window`), or the finding text names
# another file that lost lines (`references_file_with_deletions`).  A failed
# project diff or hunk read keeps the finding blocking too.  `off` restores
# the previous payload exactly (no `advisory` field, no `line_ownership` key).
SECURITY_AUDIT_LINE_OWNERSHIP="$(printf '%s' "${SECURITY_AUDIT_LINE_OWNERSHIP:-project}" | tr '[:upper:]' '[:lower:]')"
case "${SECURITY_AUDIT_LINE_OWNERSHIP}" in
	project|off)
		;;
	*)
		echo "::warning::security-audit: SECURITY_AUDIT_LINE_OWNERSHIP must be project or off; defaulting to project"
		SECURITY_AUDIT_LINE_OWNERSHIP="project"
		;;
esac
SECURITY_AUDIT_LINE_OWNERSHIP_HUNK_WINDOW="${SECURITY_AUDIT_LINE_OWNERSHIP_HUNK_WINDOW:-40}"
if ! [[ "${SECURITY_AUDIT_LINE_OWNERSHIP_HUNK_WINDOW}" =~ ^[0-9]+$ ]]; then
	echo "::warning::security-audit: SECURITY_AUDIT_LINE_OWNERSHIP_HUNK_WINDOW must be a non-negative integer; defaulting to 40"
	SECURITY_AUDIT_LINE_OWNERSHIP_HUNK_WINDOW="40"
fi
SECURITY_AUDIT_OVERSIZED_FILE_MAX_BYTES="${SECURITY_AUDIT_OVERSIZED_FILE_MAX_BYTES:-16777216}"
if ! [[ "${SECURITY_AUDIT_OVERSIZED_FILE_MAX_BYTES}" =~ ^[0-9]+$ ]] || [ "${SECURITY_AUDIT_OVERSIZED_FILE_MAX_BYTES}" -eq 0 ]; then
	echo "::warning::security-audit: SECURITY_AUDIT_OVERSIZED_FILE_MAX_BYTES must be a positive integer; defaulting to 16777216"
	SECURITY_AUDIT_OVERSIZED_FILE_MAX_BYTES=16777216
fi
SECURITY_AUDIT_OVERSIZED_TOTAL_MAX_BYTES="${SECURITY_AUDIT_OVERSIZED_TOTAL_MAX_BYTES:-67108864}"
if ! [[ "${SECURITY_AUDIT_OVERSIZED_TOTAL_MAX_BYTES}" =~ ^[0-9]+$ ]] || [ "${SECURITY_AUDIT_OVERSIZED_TOTAL_MAX_BYTES}" -eq 0 ]; then
	echo "::warning::security-audit: SECURITY_AUDIT_OVERSIZED_TOTAL_MAX_BYTES must be a positive integer; defaulting to 67108864"
	SECURITY_AUDIT_OVERSIZED_TOTAL_MAX_BYTES=67108864
fi

# Optional non-default branch the audit targets (issues mode only), set by the
# workflow's `ref` dispatch input for /implement-plan-claude project branches.
# The workflow also sets SECURITY_AUDIT_DIFF_BASE / SECURITY_AUDIT_DIFF_HEAD to
# the branch's merge-base with the default branch and its head, so the audit
# covers exactly the project's changes.  Follow-up issues then carry an
# `Integration branch:` line (scripts/resolve_integration_ref.sh routes their
# fix PRs onto that branch), and the tracker's default-branch
# last-audited-commit marker is left untouched.
SECURITY_AUDIT_TARGET_REF="${SECURITY_AUDIT_TARGET_REF:-}"
if [ -n "${SECURITY_AUDIT_TARGET_REF}" ]; then
	if [ "${SECURITY_AUDIT_OUTPUT_MODE}" != "issues" ]; then
		echo "SECURITY_AUDIT_TARGET_REF is only valid in issues mode" >&2
		exit 1
	fi
	if [ -z "${SECURITY_AUDIT_DIFF_BASE}" ]; then
		echo "SECURITY_AUDIT_TARGET_REF requires SECURITY_AUDIT_DIFF_BASE and SECURITY_AUDIT_DIFF_HEAD" >&2
		exit 1
	fi
	if ! git check-ref-format --branch "${SECURITY_AUDIT_TARGET_REF}" >/dev/null 2>&1; then
		echo "SECURITY_AUDIT_TARGET_REF must be a valid branch name" >&2
		exit 1
	fi
fi

# Skip the whole audit when HEAD matches the last audited commit recorded on
# the tracker issue (log-only skip; no issue comment).
SECURITY_AUDIT_SKIP_IF_UNCHANGED="${SECURITY_AUDIT_SKIP_IF_UNCHANGED:-true}"
# Scope the audit to commits since the last audited commit when possible;
# first runs and history rewrites fail open to the full default-branch scope.
SECURITY_AUDIT_INCREMENTAL="${SECURITY_AUDIT_INCREMENTAL:-true}"

: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY is required}"

if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "issues" ]; then
	: "${GH_TOKEN:?GH_TOKEN is required}"
	: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
	[[ "${GITHUB_REPOSITORY}" =~ ^[^/]+/[^/]+$ ]] || {
		echo "GITHUB_REPOSITORY must be in owner/repo format" >&2
		exit 1
	}
fi

SECURITY_AUDIT_CONFIDENCE_GATE="${SECURITY_AUDIT_CONFIDENCE_GATE:-8}"
if ! [[ "${SECURITY_AUDIT_CONFIDENCE_GATE}" =~ ^[0-9]+$ ]] \
	|| [ "${SECURITY_AUDIT_CONFIDENCE_GATE}" -lt 1 ] \
	|| [ "${SECURITY_AUDIT_CONFIDENCE_GATE}" -gt 10 ]; then
	echo "SECURITY_AUDIT_CONFIDENCE_GATE must be an integer from 1 to 10" >&2
	exit 1
fi

SECURITY_AUDIT_FP_EXCLUSIONS="${SECURITY_AUDIT_FP_EXCLUSIONS:-scripts/security_audit_fp_exclusions.json}"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "${REPO_ROOT}"

# Consumer-called runs (workflow-templates/ai-security-audit.yml wrapper) stage
# this repo's scripts/prompts outside the audited checkout and point
# SECURITY_AUDIT_SUPPORT_DIR at that staged tree. Source-repo runs leave it
# unset so support files resolve from the audited checkout itself,
# byte-identical to the pre-consumer behaviour.
SECURITY_AUDIT_SUPPORT_DIR="${SECURITY_AUDIT_SUPPORT_DIR:-${REPO_ROOT}}"

# Resolve the exclusion catalog: a copy in the audited repository wins (so a
# consumer can pin its own catalog at the default relative path); otherwise a
# relative path falls back to the staged support tree.
if [ ! -f "${SECURITY_AUDIT_FP_EXCLUSIONS}" ] \
	&& [[ "${SECURITY_AUDIT_FP_EXCLUSIONS}" != /* ]] \
	&& [ -f "${SECURITY_AUDIT_SUPPORT_DIR}/${SECURITY_AUDIT_FP_EXCLUSIONS}" ]; then
	SECURITY_AUDIT_FP_EXCLUSIONS="${SECURITY_AUDIT_SUPPORT_DIR}/${SECURITY_AUDIT_FP_EXCLUSIONS}"
fi

[ -f "${SECURITY_AUDIT_FP_EXCLUSIONS}" ] || {
	echo "SECURITY_AUDIT_FP_EXCLUSIONS not found: ${SECURITY_AUDIT_FP_EXCLUSIONS}" >&2
	exit 1
}

export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

security_audit_require_cmd bash
security_audit_require_cmd python3

if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "issues" ]; then
	security_audit_require_cmd gh
	[ -f "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/label_helpers.sh" ] || {
		echo "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/label_helpers.sh is required" >&2
		exit 1
	}

	# shellcheck disable=SC1091
	source "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/label_helpers.sh"

	# label_helpers.sh only finds gh_helpers.sh relative to the current working
	# directory; consumer-called runs execute from the audited checkout, so
	# re-source the staged gh_helpers.sh to restore the real gh_retry wrapper
	# (re-sourcing is a no-op redefinition on source-repo runs).
	if [ -f "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/gh_helpers.sh" ]; then
		# shellcheck disable=SC1091
		source "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/gh_helpers.sh"
	fi
	if [ -f "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/tg_helpers.sh" ]; then
		# tg_helpers.sh sources gh_helpers.sh relative to cwd. Never let the
		# audited checkout supply executable support to the trusted runner.
		if pushd "${SECURITY_AUDIT_SUPPORT_DIR}" >/dev/null; then
			# shellcheck disable=SC1091
			source scripts/tg_helpers.sh || true
			popd >/dev/null
		fi
	fi

	ensure_label_exists "ai:security-audit" "${GITHUB_REPOSITORY}"
	ensure_label_exists "ai:security" "${GITHUB_REPOSITORY}"
fi

TRACKER_TITLE="AI Security Audit Tracker"
TRACKER_MARKER="<!-- ai:security-audit-tracker:v1 -->"
FOLLOWUP_MARKER_PREFIX="<!-- ai:security-finding:"
LAST_SHA_MARKER_PREFIX="<!-- ai:security-audit-last-sha:"
PARTIAL_COVERAGE_MARKER="<!-- ai:security-audit-partial-coverage:v1 -->"
# Past this many changed files an incremental diff stops being cheaper than a
# full audit, so the scope resolver falls back to the full default-branch scope.
SECURITY_AUDIT_INCREMENTAL_MAX_FILES="200"

SECURITY_AUDIT_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/security-audit.XXXXXX")"
trap 'rm -rf "${SECURITY_AUDIT_RUNTIME_DIR}"' EXIT

TRACKER_BODY_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/tracker-issue-body.md"
TRACKER_CANDIDATES_JSON="${SECURITY_AUDIT_RUNTIME_DIR}/tracker-candidates.json"
TRACKER_SELECTION_ENV="${SECURITY_AUDIT_RUNTIME_DIR}/tracker-selection.env"
RENDERED_PROMPT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/prompt.txt"
RENDER_PROMPT_ERROR_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/prompt-render-error.txt"
CODEX_OUTPUT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/codex-output.json"
CODEX_ERROR_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/codex-error.txt"
FILTERED_FINDINGS_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/filtered-findings.json"
FILTER_SUMMARY_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/filter-summary.json"
EXISTING_FOLLOWUPS_JSON="${SECURITY_AUDIT_RUNTIME_DIR}/existing-followups.json"
TRACKER_COMMENT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/tracker-comment.md"
FOLLOWUP_INDEX_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/followup-index.tsv"
FOLLOWUP_SUMMARY_ENV="${SECURITY_AUDIT_RUNTIME_DIR}/followup-summary.env"
FOLLOWUP_BODY_DIR="${SECURITY_AUDIT_RUNTIME_DIR}/followups"
CHANGED_FILES_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/changed-files.txt"
TRACKER_BODY_WITH_SHA_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/tracker-issue-body-with-sha.md"
SECURITY_AUDIT_MONEY_LENS_TEMPLATE_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/security-money-lens.txt"
SECURITY_AUDIT_MONEY_LENS_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/rendered-security-money-lens.txt"
FINDINGS_PACKAGE_ERROR_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/findings-package-error.txt"
DELTA_CHANGED_FILES_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/delta-changed-files.txt"
PRIOR_FINDINGS_SCOPE_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/prior-findings-scope.txt"
PRIOR_FINDINGS_PROMPT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/prior-findings-prompt.txt"
PRIOR_FINDINGS_ERROR_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/prior-findings-error.txt"
WAIVED_FINDINGS_NORMALIZED_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/waived-findings.json"
WAIVED_FINDINGS_PROMPT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/waived-findings-prompt.txt"
WAIVED_FINDINGS_ERROR_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/waived-findings-error.txt"
FIX_CYCLE_DIFFS_SCOPE_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/fix-cycle-diffs-scope.txt"
FIX_CYCLE_DIFFS_PROMPT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/fix-cycle-diffs-prompt.txt"
FIX_CYCLE_DIFFS_ERROR_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/fix-cycle-diffs-error.txt"
OVERSIZED_EXPORT_DIR="${SECURITY_AUDIT_RUNTIME_DIR}/oversized-chunks"
OVERSIZED_SCOPE_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/oversized-scope.txt"
OVERSIZED_PROMPT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/oversized-prompt.txt"
OVERSIZED_ERROR_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/oversized-error.txt"
LINE_OWNERSHIP_SUMMARY_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/line-ownership-summary.json"
LINE_OWNERSHIP_ERROR_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/line-ownership-error.txt"

if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "findings-json" ]; then
	if [ -z "${SECURITY_AUDIT_FINDINGS_OUT}" ]; then
		security_audit_emit_failure "findings-output-preflight" "(unset)" "SECURITY_AUDIT_FINDINGS_OUT is required in findings-json mode"
		exit 1
	fi
	security_audit_require_writable_destination "findings-output-preflight" "${SECURITY_AUDIT_FINDINGS_OUT}"
	if [ -n "${SECURITY_AUDIT_PROJECT_SPEC_PATH}" ]; then
		security_audit_require_file "project-spec-preflight" "${SECURITY_AUDIT_PROJECT_SPEC_PATH}"
	fi
	if [ -n "${SECURITY_AUDIT_PRIOR_FINDINGS}" ]; then
		security_audit_require_file "prior-findings-preflight" "${SECURITY_AUDIT_PRIOR_FINDINGS}"
	fi
	if [ -n "${SECURITY_AUDIT_WAIVED_FINDINGS}" ]; then
		security_audit_require_file "waived-findings-preflight" "${SECURITY_AUDIT_WAIVED_FINDINGS}"
	fi
	if [ -n "${SECURITY_AUDIT_FIX_CYCLE_DIFFS}" ] && [ ! -f "${SECURITY_AUDIT_FIX_CYCLE_DIFFS}" ]; then
		# Fail open (unlike prior/waived findings): the newly-introduced-code
		# section is an aid, never a gate, so its absence must not stop the audit.
		echo "::warning::security-audit: SECURITY_AUDIT_FIX_CYCLE_DIFFS file is missing; the audit runs without the newly introduced code section"
		SECURITY_AUDIT_FIX_CYCLE_DIFFS=""
	fi
fi

LAST_AUDITED_SHA=""
TRACKER_PARTIAL_COVERAGE="false"
TRACKER_NUMBER=""
TRACKER_STATE=""

if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "issues" ]; then
cat > "${TRACKER_BODY_FILE}" <<EOF
${TRACKER_MARKER}
# ${TRACKER_TITLE}

This issue is managed by \`.github/workflows/security-audit.yml\`.

It collects weekly and ad-hoc default-branch OWASP Top 10 + STRIDE audit results.
Follow-up issues created from this tracker use the additive label \`ai:security\`.
EOF

# No existing prefetch/cache exists in this standalone workflow. One bulk
# issue-list call covers tracker discovery without per-issue follow-up probes.
gh_retry gh issue list \
	--repo "${GITHUB_REPOSITORY}" \
	--state all \
	--label "ai:security-audit" \
	--limit 50 \
		--json number,title,body,state,url > "${TRACKER_CANDIDATES_JSON}"

python3 - "${TRACKER_CANDIDATES_JSON}" "${TRACKER_MARKER}" "${LAST_SHA_MARKER_PREFIX}" "${PARTIAL_COVERAGE_MARKER}" > "${TRACKER_SELECTION_ENV}" <<'PY'
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

candidates_path = Path(sys.argv[1])
marker = sys.argv[2]
last_sha_marker_prefix = sys.argv[3]
partial_coverage_marker = sys.argv[4]

try:
	candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
	raise SystemExit(f"failed to load tracker candidates: {exc}")

selected = None
for candidate in candidates:
	if not isinstance(candidate, dict):
		continue
	body = str(candidate.get("body") or "")
	if marker not in body:
		continue
	selected = candidate
	if str(candidate.get("state") or "").upper() == "OPEN":
		break

number = ""
state = ""
last_audited_sha = ""
partial_coverage = False
if isinstance(selected, dict):
	number = str(selected.get("number") or "").strip()
	state = str(selected.get("state") or "").strip()
	# The tracker-discovery list call above already returns the body, so the
	# last-audited-commit marker is parsed without any additional API call.
	last_sha_match = re.search(
		re.escape(last_sha_marker_prefix) + r"([0-9a-fA-F]{7,40}) -->",
		str(selected.get("body") or ""),
	)
	if last_sha_match is not None:
		last_audited_sha = last_sha_match.group(1).strip().lower()
	partial_coverage = partial_coverage_marker in str(selected.get("body") or "")

print(f"TRACKER_NUMBER={shlex.quote(number)}")
print(f"TRACKER_STATE={shlex.quote(state)}")
print(f"LAST_AUDITED_SHA={shlex.quote(last_audited_sha)}")
print(f"TRACKER_PARTIAL_COVERAGE={'true' if partial_coverage else 'false'}")
PY

# shellcheck disable=SC1090
source "${TRACKER_SELECTION_ENV}"

if [ -z "${TRACKER_NUMBER}" ]; then
	TRACKER_URL="$(gh_retry gh issue create \
		--repo "${GITHUB_REPOSITORY}" \
		--title "${TRACKER_TITLE}" \
		--label "ai:security-audit" \
		--body-file "${TRACKER_BODY_FILE}")"
	TRACKER_NUMBER="${TRACKER_URL##*/}"
else
	if [ "$(printf '%s' "${TRACKER_STATE}" | tr '[:lower:]' '[:upper:]')" = "CLOSED" ]; then
		gh_retry gh issue reopen "${TRACKER_NUMBER}" --repo "${GITHUB_REPOSITORY}"
	fi
	gh_retry gh issue edit "${TRACKER_NUMBER}" --repo "${GITHUB_REPOSITORY}" --add-label "ai:security-audit"
fi
fi

# --- Scope resolution: skip-if-unchanged + incremental diff scope ---------
# HEAD_SHA is empty when the checkout is not a git repository; both gates
# then fail open to the historical full-scope audit.
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"

AUDIT_SCOPE_MODE="full"
AUDIT_SCOPE_REASON="no last-audited commit recorded on the tracker"
AUDIT_SCOPE_BASE_SHA=""
AUDIT_SCOPE_HEAD_SHA="${HEAD_SHA}"
: > "${CHANGED_FILES_FILE}"

if [ -n "${SECURITY_AUDIT_DIFF_BASE}" ]; then
	if [ -z "${HEAD_SHA}" ]; then
		security_audit_emit_failure "diff-scope" "${REPO_ROOT}" "checkout is not a git repository"
		exit 1
	fi
	if ! AUDIT_SCOPE_BASE_SHA="$(git rev-parse --verify --end-of-options "${SECURITY_AUDIT_DIFF_BASE}^{commit}" 2>/dev/null)"; then
		security_audit_emit_failure "diff-scope" "${SECURITY_AUDIT_DIFF_BASE}" "SECURITY_AUDIT_DIFF_BASE does not resolve to a commit"
		exit 1
	fi
	if ! AUDIT_SCOPE_HEAD_SHA="$(git rev-parse --verify --end-of-options "${SECURITY_AUDIT_DIFF_HEAD}^{commit}" 2>/dev/null)"; then
		security_audit_emit_failure "diff-scope" "${SECURITY_AUDIT_DIFF_HEAD}" "SECURITY_AUDIT_DIFF_HEAD does not resolve to a commit"
		exit 1
	fi
	if [ "${AUDIT_SCOPE_HEAD_SHA}" != "${HEAD_SHA}" ]; then
		security_audit_emit_failure "diff-scope" "${SECURITY_AUDIT_DIFF_HEAD}" "explicit diff head does not match the checked-out HEAD"
		exit 1
	fi
	if ! git merge-base --is-ancestor "${AUDIT_SCOPE_BASE_SHA}" "${AUDIT_SCOPE_HEAD_SHA}" 2>/dev/null; then
		security_audit_emit_failure "diff-scope" "${SECURITY_AUDIT_DIFF_BASE}..${SECURITY_AUDIT_DIFF_HEAD}" "explicit diff base is not an ancestor of the diff head"
		exit 1
	fi
	if ! git diff --name-only "${AUDIT_SCOPE_BASE_SHA}..${AUDIT_SCOPE_HEAD_SHA}" -- > "${CHANGED_FILES_FILE}"; then
		security_audit_emit_failure "diff-scope" "${SECURITY_AUDIT_DIFF_BASE}..${SECURITY_AUDIT_DIFF_HEAD}" "unable to derive explicit changed-file scope"
		exit 1
	fi
	CHANGED_FILE_COUNT="$(grep -c . "${CHANGED_FILES_FILE}" 2>/dev/null || true)"
	if ! [[ "${CHANGED_FILE_COUNT}" =~ ^[0-9]+$ ]]; then
		security_audit_emit_failure "diff-scope" "${SECURITY_AUDIT_DIFF_BASE}..${SECURITY_AUDIT_DIFF_HEAD}" "could not count explicitly changed files"
		exit 1
	fi
	AUDIT_SCOPE_MODE="incremental"
	AUDIT_SCOPE_REASON="${CHANGED_FILE_COUNT} files in explicit range ${AUDIT_SCOPE_BASE_SHA}..${AUDIT_SCOPE_HEAD_SHA}"
	if [ -n "${SECURITY_AUDIT_DIFF_SINCE}" ]; then
		# Delta narrowing: keep only range files that also changed since the
		# last audited commit.  Fails closed on an unusable SINCE commit so a
		# stale or rewritten pointer never silently widens or empties scope.
		if ! AUDIT_SCOPE_SINCE_SHA="$(git rev-parse --verify --end-of-options "${SECURITY_AUDIT_DIFF_SINCE}^{commit}" 2>/dev/null)"; then
			security_audit_emit_failure "diff-scope" "${SECURITY_AUDIT_DIFF_SINCE}" "SECURITY_AUDIT_DIFF_SINCE does not resolve to a commit"
			exit 1
		fi
		if ! git merge-base --is-ancestor "${AUDIT_SCOPE_SINCE_SHA}" "${AUDIT_SCOPE_HEAD_SHA}" 2>/dev/null; then
			security_audit_emit_failure "diff-scope" "${SECURITY_AUDIT_DIFF_SINCE}..${SECURITY_AUDIT_DIFF_HEAD}" "SECURITY_AUDIT_DIFF_SINCE is not an ancestor of the diff head"
			exit 1
		fi
		if ! git diff --name-only "${AUDIT_SCOPE_SINCE_SHA}..${AUDIT_SCOPE_HEAD_SHA}" -- > "${DELTA_CHANGED_FILES_FILE}"; then
			security_audit_emit_failure "diff-scope" "${SECURITY_AUDIT_DIFF_SINCE}..${SECURITY_AUDIT_DIFF_HEAD}" "unable to derive delta changed-file scope"
			exit 1
		fi
		if ! grep -Fxf "${DELTA_CHANGED_FILES_FILE}" "${CHANGED_FILES_FILE}" > "${CHANGED_FILES_FILE}.delta" 2>/dev/null; then
			# grep exits 1 on an empty intersection; that is a legitimate
			# (narrow) scope, not an error.
			: > "${CHANGED_FILES_FILE}.delta"
		fi
		mv "${CHANGED_FILES_FILE}.delta" "${CHANGED_FILES_FILE}"
		DELTA_FILE_COUNT="$(grep -c . "${CHANGED_FILES_FILE}" 2>/dev/null || true)"
		[[ "${DELTA_FILE_COUNT}" =~ ^[0-9]+$ ]] || DELTA_FILE_COUNT=0
		AUDIT_SCOPE_REASON="${DELTA_FILE_COUNT} of ${CHANGED_FILE_COUNT} files in explicit range ${AUDIT_SCOPE_BASE_SHA}..${AUDIT_SCOPE_HEAD_SHA} changed since last audited commit ${AUDIT_SCOPE_SINCE_SHA}"
	fi
elif [ -z "${HEAD_SHA}" ]; then
	AUDIT_SCOPE_REASON="checkout is not a git repository; scope gates fail open to a full audit"
elif [ "${TRACKER_PARTIAL_COVERAGE}" = "true" ]; then
	AUDIT_SCOPE_REASON="previous default-branch full scan skipped over-cap text; repeating the full audit"
elif [ -n "${LAST_AUDITED_SHA}" ]; then
	if [ "${LAST_AUDITED_SHA}" = "${HEAD_SHA}" ]; then
		if security_audit_flag_enabled "${SECURITY_AUDIT_SKIP_IF_UNCHANGED}"; then
			echo "security-audit: skipping — HEAD ${HEAD_SHA} unchanged since last audit (tracker=#${TRACKER_NUMBER})."
			exit 0
		fi
		AUDIT_SCOPE_REASON="HEAD unchanged since last audit but SECURITY_AUDIT_SKIP_IF_UNCHANGED is disabled"
	elif security_audit_flag_enabled "${SECURITY_AUDIT_INCREMENTAL}"; then
		if git cat-file -e "${LAST_AUDITED_SHA}^{commit}" 2>/dev/null \
			&& git merge-base --is-ancestor "${LAST_AUDITED_SHA}" "${HEAD_SHA}" 2>/dev/null; then
			git diff --name-only "${LAST_AUDITED_SHA}..${HEAD_SHA}" > "${CHANGED_FILES_FILE}"
			CHANGED_FILE_COUNT="$(grep -c . "${CHANGED_FILES_FILE}" 2>/dev/null || true)"
			if ! [[ "${CHANGED_FILE_COUNT}" =~ ^[0-9]+$ ]]; then
				AUDIT_SCOPE_REASON="could not count changed files since ${LAST_AUDITED_SHA}; falling back to a full audit"
			elif [ "${CHANGED_FILE_COUNT}" -eq 0 ]; then
				if security_audit_flag_enabled "${SECURITY_AUDIT_SKIP_IF_UNCHANGED}"; then
					echo "security-audit: skipping — no content changes between ${LAST_AUDITED_SHA} and ${HEAD_SHA} (tracker=#${TRACKER_NUMBER})."
					exit 0
				fi
				AUDIT_SCOPE_REASON="empty diff since last audit but SECURITY_AUDIT_SKIP_IF_UNCHANGED is disabled"
			elif [ "${CHANGED_FILE_COUNT}" -gt "${SECURITY_AUDIT_INCREMENTAL_MAX_FILES}" ]; then
				AUDIT_SCOPE_REASON="${CHANGED_FILE_COUNT} changed files exceed the incremental cap of ${SECURITY_AUDIT_INCREMENTAL_MAX_FILES}; falling back to a full audit"
			else
				AUDIT_SCOPE_MODE="incremental"
				AUDIT_SCOPE_REASON="${CHANGED_FILE_COUNT} files changed since last audited commit ${LAST_AUDITED_SHA}"
				AUDIT_SCOPE_BASE_SHA="${LAST_AUDITED_SHA}"
				AUDIT_SCOPE_HEAD_SHA="${HEAD_SHA}"
			fi
		else
			AUDIT_SCOPE_REASON="last audited commit ${LAST_AUDITED_SHA} is missing or not an ancestor of HEAD (history rewrite?); falling back to a full audit"
		fi
	else
		AUDIT_SCOPE_REASON="SECURITY_AUDIT_INCREMENTAL is disabled"
	fi
fi
echo "security-audit: scope=${AUDIT_SCOPE_MODE} (${AUDIT_SCOPE_REASON})"

# --- Prior findings: extend scope + render the prompt section ---------------
# Findings reported by earlier audits of this project keep their files in
# scope (so a still-present finding can be re-emitted through the incremental
# post-filter) and are listed for the model to verify.  Validation fails
# closed: the file is produced by the orchestrator from its own state, so a
# malformed one signals a caller bug, not an audit result.
PRIOR_FINDINGS_COUNT=0
: > "${PRIOR_FINDINGS_SCOPE_FILE}"
: > "${PRIOR_FINDINGS_PROMPT_FILE}"
if [ -n "${SECURITY_AUDIT_PRIOR_FINDINGS}" ]; then
	if ! PRIOR_FINDINGS_COUNT="$(python3 - \
		"${REPO_ROOT}" \
		"${SECURITY_AUDIT_PRIOR_FINDINGS}" \
		"${PRIOR_FINDINGS_SCOPE_FILE}" \
		"${PRIOR_FINDINGS_PROMPT_FILE}" 2> "${PRIOR_FINDINGS_ERROR_FILE}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath

repo_root = Path(sys.argv[1]).resolve()
prior_findings_path = Path(sys.argv[2])
scope_path = Path(sys.argv[3])
prompt_path = Path(sys.argv[4])

try:
	prior_findings = json.loads(prior_findings_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
	raise SystemExit(f"unable to load prior findings: {exc}")
if not isinstance(prior_findings, list):
	raise SystemExit("prior findings must be a JSON array")


def text_field(finding: dict, key: str) -> str:
	value = finding.get(key)
	if not isinstance(value, (str, int, float)) or isinstance(value, bool):
		return ""
	sanitized_prompt_value = " ".join(str(value).split()).replace("`", "")
	for untrusted_fence in (
		"=== BEGIN UNTRUSTED PRIOR FINDINGS ===", "=== END UNTRUSTED PRIOR FINDINGS ===",
		"=== BEGIN UNTRUSTED FIX-CYCLE CODE ===", "=== END UNTRUSTED FIX-CYCLE CODE ===",
		"=== BEGIN UNTRUSTED ACCEPTED FINDINGS ===", "=== END UNTRUSTED ACCEPTED FINDINGS ===",
	):
		sanitized_prompt_value = sanitized_prompt_value.replace(untrusted_fence, "[untrusted marker removed]")
	return sanitized_prompt_value


scope_files: list[str] = []
prompt_lines: list[str] = []
for index, finding in enumerate(prior_findings):
	if not isinstance(finding, dict):
		raise SystemExit(f"prior finding #{index} must be an object")
	file_value = finding.get("file")
	if not isinstance(file_value, str) or not file_value.strip():
		raise SystemExit(f"prior finding #{index} is missing its file")
	relative_file_path = PurePosixPath(file_value.strip())
	if relative_file_path.is_absolute() or ".." in relative_file_path.parts:
		raise SystemExit(f"prior finding #{index} cites a non-repository path")
	relative_file = relative_file_path.as_posix()
	if not relative_file or relative_file == ".":
		raise SystemExit(f"prior finding #{index} cites a non-repository path")
	try:
		resolved = (repo_root / relative_file).resolve()
		resolved.relative_to(repo_root)
	except (OSError, ValueError):
		raise SystemExit(f"prior finding #{index} cites a path outside the repository")
	# A deleted file cannot carry a finding any more; it stays in the prompt
	# list (the model may confirm the removal) but never enters scope.
	if resolved.is_file() and relative_file not in scope_files:
		scope_files.append(relative_file)
	finding_id = text_field(finding, "finding_id") or f"prior-finding-{index + 1}"
	line_value = finding.get("line")
	location = relative_file
	if isinstance(line_value, int) and not isinstance(line_value, bool) and line_value > 0:
		location = f"{relative_file}:{line_value}"
	cycle_value = finding.get("cycle")
	cycle_note = ""
	if isinstance(cycle_value, int) and not isinstance(cycle_value, bool) and cycle_value > 0:
		cycle_note = f" (reported in fix cycle {cycle_value})"
	prompt_lines.append(
		"- `{id}`{cycle} | {category} | {severity} | confidence {confidence} | {location}\n"
		"  Exploit scenario: {exploit}\n"
		"  Recommendation given: {recommendation}".format(
			id=finding_id,
			cycle=cycle_note,
			category=text_field(finding, "owasp_or_stride_category") or "uncategorised",
			severity=text_field(finding, "severity") or "unknown",
			confidence=text_field(finding, "confidence") or "?",
			location=location,
			exploit=text_field(finding, "exploit_scenario") or "(not recorded)",
			recommendation=text_field(finding, "recommendation") or "(not recorded)",
		)
	)

scope_path.write_text("".join(f"{path}\n" for path in scope_files), encoding="utf-8")
prompt_path.write_text("".join(f"{line}\n" for line in prompt_lines), encoding="utf-8")
print(len(prior_findings))
PY
	)"; then
		security_audit_emit_path_diagnostic "${PRIOR_FINDINGS_ERROR_FILE}"
		security_audit_emit_failure "prior-findings" "${SECURITY_AUDIT_PRIOR_FINDINGS}" "$(head -n1 "${PRIOR_FINDINGS_ERROR_FILE}" 2>/dev/null || echo 'prior findings could not be processed')"
		exit 1
	fi
	[[ "${PRIOR_FINDINGS_COUNT}" =~ ^[0-9]+$ ]] || PRIOR_FINDINGS_COUNT=0
	PRIOR_FINDINGS_SCOPE_COUNT="$(grep -c . "${PRIOR_FINDINGS_SCOPE_FILE}" 2>/dev/null || true)"
	[[ "${PRIOR_FINDINGS_SCOPE_COUNT}" =~ ^[0-9]+$ ]] || PRIOR_FINDINGS_SCOPE_COUNT=0
	if [ "${AUDIT_SCOPE_MODE}" = "incremental" ] && [ "${PRIOR_FINDINGS_SCOPE_COUNT}" -gt 0 ]; then
		cat "${CHANGED_FILES_FILE}" "${PRIOR_FINDINGS_SCOPE_FILE}" | grep . | sort -u > "${CHANGED_FILES_FILE}.union"
		mv "${CHANGED_FILES_FILE}.union" "${CHANGED_FILES_FILE}"
	fi
	echo "security-audit: prior-findings=${PRIOR_FINDINGS_COUNT} (${PRIOR_FINDINGS_SCOPE_COUNT} cited files kept in scope)"
fi

# --- Fix-cycle diffs: extend scope + render newly introduced code ------------
# Each entry names the code one fix cycle wrote (since_sha..head_sha, files).
# Files join the incremental scope so a hole in a file that no finding ever
# cited stays auditable for one more cycle, and the added/modified hunks are
# rendered for the prompt under the line/byte caps.  Everything fails open: a
# broken entry is skipped with a warning and the audit proceeds without it.
FIX_CYCLE_DIFFS_SUMMARY=""
: > "${FIX_CYCLE_DIFFS_SCOPE_FILE}"
: > "${FIX_CYCLE_DIFFS_PROMPT_FILE}"
if [ -n "${SECURITY_AUDIT_FIX_CYCLE_DIFFS}" ]; then
	if FIX_CYCLE_DIFFS_SUMMARY="$(python3 - \
		"${REPO_ROOT}" \
		"${SECURITY_AUDIT_FIX_CYCLE_DIFFS}" \
		"${FIX_CYCLE_DIFFS_SCOPE_FILE}" \
		"${FIX_CYCLE_DIFFS_PROMPT_FILE}" \
		"${SECURITY_AUDIT_FIX_DIFF_MAX_LINES}" \
		"${SECURITY_AUDIT_FIX_DIFF_MAX_BYTES}" 2> "${FIX_CYCLE_DIFFS_ERROR_FILE}" <<'PY'
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

repo_root = Path(sys.argv[1]).resolve()
entries_path = Path(sys.argv[2])
scope_path = Path(sys.argv[3])
prompt_path = Path(sys.argv[4])
max_lines = int(sys.argv[5])
max_bytes = int(sys.argv[6])

SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
FENCES = (
	"=== BEGIN UNTRUSTED PRIOR FINDINGS ===", "=== END UNTRUSTED PRIOR FINDINGS ===",
	"=== BEGIN UNTRUSTED FIX-CYCLE CODE ===", "=== END UNTRUSTED FIX-CYCLE CODE ===",
	"=== BEGIN UNTRUSTED ACCEPTED FINDINGS ===", "=== END UNTRUSTED ACCEPTED FINDINGS ===",
)
MAX_FILES_PER_ENTRY = 200


def warn(message: str) -> None:
	print(f"fix-cycle-diffs: {message}", file=sys.stderr)


def sanitize(text: str) -> str:
	for fence in FENCES:
		text = text.replace(fence, "[untrusted marker removed]")
	return text


def git(*args: str) -> str | None:
	try:
		completed = subprocess.run(
			["git", *args],
			cwd=repo_root,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			check=False,
		)
	except (OSError, ValueError) as exc:
		warn(f"git {' '.join(args[:2])} failed: {exc}")
		return None
	if completed.returncode != 0:
		return None
	return completed.stdout


def relative_repo_file(index: int, value: object) -> str | None:
	if not isinstance(value, str) or not value.strip():
		warn(f"entry #{index} lists a non-string file; skipped")
		return None
	candidate = PurePosixPath(value.strip())
	if candidate.is_absolute() or ".." in candidate.parts:
		warn(f"entry #{index} lists a non-repository path; skipped")
		return None
	relative = candidate.as_posix()
	if relative.startswith("./"):
		relative = relative[2:]
	if not relative or relative == ".":
		warn(f"entry #{index} lists a non-repository path; skipped")
		return None
	try:
		(repo_root / relative).resolve().relative_to(repo_root)
	except (OSError, ValueError):
		warn(f"entry #{index} lists a path outside the repository; skipped")
		return None
	return relative


try:
	entries = json.loads(entries_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
	raise SystemExit(f"unable to load fix-cycle diffs: {exc}")
if not isinstance(entries, list):
	raise SystemExit("fix-cycle diffs must be a JSON array")

scope_files: list[str] = []
prompt_blocks: list[str] = []
entry_count = 0
hunk_files = 0
omitted_files = 0
used_lines = 0
used_bytes = 0
over_budget = False

for index, entry in enumerate(entries):
	if not isinstance(entry, dict):
		warn(f"entry #{index} is not an object; skipped")
		continue
	raw_files = entry.get("files")
	if not isinstance(raw_files, list):
		warn(f"entry #{index} has no files array; skipped")
		continue
	files: list[str] = []
	for raw_file in raw_files[:MAX_FILES_PER_ENTRY]:
		relative = relative_repo_file(index, raw_file)
		if relative and relative not in files:
			files.append(relative)
	if len(raw_files) > MAX_FILES_PER_ENTRY:
		warn(f"entry #{index} lists {len(raw_files)} files; only the first {MAX_FILES_PER_ENTRY} are used")
	if not files:
		warn(f"entry #{index} has no usable files; skipped")
		continue
	entry_count += 1
	cycle = entry.get("cycle")
	cycle_label = "Head advance after a clean pass"
	if isinstance(cycle, int) and not isinstance(cycle, bool) and cycle > 0:
		cycle_label = f"Fix cycle {cycle}"
	since_sha = entry.get("since_sha")
	head_sha = entry.get("head_sha")
	range_ok = False
	range_note = ""
	if not (isinstance(since_sha, str) and SHA_RE.match(since_sha) and isinstance(head_sha, str) and SHA_RE.match(head_sha)):
		range_note = "commit range not recorded; files listed by name"
		since_sha = head_sha = ""
	else:
		since_resolved = git("rev-parse", "--verify", "--end-of-options", f"{since_sha}^{{commit}}")
		head_resolved = git("rev-parse", "--verify", "--end-of-options", f"{head_sha}^{{commit}}")
		if since_resolved is None or head_resolved is None:
			range_note = "commit range does not resolve in this checkout; files listed by name"
		elif git("merge-base", "--is-ancestor", since_resolved.strip(), head_resolved.strip()) is None:
			range_note = "recorded since-commit is not an ancestor of the recorded head; files listed by name"
		else:
			range_ok = True
			since_sha = since_resolved.strip()
			head_sha = head_resolved.strip()
	if range_note:
		warn(f"entry #{index} ({cycle_label.lower()}): {range_note}")
	for relative in files:
		if (repo_root / relative).is_file() and relative not in scope_files:
			scope_files.append(relative)
	header = f"{cycle_label}"
	if range_ok:
		header += f" -- commits {since_sha[:12]}..{head_sha[:12]}"
	header += f"; files: {', '.join(files)}"
	if range_note:
		header += f" ({range_note})"
	lines = [sanitize(header)]
	if range_ok:
		for relative in files:
			diff_text = git(
				"diff", "--no-color", "--no-ext-diff", "--unified=3", "--diff-filter=AM",
				f"{since_sha}..{head_sha}", "--", relative,
			)
			if diff_text is None:
				warn(f"entry #{index}: git diff failed for {relative}; listed by name")
				lines.append(f"--- {relative}: hunks unavailable (git diff failed; file is in scope by name) ---")
				continue
			diff_text = sanitize(diff_text.rstrip("\n"))
			if not diff_text:
				lines.append(f"--- {relative}: no added or modified hunks in this range ---")
				continue
			diff_lines = diff_text.count("\n") + 1
			diff_bytes = len(diff_text.encode("utf-8"))
			if over_budget or used_lines + diff_lines > max_lines or used_bytes + diff_bytes > max_bytes:
				over_budget = True
				omitted_files += 1
				lines.append(f"--- {relative}: hunks omitted (size cap reached; file is in scope by name, read it directly) ---")
				continue
			used_lines += diff_lines
			used_bytes += diff_bytes
			hunk_files += 1
			lines.append(f"--- {relative} ---")
			lines.append(diff_text)
	prompt_blocks.append(sanitize("\n".join(lines)))

if omitted_files:
	prompt_blocks.append(
		f"Hunks omitted for {omitted_files} file(s): the fix-cycle diff exceeded the cap "
		f"(SECURITY_AUDIT_FIX_DIFF_MAX_LINES={max_lines}, SECURITY_AUDIT_FIX_DIFF_MAX_BYTES={max_bytes}). "
		"Those files are in scope by name; read them directly."
	)
	warn(
		f"hunks omitted for {omitted_files} file(s) over the cap "
		f"(lines {max_lines}, bytes {max_bytes}); they are in scope by name only"
	)

scope_path.write_text("".join(f"{path}\n" for path in scope_files), encoding="utf-8")
prompt_path.write_text("".join(f"{block}\n\n" for block in prompt_blocks), encoding="utf-8")
print(
	f"fix-cycle-diffs={entry_count} entries ({len(scope_files)} files kept in scope, "
	f"hunks for {hunk_files} files: {used_lines} lines, {used_bytes} bytes, {omitted_files} files over the cap)"
)
PY
	)"; then
		if [ -s "${FIX_CYCLE_DIFFS_ERROR_FILE}" ]; then
			while IFS= read -r fix_cycle_diffs_warning_line; do
				[ -n "${fix_cycle_diffs_warning_line}" ] || continue
				echo "::warning::security-audit: ${fix_cycle_diffs_warning_line}"
			done < "${FIX_CYCLE_DIFFS_ERROR_FILE}"
		fi
		FIX_CYCLE_DIFFS_SCOPE_COUNT="$(grep -c . "${FIX_CYCLE_DIFFS_SCOPE_FILE}" 2>/dev/null || true)"
		[[ "${FIX_CYCLE_DIFFS_SCOPE_COUNT}" =~ ^[0-9]+$ ]] || FIX_CYCLE_DIFFS_SCOPE_COUNT=0
		if [ "${AUDIT_SCOPE_MODE}" = "incremental" ] && [ "${FIX_CYCLE_DIFFS_SCOPE_COUNT}" -gt 0 ]; then
			cat "${CHANGED_FILES_FILE}" "${FIX_CYCLE_DIFFS_SCOPE_FILE}" | grep . | sort -u > "${CHANGED_FILES_FILE}.union"
			mv "${CHANGED_FILES_FILE}.union" "${CHANGED_FILES_FILE}"
		fi
		echo "security-audit: ${FIX_CYCLE_DIFFS_SUMMARY}"
	else
		# Fail open: the section is an aid, never a gate.
		echo "::warning::security-audit: fix-cycle diffs could not be processed ($(head -n1 "${FIX_CYCLE_DIFFS_ERROR_FILE}" 2>/dev/null || echo 'unknown error')); the audit runs without the newly introduced code section"
		: > "${FIX_CYCLE_DIFFS_SCOPE_FILE}"
		: > "${FIX_CYCLE_DIFFS_PROMPT_FILE}"
	fi
fi

# --- Waived findings: normalize + render the prompt section ----------------
# Accepted findings never enter scope on their own (they are not to be
# re-audited); they are listed for the model as accepted and enforced by the
# post-filter below.  Validation fails closed: the file comes from the
# orchestrator's own state or an operator command it already validated.
WAIVED_FINDINGS_COUNT=0
: > "${WAIVED_FINDINGS_PROMPT_FILE}"
printf '[]\n' > "${WAIVED_FINDINGS_NORMALIZED_FILE}"
if [ -n "${SECURITY_AUDIT_WAIVED_FINDINGS}" ]; then
	if ! WAIVED_FINDINGS_COUNT="$(python3 - \
		"${SECURITY_AUDIT_WAIVED_FINDINGS}" \
		"${WAIVED_FINDINGS_NORMALIZED_FILE}" \
		"${WAIVED_FINDINGS_PROMPT_FILE}" 2> "${WAIVED_FINDINGS_ERROR_FILE}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath

waived_findings_path = Path(sys.argv[1])
normalized_path = Path(sys.argv[2])
prompt_path = Path(sys.argv[3])

try:
	waived_findings = json.loads(waived_findings_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
	raise SystemExit(f"unable to load waived findings: {exc}")
if not isinstance(waived_findings, list):
	raise SystemExit("waived findings must be a JSON array")


def text_field(finding: dict, key: str) -> str:
	value = finding.get(key)
	if not isinstance(value, (str, int, float)) or isinstance(value, bool):
		return ""
	sanitized_prompt_value = " ".join(str(value).split()).replace("`", "")
	for untrusted_fence in (
		"=== BEGIN UNTRUSTED PRIOR FINDINGS ===", "=== END UNTRUSTED PRIOR FINDINGS ===",
		"=== BEGIN UNTRUSTED FIX-CYCLE CODE ===", "=== END UNTRUSTED FIX-CYCLE CODE ===",
		"=== BEGIN UNTRUSTED ACCEPTED FINDINGS ===", "=== END UNTRUSTED ACCEPTED FINDINGS ===",
	):
		sanitized_prompt_value = sanitized_prompt_value.replace(untrusted_fence, "[untrusted marker removed]")
	return sanitized_prompt_value


normalized: list[dict] = []
prompt_lines: list[str] = []
for index, finding in enumerate(waived_findings):
	if not isinstance(finding, dict):
		raise SystemExit(f"waived finding #{index} must be an object")
	finding_id = text_field(finding, "finding_id")
	if not finding_id:
		raise SystemExit(f"waived finding #{index} is missing its finding_id")
	relative_file = ""
	file_value = finding.get("file")
	if isinstance(file_value, str) and file_value.strip():
		relative_file_path = PurePosixPath(file_value.strip())
		if relative_file_path.is_absolute() or ".." in relative_file_path.parts:
			raise SystemExit(f"waived finding #{index} cites a non-repository path")
		relative_file = relative_file_path.as_posix()
		if relative_file.startswith("./"):
			relative_file = relative_file[2:]
		if not relative_file or relative_file == ".":
			raise SystemExit(f"waived finding #{index} cites a non-repository path")
	line_value = finding.get("line")
	line_number = 0
	if isinstance(line_value, int) and not isinstance(line_value, bool) and line_value > 0:
		line_number = line_value
	category = text_field(finding, "owasp_or_stride_category")
	waived_finding = finding.get("finding")
	normalized.append(
		{
			"finding_id": finding_id,
			"file": relative_file,
			"line": line_number,
			"owasp_or_stride_category": category,
			"severity": text_field(finding, "severity"),
			"exploit_scenario": text_field(finding, "exploit_scenario") or (text_field(waived_finding, "exploit_scenario") if isinstance(waived_finding, dict) else ""),
		}
	)
	location = relative_file or "(location not recorded)"
	if relative_file and line_number:
		location = f"{relative_file}:{line_number}"
	prompt_lines.append(
		"- `{id}` | {category} | {severity} | {location}\n"
		"  Accepted exploit: {scenario}\n"
		"  Accepted because: {reason}".format(
			id=finding_id,
			category=category or "uncategorised",
			severity=text_field(finding, "severity") or "unknown",
			location=location,
			scenario=text_field(finding, "exploit_scenario") or (text_field(waived_finding, "exploit_scenario") if isinstance(waived_finding, dict) else "(not recorded)"),
			reason=text_field(finding, "justification") or "(not recorded)",
		)
	)

normalized_path.write_text(json.dumps(normalized, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
prompt_path.write_text("".join(f"{line}\n" for line in prompt_lines), encoding="utf-8")
print(len(normalized))
PY
	)"; then
		security_audit_emit_path_diagnostic "${WAIVED_FINDINGS_ERROR_FILE}"
		security_audit_emit_failure "waived-findings" "${SECURITY_AUDIT_WAIVED_FINDINGS}" "$(head -n1 "${WAIVED_FINDINGS_ERROR_FILE}" 2>/dev/null || echo 'waived findings could not be processed')"
		exit 1
	fi
	[[ "${WAIVED_FINDINGS_COUNT}" =~ ^[0-9]+$ ]] || WAIVED_FINDINGS_COUNT=0
	echo "security-audit: waived-findings=${WAIVED_FINDINGS_COUNT} (line window ${SECURITY_AUDIT_WAIVER_LINE_WINDOW})"
fi

# Full scans export every eligible oversized tracked file; listed prior-finding
# and fix-cycle paths still get the strict explicit-scope checks.
: > "${OVERSIZED_SCOPE_FILE}"
OVERSIZED_EXPORT_SCOPE_MODE="explicit"
if [ "${AUDIT_SCOPE_MODE}" = "incremental" ]; then
	cp "${CHANGED_FILES_FILE}" "${OVERSIZED_SCOPE_FILE}"
else
	OVERSIZED_EXPORT_SCOPE_MODE="all"
fi
cat "${PRIOR_FINDINGS_SCOPE_FILE}" "${FIX_CYCLE_DIFFS_SCOPE_FILE}" >> "${OVERSIZED_SCOPE_FILE}"
if ! PYTHONDONTWRITEBYTECODE=1 python3 "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/codex_isolated_workspace.py" export-oversized \
	"${REPO_ROOT}" "${OVERSIZED_SCOPE_FILE}" "${OVERSIZED_EXPORT_DIR}" \
	"${SECURITY_AUDIT_OVERSIZED_FILE_MAX_BYTES}" "${SECURITY_AUDIT_OVERSIZED_TOTAL_MAX_BYTES}" \
	"${OVERSIZED_EXPORT_SCOPE_MODE}" 2> "${OVERSIZED_ERROR_FILE}"; then
	security_audit_emit_path_diagnostic "${OVERSIZED_ERROR_FILE}"
	security_audit_emit_failure "oversized-scope" "${REPO_ROOT}" "$(head -n1 "${OVERSIZED_ERROR_FILE}" 2>/dev/null || echo 'oversized export failed')"
	exit 1
fi
if ! OVERSIZED_COUNTS="$(PYTHONDONTWRITEBYTECODE=1 python3 - "${OVERSIZED_EXPORT_DIR}/manifest.json" "${OVERSIZED_EXPORT_DIR}" "${OVERSIZED_PROMPT_FILE}" 2> "${OVERSIZED_ERROR_FILE}" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
scoped = manifest["scoped"]
unscoped = manifest["unscoped_oversized_count"]
if manifest["schema_version"] != "oversized_readonly_export.v1" or not isinstance(scoped, list) or not isinstance(unscoped, int) or unscoped < 0:
	raise ValueError("invalid oversized manifest")
text_capped = manifest.get("unscoped_text_capped_count", unscoped)
if type(text_capped) is not int or not 0 <= text_capped <= unscoped:
	raise ValueError("invalid oversized text coverage count")
if manifest.get("scope_mode", "explicit") not in ("explicit", "all"):
	raise ValueError("invalid oversized manifest scope mode")
lines = []
if scoped:
	lines.extend([
		"These scoped files exceed the 2 MiB snapshot limit and are absent from the workspace copy and git show. Their full contents are in these read-only chunks:",
	])
	for item in scoped:
		chunks = ", ".join(f"{sys.argv[2]}/{chunk['file']} lines {chunk['start_line']}-{chunk['end_line']}" for chunk in item["chunks"])
		lines.append(f"- {item['path']} ({item['size']} bytes): {chunks}")
	lines.extend([
		"Read EVERY chunk of EVERY listed file before concluding it is clean.",
		"Cite the original repository path and line numbers (chunk start_line + offset - 1), never the chunk path.",
	])
if unscoped:
	lines.append(f"Coverage note: {unscoped} tracked files over 2 MiB were not inspected (outside the explicit scope, binary, or over the export caps).")
Path(sys.argv[3]).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
print(len(scoped), unscoped, text_capped)
PY
)" || ! [[ "${OVERSIZED_COUNTS}" =~ ^[0-9]+\ [0-9]+\ [0-9]+$ ]]; then
	security_audit_emit_path_diagnostic "${OVERSIZED_ERROR_FILE}"
	security_audit_emit_failure "oversized-scope" "${REPO_ROOT}" "oversized manifest could not be processed"
	exit 1
fi
read -r OVERSIZED_SCOPED_COUNT OVERSIZED_UNSCOPED_COUNT OVERSIZED_TEXT_CAPPED_COUNT <<< "${OVERSIZED_COUNTS}"
echo "security-audit: oversized scoped=${OVERSIZED_SCOPED_COUNT} unscoped=${OVERSIZED_UNSCOPED_COUNT} text_capped=${OVERSIZED_TEXT_CAPPED_COUNT}"

if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "issues" ] \
		&& [ -z "${SECURITY_AUDIT_TARGET_REF}" ] \
		&& [ "${OVERSIZED_TEXT_CAPPED_COUNT}" -gt 0 ]; then
	# Persist incomplete coverage before invoking the model or publishing
	# findings, so any later failure still forces the next default-branch run
	# back through the full repository.
	{
		cat "${TRACKER_BODY_FILE}"
		echo
		if [ -n "${LAST_AUDITED_SHA}" ]; then
			echo "Last audited commit (managed automatically; do not edit):"
			echo "${LAST_SHA_MARKER_PREFIX}${LAST_AUDITED_SHA} -->"
		fi
		echo "${PARTIAL_COVERAGE_MARKER}"
	} > "${TRACKER_BODY_WITH_SHA_FILE}"
	gh_retry gh issue edit "${TRACKER_NUMBER}" \
		--repo "${GITHUB_REPOSITORY}" \
		--body-file "${TRACKER_BODY_WITH_SHA_FILE}"
fi

SECURITY_AUDIT_RENDER_HELPER="${SECURITY_AUDIT_SUPPORT_DIR}/scripts/render_prompt.sh"
SECURITY_AUDIT_PROMPT_PATH="${SECURITY_AUDIT_SUPPORT_DIR}/prompts/mode-security-audit.txt"
security_audit_require_file "prompt-preflight" "${SECURITY_AUDIT_RENDER_HELPER}"
security_audit_require_file "prompt-preflight" "${SECURITY_AUDIT_PROMPT_PATH}"
security_audit_require_directory "prompt-preflight" "${SECURITY_AUDIT_RUNTIME_DIR}"
security_audit_require_writable_destination "prompt-preflight" "${RENDERED_PROMPT_FILE}"
security_audit_require_writable_destination "prompt-preflight" "${RENDER_PROMPT_ERROR_FILE}"

if bash "${SECURITY_AUDIT_RENDER_HELPER}" "${SECURITY_AUDIT_PROMPT_PATH}" \
		> "${RENDERED_PROMPT_FILE}" 2> "${RENDER_PROMPT_ERROR_FILE}"; then
	:
else
	RENDER_PROMPT_STATUS=$?
	security_audit_emit_path_diagnostic "${RENDER_PROMPT_ERROR_FILE}"
	security_audit_emit_failure "prompt-render" "${SECURITY_AUDIT_PROMPT_PATH}" "prompt renderer exited nonzero"
	exit "${RENDER_PROMPT_STATUS}"
fi

if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "findings-json" ]; then
	printf '%s\n' '{{REFERENCE_SECURITY_MONEY_LENS}}' > "${SECURITY_AUDIT_MONEY_LENS_TEMPLATE_FILE}"
	if bash "${SECURITY_AUDIT_RENDER_HELPER}" "${SECURITY_AUDIT_MONEY_LENS_TEMPLATE_FILE}" \
			> "${SECURITY_AUDIT_MONEY_LENS_FILE}" 2> "${RENDER_PROMPT_ERROR_FILE}"; then
		:
	else
		MONEY_LENS_RENDER_STATUS=$?
		security_audit_emit_path_diagnostic "${RENDER_PROMPT_ERROR_FILE}"
		security_audit_emit_failure "prompt-render" "${SECURITY_AUDIT_MONEY_LENS_TEMPLATE_FILE}" "security money lens renderer exited nonzero"
		exit "${MONEY_LENS_RENDER_STATUS}"
	fi
fi

if security_audit_append_prompt_context >> "${RENDERED_PROMPT_FILE}"; then
	:
else
	PROMPT_CONTEXT_STATUS=$?
	security_audit_emit_failure "prompt-render" "${RENDERED_PROMPT_FILE}" "prompt context assembly exited nonzero"
	exit "${PROMPT_CONTEXT_STATUS}"
fi

# --- Engine: Claude first (SECURITY_AUDIT role), codex on any failure ------
# The role resolves through scripts/ai_engine.sh from the trusted support
# tree: the engine config, the project's labels in AI_ENGINE_LABELS (`ai:codex`
# keeps the audit on codex), AI_ENGINE_SECURITY_AUDIT, AI_ENGINE. Claude runs
# in the same credential-free, network-isolated container as codex
# (claude_run, read profile) on the role's model and effort. Every Claude
# failure -- the account pool gated at gate_utilization (CLAUDE_POOL_REASON=
# all_gated), no usable account, no isolation, a crash, a timeout, or output
# that is missing or is not a JSON array of objects -- logs
# AI_ENGINE_FALLBACK role=SECURITY_AUDIT. Under AI_ENGINE_FALLBACK_POLICY=
# capacity (the default; plan item 3e, D1) only the capacity reasons
# (all_gated, all_usage_limit) rerun the same prompt on codex; every other
# reason logs ::error::AI_ENGINE_FALLBACK_REFUSED and fails the audit.
# AI_ENGINE_FALLBACK_POLICY=always reruns on codex for every reason.
CLAUDE_AUDIT_OUTPUT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/claude-output.txt"
SECURITY_AUDIT_ENGINE="codex"
SECURITY_AUDIT_AI_ENGINE_SCRIPT="${SECURITY_AUDIT_SUPPORT_DIR}/scripts/ai_engine.sh"
if [ -f "${SECURITY_AUDIT_AI_ENGINE_SCRIPT}" ]; then
	# shellcheck source=/dev/null
	if source "${SECURITY_AUDIT_AI_ENGINE_SCRIPT}"; then
		SECURITY_AUDIT_ENGINE="$(AI_ENGINE_MODEL_HINT="" AI_ENGINE_EFFORT_HINT="" ai_engine_for_role SECURITY_AUDIT || echo codex)"
	else
		echo "security-audit: ai_engine.sh could not be loaded; running codex" >&2
	fi
else
	echo "security-audit: ai_engine.sh not found in the support tree; running codex" >&2
fi
[ "${SECURITY_AUDIT_ENGINE}" = "claude" ] || SECURITY_AUDIT_ENGINE="codex"

# Returns 0 with the findings array in CODEX_OUTPUT_FILE (the file the
# post-filter reads), 1 after logging a fallback that may run codex, or 2
# after logging a refused fallback (AI_ENGINE_FALLBACK_EXIT=76): the audit
# then fails closed instead of running codex.
security_audit_try_claude()
{
	local claude_rc=0 claude_include="" claude_check_reason=""
	if [ "${CLAUDE_POOL_REASON:-}" = "all_gated" ]; then
		ai_engine_fallback SECURITY_AUDIT all_gated
		[ "${AI_ENGINE_FALLBACK_EXIT:-75}" -eq 76 ] && return 2
		return 1
	fi
	if [ "${OVERSIZED_SCOPED_COUNT}" -gt 0 ]; then
		claude_include="${OVERSIZED_EXPORT_DIR}"
	fi
	AI_ENGINE_MODEL_HINT="" AI_ENGINE_EFFORT_HINT="" AI_ENGINE_READ_ONLY="true" \
		AI_ENGINE_INCLUDE_PATHS="${claude_include}" \
		claude_run SECURITY_AUDIT "${RENDERED_PROMPT_FILE}" "${CLAUDE_AUDIT_OUTPUT_FILE}" "${PWD}" || claude_rc=$?
	case "${claude_rc}" in
		0) ;;
		# claude_run already logged AI_ENGINE_FALLBACK with its reason; 76
		# also logged AI_ENGINE_FALLBACK_REFUSED.
		75) return 1 ;;
		76) return 2 ;;
		124) ai_engine_fallback SECURITY_AUDIT timeout ;;
		*) ai_engine_fallback SECURITY_AUDIT "crashed_rc_${claude_rc}" ;;
	esac
	if [ "${claude_rc}" -ne 0 ]; then
		[ "${AI_ENGINE_FALLBACK_EXIT:-75}" -eq 76 ] && return 2
		return 1
	fi
	# Same top-level contract the post-filter enforces on codex output: a JSON
	# array. One outer ```json fence is stripped; anything else falls back.
	if ! claude_check_reason="$(python3 - "${CLAUDE_AUDIT_OUTPUT_FILE}" "${CODEX_OUTPUT_FILE}" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
	text = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
except OSError:
	text = ""
if not text:
	print("missing_output")
	sys.exit(1)
fenced = re.fullmatch(r"```json[ \t]*\n(.*)\n```", text, re.S)
if fenced:
	text = fenced.group(1).strip()
try:
	findings = json.loads(text)
except ValueError:
	print("malformed_output")
	sys.exit(1)
if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
	print("schema_mismatch")
	sys.exit(1)
Path(sys.argv[2]).write_text(json.dumps(findings) + "\n", encoding="utf-8")
print("ok")
PY
)"; then
		ai_engine_fallback SECURITY_AUDIT "${claude_check_reason:-malformed_output}"
		[ "${AI_ENGINE_FALLBACK_EXIT:-75}" -eq 76 ] && return 2
		return 1
	fi
	return 0
}

SECURITY_AUDIT_CLAUDE_RC=1
if [ "${SECURITY_AUDIT_ENGINE}" = "claude" ]; then
	security_audit_try_claude && SECURITY_AUDIT_CLAUDE_RC=0 || SECURITY_AUDIT_CLAUDE_RC=$?
fi
if [ "${SECURITY_AUDIT_CLAUDE_RC}" -eq 2 ]; then
	# A refused (non-capacity) Claude fallback never reruns on codex (D1).
	security_audit_emit_failure "claude-engine" "${CLAUDE_AUDIT_OUTPUT_FILE}" "Claude engine unavailable for a non-capacity reason (AI_ENGINE_FALLBACK_REFUSED); codex fallback refused"
	exit 1
fi
if [ "${SECURITY_AUDIT_CLAUDE_RC}" -eq 0 ]; then
	echo "security-audit: engine=claude"
else
	security_audit_require_file "codex-preflight" "${RENDERED_PROMPT_FILE}"
	SECURITY_AUDIT_CODEX_HOME="${CODEX_HOME:-${HOME:-}/.codex}"
	security_audit_require_directory "codex-preflight" "${SECURITY_AUDIT_CODEX_HOME}"
	security_audit_require_file "codex-preflight" "${SECURITY_AUDIT_CODEX_HOME}/config.toml"
	if ! command -v codex >/dev/null 2>&1; then
		security_audit_emit_failure "codex-preflight" "codex" "required command is unavailable"
		exit 1
	fi
	security_audit_require_writable_destination "codex-preflight" "${CODEX_OUTPUT_FILE}"
	security_audit_require_writable_destination "codex-preflight" "${CODEX_ERROR_FILE}"

	# The audited code is untrusted input, so the agent runs in the
	# credential-free, network-isolated container (read-only snapshot of the
	# audit checkout), launched from the trusted support checkout.
	audit_isolated_args=()
	if [ "${OVERSIZED_SCOPED_COUNT}" -gt 0 ]; then
		audit_isolated_args=(--include "${OVERSIZED_EXPORT_DIR}")
	fi
	if bash "${SECURITY_AUDIT_SUPPORT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/codex_isolated_exec.sh" run --mode read-only ${audit_isolated_args[@]+"${audit_isolated_args[@]}"} -- \
			--ask-for-approval never \
			-c model_verbosity=low \
			-c include_apply_patch_tool=true \
			exec \
			--skip-git-repo-check \
			--model "${WORKFLOW_EDITOR_MODEL:-openai/gpt-6-sol}" \
			--sandbox read-only < "${RENDERED_PROMPT_FILE}" \
			> "${CODEX_OUTPUT_FILE}" 2> "${CODEX_ERROR_FILE}"; then
		:
	else
		CODEX_EXECUTION_STATUS=$?
		CODEX_TAIL_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/codex-stderr-tail.txt"
		CODEX_PROVIDER_CLASS="$(security_audit_emit_codex_stderr_tail "${CODEX_ERROR_FILE}" "${RENDERED_PROMPT_FILE}" "${CODEX_TAIL_FILE}")" || CODEX_PROVIDER_CLASS="unknown"
		security_audit_emit_path_diagnostic "${CODEX_TAIL_FILE}" "sanitized-tail"
		security_audit_emit_failure "codex-execution" "codex" "Codex exited nonzero" "${CODEX_PROVIDER_CLASS}"
		exit "${CODEX_EXECUTION_STATUS}"
	fi
	echo "security-audit: engine=codex"
fi

python3 - \
	"${REPO_ROOT}" \
	"${CODEX_OUTPUT_FILE}" \
	"${SECURITY_AUDIT_FP_EXCLUSIONS}" \
	"${SECURITY_AUDIT_CONFIDENCE_GATE}" \
	"${FILTERED_FINDINGS_FILE}" \
	"${FILTER_SUMMARY_FILE}" \
	"${AUDIT_SCOPE_MODE}" \
	"${CHANGED_FILES_FILE}" \
	"${WAIVED_FINDINGS_NORMALIZED_FILE}" \
	"${SECURITY_AUDIT_WAIVER_LINE_WINDOW}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath

repo_root = Path(sys.argv[1]).resolve()
codex_output_path = Path(sys.argv[2])
exclusions_path = Path(sys.argv[3])
confidence_gate = int(sys.argv[4])
filtered_findings_path = Path(sys.argv[5])
summary_path = Path(sys.argv[6])
audit_scope_mode = sys.argv[7]
changed_files_path = Path(sys.argv[8])
waived_findings_path = Path(sys.argv[9])
waiver_line_window = int(sys.argv[10])

# Incremental scope is enforced here deterministically: even if the model
# ignores the prompt's changed-file restriction, out-of-scope findings never
# reach the tracker or follow-up issues.
changed_files: set[str] = set()
if audit_scope_mode == "incremental":
	try:
		changed_files = {
			line.strip()
			for line in changed_files_path.read_text(encoding="utf-8").splitlines()
			if line.strip()
		}
	except OSError as exc:
		raise SystemExit(f"unable to read changed-files list: {exc}")

severity_rank = {
	"critical": 0,
	"high": 1,
	"medium": 2,
	"low": 3,
}
allowed_exact_fields = {"finding_id", "owasp_or_stride_category", "severity", "file"}
allowed_contains_fields = {"exploit_scenario", "recommendation"}


def fail(message: str) -> None:
	raise SystemExit(message)


def load_json(path: Path, *, label: str):
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except OSError as exc:
		fail(f"unable to read {label}: {exc}")
	except json.JSONDecodeError as exc:
		fail(f"invalid JSON in {label}: {exc}")


def normalize_exclusions(raw_exclusions: object) -> list[dict[str, object]]:
	if not isinstance(raw_exclusions, dict):
		fail("security-audit exclusions must be a JSON object")
	if raw_exclusions.get("schema_version") != "security_audit_fp_exclusions.v1":
		fail("unsupported security-audit exclusions schema_version")
	rules = raw_exclusions.get("rules")
	if not isinstance(rules, list):
		fail("security-audit exclusions rules must be a JSON array")
	normalized_rules: list[dict[str, object]] = []
	for rule in rules:
		if not isinstance(rule, dict):
			fail("each security-audit exclusion rule must be an object")
		rule_id = str(rule.get("id") or "").strip()
		reason = str(rule.get("reason") or "").strip()
		fields = rule.get("fields") or {}
		contains = rule.get("contains") or {}
		if not rule_id or not reason:
			fail("security-audit exclusion rules require non-empty id and reason")
		if not isinstance(fields, dict) or not isinstance(contains, dict):
			fail(f"security-audit exclusion rule {rule_id} must use object fields/contains matchers")
		normalized_fields: dict[str, str] = {}
		for key, value in fields.items():
			if key not in allowed_exact_fields:
				fail(f"security-audit exclusion rule {rule_id} uses unsupported exact field {key}")
			if not isinstance(value, str) or not value.strip():
				fail(f"security-audit exclusion rule {rule_id} must use non-empty string exact matches")
			normalized_fields[key] = value.strip()
		normalized_contains: dict[str, list[str]] = {}
		for key, value in contains.items():
			if key not in allowed_contains_fields:
				fail(f"security-audit exclusion rule {rule_id} uses unsupported contains field {key}")
			if isinstance(value, str):
				values = [value]
			elif isinstance(value, list):
				values = value
			else:
				fail(f"security-audit exclusion rule {rule_id} must use string or string-list contains values")
			needles: list[str] = []
			for needle in values:
				if not isinstance(needle, str) or not needle.strip():
					fail(f"security-audit exclusion rule {rule_id} contains entries must be non-empty strings")
				needles.append(needle.strip().lower())
			normalized_contains[key] = needles
		normalized_rules.append(
			{
				"id": rule_id,
				"reason": reason,
				"fields": normalized_fields,
				"contains": normalized_contains,
			}
		)
	return normalized_rules


def normalize_finding(raw_finding: object) -> tuple[dict[str, object] | None, str | None]:
	if not isinstance(raw_finding, dict):
		return None, "finding must be an object"
	finding_id = str(raw_finding.get("finding_id") or "").strip()
	category = str(raw_finding.get("owasp_or_stride_category") or "").strip()
	severity = str(raw_finding.get("severity") or "").strip().lower()
	exploit_scenario = str(raw_finding.get("exploit_scenario") or "").strip()
	recommendation = str(raw_finding.get("recommendation") or "").strip()
	file_value = str(raw_finding.get("file") or "").strip()
	confidence = raw_finding.get("confidence")
	line = raw_finding.get("line")

	if not finding_id:
		return None, "finding_id is required"
	if not category:
		return None, f"{finding_id}: owasp_or_stride_category is required"
	if severity not in severity_rank:
		return None, f"{finding_id}: severity must be one of critical/high/medium/low"
	if isinstance(confidence, bool) or not isinstance(confidence, int) or confidence < 1 or confidence > 10:
		return None, f"{finding_id}: confidence must be an integer from 1 to 10"
	if isinstance(line, bool) or not isinstance(line, int) or line < 1:
		return None, f"{finding_id}: line must be a positive integer"
	if not exploit_scenario:
		return None, f"{finding_id}: exploit_scenario is required"
	if not recommendation:
		return None, f"{finding_id}: recommendation is required"
	if not file_value:
		return None, f"{finding_id}: file is required"

	try:
		candidate_path = PurePosixPath(file_value)
	except Exception as exc:
		return None, f"{finding_id}: invalid file path: {exc}"

	if candidate_path.is_absolute() or ".." in candidate_path.parts:
		return None, f"{finding_id}: file must be a repository-relative path"

	normalized_file = candidate_path.as_posix()
	if normalized_file.startswith("./"):
		normalized_file = normalized_file[2:]
	if not normalized_file or normalized_file == ".":
		return None, f"{finding_id}: file must resolve to a repository file"

	resolved_path = (repo_root / normalized_file).resolve()
	try:
		resolved_path.relative_to(repo_root)
	except ValueError:
		return None, f"{finding_id}: file escapes repository root"
	if not resolved_path.is_file():
		return None, f"{finding_id}: file does not exist in checkout"

	try:
		line_count = len(resolved_path.read_text(encoding="utf-8", errors="replace").splitlines())
	except OSError as exc:
		return None, f"{finding_id}: unable to read file: {exc}"
	if line_count < 1:
		return None, f"{finding_id}: file is empty and cannot back a concrete line reference"
	if line > line_count:
		return None, f"{finding_id}: line {line} exceeds file length {line_count}"

	return (
		{
			"finding_id": finding_id,
			"owasp_or_stride_category": category,
			"severity": severity,
			"confidence": confidence,
			"file": normalized_file,
			"line": line,
			"exploit_scenario": exploit_scenario,
			"recommendation": recommendation,
		},
		None,
	)


def matching_exclusion_rule(finding: dict[str, object], rules: list[dict[str, object]]) -> str | None:
	for rule in rules:
		rule_fields = rule["fields"]
		rule_contains = rule["contains"]
		matched = True
		for field_name, expected_value in rule_fields.items():
			if str(finding.get(field_name) or "") != str(expected_value):
				matched = False
				break
		if not matched:
			continue
		for field_name, needles in rule_contains.items():
			haystack = str(finding.get(field_name) or "").lower()
			if not all(needle in haystack for needle in needles):
				matched = False
				break
		if matched:
			return str(rule["id"])
	return None


raw_findings = load_json(codex_output_path, label="Codex output")
exclusion_rules = normalize_exclusions(load_json(exclusions_path, label="security-audit exclusions"))
waived_findings_input = load_json(waived_findings_path, label="waived findings") if waived_findings_path.is_file() else []
if not isinstance(waived_findings_input, list):
	fail("waived findings must be a JSON array")


def matching_waiver(finding: dict[str, object]) -> str | None:
	"""Return the waived finding_id this finding re-reports, if any.

	Exact id first; otherwise the same file and category within the line
	window of the waived line, because the auditor mints a new id on every
	run and a fix commit shifts the cited line.
	"""
	finding_id = str(finding.get("finding_id") or "")
	finding_file = str(finding.get("file") or "")
	finding_category = " ".join(str(finding.get("owasp_or_stride_category") or "").lower().split())
	finding_line = int(finding.get("line") or 0)
	for waiver in waived_findings_input:
		if not isinstance(waiver, dict):
			continue
		waived_id = str(waiver.get("finding_id") or "")
		waived_file = str(waiver.get("file") or "")
		waived_category = " ".join(str(waiver.get("owasp_or_stride_category") or "").lower().split())
		waived_severity = str(waiver.get("severity") or "").strip().lower()
		waived_scenario = " ".join(str(waiver.get("exploit_scenario") or "").lower().split())
		finding_scenario = " ".join(str(finding.get("exploit_scenario") or "").lower().split())
		if (waived_id and waived_id == finding_id
			and (not waived_category or waived_category == finding_category)
			and (not waived_severity or waived_severity == str(finding.get("severity") or "").strip().lower())
			and (not waived_scenario or waived_scenario == finding_scenario)):
			return waived_id
		waived_line = waiver.get("line")
		if not waived_file or not waived_category or not isinstance(waived_line, int) or isinstance(waived_line, bool) or waived_line < 1:
			continue
		if (waived_file == finding_file and waived_category == finding_category
			and waived_severity == str(finding.get("severity") or "").strip().lower()
			and waived_scenario and waived_scenario == finding_scenario
			and abs(waived_line - finding_line) <= waiver_line_window):
			return waived_id or "(unnamed waiver)"
	return None


if not isinstance(raw_findings, list):
	fail("security-audit Codex output must be a JSON array")

kept_findings: list[dict[str, object]] = []
invalid_findings: list[dict[str, str]] = []
excluded_findings: list[dict[str, str]] = []
low_confidence_findings: list[str] = []
out_of_scope_findings: list[str] = []
waived_findings: list[dict[str, str]] = []
seen_ids: set[str] = set()

for raw_finding in raw_findings:
	normalized_finding, error_message = normalize_finding(raw_finding)
	if normalized_finding is None:
		invalid_findings.append({"error": error_message or "invalid finding"})
		continue
	finding_id = str(normalized_finding["finding_id"])
	if finding_id in seen_ids:
		invalid_findings.append({"error": f"{finding_id}: duplicate finding_id"})
		continue
	seen_ids.add(finding_id)
	if audit_scope_mode == "incremental" and str(normalized_finding["file"]) not in changed_files:
		out_of_scope_findings.append(finding_id)
		continue
	if int(normalized_finding["confidence"]) < confidence_gate:
		low_confidence_findings.append(finding_id)
		continue
	rule_id = matching_exclusion_rule(normalized_finding, exclusion_rules)
	if rule_id is not None:
		excluded_findings.append({"finding_id": finding_id, "rule_id": rule_id})
		continue
	waived_id = matching_waiver(normalized_finding)
	if waived_id is not None:
		waived_findings.append({"finding_id": finding_id, "waived_finding_id": waived_id})
		continue
	kept_findings.append(normalized_finding)

kept_findings.sort(
	key=lambda finding: (
		severity_rank[str(finding["severity"])],
		-int(finding["confidence"]),
		str(finding["file"]),
		int(finding["line"]),
		str(finding["finding_id"]),
	)
)

filtered_findings_path.write_text(
	json.dumps(kept_findings, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
	encoding="utf-8",
)
summary_path.write_text(
	json.dumps(
		{
			"kept": len(kept_findings),
			"suppressed_low_confidence": len(low_confidence_findings),
			"suppressed_excluded": len(excluded_findings),
			"suppressed_invalid": len(invalid_findings),
			"suppressed_out_of_scope": len(out_of_scope_findings),
			"suppressed_waived": len(waived_findings),
			"low_confidence_finding_ids": low_confidence_findings,
			"excluded": excluded_findings,
			"invalid": invalid_findings,
			"out_of_scope_finding_ids": out_of_scope_findings,
			"waived": waived_findings,
		},
		ensure_ascii=True,
		indent=2,
		sort_keys=True,
	)
	+ "\n",
	encoding="utf-8",
)
PY

# --- Line ownership (plan item 4b, decision D4) -----------------------------
# Effective only for findings-json audits of an explicit range; otherwise the
# mode is `off` and the payload is unchanged.  Blame always runs against the
# explicit range base (the integration merge-base), never
# SECURITY_AUDIT_DIFF_SINCE, so project code from earlier fix cycles stays
# project-written.  Each finding cites exactly one line, so "every cited line"
# (D4) and "any cited line" (issue wording) are the same test here.
SECURITY_AUDIT_LINE_OWNERSHIP_EFFECTIVE="off"
if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "findings-json" ] \
		&& [ -n "${SECURITY_AUDIT_DIFF_BASE}" ] \
		&& [ "${SECURITY_AUDIT_LINE_OWNERSHIP}" = "project" ]; then
	SECURITY_AUDIT_LINE_OWNERSHIP_EFFECTIVE="project"
fi
if [ "${SECURITY_AUDIT_LINE_OWNERSHIP_EFFECTIVE}" = "project" ]; then
	if LINE_OWNERSHIP_LOG="$(PYTHONDONTWRITEBYTECODE=1 python3 - \
		"${REPO_ROOT}" \
		"${FILTERED_FINDINGS_FILE}" \
		"${LINE_OWNERSHIP_SUMMARY_FILE}" \
		"${AUDIT_SCOPE_BASE_SHA}" \
		"${AUDIT_SCOPE_HEAD_SHA}" \
		"${SECURITY_AUDIT_LINE_OWNERSHIP_HUNK_WINDOW}" 2> "${LINE_OWNERSHIP_ERROR_FILE}" <<'PY'
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

repo_root = Path(sys.argv[1])
findings_path = Path(sys.argv[2])
summary_path = Path(sys.argv[3])
base_sha = sys.argv[4].strip()
head_sha = sys.argv[5].strip()
try:
	hunk_window = int(sys.argv[6]) if len(sys.argv) > 6 else 40
except ValueError:
	hunk_window = 40
if hunk_window < 0:
	hunk_window = 40

# Per-call bound on `git blame` (and the one-off rev-list); a timeout marks
# the finding's ownership unknown, which keeps it blocking.
LINE_OWNERSHIP_BLAME_TIMEOUT_SECS = 30
LINE_OWNERSHIP_REVLIST_TIMEOUT_SECS = 120
LINE_OWNERSHIP_DIFF_TIMEOUT_SECS = 120
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git(args: list[str], timeout: int) -> subprocess.CompletedProcess:
	# List-form argv: finding data never reaches a shell.  core.fsmonitor is
	# cleared so no repository-configured hook runs.
	return subprocess.run(
		["git", "-c", "core.fsmonitor=", *args],
		cwd=repo_root,
		capture_output=True,
		text=True,
		encoding="utf-8",
		errors="replace",
		timeout=timeout,
		check=False,
	)


def safe_id(value: object) -> str:
	return re.sub(r"[^A-Za-z0-9._:-]", "_", str(value or "?"))[:120] or "?"


findings = json.loads(findings_path.read_text(encoding="utf-8"))
if not isinstance(findings, list):
	raise SystemExit("filtered findings must be a JSON array")

global_reason = ""
project_commits: set[str] = set()
files_with_deletions: set[str] = set()
hunk_cache: dict[str, list[tuple[int, int]] | None] = {}
if not SHA_RE.match(base_sha) or not SHA_RE.match(head_sha):
	global_reason = "range_unresolved"
else:
	try:
		shallow = git(["rev-parse", "--is-shallow-repository"], LINE_OWNERSHIP_BLAME_TIMEOUT_SECS)
		if shallow.returncode != 0 or shallow.stdout.strip() == "true":
			global_reason = "shallow_history"
		elif git(["merge-base", "--is-ancestor", base_sha, head_sha], LINE_OWNERSHIP_BLAME_TIMEOUT_SECS).returncode != 0:
			global_reason = "base_not_ancestor"
		else:
			revlist = git(["rev-list", "--end-of-options", f"{base_sha}..{head_sha}"], LINE_OWNERSHIP_REVLIST_TIMEOUT_SECS)
			if revlist.returncode != 0:
				global_reason = "rev_list_failed"
			else:
				project_commits = {line.strip() for line in revlist.stdout.splitlines() if SHA_RE.match(line.strip())}
				# One project-wide diff: every path that lost lines (or is
				# binary) in base..head.  --no-renames reports a rename as a
				# full delete plus add, the conservative reading.  A failure
				# keeps every finding blocking.
				numstat = git(
					["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", base_sha, head_sha],
					LINE_OWNERSHIP_DIFF_TIMEOUT_SECS,
				)
				if numstat.returncode != 0:
					global_reason = "diff_failed"
				else:
					for record in numstat.stdout.split("\0"):
						parts = record.split("\t", 2)
						if len(parts) != 3 or not parts[2]:
							continue
						added_count, deleted_count, changed_path = parts
						if deleted_count == "-" or added_count == "-":
							files_with_deletions.add(changed_path)
						elif deleted_count.isdigit() and int(deleted_count) > 0:
							files_with_deletions.add(changed_path)
	except subprocess.TimeoutExpired:
		global_reason = "timeout"


def added_hunks(path: str) -> list[tuple[int, int]] | None:
	# Added line ranges [start, end] of the project diff for one file, or None
	# when the diff cannot be read or parsed (the caller keeps it blocking).
	if path in hunk_cache:
		return hunk_cache[path]
	ranges: list[tuple[int, int]] | None = []
	try:
		diff = git(
			["diff", "-U0", "--no-ext-diff", "--no-textconv", "--no-renames", base_sha, head_sha, "--", path],
			LINE_OWNERSHIP_DIFF_TIMEOUT_SECS,
		)
	except subprocess.TimeoutExpired:
		ranges = None
	else:
		if diff.returncode != 0:
			ranges = None
		else:
			for diff_line in diff.stdout.splitlines():
				if not diff_line.startswith("@@"):
					continue
				match = HUNK_RE.match(diff_line)
				if match is None:
					ranges = None
					break
				start = int(match.group(1))
				count = int(match.group(2)) if match.group(2) is not None else 1
				if count > 0:
					ranges.append((start, start + count - 1))
	hunk_cache[path] = ranges
	return ranges


def finding_strings(value: object) -> list[str]:
	if isinstance(value, str):
		return [value]
	if isinstance(value, dict):
		return [text for item in value.values() for text in finding_strings(item)]
	if isinstance(value, list):
		return [text for item in value for text in finding_strings(item)]
	return []


def deletion_block_reason(finding: dict, path: str, line: int) -> tuple[str, bool]:
	# Returns (reason, unknown) when a pre-project line must stay blocking
	# because the project may have removed or bypassed its protection.
	# Matching only ever adds blocking, never removes it.
	if path in files_with_deletions:
		return "deleted_lines_in_file", False
	ranges = added_hunks(path)
	if ranges is None:
		return "hunk_diff_failed", True
	for start, end in ranges:
		if start - hunk_window <= line <= end + hunk_window:
			return "changed_hunk_within_window", False
	texts = finding_strings({key: value for key, value in finding.items() if key not in ("file", "line")})
	for other_path in files_with_deletions:
		if other_path != path and any(other_path in text for text in texts):
			return "references_file_with_deletions", False
	return "", False

blocking = advisory = unknown = 0
log_lines: list[str] = []
annotated: list[object] = []
for finding in findings:
	if not isinstance(finding, dict):
		annotated.append(finding)
		continue
	reason = global_reason
	is_advisory = False
	if not reason:
		file_value = finding.get("file")
		line_value = finding.get("line")
		if (not isinstance(file_value, str) or not file_value
			or isinstance(line_value, bool) or not isinstance(line_value, int) or line_value < 1):
			reason = "missing_line"
		else:
			try:
				blame = git(
					["blame", "--porcelain", "--no-textconv", "-L", f"{line_value},{line_value}", head_sha, "--", file_value],
					LINE_OWNERSHIP_BLAME_TIMEOUT_SECS,
				)
			except subprocess.TimeoutExpired:
				reason = "timeout"
			else:
				if blame.returncode != 0:
					reason = "missing_line" if "no such path" in blame.stderr or "has only" in blame.stderr else "blame_failed"
				else:
					first_line = blame.stdout.splitlines()[0] if blame.stdout else ""
					commit = first_line.split(" ", 1)[0] if first_line else ""
					if not SHA_RE.match(commit):
						reason = "parse_failed"
					else:
						is_advisory = commit not in project_commits
						if is_advisory:
							block_reason, block_unknown = deletion_block_reason(finding, file_value, line_value)
							if block_reason:
								is_advisory = False
								if block_unknown:
									reason = block_reason
								else:
									log_lines.append(
										f"security-audit: line_ownership_blocking finding={safe_id(finding.get('finding_id'))} reason={block_reason}"
									)
	finding = dict(finding)
	finding["advisory"] = is_advisory
	annotated.append(finding)
	if reason:
		unknown += 1
		log_lines.append(f"security-audit: line_ownership_unknown finding={safe_id(finding.get('finding_id'))} reason={reason}")
	if is_advisory:
		advisory += 1
	else:
		blocking += 1

with tempfile.NamedTemporaryFile(
	mode="w", encoding="utf-8", dir=findings_path.parent, prefix=".line-ownership.", suffix=".tmp", delete=False
) as temporary_file:
	json.dump(annotated, temporary_file, ensure_ascii=True, indent=2, sort_keys=True)
	temporary_file.write("\n")
	temporary_name = temporary_file.name
summary_path.write_text(
	json.dumps({"mode": "project", "blocking": blocking, "advisory": advisory, "unknown": unknown}, sort_keys=True) + "\n",
	encoding="utf-8",
)
os.replace(temporary_name, findings_path)
log_lines.append(f"security-audit: line_ownership mode=project blocking={blocking} advisory={advisory} unknown={unknown}")
print("\n".join(log_lines))
PY
	)"; then
		printf '%s\n' "${LINE_OWNERSHIP_LOG}"
	else
		# Fail safe: the findings file is replaced only after a full pass, so
		# it is untouched here and every finding stays blocking at the poller.
		rm -f "${LINE_OWNERSHIP_SUMMARY_FILE}"
		echo "::warning::security-audit: line_ownership annotation failed; all findings stay blocking"
	fi
fi

if [ "${SECURITY_AUDIT_OUTPUT_MODE}" = "findings-json" ]; then
	if python3 - \
		"${FILTERED_FINDINGS_FILE}" \
		"${FILTER_SUMMARY_FILE}" \
		"${SECURITY_AUDIT_FINDINGS_OUT}" \
		"${OVERSIZED_EXPORT_DIR}/manifest.json" \
		"${LINE_OWNERSHIP_SUMMARY_FILE}" 2> "${FINDINGS_PACKAGE_ERROR_FILE}" <<'PY'
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

findings_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
oversized_manifest_path = Path(sys.argv[4])
line_ownership_summary_path = Path(sys.argv[5]) if len(sys.argv) > 5 else None
count_keys = (
	"kept",
	"suppressed_excluded",
	"suppressed_invalid",
	"suppressed_low_confidence",
	"suppressed_out_of_scope",
	"suppressed_waived",
)


def load_json(path: Path, *, label: str):
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise SystemExit(f"unable to load {label}: {exc}")


findings = load_json(findings_path, label="filtered findings")
summary = load_json(summary_path, label="filter summary")
if not isinstance(findings, list) or not isinstance(summary, dict):
	raise SystemExit("security-audit findings packaging received invalid JSON payloads")

counts: dict[str, int] = {}
for count_key in count_keys:
	count_value = summary.get(count_key)
	if isinstance(count_value, bool) or not isinstance(count_value, int) or count_value < 0:
		raise SystemExit(f"security-audit findings packaging received invalid count: {count_key}")
	counts[count_key] = count_value
if counts["kept"] != len(findings):
	raise SystemExit("security-audit findings packaging received a mismatched kept count")

payload = {
	"schema_version": "security_audit_findings.v1",
	"findings": findings,
	"counts": counts,
}
oversized_manifest = load_json(oversized_manifest_path, label="oversized manifest")
payload["coverage"] = {
	"oversized_threshold_bytes": oversized_manifest["threshold_bytes"],
	"scoped_oversized_chunked": [item["path"] for item in oversized_manifest["scoped"]],
	"unscoped_oversized_skipped": [item["path"] for item in oversized_manifest["unscoped_oversized"]],
	"unscoped_oversized_skipped_count": oversized_manifest["unscoped_oversized_count"],
	"unscoped_text_capped_count": oversized_manifest.get("unscoped_text_capped_count", oversized_manifest["unscoped_oversized_count"]),
}
# Additive (plan item 4b): present only when line ownership annotated the
# findings; SECURITY_AUDIT_LINE_OWNERSHIP=off leaves the payload unchanged.
if line_ownership_summary_path is not None and line_ownership_summary_path.is_file():
	line_ownership = load_json(line_ownership_summary_path, label="line ownership summary")
	if isinstance(line_ownership, dict):
		payload["line_ownership"] = {
			key: line_ownership.get(key) for key in ("mode", "blocking", "advisory", "unknown")
		}

temporary_path: Path | None = None
try:
	with tempfile.NamedTemporaryFile(
		mode="w",
		encoding="utf-8",
		dir=output_path.parent,
		prefix=f".{output_path.name}.",
		suffix=".tmp",
		delete=False,
	) as temporary_file:
		temporary_path = Path(temporary_file.name)
		json.dump(payload, temporary_file, ensure_ascii=True, indent=2, sort_keys=True)
		temporary_file.write("\n")
		temporary_file.flush()
		os.fsync(temporary_file.fileno())
	os.replace(temporary_path, output_path)
except OSError as exc:
	if temporary_path is not None:
		try:
			temporary_path.unlink(missing_ok=True)
		except OSError:
			pass
	raise SystemExit(f"unable to publish security-audit findings: {exc}")
PY
	then
		:
	else
		FINDINGS_PACKAGE_STATUS=$?
		security_audit_emit_path_diagnostic "${FINDINGS_PACKAGE_ERROR_FILE}"
		security_audit_emit_failure "findings-output" "${SECURITY_AUDIT_FINDINGS_OUT}" "findings packaging exited nonzero"
		exit "${FINDINGS_PACKAGE_STATUS}"
	fi
	echo "security-audit: findings-json output written"
	exit 0
fi

# Standalone workflow: no cycle-local issue cache exists here. Fetch every
# existing `ai:security` issue once (open and closed) and reuse the result for
# the finding-marker dedupe. This replaces the former `gh issue list --limit
# 200` call rather than adding one (§15): with no follow-up cap, a repo can
# pass 200 labelled issues, and a truncated list would re-file every finding
# whose marker fell off the end. One paginated REST read, ceil(N/100) calls;
# `--slurp` wraps the pages in one JSON array, and pull requests (which the
# issues endpoint also returns) are skipped by the dedupe below.
gh_retry gh api --method GET --paginate --slurp "repos/${GITHUB_REPOSITORY}/issues" \
	-f labels="ai:security" \
	-f state=all \
	-f per_page=100 > "${EXISTING_FOLLOWUPS_JSON}"

python3 - \
	"${FILTERED_FINDINGS_FILE}" \
	"${FILTER_SUMMARY_FILE}" \
	"${EXISTING_FOLLOWUPS_JSON}" \
	"${TRACKER_NUMBER}" \
	"${TRACKER_COMMENT_FILE}" \
	"${FOLLOWUP_BODY_DIR}" \
	"${FOLLOWUP_INDEX_FILE}" \
	"${FOLLOWUP_SUMMARY_ENV}" \
	"${SECURITY_AUDIT_CONFIDENCE_GATE}" \
	"${SECURITY_AUDIT_FP_EXCLUSIONS}" \
	"${FOLLOWUP_MARKER_PREFIX}" \
	"${AUDIT_SCOPE_MODE}" \
	"${AUDIT_SCOPE_HEAD_SHA}" \
	"${AUDIT_SCOPE_BASE_SHA}" \
	"${SECURITY_AUDIT_TARGET_REF}" \
	"${OVERSIZED_EXPORT_DIR}/manifest.json" <<'PY'
from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

findings_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
existing_followups_path = Path(sys.argv[3])
tracker_number = sys.argv[4]
tracker_comment_path = Path(sys.argv[5])
followup_body_dir = Path(sys.argv[6])
followup_index_path = Path(sys.argv[7])
followup_summary_env_path = Path(sys.argv[8])
confidence_gate = sys.argv[9]
exclusions_path = sys.argv[10]
followup_marker_prefix = sys.argv[11]
audit_scope_mode = sys.argv[12]
head_sha = sys.argv[13].strip()
last_audited_sha = sys.argv[14].strip()
target_ref = sys.argv[15].strip()
oversized_manifest = json.loads(Path(sys.argv[16]).read_text(encoding="utf-8"))


def load_json(path: Path, *, label: str):
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise SystemExit(f"unable to load {label}: {exc}")


def truncate_title(title: str) -> str:
	normalized = " ".join(title.split())
	if len(normalized) <= 250:
		return normalized
	return normalized[:247] + "..."


findings = load_json(findings_path, label="filtered findings")
summary = load_json(summary_path, label="filter summary")
existing_followups = load_json(existing_followups_path, label="existing follow-up issues")

if not isinstance(findings, list) or not isinstance(summary, dict) or not isinstance(existing_followups, list):
	raise SystemExit("security-audit summary generation received invalid JSON payloads")

# `gh api --paginate --slurp` yields one array per page; flatten them.
existing_followup_issues: list[object] = []
for page in existing_followups:
	if isinstance(page, list):
		existing_followup_issues.extend(page)
	else:
		existing_followup_issues.append(page)

marker_regex = re.compile(re.escape(followup_marker_prefix) + r"([^>]+) -->")
existing_finding_ids: set[str] = set()
existing_finding_numbers: dict[str, int] = {}
now_utc = datetime.now(timezone.utc)

for issue in existing_followup_issues:
	if not isinstance(issue, dict) or issue.get("pull_request"):
		continue
	body = str(issue.get("body") or "")
	match = marker_regex.search(body)
	if match is None:
		continue
	finding_id = match.group(1).strip()
	if finding_id:
		existing_finding_ids.add(finding_id)
		if isinstance(issue.get("number"), int) and issue["number"] > 0:
			existing_finding_numbers[finding_id] = issue["number"]

# Every surviving finding without a marked follow-up gets its own issue; there
# is no per-run or per-week cap.
planned_followups: list[dict[str, object]] = []
skipped_existing_count = 0

for finding in findings:
	if not isinstance(finding, dict):
		continue
	finding_id = str(finding.get("finding_id") or "").strip()
	if finding_id in existing_finding_ids:
		skipped_existing_count += 1
		continue
	planned_followups.append(finding)

if target_ref:
	scope_line = f"- Audit scope: branch `{target_ref}` changes since its merge-base with the default branch (`{last_audited_sha}`..`{head_sha}`)"
elif audit_scope_mode == "incremental" and last_audited_sha:
	scope_line = f"- Audit scope: incremental (`{last_audited_sha}`..`{head_sha}`)"
else:
	scope_line = "- Audit scope: full default-branch checkout"

text_capped_count = oversized_manifest.get("unscoped_text_capped_count", oversized_manifest["unscoped_oversized_count"])
text_capped_files = oversized_manifest.get("unscoped_text_capped", [])
partial_coverage_line = ""
if text_capped_count:
	partial_coverage_line = f"- Coverage: partial — {text_capped_count} tracked text files over the export caps were not inspected"
	if text_capped_files:
		partial_coverage_line += ": " + ", ".join(
			f"`{item['path']}` ({item['size']} bytes, {item['reason']})" for item in text_capped_files
		)
	if text_capped_count > len(text_capped_files):
		partial_coverage_line += f" (+{text_capped_count - len(text_capped_files)} more)"
	if not target_ref:
		partial_coverage_line += "; the last-audited-commit marker was not moved, so the next run audits the full repository"

comment_lines = [
	f"## {now_utc.date().isoformat()} Security audit",
	"",
	scope_line,
	f"- Audited commit: `{head_sha or 'n/a'}`",
	*([f"- Oversized scoped files read in chunks: {len(oversized_manifest['scoped'])}"] if oversized_manifest["scoped"] else []),
	*([partial_coverage_line] if partial_coverage_line else []),
	*([f"- Coverage note: {oversized_manifest['unscoped_oversized_count']} tracked files over 2 MiB were not inspected (outside the explicit scope, binary, or over the export caps): " + ", ".join(f"`{item['path']}`" for item in oversized_manifest["unscoped_oversized"]) + (f" (+{oversized_manifest['unscoped_oversized_count'] - len(oversized_manifest['unscoped_oversized'])} more)" if oversized_manifest['unscoped_oversized_count'] > len(oversized_manifest['unscoped_oversized']) else "")] if oversized_manifest["unscoped_oversized_count"] else []),
	f"- Confidence gate: `>= {confidence_gate}`",
	f"- Exclusion catalog: `{exclusions_path}`",
	f"- Findings surfaced: {len(findings)}",
	f"- Suppressed low-confidence findings: {int(summary.get('suppressed_low_confidence', 0))}",
	f"- Suppressed excluded findings: {int(summary.get('suppressed_excluded', 0))}",
	f"- Suppressed invalid findings: {int(summary.get('suppressed_invalid', 0))}",
	f"- Suppressed out-of-scope findings: {int(summary.get('suppressed_out_of_scope', 0))}",
	f"- New follow-up issues planned this run: {len(planned_followups)}",
	f"- Findings skipped because a marked follow-up issue already exists: {skipped_existing_count}",
]

if findings:
	comment_lines.extend(["", "### Findings", ""])
	for idx, finding in enumerate(findings, 1):
		comment_lines.extend(
			[
				f"{idx}. **[{finding['severity']} | {finding['confidence']}/10]** `{finding['file']}:{finding['line']}` — `{finding['owasp_or_stride_category']}`",
				f"   - Finding ID: `{finding['finding_id']}`",
				f"   - Exploit scenario: {finding['exploit_scenario']}",
				f"   - Recommendation: {finding['recommendation']}",
			]
		)
else:
	comment_lines.extend(["", "No findings met the confidence gate after exclusions."])

tracker_comment_path.write_text("\n".join(comment_lines) + "\n", encoding="utf-8")

followup_body_dir.mkdir(parents=True, exist_ok=True)
index_lines: list[str] = []
for idx, finding in enumerate(findings):
	# The index includes existing findings so a retry can continue a chain
	# after a partially successful earlier run without refiling its predecessor.
	file_key = hashlib.sha256(str(finding["file"]).encode("utf-8")).hexdigest()
	finding_id = str(finding["finding_id"])
	if finding_id in existing_finding_ids:
		index_lines.append(f"-\t-\t{file_key}\t{existing_finding_numbers.get(finding_id, 0)}\n")
		continue
	title = truncate_title(
		f"[security-audit] {finding['finding_id']}: {finding['severity']} {finding['file']}:{finding['line']}"
	)
	body_path = followup_body_dir / f"followup-{idx}.md"
	body_lines = [
		f"{followup_marker_prefix}{finding['finding_id']} -->",
		f"Refs #{tracker_number}",
		"",
		"Generated by `.github/workflows/security-audit.yml`.",
		"",
		f"- Category: `{finding['owasp_or_stride_category']}`",
		f"- Severity: `{finding['severity']}`",
		f"- Confidence: `{finding['confidence']}/10`",
		f"- Location: `{finding['file']}:{finding['line']}`",
	]
	if target_ref:
		# resolve_integration_ref.sh reads this line, so clarify / plan /
		# implement check out the audited branch and the fix PR targets it.
		body_lines.append(f"- Integration branch: `{target_ref}`")
	body_lines += [
		"",
		"## Exploit scenario",
		str(finding["exploit_scenario"]),
		"",
		"## Recommendation",
		str(finding["recommendation"]),
	]
	body_path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
	index_lines.append(f"{body_path}\t{title}\t{file_key}\t0\n")

followup_index_path.write_text("".join(index_lines), encoding="utf-8")
finding_ids_b64 = base64.b64encode(
	json.dumps([finding["finding_id"] for finding in findings], ensure_ascii=True).encode("ascii")
).decode("ascii")
followup_summary_env_path.write_text(
	"\n".join(
		[
			f"SURVIVING_FINDINGS_COUNT={shlex.quote(str(len(findings)))}",
			f"SURVIVING_FINDING_IDS_B64={shlex.quote(finding_ids_b64)}",
			f"FOLLOWUP_CREATE_COUNT={shlex.quote(str(len(planned_followups)))}",
		]
	)
	+ "\n",
	encoding="utf-8",
)
PY

gh_retry gh issue comment "${TRACKER_NUMBER}" \
	--repo "${GITHUB_REPOSITORY}" \
	--body-file "${TRACKER_COMMENT_FILE}"

declare -A LAST_FOLLOWUP_BY_FILE=()
while IFS=$'\t' read -r FOLLOWUP_BODY_PATH FOLLOWUP_TITLE FOLLOWUP_FILE_KEY FOLLOWUP_EXISTING_NUMBER; do
	if [ "${FOLLOWUP_EXISTING_NUMBER}" != "0" ]; then
		[[ "${FOLLOWUP_EXISTING_NUMBER}" =~ ^[1-9][0-9]*$ ]] || {
			echo "security-audit: existing follow-up has no verified issue number; refusing to break chain" >&2
			exit 1
		}
		LAST_FOLLOWUP_BY_FILE["${FOLLOWUP_FILE_KEY}"]="${FOLLOWUP_EXISTING_NUMBER}"
		continue
	fi
	[ -n "${FOLLOWUP_BODY_PATH}" ] || continue
	if [ -n "${LAST_FOLLOWUP_BY_FILE[${FOLLOWUP_FILE_KEY}]:-}" ]; then
		printf '\n- Depends on: #%s\n' "${LAST_FOLLOWUP_BY_FILE[${FOLLOWUP_FILE_KEY}]}" >> "${FOLLOWUP_BODY_PATH}"
	fi
	# An ambiguous create result aborts: the next run reconciles by the
	# existing finding marker before trying to create another follow-up.
	# Issue creation is non-idempotent. gh_retry discards a failed attempt's
	# stdout and retries it, which can create a duplicate when gh has already
	# printed the URL. Stop here; next audit reconciles by finding marker.
	FOLLOWUP_URL="$(gh issue create \
		--repo "${GITHUB_REPOSITORY}" \
		--title "${FOLLOWUP_TITLE}" \
		--label "ai:security" \
		--body-file "${FOLLOWUP_BODY_PATH}")"
	case "${FOLLOWUP_URL}" in
		"https://github.com/${GITHUB_REPOSITORY}/issues/"*) ;;
		*) echo "security-audit: unverified follow-up URL; refusing to break chain" >&2; exit 1 ;;
	esac
	FOLLOWUP_NUMBER="${FOLLOWUP_URL##*/}"
	[[ "${FOLLOWUP_NUMBER}" =~ ^[1-9][0-9]*$ ]] || {
		echo "security-audit: unverified follow-up number; refusing to break chain" >&2
		exit 1
	}
	LAST_FOLLOWUP_BY_FILE["${FOLLOWUP_FILE_KEY}"]="${FOLLOWUP_NUMBER}"
done < "${FOLLOWUP_INDEX_FILE}"

if [ -n "${SECURITY_AUDIT_TARGET_REF}" ]; then
	# A branch audit covers a range that is not on the default branch; the
	# marker records default-branch progress only, so leave it untouched.
	echo "security-audit: target_ref=${SECURITY_AUDIT_TARGET_REF}; leaving the tracker's last-audited-commit marker unchanged."
elif [ "${OVERSIZED_TEXT_CAPPED_COUNT:-0}" -gt 0 ]; then
	echo "security-audit: coverage=partial skipped_text=${OVERSIZED_TEXT_CAPPED_COUNT}; leaving the last-audited-commit marker unchanged."
	echo "::warning::Security audit coverage is partial: ${OVERSIZED_TEXT_CAPPED_COUNT} tracked text files over export caps were not inspected; last-audited-commit marker unchanged."
	if declare -F tg_send_msg >/dev/null; then
		tg_send_msg "${GITHUB_REPOSITORY}: security audit coverage partial — ${OVERSIZED_TEXT_CAPPED_COUNT} text files over export caps not inspected; marker unchanged (tracker #${TRACKER_NUMBER})" WARNING >/dev/null 2>&1 || true
	fi
elif [ -n "${HEAD_SHA}" ]; then
	# Persist the audited HEAD SHA on the tracker body so the next run can
	# skip when unchanged or diff-scope against it. One extra `gh issue edit`
	# per completed audit; reads are free because the tracker-discovery
	# `gh issue list` above already returns the body (§15). Runs only after
	# the comment and follow-ups posted, so a failed run re-audits the same
	# range instead of silently advancing the marker.
	{
		cat "${TRACKER_BODY_FILE}"
		echo
		echo "Last audited commit (managed automatically; do not edit):"
		echo "${LAST_SHA_MARKER_PREFIX}${HEAD_SHA} -->"
	} > "${TRACKER_BODY_WITH_SHA_FILE}"
	gh_retry gh issue edit "${TRACKER_NUMBER}" \
		--repo "${GITHUB_REPOSITORY}" \
		--body-file "${TRACKER_BODY_WITH_SHA_FILE}"
fi

# shellcheck disable=SC1090
source "${FOLLOWUP_SUMMARY_ENV}"

echo "security-audit: tracker=#${TRACKER_NUMBER} findings=${SURVIVING_FINDINGS_COUNT} followups_created=${FOLLOWUP_CREATE_COUNT} finding_ids_b64=${SURVIVING_FINDING_IDS_B64}"
