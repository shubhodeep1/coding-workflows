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
# Optional env:
#   COMMITTED_FILES_FILE — path to the "- <file>" list written by
#   scripts/review_commit_changes.sh. When present and non-empty, it
#   lets the shim recognise the case where the editor's productive
#   change WAS committed but only touched the review-issue ledger
#   path (triggering LEDGER_ONLY_COMMIT=true, which in turn runs the
#   caller's detector as a skepticism gate). If every file path
#   referenced in the narrative claims is already in the committed-
#   files list, the edit did land — return "false" so the caller
#   downgrades to an informational log. If any narrative path is
#   missing from the committed set, fall through to "true" so the
#   PR #1472 safety check (editor claimed X, only ledger committed)
#   still fires.
# Output shape:
#   stdout: single token "true" or "false", newline-terminated.
#   exit code: 0 for handled fail-open paths; may be non-zero on
#   unexpected command errors (caller should use `|| echo "true"`).
# Git calls: one (`git status --porcelain`) in the caller's CWD.
# Fail-open: yes — when the summary is unreadable we print "true" and
#   leave the existing heuristic in control.
#
# See fun-token-multi-chain PR #117 (runs 24537598009 / 24540975236)
# for the original reproducing false-positive case. The
# COMMITTED_FILES_FILE subset check addresses the PR #1524 case where
# the editor's productive fix IS to remove the tracked ledger path.
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
	exit 0
fi

# Ledger-delete / productive-ledger-edit escape hatch: when the working
# tree is clean (all claimed edits must already be in HEAD) AND every
# file path referenced in the narrative claims is in the committed-
# files list, the edit did land — treat as false positive. The
# PR #1472 safety check (editor claimed a non-ledger edit but only the
# ledger got committed) is preserved because its narrative path would
# NOT be in the committed set, so the subset check fails and we fall
# through to "true".
if [ -z "${porcelain}" ] && [ -n "${COMMITTED_FILES_FILE:-}" ] && [ -s "${COMMITTED_FILES_FILE:-}" ]; then
	committed_paths="$(grep -E '^-[[:space:]]+' "${COMMITTED_FILES_FILE}" 2>/dev/null \
		| sed -E 's/^-[[:space:]]+//' \
		| grep -vE '^(none([[:space:][:punct:]]|$)|commit skipped)' \
		| while IFS= read -r committed_path; do
			sed -e 's#^\./##' -e 's#//*#/#g' -e 's#/$##' <<< "${committed_path}"
		done \
		| sort -u || true)"
	if [ -n "${committed_paths}" ] && [ -n "${narrative_claims}" ]; then
		# Only extract paths from narrative bullets that actually claim an
		# edit. A bullet like "- Assumption: `foo.txt` is empty" mentions a
		# path but doesn't claim a change to it; counting such paths toward
		# the subset check would incorrectly fail to downgrade legitimate
		# ledger-only commits. The edit-verb filter below mirrors the
		# edit_claim_regex used by the caller's main detector heuristic.
		narrative_paths="$(printf '%s\n' "${narrative_claims}" \
			| grep -iE '\b(modif(y|ied|ies|ying)|updat(e|ed|es|ing)|change(d|s|ing)?|add(ed|s|ing)?|remov(e|ed|es|ing)|delet(e|ed|es|ing)|renam(e|ed|es|ing)|creat(e|ed|es|ing)|fix(ed|es|ing)?|patch(ed|es|ing)?|implement(ed|s|ing)?|refactor(ed|s|ing)?|tweak(ed|s|ing)?|adjust(ed|s|ing)?|improv(e|ed|es|ing)|resolv(e|ed|es|ing))\b' \
			| grep -oE '`[^`]+`|\.?[A-Za-z0-9_][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]*\.[A-Za-z0-9]{1,16}' \
			| tr -d '`' \
			| grep -E '^[^[:space:]]*/[^[:space:]]+$' \
			| grep -vE '^[[:space:]]*$' \
			| while IFS= read -r narrative_path; do
				sed -e 's#^\./##' -e 's#//*#/#g' -e 's#/$##' <<< "${narrative_path}"
			done \
			| sort -u || true)"
		if [ -n "${narrative_paths}" ]; then
			all_in_commit="true"
			while IFS= read -r nar_path; do
				[ -z "${nar_path}" ] && continue
				if ! printf '%s\n' "${committed_paths}" | grep -qxF "${nar_path}"; then
					all_in_commit="false"
					break
				fi
			done <<< "${narrative_paths}"
			if [ "${all_in_commit}" = "true" ]; then
				echo "false"
				exit 0
			fi
		fi
	fi
fi

echo "true"
