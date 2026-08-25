#!/usr/bin/env bash
# Consolidate N reviewer outputs from one review pass into a single findings
# ledger via codex-cli (model: openai/gpt-5.6-luna, reasoning: medium).
#
# Invoked twice per review run:
#   --prefix pass1  --output ${CROSS_POLLINATION_FILE}  → feeds pass-2 reviewers
#   --prefix review --output ${REVIEWER_CONSENSUS_FILE} → feeds editor + memory
#
# Reads every ${PREVIOUS_REVIEWS_DIR}/<prefix>_<safe_model>.txt, builds one
# prompt with per-reviewer sentinels, spawns codex-cli with an isolated
# CODEX_HOME so the model/reasoning override cannot leak into the editor
# call. Retries up to 10× with exponential backoff (5s, 10s, 20s, 40s, 80s,
# 160s, 320s, 640s, 1280s between attempts; no cap) on empty stdout /
# non-zero exit / timeout. Hard-fails the workflow on final failure — the
# job-level "Telegram failure" step in review_autofix.yml surfaces the
# incident to the operator. The PR-closed sentinel is polled every 2s during
# each backoff sleep so a mid-retry PR close exits cleanly without waiting
# out the remaining delay.
#
# Env contract:
#   PREVIOUS_REVIEWS_DIR              dir holding <prefix>_*.txt
#   RUNTIME_DIR                       dir for temp codex home + logs
#   SUPPORT_SCRIPTS_DIR               helper scripts (for gh_helpers.sh)
#   CODEX_HOME                        shared codex home (source of config.toml)
#   XPOLL_SUMMARISER_MODEL            default: openai/gpt-5.6-luna
#   XPOLL_SUMMARISER_REASONING        default: medium
#   XPOLL_SUMMARISER_LINES_PER_REVIEWER  target lines per reviewer section (default 160)
#   XPOLL_SUMMARISER_CALL_TIMEOUT_SECS   per-attempt timeout (default 2400)
#   XPOLL_SUMMARISER_MAX_INPUT_LINES     pre-truncate per-reviewer input above this (default 3000)
#   PR_NUMBER                         used to honour /tmp/pr_closed_sentinel_<n>

set -euo pipefail

PREFIX=""
OUTPUT=""
while [ $# -gt 0 ]; do
	case "$1" in
		--prefix)
			PREFIX="$2"; shift 2 ;;
		--output)
			OUTPUT="$2"; shift 2 ;;
		*)
			echo "summarize_reviewer_consensus.sh: unknown arg '$1'" >&2
			exit 2 ;;
	esac
done

case "${PREFIX}" in
	pass1|review) ;;
	*)
		echo "summarize_reviewer_consensus.sh: --prefix must be 'pass1' or 'review' (got '${PREFIX}')" >&2
		exit 2 ;;
esac
if [ -z "${OUTPUT}" ]; then
	echo "summarize_reviewer_consensus.sh: --output is required" >&2
	exit 2
fi

: "${PREVIOUS_REVIEWS_DIR:?PREVIOUS_REVIEWS_DIR must be set}"
: "${RUNTIME_DIR:?RUNTIME_DIR must be set}"

# Source gh_helpers.sh for sanitize_codex_prompt_file (best-effort).
if [ -f "${SUPPORT_SCRIPTS_DIR:-scripts}/gh_helpers.sh" ]; then
	# shellcheck source=gh_helpers.sh
	# shellcheck disable=SC1091
	source "${SUPPORT_SCRIPTS_DIR:-scripts}/gh_helpers.sh" 2>/dev/null || true
fi

SUMMARISER_MODEL="${XPOLL_SUMMARISER_MODEL:-openai/gpt-5.6-luna}"
SUMMARISER_REASONING="${XPOLL_SUMMARISER_REASONING:-medium}"
SUMMARISER_TARGET_PER_REVIEWER="${XPOLL_SUMMARISER_LINES_PER_REVIEWER:-160}"
SUMMARISER_CALL_TIMEOUT="${XPOLL_SUMMARISER_CALL_TIMEOUT_SECS:-2400}"
SUMMARISER_MAX_INPUT_LINES="${XPOLL_SUMMARISER_MAX_INPUT_LINES:-3000}"

if [ -n "${PR_NUMBER:-}" ] && [ -f "/tmp/pr_closed_sentinel_${PR_NUMBER}" ]; then
	echo "summariser (${PREFIX}): PR #${PR_NUMBER} closed — skipping summarisation." >&2
	mkdir -p "$(dirname "${OUTPUT}")"
	printf '(No consensus — PR closed during review.)\n' > "${OUTPUT}"
	exit 0
fi

# ── Gather inputs ────────────────────────────────────────────────────────
shopt -s nullglob
input_files=( "${PREVIOUS_REVIEWS_DIR}/${PREFIX}_"*.txt )
shopt -u nullglob

declare -a reviewer_files=()
declare -a reviewer_slugs=()
for f in "${input_files[@]}"; do
	[ -f "${f}" ] || continue
	# Exclude status/log files that share the prefix.
	case "$(basename "${f}")" in
		status_*|*.log) continue ;;
	esac
	# Drop files containing only a failure marker — nothing to summarise.
	if grep -q '^.*failed after retries' "${f}" 2>/dev/null; then
		continue
	fi
	if [ ! -s "${f}" ]; then
		continue
	fi
	local_slug="$(basename "${f}" .txt)"
	local_slug="${local_slug#${PREFIX}_}"
	reviewer_files+=( "${f}" )
	reviewer_slugs+=( "${local_slug}" )
done

if [ "${#reviewer_files[@]}" -eq 0 ]; then
	echo "summariser (${PREFIX}): no non-empty ${PREFIX}_*.txt inputs — writing empty ledger." >&2
	mkdir -p "$(dirname "${OUTPUT}")"
	{
		echo "=== CONSENSUS FINDINGS ==="
		echo "(No findings reported.)"
		echo "=== END CONSENSUS FINDINGS ==="
		echo
		echo "=== CONSENSUS TASK GAPS ==="
		echo "(No task gaps reported.)"
		echo "=== END CONSENSUS TASK GAPS ==="
	} > "${OUTPUT}"
	exit 0
fi

# ── Build prompt ─────────────────────────────────────────────────────────
n_reviewers="${#reviewer_files[@]}"
target_lines=$(( SUMMARISER_TARGET_PER_REVIEWER * n_reviewers + 120 ))

prompt_file="$(mktemp "${RUNTIME_DIR}/summariser_prompt_${PREFIX}.XXXXXX")"
{
	cat <<PROMPT_HEADER
You compress ${n_reviewers} ${PREFIX} code-review outputs into ONE consolidated
findings ledger plus per-reviewer sections. This is STRICT summarisation plus
cross-reviewer deduplication. Do NOT invent, weaken, strengthen, or drop
findings.

The full untruncated per-reviewer outputs remain on disk at
  ${PREVIOUS_REVIEWS_DIR}/${PREFIX}_<reviewer_slug>.txt
Downstream consumers (pass-2 reviewers or the editor) may open those files if
a ledger entry is ambiguous. Do NOT paraphrase source lines that cite file
paths or line numbers — copy them verbatim.

OUTPUT FORMAT (sentinel-delimited, in this exact order, nothing before or after):

=== CONSENSUS FINDINGS ===
- {file}:{line_range} | severity={low|medium|high|critical} | confidence=[1-5]
  flagged_by: [<reviewer_slug>, <reviewer_slug>, ...]
  PROBLEM: one-sentence statement of the bug
  WHY: one-sentence justification; on severity disagreement use the HIGHEST
       and note the disagreement
- ...
=== END CONSENSUS FINDINGS ===

=== CONSENSUS TASK GAPS ===
- requirement: <verbatim quote or close paraphrase from the LINKED ISSUE / PR DESCRIPTION>
  expected_change_site: <file or symbol where the missing implementation belongs>
  confidence=[1-5]
  flagged_by: [<reviewer_slug>, <reviewer_slug>, ...]
  EVIDENCE: which file(s) / symbol(s) / hunk(s) should contain the implementation but do not
- ...
=== END CONSENSUS TASK GAPS ===

=== FINDINGS FROM <reviewer_slug_1> ===
- {file}:{line_range} | severity=... | confidence=...
  PROBLEM: ...
  WHY: ...
=== END FINDINGS FROM <reviewer_slug_1> ===

(one per-reviewer section per input block, in input order)

How to bucket reviewer entries:
- Entries that follow the standard reviewer issue shape (File:/Line or code reference:/Problem:/Why it fails at runtime:/ISSUE_CONFIDENCE:) belong in CONSENSUS FINDINGS.
- Entries emitted under a reviewer's "TASK COMPLETENESS / INTENT GAPS" checklist heading, or that follow the TASK_GAP shape (Requirement:/Expected change site:/Evidence of absence:/ISSUE_CONFIDENCE:), belong in CONSENSUS TASK GAPS. Do NOT shoehorn a TASK_GAP into CONSENSUS FINDINGS just because it lacks a file:line.
- Always emit BOTH blocks even when one is empty; the empty body is the single line "(No findings reported.)" or "(No task gaps reported.)".

Deduplication rules for the CONSENSUS FINDINGS block:
1. Two findings are duplicates when they refer to the same file AND their line
   ranges overlap OR abut within 5 lines AND they describe the same root cause
   (same class of bug — e.g. both "null-deref on req.user", not merely "bug on
   line 42"). When in doubt, do NOT merge; emit separately.
2. When merging, union flagged_by. Keep file + tightest line range spanning
   all reviewer claims. severity = max across reviewers. confidence = max
   across reviewers. PROBLEM = clearest phrasing.
3. Findings from ONE reviewer still appear in CONSENSUS with flagged_by: [that_slug].
   Do not suppress singletons.

Deduplication rules for the CONSENSUS TASK GAPS block:
A. Two task gaps are duplicates when they describe the same missing deliverable
    (same requirement intent — e.g. both "phone-format validator missing", not merely
    "something missing in user_importer.py") AND they name the same expected change
    site (same file OR same symbol). When in doubt, do NOT merge; emit separately.
B. When merging task gaps, union flagged_by. confidence = max across reviewers.
    Keep the clearest requirement phrasing and the most specific expected_change_site.
    Union the EVIDENCE lines into one comma-separated statement.
C. Singleton task gaps still appear in CONSENSUS TASK GAPS with flagged_by: [that_slug].
    Do not suppress them just because only one reviewer surfaced the gap.

Per-reviewer sections:
4. Preserve EVERY distinct finding from each input block, including TASK_GAP entries
   in their original TASK_GAP shape. If space is tight, prefer high-severity /
   high-confidence first; NEVER drop silently — collapse related items into a
   single bullet suffixed "(N related items)".
5. Keep file paths and line numbers verbatim. Do not guess.
6. Do not invent severity or confidence. Omit the field if the source did not
   state one.
7. If a source block reported no findings, its body is:
   (No findings reported.)

Global constraints:
8. No prose. No preambles. No markdown headers other than the === sentinels.
9. Target total length <= ${target_lines} lines. Count before replying; compress
   per-reviewer sections further (never the CONSENSUS or CONSENSUS TASK GAPS blocks) if over.
10. Emit nothing after the final === END FINDINGS FROM <last_slug> === line.

--- BEGIN INPUTS ---
PROMPT_HEADER

	for i in "${!reviewer_files[@]}"; do
		f="${reviewer_files[$i]}"
		slug="${reviewer_slugs[$i]}"
		echo
		echo "--- BEGIN ${PREFIX^^} OUTPUT FROM ${slug} ---"
		# Per-reviewer pre-truncation keeps the whole prompt within codex-cli
		# context budget even if one reviewer produced an enormous log.
		head -n "${SUMMARISER_MAX_INPUT_LINES}" "${f}"
		echo "--- END ${PREFIX^^} OUTPUT FROM ${slug} ---"
	done
	echo
	echo "--- END INPUTS ---"
} > "${prompt_file}"

echo "summariser (${PREFIX}): ${n_reviewers} input(s); target_lines=${target_lines}; prompt_bytes=$(wc -c < "${prompt_file}")"

# ── Isolated CODEX_HOME with gpt-5.6-luna + medium reasoning ────────────
# Mirrors the reviewer pattern at scripts/review_run_reviewers.sh:906-1024:
# copy the base CODEX_HOME, then sed-patch model_reasoning_effort in the
# config.toml so the editor's shared CODEX_HOME is NEVER mutated.
codex_bin="$(command -v codex || true)"
if [ -z "${codex_bin}" ]; then
	echo "summariser (${PREFIX}): FATAL — codex CLI not in PATH." >&2
	exit 1
fi

summariser_codex_root="${RUNNER_TEMP:-${HOME}/.cache}/codex_home_summariser"
mkdir -p "${summariser_codex_root}"
summariser_codex_home="$(mktemp -d "${summariser_codex_root}/${PREFIX}.XXXXXX")" || {
	echo "summariser (${PREFIX}): FATAL — failed to create temp codex home." >&2
	exit 1
}
trap 'rm -rf "${summariser_codex_home}" "${prompt_file}" 2>/dev/null || true' EXIT

if [ -d "${CODEX_HOME:-}" ]; then
	cp -r "${CODEX_HOME}/." "${summariser_codex_home}/"
fi
mkdir -p "${summariser_codex_home}/bin"

# Patch reasoning effort in the isolated config — same sed recipe the reviewer
# runner uses. Check both top-level and .codex/ nested layouts.
#
# Also pin sandbox_mode to "read-only" in the cloned config. The summariser is
# advisory: its prompt explicitly says "Your output must never be treated as
# a gate" and reviewers run sandbox=read-only for the same reason. The CLI
# already passes `--sandbox read-only` (see the codex invocation below), but
# run 25370115370 demonstrated that the inherited config.toml's
# `sandbox_mode = "workspace-write"` takes precedence over the flag — the
# summariser session header reported `sandbox: workspace-write [...] (network
# access enabled)` and the model used its write access to overwrite
# tests/e2e_smoke_canary.txt via `printf >`. That happened to fix the smoke
# fixture this once but is a latent foot-gun: a future summariser run could
# rewrite arbitrary repo files with no allowlist guard. Fix at config-source.
for cfg in "${summariser_codex_home}/config.toml" "${summariser_codex_home}/.codex/config.toml"; do
	if [ -f "${cfg}" ]; then
		sed -i "s/^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*\".*\"/model_reasoning_effort = \"${SUMMARISER_REASONING}\"/" "${cfg}" 2>/dev/null || true
		if grep -qE '^[[:space:]]*sandbox_mode[[:space:]]*=' "${cfg}" 2>/dev/null; then
			sed -i 's|^[[:space:]]*sandbox_mode[[:space:]]*=.*|sandbox_mode = "read-only"|' "${cfg}" 2>/dev/null || true
		else
			printf '\nsandbox_mode = "read-only"\n' >> "${cfg}" 2>/dev/null || true
		fi
		# Post-edit verification: the sed/printf above silence errors with
		# `2>/dev/null || true` so a permission / disk-full / sed-syntax
		# failure would otherwise leave the config carrying whatever
		# sandbox_mode the inherited file had (including workspace-write,
		# the exact thing this pin is meant to neutralise). Verify the
		# canonical line landed; warn loudly if it didn't so the operator
		# sees the regression in the live job log instead of trusting a
		# silent pin failure. We do NOT hard-fail the summariser step on
		# a missed pin — the summariser is non-gating and the CLI's own
		# `--sandbox read-only` flag still applies; an audible warning is
		# the right severity (matches the pattern in
		# scripts/review_conflict_resolve.sh's reasoning-effort verifier).
		if ! grep -qE '^sandbox_mode = "read-only"$' "${cfg}" 2>/dev/null; then
			echo "::warning::summariser sandbox pin did not take effect on ${cfg}: expected line 'sandbox_mode = \"read-only\"' is missing. Inherited sandbox setting may persist; verify the run's session-header sandbox line." >&2
		fi
	fi
done

# ── Retry loop (10 attempts, exponential backoff 5s→1280s, hard-fail) ────
# Backoff base=5s doubles each failure (5,10,20,40,80,160,320,640,1280); no
# cap. Sentinel is polled every 2s during the sleep so a PR close mid-retry
# exits cleanly without waiting the full remaining delay.
SUMMARISER_MAX_ATTEMPTS=10
SUMMARISER_BACKOFF_BASE_SECS=5
log_file="${RUNTIME_DIR}/summariser_${PREFIX}.log"
: > "${log_file}"

# Interruptible sleep: sleep ${1} seconds total in 2s chunks, bailing out
# early (exit 0 from the script) if the PR-closed sentinel appears.
sentinel_aware_sleep()
{
	local total="$1"
	local slept=0
	local step
	while [ "${slept}" -lt "${total}" ]; do
		if [ -n "${PR_NUMBER:-}" ] && [ -f "/tmp/pr_closed_sentinel_${PR_NUMBER}" ]; then
			echo "summariser (${PREFIX}): PR #${PR_NUMBER} closed during backoff (after ${slept}s of ${total}s) — exiting cleanly." | tee -a "${log_file}" >&2
			mkdir -p "$(dirname "${OUTPUT}")"
			printf '(No consensus — PR closed during review.)\n' > "${OUTPUT}"
			exit 0
		fi
		step=$(( total - slept ))
		if [ "${step}" -gt 2 ]; then
			step=2
		fi
		sleep "${step}"
		slept=$(( slept + step ))
	done
}

attempt=1
last_rc=0
while [ "${attempt}" -le "${SUMMARISER_MAX_ATTEMPTS}" ]; do
	if [ -n "${PR_NUMBER:-}" ] && [ -f "/tmp/pr_closed_sentinel_${PR_NUMBER}" ]; then
		echo "summariser (${PREFIX}): PR #${PR_NUMBER} closed mid-retry — exiting cleanly." | tee -a "${log_file}" >&2
		mkdir -p "$(dirname "${OUTPUT}")"
		printf '(No consensus — PR closed during review.)\n' > "${OUTPUT}"
		exit 0
	fi

	tmp_stdout="$(mktemp)"
	tmp_stderr="$(mktemp)"
	echo "summariser (${PREFIX}): attempt ${attempt}/${SUMMARISER_MAX_ATTEMPTS} — model=${SUMMARISER_MODEL} reasoning=${SUMMARISER_REASONING}" | tee -a "${log_file}"

	last_rc=0
	if command -v sanitize_codex_prompt_file >/dev/null 2>&1; then
		sanitize_codex_prompt_file "${prompt_file}"
	fi
	CODEX_HOME="${summariser_codex_home}" \
		timeout --signal=KILL "${SUMMARISER_CALL_TIMEOUT}" \
		"${codex_bin}" --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${SUMMARISER_MODEL}" --sandbox read-only < "${prompt_file}" \
		> "${tmp_stdout}" 2> "${tmp_stderr}" \
		|| last_rc=$?

	# Append stderr tail for diagnostics regardless of outcome.
	{
		echo "----- attempt ${attempt} stderr tail -n 40 -----"
		tail -n 40 "${tmp_stderr}" 2>/dev/null | sed 's/^/  | /'
		echo "--------------------------------------------"
	} >> "${log_file}"

	if [ "${last_rc}" -eq 0 ] && [ -s "${tmp_stdout}" ]; then
		mkdir -p "$(dirname "${OUTPUT}")"
		mv "${tmp_stdout}" "${OUTPUT}"
		rm -f "${tmp_stderr}"
		echo "summariser (${PREFIX}): success on attempt ${attempt}; output_bytes=$(wc -c < "${OUTPUT}")" | tee -a "${log_file}"
		exit 0
	fi

	if [ "${last_rc}" -eq 124 ] || [ "${last_rc}" -eq 137 ]; then
		echo "summariser (${PREFIX}): attempt ${attempt} timed out after ${SUMMARISER_CALL_TIMEOUT}s." | tee -a "${log_file}" >&2
	elif [ "${last_rc}" -ne 0 ]; then
		echo "summariser (${PREFIX}): attempt ${attempt} exited rc=${last_rc}." | tee -a "${log_file}" >&2
	else
		echo "summariser (${PREFIX}): attempt ${attempt} produced empty stdout (codex-cli returned 0 but emitted no final message)." | tee -a "${log_file}" >&2
	fi

	rm -f "${tmp_stdout}" "${tmp_stderr}"

	# Sleep before the next attempt (skip after the final attempt).
	if [ "${attempt}" -lt "${SUMMARISER_MAX_ATTEMPTS}" ]; then
		backoff_secs=$(( SUMMARISER_BACKOFF_BASE_SECS * (1 << (attempt - 1)) ))
		echo "summariser (${PREFIX}): backing off ${backoff_secs}s before attempt $(( attempt + 1 ))/${SUMMARISER_MAX_ATTEMPTS}." | tee -a "${log_file}" >&2
		sentinel_aware_sleep "${backoff_secs}"
	fi

	attempt=$(( attempt + 1 ))
done

echo "::error::summariser (${PREFIX}): all ${SUMMARISER_MAX_ATTEMPTS} attempts failed (last rc=${last_rc}). See ${log_file}." >&2
# Hard-fail so the workflow job enters failure() and the existing
# "Telegram failure" step at review_autofix.yml:4196-4220 alerts the operator.
exit 1
