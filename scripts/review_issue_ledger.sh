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
	local line_start anchor_line ext range_start range_end
	if [ -z "${file_path}" ]; then
		printf '%s\n' ""
		return
	fi
	if [[ "${file_path}" == /* ]] || [[ "${file_path}" =~ (^|/)\.\.(/|$) ]] || [[ "${file_path}" =~ [[:cntrl:]] ]]; then
		printf '%s\n' ""
		return
	fi
	if [[ "${file_path}" == .git ]] || [[ "${file_path}" == .git/* ]] || [[ "${file_path}" == */.git ]] || [[ "${file_path}" == */.git/* ]]; then
		printf '%s\n' ""
		return
	fi
	if [[ "${line_spec}" =~ ^([0-9]+)(-([0-9]+))?$ ]]; then
		line_start="${BASH_REMATCH[1]}"
	else
		line_start=""
	fi
	if [ -z "${line_start}" ]; then
		line_start=1
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
	local object_type
	object_type="$(git cat-file -t "HEAD:${file_path}" 2>/dev/null || true)"
	if [ "${object_type}" = "blob" ]; then
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
	gawk -v out_file="${out_file}" '
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
			gsub(/\t/, " ", $0)
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
	local convergence_value="${12:--}"
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
		if [ -n "${convergence_value}" ] && [ "${convergence_value}" != "-" ]; then
			echo "CONVERGENCE: advisory-lone"
			echo "CONVERGENCE_ROUND: ${convergence_value%%|*}"
			echo "CONVERGENCE_REVIEWERS: ${convergence_value#*|}"
		fi
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
		function flush_entry(    encoded_outcomes, conv) {
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
			# Convergence column (issue #10056) is never empty, so tab-IFS
			# reads cannot shift the outcomes column.
			conv = "-"
			if (conv_flag == "advisory-lone" && conv_round ~ /^[0-9]+$/) {
				gsub(/[^A-Za-z0-9_.,-]/, "", conv_reviewers)
				if (conv_reviewers == "") {
					conv_reviewers = "none"
				}
				conv = conv_round "|" conv_reviewers
			}
			print entry_id "\t" file "\t" lines "\t" lens "\t" severity "\t" status "\t" first_seen "\t" last_seen "\t" persist_count "\t" conv "\t" encoded_outcomes >> out_file
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
				conv_flag = ""
				conv_round = ""
				conv_reviewers = ""
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
			if (line ~ /^CONVERGENCE:[[:space:]]*/) {
				conv_flag = trim(substr(line, index(line, ":") + 1))
				in_editor_outcomes = 0
				next
			}
			if (line ~ /^CONVERGENCE_ROUND:[[:space:]]*/) {
				conv_round = trim(substr(line, index(line, ":") + 1))
				in_editor_outcomes = 0
				next
			}
			if (line ~ /^CONVERGENCE_REVIEWERS:[[:space:]]*/) {
				conv_reviewers = trim(substr(line, index(line, ":") + 1))
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
	gawk -v keep_file="${keep_ids_file}" -v residual_file="${residual_ids_file}" '
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

# ── Review convergence rule (issue #10056) ─────────────────────────
# From round REVIEW_CONVERGENCE_FROM_ROUND (default 3) on, a consolidated
# finding blocks only when REVIEW_CONVERGENCE_MIN_REVIEWERS (default 2) or
# more reviewer files support it, it carries a FLOOR_CRITICAL_KEYWORD:* floor
# tag on the same file within +/-3 lines, or its LENS is a security lens
# (lens 1 or lens 9 of prompts/review-consolidator.txt). Any other
# must-fix / nice-to-have / unclassified finding is reclassified
# `advisory-lone`: it stays in the ledger, the PR comment and the judge notes
# but the editor gets only an ADVISORY stub. FLOOR_HIGH_CONFIDENCE and
# FLOOR_MULTI_REVIEWER never exempt (operator decision AD-1 / AD-2).
# The reviewer count comes from the reviewer bundle's per-file sections, never
# from the consolidator's FLAGGED_BY claim alone. REVIEW_CONVERGENCE_ENABLED=false
# skips every step below (no file, no log line).
review_convergence_load_config()
{
	local enabled_raw from_raw min_raw count_raw
	enabled_raw="$(printf '%s' "${REVIEW_CONVERGENCE_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${enabled_raw}" in
		0|false|no|off)
			REVIEW_CONVERGENCE_ON=0
			;;
		*)
			REVIEW_CONVERGENCE_ON=1
			;;
	esac
	from_raw="${REVIEW_CONVERGENCE_FROM_ROUND:-3}"
	if [[ "${from_raw}" =~ ^[2-5]$ ]]; then
		REVIEW_CONVERGENCE_FROM="${from_raw}"
	else
		REVIEW_CONVERGENCE_FROM=3
	fi
	min_raw="${REVIEW_CONVERGENCE_MIN_REVIEWERS:-2}"
	if [[ "${min_raw}" =~ ^[0-9]{1,3}$ ]] && [ "$((10#${min_raw}))" -ge 2 ]; then
		REVIEW_CONVERGENCE_MIN="$((10#${min_raw}))"
	else
		REVIEW_CONVERGENCE_MIN=2
	fi
	# Round = autofix commits already counted toward MAX_AUTOFIX_ITERATIONS
	# (the retrigger_guard step output) plus one; the script's own ITERATION
	# (AUTOFIX_ITERATION) is the fallback when the step env is absent.
	count_raw="${REVIEW_CONVERGENCE_AUTOFIX_COUNT:-}"
	if [[ "${count_raw}" =~ ^[0-9]{1,6}$ ]]; then
		REVIEW_CONVERGENCE_ROUND="$((10#${count_raw} + 1))"
	else
		REVIEW_CONVERGENCE_ROUND="${ITERATION}"
	fi
	REVIEW_CONVERGENCE_ADVISORY_FILE="${REVIEW_CONVERGENCE_ADVISORY_FILE:-${RUNTIME_DIR}/review_convergence_advisory.txt}"
	REVIEWER_BUNDLE_FILE="${REVIEWER_BUNDLE_FILE:-${RUNTIME_DIR}/reviewer_bundle.txt}"
}

review_convergence_log()
{
	local blocking="$1" advisory="$2" applied="$3" reason="$4" outcome="continue"
	# A failed classification applied nothing and every finding still blocks,
	# so it never reports an approval.
	if [ "${blocking}" = "0" ] && [ "${reason}" != "error" ]; then
		outcome="approve"
	fi
	printf 'REVIEW_CONVERGENCE pr=%s round=%s blocking=%s advisory_lone=%s outcome=%s applied=%s reason=%s\n' \
		"${PR_NUMBER}" "${REVIEW_CONVERGENCE_ROUND}" "${blocking}" "${advisory}" "${outcome}" "${applied}" "${reason}" >&2
}

review_convergence_classify()
{
	if [ "${REVIEW_CONVERGENCE_ON}" != "1" ]; then
		return 0
	fi
	local conv_tmp_dir bundle_in floor_in issues_in force_reason="" stats_line
	local conv_blocking conv_advisory conv_applied conv_reason
	mkdir -p "$(dirname "${REVIEW_CONVERGENCE_ADVISORY_FILE}")"
	: > "${REVIEW_CONVERGENCE_ADVISORY_FILE}"
	bundle_in="${REVIEWER_BUNDLE_FILE}"
	floor_in="${FLOOR_TAGS_FILE}"
	issues_in="${REVIEW_ISSUES_FILE}"
	# A missing input never relaxes the rule: findings stay blocking.
	if [ ! -f "${bundle_in}" ] || [ ! -r "${bundle_in}" ]; then
		bundle_in="/dev/null"
		force_reason="input_unavailable"
	fi
	if [ ! -f "${floor_in}" ] || [ ! -r "${floor_in}" ]; then
		floor_in="/dev/null"
		force_reason="input_unavailable"
	fi
	if [ ! -f "${issues_in}" ] || [ ! -r "${issues_in}" ]; then
		issues_in="/dev/null"
	fi
	if ! conv_tmp_dir="$(mktemp -d)"; then
		review_convergence_log 0 0 0 error
		return 0
	fi
	if ! gawk -v apply_round="${REVIEW_CONVERGENCE_ROUND}" -v from_round="${REVIEW_CONVERGENCE_FROM}" \
		-v min_reviewers="${REVIEW_CONVERGENCE_MIN}" -v tolerance=3 -v force_reason="${force_reason}" \
		-v adv_out="${conv_tmp_dir}/advisory.tsv" -v stats_out="${conv_tmp_dir}/stats.txt" '
		function trim(s) {
			sub(/^[[:space:]]+/, "", s)
			sub(/[[:space:]]+$/, "", s)
			return s
		}
		function norm_path(p) {
			p = trim(p)
			gsub(/[`"\047]/, "", p)
			sub(/^\.\//, "", p)
			return p
		}
		function norm_text(s) {
			s = tolower(s)
			gsub(/[[:space:]]+/, " ", s)
			return trim(s)
		}
		function clean_field(s, maxlen) {
			gsub(/[[:cntrl:]]/, " ", s)
			gsub(/[`@<>]/, "", s)
			gsub(/[[:space:]]+/, " ", s)
			s = trim(s)
			if (maxlen > 0 && length(s) > maxlen) {
				s = substr(s, 1, maxlen)
				# Never leave a cut UTF-8 sequence at the end.
				sub(/[\300-\377][\200-\277]*$/, "", s)
			}
			return s
		}
		function clean_name(s) {
			gsub(/[^A-Za-z0-9_.-]/, "", s)
			return s
		}
		function value_of(l) {
			return trim(substr(l, index(l, ":") + 1))
		}
		function evidence_text(e) {
			sub(/^[[:space:]]*[A-Za-z0-9_.-]+>[[:space:]]*/, "", e)
			e = trim(e)
			if (e ~ /^".*"$/) {
				e = substr(e, 2, length(e) - 2)
			}
			return e
		}
		function section_open(path,    base, key) {
			base = path
			sub(/^.*\//, "", base)
			sub(/\.txt$/, "", base)
			key = base
			sub(/^review_/, "", key)
			if (key == "") {
				key = "unknown_reviewer"
			}
			if (!(key in sec_index)) {
				nsec++
				sec_index[key] = nsec
				sec_name[nsec] = key
				sec_full[nsec] = base
				sec_text[nsec] = ""
				sec_nonempty[nsec] = 0
				nanc[nsec] = 0
				pending_file[nsec] = ""
			}
			cur = sec_index[key]
		}
		function add_anchor(s, f, ln) {
			f = norm_path(f)
			if (f == "" || ln == "") {
				return
			}
			nanc[s]++
			anc_file[s, nanc[s]] = f
			anc_line[s, nanc[s]] = ln + 0
		}
		# Same anchor shapes as review_parse_consolidator.sh emit_anchor
		# (path:line, and a FILE: line followed by a LINE: line); path:line is
		# also accepted mid-line, which can only add support (toward blocking).
		function scan_content_line(s, line,    rest, hit, f, l) {
			if (line ~ /[^[:space:]]/) {
				sec_nonempty[s] = 1
			}
			sec_text[s] = sec_text[s] " " norm_text(line)
			rest = line
			gsub(/[`"\047]/, " ", rest)
			while (match(rest, /([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*):([0-9]+)/, hit)) {
				add_anchor(s, hit[1], hit[3])
				rest = substr(rest, RSTART + RLENGTH)
			}
			if (match(line, /^[[:space:]]*[Ff][Ii][Ll][Ee]:[[:space:]]*([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*)[[:space:]]*$/, f)) {
				pending_file[s] = f[1]
				return
			}
			if (pending_file[s] != "" && match(line, /^[[:space:]]*[Ll][Ii][Nn][Ee]:[[:space:]]*([0-9]+)/, l)) {
				add_anchor(s, pending_file[s], l[1])
				pending_file[s] = ""
				return
			}
			if (pending_file[s] != "" && line ~ /^[[:space:]]*$/) {
				pending_file[s] = ""
			}
		}
		function prepare(    s) {
			if (prepared) {
				return
			}
			prepared = 1
			panel = 0
			for (s = 1; s <= nsec; s++) {
				if (sec_nonempty[s]) {
					panel++
				}
			}
			applied = 0
			if (force_reason != "") {
				reason = force_reason
			} else if (apply_round + 0 < from_round + 0) {
				reason = "disabled_round"
			} else if (panel < min_reviewers + 1) {
				reason = "panel_too_small"
			} else {
				applied = 1
				reason = "applied"
			}
		}
		function emit_plain(    i) {
			for (i = 1; i <= nb; i++) {
				print blk[i]
			}
		}
		function emit_rewritten(new_cls, extra,    i, l, done) {
			done = 0
			for (i = 1; i <= nb; i++) {
				l = blk[i]
				if (l ~ /^CONVERGENCE_(PRIOR_CLASSIFICATION|REVIEWERS|ROUND):/) {
					continue
				}
				if (l ~ /^CLASSIFICATION:/) {
					if (!done) {
						print "CLASSIFICATION: " new_cls
						if (extra != "") {
							printf "%s", extra
						}
						done = 1
					}
					continue
				}
				if (i == nb && !done) {
					print "CLASSIFICATION: " new_cls
					if (extra != "") {
						printf "%s", extra
					}
					done = 1
				}
				print l
			}
		}
		function process_block(    i, l, cls, cls_l, prior, p, has_round, file, lines, lens, sev, flagged, in_ev, nev, ev, r, lo, hi, t, has_range, s, k, q, sup, cnt, reviewers, exempt, ulens, eff, summary, extra) {
			cls = ""
			prior = ""
			has_round = 0
			file = ""
			lines = ""
			lens = ""
			sev = ""
			flagged = ""
			in_ev = 0
			nev = 0
			for (i = 2; i < nb; i++) {
				l = blk[i]
				if (in_ev) {
					if (l ~ /^  /) {
						t = substr(l, 3)
						sub(/^UNTRUSTED_DATA:[[:space:]]?/, "", t)
						nev++
						ev[nev] = t
						continue
					}
					in_ev = 0
				}
				if (l ~ /^EVIDENCE:[[:space:]]*$/) {
					in_ev = 1
				} else if (l ~ /^FILE:/) {
					file = norm_path(value_of(l))
				} else if (l ~ /^LINES:/) {
					lines = value_of(l)
				} else if (l ~ /^LENS:/) {
					lens = value_of(l)
				} else if (l ~ /^SEVERITY:/) {
					sev = value_of(l)
				} else if (l ~ /^FLAGGED_BY:/) {
					flagged = value_of(l)
				} else if (l ~ /^CLASSIFICATION:/) {
					cls = value_of(l)
				} else if (l ~ /^CONVERGENCE_PRIOR_CLASSIFICATION:/) {
					prior = value_of(l)
				} else if (l ~ /^CONVERGENCE_ROUND:/) {
					has_round = 1
				}
			}
			cls_l = tolower(cls)
			eff = cls_l
			# A model-written advisory-lone is never trusted: re-evaluate it.
			if (cls_l == "advisory-lone") {
				eff = "unclassified"
				p = tolower(prior)
				if (has_round && (p == "must-fix" || p == "nice-to-have" || p == "unclassified")) {
					eff = p
				}
			}
			if (!(eff == "" || eff == "must-fix" || eff == "nice-to-have" || eff == "unclassified")) {
				emit_plain()
				return
			}
			has_range = 0
			if (match(lines, /^([0-9]+)([[:space:]]*-[[:space:]]*([0-9]+))?/, r)) {
				lo = r[1] + 0
				hi = (r[3] != "") ? r[3] + 0 : lo
				if (hi < lo) {
					t = lo
					lo = hi
					hi = t
				}
				has_range = 1
			}
			cnt = 0
			reviewers = ""
			exempt = 0
			if (applied) {
				for (s = 1; s <= nsec; s++) {
					sup[s] = 0
					named[s] = 0
					if (!sec_nonempty[s]) {
						continue
					}
					if (has_range && file != "") {
						for (k = 1; k <= nanc[s]; k++) {
							if (anc_file[s, k] == file && anc_line[s, k] >= lo - tolerance && anc_line[s, k] <= hi + tolerance) {
								sup[s] = 1
								break
							}
						}
					}
					if (!sup[s]) {
						for (k = 1; k <= nev; k++) {
							q = norm_text(evidence_text(ev[k]))
							if (length(q) >= 24 && index(sec_text[s], q) > 0) {
								sup[s] = 1
								break
							}
						}
					}
				}
				# Every reviewer section whose own file supports the finding
				# counts, whether or not FLAGGED_BY names it: the count comes
				# from the per-reviewer files, so an incomplete model
				# attribution never demotes a finding (safe direction), and a
				# FLAGGED_BY name without file support never adds one.
				for (s = 1; s <= nsec; s++) {
					if (sup[s]) {
						cnt++
						reviewers = reviewers (reviewers == "" ? "" : ",") clean_name(sec_name[s])
					}
				}
				# Security lenses (lens 1 and lens 9) always block. LENS is
				# model-written and passed through verbatim, so match by keyword,
				# case-insensitively; an empty or UNKNOWN_LENS lens cannot be
				# shown to be non-security and also keeps blocking.
				ulens = toupper(trim(lens))
				if (ulens ~ /SECURITY|TRUST-BOUNDARY|IMPLICIT-EXECUTION/ || ulens == "" || ulens == "UNKNOWN_LENS" || file == "") {
					exempt = 1
				}
				for (k = 1; !exempt && k <= ncrit; k++) {
					if (crit_file[k] == file && (!has_range || (crit_line[k] >= lo - tolerance && crit_line[k] <= hi + tolerance))) {
						exempt = 1
					}
				}
			}
			if (reviewers == "") {
				reviewers = "none"
			}
			if (applied && cnt < min_reviewers + 0 && !exempt) {
				advisory++
				if (eff == "") {
					eff = "unclassified"
				}
				extra = "CONVERGENCE_PRIOR_CLASSIFICATION: " eff "\nCONVERGENCE_REVIEWERS: " reviewers "\nCONVERGENCE_ROUND: " apply_round "\n"
				emit_rewritten("advisory-lone", extra)
				summary = (nev > 0) ? clean_field(evidence_text(ev[1]), 200) : ""
				if (summary == "") {
					summary = "no summary"
				}
				printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n", clean_field(block_id, 80), clean_field(file ":" (lines == "" ? "unknown" : lines), 300), clean_field(lens == "" ? "UNKNOWN_LENS" : lens, 80), clean_field(sev == "" ? "unknown" : sev, 20), reviewers, apply_round, summary > adv_out
				return
			}
			blocking++
			if (cls_l == "advisory-lone") {
				emit_rewritten(eff == "" ? "unclassified" : eff, "")
			} else {
				emit_plain()
			}
		}
		BEGIN {
			nsec = 0
			ncrit = 0
			cur = 0
			in_content = 0
			blocking = 0
			advisory = 0
			in_block = 0
			nb = 0
			printf "" > adv_out
		}
		ARGIND == 1 {
			line = $0
			sub(/\r$/, "", line)
			if (in_content) {
				if (bytes_limit >= 0) {
					consumed += length($0) + 1
					scan_content_line(cur, line)
					if (consumed >= bytes_limit) {
						in_content = 0
					}
					next
				}
				if (line == "CONTENT_END") {
					in_content = 0
					next
				}
				scan_content_line(cur, line)
				next
			}
			if (match(line, /^FILE_PATH:[[:space:]]*(.+)$/, fm)) {
				section_open(trim(fm[1]))
				has_bytes = 0
				next
			}
			if (cur > 0 && match(line, /^BYTES:[[:space:]]*([0-9]+)[[:space:]]*$/, bm)) {
				pending_bytes = bm[1] + 0
				has_bytes = 1
				next
			}
			# BYTES bounds the content, so reviewer text cannot forge a
			# CONTENT_END / FILE_PATH pair and add a fake reviewer section.
			if (cur > 0 && line == "CONTENT_START") {
				in_content = 1
				consumed = 0
				bytes_limit = has_bytes ? pending_bytes : -1
				has_bytes = 0
				if (bytes_limit == 0) {
					in_content = 0
				}
			}
			next
		}
		ARGIND == 2 {
			split($0, fr, "\t")
			if (fr[2] !~ /FLOOR_CRITICAL_KEYWORD:/) {
				next
			}
			if (!match(trim(fr[1]), /^(.+):([0-9]+)$/, am)) {
				next
			}
			ncrit++
			crit_file[ncrit] = norm_path(am[1])
			crit_line[ncrit] = am[2] + 0
			next
		}
		ARGIND == 3 {
			prepare()
			line = $0
			if (match(line, /^=== ISSUE[[:space:]]+(.+)[[:space:]]+===$/, im)) {
				if (in_block) {
					emit_plain()
				}
				in_block = 1
				block_id = trim(im[1])
				nb = 0
				blk[++nb] = line
				next
			}
			if (in_block) {
				blk[++nb] = line
				if (line ~ /^=== END ISSUE[[:space:]]+/) {
					process_block()
					in_block = 0
					nb = 0
				}
				next
			}
			print line
		}
		END {
			prepare()
			if (in_block) {
				emit_plain()
			}
			printf "blocking=%d advisory=%d applied=%d reason=%s panel=%d\n", blocking, advisory, applied, reason, panel > stats_out
		}
	' "${bundle_in}" "${floor_in}" "${issues_in}" > "${conv_tmp_dir}/issues.txt"; then
		printf '::warning::review convergence step failed; review_issues.txt left unchanged\n' >&2
		rm -rf "${conv_tmp_dir}"
		review_convergence_log 0 0 0 error
		return 0
	fi
	stats_line="$(cat "${conv_tmp_dir}/stats.txt" 2>/dev/null || true)"
	conv_blocking="$(printf '%s' "${stats_line}" | sed -n 's/.*blocking=\([0-9]*\).*/\1/p')"
	conv_advisory="$(printf '%s' "${stats_line}" | sed -n 's/.*advisory=\([0-9]*\).*/\1/p')"
	conv_applied="$(printf '%s' "${stats_line}" | sed -n 's/.*applied=\([01]\).*/\1/p')"
	conv_reason="$(printf '%s' "${stats_line}" | sed -n 's/.*reason=\([a-z_]*\).*/\1/p')"
	if [ -z "${conv_blocking}" ] || [ -z "${conv_advisory}" ] || [ -z "${conv_applied}" ] || [ -z "${conv_reason}" ]; then
		rm -rf "${conv_tmp_dir}"
		review_convergence_log 0 0 0 error
		return 0
	fi
	if [ "${issues_in}" != "/dev/null" ]; then
		mv "${conv_tmp_dir}/issues.txt" "${REVIEW_ISSUES_FILE}"
	fi
	mv "${conv_tmp_dir}/advisory.tsv" "${REVIEW_CONVERGENCE_ADVISORY_FILE}"
	rm -rf "${conv_tmp_dir}"
	review_convergence_log "${conv_blocking}" "${conv_advisory}" "${conv_applied}" "${conv_reason}"
	return 0
}

# Turns each advisory-lone ISSUE block the convergence step listed into an
# `=== ADVISORY ISSUE <id> ===` stub without CURRENT_CODE or
# SUGGESTED_APPROACH, so the editor has nothing to act on. Runs after the
# ledger's residual rewrite: an accepted-residual RESIDUAL stub wins.
review_convergence_stub_advisory_blocks()
{
	if [ "${REVIEW_CONVERGENCE_ON:-0}" != "1" ] || [ ! -s "${REVIEW_CONVERGENCE_ADVISORY_FILE}" ] || [ ! -f "${REVIEW_ISSUES_FILE}" ]; then
		return 0
	fi
	local stub_tmp
	if ! stub_tmp="$(mktemp)"; then
		return 0
	fi
	if gawk -v adv_file="${REVIEW_CONVERGENCE_ADVISORY_FILE}" '
		function trim(s) {
			sub(/^[[:space:]]+/, "", s)
			sub(/[[:space:]]+$/, "", s)
			return s
		}
		function flush_block(    i, l, cls, out, conv_out) {
			cls = ""
			out = ""
			conv_out = ""
			for (i = 2; i < nb; i++) {
				l = blk[i]
				if (l ~ /^(FILE|LINES|LENS|SEVERITY|FLAGGED_BY):/) {
					out = out l "\n"
				} else if (l ~ /^CONVERGENCE_(PRIOR_CLASSIFICATION|REVIEWERS|ROUND):/) {
					conv_out = conv_out l "\n"
				} else if (l ~ /^CLASSIFICATION:/) {
					cls = trim(substr(l, index(l, ":") + 1))
				}
			}
			if (cls != "advisory-lone" || !(block_id in summary)) {
				for (i = 1; i <= nb; i++) {
					print blk[i]
				}
				return
			}
			print "=== ADVISORY ISSUE " block_id " ==="
			printf "%s", out
			print "CLASSIFICATION: advisory-lone"
			printf "%s", conv_out
			print "SUMMARY: " summary[block_id]
			print "=== END ADVISORY ISSUE " block_id " ==="
		}
		BEGIN {
			while ((getline row < adv_file) > 0) {
				split(row, f, "\t")
				if (f[1] != "") {
					summary[f[1]] = f[7]
				}
			}
			close(adv_file)
			in_block = 0
			nb = 0
		}
		{
			line = $0
			if (match(line, /^=== ISSUE[[:space:]]+(.+)[[:space:]]+===$/, m)) {
				if (in_block) {
					for (i = 1; i <= nb; i++) {
						print blk[i]
					}
				}
				in_block = 1
				block_id = trim(m[1])
				nb = 0
				blk[++nb] = line
				next
			}
			if (in_block) {
				blk[++nb] = line
				if (line ~ /^=== END ISSUE[[:space:]]+/) {
					flush_block()
					in_block = 0
					nb = 0
				}
				next
			}
			print line
		}
		END {
			if (in_block) {
				for (i = 1; i <= nb; i++) {
					print blk[i]
				}
			}
		}
	' "${REVIEW_ISSUES_FILE}" > "${stub_tmp}"; then
		mv "${stub_tmp}" "${REVIEW_ISSUES_FILE}"
	else
		printf '::warning::review convergence stub rewrite failed; review_issues.txt left unchanged\n' >&2
		rm -f "${stub_tmp}"
	fi
	return 0
}

RUNTIME_DIR="${RUNTIME_DIR:?RUNTIME_DIR is required}"
PR_NUMBER="${PR_NUMBER:-0}"
PR_NUMBER="${PR_NUMBER//[^0-9]/}"
: "${PR_NUMBER:=0}"
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
# Per-PR ledger path so concurrent PRs never share a file (no cross-PR merge
# conflicts on main). PR_NUMBER defaults to 0 above, so local/dev runs without
# PR context land on .ai/review_issue_ledger/pr-0.txt — still covered by the
# .ai/review_issue_ledger/ gitignore rule.
REVIEW_LEDGER_PATH="${REVIEW_LEDGER_PATH:-.ai/review_issue_ledger/pr-${PR_NUMBER}.txt}"

mkdir -p "$(dirname "${LEDGER_STATUS_FILE}")"

review_convergence_load_config
review_convergence_classify

if [ "${REVIEW_LEDGER_ENABLED}" = "0" ]; then
	: > "${LEDGER_STATUS_FILE}"
	review_convergence_stub_advisory_blocks
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
header_last_updated="${ITERATION}"
if [ -s "${prior_header_file}" ]; then
	while IFS='=' read -r key value; do
		case "${key}" in
			FIRST_SEEN_ITERATION)
				if [[ "${value}" =~ ^[0-9]+$ ]]; then
					header_first_seen="${value}"
				fi
				;;
			LAST_UPDATED_ITERATION)
				if [[ "${value}" =~ ^[0-9]+$ ]] && [ "${value}" -gt "${header_last_updated}" ]; then
					header_last_updated="${value}"
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
declare -A PRIOR_CONVERGENCE=()

if [ -s "${prior_entries_file}" ]; then
	while IFS=$'\t' read -r issue_id file_path lines lens severity status first_seen last_seen persist_count prior_convergence editor_outcomes || [ -n "${issue_id}" ]; do
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
		PRIOR_CONVERGENCE["${issue_id}"]="${prior_convergence:--}"
	done < "${prior_entries_file}"
fi

# Convergence advisory rows (block_id -> round|reviewers), issue #10056.
declare -A CONVERGENCE_BY_BLOCK=()
if [ "${REVIEW_CONVERGENCE_ON}" = "1" ] && [ -s "${REVIEW_CONVERGENCE_ADVISORY_FILE}" ]; then
	while IFS=$'\t' read -r conv_block_id _conv_anchor _conv_lens _conv_severity conv_reviewers conv_round _conv_summary || [ -n "${conv_block_id}" ]; do
		[ -n "${conv_block_id}" ] || continue
		[[ "${conv_round}" =~ ^[0-9]+$ ]] || continue
		conv_reviewers="${conv_reviewers//[^A-Za-z0-9_.,-]/}"
		CONVERGENCE_BY_BLOCK["${conv_block_id}"]="${conv_round}|${conv_reviewers:-none}"
	done < "${REVIEW_CONVERGENCE_ADVISORY_FILE}"
fi

declare -A CURRENT_FILE=()
declare -A CURRENT_LINES=()
declare -A CURRENT_LENS=()
declare -A CURRENT_SEVERITY=()
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
declare -A FINAL_CONVERGENCE=()
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
	last_seen="${ITERATION}"
	prior_last_seen="${PRIOR_LAST_SEEN["${issue_id}"]:-0}"
	if [[ "${prior_last_seen}" =~ ^[0-9]+$ ]] && [ "${prior_last_seen}" -gt "${last_seen}" ]; then
		last_seen="${prior_last_seen}"
	fi
	if [[ "${first_seen}" =~ ^[0-9]+$ ]] && [ "${first_seen}" -gt "${last_seen}" ]; then
		last_seen="${first_seen}"
	fi
	FINAL_LAST_SEEN["${issue_id}"]="${last_seen}"
	FINAL_PERSIST["${issue_id}"]="${persist_count}"
	FINAL_OUTCOMES["${issue_id}"]="${PRIOR_OUTCOMES["${issue_id}"]:-}"
	FINAL_CONVERGENCE["${issue_id}"]="-"
	if [ "${CURRENT_CLASSIFICATION["${issue_id}"]}" = "advisory-lone" ] && [ -n "${CURRENT_BLOCK_ID["${issue_id}"]:-}" ]; then
		FINAL_CONVERGENCE["${issue_id}"]="${CONVERGENCE_BY_BLOCK["${CURRENT_BLOCK_ID["${issue_id}"]}"]:--}"
	fi
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
	last_seen="${ITERATION}"
	prior_last_seen="${PRIOR_LAST_SEEN["${issue_id}"]:-0}"
	if [[ "${prior_last_seen}" =~ ^[0-9]+$ ]] && [ "${prior_last_seen}" -gt "${last_seen}" ]; then
		last_seen="${prior_last_seen}"
	fi
	prior_first_seen="${PRIOR_FIRST_SEEN["${issue_id}"]:-0}"
	if [[ "${prior_first_seen}" =~ ^[0-9]+$ ]] && [ "${prior_first_seen}" -gt "${last_seen}" ]; then
		last_seen="${prior_first_seen}"
	fi
	FINAL_LAST_SEEN["${issue_id}"]="${last_seen}"
	FINAL_PERSIST["${issue_id}"]="${persist_count}"
	FINAL_OUTCOMES["${issue_id}"]="${PRIOR_OUTCOMES["${issue_id}"]}"
	FINAL_CONVERGENCE["${issue_id}"]="${PRIOR_CONVERGENCE["${issue_id}"]:--}"
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
	echo "LAST_UPDATED_ITERATION: ${header_last_updated}"
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
		"${FINAL_OUTCOMES["${issue_id}"]}" \
		"${FINAL_CONVERGENCE["${issue_id}"]:--}"
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
review_convergence_stub_advisory_blocks

review_log "pr=${PR_NUMBER} iteration=${ITERATION} ledger_prior_entries=${prior_entries_count} transitions=NEW:${COUNT_TRANSITIONS[NEW]},PERSISTING:${COUNT_TRANSITIONS[PERSISTING]},FIXED:${COUNT_TRANSITIONS[FIXED]},RESURGENT:${COUNT_TRANSITIONS[RESURGENT]},accepted-residual:${COUNT_TRANSITIONS[accepted-residual]} accepted_residual_added=${accepted_residual_added} ledger_reset=${ledger_reset} hash_collision=${auto_collision_count}"

exit 0
