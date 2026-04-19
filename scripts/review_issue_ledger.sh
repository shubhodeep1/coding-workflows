#!/usr/bin/env bash
set -Eeuo pipefail

export LC_ALL=C
review_log()
{
	printf 'stage=ledger %s\n' "$*" >&2
}

trim()
{
	local value="$1"
	value="${value#"${value%%[![:space:]]*}"}"
	value="${value%"${value##*[![:space:]]}"}"
	printf '%s' "${value}"
}

lower_file_ext()
{
	local file="$1"
	if [[ "${file}" != *.* ]]; then
		printf '%s' ""
		return
	fi
	printf '%s' "${file##*.}" | tr '[:upper:]' '[:lower:]'
}

normalize_stream_lines()
{
	local ext="$1"
	awk -v ext="${ext}" '
		function trim(s) {
			sub(/^[[:space:]]+/, "", s)
			sub(/[[:space:]]+$/, "", s)
			return s
		}
		function strip_comments(s, out) {
			out = s
			if (ext ~ /^(py|sh|bash|yml|yaml|rb|pl|ini|cfg|toml|make|mk)$/) {
				sub(/#.*/, "", out)
			}
			if (ext ~ /^(js|ts|jsx|tsx|java|go|c|cc|cpp|h|hpp|cs|swift|kt|kts|scala|php|rs)$/) {
				sub(/\/\/.*$/, "", out)
			}
			while (match(out, /\/\*[^*]*\*\//)) {
				out = substr(out, 1, RSTART - 1) substr(out, RSTART + RLENGTH)
			}
			return out
		}
		{
			line = strip_comments($0)
			line = trim(line)
			if (line == "") {
				next
			}
			gsub(/[[:space:]]+/, " ", line)
			print tolower(line)
		}
	'
}

read_anchor_context()
{
	local file_path="$1"
	local line_spec="$2"
	local fallback_current_code="$3"
	local line_start line_end anchor_line ext range_start range_end
	if [ -z "${file_path}" ]; then
		printf '%s\n' ""
		return
	fi
	if [[ "${file_path}" == /* ]] || [[ "${file_path}" =~ (^|/)\.\.(/|$) ]] || [[ ! "${file_path}" =~ ^[A-Za-z0-9_./-]+$ ]]; then
		printf '%s\n' ""
		return
	fi
	if [[ "${file_path}" == .git ]] || [[ "${file_path}" == .git/* ]] || [[ "${file_path}" == */.git ]] || [[ "${file_path}" == */.git/* ]]; then
		printf '%s\n' ""
		return
	fi
	if [[ "${line_spec}" =~ ^([0-9]+)(-([0-9]+))?$ ]]; then
		line_start="${BASH_REMATCH[1]}"
		if [ -n "${BASH_REMATCH[3]:-}" ]; then
			line_end="${BASH_REMATCH[3]}"
		else
			line_end="${BASH_REMATCH[1]}"
		fi
	else
		line_start=""
		line_end=""
	fi
	if [ -z "${line_start}" ]; then
		line_start=1
		line_end=1
	fi
	anchor_line="${line_start}"
	if [ "${anchor_line}" -lt 1 ]; then
		anchor_line=1
	fi
	range_start=$((anchor_line - 2))
	if [ "${range_start}" -lt 1 ]; then
		range_start=1
	fi
	range_end=$((anchor_line + 2))
	ext="$(lower_file_ext "${file_path}")"
	if [ -f "${file_path}" ]; then
		sed -n "${range_start},${range_end}p" -- "${file_path}" | normalize_stream_lines "${ext}"
		return
	fi
	if git cat-file -e "HEAD:${file_path}" >/dev/null 2>&1; then
		git show "HEAD:${file_path}" | sed -n "${range_start},${range_end}p" | normalize_stream_lines "${ext}"
		return
	fi
	if [ -n "${fallback_current_code}" ]; then
		printf '%s\n' "${fallback_current_code}" | normalize_stream_lines "${ext}"
		return
	fi
	printf '%s\n' ""
}

hash_issue_id_base()
{
	local file_path="$1"
	local anchor_fp="$2"
	local lens="$3"
	local severity_floor="$4"
	local joined
	joined="${file_path}"$'\x1f'"${anchor_fp}"$'\x1f'"${lens}"$'\x1f'"${severity_floor}"
	printf '%s' "iss_$(printf '%s' "${joined}" | sha256sum | awk '{print $1}' | cut -c1-16)"
}

capture_floor_map()
{
	local floor_file="$1"
	local out_file="$2"
	: > "${out_file}"
	if [ ! -s "${floor_file}" ]; then
		return
	fi
	awk -F '\t' '
		function strongest(a, b) {
			if (a == "") {
				return b
			}
			if (a ~ /^SECURITY$/ || b ~ /^SECURITY$/) {
				if (a ~ /^SECURITY$/) {
					return a
				}
				return b
			}
			return a
		}
		{
			anchor = $1
			tags = $2
			if (anchor == "" || tags == "") {
				next
			}
			n = split(tags, arr, ",")
			best = ""
			for (i = 1; i <= n; i++) {
				tag = arr[i]
				gsub(/^[[:space:]]+|[[:space:]]+$/, "", tag)
				if (tag ~ /^FLOOR_CRITICAL_KEYWORD:/) {
					cat = tag
					sub(/^FLOOR_CRITICAL_KEYWORD:/, "", cat)
					gsub(/[^A-Za-z0-9_]+/, "_", cat)
					cat = toupper(cat)
					if (cat == "") {
						cat = "UNCATEGORIZED"
					}
					best = strongest(best, cat)
				}
			}
			if (best != "") {
				print anchor "\t" best
			}
		}
	' "${floor_file}" > "${out_file}"
}

parse_review_issues()
{
	local review_issues_file="$1"
	local out_file="$2"
	: > "${out_file}"
	if [ ! -s "${review_issues_file}" ]; then
		return
	fi
	awk -v out_file="${out_file}" '
		function trim(s) {
			sub(/^[[:space:]]+/, "", s)
			sub(/[[:space:]]+$/, "", s)
			return s
		}
		function flush_block(    code) {
			if (block_id == "") {
				return
			}
			file = trim(file)
			lines = trim(lines)
			lens = trim(lens)
			severity = trim(severity)
			classification = trim(classification)
			if (lens == "") {
				lens = "UNKNOWN_LENS"
			}
			if (severity == "") {
				severity = "low"
			}
			if (classification == "") {
				classification = "unclassified"
			}
			code = current_code
			gsub(/\r/, "", code)
			gsub(/\\/, "\\\\", code)
			gsub(/\t/, "\\t", code)
			gsub(/\n/, "\\n", code)
			print block_id "\t" file "\t" lines "\t" lens "\t" severity "\t" classification "\t" code >> out_file
		}
		BEGIN {
			in_block = 0
			collect_code = 0
			block_id = ""
			file = ""
			lines = ""
			lens = ""
			severity = ""
			classification = ""
			current_code = ""
		}
		{
			line = $0
			if (match(line, /^=== ISSUE[[:space:]]+(.+)[[:space:]]+===$/, m)) {
				flush_block()
				in_block = 1
				collect_code = 0
				block_id = trim(m[1])
				file = ""
				lines = ""
				lens = ""
				severity = ""
				classification = ""
				current_code = ""
				next
			}
			if (match(line, /^=== END ISSUE[[:space:]]+(.+)[[:space:]]+===$/, m)) {
				flush_block()
				in_block = 0
				collect_code = 0
				block_id = ""
				next
			}
			if (!in_block) {
				next
			}
			if (line ~ /^FILE:[[:space:]]*/) {
				file = trim(substr(line, index(line, ":") + 1))
				collect_code = 0
				next
			}
			if (line ~ /^LINES:[[:space:]]*/) {
				lines = trim(substr(line, index(line, ":") + 1))
				collect_code = 0
				next
			}
			if (line ~ /^LENS:[[:space:]]*/) {
				lens = trim(substr(line, index(line, ":") + 1))
				collect_code = 0
				next
			}
			if (line ~ /^SEVERITY:[[:space:]]*/) {
				severity = trim(substr(line, index(line, ":") + 1))
				collect_code = 0
				next
			}
			if (line ~ /^CLASSIFICATION:[[:space:]]*/) {
				classification = trim(substr(line, index(line, ":") + 1))
				collect_code = 0
				next
			}
			if (line ~ /^CURRENT_CODE:[[:space:]]*$/) {
				collect_code = 1
				next
			}
			if (line ~ /^(EVIDENCE|SUGGESTED_APPROACH|NOTES|PARSER_TAGS|UNRECOGNISED):/) {
				collect_code = 0
				next
			}
			if (collect_code) {
				t = line
				sub(/^  /, "", t)
				if (current_code == "") {
					current_code = t
				} else {
					current_code = current_code "\n" t
				}
			}
		}
		END {
			flush_block()
		}
	' "${review_issues_file}"
}

normalize_ledger_status()
{
	local status="$1"
	case "${status}" in
		NEW|PERSISTING|FIXED|RESURGENT|accepted-residual)
			printf '%s' "${status}"
			;;
		*)
			printf '%s' "PERSISTING"
			;;
	esac
}

format_editor_outcomes_for_status()
{
	local editor_outcomes="$1"
	printf '%s\n' "${editor_outcomes}" | awk '
		{
			gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
			if ($0 == "") {
				next
			}
			if (count > 0) {
				printf ", "
			}
			printf "%s", $0
			count++
		}
		END {
			if (count == 0) {
				printf "none"
			}
		}
	'
}

write_current_entry()
{
	local entry_file="$1"
	local issue_id="$2"
	local file_path="$3"
	local lines="$4"
	local lens="$5"
	local severity="$6"
	local status="$7"
	local first_seen="$8"
	local last_seen="$9"
	local persist_count="${10}"
	local editor_outcomes="${11}"
	{
		echo "=== ENTRY ${issue_id} ==="
		echo "FILE: ${file_path}"
		echo "LINES: ${lines}"
		echo "LENS: ${lens}"
		echo "SEVERITY: ${severity}"
		echo "STATUS: ${status}"
		echo "FIRST_SEEN_ITERATION: ${first_seen}"
		echo "LAST_SEEN_ITERATION: ${last_seen}"
		echo "PERSIST_COUNT: ${persist_count}"
		echo "EDITOR_OUTCOMES:"
		while IFS= read -r outcome_line || [ -n "${outcome_line}" ]; do
			echo "  ${outcome_line}"
		done < <(printf '%s\n' "${editor_outcomes}" | sed '/^[[:space:]]*$/d')
		echo "=== END ENTRY ==="
	} >> "${entry_file}"
}

parse_prior_ledger()
{
	local ledger_file="$1"
	local out_file="$2"
	local header_file="$3"
	: > "${out_file}"
	: > "${header_file}"
	if [ ! -s "${ledger_file}" ]; then
		return 0
	fi
	if ! awk -v out_file="${out_file}" -v header_file="${header_file}" '
		function trim(s) {
			sub(/^[[:space:]]+/, "", s)
			sub(/[[:space:]]+$/, "", s)
			return s
		}
		function flush_entry(    encoded_outcomes) {
			if (entry_id == "") {
				return
			}
			if (file == "" || lens == "" || severity == "" || status == "" || first_seen == "" || last_seen == "" || persist_count == "") {
				err = 1
				return
			}
			encoded_outcomes = editor_outcomes
			gsub(/\r/, "", encoded_outcomes)
			gsub(/\\/, "\\\\", encoded_outcomes)
			gsub(/\t/, "\\t", encoded_outcomes)
			gsub(/\n/, "\\n", encoded_outcomes)
			print entry_id "\t" file "\t" lines "\t" lens "\t" severity "\t" status "\t" first_seen "\t" last_seen "\t" persist_count "\t" encoded_outcomes >> out_file
		}
		BEGIN {
			err = 0
			state = "start"
			in_editor_outcomes = 0
			entry_id = ""
			file = ""
			lines = ""
			lens = ""
			severity = ""
			status = ""
			first_seen = ""
			last_seen = ""
			persist_count = ""
			editor_outcomes = ""
			pr_number = ""
			first_seen_iteration = ""
			last_updated_iteration = ""
		}
		{
			line = $0
			if (line ~ /^=== LEDGER v1 ===$/) {
				state = "header"
				next
			}
			if (state == "start") {
				if (trim(line) == "") {
					next
				}
				err = 1
				next
			}
			if (state == "header") {
				if (line ~ /^PR_NUMBER:[[:space:]]*/) {
					pr_number = trim(substr(line, index(line, ":") + 1))
					next
				}
				if (line ~ /^FIRST_SEEN_ITERATION:[[:space:]]*/) {
					first_seen_iteration = trim(substr(line, index(line, ":") + 1))
					next
				}
				if (line ~ /^LAST_UPDATED_ITERATION:[[:space:]]*/) {
					last_updated_iteration = trim(substr(line, index(line, ":") + 1))
					next
				}
				if (line ~ /^=== END HEADER ===$/) {
					state = "body"
					next
				}
				if (trim(line) == "") {
					next
				}
				err = 1
				next
			}
			if (line ~ /^=== ENTRY /) {
				flush_entry()
				entry_id = line
				sub(/^=== ENTRY /, "", entry_id)
				sub(/ ===$/, "", entry_id)
				file = ""
				lines = ""
				lens = ""
				severity = ""
				status = ""
				first_seen = ""
				last_seen = ""
				persist_count = ""
				editor_outcomes = ""
				in_editor_outcomes = 0
				next
			}
			if (line ~ /^=== END ENTRY ===$/) {
				flush_entry()
				entry_id = ""
				in_editor_outcomes = 0
				next
			}
			if (entry_id == "") {
				if (trim(line) == "") {
					next
				}
				err = 1
				next
			}
			if (line ~ /^FILE:[[:space:]]*/) {
				file = trim(substr(line, index(line, ":") + 1))
				next
			}
			if (line ~ /^LINES:[[:space:]]*/) {
				lines = trim(substr(line, index(line, ":") + 1))
				next
			}
			if (line ~ /^LENS:[[:space:]]*/) {
				lens = trim(substr(line, index(line, ":") + 1))
				next
			}
			if (line ~ /^SEVERITY:[[:space:]]*/) {
				severity = trim(substr(line, index(line, ":") + 1))
				next
			}
			if (line ~ /^STATUS:[[:space:]]*/) {
				status = trim(substr(line, index(line, ":") + 1))
				next
			}
			if (line ~ /^FIRST_SEEN_ITERATION:[[:space:]]*/) {
				first_seen = trim(substr(line, index(line, ":") + 1))
				next
			}
			if (line ~ /^LAST_SEEN_ITERATION:[[:space:]]*/) {
				last_seen = trim(substr(line, index(line, ":") + 1))
				next
			}
			if (line ~ /^PERSIST_COUNT:[[:space:]]*/) {
				persist_count = trim(substr(line, index(line, ":") + 1))
				in_editor_outcomes = 0
				next
			}
			if (line ~ /^EDITOR_OUTCOMES:[[:space:]]*$/) {
				editor_outcomes = ""
				in_editor_outcomes = 1
				next
			}
			if (line ~ /^EDITOR_OUTCOMES:[[:space:]]*/) {
				editor_outcomes = trim(substr(line, index(line, ":") + 1))
				in_editor_outcomes = 0
				next
			}
			if (in_editor_outcomes && line ~ /^  /) {
				line = substr(line, 3)
				if (editor_outcomes == "") {
					editor_outcomes = line
				} else {
					editor_outcomes = editor_outcomes "\n" line
				}
				next
			}
			in_editor_outcomes = 0
			if (trim(line) == "") {
				next
			}
			err = 1
		}
		END {
			flush_entry()
			if (pr_number == "" || first_seen_iteration == "" || last_updated_iteration == "") {
				err = 1
			}
			if (err != 0) {
				exit 2
			}
			print "PR_NUMBER=" pr_number >> header_file
			print "FIRST_SEEN_ITERATION=" first_seen_iteration >> header_file
			print "LAST_UPDATED_ITERATION=" last_updated_iteration >> header_file
		}
	' "${ledger_file}"; then
		return 1
	fi
	return 0
}

rewrite_review_issues_without_residuals()
{
	local review_issues_file="$1"
	local keep_ids_file="$2"
	local residual_ids_file="$3"
	local out_file="$4"
	awk -v keep_file="${keep_ids_file}" -v residual_file="${residual_ids_file}" '
		function trim(s) {
			sub(/^[[:space:]]+/, "", s)
			sub(/[[:space:]]+$/, "", s)
			return s
		}
		function reset_block_meta() {
			file_value = ""
			lines_value = ""
			lens_value = ""
		}
		function emit_residual_stub(    issue_id) {
			issue_id = residual[block_id]
			if (issue_id == "") {
				return
			}
			if (file_value == "") {
				file_value = "unknown"
			}
			if (lines_value == "") {
				lines_value = "unknown"
			}
			if (lens_value == "") {
				lens_value = "UNKNOWN_LENS"
			}
			printf "=== RESIDUAL ISSUE %s ===\n", block_id
			printf "FILE: %s\n", file_value
			printf "LINES: %s\n", lines_value
			printf "LENS: %s\n", lens_value
			print "STATUS: accepted-residual"
			printf "LEDGER_ISSUE_ID: %s\n", issue_id
			printf "=== END RESIDUAL ISSUE %s ===\n", block_id
		}
		BEGIN {
			while ((getline line < keep_file) > 0) {
				line = trim(line)
				if (line != "") {
					keep[line] = 1
				}
			}
			close(keep_file)
			while ((getline line < residual_file) > 0) {
				n = split(line, fields, "\t")
				if (n >= 2) {
					block = trim(fields[1])
					issue_id = trim(fields[2])
					if (block != "" && issue_id != "") {
						residual[block] = issue_id
					}
				}
			}
			close(residual_file)
			in_block = 0
			buffer = ""
			block_id = ""
			reset_block_meta()
		}
		function flush_block() {
			if (block_id == "") {
				return
			}
			if (keep[block_id]) {
				printf "%s", buffer
			} else if (residual[block_id] != "") {
				emit_residual_stub()
			}
		}
		{
			line = $0
			if (match(line, /^=== ISSUE[[:space:]]+(.+)[[:space:]]+===$/, m)) {
				flush_block()
				in_block = 1
				block_id = trim(m[1])
				buffer = line "\n"
				reset_block_meta()
				next
			}
			if (in_block) {
				buffer = buffer line "\n"
				if (line ~ /^FILE:[[:space:]]*/) {
					file_value = trim(substr(line, index(line, ":") + 1))
				} else if (line ~ /^LINES:[[:space:]]*/) {
					lines_value = trim(substr(line, index(line, ":") + 1))
				} else if (line ~ /^LENS:[[:space:]]*/) {
					lens_value = trim(substr(line, index(line, ":") + 1))
				}
				if (line ~ /^=== END ISSUE[[:space:]]+/) {
					flush_block()
					in_block = 0
					block_id = ""
					buffer = ""
					reset_block_meta()
				}
				next
			}
			print line
		}
		END {
			if (in_block) {
				flush_block()
			}
		}
	' "${review_issues_file}" > "${out_file}"
}

RUNTIME_DIR="${RUNTIME_DIR:?RUNTIME_DIR is required}"
PR_NUMBER="${PR_NUMBER:-0}"
ITERATION_RAW="${AUTOFIX_ITERATION:-${ITERATION:-1}}"
if [[ ! "${ITERATION_RAW}" =~ ^[0-9]+$ ]] || [ "${ITERATION_RAW}" -lt 1 ]; then
	ITERATION=1
else
	ITERATION="${ITERATION_RAW}"
fi

REVIEW_ISSUES_FILE="${REVIEW_ISSUES_FILE:-${RUNTIME_DIR}/review_issues.txt}"
REVIEW_LEDGER_ENABLED="${REVIEW_LEDGER_ENABLED:-1}"
REVIEW_LEDGER_PERSIST_LIMIT_RAW="${REVIEW_LEDGER_PERSIST_LIMIT:-2}"
if [[ "${REVIEW_LEDGER_PERSIST_LIMIT_RAW}" =~ ^[0-9]+$ ]] && [ "${REVIEW_LEDGER_PERSIST_LIMIT_RAW}" -ge 1 ]; then
	REVIEW_LEDGER_PERSIST_LIMIT="${REVIEW_LEDGER_PERSIST_LIMIT_RAW}"
else
	REVIEW_LEDGER_PERSIST_LIMIT=2
fi
LEDGER_STATUS_FILE="${LEDGER_STATUS_FILE:-${RUNTIME_DIR}/ledger_status.txt}"
FLOOR_TAGS_FILE="${FLOOR_TAGS_FILE:-${RUNTIME_DIR}/floor_tags.txt}"
REVIEW_LEDGER_PATH="${REVIEW_LEDGER_PATH:-.ai/review_issue_ledger.txt}"

mkdir -p "$(dirname "${LEDGER_STATUS_FILE}")"

if [ "${REVIEW_LEDGER_ENABLED}" = "0" ]; then
	: > "${LEDGER_STATUS_FILE}"
	review_log "pr=${PR_NUMBER} iteration=${ITERATION} disabled=1 ledger_prior_entries=0 transitions=NEW:0,PERSISTING:0,FIXED:0,RESURGENT:0,accepted-residual:0 accepted_residual_added=0 ledger_reset=0 hash_collision=0"
	exit 0
fi

mkdir -p "$(dirname "${REVIEW_LEDGER_PATH}")"
if [ ! -f "${REVIEW_ISSUES_FILE}" ]; then
	: > "${REVIEW_ISSUES_FILE}"
fi

tmp_dir="$(mktemp -d)"
cleanup()
{
	rm -rf "${tmp_dir}"
}
trap cleanup EXIT

parsed_current_file="${tmp_dir}/parsed_current.tsv"
prior_entries_file="${tmp_dir}/prior_entries.tsv"
prior_header_file="${tmp_dir}/prior_header.env"
floor_map_file="${tmp_dir}/floor_map.tsv"
current_with_ids_file="${tmp_dir}/current_with_ids.tsv"
new_entries_file="${tmp_dir}/new_entries.txt"
status_tmp_file="${tmp_dir}/ledger_status.txt"
keep_block_ids_file="${tmp_dir}/keep_block_ids.txt"
residual_block_ids_file="${tmp_dir}/residual_block_ids.tsv"
filtered_review_issues_file="${tmp_dir}/review_issues.filtered.txt"

parse_review_issues "${REVIEW_ISSUES_FILE}" "${parsed_current_file}"
capture_floor_map "${FLOOR_TAGS_FILE}" "${floor_map_file}"

ledger_reset=0
if ! parse_prior_ledger "${REVIEW_LEDGER_PATH}" "${prior_entries_file}" "${prior_header_file}"; then
	ledger_reset=1
	: > "${prior_entries_file}"
	: > "${prior_header_file}"
	review_log "pr=${PR_NUMBER} iteration=${ITERATION} ledger_reset=1 reason=malformed_prior_ledger path=${REVIEW_LEDGER_PATH}"
fi

prior_entries_count=0
if [ -s "${prior_entries_file}" ]; then
	prior_entries_count="$(wc -l < "${prior_entries_file}" | tr -d '[:space:]')"
fi

header_first_seen="${ITERATION}"
if [ -s "${prior_header_file}" ]; then
	while IFS='=' read -r key value; do
		case "${key}" in
			FIRST_SEEN_ITERATION)
				if [[ "${value}" =~ ^[0-9]+$ ]]; then
					header_first_seen="${value}"
				fi
				;;
		esac
	done < "${prior_header_file}"
fi

repo_ignorecase=false
if git config --bool core.ignorecase >/dev/null 2>&1; then
	repo_ignorecase="$(git config --bool core.ignorecase 2>/dev/null || echo false)"
fi

awk -F '\t' -v floor_file="${floor_map_file}" -v out_file="${current_with_ids_file}" '
	function trim(s) {
		sub(/^[[:space:]]+/, "", s)
		sub(/[[:space:]]+$/, "", s)
		return s
	}
	BEGIN {
		while ((getline fl < floor_file) > 0) {
			split(fl, f, "\t")
			if (f[1] != "" && f[2] != "") {
				floor[f[1]] = f[2]
			}
		}
		close(floor_file)
	}
	{
		issue_block_id = $1
		file = trim($2)
		lines = trim($3)
		lens = trim($4)
		severity = trim($5)
		classification = trim($6)
		current_code = $7
		if (file == "") {
			next
		}
		if (lens == "") {
			lens = "UNKNOWN_LENS"
		}
		if (severity == "") {
			severity = "low"
		}
		split(lines, ln_arr, "-")
		anchor = file ":" trim(ln_arr[1])
		floor_cat = floor[anchor]
		if (floor_cat == "") {
			floor_cat = "none"
		}
		print issue_block_id "\t" file "\t" lines "\t" lens "\t" severity "\t" floor_cat "\t" classification "\t" current_code >> out_file
	}
' "${parsed_current_file}"

: > "${new_entries_file}"
: > "${status_tmp_file}"
: > "${keep_block_ids_file}"
: > "${residual_block_ids_file}"

declare -A PRIOR_FILE=()
declare -A PRIOR_LINES=()
declare -A PRIOR_LENS=()
declare -A PRIOR_SEVERITY=()
declare -A PRIOR_STATUS=()
declare -A PRIOR_FIRST_SEEN=()
declare -A PRIOR_LAST_SEEN=()
declare -A PRIOR_PERSIST=()
declare -A PRIOR_OUTCOMES=()

if [ -s "${prior_entries_file}" ]; then
	while IFS=$'\t' read -r issue_id file_path lines lens severity status first_seen last_seen persist_count editor_outcomes || [ -n "${issue_id}" ]; do
		[ -n "${issue_id}" ] || continue
		PRIOR_FILE["${issue_id}"]="${file_path}"
		PRIOR_LINES["${issue_id}"]="${lines}"
		PRIOR_LENS["${issue_id}"]="${lens}"
		PRIOR_SEVERITY["${issue_id}"]="${severity}"
		PRIOR_STATUS["${issue_id}"]="$(normalize_ledger_status "${status}")"
		if [[ "${first_seen}" =~ ^[0-9]+$ ]]; then
			PRIOR_FIRST_SEEN["${issue_id}"]="${first_seen}"
		else
			PRIOR_FIRST_SEEN["${issue_id}"]="${ITERATION}"
		fi
		if [[ "${last_seen}" =~ ^[0-9]+$ ]]; then
			PRIOR_LAST_SEEN["${issue_id}"]="${last_seen}"
		else
			PRIOR_LAST_SEEN["${issue_id}"]="${ITERATION}"
		fi
		if [[ "${persist_count}" =~ ^[0-9]+$ ]]; then
			PRIOR_PERSIST["${issue_id}"]="${persist_count}"
		else
			PRIOR_PERSIST["${issue_id}"]="0"
		fi
		editor_outcomes="${editor_outcomes//\\t/$'\t'}"
		editor_outcomes="${editor_outcomes//\\n/$'\n'}"
		editor_outcomes="${editor_outcomes//\\\\/\\}"
		PRIOR_OUTCOMES["${issue_id}"]="${editor_outcomes}"
	done < "${prior_entries_file}"
fi

declare -A CURRENT_FILE=()
declare -A CURRENT_LINES=()
declare -A CURRENT_LENS=()
declare -A CURRENT_SEVERITY=()
declare -A CURRENT_FLOOR=()
declare -A CURRENT_CLASSIFICATION=()
declare -A CURRENT_BLOCK_ID=()
declare -A CURRENT_PRESENT=()
declare -A ISSUE_ID_COLLISION_COUNT=()

auto_collision_count=0
if [ -s "${current_with_ids_file}" ]; then
	while IFS=$'\t' read -r block_id file_path lines lens severity floor_cat classification current_code || [ -n "${block_id}${file_path}" ]; do
		[ -n "${file_path}" ] || continue
		current_code="${current_code//\\\\/$'\x1f'}"
		current_code="${current_code//\\n/$'\n'}"
		current_code="${current_code//\\t/$'\t'}"
		current_code="${current_code//$'\x1f'/\\}"
		canonical_path="${file_path}"
		if [ "${repo_ignorecase}" = "true" ]; then
			canonical_path="$(printf '%s' "${canonical_path}" | tr '[:upper:]' '[:lower:]')"
		fi
		ext="$(lower_file_ext "${file_path}")"
		anchor_norm_lines="$(read_anchor_context "${file_path}" "${lines}" "${current_code}" | sed '/^[[:space:]]*$/d')"
		if [ -z "${anchor_norm_lines}" ]; then
			anchor_norm_lines="$(printf '%s\n' "${current_code}" | normalize_stream_lines "${ext}" | sed '/^[[:space:]]*$/d')"
		fi
		if [ -n "${anchor_norm_lines}" ]; then
			anchor_fp="$(printf '%s\n' "${anchor_norm_lines}" | sha256sum | awk '{print $1}' | cut -c1-12)"
		else
			anchor_fp="000000000000"
		fi
		base_id="$(hash_issue_id_base "${canonical_path}" "${anchor_fp}" "${lens}" "${floor_cat}")"
		issue_id="${base_id}"
		if [ -n "${CURRENT_PRESENT["${issue_id}"]:-}" ]; then
			if [ "${CURRENT_FILE["${issue_id}"]}|${CURRENT_LINES["${issue_id}"]}|${CURRENT_LENS["${issue_id}"]}|${CURRENT_SEVERITY["${issue_id}"]}|${CURRENT_CLASSIFICATION["${issue_id}"]}" != "${file_path}|${lines}|${lens}|${severity}|${classification}" ]; then
				suffix="${ISSUE_ID_COLLISION_COUNT["${base_id}"]:-0}"
				while :; do
					suffix=$((suffix + 1))
					candidate="${base_id}:${suffix}"
					if [ -z "${CURRENT_PRESENT["${candidate}"]:-}" ]; then
						issue_id="${candidate}"
						ISSUE_ID_COLLISION_COUNT["${base_id}"]="${suffix}"
						auto_collision_count=$((auto_collision_count + 1))
						review_log "pr=${PR_NUMBER} iteration=${ITERATION} hash_collision=1 base_id=${base_id} assigned=${issue_id}"
						break
					fi
				done
			fi
		fi
		CURRENT_PRESENT["${issue_id}"]=1
		CURRENT_FILE["${issue_id}"]="${file_path}"
		CURRENT_LINES["${issue_id}"]="${lines}"
		CURRENT_LENS["${issue_id}"]="${lens}"
		CURRENT_SEVERITY["${issue_id}"]="${severity}"
		CURRENT_FLOOR["${issue_id}"]="${floor_cat}"
		CURRENT_CLASSIFICATION["${issue_id}"]="${classification}"
		CURRENT_BLOCK_ID["${issue_id}"]="${block_id}"
	done < "${current_with_ids_file}"
fi

declare -A FINAL_FILE=()
declare -A FINAL_LINES=()
declare -A FINAL_LENS=()
declare -A FINAL_SEVERITY=()
declare -A FINAL_STATUS=()
declare -A FINAL_FIRST_SEEN=()
declare -A FINAL_LAST_SEEN=()
declare -A FINAL_PERSIST=()
declare -A FINAL_OUTCOMES=()
declare -A SEEN_FINAL=()
declare -A COUNT_TRANSITIONS=([NEW]=0 [PERSISTING]=0 [FIXED]=0 [RESURGENT]=0 [accepted-residual]=0)

accepted_residual_added=0

for issue_id in "${!CURRENT_PRESENT[@]}"; do
	prior_status="${PRIOR_STATUS["${issue_id}"]:-}"
	prior_persist="${PRIOR_PERSIST["${issue_id}"]:-0}"
	if ! [[ "${prior_persist}" =~ ^[0-9]+$ ]]; then
		prior_persist=0
	fi
	if [ -z "${prior_status}" ]; then
		status="NEW"
		persist_count=1
		first_seen="${ITERATION}"
	else
		first_seen="${PRIOR_FIRST_SEEN["${issue_id}"]:-${ITERATION}}"
		case "${prior_status}" in
			FIXED)
				status="RESURGENT"
				persist_count=1
				;;
			accepted-residual)
				status="accepted-residual"
				persist_count="${prior_persist}"
				;;
			RESURGENT|NEW|PERSISTING)
				status="PERSISTING"
				persist_count=$((prior_persist + 1))
				;;
			*)
				status="PERSISTING"
				persist_count=$((prior_persist + 1))
				;;
		esac
	fi
	if [ "${status}" != "accepted-residual" ] && [ "${persist_count}" -ge "${REVIEW_LEDGER_PERSIST_LIMIT}" ]; then
		status="accepted-residual"
		accepted_residual_added=$((accepted_residual_added + 1))
	fi
	case "${status}" in
		NEW)
			COUNT_TRANSITIONS[NEW]=$((COUNT_TRANSITIONS[NEW] + 1))
			;;
		RESURGENT)
			COUNT_TRANSITIONS[RESURGENT]=$((COUNT_TRANSITIONS[RESURGENT] + 1))
			;;
		accepted-residual)
			COUNT_TRANSITIONS[accepted-residual]=$((COUNT_TRANSITIONS[accepted-residual] + 1))
			;;
		*)
			COUNT_TRANSITIONS[PERSISTING]=$((COUNT_TRANSITIONS[PERSISTING] + 1))
			;;
	esac
	FINAL_FILE["${issue_id}"]="${CURRENT_FILE["${issue_id}"]}"
	FINAL_LINES["${issue_id}"]="${CURRENT_LINES["${issue_id}"]}"
	FINAL_LENS["${issue_id}"]="${CURRENT_LENS["${issue_id}"]}"
	FINAL_SEVERITY["${issue_id}"]="${CURRENT_SEVERITY["${issue_id}"]}"
	FINAL_STATUS["${issue_id}"]="${status}"
	FINAL_FIRST_SEEN["${issue_id}"]="${first_seen}"
	FINAL_LAST_SEEN["${issue_id}"]="${ITERATION}"
	FINAL_PERSIST["${issue_id}"]="${persist_count}"
	FINAL_OUTCOMES["${issue_id}"]="${PRIOR_OUTCOMES["${issue_id}"]:-}"
	SEEN_FINAL["${issue_id}"]=1
	if [ "${status}" != "accepted-residual" ] && [ -n "${CURRENT_BLOCK_ID["${issue_id}"]:-}" ]; then
		printf '%s\n' "${CURRENT_BLOCK_ID["${issue_id}"]}" >> "${keep_block_ids_file}"
	elif [ "${status}" = "accepted-residual" ] && [ -n "${CURRENT_BLOCK_ID["${issue_id}"]:-}" ]; then
		printf '%s\t%s\n' "${CURRENT_BLOCK_ID["${issue_id}"]}" "${issue_id}" >> "${residual_block_ids_file}"
	fi
	display_outcomes="$(format_editor_outcomes_for_status "${FINAL_OUTCOMES["${issue_id}"]}")"
	printf '%s\t%s\t%s\t%s:%s\t%s\t%s\n' \
		"${issue_id}" "${status}" "${persist_count}" \
		"${CURRENT_FILE["${issue_id}"]}" "${CURRENT_LINES["${issue_id}"]}" \
		"${CURRENT_LENS["${issue_id}"]}" "${display_outcomes}" >> "${status_tmp_file}"
done

for issue_id in "${!PRIOR_STATUS[@]}"; do
	if [ -n "${SEEN_FINAL["${issue_id}"]:-}" ]; then
		continue
	fi
	prior_status="${PRIOR_STATUS["${issue_id}"]}"
	if [ "${prior_status}" = "FIXED" ] || [ "${prior_status}" = "accepted-residual" ]; then
		status="${prior_status}"
	else
		status="FIXED"
		COUNT_TRANSITIONS[FIXED]=$((COUNT_TRANSITIONS[FIXED] + 1))
	fi
	persist_count="${PRIOR_PERSIST["${issue_id}"]:-0}"
	if ! [[ "${persist_count}" =~ ^[0-9]+$ ]]; then
		persist_count=0
	fi
	FINAL_FILE["${issue_id}"]="${PRIOR_FILE["${issue_id}"]}"
	FINAL_LINES["${issue_id}"]="${PRIOR_LINES["${issue_id}"]}"
	FINAL_LENS["${issue_id}"]="${PRIOR_LENS["${issue_id}"]}"
	FINAL_SEVERITY["${issue_id}"]="${PRIOR_SEVERITY["${issue_id}"]}"
	FINAL_STATUS["${issue_id}"]="${status}"
	FINAL_FIRST_SEEN["${issue_id}"]="${PRIOR_FIRST_SEEN["${issue_id}"]}"
	FINAL_LAST_SEEN["${issue_id}"]="${ITERATION}"
	FINAL_PERSIST["${issue_id}"]="${persist_count}"
	FINAL_OUTCOMES["${issue_id}"]="${PRIOR_OUTCOMES["${issue_id}"]}"
	SEEN_FINAL["${issue_id}"]=1
	display_outcomes="$(format_editor_outcomes_for_status "${PRIOR_OUTCOMES["${issue_id}"]}")"
	printf '%s\t%s\t%s\t%s:%s\t%s\t%s\n' \
		"${issue_id}" "${status}" "${persist_count}" \
		"${PRIOR_FILE["${issue_id}"]}" "${PRIOR_LINES["${issue_id}"]}" \
		"${PRIOR_LENS["${issue_id}"]}" "${display_outcomes}" >> "${status_tmp_file}"
done

{
	echo "=== LEDGER v1 ==="
	echo "PR_NUMBER: ${PR_NUMBER}"
	echo "FIRST_SEEN_ITERATION: ${header_first_seen}"
	echo "LAST_UPDATED_ITERATION: ${ITERATION}"
	echo "=== END HEADER ==="
	echo
} > "${new_entries_file}"

while IFS= read -r issue_id || [ -n "${issue_id}" ]; do
	[ -n "${issue_id}" ] || continue
	write_current_entry "${new_entries_file}" \
		"${issue_id}" \
		"${FINAL_FILE["${issue_id}"]}" \
		"${FINAL_LINES["${issue_id}"]}" \
		"${FINAL_LENS["${issue_id}"]}" \
		"${FINAL_SEVERITY["${issue_id}"]}" \
		"${FINAL_STATUS["${issue_id}"]}" \
		"${FINAL_FIRST_SEEN["${issue_id}"]}" \
		"${FINAL_LAST_SEEN["${issue_id}"]}" \
		"${FINAL_PERSIST["${issue_id}"]}" \
		"${FINAL_OUTCOMES["${issue_id}"]}"
	echo >> "${new_entries_file}"
done < <(printf '%s\n' "${!SEEN_FINAL[@]}" | LC_ALL=C sort)

mv "${new_entries_file}" "${REVIEW_LEDGER_PATH}"

if [ -s "${status_tmp_file}" ]; then
	LC_ALL=C sort -t $'\t' -k1,1 "${status_tmp_file}" > "${LEDGER_STATUS_FILE}"
else
	: > "${LEDGER_STATUS_FILE}"
fi

rewrite_review_issues_without_residuals "${REVIEW_ISSUES_FILE}" "${keep_block_ids_file}" "${residual_block_ids_file}" "${filtered_review_issues_file}"
mv "${filtered_review_issues_file}" "${REVIEW_ISSUES_FILE}"

review_log "pr=${PR_NUMBER} iteration=${ITERATION} ledger_prior_entries=${prior_entries_count} transitions=NEW:${COUNT_TRANSITIONS[NEW]},PERSISTING:${COUNT_TRANSITIONS[PERSISTING]},FIXED:${COUNT_TRANSITIONS[FIXED]},RESURGENT:${COUNT_TRANSITIONS[RESURGENT]},accepted-residual:${COUNT_TRANSITIONS[accepted-residual]} accepted_residual_added=${accepted_residual_added} ledger_reset=${ledger_reset} hash_collision=${auto_collision_count}"

exit 0
