#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

BUNDLE_FILE="${1:-reviewer_bundle.txt}"
OUT_FILE="${2:-floor_tags.txt}"
TOLERANCE_LINES=3

warn() {
	local event="$1"
	shift || true
	printf 'stage=floor_rules level=warning event=%s %s\n' "${event}" "$*" >&2 || true
}

log_stats() {
	printf 'stage=floor_rules anchors_scanned=%s multi_reviewer_hits=%s keyword_hits=%s high_confidence_hits=%s output_rows=%s\n' \
		"$1" "$2" "$3" "$4" "$5" >&2 || true
}

write_empty_output() {
	mkdir -p "$(dirname "${OUT_FILE}")"
	: > "${OUT_FILE}"
}

fail_open() {
	local reason="${1:-unhandled_error}"
	trap - ERR
	warn "fail_open" "reason=${reason} floor_rules_failed=1 bundle=${BUNDLE_FILE} output=${OUT_FILE}"
	mkdir -p "$(dirname "${OUT_FILE}")" >/dev/null 2>&1 || true
	: > "${OUT_FILE}" 2>/dev/null || true
	exit 0
}

write_builtin_keywords() {
	cat <<'__KEYWORDS__'
SECURITY|sql injection
SECURITY|command injection
SECURITY|xss
SECURITY|csrf
SECURITY|ssrf
SECURITY|path traversal
SECURITY|rce
SECURITY|remote code
SECURITY|deserializ
SECURITY|eval(
SECURITY|shell=true
SECURITY|unescaped
SECURITY|unsanitised
SECURITY|plaintext password
SECURITY|hardcoded secret
SECURITY|hardcoded key
SECURITY|hardcoded token
SECURITY|tls verify
SECURITY|ssl verify
SECURITY|cert validation
SECURITY|md5
SECURITY|sha1
SECURITY|static iv
SECURITY|static nonce
AUTH|auth bypass
AUTH|authz bypass
AUTH|permission check
AUTH|missing auth
AUTH|anonymous access
AUTH|idor
AUTH|privilege escalation
CONCURRENCY|race condition
CONCURRENCY|data race
CONCURRENCY|not thread-safe
CONCURRENCY|missing lock
CONCURRENCY|double-write
CONCURRENCY|lost update
CONCURRENCY|toctou
CONCURRENCY|check-then-act
RESOURCE|unbounded
RESOURCE|memory leak
RESOURCE|file descriptor leak
RESOURCE|fd leak
RESOURCE|connection leak
RESOURCE|infinite loop
RESOURCE|missing timeout
RESOURCE|no timeout
RESOURCE|unbounded retry
DATA_LOSS|data loss
DATA_LOSS|silent truncation
DATA_LOSS|silently drop
DATA_LOSS|swallows error
DATA_LOSS|swallowed exception
DATA_LOSS|catch and ignore
DATA_LOSS|catches all exceptions
MONGO|full collection scan
MONGO|missing index
MONGO|wrong index
MONGO|drop index
MONGO|drop and recreate
MONGO|ad-hoc createindex
MONGO|ad-hoc create_index
MONGO|e11000
MONGO|no idempotency key
MONGO|partial index
MONGO|collation mismatch
NAMING|renamed
NAMING|removed variable
NAMING|removed function
NAMING|removed field
NAMING|removed env
NAMING|breaking change
NAMING|api break
__KEYWORDS__
}

normalize_override_keywords() {
	local source_file="$1"
	local target_file="$2"
	awk '
		function trim(s) {
			sub(/^[[:space:]]+/, "", s)
			sub(/[[:space:]]+$/, "", s)
			return s
		}
		{
			line = $0
			sub(/\r$/, "", line)
			line = trim(line)
			if (line == "" || substr(line, 1, 1) == "#") {
				next
			}
			sep = index(line, "|")
			if (sep == 0) {
				sep = index(line, "\t")
			}
			if (sep == 0) {
				next
			}
			category = trim(substr(line, 1, sep - 1))
			keyword = trim(substr(line, sep + 1))
			if (category == "" || keyword == "") {
				next
			}
			print category "|" keyword
		}
	' "${source_file}" > "${target_file}"
}

tmp_dir=""
if ! tmp_dir="$(mktemp -d 2>/dev/null)"; then
	fail_open "tmp_dir_create_failed"
fi
cleanup() {
	rm -rf "${tmp_dir}"
}
trap cleanup EXIT
trap 'fail_open "unhandled_error"' ERR

if [ ! -s "${BUNDLE_FILE}" ]; then
	warn "bundle_missing_or_empty" "bundle=${BUNDLE_FILE}"
	write_empty_output
	log_stats 0 0 0 0 0
	exit 0
fi

keyword_catalog_file="${tmp_dir}/keywords.txt"
write_builtin_keywords > "${keyword_catalog_file}"

if [ -n "${REVIEW_FLOOR_KEYWORDS_FILE:-}" ]; then
	if [ -f "${REVIEW_FLOOR_KEYWORDS_FILE}" ] && [ -r "${REVIEW_FLOOR_KEYWORDS_FILE}" ]; then
		override_keywords_file="${tmp_dir}/keywords_override.txt"
		normalize_override_keywords "${REVIEW_FLOOR_KEYWORDS_FILE}" "${override_keywords_file}"
		if [ -s "${override_keywords_file}" ]; then
			keyword_catalog_file="${override_keywords_file}"
		else
			warn "keyword_file_invalid" "keyword_file_invalid=1 keyword_file=${REVIEW_FLOOR_KEYWORDS_FILE} fallback=builtin"
		fi
	else
		warn "keyword_file_missing" "keyword_file_missing=1 keyword_file=${REVIEW_FLOOR_KEYWORDS_FILE} fallback=builtin"
	fi
fi

raw_rows_file="${tmp_dir}/floor_rows.tsv"
stats_file="${tmp_dir}/stats.txt"

awk -v tolerance_lines="${TOLERANCE_LINES}" -v raw_out="${raw_rows_file}" -v stats_out="${stats_file}" -v repo_root="${PWD}" '
	function trim(s) {
		sub(/^[[:space:]]+/, "", s)
		sub(/[[:space:]]+$/, "", s)
		return s
	}
	function canonical_category(s, t) {
		t = toupper(trim(s))
		gsub(/[^A-Z0-9]+/, "_", t)
		gsub(/^_+/, "", t)
		gsub(/_+$/, "", t)
		if (t == "") {
			t = "UNCATEGORIZED"
		}
		return t
	}
	function reviewer_from_path(path, name) {
		name = trim(path)
		gsub(/^.*\//, "", name)
		sub(/\.[^.]*$/, "", name)
		if (name == "") {
			name = "unknown_reviewer"
		}
		return name
	}
	function normalize_file(path, p) {
		p = trim(path)
		gsub(/[`"]/, "", p)
		sub(/^[.][\/]/, "", p)
		gsub(/\\/, "/", p)
		if (substr(p, 1, 1) == "/" && repo_root != "") {
			prefix = repo_root "/"
			if (index(p, prefix) == 1) {
				p = substr(p, length(prefix) + 1)
			}
		}
		if (match(p, /:[0-9]+$/)) {
			p = substr(p, 1, RSTART - 1)
		}
		sub(/:[0-9]+[^0-9]*$/, "", p)
		sub(/[,:;]+$/, "", p)
		return trim(p)
	}
	function value_after_colon(line, idx, v) {
		idx = index(line, ":")
		if (idx == 0) {
			return ""
		}
		v = substr(line, idx + 1)
		return trim(v)
	}
	function is_field_label(line, lower) {
		lower = tolower(trim(line))
		return (lower ~ /^file[[:space:]]*:/ ||
			lower ~ /^line or code reference[[:space:]]*:/ ||
			lower ~ /^code[[:space:]]*:/ ||
			lower ~ /^problem[[:space:]]*:/ ||
			lower ~ /^why it fails at runtime[[:space:]]*:/ ||
			lower ~ /^issue_confidence[[:space:]]*:/)
	}
	function extract_line(ref, lower, token) {
		lower = tolower(trim(ref))
		if (lower == "") {
			return 0
		}
		if (match(lower, /line[[:space:]#:=-]*[0-9]+/)) {
			token = substr(lower, RSTART, RLENGTH)
			if (match(token, /[0-9]+/)) { token = substr(token, RSTART, RLENGTH) }
			if (token != "") {
				return token + 0
			}
		}
		if (match(lower, /(^|[^[:alnum:]_])l[0-9]+([^[:alnum:]_]|$)/)) {
			token = substr(lower, RSTART, RLENGTH)
			if (match(token, /[0-9]+/)) { token = substr(token, RSTART, RLENGTH) }
			if (token != "") {
				return token + 0
			}
		}
		if (match(lower, /:[[:space:]]*[0-9]+/)) {
			token = substr(lower, RSTART, RLENGTH)
			if (match(token, /[0-9]+/)) { token = substr(token, RSTART, RLENGTH) }
			if (token != "") {
				return token + 0
			}
		}
		if (match(lower, /^[[:space:]]*[0-9]+([[:space:]]*-[[:space:]]*[0-9]+)?[[:space:]]*$/)) {
			token = substr(lower, RSTART, RLENGTH)
			if (match(token, /[0-9]+/)) { token = substr(token, RSTART, RLENGTH) }
			if (token != "") {
				return token + 0
			}
		}
		return 0
	}
	function append_issue_text(line, clean) {
		clean = line
		if (is_field_label(clean) && index(clean, ":") > 0) {
			clean = substr(clean, index(clean, ":") + 1)
		}
		gsub(/\t/, " ", clean)
		sub(/\r$/, "", clean)
		clean = trim(clean)
		if (clean == "") {
			return
		}
		if (issue_text == "") {
			issue_text = clean
			return
		}
		if (length(issue_text) < 4000) {
			issue_text = issue_text " " clean
		}
	}
	function choose_excerpt(out) {
		out = trim(issue_excerpt)
		if (out == "") {
			out = trim(issue_text)
		}
		gsub(/\t/, " ", out)
		if (length(out) > 240) {
			out = substr(out, 1, 240)
		}
		return out
	}
	function reset_issue() {
		have_issue = 0
		pending_file = 0
		pending_line_ref = 0
		issue_file = ""
		issue_line_ref = ""
		issue_confidence = ""
		issue_excerpt = ""
		issue_text = ""
	}
	function emit_issue(file_norm, line_num) {
		if (!have_issue) {
			reset_issue()
			return
		}
		file_norm = normalize_file(issue_file)
		if (file_norm == "") {
			reset_issue()
			return
		}
		line_num = extract_line(issue_line_ref)
		if (line_num <= 0) {
			line_num = extract_line(issue_file)
		}
		if (line_num <= 0) {
			reset_issue()
			return
		}
		record_count++
		rec_file[record_count] = file_norm
		rec_line[record_count] = line_num
		rec_reviewer[record_count] = active_reviewer
		rec_text[record_count] = tolower(issue_text)
		rec_conf[record_count] = tolower(trim(issue_confidence))
		rec_excerpt[record_count] = choose_excerpt()
		reset_issue()
	}
	function is_high_conf(conf, lowered) {
		lowered = tolower(trim(conf))
		if (lowered == "") {
			return 0
		}
		if (lowered ~ /(^|[^0-9.])5([[:space:]]*\/[[:space:]]*5)?([^0-9.]|$)/) {
			return 1
		}
		if (lowered ~ /(^|[^[:alpha:]])max[[:space:]]*confidence([^[:alpha:]]|$)/) {
			return 1
		}
		if (lowered ~ /(^|[^[:alpha:]])highest[[:space:]]*confidence([^[:alpha:]]|$)/) {
			return 1
		}
		if (lowered ~ /(^|[^[:alpha:]])very[[:space:]]*high[[:space:]]*confidence([^[:alpha:]]|$)/) {
			return 1
		}
		if (lowered ~ /(^|[^[:alpha:]])certain([^[:alpha:]]|$)/) {
			return 1
		}
		return 0
	}
	function append_tag(csv, tag) {
		if (csv == "") {
			return tag
		}
		return csv "," tag
	}
	function keyword_matches(text, keyword, pattern) {
		if (keyword == "deserializ") {
			return (index(text, keyword) > 0)
		}
		if (keyword ~ /^[[:alnum:]_]+$/) {
			pattern = "(^|[^[:alnum:]_])" keyword "([^[:alnum:]_]|$)"
			return (text ~ pattern)
		}
		return (index(text, keyword) > 0)
	}
	BEGIN {
		active_reviewer = "unknown_reviewer"
		record_count = 0; tagged_record_count = 0; multi_reviewer_hit_count = 0; keyword_hit_count = 0; high_confidence_hit_count = 0;
		OFS = "\t"
	}
	NR == FNR {
		line = trim($0)
		if (line == "" || substr(line, 1, 1) == "#") {
			next
		}
		sep = index(line, "|")
		if (sep == 0) {
			next
		}
		cat = canonical_category(substr(line, 1, sep - 1))
		kw = tolower(trim(substr(line, sep + 1)))
		if (cat == "" || kw == "") {
			next
		}
		if (!(cat in category_seen)) {
			category_count++
			category_order[category_count] = cat
			category_seen[cat] = 1
		}
		pair_key = cat SUBSEP kw
		if (!(pair_key in keyword_seen)) {
			keyword_count++
			keyword_cat[keyword_count] = cat
			keyword_text[keyword_count] = kw
			keyword_seen[pair_key] = 1
		}
		next
	}
	{
		line_raw = $0
		sub(/\r$/, "", line_raw)
		line_trim = trim(line_raw)
		line_lower = tolower(line_trim)

		if (!in_content) {
			if (line_trim ~ /^FILE_PATH[[:space:]]*:/) {
				active_reviewer = reviewer_from_path(value_after_colon(line_trim))
				next
			}
			if (line_trim == "CONTENT_START") {
				in_content = 1
				reset_issue()
				next
			}
			next
		}

		if (line_trim == "CONTENT_END") {
			emit_issue()
			in_content = 0
			next
		}

		if (line_lower ~ /^file[[:space:]]*:/) {
			emit_issue()
			have_issue = 1
			pending_file = 0
			pending_line_ref = 0
			issue_file = value_after_colon(line_raw)
			if (issue_file == "") {
				pending_file = 1
			}
			append_issue_text(line_raw)
			next
		}

		if (!have_issue && line_trim == "") {
			next
		}

		if (!have_issue && (line_lower ~ /^line or code reference[[:space:]]*:/ || line_lower ~ /^code[[:space:]]*:/ || line_lower ~ /^problem[[:space:]]*:/ || line_lower ~ /^issue_confidence[[:space:]]*:/)) {
			have_issue = 1
		}

		if (pending_file && line_trim != "" && !is_field_label(line_trim)) {
			issue_file = line_trim
			pending_file = 0
			append_issue_text(line_raw)
			next
		}

		if (line_lower ~ /^line or code reference[[:space:]]*:/ || line_lower ~ /^code[[:space:]]*:/) {
			issue_line_ref = value_after_colon(line_raw)
			pending_line_ref = (issue_line_ref == "")
			append_issue_text(line_raw)
			next
		}

		if (pending_line_ref && line_trim != "" && !is_field_label(line_trim)) {
			issue_line_ref = line_trim
			pending_line_ref = 0
			append_issue_text(line_raw)
			next
		}

		if (line_lower ~ /^issue_confidence[[:space:]]*:/) {
			issue_confidence = value_after_colon(line_raw)
			append_issue_text(line_raw)
			next
		}

		if (line_lower ~ /^problem[[:space:]]*:/) {
			v = value_after_colon(line_raw)
			if (issue_excerpt == "" && v != "") {
				issue_excerpt = v
			}
			append_issue_text(line_raw)
			next
		}

		if (line_lower ~ /^why it fails at runtime[[:space:]]*:/) {
			v = value_after_colon(line_raw)
			if (issue_excerpt == "" && v != "") {
				issue_excerpt = v
			}
			append_issue_text(line_raw)
			next
		}

		if (issue_excerpt == "" && line_trim != "" && !is_field_label(line_trim)) {
			issue_excerpt = line_trim
		}
		append_issue_text(line_raw)
	}
	END {
		if (in_content) {
			emit_issue()
		}

		for (i = 1; i <= record_count; i++) {
			has_keyword = 0
			for (k = 1; k <= keyword_count; k++) {
				if (keyword_matches(rec_text[i], keyword_text[k])) {
					record_keyword[i SUBSEP keyword_cat[k]] = 1
					has_keyword = 1
				}
			}
			if (has_keyword) {
				keyword_hit_count++
			}
			if (is_high_conf(rec_conf[i])) {
				record_high_conf[i] = 1
				high_confidence_hit_count++
			}
		}

		for (i = 1; i <= record_count; i++) {
			for (j = i + 1; j <= record_count; j++) {
				if (rec_file[i] != rec_file[j]) {
					continue
				}
				if (rec_reviewer[i] == rec_reviewer[j]) {
					continue
				}
				diff = rec_line[i] - rec_line[j]
				if (diff < 0) {
					diff = -diff
				}
				if (diff <= tolerance_lines) {
					if (!record_multi_reviewer[i]) {
						record_multi_reviewer[i] = 1
						multi_reviewer_hit_count++
					}
					if (!record_multi_reviewer[j]) {
						record_multi_reviewer[j] = 1
						multi_reviewer_hit_count++
					}
				}
			}
		}

		for (i = 1; i <= record_count; i++) {
			tags = ""
			if (record_multi_reviewer[i]) {
				tags = append_tag(tags, "FLOOR_MULTI_REVIEWER")
			}
			for (c = 1; c <= category_count; c++) {
				cat = category_order[c]
				if (record_keyword[i SUBSEP cat]) {
					tags = append_tag(tags, "FLOOR_CRITICAL_KEYWORD:" cat)
				}
			}
			if (record_high_conf[i]) {
				tags = append_tag(tags, "FLOOR_HIGH_CONFIDENCE")
			}
			if (tags == "") {
				continue
			}
			tagged_record_count++
			print rec_file[i], rec_line[i], tags, rec_reviewer[i], rec_excerpt[i] >> raw_out
		}

		print "anchors_scanned=" (record_count + 0) > stats_out
		print "multi_reviewer_hits=" (multi_reviewer_hit_count + 0) >> stats_out
		print "keyword_hits=" (keyword_hit_count + 0) >> stats_out
		print "high_confidence_hits=" (high_confidence_hit_count + 0) >> stats_out
		print "tagged_records=" (tagged_record_count + 0) >> stats_out
	}
' "${keyword_catalog_file}" "${BUNDLE_FILE}"

mkdir -p "$(dirname "${OUT_FILE}")"
if [ -s "${raw_rows_file}" ]; then
	LC_ALL=C sort -t "$(printf "\t")" -s -k1,1 -k2,2n -k3,3 -k4,4 -k5,5 "${raw_rows_file}" \
		| awk -F '\t' 'BEGIN { OFS = "\t" } { print $1 ":" $2, $3, $4, $5 }' > "${OUT_FILE}"
else
	: > "${OUT_FILE}"
fi

anchors_scanned=0
multi_reviewer_hits=0
keyword_hits=0
high_confidence_hits=0
if [ -s "${stats_file}" ]; then
	while IFS='=' read -r key value; do
		case "${key}" in
			anchors_scanned)
				anchors_scanned="${value}"
				;;
			multi_reviewer_hits)
				multi_reviewer_hits="${value}"
				;;
			keyword_hits)
				keyword_hits="${value}"
				;;
			high_confidence_hits)
				high_confidence_hits="${value}"
				;;
		esac
	done < "${stats_file}"
fi

output_rows="$(wc -l < "${OUT_FILE}" | tr -d '[:space:]')"
log_stats "${anchors_scanned}" "${multi_reviewer_hits}" "${keyword_hits}" "${high_confidence_hits}" "${output_rows}"
