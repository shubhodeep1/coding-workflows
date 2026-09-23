#!/usr/bin/env bash
set -euo pipefail

SUPPORT_SCRIPTS_DIR="${SUPPORT_SCRIPTS_DIR:-scripts}"
if [ -z "${SUPPORT_ROOT_DIR:-}" ]; then
	if [ "$(basename "${SUPPORT_SCRIPTS_DIR}")" = "scripts" ]; then
		SUPPORT_ROOT_DIR="$(dirname "${SUPPORT_SCRIPTS_DIR}")"
	else
		SUPPORT_ROOT_DIR="${SUPPORT_SCRIPTS_DIR}"
	fi
fi
SUPPORT_PROMPTS_DIR="${SUPPORT_PROMPTS_DIR:-${SUPPORT_ROOT_DIR}/prompts}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/review-synthesise-smoke-${RANDOM}}"
mkdir -p "${RUNTIME_DIR}"
OPENCODE_HELPERS_PATH="${OPENCODE_HELPERS_PATH:-${SUPPORT_SCRIPTS_DIR}/opencode_helpers.sh}"
OPENCODE_CONFIG_WRITER_PATH="${OPENCODE_CONFIG_WRITER_PATH:-${SUPPORT_SCRIPTS_DIR}/write_opencode_config.sh}"
if [ ! -f "${OPENCODE_HELPERS_PATH}" ] || ! source "${OPENCODE_HELPERS_PATH}" 2>/dev/null; then
	printf 'BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=missing_opencode_helpers round=0 path=.ai/review_runtime/pr-unknown/round-unknown/synth_manifest.json\n'
	exit 0
fi
# Preserve the step's original stdout so workflow-command annotations can be
# emitted without being swallowed by command substitutions.
exec 3>&1

behavioural_smoke_log_ok()
{
	printf 'BEHAVIOURAL_SMOKE_SYNTHESISED count=%s round=%s language=%s path=%s\n' \
		"$1" "$2" "$3" "$4"
}

behavioural_smoke_log_fail()
{
	printf 'BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=%s round=%s path=%s\n' \
		"$1" "$2" "$3"
}

behavioural_smoke_emit_warning()
{
	local message="$1"

	message="${message//%/%25}"
	message="${message//$'\r'/%0D}"
	message="${message//$'\n'/%0A}"
	(printf '::warning::%s\n' "${message}" >&3) 2>/dev/null || true
}

behavioural_smoke_has_requirements_files()
{
	compgen -G 'requirements*.txt' >/dev/null 2>&1
}

detect_behavioural_smoke_language()
{
	local requested_language=""

	if [ -n "${BEHAVIOURAL_SMOKE_LANG:-}" ]; then
		requested_language="$(printf '%s' "${BEHAVIOURAL_SMOKE_LANG}" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
		case "${requested_language}" in
			shell|python|javascript)
				printf '%s\n' "${requested_language}"
				return 0
				;;
			*)
				if [ -n "${requested_language}" ]; then
					behavioural_smoke_emit_warning "Invalid BEHAVIOURAL_SMOKE_LANG '${requested_language}'; falling back to repo auto-detection."
				fi
				;;
		esac
	fi

	if [ -f "pyproject.toml" ] || behavioural_smoke_has_requirements_files; then
		printf '%s\n' 'python'
		return 0
	fi

	if [ -f "package.json" ]; then
		printf '%s\n' 'javascript'
		return 0
	fi

	printf '%s\n' 'shell'
}

count_remaining_issues()
{
	local judge_artifact="$1"
	local expected_round="$2"
	local expected_head_sha="$3"

	_opencode_run_isolated_python - "${judge_artifact}" "${expected_round}" "${expected_head_sha}" <<'PY'
import json
import re
import sys

judge_artifact, expected_round_raw, expected_head_sha = sys.argv[1:4]
expected_round = int(expected_round_raw)

with open(judge_artifact, 'r', encoding='utf-8') as handle:
	payload = json.load(handle)

if not isinstance(payload, dict):
	sys.exit(1)
if payload.get('round') != expected_round:
	sys.exit(1)
if payload.get('head_sha') != expected_head_sha:
	sys.exit(1)
remaining = payload.get('remaining_issues')
if not isinstance(remaining, list):
	sys.exit(1)

for issue in remaining:
	if not isinstance(issue, dict):
		sys.exit(1)
	required = {
		'id',
		'file',
		'line_start',
		'line_end',
		'symptom',
		'evidence_quote',
		'severity',
	}
	if not required.issubset(issue.keys()):
		sys.exit(1)
	line_start = issue.get('line_start')
	line_end = issue.get('line_end')
	if type(line_start) is not int or type(line_end) is not int:
		sys.exit(1)
	if line_start < 1 or line_end < line_start:
		sys.exit(1)
	severity = issue.get('severity')
	if severity not in {'must-fix', 'nice-to-have'}:
		sys.exit(1)
	for key in ('id', 'file', 'symptom', 'evidence_quote'):
		value = issue.get(key)
		if not isinstance(value, str):
			sys.exit(1)
		if not re.sub(r'\s+', ' ', value).strip():
			sys.exit(1)

print(len(remaining))
PY
}

extract_and_write_synth_bundle()
{
	local src="$1"
	local judge_artifact="$2"
	local synth_dir="$3"
	local bundle_path="$4"
	local expected_round="$5"
	local expected_head_sha="$6"
	local language_hint="$7"

	_opencode_run_isolated_python - \
		"${src}" \
		"${judge_artifact}" \
		"${synth_dir}" \
		"${bundle_path}" \
		"${expected_round}" \
		"${expected_head_sha}" \
		"${language_hint}" <<'PY'
import json
import re
import stat
import sys
from json import JSONDecoder, JSONDecodeError
from pathlib import Path, PurePosixPath

src, judge_artifact, synth_dir, bundle_path, expected_round_raw, expected_head_sha, language_hint = sys.argv[1:8]
expected_round = int(expected_round_raw)
max_assertions = 100
max_literal_length = 4096
max_reason_length = 512
max_source_bytes = 1_048_576
disallowed_roots = {'.git', '.ai', 'validation'}
repo_root = Path.cwd().resolve()


def squish(value, limit=None):
	text = re.sub(r'\s+', ' ', str(value)).strip()
	if limit is not None and len(text) > limit:
		text = text[: max(limit - 3, 0)].rstrip() + '...'
	return text


def load_raw(path):
	try:
		return Path(path).read_text(encoding='utf-8', errors='replace')
	except OSError:
		return ''


def load_candidates(text):
	candidates = []
	stripped = text.strip()
	if stripped:
		candidates.append(stripped)
	cleaned = re.sub(r'```(?:json)?\s*', '', text)
	cleaned = re.sub(r'```\s*$', '', cleaned, flags=re.MULTILINE).strip()
	if cleaned and cleaned != stripped:
		candidates.append(cleaned)
	decoder = JSONDecoder()
	for opener in ('[', '{'):
		index = -1
		while True:
			index = text.find(opener, index + 1)
			if index == -1:
				break
			try:
				candidate, _ = decoder.raw_decode(text, index)
			except JSONDecodeError:
				continue
			candidates.append(candidate)
	return candidates


def normalize_issue(issue):
	if not isinstance(issue, dict):
		return None
	required = {'id', 'file', 'line_start', 'line_end', 'symptom', 'evidence_quote', 'severity'}
	if not required.issubset(issue):
		return None
	line_start = issue.get('line_start')
	line_end = issue.get('line_end')
	if type(line_start) is not int or type(line_end) is not int or line_start < 1 or line_end < line_start:
		return None
	if issue.get('severity') not in {'must-fix', 'nice-to-have'}:
		return None
	values = [issue.get(key) for key in ('id', 'file', 'symptom', 'evidence_quote')]
	if not all(isinstance(value, str) and squish(value) for value in values):
		return None
	return {
		'id': squish(values[0]),
		'file': squish(values[1]),
		'line_start': line_start,
		'line_end': line_end,
		'symptom': squish(values[2], 200),
		'evidence_quote': squish(values[3], 200),
		'severity': issue['severity'],
	}


def load_judge_payload(path):
	with open(path, 'r', encoding='utf-8') as handle:
		payload = json.load(handle)
	if not isinstance(payload, dict) or payload.get('round') != expected_round or payload.get('head_sha') != expected_head_sha:
		return None
	remaining = payload.get('remaining_issues')
	if not isinstance(remaining, list) or len(remaining) > max_assertions:
		return None
	normalized = [normalize_issue(issue) for issue in remaining]
	if any(issue is None for issue in normalized):
		return None
	return {'round': expected_round, 'head_sha': expected_head_sha, 'remaining_issues': normalized}


def normalize_repo_path(value):
	if not isinstance(value, str) or not value or len(value) > 512 or '\\' in value:
		return None
	path_value = PurePosixPath(value)
	if path_value.is_absolute() or value != path_value.as_posix() or any(part in ('', '.', '..') for part in path_value.parts):
		return None
	if not path_value.parts or path_value.parts[0] in disallowed_roots:
		return None
	candidate = repo_root.joinpath(*path_value.parts)
	try:
		candidate.relative_to(repo_root)
		cursor = candidate
		while cursor != repo_root:
			if cursor.is_symlink():
				return None
			cursor = cursor.parent
		metadata = candidate.stat()
	except (OSError, ValueError):
		return None
	if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_source_bytes:
		return None
	return path_value.as_posix()


def normalize_generated_items(candidate, issue_count):
	if isinstance(candidate, str):
		candidate = json.loads(candidate)
	if isinstance(candidate, dict):
		for key in ('assertions', 'items'):
			if key in candidate:
				candidate = candidate[key]
				break
		else:
			return None
	if not isinstance(candidate, list) or len(candidate) != issue_count or len(candidate) > max_assertions:
		return None
	normalized = []
	for item in candidate:
		if not isinstance(item, dict) or type(item.get('expected_to_fail_until_fixed')) is not bool:
			return None
		assertion_type = item.get('type')
		if assertion_type == 'inconclusive':
			if set(item) != {'type', 'reason', 'expected_to_fail_until_fixed'}:
				return None
			reason = item.get('reason')
			if not isinstance(reason, str) or not reason.strip() or len(reason) > max_reason_length:
				return None
			normalized.append({
				'type': assertion_type,
				'reason': squish(reason, max_reason_length),
				'expected_to_fail_until_fixed': item['expected_to_fail_until_fixed'],
			})
			continue
		if assertion_type not in {'text_present', 'text_absent', 'literal_count'}:
			return None
		expected_keys = {'type', 'path', 'literal', 'expected_to_fail_until_fixed'}
		if assertion_type == 'literal_count':
			expected_keys.update({'min_count', 'max_count'})
		if set(item) != expected_keys:
			return None
		path_value = normalize_repo_path(item.get('path'))
		literal = item.get('literal')
		if path_value is None or not isinstance(literal, str) or not literal or len(literal) > max_literal_length:
			return None
		normalized_item = {
			'type': assertion_type,
			'path': path_value,
			'literal': literal,
			'expected_to_fail_until_fixed': item['expected_to_fail_until_fixed'],
		}
		if assertion_type == 'literal_count':
			minimum = item.get('min_count')
			maximum = item.get('max_count')
			if type(minimum) is not int or type(maximum) is not int or minimum < 0 or maximum < minimum or maximum > 1_000_000:
				return None
			normalized_item.update({'min_count': minimum, 'max_count': maximum})
		normalized.append(normalized_item)
	return normalized


judge_payload = load_judge_payload(judge_artifact)
if judge_payload is None:
	sys.exit(1)
issues = judge_payload['remaining_issues']
raw = load_raw(src)
validated_items = None
for candidate in load_candidates(raw):
	try:
		validated_items = normalize_generated_items(candidate, len(issues))
	except Exception:
		validated_items = None
	if validated_items is not None:
		break
if validated_items is None:
	print('Behavioural smoke synthesis skipped: could not validate synthesis output', file=sys.stderr)
	sys.exit(1)

bundle_rows = []
for issue, generated_item in zip(issues, validated_items):
	bundle_rows.append({
		'issue_id': issue['id'],
		'file': issue['file'],
		'line_start': issue['line_start'],
		'line_end': issue['line_end'],
		'severity': issue['severity'],
		'assertion': generated_item,
	})
bundle = {
	'schema_version': 'behavioural_smoke_assertions.v1',
	'round': judge_payload['round'],
	'head_sha': judge_payload['head_sha'],
	'language': language_hint,
	'assertions': bundle_rows,
}
Path(synth_dir).mkdir(parents=True, exist_ok=True)
bundle_path_obj = Path(bundle_path)
bundle_path_obj.parent.mkdir(parents=True, exist_ok=True)
bundle_path_obj.write_text(
	json.dumps(bundle, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n',
	encoding='utf-8',
)
print(str(len(bundle_rows)))
PY
}
PROMPT_TEMPLATE="${SUPPORT_PROMPTS_DIR}/behavioural-smoke-synthesise.txt"
PR_NUMBER="${PR_NUMBER:-}"
CURRENT_HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
if [ -z "${CURRENT_HEAD_SHA}" ]; then
	CURRENT_HEAD_SHA="${HEAD_SHA:-}"
fi

if [ -z "${PR_NUMBER}" ]; then
	behavioural_smoke_log_fail "missing_pr_number" "0" ".ai/review_runtime/pr-unknown/round-unknown/synth/synth_round_unknown_manifest.json"
	exit 0
fi

if [[ ! "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
	behavioural_smoke_log_fail "invalid_pr_number" "0" ".ai/review_runtime/pr-invalid/round-unknown/synth/synth_round_unknown_manifest.json"
	exit 0
fi

ROUND_NUMBER_BASE="${ROUND_NUMBER:-}"
if [[ "${ROUND_NUMBER_BASE}" =~ ^[0-9]+$ ]]; then
	CURRENT_ROUND="$((ROUND_NUMBER_BASE + 1))"
elif [[ "${AUTOFIX_ITERATION:-}" =~ ^[0-9]+$ ]]; then
	CURRENT_ROUND="${AUTOFIX_ITERATION}"
else
	CURRENT_ROUND="1"
fi

ARTIFACT_DIR=".ai/review_runtime/pr-${PR_NUMBER}/round-${CURRENT_ROUND}"
JUDGE_ARTIFACT="${ARTIFACT_DIR}/judge_interim.json"
SYNTH_DIR="${ARTIFACT_DIR}/synth"
MANIFEST_PATH="${SYNTH_DIR}/synth_round_${CURRENT_ROUND}_assertions.json"

if [ -z "${CURRENT_HEAD_SHA}" ]; then
	behavioural_smoke_log_fail "missing_head_sha" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi

if [ ! -s "${JUDGE_ARTIFACT}" ]; then
	behavioural_smoke_log_fail "missing_judge_artifact" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi

if [ ! -f "${PROMPT_TEMPLATE}" ]; then
	behavioural_smoke_log_fail "missing_prompt" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi

LANGUAGE_HINT="$(detect_behavioural_smoke_language)"
PROMPT_FILE="${RUNTIME_DIR}/behavioural_smoke_prompt.txt"
RAW_OUTPUT_FILE="${RUNTIME_DIR}/behavioural_smoke_raw.txt"
STDERR_FILE="${RUNTIME_DIR}/behavioural_smoke_stderr.txt"
VALIDATION_ENV_FILE="${VALIDATE_ENV_FILE:-validation/validate.env}"
BEHAVIOURAL_SMOKE_MODEL="${BEHAVIOURAL_SMOKE_MODEL:-openai/gpt-5.6-luna}"
BEHAVIOURAL_SMOKE_TIMEOUT_DEFAULT=120
BEHAVIOURAL_SMOKE_TIMEOUT_MAX=3600
BEHAVIOURAL_SMOKE_TIMEOUT_S="${BEHAVIOURAL_SMOKE_TIMEOUT_S:-${BEHAVIOURAL_SMOKE_TIMEOUT_DEFAULT}}"

if ! [[ "${BEHAVIOURAL_SMOKE_TIMEOUT_S}" =~ ^[0-9]+$ ]]; then
	BEHAVIOURAL_SMOKE_TIMEOUT_S="${BEHAVIOURAL_SMOKE_TIMEOUT_DEFAULT}"
else
	BEHAVIOURAL_SMOKE_TIMEOUT_CANONICAL="${BEHAVIOURAL_SMOKE_TIMEOUT_S#"${BEHAVIOURAL_SMOKE_TIMEOUT_S%%[!0]*}"}"
	BEHAVIOURAL_SMOKE_TIMEOUT_CANONICAL="${BEHAVIOURAL_SMOKE_TIMEOUT_CANONICAL:-0}"
	if [ "${BEHAVIOURAL_SMOKE_TIMEOUT_CANONICAL}" = "0" ] \
		|| [ "${#BEHAVIOURAL_SMOKE_TIMEOUT_CANONICAL}" -gt "${#BEHAVIOURAL_SMOKE_TIMEOUT_MAX}" ]; then
		BEHAVIOURAL_SMOKE_TIMEOUT_S="${BEHAVIOURAL_SMOKE_TIMEOUT_DEFAULT}"
	else
		# shellcheck disable=SC2071  # same-length digit strings; string compare avoids integer overflow
		if [ "${#BEHAVIOURAL_SMOKE_TIMEOUT_CANONICAL}" -eq "${#BEHAVIOURAL_SMOKE_TIMEOUT_MAX}" ] && [ "${BEHAVIOURAL_SMOKE_TIMEOUT_CANONICAL}" \> "${BEHAVIOURAL_SMOKE_TIMEOUT_MAX}" ]; then
			BEHAVIOURAL_SMOKE_TIMEOUT_S="${BEHAVIOURAL_SMOKE_TIMEOUT_DEFAULT}"
		else
			BEHAVIOURAL_SMOKE_TIMEOUT_S="${BEHAVIOURAL_SMOKE_TIMEOUT_CANONICAL}"
		fi
	fi
fi

if ! CURRENT_ISSUES_COUNT="$(count_remaining_issues "${JUDGE_ARTIFACT}" "${CURRENT_ROUND}" "${CURRENT_HEAD_SHA}" 2>/dev/null)"; then
	behavioural_smoke_log_fail "invalid_judge_artifact" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi

{
	if [ -f ./pre_assembled_static.txt ]; then
		cat ./pre_assembled_static.txt
		echo
	fi
	echo "=== BEHAVIOURAL SMOKE SYNTHESIS TASK ==="
	echo
	if [ -x "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" ]; then
		(
			cd "${SUPPORT_ROOT_DIR}"
			bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${PROMPT_TEMPLATE}"
		)
	else
		cat "${PROMPT_TEMPLATE}"
	fi
	echo
	echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_BEHAVIOURAL_SMOKE:-20}"
	echo
	echo "=== ROUND CONTEXT ==="
	echo "round: ${CURRENT_ROUND}"
	echo "head_sha: ${CURRENT_HEAD_SHA}"
	echo "language_hint: ${LANGUAGE_HINT}"
	echo
	echo "=== JUDGE INTERIM ARTIFACT ==="
	cat "${JUDGE_ARTIFACT}"
	echo
	echo "=== VALIDATION ENV CONTEXT ==="
	if [ -f "${VALIDATION_ENV_FILE}" ]; then
		cat "${VALIDATION_ENV_FILE}"
	else
		echo 'No validation/validate.env file available.'
	fi
} > "${PROMPT_FILE}"

rm -rf "${SYNTH_DIR}"

if [ "${CURRENT_ISSUES_COUNT}" = "0" ]; then
	printf '[]\n' > "${RAW_OUTPUT_FILE}"
	if synth_count="$(extract_and_write_synth_bundle "${RAW_OUTPUT_FILE}" "${JUDGE_ARTIFACT}" "${SYNTH_DIR}" "${MANIFEST_PATH}" "${CURRENT_ROUND}" "${CURRENT_HEAD_SHA}" "${LANGUAGE_HINT}")" \
		&& [ -s "${MANIFEST_PATH}" ]; then
		behavioural_smoke_log_ok "${synth_count}" "${CURRENT_ROUND}" "${LANGUAGE_HINT}" "${MANIFEST_PATH}"
		exit 0
	fi
	rm -rf "${SYNTH_DIR}"
	behavioural_smoke_log_fail "json_parse_failed" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi

if [ ! -f "${OPENCODE_HELPERS_PATH}" ] || ! source "${OPENCODE_HELPERS_PATH}" 2>/dev/null; then
	behavioural_smoke_log_fail "missing_opencode_helpers" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi
if [ ! -r "${OPENCODE_CONFIG_WRITER_PATH}" ]; then
	opencode_emit_failure_alert review_synthesise_smoke reviewer "${BEHAVIOURAL_SMOKE_MODEL}" 1 config_writer_missing || true
	behavioural_smoke_log_fail "config_writer_missing" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi

BEHAVIOURAL_SMOKE_OPENCODE_CONFIG="${RUNTIME_DIR}/behavioural_smoke_opencode.json"
BEHAVIOURAL_SMOKE_OPENCODE_WORKSPACE="$(pwd)"
if ! bash "${OPENCODE_CONFIG_WRITER_PATH}" \
	--role reviewer \
	--model "${BEHAVIOURAL_SMOKE_MODEL}" \
	--project-path "${BEHAVIOURAL_SMOKE_OPENCODE_WORKSPACE}" \
	--config-path "${BEHAVIOURAL_SMOKE_OPENCODE_CONFIG}" \
	--serena off; then
	opencode_emit_failure_alert review_synthesise_smoke reviewer "${BEHAVIOURAL_SMOKE_MODEL}" 1 config_generation || true
	behavioural_smoke_log_fail "config_generation" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi
if ! opencode_require_bootstrap review_synthesise_smoke reviewer "${BEHAVIOURAL_SMOKE_MODEL}" \
	"${BEHAVIOURAL_SMOKE_OPENCODE_CONFIG}" "${OPENCODE_VERSION:-1.18.23}" "${OPENCODE_CONFIG_WRITER_PATH}"; then
	behavioural_smoke_log_fail "opencode_bootstrap" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi

behavioural_smoke_opencode_cmd=(
	env "OPENROUTER_API_KEY=${OPENCODE_HELPERS_PROVIDER_API_KEY}"
	bash -c
	# shellcheck disable=SC2016
	'set -euo pipefail; source "$1"; shift; opencode_run_cmd "$@"'
	opencode-behavioural-smoke
	"${OPENCODE_HELPERS_PATH}"
	reviewer
	"${BEHAVIOURAL_SMOKE_MODEL}"
	low
	"${BEHAVIOURAL_SMOKE_OPENCODE_CONFIG}"
	"${BEHAVIOURAL_SMOKE_OPENCODE_WORKSPACE}"
)

if timeout --signal=TERM --kill-after=30s -- "${BEHAVIOURAL_SMOKE_TIMEOUT_S}" \
	"${behavioural_smoke_opencode_cmd[@]}" \
	< "${PROMPT_FILE}" > "${RAW_OUTPUT_FILE}" 2> "${STDERR_FILE}"; then
	cmd_rc=0
else
	cmd_rc=$?
fi

behavioural_smoke_clean_output="${RAW_OUTPUT_FILE}.ansi-clean"
if opencode_strip_ansi < "${RAW_OUTPUT_FILE}" > "${behavioural_smoke_clean_output}"; then
	mv "${behavioural_smoke_clean_output}" "${RAW_OUTPUT_FILE}"
else
	rm -f "${behavioural_smoke_clean_output}"
fi
behavioural_smoke_clean_stderr="${STDERR_FILE}.ansi-clean"
if opencode_strip_ansi < "${STDERR_FILE}" > "${behavioural_smoke_clean_stderr}"; then
	mv "${behavioural_smoke_clean_stderr}" "${STDERR_FILE}"
else
	rm -f "${behavioural_smoke_clean_stderr}"
fi
if [ "${cmd_rc}" -ne 0 ]; then
	opencode_emit_failure_alert review_synthesise_smoke reviewer "${BEHAVIOURAL_SMOKE_MODEL}" "${cmd_rc}" invocation_failed || true
fi

if synth_count="$(extract_and_write_synth_bundle "${RAW_OUTPUT_FILE}" "${JUDGE_ARTIFACT}" "${SYNTH_DIR}" "${MANIFEST_PATH}" "${CURRENT_ROUND}" "${CURRENT_HEAD_SHA}" "${LANGUAGE_HINT}")" \
	&& [ -s "${MANIFEST_PATH}" ]; then
	behavioural_smoke_log_ok "${synth_count}" "${CURRENT_ROUND}" "${LANGUAGE_HINT}" "${MANIFEST_PATH}"
	exit 0
fi

if synth_count="$(extract_and_write_synth_bundle "${STDERR_FILE}" "${JUDGE_ARTIFACT}" "${SYNTH_DIR}" "${MANIFEST_PATH}" "${CURRENT_ROUND}" "${CURRENT_HEAD_SHA}" "${LANGUAGE_HINT}")" \
	&& [ -s "${MANIFEST_PATH}" ]; then
	behavioural_smoke_log_ok "${synth_count}" "${CURRENT_ROUND}" "${LANGUAGE_HINT}" "${MANIFEST_PATH}"
	exit 0
fi

if [ -s "${STDERR_FILE}" ]; then
	{
		echo 'BEHAVIOURAL_SMOKE_SYNTHESIS_STDERR_BEGIN'
		tail -n 80 "${STDERR_FILE}" || true
		echo 'BEHAVIOURAL_SMOKE_SYNTHESIS_STDERR_END'
	} >&2
fi

rm -rf "${SYNTH_DIR}"
failure_reason="json_parse_failed"
if [ "${cmd_rc}" -eq 124 ]; then
	failure_reason="timeout"
elif [ "${cmd_rc}" -eq 137 ]; then
	failure_reason="killed"
elif [ "${cmd_rc}" -ne 0 ]; then
	failure_reason="llm_failed"
fi

behavioural_smoke_log_fail "${failure_reason}" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
exit 0
