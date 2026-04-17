#!/usr/bin/env bash
# Defense-in-depth guard for the EDITOR_CHANGES_LOST detection step in
# .github/workflows/review_autofix.yml.
#
# Given an editor summary file on argv[1], print either:
#   "true"  — the summary + working tree show concrete evidence of edits
#             that were not committed (real changes-lost scenario).
#   "false" — the narrative "Changes made:" block reports no concrete
#             edits AND `git status --porcelain` is empty, so treating
#             this as EDITOR_CHANGES_LOST is a false positive.
#
# The script is intentionally conservative: on any read error or missing
# input it prints "true" (fail-open) so the existing detection behaviour
# is preserved.
#
# Input shape:
#   $1 — path to the editor summary file (EDITOR_SUMMARY_FILE).
# Output shape:
#   stdout: single token "true" or "false", newline-terminated.
#   exit code: 0 for handled fail-open paths; may be non-zero on
#   unexpected command errors (caller should use `|| echo "true"`).
# Git calls: one (`git status --porcelain`) in the caller's CWD.
# Fail-open: yes — when the summary is unreadable we print "true" and
#   leave the existing heuristic in control.
#
# See fun-token-multi-chain PR #117 (runs 24537598009 / 24540975236)
# for the reproducing false-positive case.
set -euo pipefail

summary_file="${1:-}"

if [ -z "${summary_file}" ] || [ ! -s "${summary_file}" ]; then
	echo "true"
	exit 0
fi

if ! porcelain="$(git status --porcelain 2>/dev/null)"; then
	echo "true"
	exit 0
fi

changes_section="$(awk '
	/^[[:space:]]*Changes made:/ { in_section=1; next }
	in_section && /^[[:space:]]*[A-Za-z].*:/ { exit }
	in_section { print }
' "${summary_file}")"

narrative_claims=""
if [ -n "${changes_section}" ]; then
	narrative_claims="$(printf '%s\n' "${changes_section}" | grep -viE '^[[:space:]]*$|^[[:space:]]*-[[:space:]]*none([[:space:]]|$)|^[[:space:]]{2,}-|^[[:space:]]*-[[:space:]]*(Validation executed|Validation limitation|Ran [^:]*(validation|check|test)|Assumptions?( applied| made)|Missing[- ]context|No [^:]*modified|No [^:]*changed|No [^:]*touched|No changes|No modifications)' || true)"
fi

if [ -z "${porcelain}" ] && [ -z "${narrative_claims}" ]; then
	echo "false"
else
	echo "true"
fi
