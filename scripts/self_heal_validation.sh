#!/usr/bin/env bash
# self_heal_validation.sh — attempt to self-heal a validation failure by
# patching one of the four validation prompt files based on the current
# failure context, then signalling validate_process.sh to re-run.
#
# This script is invoked ONLY by scripts/validate_process.sh, which passes
# the failure context via env vars and reads back the patch decision from
# ${SELF_HEAL_DECISION_FILE}. Callers must check the exit code:
#   0  — patch produced and applied; caller should re-exec validate_process.sh
#   1  — no patch produced; caller should fall through to normal hard-fail
#   2  — hard error inside self-heal (LLM failure, malformed decision, budget
#        exhausted, etc.); caller should fall through to normal hard-fail
#
# Required env vars (set by validate_process.sh):
#   RUNTIME_DIR                — scratch dir shared with the caller
#   SELF_HEAL_ATTEMPT          — current attempt count (0-based, pre-increment)
#   MAX_SELF_HEAL_ATTEMPTS     — budget for this validate_process.sh invocation
#   SELF_HEAL_PATCHES_FILE     — JSONL ledger of accumulated patches
#   SELF_HEAL_FAILURE_PHASE    — string tag ("generate"|"preflight"|"canary"|"diagnose"|"runtime"|"discover")
#   MODEL_EDITOR               — OpenRouter model slug for the self-heal LLM call
#   OPENROUTER_API_KEY         — for codex exec
# Optional:
#   VALIDATION_RESULT_FILE, DIAGNOSE_RESULT_FILE, VALIDATION_LOG_TAIL_FILE,
#   CONTAINER_LOG_TAIL_FILE, VALIDATE_HINTS_FILE, STATIC_CONTEXT_FILE,
#   DISCOVER_OUTPUT_FILE, GENERATE_OUTPUT_FILE, DIAGNOSE_OUTPUT_FILE

set -euo pipefail

: "${RUNTIME_DIR:?RUNTIME_DIR is required}"
: "${SELF_HEAL_ATTEMPT:?SELF_HEAL_ATTEMPT is required}"
: "${MAX_SELF_HEAL_ATTEMPTS:?MAX_SELF_HEAL_ATTEMPTS is required}"
: "${SELF_HEAL_PATCHES_FILE:?SELF_HEAL_PATCHES_FILE is required}"
: "${SELF_HEAL_FAILURE_PHASE:=unknown}"
: "${MODEL_EDITOR:?MODEL_EDITOR is required}"
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY is required}"

command -v jq >/dev/null 2>&1 || { echo "self-heal: jq is required" >&2; exit 2; }
command -v patch >/dev/null 2>&1 || { echo "self-heal: patch is required" >&2; exit 2; }

# Budget check — if we're already at or past the budget, bail out immediately.
if [ "${SELF_HEAL_ATTEMPT}" -ge "${MAX_SELF_HEAL_ATTEMPTS}" ]; then
	echo "self-heal: budget exhausted (attempt=${SELF_HEAL_ATTEMPT} max=${MAX_SELF_HEAL_ATTEMPTS})" >&2
	exit 2
fi

SELF_HEAL_PROMPT_FILE="${RUNTIME_DIR}/validate_self_heal_prompt.txt"
SELF_HEAL_OUTPUT_FILE="${RUNTIME_DIR}/validate_self_heal_output.txt"
SELF_HEAL_LOG_FILE="${RUNTIME_DIR}/validate_self_heal.log"
SELF_HEAL_DECISION_FILE="${RUNTIME_DIR}/validate_self_heal_decision.json"
SELF_HEAL_PATCH_TMP="${RUNTIME_DIR}/validate_self_heal_patch.diff"

ALLOWED_TARGETS=(
	"mode-validate-discover.txt"
	"mode-validate-generate.txt"
	"mode-validate-fix-harness.txt"
	"mode-validate-diagnose.txt"
)

is_allowed_target()
{
	local needle="$1"
	local candidate
	for candidate in "${ALLOWED_TARGETS[@]}"; do
		if [ "${needle}" = "${candidate}" ]; then
			return 0
		fi
	done
	return 1
}

_cat_if_exists()
{
	local label="$1"
	local path="${2:-}"
	if [ -n "${path}" ] && [ -s "${path}" ]; then
		echo "=== ${label} ==="
		cat "${path}"
		echo
	fi
}

_tail_if_exists()
{
	local label="$1"
	local path="${2:-}"
	local n="${3:-200}"
	if [ -n "${path}" ] && [ -s "${path}" ]; then
		echo "=== ${label} (tail ${n}) ==="
		tail -n "${n}" "${path}"
		echo
	fi
}

# Ensure the patches ledger exists.
: > "${SELF_HEAL_LOG_FILE}"
if [ ! -f "${SELF_HEAL_PATCHES_FILE}" ]; then
	: > "${SELF_HEAL_PATCHES_FILE}"
fi

# ---------------------------------------------------------------
# Compose the self-heal prompt
# ---------------------------------------------------------------
{
	_cat_if_exists "STATIC CONTEXT" "${STATIC_CONTEXT_FILE:-}"
	echo
	echo "=== SELF-HEAL TASK ==="
	echo
	bash scripts/render_prompt.sh prompts/mode-validate-self-heal.txt
	echo
	echo "=== SELF-HEAL ATTEMPT ==="
	echo "attempt_number: $((SELF_HEAL_ATTEMPT + 1))"
	echo "max_attempts: ${MAX_SELF_HEAL_ATTEMPTS}"
	echo "failing_phase: ${SELF_HEAL_FAILURE_PHASE}"
	echo
	echo "=== PRIOR ACCUMULATED SELF-HEAL PATCHES (this run) ==="
	if [ -s "${SELF_HEAL_PATCHES_FILE}" ]; then
		cat "${SELF_HEAL_PATCHES_FILE}"
	else
		echo "(none — this is the first self-heal attempt for this run)"
	fi
	echo
	echo "=== CURRENT VALIDATION PROMPT FILES (with any prior self-heal patches already applied) ==="
	for _target in "${ALLOWED_TARGETS[@]}"; do
		if [ -f "prompts/${_target}" ]; then
			echo "--- prompts/${_target} ---"
			cat "prompts/${_target}"
			echo
			echo "--- end prompts/${_target} ---"
			echo
		fi
	done
	echo
	_cat_if_exists "STRUCTURED VALIDATION FAILURE JSON" "${VALIDATION_RESULT_FILE:-}"
	_cat_if_exists "DIAGNOSIS JSON" "${DIAGNOSE_RESULT_FILE:-}"
	_tail_if_exists "VALIDATION LOG TAIL" "${VALIDATION_LOG_TAIL_FILE:-}" 200
	_tail_if_exists "CONTAINER LOG TAIL" "${CONTAINER_LOG_TAIL_FILE:-}" 200
	_cat_if_exists "VALIDATE HINTS" "${VALIDATE_HINTS_FILE:-}"
	_tail_if_exists "DISCOVER OUTPUT" "${DISCOVER_OUTPUT_FILE:-}" 120
	_tail_if_exists "GENERATE OUTPUT" "${GENERATE_OUTPUT_FILE:-}" 120
	_tail_if_exists "DIAGNOSE OUTPUT" "${DIAGNOSE_OUTPUT_FILE:-}" 120
} > "${SELF_HEAL_PROMPT_FILE}" 2>> "${SELF_HEAL_LOG_FILE}"

# ---------------------------------------------------------------
# Call the self-heal LLM (up to 2 attempts to parse valid JSON)
# ---------------------------------------------------------------
SELF_HEAL_LLM_SUCCESS=false
for _llm_attempt in 1 2; do
	echo "self-heal: LLM call attempt ${_llm_attempt}/2" >> "${SELF_HEAL_LOG_FILE}"
	if cat "${SELF_HEAL_PROMPT_FILE}" | codex exec --model "${MODEL_EDITOR}" --full-auto > "${SELF_HEAL_OUTPUT_FILE}" 2>> "${SELF_HEAL_LOG_FILE}"; then
		# Extract the last JSON object that contains a "target_prompt" key.
		# Use json.JSONDecoder.raw_decode() which is string/escape-aware
		# (the earlier brace-depth counter mis-handled JSON strings that
		# contain literal '{' or '}' — unified diffs frequently do).
		if python3 - "${SELF_HEAL_OUTPUT_FILE}" "${SELF_HEAL_DECISION_FILE}" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, 'r', encoding='utf-8', errors='replace') as fh:
    txt = fh.read()

decoder = json.JSONDecoder()

def is_valid_candidate(obj):
    return isinstance(obj, dict) and 'target_prompt' in obj and 'patch' in obj

picked = None

# Fast path: whole output is already valid JSON.
try:
    whole = json.loads(txt)
except Exception:
    whole = None
else:
    if is_valid_candidate(whole):
        picked = whole

# Fallback: scan for each '{' and attempt raw_decode — which correctly
# handles strings, escapes, and nested structures. Keep the LAST valid
# candidate so a trailing JSON block in mixed model output wins.
if picked is None:
    for i, ch in enumerate(txt):
        if ch != '{':
            continue
        try:
            obj, _end = decoder.raw_decode(txt, i)
        except Exception:
            continue
        if is_valid_candidate(obj):
            picked = obj

if picked is None:
    sys.exit(1)
with open(dst, 'w', encoding='utf-8') as fh:
    json.dump(picked, fh, ensure_ascii=False, indent=2)
PY
		then
			SELF_HEAL_LLM_SUCCESS=true
			break
		fi
	fi
	if [ "${_llm_attempt}" -lt 2 ]; then
		sleep $((_llm_attempt * 5))
	fi
done

if [ "${SELF_HEAL_LLM_SUCCESS}" != "true" ]; then
	echo "self-heal: LLM call or JSON parse failed twice; abandoning self-heal" >&2
	exit 2
fi

# ---------------------------------------------------------------
# Validate the decision
# ---------------------------------------------------------------
DECISION_TARGET="$(jq -r '.target_prompt // ""' "${SELF_HEAL_DECISION_FILE}")"
DECISION_CLASS="$(jq -r '.failure_class // "unknown"' "${SELF_HEAL_DECISION_FILE}")"
DECISION_CONF="$(jq -r '.confidence // 0' "${SELF_HEAL_DECISION_FILE}")"
DECISION_RATIONALE="$(jq -r '.rationale // ""' "${SELF_HEAL_DECISION_FILE}")"
DECISION_PATCH="$(jq -r '.patch // ""' "${SELF_HEAL_DECISION_FILE}")"

echo "self-heal: target=${DECISION_TARGET} class=${DECISION_CLASS} confidence=${DECISION_CONF}" >> "${SELF_HEAL_LOG_FILE}"

# Empty patch means "no self-heal proposed" — fall through to hard fail.
if [ -z "${DECISION_PATCH}" ] || [ "${DECISION_PATCH}" = "null" ]; then
	echo "self-heal: empty patch proposed; rationale: ${DECISION_RATIONALE}" >&2
	exit 1
fi

# Target must be one of the four allowed validation prompts.
if ! is_allowed_target "${DECISION_TARGET}"; then
	echo "self-heal: refusing — target_prompt '${DECISION_TARGET}' is not in the self-heal allow-list" >&2
	exit 2
fi

# Write patch to a tmp file and validate.
printf '%s\n' "${DECISION_PATCH}" > "${SELF_HEAL_PATCH_TMP}"

# Reject non-additive diffs explicitly before any dry-run/apply.
# The self-heal prompt's hard constraints require patches to be additive
# clarifications only: no deletions, no renames, no schema/identifier
# changes, no file deletions via /dev/null headers. We enforce the
# syntactic parts of that contract here so a misbehaving self-heal LLM
# cannot remove "Hard constraints" blocks, rename JSON schema fields, or
# delete the target prompt file wholesale.
if grep -Eq '^(---|\+\+\+) /dev/null([[:space:]]|$)' "${SELF_HEAL_PATCH_TMP}"; then
	echo "self-heal: refusing — patch uses /dev/null headers (file add/delete not allowed)" >&2
	exit 2
fi
if grep -Eq '^(deleted file mode|new file mode|rename from |rename to |copy from |copy to )' "${SELF_HEAL_PATCH_TMP}"; then
	echo "self-heal: refusing — patch includes non-additive file operations (rename/copy/delete/new)" >&2
	exit 2
fi
# Reject any deletion lines inside hunks. This catches *all* deletions,
# including lines whose original content begins with '-' (e.g. '--foo'),
# while ignoring file header lines outside hunks.
if awk '
  /^diff --git / { in_hunk=0; next }
  /^@@ / { in_hunk=1; next }
  in_hunk && /^-/ { found=1; exit }
  END { exit(found ? 0 : 1) }
' "${SELF_HEAL_PATCH_TMP}"; then
	echo "self-heal: refusing — patch contains deletion lines; self-heal patches must be additive-only" >&2
	exit 2
fi

# Reject obvious out-of-scope diffs: patch must only touch prompts/<target>.
# Accept a/, b/ prefixes and optional trailing whitespace+timestamp.
# (/dev/null is excluded here because the earlier guard rejects it.)
_expected_re="(a/|b/)?prompts/${DECISION_TARGET}([[:space:]]|\$)"
if grep -E '^(\+\+\+|---) ' "${SELF_HEAL_PATCH_TMP}" | grep -vE "${_expected_re}" >/dev/null 2>&1; then
	echo "self-heal: refusing — patch touches files outside prompts/${DECISION_TARGET}" >&2
	exit 2
fi

# Dry-run the patch.
if ! patch --dry-run -p1 -N -s < "${SELF_HEAL_PATCH_TMP}" >> "${SELF_HEAL_LOG_FILE}" 2>&1; then
	# Try git apply as a fallback (handles different whitespace tolerances).
	if ! git apply --check "${SELF_HEAL_PATCH_TMP}" >> "${SELF_HEAL_LOG_FILE}" 2>&1; then
		echo "self-heal: refusing — patch does not apply cleanly to prompts/${DECISION_TARGET}" >&2
		exit 2
	fi
	_APPLY_WITH_GIT=true
else
	_APPLY_WITH_GIT=false
fi

# ---------------------------------------------------------------
# Apply the patch
# ---------------------------------------------------------------
# Record the target file hash before applying so we can detect the
# "already applied" case that `patch -N` treats as a successful no-op.
# Without this, a model-produced idempotent patch would burn a self-heal
# attempt without changing the prompt, and the re-exec would hit the
# same failure and repeat — silently draining MAX_SELF_HEAL_ATTEMPTS.
_target_file="prompts/${DECISION_TARGET}"
_hash_before=""
if [ -f "${_target_file}" ]; then
	_hash_before="$(sha256sum "${_target_file}" | awk '{print $1}')"
fi

if [ "${_APPLY_WITH_GIT}" = "true" ]; then
	if ! git apply "${SELF_HEAL_PATCH_TMP}" >> "${SELF_HEAL_LOG_FILE}" 2>&1; then
		echo "self-heal: git apply failed after dry-run succeeded (race)" >&2
		exit 2
	fi
else
	if ! patch -p1 -N -s < "${SELF_HEAL_PATCH_TMP}" >> "${SELF_HEAL_LOG_FILE}" 2>&1; then
		echo "self-heal: patch -p1 failed after dry-run succeeded (race)" >&2
		exit 2
	fi
fi

_hash_after=""
if [ -f "${_target_file}" ]; then
	_hash_after="$(sha256sum "${_target_file}" | awk '{print $1}')"
fi
if [ -n "${_hash_before}" ] && [ "${_hash_before}" = "${_hash_after}" ]; then
	echo "self-heal: refusing — patch applied cleanly but left prompts/${DECISION_TARGET} unchanged (likely already-applied / idempotent no-op); treating as no patch to avoid burning budget" >&2
	exit 1
fi

# ---------------------------------------------------------------
# Record the patch in the ledger
# ---------------------------------------------------------------
_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
_ledger_entry="$(jq -cn \
	--arg ts "${_ts}" \
	--arg target "${DECISION_TARGET}" \
	--arg phase "${SELF_HEAL_FAILURE_PHASE}" \
	--arg class "${DECISION_CLASS}" \
	--arg rationale "${DECISION_RATIONALE}" \
	--argjson attempt "$((SELF_HEAL_ATTEMPT + 1))" \
	--argjson confidence "${DECISION_CONF}" \
	--rawfile patch "${SELF_HEAL_PATCH_TMP}" \
	'{
		timestamp: $ts,
		attempt: $attempt,
		failure_phase: $phase,
		failure_class: $class,
		confidence: $confidence,
		target_prompt: $target,
		rationale: $rationale,
		patch: $patch
	}')"
printf '%s\n' "${_ledger_entry}" >> "${SELF_HEAL_PATCHES_FILE}"

echo "self-heal: applied patch to prompts/${DECISION_TARGET} (attempt $((SELF_HEAL_ATTEMPT + 1))/${MAX_SELF_HEAL_ATTEMPTS})" >&2
exit 0
