#!/usr/bin/env bash
# validate_editor_audit.sh — Shared validator for the "Review file issue
# audit:" section of an editor summary, consumed by:
#
#   1. .github/workflows/review_autofix.yml — "Validate editor no-op
#      disposition" step ("Check 2: Reviewer audit sanity"). Replaces the
#      previously-inlined arithmetic block. Sets EDITOR_NOOP_SUSPICIOUS=true
#      when the helper exits non-zero (caller-side).
#
#   2. scripts/orchestrate_poll_process.sh — noop-suspicious recovery
#      sweep. The poller calls this helper against the latest editor
#      summary PR comment to decide whether a force-merge fallback is
#      safe (audit healthy → fallback eligible; audit not healthy →
#      ERROR alert + leave PR open).
#
# Keeping a single implementation prevents the two callers from
# disagreeing about what "audit healthy" means (per the task spec for
# Fix B in this PR).
#
# Usage as a library (preferred — avoids subshell overhead):
#   source scripts/validate_editor_audit.sh
#   rc=0
#   validate_editor_audit_arithmetic "${EDITOR_SUMMARY_FILE}" \
#     "${REVIEWERS_SUCCESSFUL:-}" || rc=$?
#
# Usage as a script:
#   bash scripts/validate_editor_audit.sh <summary-file> [reviewers_successful]
#
# Arguments:
#   $1 (summary-file)         Path to the editor summary text file. Must
#                             contain a "Review file issue audit:" section.
#                             For the poller, write the latest editor
#                             summary PR comment body to a tempfile and
#                             pass that.
#   $2 (reviewers_successful) Optional integer. When passed and present
#                             in the empty-audit warning, the message is
#                             prefixed with the reviewer count so the
#                             workflow's existing operator alert wording
#                             is preserved byte-for-byte. Omit (or pass
#                             "") for the poller path, which has no
#                             REVIEWERS_SUCCESSFUL env var.
#
# Exit codes:
#   0 — audit section is non-fallback AND every parseable entry's
#       arithmetic balances (total == applied + already_applied + ignored).
#       Audit healthy.
#   1 — audit section is empty or contains only fallback text. The
#       reviewer panel produced no usable audit.
#   2 — at least one audit entry has an arithmetic mismatch OR is
#       malformed/unparseable. The editor claimed disposition counts
#       that don't add up to the total, or emitted a line that the
#       validator cannot safely interpret.
#   3 — usage error (missing/unreadable summary file).
#
# Warning lines emitted to stderr match the legacy inline block in
# review_autofix.yml byte-for-byte so existing cross-workflow grep
# contracts (test_review_autofix_editor_noop_cascade_contract.py and the
# e2e poller in test-and-mark-stable.yml) keep passing.

# Function body: parse summary file's audit section, return per the
# exit-code contract above.
validate_editor_audit_arithmetic()
{
	local summary_file="${1:-}"
	local reviewers_successful="${2:-}"

	if [ -z "${summary_file}" ] || [ ! -f "${summary_file}" ]; then
		echo "::warning::validate_editor_audit_arithmetic: summary file '${summary_file}' missing or unreadable." >&2
		return 3
	fi

	local audit_section
	audit_section="$(awk '
		/^[[:space:]]*Review file issue audit:/ { in_s=1; next }
		in_s && /^[[:space:]]*PR comment audit:/ { exit }
		in_s { print }
	' "${summary_file}")"

	local real_audit
	real_audit="$(printf '%s\n' "${audit_section}" \
		| grep -vE '^[[:space:]]*$' \
		| grep -viE '^[[:space:]]*-[[:space:]]*none([[:space:][:punct:]]|$)' \
		| grep -viE '^[[:space:]]*-[[:space:]]*editor failed([[:space:][:punct:]]|$)' \
		|| true)"

	if [ -z "${real_audit}" ]; then
		# Preserve the workflow's existing warning literal byte-for-byte
		# when called from the workflow (reviewers_successful supplied).
		# The poller path passes "" and gets a generic warning instead;
		# both still grep-match "Editor audit section is empty or
		# contains only fallback text" for operator searches.
		if [[ "${reviewers_successful}" =~ ^[0-9]+$ ]]; then
			echo "::warning::${reviewers_successful} reviewer(s) succeeded but editor audit section is empty or contains only fallback text." >&2
		else
			echo "::warning::Editor audit section is empty or contains only fallback text." >&2
		fi
		return 1
	fi

	local mismatch_found=false
	local line line_lower line_without_already t a aa ig sum
	while IFS= read -r line; do
		[ -n "${line}" ] || continue
		line_lower="$(printf '%s' "${line}" | tr '[:upper:]' '[:lower:]')"
		t=""
		a=""
		aa=""
		ig=""
		line_without_already="${line_lower}"

		if [[ "${line_lower}" =~ total[[:space:]]+issues[[:space:]]+listed[^0-9]*([0-9]+) ]]; then
			t="${BASH_REMATCH[1]}"
		fi
		if [[ "${line_lower}" =~ issues[[:space:]]+already[[:space:]]+applied[^0-9]*([0-9]+) ]]; then
			aa="${BASH_REMATCH[1]}"
			line_without_already="${line_lower/${BASH_REMATCH[0]}/}"
		fi
		if [[ "${line_without_already}" =~ issues[[:space:]]+applied[^0-9]*([0-9]+) ]]; then
			a="${BASH_REMATCH[1]}"
		fi
		if [[ "${line_lower}" =~ issues[[:space:]]+ignored[^0-9]*([0-9]+) ]]; then
			ig="${BASH_REMATCH[1]}"
		fi

		if [ -z "${t}" ] || [ -z "${a}" ] || [ -z "${aa}" ] || [ -z "${ig}" ]; then
			echo "::warning::Audit entry arithmetic mismatch: unparseable audit line: ${line}" >&2
			mismatch_found=true
			continue
		fi

		sum=$((a + aa + ig))

		if [ "${sum}" -ne "${t}" ]; then
			echo "::warning::Audit entry arithmetic mismatch: total=${t} but applied(${a})+already_applied(${aa})+ignored(${ig})=${sum}" >&2
			mismatch_found=true
		fi
	done <<< "${real_audit}"

	if [ "${mismatch_found}" = true ]; then
		return 2
	fi

	return 0
}

# When executed directly (not sourced), dispatch to the function with
# the caller's arguments and exit with its return code. Sourcing the
# file is a pure function-definition side-effect; no work runs.
if [ "${BASH_SOURCE[0]:-$0}" = "${0}" ]; then
	validate_editor_audit_arithmetic "$@"
	exit $?
fi
