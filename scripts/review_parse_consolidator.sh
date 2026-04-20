#!/usr/bin/env bash
set -euo pipefail

review_log()
{
	printf 'stage=parser %s\n' "$*" >&2
}

trim()
{
	local value="$1"
	value="${value#"${value%%[![:space:]]*}"}"
	value="${value%"${value##*[![:space:]]}"}"
	printf '%s' "${value}"
}

emit_indented()
{
	local text="$1"
	local line
	if [ -z "${text}" ]; then
		printf '  \n'
		return
	fi
	while IFS= read -r line || [ -n "${line}" ]; do
		printf '  %s\n' "${line}"
	done <<< "${text}"
}

write_stats()
{
	cat > "${PARSER_STATS_FILE}" <<STATS
parsed_blocks=${parsed_blocks}
passthrough_blocks=${passthrough_blocks}
line_unverified=${line_unverified}
evidence_unverified=${evidence_unverified}
dropped_invalid_file=${dropped_invalid_file}
dropped_unknown_reason=${dropped_unknown_reason}
parse_failed=${parse_failed}
parse_error=${parse_error}
anchors_total=${anchors_total}
anchors_covered=${anchors_covered}
STATS
}

finish_parse_failure()
{
	local reason="$1"
	local rc="${2:-1}"
	parse_failed=1
	parse_error="${reason}"
	: > "${REVIEW_ISSUES_FILE}"
	write_stats
	review_log "parse_failed=1 reason=${reason} failopen=${REVIEW_PARSER_FAILOPEN}"
	if [ "${REVIEW_PARSER_FAILOPEN}" = "0" ]; then
		exit "${rc}"
	fi
	exit 0
}

validate_file_path()
{
	local file_path="$1"
	if [ -z "${file_path}" ]; then
		return 1
	fi
	if [[ "${file_path}" == /* ]]; then
		return 1
	fi
	if [[ "${file_path}" == *".."* ]]; then
		return 1
	fi
	if [[ "${file_path}" =~ [[:cntrl:]] ]]; then
		return 1
	fi
	local object_type
	object_type="$(git cat-file -t "HEAD:${file_path}" 2>/dev/null || true)"
	if [ "${object_type}" != "blob" ]; then
		return 1
	fi
	return 0
}

declare -A FILE_LINE_COUNT_CACHE=()
get_file_line_count()
{
	local file_path="$1"
	if [ -n "${FILE_LINE_COUNT_CACHE[${file_path}]+x}" ]; then
		printf '%s' "${FILE_LINE_COUNT_CACHE[${file_path}]}"
		return
	fi
	local count
	count="$(git show "HEAD:${file_path}" | awk 'END { print NR }' | tr -d '[:space:]' || true)"
	if [[ ! "${count}" =~ ^[0-9]+$ ]]; then
		count=0
	fi
	FILE_LINE_COUNT_CACHE["${file_path}"]="${count}"
	printf '%s' "${count}"
}

PARSED_LINE_START=0
PARSED_LINE_END=0
parse_line_range()
{
	local line_spec="$1"
	if [[ ! "${line_spec}" =~ ^([0-9]+)(-([0-9]+))?$ ]]; then
		return 1
	fi
	PARSED_LINE_START="${BASH_REMATCH[1]}"
	if [ -n "${BASH_REMATCH[3]:-}" ]; then
		PARSED_LINE_END="${BASH_REMATCH[3]}"
	else
		PARSED_LINE_END="${BASH_REMATCH[1]}"
	fi
	if [ "${PARSED_LINE_START}" -lt 1 ] || [ "${PARSED_LINE_END}" -lt "${PARSED_LINE_START}" ]; then
		return 1
	fi
	return 0
}

verify_line_range_at_head()
{
	local file_path="$1"
	local line_spec="$2"
	if ! parse_line_range "${line_spec}"; then
		return 1
	fi
	local line_count
	line_count="$(get_file_line_count "${file_path}")"
	if [ "${line_count}" -lt "${PARSED_LINE_END}" ]; then
		return 1
	fi
	return 0
}

declare -A KNOWN_REVIEWERS=()
load_known_reviewers()
{
	while IFS= read -r line || [ -n "${line}" ]; do
		if [[ "${line}" =~ ^FILE_PATH:[[:space:]]*(.+)$ ]]; then
			local path_value base_name stripped_name
			path_value="$(trim "${BASH_REMATCH[1]}")"
			base_name="$(basename "${path_value}")"
			base_name="${base_name%.txt}"
			if [ -n "${base_name}" ]; then
				KNOWN_REVIEWERS["${base_name}"]=1
			fi
			stripped_name="${base_name#review_}"
			if [ -n "${stripped_name}" ]; then
				KNOWN_REVIEWERS["${stripped_name}"]=1
			fi
		fi
		if [[ "${line}" =~ (reviewer_[A-Za-z0-9_-]+) ]]; then
			KNOWN_REVIEWERS["${BASH_REMATCH[1]}"]=1
		fi
	done < "${REVIEWER_BUNDLE_FILE}"
}

SANITIZED_FLAGGED_BY=""
FLAGGED_BY_UNVERIFIED=0
sanitize_flagged_by()
{
	local raw_value="$1"
	local had_any=0
	local -a kept=()
	local token trimmed_token
	IFS=',' read -r -a tokens <<< "${raw_value}"
	for token in "${tokens[@]}"; do
		trimmed_token="$(trim "${token}")"
		if [ -z "${trimmed_token}" ]; then
			continue
		fi
		had_any=1
		if [ -n "${KNOWN_REVIEWERS[${trimmed_token}]+x}" ]; then
			kept+=("${trimmed_token}")
		fi
	done
	if [ "${#kept[@]}" -gt 0 ]; then
		SANITIZED_FLAGGED_BY="$(printf '%s, ' "${kept[@]}")"
		SANITIZED_FLAGGED_BY="${SANITIZED_FLAGGED_BY%, }"
	elif [ "${had_any}" -eq 1 ]; then
		SANITIZED_FLAGGED_BY="unknown"
	else
		SANITIZED_FLAGGED_BY=""
	fi
	FLAGGED_BY_UNVERIFIED=0
	if [ "${had_any}" -eq 1 ] && [ "${#kept[@]}" -eq 0 ]; then
		FLAGGED_BY_UNVERIFIED=1
	fi
}

RUNTIME_DIR="${RUNTIME_DIR:?RUNTIME_DIR is required}"
REVIEW_PARSER_FAILOPEN="${REVIEW_PARSER_FAILOPEN:-1}"
CONSOLIDATOR_RAW_FILE="${CONSOLIDATOR_RAW_FILE:-${RUNTIME_DIR}/consolidator_raw.txt}"
REVIEWER_BUNDLE_FILE="${REVIEWER_BUNDLE_FILE:-${RUNTIME_DIR}/reviewer_bundle.txt}"
REVIEW_ISSUES_FILE="${REVIEW_ISSUES_FILE:-${RUNTIME_DIR}/review_issues.txt}"
PARSER_STATS_FILE="${PARSER_STATS_FILE:-${RUNTIME_DIR}/parser_stats.txt}"

parsed_blocks=0
passthrough_blocks=0
line_unverified=0
evidence_unverified=0
dropped_invalid_file=0
dropped_unknown_reason=0
unmatched_markers=0
parse_failed=0
parse_error=""
anchors_total=0
anchors_covered=0
on_parser_error()
{
	local rc="$?"
	local line_no="${BASH_LINENO[0]:-unknown}"
	trap - ERR
	set +e
	finish_parse_failure "internal_error_line_${line_no}" "${rc}"
}

trap 'on_parser_error' ERR

if [ ! -f "${REVIEWER_BUNDLE_FILE}" ]; then
	finish_parse_failure "missing_reviewer_bundle" 2
fi
if [ ! -f "${CONSOLIDATOR_RAW_FILE}" ]; then
	finish_parse_failure "missing_consolidator_raw" 2
fi

load_known_reviewers

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT
parsed_blocks_file="${tmp_dir}/parsed_blocks.txt"
covered_ranges_file="${tmp_dir}/covered_ranges.tsv"
anchors_file="${tmp_dir}/anchors.tsv"
covered_anchors_file="${tmp_dir}/covered_anchors.tsv"
uncovered_anchors_file="${tmp_dir}/uncovered_anchors.tsv"
passthrough_file="${tmp_dir}/passthrough_blocks.txt"

: > "${parsed_blocks_file}"
: > "${covered_ranges_file}"
: > "${covered_anchors_file}"
: > "${uncovered_anchors_file}"
: > "${passthrough_file}"

gawk '
function emit_anchor(file, line, excerpt, key) {
	key = file ":" line
	if (seen[key]) {
		return
	}
	seen[key] = 1
	print file "\t" line "\t" excerpt
}
{
	line = $0
	if (match(line, /^[[:space:]]*([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*):([0-9]+)/, hit)) {
		emit_anchor(hit[1], hit[3], line)
	}
	if (match(line, /^[[:space:]]*[Ff][Ii][Ll][Ee]:[[:space:]]*([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*)[[:space:]]*$/, f)) {
		pending_file = f[1]
		next
	}
	if (pending_file != "" && match(line, /^[[:space:]]*[Ll][Ii][Nn][Ee]:[[:space:]]*([0-9]+)/, l)) {
		emit_anchor(pending_file, l[1], line)
		pending_file = ""
		next
	}
	if (pending_file != "" && line ~ /^[[:space:]]*$/) {
		pending_file = ""
	}
}
' "${REVIEWER_BUNDLE_FILE}" | LC_ALL=C sort -t $'\t' -k1,1 -k2,2n > "${anchors_file}"
anchors_total="$(wc -l < "${anchors_file}" | tr -d '[:space:]')"

block_open=0
block_id=""
current_field=""
marker_open_count=0

block_file=""
block_lines=""
block_lens=""
block_severity=""
block_flagged_by=""
block_classification=""
block_merged_from=""
block_evidence=""
block_current_code=""
block_suggested_approach=""
block_notes=""
block_unknown=""

append_multiline_field()
{
	local field_name="$1"
	local next_line="$2"
	local current_value
	current_value="${!field_name}"
	if [ -n "${current_value}" ]; then
		printf -v "${field_name}" '%s\n%s' "${current_value}" "${next_line}"
	else
		printf -v "${field_name}" '%s' "${next_line}"
	fi
}

reset_block()
{
	block_file=""
	block_lines=""
	block_lens=""
	block_severity=""
	block_flagged_by=""
	block_classification=""
	block_merged_from=""
	block_evidence=""
	block_current_code=""
	block_suggested_approach=""
	block_notes=""
	block_unknown=""
	current_field=""
}

process_block()
{
	local issue_id="$1"
	local file_path line_spec lens severity flagged_by classification merged_from notes
	local unknown evidence code_block suggested
	local -a parser_tags=()

	file_path="$(trim "${block_file}")"
	line_spec="$(trim "${block_lines}")"
	lens="$(trim "${block_lens}")"
	severity="$(trim "${block_severity}")"
	flagged_by="$(trim "${block_flagged_by}")"
	classification="$(trim "${block_classification}")"
	merged_from="$(trim "${block_merged_from}")"
	notes="$(trim "${block_notes}")"
	unknown="${block_unknown}"
	evidence="${block_evidence}"
	code_block="${block_current_code}"
	suggested="${block_suggested_approach}"

	if ! validate_file_path "${file_path}"; then
		local safe_file_path="${file_path//[^A-Za-z0-9_./-]/_}"
		dropped_invalid_file=$((dropped_invalid_file + 1))
		review_log "dropped_invalid_file path=${safe_file_path:0:160} reason=invalid_file_path"
		return
	fi

	sanitize_flagged_by "${flagged_by}"
	flagged_by="${SANITIZED_FLAGGED_BY}"
	if [ "${FLAGGED_BY_UNVERIFIED}" -eq 1 ]; then
		parser_tags+=("EVIDENCE_UNVERIFIED")
		evidence_unverified=$((evidence_unverified + 1))
	fi

	local line_is_verified=1
	if ! verify_line_range_at_head "${file_path}" "${line_spec}"; then
		line_is_verified=0
		parser_tags+=("LINE_UNVERIFIED")
		line_unverified=$((line_unverified + 1))
	else
		printf '%s\t%s\t%s\n' "${file_path}" "${PARSED_LINE_START}" "${PARSED_LINE_END}" >> "${covered_ranges_file}"
	fi

	if [ -z "${lens}" ]; then
		lens="UNKNOWN_LENS"
	fi
	if [ -z "${severity}" ]; then
		severity="low"
	fi
	if [ -z "${classification}" ]; then
		classification="unclassified"
	fi
	if [ "${classification}" = "non-actionable" ] && [ -z "${notes}" ]; then
		classification="unclassified"
	fi

	parsed_blocks=$((parsed_blocks + 1))

	{
		echo "=== ISSUE ${issue_id} ==="
		echo "FILE: ${file_path}"
		echo "LINES: ${line_spec}"
		echo "LENS: ${lens}"
		echo "SEVERITY: ${severity}"
		echo "FLAGGED_BY: ${flagged_by}"
		echo "CLASSIFICATION: ${classification}"
		if [ -n "${merged_from}" ]; then
			echo "MERGED_FROM: ${merged_from}"
		fi
		echo "EVIDENCE:"
		emit_indented "${evidence}"
		echo "CURRENT_CODE:"
		emit_indented "${code_block}"
		echo "SUGGESTED_APPROACH:"
		emit_indented "${suggested}"
		if [ -n "${notes}" ]; then
			echo "NOTES:"
			emit_indented "${notes}"
		fi
		if [ "${#parser_tags[@]}" -gt 0 ]; then
			echo "PARSER_TAGS: $(printf '%s,' "${parser_tags[@]}" | sed 's/,$//')"
		fi
		if [ -n "${unknown}" ]; then
			echo "UNRECOGNISED:"
			emit_indented "${unknown}"
		fi
		echo "=== END ISSUE ${issue_id} ==="
		echo
	} >> "${parsed_blocks_file}"

	if [ "${line_is_verified}" -eq 0 ]; then
		review_log "issue=${issue_id} file=${file_path} line_unverified=1"
	fi
}

while IFS= read -r line || [ -n "${line}" ]; do
	if [[ "${line}" =~ ^===[[:space:]]ISSUE[[:space:]]([0-9]+)[[:space:]]===$ ]]; then
		if [ "${block_open}" -eq 1 ]; then
			dropped_unknown_reason=$((dropped_unknown_reason + 1))
			unmatched_markers=$((unmatched_markers + 1))
		fi
		reset_block
		block_open=1
		block_id="${BASH_REMATCH[1]}"
		marker_open_count=$((marker_open_count + 1))
		continue
	fi

	if [[ "${line}" =~ ^===[[:space:]]END[[:space:]]ISSUE[[:space:]]([0-9]+)[[:space:]]===$ ]]; then
		if [ "${block_open}" -eq 1 ] && [ "${block_id}" = "${BASH_REMATCH[1]}" ]; then
			process_block "${block_id}"
			block_open=0
			reset_block
		else
			dropped_unknown_reason=$((dropped_unknown_reason + 1))
			unmatched_markers=$((unmatched_markers + 1))
		fi
		continue
	fi

	if [ "${block_open}" -ne 1 ]; then
		continue
	fi

	if [[ "${line}" =~ ^(FILE|LINES|LENS|SEVERITY|FLAGGED_BY|CLASSIFICATION|MERGED_FROM|EVIDENCE|CURRENT_CODE|SUGGESTED_APPROACH|NOTES):[[:space:]]*(.*)$ ]]; then
		header="${BASH_REMATCH[1]}"
		value="${BASH_REMATCH[2]}"
		case "${header}" in
			FILE)
				block_file="${value}"
				current_field=""
				;;
			LINES)
				block_lines="${value}"
				current_field=""
				;;
			LENS)
				block_lens="${value}"
				current_field=""
				;;
			SEVERITY)
				block_severity="${value}"
				current_field=""
				;;
			FLAGGED_BY)
				block_flagged_by="${value}"
				current_field=""
				;;
			CLASSIFICATION)
				block_classification="${value}"
				current_field=""
				;;
			MERGED_FROM)
				block_merged_from="${value}"
				current_field=""
				;;
			EVIDENCE)
				block_evidence="${value}"
				current_field="EVIDENCE"
				;;
			CURRENT_CODE)
				block_current_code="${value}"
				current_field="CURRENT_CODE"
				;;
			SUGGESTED_APPROACH)
				block_suggested_approach="${value}"
				current_field="SUGGESTED_APPROACH"
				;;
			NOTES)
				block_notes="${value}"
				current_field="NOTES"
				;;
			*)
				append_multiline_field block_unknown "${line}"
				current_field=""
				;;
		esac
		continue
	fi

	case "${current_field}" in
		EVIDENCE)
			append_multiline_field block_evidence "${line}"
			;;
		CURRENT_CODE)
			append_multiline_field block_current_code "${line}"
			;;
		SUGGESTED_APPROACH)
			append_multiline_field block_suggested_approach "${line}"
			;;
		NOTES)
			append_multiline_field block_notes "${line}"
			;;
		*)
			append_multiline_field block_unknown "${line}"
			;;
	esac

done < "${CONSOLIDATOR_RAW_FILE}"

if [ "${block_open}" -eq 1 ]; then
	dropped_unknown_reason=$((dropped_unknown_reason + 1))
	unmatched_markers=$((unmatched_markers + 1))
fi

if [ "${marker_open_count}" -eq 0 ]; then
	parse_failed=1
	parse_error="no_issue_markers"
	review_log "event=no_issue_markers failopen=${REVIEW_PARSER_FAILOPEN}"
	if [ "${REVIEW_PARSER_FAILOPEN}" = "0" ]; then
		: > "${REVIEW_ISSUES_FILE}"
		write_stats
		exit 3
	fi
fi

if [ -s "${covered_ranges_file}" ] && [ -s "${anchors_file}" ]; then
	awk -F'\t' -v covered_out="${covered_anchors_file}" -v uncovered_out="${uncovered_anchors_file}" '
	NR == FNR {
		count[$1]++
		start[$1, count[$1]] = $2 + 0
		end[$1, count[$1]] = $3 + 0
		next
	}
	{
		file = $1
		line = $2 + 0
		is_covered = 0
		for (idx = 1; idx <= count[file]; idx++) {
			if (line >= start[file, idx] && line <= end[file, idx]) {
				is_covered = 1
				break
			}
		}
		if (is_covered) {
			print $0 >> covered_out
		} else {
			print $0 >> uncovered_out
		}
	}
	' "${covered_ranges_file}" "${anchors_file}"
elif [ -s "${anchors_file}" ]; then
	cp "${anchors_file}" "${uncovered_anchors_file}"
fi

if [ -s "${covered_anchors_file}" ]; then
	anchors_covered="$(wc -l < "${covered_anchors_file}" | tr -d '[:space:]')"
else
	anchors_covered=0
fi

if [ -s "${uncovered_anchors_file}" ]; then
	pass_id=0
	while IFS=$'\t' read -r anchor_file anchor_line anchor_excerpt || [ -n "${anchor_file}${anchor_line}${anchor_excerpt}" ]; do
		pass_id=$((pass_id + 1))
		pass_code="$(printf '%03d' "${pass_id}")"
		{
			echo "=== ISSUE PASSTHROUGH ${pass_code} ==="
			echo "FILE: ${anchor_file}"
			echo "LINES: ${anchor_line}"
			echo "CLASSIFICATION: unclassified"
			echo "EVIDENCE:"
			emit_indented "reviewer_bundle> ${anchor_excerpt}"
			echo "NOTES:"
			emit_indented "Anchor was not covered by consolidator output; passthrough emitted by parser."
			echo "=== END ISSUE PASSTHROUGH ${pass_code} ==="
			echo
		} >> "${passthrough_file}"
	done < "${uncovered_anchors_file}"
	passthrough_blocks="${pass_id}"
fi

cat "${parsed_blocks_file}" "${passthrough_file}" > "${REVIEW_ISSUES_FILE}"
write_stats

coverage_ratio="0"
if [ "${anchors_total}" -gt 0 ]; then
	coverage_ratio="$(awk -v covered="${anchors_covered}" -v total="${anchors_total}" 'BEGIN { printf "%.3f", covered / total }')"
fi
review_log "parsed_blocks=${parsed_blocks} passthrough_blocks=${passthrough_blocks} anchors_total=${anchors_total} anchors_covered=${anchors_covered} coverage_ratio=${coverage_ratio} line_unverified=${line_unverified} evidence_unverified=${evidence_unverified} dropped_invalid_file=${dropped_invalid_file} dropped_unknown_reason=${dropped_unknown_reason} unmatched_markers=${unmatched_markers} parse_failed=${parse_failed}"

rm -rf "${tmp_dir}"
exit 0
