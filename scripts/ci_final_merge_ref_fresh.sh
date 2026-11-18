#!/usr/bin/env bash
# Final-CI merge-ref freshness check (final-CI self-heal, 2026-11-17).
#
# A final CI run (ci.yml `workflow_dispatch`, cut-ci-runner-time Phase 4)
# checks out `refs/pull/<n>/merge` in every job. GitHub refreshes that ref
# lazily after the base moves, so a job can test the head merged into an
# older base tip: PR #9602's run on 2026-11-17 07:21 UTC tested a merge with
# base commit df30f80a although the base had been at its fix 8a1664a5 (push
# CI green) since 05:37, and failed on tests the base had already fixed. The
# shallow-checkout steps only checked the merge's second parent (the PR head).
#
# Runs as the step right after each final-CI checkout. It compares the
# checked-out merge commit's first parent with the base branch's live tip
# (`git ls-remote`, no API call):
#   - equal: logs `outcome=fresh` and exits 0;
#   - different: up to CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS times
#     (default 6, 1-20), CI_FINAL_MERGE_REF_REFRESH_SECONDS apart (default 10,
#     1-60), reads `pulls/<n>` (which makes GitHub recompute the merge ref),
#     re-fetches the merge ref and re-reads the tip. When the new merge
#     commit still merges the dispatched head (GITHUB_SHA) and its first
#     parent is a base tip seen during this step, the job checks it out
#     (`outcome=refreshed`) and continues on the current combination;
#   - still stale after the attempts, or GitHub reports the PR as not
#     mergeable (a conflict, so no merge ref can be computed): `::error::` and
#     exit 1 (`outcome=stale` / `outcome=conflict`). The red `lint` sends the
#     PR back through `final-ci-failed`, and the review sweep's base sync
#     (scripts/review_final_ci_sweep.sh) updates a head that is behind.
# Sync-only acceptance (#9688, plan Phase 10 / R7 / D10): final-CI jobs can
# wait about an hour for a runner, and an integration branch that syncs `main`
# in the meantime leaves the merge ref one sync behind (PR #9602 on
# 2026-11-17/18: 24, then 36 jobs ended `outcome=stale`). When the refresh
# attempts have run out (after at least one re-fetch worked) and
# CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED is on (default true;
# true/1/yes/on), ci_final_merge_ref_sync_only accepts the checked-out merge
# only when all of these hold, and fails closed (`outcome=stale`,
# `detail=<token>`) on anything else, including an unreadable response:
#   - the base is not the default branch (`foreign_commit`, no extra call);
#   - `compare/<tested_base>...<tip>` is `ahead` (`not_ahead`), lists every
#     commit (at most 250) and fewer than 300 files (`truncated`);
#   - every commit in that range is a default-branch commit (in
#     `compare/<tested_base>...<default branch>`), a two-parent merge titled
#     exactly `chore: sync <default branch> into <base>` whose second parent
#     is such a commit, or a commit whose subject starts with
#     `[ai-merge-resolve]` (`foreign_commit`);
#   - no file changed in the range (name or previous name) is in the PR's file
#     list (`file_overlap`); a PR with more than 3000 files, or a listing that
#     does not match `changed_files`, is `truncated`;
#   - the PR head is still GITHUB_SHA after the file listing (`api_error`),
#     and the tip is the one the last lookup returned (`api_error` when that
#     lookup failed; no earlier tip is used);
#   - after all of those reads, the base tip read again with `git ls-remote`
#     (no API call) is still that tip: a base commit that landed during the
#     reads is outside the checked range, so a moved or unreadable tip is
#     `api_error` (fail-closed).
# On accept the job keeps the checked-out merge, logs `outcome=stale_accepted
# reason=sync_only_no_overlap ... commits=<k> files=<m>`, adds one line to
# $GITHUB_STEP_SUMMARY and exits 0. Switch off: today's stale exit, unchanged.
# Fail-open: not a final-CI run, the switch off, an unreadable base branch,
# tip or commit before the refresh (the sync-only check's own tip re-read
# above fails closed instead), a merge ref that could not be re-fetched at all (e.g. a
# private repository and a checkout without persisted credentials), or a head
# that moved (the run is superseded anyway) logs and exits 0, so the job tests
# what it checked out, as before this check.
#
# Usage: ci_final_merge_ref_fresh.sh [--depth <n>]   (fetch depth of the
# job's checkout, default 1, 1-50; the re-fetch keeps the same depth)
# Env: FINAL_CI_PR_NUMBER (empty = not a final-CI run, no-op), GITHUB_SHA,
# GITHUB_REPOSITORY, GH_TOKEN (optional: without it the PR is read from the
# public REST API unauthenticated; CI_FINAL_MERGE_REF_API_URL overrides the
# API base for tests), CI_FINAL_MERGE_REF_FRESH_ENABLED (default true;
# true/1/yes/on), the two refresh knobs above,
# CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED (default true; read from the
# step env only, ci.yml passes no repository variable for it).
# API budget (§15): one `GET pulls/<n>` for the base branch name, the default
# branch and `changed_files` (the job's shallow-checkout step reads the same
# PR later for its own exports; the check must run before that step, so it
# cannot reuse that read), plus one `GET pulls/<n>` per refresh attempt, only
# when the merge ref is stale. The sync-only check adds nothing on the fresh,
# refreshed, conflict, skipped and switch-off paths; on the final stale path it
# reads 4 endpoints, in this order and stopping at the first failed
# condition: `GET compare/<tested_base>...<tip>`, `GET
# compare/<tested_base>...<default branch>`, `GET pulls/<n>/files` at 100 per
# page (one GET per 100 PR files, at most 30 for 3000), then one `GET
# pulls/<n>` to confirm the head is still GITHUB_SHA. That is 2 compare reads
# plus 1 per 100 PR files plus 1: 4 for a PR of up to 100 files, at most 33.
# Log: `CI_FINAL_MERGE_REF pr=<n> head=<sha> outcome=fresh|refreshed|stale|stale_accepted|conflict|skipped reason=<token> [base=<ref>] [tested_base=<sha>] [tip=<sha>] [commits=<k>] [files=<m>] [detail=not_ahead|truncated|foreign_commit|file_overlap|api_error]`.

set -uo pipefail

ci_final_merge_ref_log()
{
	echo "CI_FINAL_MERGE_REF pr=${FINAL_CI_PR_NUMBER:-?} head=${GITHUB_SHA:-?} outcome=$1 reason=$2${3:+ $3}"
}

# `ci_final_merge_ref_parents <rev>` prints "<first> <second>" from the raw
# commit object: in a depth-1 clone `git rev-parse <rev>^1` cannot see a
# parent that was not fetched, but the commit object still names it.
ci_final_merge_ref_parents()
{
	git cat-file -p "$1" 2>/dev/null | awk '/^$/ { exit } /^parent / { p = p (p == "" ? "" : " ") $2 } END { print p }'
}

# `ci_final_merge_ref_pull <jq filter>`: one `GET pulls/<n>` through `gh api`.
# A job that gets no token (tests-sandbox-python passes none, see
# tests/test_sandbox_python_coverage.py) reads the public REST endpoint
# without credentials instead. Prints nothing and returns 1 on failure.
ci_final_merge_ref_pull()
{
	local filter="$1" pull_json=""
	if [ -n "${GH_TOKEN:-}" ] && gh api "repos/${GITHUB_REPOSITORY}/pulls/${FINAL_CI_PR_NUMBER}" --jq "${filter}" 2>/dev/null; then
		return 0
	fi
	pull_json="$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
		"${CI_FINAL_MERGE_REF_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY}/pulls/${FINAL_CI_PR_NUMBER}" 2>/dev/null)" || return 1
	printf '%s' "${pull_json}" | jq -r "${filter}" 2>/dev/null
}

# `ci_final_merge_ref_api_get <path under repos/<o>/<r>/>`: one read-only GET
# through the same auth path as ci_final_merge_ref_pull (gh with GH_TOKEN,
# else, or when gh fails, the public REST endpoint without credentials).
# Prints the raw JSON; returns 1 when the call fails or the body is not JSON.
ci_final_merge_ref_api_get()
{
	local api_path="$1" api_body=""
	if [ -n "${GH_TOKEN:-}" ]; then
		api_body="$(gh api "repos/${GITHUB_REPOSITORY}/${api_path}" 2>/dev/null)" || api_body=""
	fi
	if [ -z "${api_body}" ]; then
		api_body="$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
			"${CI_FINAL_MERGE_REF_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY}/${api_path}" 2>/dev/null)" || return 1
	fi
	printf '%s' "${api_body}" | jq -e . >/dev/null 2>&1 || return 1
	printf '%s\n' "${api_body}"
}

# `ci_final_merge_ref_sync_only <base_ref> <tip> <default_branch> <changed_files>`
# runs in its own subshell (call it through command substitution). It prints
# `ok <tested_base> <commits> <files>` and returns 0 when only integration
# syncs and resolver commits, none of which touch a PR file, separate the
# checked-out merge's base from <tip>. Otherwise it prints one detail token
# (not_ahead|truncated|foreign_commit|file_overlap|api_error) and returns 1.
# Response bodies are untrusted: jq parses them and compares their values as
# data; nothing from them reaches a shell command or a URL.
ci_final_merge_ref_sync_only()
(
	sync_base_ref="$1"
	sync_tip="$2"
	sync_default_branch="$3"
	sync_pr_changed_files="$4"
	sync_reject()
	{
		echo "$1"
		exit 1
	}
	if ! [[ "${sync_default_branch}" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "${sync_default_branch}" == *..* ]] || [[ "${sync_default_branch}" == -* ]]; then
		sync_reject api_error
	fi
	[[ "${sync_pr_changed_files}" =~ ^[0-9]{1,9}$ ]] || sync_reject api_error
	sync_pr_changed_files=$((10#${sync_pr_changed_files}))
	[[ "${sync_tip}" =~ ^[0-9a-f]{40}$ ]] || sync_reject api_error
	# The merge actually checked out: the refresh loop's `first` may hold the
	# last re-fetched ref's parent instead.
	read -r sync_tested_base sync_tested_head <<< "$(ci_final_merge_ref_parents HEAD)"
	if ! [[ "${sync_tested_base:-}" =~ ^[0-9a-f]{40}$ ]] || [ "${sync_tested_head:-}" != "${GITHUB_SHA}" ]; then
		sync_reject api_error
	fi
	# A PR into the default branch is not separated from its tip by
	# integration syncs; every such stale ref still fails as before.
	[ "${sync_base_ref}" != "${sync_default_branch}" ] || sync_reject foreign_commit
	sync_tmp="$(mktemp -d "${RUNNER_TEMP:-/tmp}/ci-final-merge-ref.XXXXXX" 2>/dev/null)" || sync_reject api_error
	trap 'rm -rf "${sync_tmp}"' EXIT

	# GET 1: the range between the tested base and the tip.
	ci_final_merge_ref_api_get "compare/${sync_tested_base}...${sync_tip}" > "${sync_tmp}/range.json" || sync_reject api_error
	sync_verdict="$(jq -r '
		if type != "object" or (.status | type) != "string" then "api_error"
		elif .status != "ahead" then "not_ahead"
		elif (.total_commits | type) != "number" or (.commits | type) != "array" or (.files | type) != "array" then "api_error"
		elif .total_commits > 250 or (.commits | length) != .total_commits or (.files | length) >= 300 then "truncated"
		else "ok" end' "${sync_tmp}/range.json" 2>/dev/null)" || sync_reject api_error
	[ "${sync_verdict}" = "ok" ] || sync_reject "${sync_verdict:-api_error}"

	# GET 2: the default-branch commits the tested base does not have.
	ci_final_merge_ref_api_get "compare/${sync_tested_base}...${sync_default_branch}" > "${sync_tmp}/default.json" || sync_reject api_error
	sync_verdict="$(jq -r '
		if type != "object" or (.total_commits | type) != "number" or (.commits | type) != "array" then "api_error"
		elif .total_commits > 250 or (.commits | length) < .total_commits then "truncated"
		else "ok" end' "${sync_tmp}/default.json" 2>/dev/null)" || sync_reject api_error
	[ "${sync_verdict}" = "ok" ] || sync_reject "${sync_verdict:-api_error}"

	# One pass over the range: count the commits that are neither a
	# default-branch commit, a sync merge of one, nor a resolver commit.
	sync_verdict="$(jq -r --slurpfile default "${sync_tmp}/default.json" \
		--arg subject "chore: sync ${sync_default_branch} into ${sync_base_ref}" '
		(reduce ($default[0].commits[] | .sha | select(type == "string")) as $s ({}; .[$s] = true)) as $d
		| [ .commits[]
			| ((.commit.message // "") | tostring | split("\n")[0]) as $title
			| select(
				($d[(.sha // "") | tostring] != true)
				and ((((.parents // []) | length) == 2 and $title == $subject
					and $d[(.parents[1].sha // "") | tostring] == true) | not)
				and ($title | startswith("[ai-merge-resolve]") | not)
			) ]
		| length' "${sync_tmp}/range.json" 2>/dev/null)" || sync_reject api_error
	[[ "${sync_verdict}" =~ ^[0-9]+$ ]] || sync_reject api_error
	[ "${sync_verdict}" -eq 0 ] || sync_reject foreign_commit

	# GET 3: the PR's file list, 100 per page, at most 30 pages.
	[ "${sync_pr_changed_files}" -le 3000 ] || sync_reject truncated
	sync_pages=$(( (sync_pr_changed_files + 99) / 100 ))
	[ "${sync_pages}" -ge 1 ] || sync_pages=1
	: > "${sync_tmp}/pr_files.jsonl" || sync_reject api_error
	for (( sync_page = 1; sync_page <= sync_pages; sync_page++ )); do
		ci_final_merge_ref_api_get "pulls/${FINAL_CI_PR_NUMBER}/files?per_page=100&page=${sync_page}" > "${sync_tmp}/page.json" || sync_reject api_error
		sync_page_len="$(jq -r 'if type == "array" then length else "x" end' "${sync_tmp}/page.json" 2>/dev/null)" || sync_reject api_error
		[[ "${sync_page_len}" =~ ^[0-9]+$ ]] || sync_reject api_error
		jq -c '.[]' "${sync_tmp}/page.json" >> "${sync_tmp}/pr_files.jsonl" 2>/dev/null || sync_reject api_error
		[ "${sync_page_len}" -ge 100 ] || break
	done
	sync_listed="$(wc -l < "${sync_tmp}/pr_files.jsonl" | tr -d ' ')"
	# More than 3000, or not the count GitHub reports: not a complete listing.
	if ! [[ "${sync_listed}" =~ ^[0-9]+$ ]] || [ "${sync_listed}" -gt 3000 ] || [ "${sync_listed}" -ne "${sync_pr_changed_files}" ]; then
		sync_reject truncated
	fi

	sync_verdict="$(jq -r --slurpfile pr "${sync_tmp}/pr_files.jsonl" '
		(reduce ($pr[] | (.filename, .previous_filename) | select(type == "string")) as $f ({}; .[$f] = true)) as $p
		| [ .files[] | (.filename, .previous_filename) | select(type == "string") | select($p[.] == true) ] | length' \
		"${sync_tmp}/range.json" 2>/dev/null)" || sync_reject api_error
	[[ "${sync_verdict}" =~ ^[0-9]+$ ]] || sync_reject api_error
	[ "${sync_verdict}" -eq 0 ] || sync_reject file_overlap

	# GET 4: the live PR head after the listing. `pulls/<n>/files` lists the
	# live head's files; a head that moved since the checkout (same file count,
	# other files) would have been checked against the wrong set.
	sync_live_head="$(ci_final_merge_ref_pull '.head.sha // ""' 2>/dev/null)" || sync_reject api_error
	[ "${sync_live_head}" = "${GITHUB_SHA}" ] || sync_reject api_error
	# The live base tip after all reads (`git ls-remote`, no API call): a base
	# commit that landed during the reads above is outside the checked range,
	# so a moved (or unreadable) tip fails closed as api_error.
	sync_tip_now="$(ci_final_merge_ref_tip "${sync_base_ref}")"
	[ "${sync_tip_now}" = "${sync_tip}" ] || sync_reject api_error

	sync_counts="$(jq -r '"\(.total_commits) \(.files | length)"' "${sync_tmp}/range.json" 2>/dev/null)" || sync_reject api_error
	[[ "${sync_counts}" =~ ^[0-9]+\ [0-9]+$ ]] || sync_reject api_error
	echo "ok ${sync_tested_base} ${sync_counts}"
	exit 0
)

ci_final_merge_ref_tip()
{
	git ls-remote origin "refs/heads/$1" 2>/dev/null | awk 'NR == 1 { print $1 }'
}

ci_final_merge_ref_main()
{
	local depth=1 switch attempts sleep_s base_ref tip first second seen_tips attempt mergeable fetched fetched_any=false
	local -a sync_pull_fields=()
	local sync_default_branch sync_pr_changed_files sync_switch sync_result sync_rc sync_last_tip sync_accepted_base sync_commits sync_files sync_detail_suffix=""
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--depth) depth="${2:-}"; shift 2 ;;
			*) shift ;;
		esac
	done
	{ [[ "${depth}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${depth}))" -ge 1 ] && [ "$((10#${depth}))" -le 50 ]; } || depth=1
	depth=$((10#${depth}))
	if [ -z "${FINAL_CI_PR_NUMBER:-}" ]; then
		ci_final_merge_ref_log skipped not_final_ci
		return 0
	fi
	switch="$(printf '%s' "${CI_FINAL_MERGE_REF_FRESH_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${switch}" in
		true|1|yes|on) ;;
		*)
			ci_final_merge_ref_log skipped disabled
			return 0
			;;
	esac
	if ! [[ "${FINAL_CI_PR_NUMBER}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${GITHUB_SHA:-}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${GITHUB_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		ci_final_merge_ref_log skipped invalid_input
		return 0
	fi
	attempts="${CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS:-6}"
	{ [[ "${attempts}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${attempts}))" -ge 1 ] && [ "$((10#${attempts}))" -le 20 ]; } || attempts=6
	attempts=$((10#${attempts}))
	sleep_s="${CI_FINAL_MERGE_REF_REFRESH_SECONDS:-10}"
	{ [[ "${sleep_s}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${sleep_s}))" -ge 1 ] && [ "$((10#${sleep_s}))" -le 60 ]; } || sleep_s=10
	sleep_s=$((10#${sleep_s}))

	read -r first second <<< "$(ci_final_merge_ref_parents HEAD)"
	if ! [[ "${first:-}" =~ ^[0-9a-f]{40}$ ]] || [ "${second:-}" != "${GITHUB_SHA}" ]; then
		# Not a two-parent merge of the dispatched head: the shallow-checkout
		# steps fail such a checkout with their own message.
		ci_final_merge_ref_log skipped not_a_merge_of_the_head "tested_base=${first:-none}"
		return 0
	fi
	# One read, one field per line (empty lines kept, so a missing field cannot
	# shift the others): the base branch, the default branch and
	# `changed_files`. The last two are only used by the sync-only check on the
	# final stale path, which fails closed on bad values.
	mapfile -t sync_pull_fields < <(ci_final_merge_ref_pull '(.base.ref // ""), (.base.repo.default_branch // ""), (.changed_files // "" | tostring)' || true)
	base_ref="${sync_pull_fields[0]:-}"
	sync_default_branch="${sync_pull_fields[1]:-}"
	sync_pr_changed_files="${sync_pull_fields[2]:-}"
	if ! [[ "${base_ref}" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "${base_ref}" == *..* ]] || [[ "${base_ref}" == -* ]]; then
		echo "::warning::CI_FINAL_MERGE_REF: could not read the base branch of PR #${FINAL_CI_PR_NUMBER}; testing the checked-out merge ref unverified."
		ci_final_merge_ref_log skipped base_ref_unreadable "tested_base=${first}"
		return 0
	fi
	tip="$(ci_final_merge_ref_tip "${base_ref}")"
	if ! [[ "${tip}" =~ ^[0-9a-f]{40}$ ]]; then
		echo "::warning::CI_FINAL_MERGE_REF: could not read the tip of ${base_ref}; testing the checked-out merge ref unverified."
		ci_final_merge_ref_log skipped tip_unreadable "base=${base_ref} tested_base=${first}"
		return 0
	fi
	if [ "${first}" = "${tip}" ]; then
		ci_final_merge_ref_log fresh base_tip "base=${base_ref} tested_base=${first}"
		return 0
	fi
	echo "CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge merges ${GITHUB_SHA} into ${first}, but ${base_ref} is at ${tip}; asking GitHub to recompute the merge ref."
	seen_tips=" ${tip} "
	for (( attempt = 1; attempt <= attempts; attempt++ )); do
		mergeable="$(ci_final_merge_ref_pull '.mergeable | tostring' || true)"
		[ -n "${mergeable}" ] || mergeable="unknown"
		if [ "${mergeable}" = "false" ]; then
			echo "::error::CI_FINAL_MERGE_REF: PR #${FINAL_CI_PR_NUMBER} no longer merges cleanly into ${base_ref} (${tip}); GitHub cannot refresh refs/pull/${FINAL_CI_PR_NUMBER}/merge. Resolve the conflict with the base; this is not a test failure."
			ci_final_merge_ref_log conflict not_mergeable "base=${base_ref} tested_base=${first} tip=${tip}"
			return 1
		fi
		sleep "${sleep_s}"
		fetched=false
		if git fetch --no-tags --quiet --depth="${depth}" origin "+refs/pull/${FINAL_CI_PR_NUMBER}/merge:refs/remotes/pull/${FINAL_CI_PR_NUMBER}/merge" 2>/dev/null; then
			fetched=true
			fetched_any=true
		fi
		tip="$(ci_final_merge_ref_tip "${base_ref}")"
		[[ "${tip}" =~ ^[0-9a-f]{40}$ ]] && seen_tips+="${tip} "
		[ "${fetched}" = "true" ] || continue
		read -r first second <<< "$(ci_final_merge_ref_parents "refs/remotes/pull/${FINAL_CI_PR_NUMBER}/merge")"
		if [ "${second:-}" != "${GITHUB_SHA}" ]; then
			# The PR head moved: a newer final run replaces this one.
			ci_final_merge_ref_log skipped head_moved "base=${base_ref} merged_head=${second:-none}"
			return 0
		fi
		if [[ "${first:-}" =~ ^[0-9a-f]{40}$ ]] && [[ "${seen_tips}" == *" ${first} "* ]]; then
			if git checkout --quiet --detach --force "refs/remotes/pull/${FINAL_CI_PR_NUMBER}/merge"; then
				ci_final_merge_ref_log refreshed base_tip "base=${base_ref} tested_base=${first} attempt=${attempt}"
				return 0
			fi
			echo "::warning::CI_FINAL_MERGE_REF: could not check out the refreshed merge ref; retrying."
		fi
	done
	if [ "${fetched_any}" != "true" ]; then
		# No re-fetch worked (e.g. a private repository and a checkout without
		# persisted credentials): staleness is known but cannot be fixed here,
		# so warn and test the checkout rather than fail a run that may pass.
		echo "::warning::CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge merges into ${first}, not the tip ${tip:-unknown} of ${base_ref}, and could not be re-fetched; testing the checked-out merge ref."
		ci_final_merge_ref_log skipped fetch_unavailable "base=${base_ref} tested_base=${first} tip=${tip:-none}"
		return 0
	fi
	sync_switch="$(printf '%s' "${CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${sync_switch}" in
		true|1|yes|on)
			# The tip from the last lookup only: when that lookup failed, an
			# older tip from seen_tips could hide newer base commits, so an
			# invalid value fails closed (api_error) inside the check.
			sync_last_tip="${tip:-}"
			sync_result="$(ci_final_merge_ref_sync_only "${base_ref}" "${sync_last_tip}" "${sync_default_branch}" "${sync_pr_changed_files}")"
			sync_rc=$?
			if [ "${sync_rc}" -eq 0 ] && [[ "${sync_result}" =~ ^ok\ ([0-9a-f]{40})\ ([0-9]+)\ ([0-9]+)$ ]]; then
				sync_accepted_base="${BASH_REMATCH[1]}"
				sync_commits="${BASH_REMATCH[2]}"
				sync_files="${BASH_REMATCH[3]}"
				echo "CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge merges into ${sync_accepted_base}, behind ${sync_last_tip}, but only integration syncs and resolver commits that touch none of this PR's files separate them; testing the checked-out merge."
				ci_final_merge_ref_log stale_accepted sync_only_no_overlap "base=${base_ref} tested_base=${sync_accepted_base} tip=${sync_last_tip} commits=${sync_commits} files=${sync_files}"
				if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
					{ printf '%s\n' "- Final-CI merge ref for PR #${FINAL_CI_PR_NUMBER} merges into ${sync_accepted_base}, behind ${base_ref} at ${sync_last_tip}, only by ${sync_commits} integration-sync/resolver commit(s) changing ${sync_files} file(s), none of them in this PR: tested the checked-out merge (CI_FINAL_MERGE_REF outcome=stale_accepted)." >> "${GITHUB_STEP_SUMMARY}"; } 2>/dev/null || true
				fi
				return 0
			fi
			case "${sync_result}" in
				not_ahead|truncated|foreign_commit|file_overlap|api_error) ;;
				*) sync_result=api_error ;;
			esac
			sync_detail_suffix=" detail=${sync_result}"
			;;
	esac
	echo "::error::CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge still merges into ${first:-unknown}, not the current tip ${tip:-unknown} of ${base_ref}, after ${attempts} refresh attempt(s). This run would test an old base, so it stops here; this is not a defect in the PR. The review sweep updates a head that is behind its base, and the next final run tests the current base."
	ci_final_merge_ref_log stale not_refreshed "base=${base_ref} tested_base=${first:-none} tip=${tip:-none}${sync_detail_suffix}"
	return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	ci_final_merge_ref_main "$@"
fi
