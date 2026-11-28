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
		unreachable)
			# Issue #10057: sticky only while the switch is on; switched off
			# it maps to PERSISTING, exactly as code without the status did.
			if [ "${REVIEW_UNREACHABLE_ACTIVE:-0}" = "1" ]; then
				printf '%s' "${status}"
			else
				printf '%s' "PERSISTING"
			fi
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
	local unreachable_reason="${12:-}"
	local unreachable_security="${13:-}"
	local unreachable_followup="${14:-}"
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
		# Issue #10057: written only for unreachable entries, so a ledger
		# without them stays byte-identical to the format before the feature.
		if [ "${status}" = "unreachable" ]; then
			echo "UNREACHABLE_REASON: ${unreachable_reason:-unknown}"
			echo "UNREACHABLE_SECURITY: ${unreachable_security:-false}"
			echo "UNREACHABLE_FOLLOWUP: ${unreachable_followup:-pending}"
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
		function dash(v) {
			return (v == "") ? "-" : v
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
			print entry_id "\t" file "\t" lines "\t" lens "\t" severity "\t" status "\t" first_seen "\t" last_seen "\t" persist_count "\t" dash(u_reason) "\t" dash(u_security) "\t" dash(u_followup) "\t" encoded_outcomes >> out_file
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
			unreachable_followup_issue = ""
			u_reason = ""
			u_security = ""
			u_followup = ""
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
				if (line ~ /^UNREACHABLE_FOLLOWUP_ISSUE:[[:space:]]*/) {
					unreachable_followup_issue = trim(substr(line, index(line, ":") + 1))
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
				u_reason = ""
				u_security = ""
				u_followup = ""
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
			if (line ~ /^UNREACHABLE_REASON:[[:space:]]*/) {
				u_reason = trim(substr(line, index(line, ":") + 1))
				in_editor_outcomes = 0
				next
			}
			if (line ~ /^UNREACHABLE_SECURITY:[[:space:]]*/) {
				u_security = trim(substr(line, index(line, ":") + 1))
				in_editor_outcomes = 0
				next
			}
			if (line ~ /^UNREACHABLE_FOLLOWUP:[[:space:]]*/) {
				u_followup = trim(substr(line, index(line, ":") + 1))
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
			if (unreachable_followup_issue != "") {
				print "UNREACHABLE_FOLLOWUP_ISSUE=" unreachable_followup_issue >> header_file
			}
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
	# Issue #10057: optional unreachable stubs (block<TAB>issue_id<TAB>followup)
	# and security-hold block ids. Both default to empty, which leaves the
	# output exactly as before.
	local unreachable_ids_file="${5:-/dev/null}"
	local hold_ids_file="${6:-/dev/null}"
	gawk -v keep_file="${keep_ids_file}" -v residual_file="${residual_ids_file}" -v unreach_file="${unreachable_ids_file}" -v hold_file="${hold_ids_file}" '
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
		function emit_unreachable_stub(    issue_id) {
			issue_id = unreach[block_id]
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
			printf "=== UNREACHABLE ISSUE %s ===\n", block_id
			printf "FILE: %s\n", file_value
			printf "LINES: %s\n", lines_value
			printf "LENS: %s\n", lens_value
			print "STATUS: unreachable"
			printf "LEDGER_ISSUE_ID: %s\n", issue_id
			printf "FOLLOWUP: %s\n", unreach_followup[block_id]
			printf "=== END UNREACHABLE ISSUE %s ===\n", block_id
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
			while ((getline line < unreach_file) > 0) {
				n = split(line, fields, "\t")
				if (n >= 3) {
					block = trim(fields[1])
					issue_id = trim(fields[2])
					if (block != "" && issue_id != "") {
						unreach[block] = issue_id
						unreach_followup[block] = trim(fields[3])
					}
				}
			}
			close(unreach_file)
			while ((getline line < hold_file) > 0) {
				line = trim(line)
				if (line != "") {
					hold[line] = 1
				}
			}
			close(hold_file)
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
				if (hold[block_id]) {
					sub(/\n/, "\nUNREACHABLE: security-hold\n", buffer)
				}
				printf "%s", buffer
			} else if (residual[block_id] != "") {
				emit_residual_stub()
			} else if (unreach[block_id] != "") {
				emit_unreachable_stub()
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

# Issue #10057: consumer protected-path rule, copied verbatim from the
# unstage guard in scripts/review_commit_changes.sh. A parity test keeps the
# two copies identical.
REVIEW_UNREACHABLE_PROTECTED_PREFIX_RE='^(prompts/|\.github/scripts/|\.github/prompts/|ai-memory/|\.codex-workflow-src/|\.codex-workflow-src-main/)'

ledger_support_file()
{
	local support_name="$1"
	local support_dir
	for support_dir in "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" "${SUPPORT_SCRIPTS_DIR:-}"; do
		if [ -n "${support_dir}" ] && [ -f "${support_dir}/${support_name}" ]; then
			printf '%s' "${support_dir}/${support_name}"
			return 0
		fi
	done
	return 1
}

unreachable_path_candidate()
{
	local candidate_path="$1"
	[ -n "${candidate_path}" ] || return 1
	[ "${candidate_path}" != "unknown" ] || return 1
	[[ "${candidate_path}" =~ ^[A-Za-z0-9._][A-Za-z0-9._/-]*$ ]] || return 1
	[[ ! "${candidate_path}" =~ (^|/)\.\.(/|$) ]] || return 1
	[[ "${candidate_path}" != *//* ]] || return 1
	return 0
}

# Issue #10057: decide from the editor's own write rules which cited paths it
# cannot change in this PR. Writes "<path><TAB><reason>" per unreachable path.
# Any helper that is missing or fails leaves its paths reachable (today's
# behaviour): a path is never marked unreachable when the rule cannot decide.
classify_unreachable_paths()
{
	local paths_file="$1"
	local out_file="$2"
	local work_dir="$3"
	local raw_file="${work_dir}/unreachable_raw.tsv"
	local wg_helper="" wg_out="" wg_rc=0
	local ws_helper="" host_root="" reach_report="${work_dir}/editor_reachable.tsv"
	: > "${out_file}"
	: > "${raw_file}"
	if [ ! -s "${paths_file}" ]; then
		return 0
	fi
	# (a) write guard, phase review_editor (.git/**, and .github/workflows/**
	# unless ALLOW_WORKFLOW_EDITS=true). One batched call for every path.
	if wg_helper="$(ledger_support_file write_guard.sh)"; then
		set +e
		wg_out="$( (source "${wg_helper}" && write_guard_check review_editor "${paths_file}") 2>&1)"
		wg_rc=$?
		set -e
		if [ "${wg_rc}" -eq 1 ]; then
			printf '%s\n' "${wg_out}" \
				| sed -n -E 's/^WRITE_GUARD_BLOCK: phase=review_editor path=([A-Za-z0-9._/-]+) reason=(blocked_glob|conditional_blocked_glob)( .*)?$/\1\twrite_guard/p' \
				>> "${raw_file}" || true
		fi
	fi
	# (b) consumer protected paths, unstaged by review_commit_changes.sh.
	if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ]; then
		{ grep -E "${REVIEW_UNREACHABLE_PROTECTED_PREFIX_RE}" "${paths_file}" || true; } \
			| sed 's/$/\tconsumer_protected_path/' >> "${raw_file}"
	fi
	# (c) review sandbox admission (allowed() + frozen .claude/ inventory).
	if ws_helper="$(ledger_support_file review_untrusted_workspace.py)"; then
		host_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
		if PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${ws_helper}" editor-reachable "${host_root}" "${paths_file}" "${reach_report}"; then
			awk -F '\t' '$1 == "unreachable" && NF == 3 { print $3 "\t" $2 }' "${reach_report}" >> "${raw_file}"
		else
			review_log "pr=${PR_NUMBER} iteration=${ITERATION} unreachable_check=error helper=editor-reachable"
		fi
	fi
	awk -F '\t' 'NF >= 2 && $1 != "" && !seen[$1]++' "${raw_file}" > "${out_file}"
}

# Issue #10057: a finding that names a failing check on the PR head (as
# listed in PR_CHECK_RUNS_CONTEXT_FILE) is never unreachable. Writes the ids
# of review_issues blocks whose text names such a check.
collect_ci_exempt_blocks()
{
	local issues_file="$1"
	local context_file="$2"
	local out_file="$3"
	: > "${out_file}"
	if [ ! -s "${issues_file}" ] || [ -z "${context_file}" ] || [ ! -s "${context_file}" ]; then
		return 0
	fi
	gawk -v ctx="${context_file}" '
		function trim(s) {
			sub(/^[[:space:]]+/, "", s)
			sub(/[[:space:]]+$/, "", s)
			return s
		}
		BEGIN {
			while ((getline line < ctx) > 0) {
				if (match(line, /^failed\[([0-9]+)\]\.name: (.*)$/, m)) {
					names[m[1]] = trim(m[2])
				} else if (match(line, /^failed\[([0-9]+)\]\.conclusion: (.*)$/, m)) {
					conclusions[m[1]] = trim(m[2])
				}
			}
			close(ctx)
			for (i in names) {
				c = conclusions[i]
				if ((c == "failure" || c == "action_required" || c == "startup_failure" || c == "stale") && length(names[i]) >= 3) {
					failing[names[i]] = 1
				}
			}
			block = ""
		}
		{
			line = $0
			if (match(line, /^=== ISSUE[[:space:]]+(.+)[[:space:]]+===$/, m)) {
				block = trim(m[1])
				text = ""
				next
			}
			if (line ~ /^=== END ISSUE[[:space:]]+/) {
				if (block != "") {
					for (n in failing) {
						if (index(text, n) > 0) {
							print block
							break
						}
					}
				}
				block = ""
				next
			}
			if (block != "") {
				text = text "\n" line
			}
		}
	' "${issues_file}" > "${out_file}"
}

# Issue #10057: the CI exemption above can only clear a finding when the
# check-runs context was actually collected. When the caller names a context
# file but it is missing, empty, or reports a collection failure, a finding
# that cites a failing head check cannot be told apart, so this round marks
# no new finding unreachable (fail toward reachable, like the path rules).
# Unset PR_CHECK_RUNS_CONTEXT_FILE (callers without the collector) and the
# "disabled" status keep the classification as before.
ci_context_undecidable()
{
	local context_file="$1"
	[ -n "${context_file}" ] || return 1
	[ -s "${context_file}" ] || return 0
	grep -Eq '^collection_status: (api_error|unavailable|writer_error)[[:space:]]*$' "${context_file}" 2>/dev/null
}

unreachable_lens_is_security()
{
	local lens_upper
	lens_upper="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
	case "${lens_upper}" in
		SECURITY*|*IMPLICIT-EXECUTION*|*TRUST-BOUNDARY*)
			return 0
			;;
	esac
	[ "${2:-}" = "SECURITY" ]
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
# Issue #10057: record findings on paths the review editor cannot change,
# keep them sticky, and hand them to the follow-up issue step. Default on;
# "false" (or 0/no/off) restores the behaviour before the feature exactly.
REVIEW_UNREACHABLE_FOLLOWUP_ENABLED="${REVIEW_UNREACHABLE_FOLLOWUP_ENABLED:-true}"
case "$(printf '%s' "${REVIEW_UNREACHABLE_FOLLOWUP_ENABLED}" | tr '[:upper:]' '[:lower:]')" in
	false|0|no|off)
		REVIEW_UNREACHABLE_ACTIVE=0
		;;
	*)
		REVIEW_UNREACHABLE_ACTIVE=1
		;;
esac
UNREACHABLE_NEW_FILE="${UNREACHABLE_NEW_FILE:-${RUNTIME_DIR}/review_unreachable_new.tsv}"
UNREACHABLE_ALL_FILE="${UNREACHABLE_ALL_FILE:-${RUNTIME_DIR}/review_unreachable_all.tsv}"
UNREACHABLE_PRE_ISSUES_FILE="${UNREACHABLE_PRE_ISSUES_FILE:-${RUNTIME_DIR}/review_issues.pre_unreachable.txt}"
rm -f "${UNREACHABLE_NEW_FILE}" "${UNREACHABLE_ALL_FILE}" "${UNREACHABLE_PRE_ISSUES_FILE}"

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
unreachable_paths_file="${tmp_dir}/unreachable_paths.txt"
unreachable_result_file="${tmp_dir}/unreachable_result.tsv"
ci_exempt_blocks_file="${tmp_dir}/ci_exempt_blocks.txt"
unreachable_block_ids_file="${tmp_dir}/unreachable_block_ids.tsv"
hold_block_ids_file="${tmp_dir}/hold_block_ids.txt"

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
header_unreachable_followup=""
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
			UNREACHABLE_FOLLOWUP_ISSUE)
				# "orphan" is the sentinel review_apply_fixes.sh writes when
				# the issue was created but its number could not be read.
				if [[ "${value}" =~ ^[1-9][0-9]{0,9}$ ]] || [ "${value}" = "orphan" ]; then
					header_unreachable_followup="${value}"
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
declare -A PRIOR_U_REASON=()
declare -A PRIOR_U_SECURITY=()
declare -A PRIOR_U_FOLLOWUP=()

if [ -s "${prior_entries_file}" ]; then
	while IFS=$'\t' read -r issue_id file_path lines lens severity status first_seen last_seen persist_count u_reason u_security u_followup editor_outcomes || [ -n "${issue_id}" ]; do
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
		[ "${u_reason}" != "-" ] || u_reason=""
		[ "${u_security}" = "true" ] || u_security="false"
		if [[ ! "${u_followup}" =~ ^#[1-9][0-9]{0,9}$ ]]; then
			u_followup="pending"
		fi
		PRIOR_U_REASON["${issue_id}"]="${u_reason}"
		PRIOR_U_SECURITY["${issue_id}"]="${u_security}"
		PRIOR_U_FOLLOWUP["${issue_id}"]="${u_followup}"
	done < "${prior_entries_file}"
fi

declare -A CURRENT_FILE=()
declare -A CURRENT_LINES=()
declare -A CURRENT_LENS=()
declare -A CURRENT_SEVERITY=()
declare -A CURRENT_CLASSIFICATION=()
declare -A CURRENT_BLOCK_ID=()
declare -A CURRENT_FLOOR=()
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
		CURRENT_FLOOR["${issue_id}"]="${floor_cat}"
	done < "${current_with_ids_file}"
fi

declare -A UNREACHABLE_PATH_REASON=()
declare -A CI_EXEMPT_BLOCK=()
if [ "${REVIEW_UNREACHABLE_ACTIVE}" = "1" ] && [ "${#CURRENT_PRESENT[@]}" -gt 0 ]; then
	: > "${unreachable_paths_file}"
	for issue_id in "${!CURRENT_PRESENT[@]}"; do
		if unreachable_path_candidate "${CURRENT_FILE["${issue_id}"]}"; then
			printf '%s\n' "${CURRENT_FILE["${issue_id}"]}" >> "${unreachable_paths_file}"
		fi
	done
	LC_ALL=C sort -u -o "${unreachable_paths_file}" "${unreachable_paths_file}"
	if ci_context_undecidable "${PR_CHECK_RUNS_CONTEXT_FILE:-}"; then
		# Sticky entries from earlier rounds stay unreachable; only new
		# classification is skipped for this round.
		: > "${unreachable_result_file}"
		review_log "pr=${PR_NUMBER} iteration=${ITERATION} unreachable_check=skipped reason=ci_context_unavailable"
	else
		classify_unreachable_paths "${unreachable_paths_file}" "${unreachable_result_file}" "${tmp_dir}"
	fi
	while IFS=$'\t' read -r u_path u_reason || [ -n "${u_path}" ]; do
		[ -n "${u_path}" ] && [ -n "${u_reason}" ] || continue
		UNREACHABLE_PATH_REASON["${u_path}"]="${u_reason}"
	done < "${unreachable_result_file}"
	collect_ci_exempt_blocks "${REVIEW_ISSUES_FILE}" "${PR_CHECK_RUNS_CONTEXT_FILE:-}" "${ci_exempt_blocks_file}"
	while IFS= read -r exempt_block || [ -n "${exempt_block}" ]; do
		[ -n "${exempt_block}" ] || continue
		CI_EXEMPT_BLOCK["${exempt_block}"]=1
	done < "${ci_exempt_blocks_file}"
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
declare -A FINAL_U_REASON=()
declare -A FINAL_U_SECURITY=()
declare -A FINAL_U_FOLLOWUP=()
declare -A SEEN_FINAL=()
declare -A COUNT_TRANSITIONS=([NEW]=0 [PERSISTING]=0 [FIXED]=0 [RESURGENT]=0 [accepted-residual]=0 [unreachable]=0)
: > "${unreachable_block_ids_file}"
: > "${hold_block_ids_file}"
unreachable_new_rows=""
unreachable_all_rows=""

accepted_residual_added=0

for issue_id in "${!CURRENT_PRESENT[@]}"; do
	prior_status="${PRIOR_STATUS["${issue_id}"]:-}"
	prior_persist="${PRIOR_PERSIST["${issue_id}"]:-0}"
	if ! [[ "${prior_persist}" =~ ^[0-9]+$ ]]; then
		prior_persist=0
	fi
	current_block="${CURRENT_BLOCK_ID["${issue_id}"]:-}"
	u_reason=""
	u_security="false"
	u_followup=""
	if [ "${prior_status}" = "unreachable" ] && [ -z "${CI_EXEMPT_BLOCK["${current_block:-_}"]:-}" ]; then
		# Issue #10057: sticky; no persist-count growth, no re-check.
		status="unreachable"
		persist_count="${prior_persist}"
		first_seen="${PRIOR_FIRST_SEEN["${issue_id}"]:-${ITERATION}}"
		u_reason="${PRIOR_U_REASON["${issue_id}"]:-}"
		u_security="${PRIOR_U_SECURITY["${issue_id}"]:-false}"
		u_followup="${PRIOR_U_FOLLOWUP["${issue_id}"]:-pending}"
	elif [ -z "${prior_status}" ]; then
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
	if [ "${REVIEW_UNREACHABLE_ACTIVE}" = "1" ] && [ "${status}" != "unreachable" ] && [ "${status}" != "accepted-residual" ] \
		&& [ -n "${UNREACHABLE_PATH_REASON["${CURRENT_FILE["${issue_id}"]}"]:-}" ] \
		&& [ -z "${CI_EXEMPT_BLOCK["${current_block:-_}"]:-}" ]; then
		status="unreachable"
		u_reason="${UNREACHABLE_PATH_REASON["${CURRENT_FILE["${issue_id}"]}"]}"
		if unreachable_lens_is_security "${CURRENT_LENS["${issue_id}"]}" "${CURRENT_FLOOR["${issue_id}"]:-}"; then
			u_security="true"
		fi
		u_followup="pending"
	fi
	if [ "${status}" != "accepted-residual" ] && [ "${status}" != "unreachable" ] && [ "${persist_count}" -ge "${REVIEW_LEDGER_PERSIST_LIMIT}" ]; then
		status="accepted-residual"
		accepted_residual_added=$((accepted_residual_added + 1))
	fi
	case "${status}" in
		unreachable)
			COUNT_TRANSITIONS[unreachable]=$((COUNT_TRANSITIONS[unreachable] + 1))
			;;
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
	FINAL_U_REASON["${issue_id}"]="${u_reason}"
	FINAL_U_SECURITY["${issue_id}"]="${u_security}"
	FINAL_U_FOLLOWUP["${issue_id}"]="${u_followup}"
	SEEN_FINAL["${issue_id}"]=1
	if [ "${status}" = "unreachable" ]; then
		unreachable_all_rows+="${issue_id}"$'\t'"${CURRENT_FILE["${issue_id}"]}"$'\t'"${CURRENT_LINES["${issue_id}"]:--}"$'\t'"${CURRENT_LENS["${issue_id}"]}"$'\t'"${u_security}"$'\t'"${u_reason:-unknown}"$'\t'"${u_followup}"$'\n'
		if [ "${u_followup}" = "pending" ]; then
			unreachable_new_rows+="${issue_id}"$'\t'"${CURRENT_FILE["${issue_id}"]}"$'\t'"${CURRENT_LINES["${issue_id}"]:--}"$'\t'"${CURRENT_LENS["${issue_id}"]}"$'\t'"${u_security}"$'\t'"${u_reason:-unknown}"$'\t'"${current_block:--}"$'\n'
		fi
	fi
	if [ "${status}" = "unreachable" ] && [ -n "${current_block}" ]; then
		if [ "${u_security}" = "true" ]; then
			# Security findings keep blocking (issue §1): the block stays
			# in the editor input, annotated, and the existing path runs.
			printf '%s\n' "${current_block}" >> "${keep_block_ids_file}"
			printf '%s\n' "${current_block}" >> "${hold_block_ids_file}"
		else
			printf '%s\t%s\t%s\n' "${current_block}" "${issue_id}" "${u_followup}" >> "${unreachable_block_ids_file}"
		fi
	elif [ "${status}" != "accepted-residual" ] && [ -n "${CURRENT_BLOCK_ID["${issue_id}"]:-}" ]; then
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
	if [ "${prior_status}" = "FIXED" ] || [ "${prior_status}" = "accepted-residual" ] || [ "${prior_status}" = "unreachable" ]; then
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
	FINAL_U_REASON["${issue_id}"]="${PRIOR_U_REASON["${issue_id}"]:-}"
	FINAL_U_SECURITY["${issue_id}"]="${PRIOR_U_SECURITY["${issue_id}"]:-false}"
	FINAL_U_FOLLOWUP["${issue_id}"]="${PRIOR_U_FOLLOWUP["${issue_id}"]:-pending}"
	SEEN_FINAL["${issue_id}"]=1
	if [ "${status}" = "unreachable" ]; then
		unreachable_all_rows+="${issue_id}"$'\t'"${PRIOR_FILE["${issue_id}"]}"$'\t'"${PRIOR_LINES["${issue_id}"]:--}"$'\t'"${PRIOR_LENS["${issue_id}"]}"$'\t'"${FINAL_U_SECURITY["${issue_id}"]}"$'\t'"${FINAL_U_REASON["${issue_id}"]:-unknown}"$'\t'"${FINAL_U_FOLLOWUP["${issue_id}"]}"$'\n'
		if [ "${FINAL_U_FOLLOWUP["${issue_id}"]}" = "pending" ]; then
			unreachable_new_rows+="${issue_id}"$'\t'"${PRIOR_FILE["${issue_id}"]}"$'\t'"${PRIOR_LINES["${issue_id}"]:--}"$'\t'"${PRIOR_LENS["${issue_id}"]}"$'\t'"${FINAL_U_SECURITY["${issue_id}"]}"$'\t'"${FINAL_U_REASON["${issue_id}"]:-unknown}"$'\t-\n'
		fi
	fi
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
	if [ "${REVIEW_UNREACHABLE_ACTIVE}" = "1" ] && [ -n "${header_unreachable_followup}" ]; then
		echo "UNREACHABLE_FOLLOWUP_ISSUE: ${header_unreachable_followup}"
	fi
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
		"${FINAL_U_REASON["${issue_id}"]:-}" \
		"${FINAL_U_SECURITY["${issue_id}"]:-}" \
		"${FINAL_U_FOLLOWUP["${issue_id}"]:-}"
	echo >> "${new_entries_file}"
done < <(printf '%s\n' "${!SEEN_FINAL[@]}" | LC_ALL=C sort)

mv "${new_entries_file}" "${REVIEW_LEDGER_PATH}"

if [ -s "${status_tmp_file}" ]; then
	LC_ALL=C sort -t $'\t' -k1,1 "${status_tmp_file}" > "${LEDGER_STATUS_FILE}"
else
	: > "${LEDGER_STATUS_FILE}"
fi

if [ "${REVIEW_UNREACHABLE_ACTIVE}" = "1" ]; then
	# Issue #10057: hand-off for review_apply_fixes.sh (follow-up issue) and
	# the editor-summary comment. The pre-rewrite copy keeps the reviewer
	# text of stubbed blocks for the follow-up issue body.
	if [ -n "${unreachable_new_rows}" ]; then
		cp "${REVIEW_ISSUES_FILE}" "${UNREACHABLE_PRE_ISSUES_FILE}"
	fi
	printf '%s' "${unreachable_new_rows}" | LC_ALL=C sort > "${UNREACHABLE_NEW_FILE}"
	printf '%s' "${unreachable_all_rows}" | LC_ALL=C sort > "${UNREACHABLE_ALL_FILE}"
	rewrite_review_issues_without_residuals "${REVIEW_ISSUES_FILE}" "${keep_block_ids_file}" "${residual_block_ids_file}" "${filtered_review_issues_file}" "${unreachable_block_ids_file}" "${hold_block_ids_file}"
else
	rewrite_review_issues_without_residuals "${REVIEW_ISSUES_FILE}" "${keep_block_ids_file}" "${residual_block_ids_file}" "${filtered_review_issues_file}"
fi
mv "${filtered_review_issues_file}" "${REVIEW_ISSUES_FILE}"

ledger_unreachable_transition=""
if [ "${REVIEW_UNREACHABLE_ACTIVE}" = "1" ]; then
	ledger_unreachable_transition=",unreachable:${COUNT_TRANSITIONS[unreachable]}"
fi
review_log "pr=${PR_NUMBER} iteration=${ITERATION} ledger_prior_entries=${prior_entries_count} transitions=NEW:${COUNT_TRANSITIONS[NEW]},PERSISTING:${COUNT_TRANSITIONS[PERSISTING]},FIXED:${COUNT_TRANSITIONS[FIXED]},RESURGENT:${COUNT_TRANSITIONS[RESURGENT]},accepted-residual:${COUNT_TRANSITIONS[accepted-residual]}${ledger_unreachable_transition} accepted_residual_added=${accepted_residual_added} ledger_reset=${ledger_reset} hash_collision=${auto_collision_count}"

exit 0
