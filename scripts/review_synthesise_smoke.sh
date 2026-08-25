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

	PYTHONDONTWRITEBYTECODE=1 python3 - "${judge_artifact}" "${expected_round}" "${expected_head_sha}" <<'PY'
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
	local manifest_path="$4"
	local expected_round="$5"
	local expected_head_sha="$6"
	local language_hint="$7"

	PYTHONDONTWRITEBYTECODE=1 python3 - \
		"${src}" \
		"${judge_artifact}" \
		"${synth_dir}" \
		"${manifest_path}" \
		"${expected_round}" \
		"${expected_head_sha}" \
		"${language_hint}" <<'PY'
import json
import os
import re
import shlex
import sys
from json import JSONDecoder, JSONDecodeError
from pathlib import Path

src, judge_artifact, synth_dir, manifest_path, expected_round_raw, expected_head_sha, language_hint = sys.argv[1:8]
expected_round = int(expected_round_raw)


def squish(value, limit=None):
	text = re.sub(r'\s+', ' ', str(value)).strip()
	if limit is not None and len(text) > limit:
		text = text[: max(limit - 3, 0)].rstrip() + '...'
	return text


def load_raw(path: str) -> str:
	try:
		return Path(path).read_text(encoding='utf-8', errors='replace')
	except OSError:
		return ''


def load_candidates(text: str):
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


def strip_outer_code_fence(text: str) -> str:
	stripped = text.strip()
	lines = stripped.splitlines()
	if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
		return "\n".join(lines[1:-1]).strip("\n")
	return text


def normalize_content(text: str) -> str:
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	text = strip_outer_code_fence(text)
	lines = text.splitlines()
	while lines and not lines[0].strip():
		lines.pop(0)
	if lines and lines[0].startswith("#!"):
		lines = lines[1:]
	text = "\n".join(lines).strip()
	if not text:
		raise ValueError("empty_content")
	# Validate backslash-continued shell lines as one logical command so
	# dangerous dispatchers cannot hide behind line continuations.
	validation_text = re.sub(r'\\\n[ \t]*', ' ', text)
	for raw_line in validation_text.splitlines():
		stripped = raw_line.strip()
		if "`" in stripped or "$(" in stripped or "<(" in stripped or ">(" in stripped:
			raise ValueError("body_unsafe_shell_construct")
		lexer = shlex.shlex(stripped, posix=True, punctuation_chars=";&|(){}><")
		lexer.whitespace_split = True
		# Keep '#' literal so shlex does not hide trailing separators/commands
		# that bash would still execute when '#' appears mid-word.
		lexer.commenters = ""
		tokens = list(lexer)
		expect_command = True
		passthrough_command = ""
		passthrough_option_value = False
		pending_redirection_target = False
		separator_tokens = {";", "&", "&&", "||", "|", "|&", "(", ")", "{", "}", "if", "then", "do", "else", "elif", "while", "until", "!"}
		redirection_tokens = {">", ">>", "<", "<<", "<<<", "<>", "<&", ">&", ">|", "&>", "&>>"}
		passthrough_tokens = {"command", "builtin", "env", "nohup", "nice", "timeout", "setsid", "time"}
		passthrough_value_tokens = {
			"env": {"-u", "-C", "--unset", "--chdir"},
			"nice": {"-n", "--adjustment"},
			"time": {"-f", "--format", "-o", "--output"},
			"timeout": {"-s", "--signal", "-k", "--kill-after"},
		}
		dangerous_command_tokens = {
			"coproc",
			".",
			"bash",
			"csh",
			"dash",
			"eval",
			"exec",
			"ksh",
			"perl",
			"php",
			"ruby",
			"sh",
			"source",
			"sudo",
			"tcsh",
			"xargs",
			"zsh",
		}
		for index, token in enumerate(tokens):
			next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
			token_basename = os.path.basename(token)
			if pending_redirection_target:
				pending_redirection_target = False
				continue
			if token in separator_tokens:
				expect_command = True
				passthrough_command = ""
				passthrough_option_value = False
				continue
			if token in redirection_tokens:
				pending_redirection_target = True
				continue
			if token.isdigit() and next_token in redirection_tokens:
				continue
			if expect_command and passthrough_option_value:
				passthrough_option_value = False
				continue
			if expect_command and re.match(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
				continue
			if expect_command and token_basename in passthrough_tokens:
				passthrough_command = token_basename
				continue
			if expect_command and passthrough_command:
				if passthrough_command == "env" and (
					token == "-S"
					or token.startswith("-S")
					or token == "--split-string"
					or token.startswith("--split-string=")
				):
					raise ValueError("body_unsafe_shell_construct")
				if token.startswith("-"):
					if token in passthrough_value_tokens.get(passthrough_command, set()):
						passthrough_option_value = True
					continue
				if passthrough_command == "nice" and re.match(r"-?\d+$", token):
					continue
				if passthrough_command == "timeout" and re.match(r"\d+(?:\.\d+)?[smhd]?$", token):
					continue
				if token_basename in passthrough_tokens:
					passthrough_command = token_basename
					continue
			if expect_command and token.startswith("$"):
				raise ValueError("body_unsafe_shell_construct")
			if expect_command and token_basename in dangerous_command_tokens:
				raise ValueError("body_unsafe_shell_construct")
			expect_command = False
			passthrough_command = ""
			passthrough_option_value = False
	return text + "\n"


def normalize_issue(issue):
	if not isinstance(issue, dict):
		return None
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
		return None
	line_start = issue.get('line_start')
	line_end = issue.get('line_end')
	if type(line_start) is not int or type(line_end) is not int:
		return None
	if line_start < 1 or line_end < line_start:
		return None
	severity = issue.get('severity')
	if severity not in {'must-fix', 'nice-to-have'}:
		return None
	issue_id = issue.get('id')
	issue_file = issue.get('file')
	symptom = issue.get('symptom')
	evidence_quote = issue.get('evidence_quote')
	if not all(isinstance(value, str) for value in (issue_id, issue_file, symptom, evidence_quote)):
		return None
	issue_id = squish(issue_id)
	issue_file = squish(issue_file)
	symptom = squish(symptom, 200)
	evidence_quote = squish(evidence_quote, 200)
	if not issue_id or not issue_file or not symptom or not evidence_quote:
		return None
	return {
		'id': issue_id,
		'file': issue_file,
		'line_start': line_start,
		'line_end': line_end,
		'symptom': symptom,
		'evidence_quote': evidence_quote,
		'severity': severity,
	}


def load_judge_payload(path: str):
	with open(path, 'r', encoding='utf-8') as handle:
		payload = json.load(handle)
	if not isinstance(payload, dict):
		return None
	if payload.get('round') != expected_round:
		return None
	if payload.get('head_sha') != expected_head_sha:
		return None
	remaining = payload.get('remaining_issues')
	if not isinstance(remaining, list):
		return None
	normalized = []
	for issue in remaining:
		normalized_issue = normalize_issue(issue)
		if normalized_issue is None:
			return None
		normalized.append(normalized_issue)
	return {
		'round': expected_round,
		'head_sha': expected_head_sha,
		'remaining_issues': normalized,
	}


def normalize_generated_items(candidate, issue_count: int):
	if isinstance(candidate, str):
		candidate = json.loads(candidate)
	if isinstance(candidate, dict):
		for key in ('files', 'tests', 'items'):
			if key in candidate:
				candidate = candidate[key]
				break
		else:
			return None
	if not isinstance(candidate, list):
		return None
	if len(candidate) != issue_count:
		return None
	normalized = []
	for item in candidate:
		if not isinstance(item, dict):
			return None
		path_value = item.get('path')
		content = item.get('content')
		expected_to_fail_until_fixed = item.get('expected_to_fail_until_fixed')
		if not isinstance(path_value, str) or not squish(path_value, 200):
			return None
		if not isinstance(content, str):
			return None
		try:
			content = normalize_content(content)
		except Exception:
			return None
		if type(expected_to_fail_until_fixed) is not bool:
			return None
		normalized.append(
			{
				'path': squish(path_value, 200),
				'content': content,
				'expected_to_fail_until_fixed': expected_to_fail_until_fixed,
			}
		)
	return normalized


def slugify(value: str) -> str:
	text = re.sub(r'[^A-Za-z0-9]+', '_', value).strip('_').lower()
	text = re.sub(r'_+', '_', text)
	if not text:
		text = 'issue'
	text = text[:64].rstrip('_')
	return text or 'issue'


def unique_filename(round_value: int, issue_id: str, seen: set[str]) -> tuple[str, str]:
	base_slug = slugify(issue_id)
	slug = base_slug
	index = 2
	filename = f'synth_round_{round_value}_{slug}.sh'
	while filename in seen:
		slug = f'{base_slug}_{index}'
		slug = slug[:64].rstrip('_') or f'issue_{index}'
		filename = f'synth_round_{round_value}_{slug}.sh'
		index += 1
	seen.add(filename)
	return slug, filename


def delimiter_for(content: str, slug: str) -> str:
	base = re.sub(r'[^A-Za-z0-9_]+', '_', slug.upper()) or 'ISSUE'
	delimiter = f'__BEHAVIOURAL_SMOKE_{base}__'
	index = 2
	while delimiter in content:
		delimiter = f'__BEHAVIOURAL_SMOKE_{base}_{index}__'
		index += 1
	return delimiter


def build_wrapper(issue, generated_item, round_value: int, slug: str) -> str:
	delimiter = delimiter_for(generated_item['content'], slug)
	issue_id = shlex.quote(issue['id'])
	round_shell = shlex.quote(str(round_value))
	tap_label = shlex.quote(squish(f"behavioural smoke {issue['id']}", 180))
	expected_flag = shlex.quote('true' if generated_item['expected_to_fail_until_fixed'] else 'false')
	lines = [
		'#!/usr/bin/env bash',
		'set -euo pipefail',
		'',
		f'ISSUE_ID={issue_id}',
		f'ROUND={round_shell}',
		f'TAP_LABEL={tap_label}',
		f'EXPECTED_TO_FAIL_UNTIL_FIXED={expected_flag}',
		'',
		'echo "1..1"',
		'',
		'_synth_output_file=""',
		'if ! _synth_output_file="$(mktemp "${TMPDIR:-/tmp}/behavioural_smoke.XXXXXX" 2>/dev/null)"; then',
		'	echo "# BEHAVIOURAL_SMOKE_PRESENT_INCONCLUSIVE issue=${ISSUE_ID} round=${ROUND} reason=mktemp_failed"',
		'	echo "ok 1 - ${TAP_LABEL}"',
		'	exit 0',
		'fi',
		'cleanup_behavioural_smoke()',
		'{',
		'	rm -f "${_synth_output_file}" >/dev/null 2>&1 || true',
		'}',
		'trap cleanup_behavioural_smoke EXIT INT TERM',
		'',
		'set +e',
		f"bash >\"${{_synth_output_file}}\" 2>&1 <<'{delimiter}'",
		generated_item['content'].rstrip('\n'),
		delimiter,
		'_synth_rc=$?',
		'set -e',
		'',
		'if [ -f "${_synth_output_file}" ]; then',
		'	while IFS= read -r _synth_line || [ -n "${_synth_line}" ]; do',
		'		echo "# ${_synth_line}"',
		'	done < "${_synth_output_file}"',
		'fi',
		'',
		'case "${_synth_rc}" in',
		'	0)',
		'		echo "# BEHAVIOURAL_SMOKE_PRESENT_PASSED issue=${ISSUE_ID} round=${ROUND}"',
		'		;;',
		'	1)',
		'		echo "# BEHAVIOURAL_SMOKE_PRESENT_FAILED issue=${ISSUE_ID} round=${ROUND}"',
		'		;;',
		'	*)',
		'		echo "# BEHAVIOURAL_SMOKE_PRESENT_INCONCLUSIVE issue=${ISSUE_ID} round=${ROUND} exit=${_synth_rc}"',
		'		;;',
		'esac',
		'echo "# expected_to_fail_until_fixed=${EXPECTED_TO_FAIL_UNTIL_FIXED}"',
		'echo "ok 1 - ${TAP_LABEL}"',
		'exit 0',
		'',
	]
	return '\n'.join(lines)


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

synth_dir_path = Path(synth_dir)
manifest_path_obj = Path(manifest_path)
synth_dir_path.mkdir(parents=True, exist_ok=True)
manifest_path_obj.parent.mkdir(parents=True, exist_ok=True)

seen_filenames: set[str] = set()
manifest_rows = []
for issue, generated_item in zip(issues, validated_items):
	slug, filename = unique_filename(expected_round, issue['id'], seen_filenames)
	wrapper_path = synth_dir_path / filename
	wrapper_path.write_text(build_wrapper(issue, generated_item, expected_round, slug), encoding='utf-8')
	os.chmod(wrapper_path, 0o755)
	manifest_rows.append(
		{
			'issue_id': issue['id'],
			'file': issue['file'],
			'line_start': issue['line_start'],
			'line_end': issue['line_end'],
			'severity': issue['severity'],
			'slug': slug,
			'cache_relpath': wrapper_path.as_posix(),
			'target_relpath': f'validation/tests/{filename}',
			'suggested_path': generated_item['path'],
			'expected_to_fail_until_fixed': generated_item['expected_to_fail_until_fixed'],
		}
	)

manifest = {
	'round': judge_payload['round'],
	'head_sha': judge_payload['head_sha'],
	'language': language_hint,
	'source_artifact': Path(judge_artifact).as_posix(),
	'target_manifest_relpath': f'validation/tests/synth_round_{expected_round}_manifest.json',
	'files': manifest_rows,
}

with open(manifest_path_obj, 'w', encoding='utf-8') as handle:
	json.dump(manifest, handle, ensure_ascii=True, indent=2)
	handle.write('\n')

print(str(len(manifest_rows)))
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
MANIFEST_PATH="${SYNTH_DIR}/synth_round_${CURRENT_ROUND}_manifest.json"

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

codex_bin="$(command -v codex || true)"
if [ -z "${codex_bin}" ]; then
	behavioural_smoke_log_fail "missing_codex" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi

smoke_codex_root="${RUNNER_TEMP:-${RUNTIME_DIR}}/codex_home_behavioural_smoke"
smoke_codex_home=""
cleanup_behavioural_smoke_codex()
{
	if [ -n "${smoke_codex_home}" ]; then
		rm -rf "${smoke_codex_home}" 2>/dev/null || true
	fi
	if [ -n "${smoke_codex_root}" ]; then
		rmdir "${smoke_codex_root}" 2>/dev/null || true
	fi
}
trap cleanup_behavioural_smoke_codex EXIT INT TERM

mkdir -p "${smoke_codex_root}"
smoke_codex_home="$(mktemp -d "${smoke_codex_root}/behavioural-smoke.XXXXXX" 2>/dev/null || printf '')"
if [ -z "${smoke_codex_home}" ]; then
	behavioural_smoke_log_fail "mktemp_failed" "${CURRENT_ROUND}" "${MANIFEST_PATH}"
	exit 0
fi

if [ -d "${CODEX_HOME:-}" ]; then
	cp -r "${CODEX_HOME}/." "${smoke_codex_home}/" 2>/dev/null || true
	chmod -R u+w "${smoke_codex_home}" 2>/dev/null || true
elif [ -d "${HOME}/.codex" ]; then
	cp -r "${HOME}/.codex/." "${smoke_codex_home}/" 2>/dev/null || true
	chmod -R u+w "${smoke_codex_home}" 2>/dev/null || true
fi

escaped_reasoning="$(printf '%s' 'low' | sed 's/[\\/&]/\\&/g')"
reasoning_config_applied=0
for cfg in "${smoke_codex_home}/config.toml" "${smoke_codex_home}/.codex/config.toml"; do
	if [ -f "${cfg}" ]; then
		reasoning_config_applied=1
		if ! grep -Eq '^[[:space:]]*model_reasoning_effort[[:space:]]*=' "${cfg}"; then
			printf 'model_reasoning_effort = "%s"\n' 'low' >> "${cfg}"
		else
			if ! sed -i \
				-e "s|^[[:space:]]*model_reasoning_effort[[:space:]]*=.*$|model_reasoning_effort = \"${escaped_reasoning}\"|" \
				"${cfg}" 2>/dev/null; then
				behavioural_smoke_emit_warning "Failed to update model_reasoning_effort in ${cfg}; continuing with existing config."
			fi
		fi
	fi
done
if [ "${reasoning_config_applied}" -eq 0 ]; then
	printf 'model_reasoning_effort = "%s"\n' 'low' > "${smoke_codex_home}/config.toml"
fi

if CODEX_HOME="${smoke_codex_home}" \
	timeout --signal=TERM --kill-after=30s -- "${BEHAVIOURAL_SMOKE_TIMEOUT_S}" \
	"${codex_bin}" --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${BEHAVIOURAL_SMOKE_MODEL}" --sandbox read-only \
	< "${PROMPT_FILE}" > "${RAW_OUTPUT_FILE}" 2> "${STDERR_FILE}"; then
	cmd_rc=0
else
	cmd_rc=$?
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
