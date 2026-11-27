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
# checked-out merge commit's first parent with the base the run tests:
#   - The run's base is pinned (final-CI local merge, 2026-11-18): the newest
#     first-parent commit of the base branch whose committer date is not after
#     the event payload's `repository.pushed_at` (GITHUB_EVENT_PATH; without
#     it, the run's `created_at` from one `GET actions/runs/<GITHUB_RUN_ID>`),
#     i.e. the base tip when the run was dispatched. Every job of the run, and every
#     re-run attempt, computes the same pin from the same immutable history,
#     so all jobs test the same base even when they start an hour apart and
#     the base moves in between. When the live tip (`git ls-remote`, no API
#     call) is not newer than created_at the pin is the tip itself (one
#     depth-1 fetch of the tip, skipped when the object is local); otherwise
#     the base branch is fetched CI_FINAL_MERGE_REF_MERGE_DEPTH deep (default
#     200, 1-1000, never below --depth) and walked. A pin that cannot be
#     computed (run unreadable, pin deeper than that window, fetch failed)
#     falls back to the live tip, per job, as before the pin existed; the log
#     line then carries `pin=none pin_reason=<token>`.
#     Known gap: the pin uses committer date as a stand-in for push time.
#     If a commit is pushed to the base after dispatch but has an older
#     committer date (a direct push of a commit made locally earlier),
#     jobs that start after that push pin it, and jobs that started before
#     pin the older tip. GitHub's merge, squash and rebase buttons, and the
#     pipeline's own pushes, set the committer date at push time, so the
#     gap needs such a direct push. Closing it would take a push-time signal
#     that costs an extra API read per job (§15); the gap is left as it is.
#   - first parent equal to the pin (or the tip, without a pin): logs
#     `outcome=fresh` (reason `base_tip`, or `pinned_base` when the base has
#     moved since the run was created) and exits 0;
#   - different, and the pin is the live tip: up to
#     CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS times (default 6, 1-20),
#     CI_FINAL_MERGE_REF_REFRESH_SECONDS apart (default 10, 1-60), reads
#     `pulls/<n>` (which makes GitHub recompute the merge ref), re-fetches the
#     merge ref and re-reads the tip. When the new merge commit still merges
#     the dispatched head (GITHUB_SHA) and its first parent is the pin (without
#     a pin: a base tip seen during this step), the job checks it out
#     (`outcome=refreshed`) and continues on the current combination;
#   - still stale after the attempts, or the base moved after the run was
#     created (GitHub's ref can never match the pin then, so no attempt is
#     made): the job builds the merge itself (`outcome=merged_locally`, reason
#     `not_refreshed` or `pinned_base`). It fetches GITHUB_SHA and the pin (or
#     the last tip read) CI_FINAL_MERGE_REF_MERGE_DEPTH deep, runs
#     `git merge-tree --write-tree` (no worktree, no hooks, no merge drivers
#     beyond git's built-ins), writes the merge commit with `commit-tree`
#     (parents: base, head; fixed author, committer, dates and message; no
#     signing), checks it out and appends
#     `CI_FINAL_MERGE_REF_LOCAL_MERGE=<sha>` to GITHUB_ENV, so the later
#     shallow-fetch steps fetch its parents instead of a commit origin does
#     not have. The commit is a pure function of (base, head), so every job
#     that merges locally builds the same commit; the log carries `merge=` and
#     `tree=`, and the fresh and refreshed lines carry `tree=` too, so a run's
#     jobs can be compared;
#   - GitHub reports the PR as not mergeable during the attempts while the
#     tip is still the pin (`outcome=conflict reason=not_mergeable`; once the
#     tip has moved past the pin, or when the tip cannot be re-read after
#     that answer, the local merge into the pin decides), or
#     the local merge conflicts (`.gitattributes` merge drivers are ignored)
#     (`outcome=conflict reason=merge_conflict`): `::error::` and exit 1;
#   - the local merge cannot be built (`outcome=stale` with reason
#     `merge_base_too_deep`: the merge base is more than
#     CI_FINAL_MERGE_REF_MERGE_DEPTH commits below the head or the base, so the
#     head is far behind; `merge_fetch_failed`; `merge_tree_unsupported`: the
#     runner's git predates `--attr-source` (2.41); `merge_failed`): `::error::`
#     and exit 1, as a stale ref did before. The red `lint` sends the PR back
#     through `final-ci-failed`, and the review sweep's base sync
#     (scripts/review_final_ci_sweep.sh) updates a head that is behind.
#   - before that stale exit fails the job, `ci_final_merge_ref_sync_only`
#     (switch CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED, default true;
#     issue #10177, Phase 10 / D10 of
#     docs/plans/pipeline-human-wait-elimination-plan.md) keeps the original
#     checkout and exits 0 (`outcome=stale_accepted
#     reason=sync_only_no_overlap`) when all of these hold, checked in this
#     order: (0) the PR's base is not the default branch (`git ls-remote
#     --symref origin HEAD`); (1) `compare/<tested_base>...<tip>` is `ahead`
#     with a complete listing (at most 250 commits, all listed, under 300
#     files); (2) every commit in that range is a default-branch commit
#     (`compare/<tested_base>...<default>`), an exact `chore: sync <default>
#     into <base>` two-parent merge whose second parent is a default-branch
#     commit, or a `[ai-merge-resolve]` commit; (3) none of the range's files
#     (with `previous_filename` for renames) is in the PR's paginated files
#     listing (at most 3000, counted against `changed_files`). tested_base is
#     the first parent of the commit the job checked out, captured before the
#     refresh loop. Any failed condition or unreadable response keeps the
#     `::error::` and exit 1 and adds `detail=default_base|not_ahead|
#     truncated|foreign_commit|file_overlap|too_many_files|api_error` to the
#     stale line; with the switch off the stale line is unchanged. The
#     residual risk (sync and resolver content in files the PR does not
#     touch is not tested together with the PR) is recorded in D10; set the
#     switch to false to roll back.
# Fail-open: not a final-CI run, the switch off, an unreadable base branch,
# tip or commit, a merge ref that could not be re-fetched at all and no other
# fetch of this step worked (e.g. a private repository and a checkout without
# persisted credentials), or a head that moved (the run is superseded anyway)
# logs and exits 0, so the job tests what it checked out, as before this
# check.
#
# Security: this step runs the PR's own copy of the script, before any test
# code, with the job's existing token (none in tests-sandbox-python). The
# local merge adds only `git fetch` calls to the same origin with the
# checkout's own credentials, and git commands that run no hooks
# (`core.hooksPath=/dev/null`), no signing and no editor; it reads or writes
# no secret, and the only file it writes outside .git is the checkout of the
# merged tree. GITHUB_ENV gets one validated 40-hex value.
#
# Usage: ci_final_merge_ref_fresh.sh [--depth <n>]   (fetch depth of the
# job's checkout, default 1, 1-50; the re-fetch keeps the same depth)
# Env: FINAL_CI_PR_NUMBER (empty = not a final-CI run, no-op), GITHUB_SHA,
# GITHUB_REPOSITORY, GITHUB_EVENT_PATH and GITHUB_RUN_ID (the pin), GITHUB_ENV
# (optional), GH_TOKEN
# (optional: without it, or when the token cannot read an endpoint, the PR and
# the run are read from the public REST API unauthenticated;
# CI_FINAL_MERGE_REF_API_URL overrides the API base for tests),
# CI_FINAL_MERGE_REF_FRESH_ENABLED (default true; true/1/yes/on), the refresh
# knobs above, CI_FINAL_MERGE_REF_MERGE_DEPTH (default 200),
# CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED (default true; true/1/yes/on),
# GITHUB_STEP_SUMMARY (optional; one line on a stale_accepted exit).
# API budget (§15): one `GET pulls/<n>` for the base branch name (the job's
# shallow-checkout step reads the same PR later for its own exports; the
# check must run before that step, so it cannot reuse that read), one
# `GET actions/runs/<id>` for the pin only when the event payload carries no
# `repository.pushed_at` (no earlier step of the job reads the run), plus one `GET pulls/<n>` per refresh attempt, only when the merge ref
# is stale and the pin is the live tip. The sync-only check adds up to 32 GETs
# per job (two `compare/...` reads plus at most 30 pages of
# `pulls/<n>/files?per_page=100`), only on the already-failing stale exit and
# never on the fresh, refreshed or merged-locally paths. Condition 0 needs no
# call, a failed compare check stops before the files listing, and a typical
# PR's listing is one page. `changed_files` comes from the widened `pulls/<n>`
# read above, not from a new call. Audited reads: the `pulls/<n>` read (base
# ref, changed_files, mergeable) and the `actions/runs/<id>` read carry no
# compare or files data, so the compare and files reads are new. Each of the
# ten final-CI jobs that reaches the stale exit pays it; on a token-less job a
# rate-limit refusal fails closed as `detail=api_error`.
# Log: `CI_FINAL_MERGE_REF pr=<n> head=<sha> outcome=fresh|refreshed|merged_locally|stale|stale_accepted|conflict|skipped reason=<token> [base=<ref>] [tested_base=<sha>] [tip=<sha>] [attempt=<n>] [merge=<sha>] [tree=<sha>] [commits=<k>] [files=<m>] [pin=<sha>|pin=none pin_reason=<token>] [detail=<token>]` (`stale_accepted` has reason `sync_only_no_overlap`).

set -uo pipefail

# Fixed identity and dates of the local merge commit: together with the fixed
# message they make the commit a pure function of (base, head).
CI_FINAL_MERGE_REF_LOCAL_IDENTITY_NAME="final-ci-local-merge"
CI_FINAL_MERGE_REF_LOCAL_IDENTITY_EMAIL="final-ci-local-merge@invalid"
CI_FINAL_MERGE_REF_LOCAL_DATE="2000-01-01T00:00:00+0000"
CI_FINAL_MERGE_REF_EMPTY_TREE="4b825dc642cb6eb9a060e54bf8d69288fbee4904"

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

# `ci_final_merge_ref_api <path under repos/<owner>/<repo>/> <jq filter>`: one
# GET through `gh api`. A job that gets no token (tests-sandbox-python passes
# none, see tests/test_sandbox_python_coverage.py), or whose token cannot read
# the endpoint (a job with `permissions:` that omit `actions`), reads the
# public REST endpoint without credentials instead. Prints nothing and
# returns 1 on failure.
ci_final_merge_ref_api()
{
	local api_path="$1" filter="$2" api_json=""
	if [ -n "${GH_TOKEN:-}" ] && gh api "repos/${GITHUB_REPOSITORY}/${api_path}" --jq "${filter}" 2>/dev/null; then
		return 0
	fi
	api_json="$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
		"${CI_FINAL_MERGE_REF_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY}/${api_path}" 2>/dev/null)" || return 1
	printf '%s' "${api_json}" | jq -r "${filter}" 2>/dev/null
}

# `ci_final_merge_ref_pull <jq filter>`: one `GET pulls/<n>`.
ci_final_merge_ref_pull()
{
	ci_final_merge_ref_api "pulls/${FINAL_CI_PR_NUMBER}" "$1"
}

ci_final_merge_ref_tip()
{
	git ls-remote origin "refs/heads/$1" 2>/dev/null | awk 'NR == 1 { print $1 }'
}

# `ci_final_merge_ref_compute_pin <base ref> <live tip> <merge depth>` sets
# CI_FINAL_MERGE_REF_PIN (40-hex or empty) and CI_FINAL_MERGE_REF_PIN_REASON,
# and CI_FINAL_MERGE_REF_FETCHED=true when one of its fetches worked.
ci_final_merge_ref_compute_pin()
{
	local base_ref="$1" live_tip="$2" merge_depth="$3" run_created="" run_created_epoch="" tip_epoch="" pinned=""
	CI_FINAL_MERGE_REF_PIN=""
	CI_FINAL_MERGE_REF_PIN_REASON=""
	CI_FINAL_MERGE_REF_PIN_SOURCE=""
	# The event payload's `repository.pushed_at` (the repository's last push of
	# any branch when the event was created) is the same file in every job and
	# attempt and costs no API call or token scope; every base push before the
	# dispatch is at or before it, and none after. Without it, the run's
	# `created_at` (one GET) bounds the same tip.
	if [ -n "${GITHUB_EVENT_PATH:-}" ] && [ -r "${GITHUB_EVENT_PATH}" ]; then
		run_created="$(jq -r '.repository.pushed_at // "" | tostring' "${GITHUB_EVENT_PATH}" 2>/dev/null || true)"
	fi
	if [[ "${run_created}" =~ ^[1-9][0-9]{8,10}$ ]]; then
		run_created_epoch="${run_created}"
	elif [[ "${run_created}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
		run_created_epoch="$(date -u -d "${run_created}" +%s 2>/dev/null || true)"
	fi
	if [[ "${run_created_epoch}" =~ ^[0-9]+$ ]]; then
		CI_FINAL_MERGE_REF_PIN_SOURCE="event_pushed_at"
	else
		CI_FINAL_MERGE_REF_PIN_SOURCE="run_created_at"
		if ! [[ "${GITHUB_RUN_ID:-}" =~ ^[1-9][0-9]*$ ]]; then
			CI_FINAL_MERGE_REF_PIN_REASON="run_id_invalid"
			return 0
		fi
		run_created="$(ci_final_merge_ref_api "actions/runs/${GITHUB_RUN_ID}" '.created_at // ""' || true)"
		run_created_epoch=""
		if ! [[ "${run_created}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
			|| ! run_created_epoch="$(date -u -d "${run_created}" +%s 2>/dev/null)" || ! [[ "${run_created_epoch}" =~ ^[0-9]+$ ]]; then
			CI_FINAL_MERGE_REF_PIN_REASON="run_created_unreadable"
			return 0
		fi
	fi
	# Cheap path: the live tip already existed when the run was created. Fetch
	# only a missing tip, so a deeper checkout keeps its history.
	if ! git cat-file -e "${live_tip}^{commit}" 2>/dev/null; then
		if git fetch --no-tags --quiet --depth=1 origin "${live_tip}" 2>/dev/null; then
			CI_FINAL_MERGE_REF_FETCHED=true
		fi
	fi
	tip_epoch="$(git log -1 --format=%ct "${live_tip}" 2>/dev/null || true)"
	if [[ "${tip_epoch}" =~ ^[0-9]+$ ]] && [ "${tip_epoch}" -le "${run_created_epoch}" ]; then
		CI_FINAL_MERGE_REF_PIN="${live_tip}"
		CI_FINAL_MERGE_REF_PIN_REASON="tip_before_run"
		return 0
	fi
	# The base moved after the run was created (or the tip's date is
	# unknown): walk the base's first-parent chain back to created_at.
	if ! git fetch --no-tags --quiet --depth="${merge_depth}" origin "+refs/heads/${base_ref}:refs/remotes/ci-final-merge-ref/base" 2>/dev/null; then
		CI_FINAL_MERGE_REF_PIN_REASON="base_fetch_failed"
		return 0
	fi
	CI_FINAL_MERGE_REF_FETCHED=true
	pinned="$(git rev-list --first-parent --max-count=1 --until="${run_created_epoch}" refs/remotes/ci-final-merge-ref/base 2>/dev/null || true)"
	if ! [[ "${pinned}" =~ ^[0-9a-f]{40}$ ]]; then
		CI_FINAL_MERGE_REF_PIN_REASON="beyond_depth"
		return 0
	fi
	CI_FINAL_MERGE_REF_PIN="${pinned}"
	CI_FINAL_MERGE_REF_PIN_REASON="base_at_run_created"
}

# `ci_final_merge_ref_merge_locally <base commit> <merge depth>` builds the
# merge of GITHUB_SHA into <base commit> and checks it out. Sets
# CI_FINAL_MERGE_REF_LOCAL_COMMIT / _TREE on success and
# CI_FINAL_MERGE_REF_LOCAL_REASON on failure. Returns 0 (checked out),
# 1 (merge conflict), 2 (could not build: see the reason).
ci_final_merge_ref_merge_locally()
{
	local merge_into="$1" merge_depth="$2" merge_tree_out="" merge_tree_rc=0 local_tree="" local_commit=""
	CI_FINAL_MERGE_REF_LOCAL_COMMIT=""
	CI_FINAL_MERGE_REF_LOCAL_TREE=""
	CI_FINAL_MERGE_REF_LOCAL_REASON=""
	if ! git fetch --no-tags --quiet --depth="${merge_depth}" origin "${GITHUB_SHA}" "${merge_into}" 2>/dev/null; then
		CI_FINAL_MERGE_REF_LOCAL_REASON="merge_fetch_failed"
		return 2
	fi
	CI_FINAL_MERGE_REF_FETCHED=true
	if ! git merge-base "${merge_into}" "${GITHUB_SHA}" >/dev/null 2>&1; then
		CI_FINAL_MERGE_REF_LOCAL_REASON="merge_base_too_deep"
		return 2
	fi
	# Attributes come from the empty tree, so a `.gitattributes` merge driver
	# (this repository sets `CHANGELOG.md merge=union`) cannot resolve a
	# conflict that GitHub's merge would report: a doubtful merge fails.
	merge_tree_out="$(git -c core.hooksPath=/dev/null --attr-source="${CI_FINAL_MERGE_REF_EMPTY_TREE}" merge-tree --write-tree --no-messages "${merge_into}" "${GITHUB_SHA}" 2>/dev/null)" || merge_tree_rc=$?
	if [ "${merge_tree_rc}" -eq 129 ]; then
		# Usage error: the runner's git predates `--attr-source` (git 2.41),
		# so tell an old git apart from a merge that really failed.
		CI_FINAL_MERGE_REF_LOCAL_REASON="merge_tree_unsupported"
		return 2
	fi
	if [ "${merge_tree_rc}" -eq 1 ]; then
		CI_FINAL_MERGE_REF_LOCAL_REASON="merge_conflict"
		return 1
	fi
	local_tree="$(printf '%s\n' "${merge_tree_out}" | awk 'NR == 1 { print $1 }')"
	if [ "${merge_tree_rc}" -ne 0 ] || ! [[ "${local_tree}" =~ ^[0-9a-f]{40}$ ]]; then
		CI_FINAL_MERGE_REF_LOCAL_REASON="merge_failed"
		return 2
	fi
	local_commit="$(printf 'Final-CI local merge of %s into %s\n' "${GITHUB_SHA}" "${merge_into}" \
		| GIT_AUTHOR_NAME="${CI_FINAL_MERGE_REF_LOCAL_IDENTITY_NAME}" GIT_AUTHOR_EMAIL="${CI_FINAL_MERGE_REF_LOCAL_IDENTITY_EMAIL}" \
			GIT_AUTHOR_DATE="${CI_FINAL_MERGE_REF_LOCAL_DATE}" GIT_COMMITTER_NAME="${CI_FINAL_MERGE_REF_LOCAL_IDENTITY_NAME}" \
			GIT_COMMITTER_EMAIL="${CI_FINAL_MERGE_REF_LOCAL_IDENTITY_EMAIL}" GIT_COMMITTER_DATE="${CI_FINAL_MERGE_REF_LOCAL_DATE}" \
			git -c commit.gpgsign=false -c core.hooksPath=/dev/null commit-tree "${local_tree}" -p "${merge_into}" -p "${GITHUB_SHA}" 2>/dev/null)"
	if ! [[ "${local_commit}" =~ ^[0-9a-f]{40}$ ]]; then
		CI_FINAL_MERGE_REF_LOCAL_REASON="merge_failed"
		return 2
	fi
	if ! git -c core.hooksPath=/dev/null checkout --quiet --detach --force "${local_commit}" 2>/dev/null \
		|| [ "$(git rev-parse HEAD 2>/dev/null)" != "${local_commit}" ] \
		|| [ "$(git rev-parse 'HEAD^{tree}' 2>/dev/null)" != "${local_tree}" ]; then
		CI_FINAL_MERGE_REF_LOCAL_REASON="merge_failed"
		return 2
	fi
	CI_FINAL_MERGE_REF_LOCAL_COMMIT="${local_commit}"
	CI_FINAL_MERGE_REF_LOCAL_TREE="${local_tree}"
	if [ -n "${GITHUB_ENV:-}" ]; then
		echo "CI_FINAL_MERGE_REF_LOCAL_MERGE=${local_commit}" >> "${GITHUB_ENV}"
	fi
	return 0
}

# `ci_final_merge_ref_default_branch` prints the short name of origin's
# default branch from `git ls-remote --symref origin HEAD` (no API call), or
# nothing and returns 1 when it is unreadable or not a safe ref name.
ci_final_merge_ref_default_branch()
{
	local symref_name=""
	symref_name="$(git ls-remote --symref origin HEAD 2>/dev/null \
		| awk -F '\t' '$2 == "HEAD" && index($1, "ref: refs/heads/") == 1 { print substr($1, 17); exit }')"
	if ! [[ "${symref_name}" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "${symref_name}" == *..* ]] || [[ "${symref_name}" == -* ]]; then
		return 1
	fi
	printf '%s\n' "${symref_name}"
}

# jq filters of the sync-only check. Every field is non-empty ("-" for a
# missing value), so a tab-split read cannot shift fields; commit subjects
# and file names are JSON-encoded, so no author-controlled tab or newline can
# break a line, and they are only ever compared as strings.
CI_FINAL_MERGE_REF_SYNC_ONLY_RANGE_FILTER='if type == "object" and has("status") then ("H\t\(.status // "-" | tostring)\t\(.total_commits // "-" | tostring)\t\((.commits // []) | length)\t\((.files // []) | length)", ((.commits // [])[] | "C\t\(.sha // "-")\t\((.parents // []) | length)\t\((.parents // [])[1].sha // "-")\t\((.commit.message // "") | split("\n") | (.[0] // "") | tojson)"), ((.files // [])[] | "F\t\(.filename // "" | tojson)", (select(.status == "renamed" and ((.previous_filename // "") != "")) | "F\t\(.previous_filename | tojson)"))) else empty end'
CI_FINAL_MERGE_REF_SYNC_ONLY_DEFAULT_FILTER='if type == "object" and has("status") then ("H\t\(.status // "-" | tostring)\t\(.total_commits // "-" | tostring)\t\((.commits // []) | length)", ((.commits // [])[] | "C\t\(.sha // "-")")) else empty end'
CI_FINAL_MERGE_REF_SYNC_ONLY_FILES_FILTER='if type == "array" then ("A\t\(length)", (.[] | "F\t\(.filename // "" | tojson)", (select(.status == "renamed" and ((.previous_filename // "") != "")) | "F\t\(.previous_filename | tojson)"))) else empty end'

# `ci_final_merge_ref_sync_only <tested_base> <tip> <base ref> <changed_files>
# <checked-out commit>`: the stale-path acceptance described in the header.
# Sets CI_FINAL_MERGE_REF_SYNC_ONLY_DETAIL (first failed condition),
# _COMMITS, _FILES and _TIP (the tip it judged). Returns 0 to accept, 1 to
# reject. The temporary directory is removed here on every return; no EXIT
# trap is set, so a caller's trap is never replaced.
ci_final_merge_ref_sync_only()
{
	local so_work_dir="" so_rc=0
	CI_FINAL_MERGE_REF_SYNC_ONLY_DETAIL=""
	CI_FINAL_MERGE_REF_SYNC_ONLY_COMMITS=""
	CI_FINAL_MERGE_REF_SYNC_ONLY_FILES=""
	CI_FINAL_MERGE_REF_SYNC_ONLY_TIP=""
	if ! so_work_dir="$(mktemp -d 2>/dev/null)" || [ ! -d "${so_work_dir}" ]; then
		CI_FINAL_MERGE_REF_SYNC_ONLY_DETAIL="api_error"
		return 1
	fi
	ci_final_merge_ref_sync_only_check "${so_work_dir}" "$@" || so_rc=$?
	rm -rf -- "${so_work_dir}"
	if [ "${so_rc}" -ne 0 ]; then
		[ -n "${CI_FINAL_MERGE_REF_SYNC_ONLY_DETAIL}" ] || CI_FINAL_MERGE_REF_SYNC_ONLY_DETAIL="api_error"
		return 1
	fi
	return 0
}

# `ci_final_merge_ref_sync_only_fail <detail>` records the first failed
# condition; callers return 1 right after it.
ci_final_merge_ref_sync_only_fail()
{
	CI_FINAL_MERGE_REF_SYNC_ONLY_DETAIL="$1"
	return 0
}

ci_final_merge_ref_sync_only_check()
{
	local so_dir="$1" so_tested_base="$2" so_tip="$3" so_base_ref="$4" so_changed_files="$5" so_checked_out="$6"
	local so_p1="" so_p2="" so_p3="" so_default="" so_kind="" so_status="" so_total="" so_listed="" so_nfiles=""
	local so_sha="" so_pcount="" so_second="" so_subject="" so_sync_subject="" so_pages=0 so_page=0 so_entries=0
	local so_page_count="" so_lines=0 so_range_total=0
	# Pre-checks (all api_error): a readable tip, the original checkout with
	# parents <tested_base> <head>, and a readable default branch.
	if ! [[ "${so_tip}" =~ ^[0-9a-f]{40}$ ]]; then
		so_tip="$(ci_final_merge_ref_tip "${so_base_ref}")"
	fi
	if ! [[ "${so_tip}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${so_tested_base}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${so_checked_out}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${FINAL_CI_PR_NUMBER:-}" =~ ^[1-9][0-9]*$ ]]; then
		ci_final_merge_ref_sync_only_fail api_error; return 1
	fi
	CI_FINAL_MERGE_REF_SYNC_ONLY_TIP="${so_tip}"
	# A failed local merge can leave HEAD elsewhere; accepting means "keep the
	# original checkout", so restore and verify it.
	if [ "$(git rev-parse HEAD 2>/dev/null)" != "${so_checked_out}" ]; then
		git -c core.hooksPath=/dev/null checkout --quiet --detach --force "${so_checked_out}" >/dev/null 2>&1 || true
	fi
	if [ "$(git rev-parse HEAD 2>/dev/null)" != "${so_checked_out}" ]; then
		ci_final_merge_ref_sync_only_fail api_error; return 1
	fi
	read -r so_p1 so_p2 so_p3 <<< "$(ci_final_merge_ref_parents HEAD)"
	if [ "${so_p1:-}" != "${so_tested_base}" ] || [ "${so_p2:-}" != "${GITHUB_SHA:-}" ] || [ -n "${so_p3:-}" ]; then
		ci_final_merge_ref_sync_only_fail api_error; return 1
	fi
	so_default="$(ci_final_merge_ref_default_branch || true)"
	if [ -z "${so_default}" ]; then
		ci_final_merge_ref_sync_only_fail api_error; return 1
	fi
	# (0) Integration base only: for a default-branch PR every commit in range
	# is a default-branch commit, so (2) would pass vacuously.
	if [ "${so_base_ref}" = "${so_default}" ]; then
		ci_final_merge_ref_sync_only_fail default_base; return 1
	fi
	# (1) Ancestry and a complete range listing.
	ci_final_merge_ref_api "compare/${so_tested_base}...${so_tip}" "${CI_FINAL_MERGE_REF_SYNC_ONLY_RANGE_FILTER}" > "${so_dir}/range" 2>/dev/null || true
	IFS=$'\t' read -r so_kind so_status so_total so_listed so_nfiles < <(grep -m 1 $'^H\t' "${so_dir}/range" 2>/dev/null || true)
	if [ "${so_kind:-}" != "H" ] || ! [[ "${so_total:-}" =~ ^[0-9]{1,9}$ ]] || ! [[ "${so_listed:-}" =~ ^[0-9]{1,9}$ ]] \
		|| ! [[ "${so_nfiles:-}" =~ ^[0-9]{1,9}$ ]]; then
		ci_final_merge_ref_sync_only_fail api_error; return 1
	fi
	if [ "${so_status}" != "ahead" ]; then
		ci_final_merge_ref_sync_only_fail not_ahead; return 1
	fi
	if [ "$((10#${so_total}))" -gt 250 ] || [ "$((10#${so_listed}))" -ne "$((10#${so_total}))" ] || [ "$((10#${so_nfiles}))" -ge 300 ]; then
		ci_final_merge_ref_sync_only_fail truncated; return 1
	fi
	so_range_total=$((10#${so_total}))
	grep $'^C\t' "${so_dir}/range" > "${so_dir}/range_commits" 2>/dev/null || true
	so_lines="$(wc -l < "${so_dir}/range_commits" | tr -d ' ')"
	if [ "${so_lines}" != "$((10#${so_listed}))" ]; then
		ci_final_merge_ref_sync_only_fail api_error; return 1
	fi
	grep $'^F\t' "${so_dir}/range" | cut -f 2- | LC_ALL=C sort -u > "${so_dir}/range_files" 2>/dev/null || true
	# (2) Commit types, against the complete set of default-branch commits.
	ci_final_merge_ref_api "compare/${so_tested_base}...${so_default}" "${CI_FINAL_MERGE_REF_SYNC_ONLY_DEFAULT_FILTER}" > "${so_dir}/default" 2>/dev/null || true
	so_kind="" so_status="" so_total="" so_listed=""
	IFS=$'\t' read -r so_kind so_status so_total so_listed < <(grep -m 1 $'^H\t' "${so_dir}/default" 2>/dev/null || true)
	if [ "${so_kind:-}" != "H" ] || ! [[ "${so_total:-}" =~ ^[0-9]{1,9}$ ]] || ! [[ "${so_listed:-}" =~ ^[0-9]{1,9}$ ]]; then
		ci_final_merge_ref_sync_only_fail api_error; return 1
	fi
	if [ "$((10#${so_total}))" -gt 250 ] || [ "$((10#${so_listed}))" -ne "$((10#${so_total}))" ]; then
		ci_final_merge_ref_sync_only_fail truncated; return 1
	fi
	grep $'^C\t' "${so_dir}/default" | cut -f 2 > "${so_dir}/default_set" 2>/dev/null || true
	if grep -qvE '^[0-9a-f]{40}$' "${so_dir}/default_set"; then
		ci_final_merge_ref_sync_only_fail api_error; return 1
	fi
	# The subject scripts/orchestrate_poll_process.sh writes, JSON-encoded the
	# way the filter encodes subjects (branch names hold no character that
	# JSON escapes).
	so_sync_subject="\"chore: sync ${so_default} into ${so_base_ref}\""
	while IFS=$'\t' read -r so_kind so_sha so_pcount so_second so_subject; do
		if ! [[ "${so_sha}" =~ ^[0-9a-f]{40}$ ]]; then
			ci_final_merge_ref_sync_only_fail api_error; return 1
		fi
		if grep -qxF -- "${so_sha}" "${so_dir}/default_set"; then
			continue
		fi
		if [ "${so_pcount}" = "2" ] && [ "${so_subject}" = "${so_sync_subject}" ] \
			&& [[ "${so_second}" =~ ^[0-9a-f]{40}$ ]] && grep -qxF -- "${so_second}" "${so_dir}/default_set"; then
			continue
		fi
		if [[ "${so_subject}" == '"[ai-merge-resolve]'* ]]; then
			continue
		fi
		ci_final_merge_ref_sync_only_fail foreign_commit; return 1
	done < "${so_dir}/range_commits"
	# (3) No overlap with the PR's complete changed-file list.
	if ! [[ "${so_changed_files}" =~ ^[0-9]{1,9}$ ]]; then
		ci_final_merge_ref_sync_only_fail api_error; return 1
	fi
	so_changed_files=$((10#${so_changed_files}))
	if [ "${so_changed_files}" -gt 3000 ]; then
		ci_final_merge_ref_sync_only_fail too_many_files; return 1
	fi
	so_pages=$(( (so_changed_files + 99) / 100 ))
	: > "${so_dir}/pr_raw"
	for (( so_page = 1; so_page <= so_pages; so_page++ )); do
		if ! ci_final_merge_ref_api "pulls/${FINAL_CI_PR_NUMBER}/files?per_page=100&page=${so_page}" "${CI_FINAL_MERGE_REF_SYNC_ONLY_FILES_FILTER}" > "${so_dir}/page" 2>/dev/null; then
			ci_final_merge_ref_sync_only_fail api_error; return 1
		fi
		so_page_count="$(awk -F '\t' '$1 == "A" { print $2; exit }' "${so_dir}/page")"
		if ! [[ "${so_page_count}" =~ ^[0-9]{1,4}$ ]]; then
			ci_final_merge_ref_sync_only_fail api_error; return 1
		fi
		so_entries=$(( so_entries + 10#${so_page_count} ))
		grep $'^F\t' "${so_dir}/page" >> "${so_dir}/pr_raw" 2>/dev/null || true
	done
	if [ "${so_entries}" -ne "${so_changed_files}" ]; then
		ci_final_merge_ref_sync_only_fail truncated; return 1
	fi
	cut -f 2- "${so_dir}/pr_raw" | LC_ALL=C sort -u > "${so_dir}/pr_files"
	if [ -n "$(LC_ALL=C comm -12 "${so_dir}/range_files" "${so_dir}/pr_files")" ]; then
		ci_final_merge_ref_sync_only_fail file_overlap; return 1
	fi
	CI_FINAL_MERGE_REF_SYNC_ONLY_COMMITS="${so_range_total}"
	CI_FINAL_MERGE_REF_SYNC_ONLY_FILES="$(wc -l < "${so_dir}/range_files" | tr -d ' ')"
	return 0
}

ci_final_merge_ref_main()
{
	local depth=1 switch attempts sleep_s base_ref tip first second seen_tips attempt mergeable fetched
	local merge_depth pin pin_note merge_into merge_reason local_rc=0 head_tree fresh_tip deferred_to_pin=false
	local sync_only_tested_base="" sync_only_checked_out="" sync_only_pr_changed_files="" sync_only_pull_line=""
	local sync_only_switch="" sync_only_detail_note=""
	CI_FINAL_MERGE_REF_FETCHED=false
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
	merge_depth="${CI_FINAL_MERGE_REF_MERGE_DEPTH:-200}"
	{ [[ "${merge_depth}" =~ ^[0-9]{1,4}$ ]] && [ "$((10#${merge_depth}))" -ge 1 ] && [ "$((10#${merge_depth}))" -le 1000 ]; } || merge_depth=200
	merge_depth=$((10#${merge_depth}))
	[ "${merge_depth}" -ge "${depth}" ] || merge_depth="${depth}"

	read -r first second <<< "$(ci_final_merge_ref_parents HEAD)"
	if ! [[ "${first:-}" =~ ^[0-9a-f]{40}$ ]] || [ "${second:-}" != "${GITHUB_SHA}" ]; then
		# Not a two-parent merge of the dispatched head: the shallow-checkout
		# steps fail such a checkout with their own message.
		ci_final_merge_ref_log skipped not_a_merge_of_the_head "tested_base=${first:-none}"
		return 0
	fi
	# The sync-only check at the stale exit needs the tested base as the
	# first parent of the commit actually checked out: the refresh loop
	# reassigns `first` from each re-fetched ref, so keep it separately.
	sync_only_tested_base="${first}"
	sync_only_checked_out="$(git rev-parse HEAD 2>/dev/null || true)"
	# One read gives the base branch and, for that check, `changed_files`.
	sync_only_pull_line="$(ci_final_merge_ref_pull '"\(.base.ref // "")\t\(.changed_files // "" | tostring)"' || true)"
	base_ref="${sync_only_pull_line%%$'\t'*}"
	if [[ "${sync_only_pull_line}" == *$'\t'* ]]; then
		sync_only_pr_changed_files="${sync_only_pull_line#*$'\t'}"
	fi
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
	ci_final_merge_ref_compute_pin "${base_ref}" "${tip}" "${merge_depth}"
	pin="${CI_FINAL_MERGE_REF_PIN}"
	if [ -n "${pin}" ]; then
		pin_note="pin=${pin} pin_source=${CI_FINAL_MERGE_REF_PIN_SOURCE}"
	else
		pin_note="pin=none pin_reason=${CI_FINAL_MERGE_REF_PIN_REASON:-unknown}"
		echo "CI_FINAL_MERGE_REF: could not pin the run's base (${CI_FINAL_MERGE_REF_PIN_REASON:-unknown}); this job tests the live tip of ${base_ref}."
	fi
	merge_into="${pin:-${tip}}"
	if [ "${first}" = "${merge_into}" ]; then
		head_tree="$(git rev-parse 'HEAD^{tree}' 2>/dev/null || echo none)"
		if [ "${first}" = "${tip}" ]; then
			ci_final_merge_ref_log fresh base_tip "base=${base_ref} tested_base=${first} tree=${head_tree} ${pin_note}"
		else
			ci_final_merge_ref_log fresh pinned_base "base=${base_ref} tested_base=${first} tip=${tip} tree=${head_tree} ${pin_note}"
		fi
		return 0
	fi
	if [ "${merge_into}" = "${tip}" ]; then
		merge_reason="not_refreshed"
		echo "CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge merges ${GITHUB_SHA} into ${first}, but ${base_ref} is at ${tip}; asking GitHub to recompute the merge ref."
		seen_tips=" ${tip} "
		for (( attempt = 1; attempt <= attempts; attempt++ )); do
			mergeable="$(ci_final_merge_ref_pull '.mergeable | tostring' || true)"
			[ -n "${mergeable}" ] || mergeable="unknown"
			if [ "${mergeable}" = "false" ] && [ -n "${pin}" ]; then
				# The cached tip predates GitHub's answer (on attempt 1 it was
				# read before the pin): re-read it, so a conflict GitHub found
				# with a tip that moved past the pin is recognised as such.
				fresh_tip="$(ci_final_merge_ref_tip "${base_ref}")"
				if [[ "${fresh_tip}" =~ ^[0-9a-f]{40}$ ]]; then
					tip="${fresh_tip}"
					seen_tips+="${tip} "
				else
					# Unknown whether GitHub judged the pin or a newer tip: the
					# local merge into the pin decides (a real conflict with the
					# pin still fails as merge_conflict).
					echo "CI_FINAL_MERGE_REF: GitHub reports a conflict but the tip of ${base_ref} could not be re-read; merging into this run's base ${pin} locally to decide."
					deferred_to_pin=true
					break
				fi
			fi
			if [ "${mergeable}" = "false" ] && [ -n "${pin}" ] && [ "${tip}" != "${pin}" ]; then
				# GitHub judged a newer tip than the run's pin: let the local
				# merge into the pin decide, as the run's other jobs do.
				echo "CI_FINAL_MERGE_REF: GitHub reports a conflict with ${base_ref} at ${tip}, past this run's base ${pin}; merging into the pin locally."
				deferred_to_pin=true
				break
			fi
			if [ "${mergeable}" = "false" ]; then
				echo "::error::CI_FINAL_MERGE_REF: PR #${FINAL_CI_PR_NUMBER} no longer merges cleanly into ${base_ref} (${tip}); GitHub cannot refresh refs/pull/${FINAL_CI_PR_NUMBER}/merge. Resolve the conflict with the base; this is not a test failure."
				ci_final_merge_ref_log conflict not_mergeable "base=${base_ref} tested_base=${first} tip=${tip} ${pin_note}"
				return 1
			fi
			sleep "${sleep_s}"
			fetched=false
			if git fetch --no-tags --quiet --depth="${depth}" origin "+refs/pull/${FINAL_CI_PR_NUMBER}/merge:refs/remotes/pull/${FINAL_CI_PR_NUMBER}/merge" 2>/dev/null; then
				fetched=true
				CI_FINAL_MERGE_REF_FETCHED=true
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
			# With a pin, only a merge into the pin keeps the run's jobs on one
			# base; without one, any tip seen during this step is current.
			if [[ "${first:-}" =~ ^[0-9a-f]{40}$ ]] && { [ "${first}" = "${pin:-}" ] || { [ -z "${pin}" ] && [[ "${seen_tips}" == *" ${first} "* ]]; }; }; then
				if git checkout --quiet --detach --force "refs/remotes/pull/${FINAL_CI_PR_NUMBER}/merge"; then
					head_tree="$(git rev-parse 'HEAD^{tree}' 2>/dev/null || echo none)"
					ci_final_merge_ref_log refreshed base_tip "base=${base_ref} tested_base=${first} attempt=${attempt} tree=${head_tree} ${pin_note}"
					return 0
				fi
				echo "::warning::CI_FINAL_MERGE_REF: could not check out the refreshed merge ref; retrying."
			fi
		done
		if [ "${deferred_to_pin}" != "true" ] && [ "${CI_FINAL_MERGE_REF_FETCHED}" != "true" ]; then
			# No fetch worked (e.g. a private repository and a checkout without
			# persisted credentials): staleness is known but cannot be fixed
			# here, so warn and test the checkout rather than fail a run that
			# may pass.
			echo "::warning::CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge merges into ${first}, not the tip ${tip:-unknown} of ${base_ref}, and could not be re-fetched; testing the checked-out merge ref."
			ci_final_merge_ref_log skipped fetch_unavailable "base=${base_ref} tested_base=${first} tip=${tip:-none}"
			return 0
		fi
		# Without a pin, merge into the newest tip read (the attempts re-read it).
		if [ -z "${pin}" ] && [[ "${tip}" =~ ^[0-9a-f]{40}$ ]]; then
			merge_into="${tip}"
		fi
		if [ "${deferred_to_pin}" != "true" ]; then
			echo "CI_FINAL_MERGE_REF: GitHub did not recompute refs/pull/${FINAL_CI_PR_NUMBER}/merge after ${attempts} attempt(s) (it still merges into ${first:-unknown}); merging ${GITHUB_SHA} into ${merge_into} locally."
		fi
	else
		merge_reason="pinned_base"
		echo "CI_FINAL_MERGE_REF: this run's base is ${pin}, the tip of ${base_ref} when the run was created; ${base_ref} is now at ${tip} and refs/pull/${FINAL_CI_PR_NUMBER}/merge merges into ${first}, so the job merges ${GITHUB_SHA} into ${pin} locally."
	fi
	ci_final_merge_ref_merge_locally "${merge_into}" "${merge_depth}" || local_rc=$?
	if [ "${local_rc}" -eq 0 ]; then
		ci_final_merge_ref_log merged_locally "${merge_reason}" "base=${base_ref} tested_base=${merge_into} tip=${tip:-none} merge=${CI_FINAL_MERGE_REF_LOCAL_COMMIT} tree=${CI_FINAL_MERGE_REF_LOCAL_TREE} ${pin_note}"
		return 0
	fi
	if [ "${local_rc}" -eq 1 ]; then
		echo "::error::CI_FINAL_MERGE_REF: PR #${FINAL_CI_PR_NUMBER} (${GITHUB_SHA}) does not merge cleanly into ${base_ref} at ${merge_into}. Resolve the conflict with the base; this is not a test failure."
		ci_final_merge_ref_log conflict merge_conflict "base=${base_ref} tested_base=${merge_into} tip=${tip:-none} ${pin_note}"
		return 1
	fi
	if [ "${CI_FINAL_MERGE_REF_LOCAL_REASON}" = "merge_fetch_failed" ] && [ "${CI_FINAL_MERGE_REF_FETCHED}" != "true" ]; then
		echo "::warning::CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge merges into ${first}, not ${merge_into}, and nothing could be fetched; testing the checked-out merge ref."
		ci_final_merge_ref_log skipped fetch_unavailable "base=${base_ref} tested_base=${first} tip=${tip:-none}"
		return 0
	fi
	sync_only_switch="$(printf '%s' "${CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${sync_only_switch}" in
		true|1|yes|on)
			if ci_final_merge_ref_sync_only "${sync_only_tested_base}" "${tip:-}" "${base_ref}" "${sync_only_pr_changed_files}" "${sync_only_checked_out}"; then
				echo "CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge is stale, but ${base_ref} moved from ${sync_only_tested_base} to ${CI_FINAL_MERGE_REF_SYNC_ONLY_TIP} only by syncs of the default branch and [ai-merge-resolve] commits that touch none of the PR's files; testing the checked-out merge."
				ci_final_merge_ref_log stale_accepted sync_only_no_overlap "base=${base_ref} tested_base=${sync_only_tested_base} tip=${CI_FINAL_MERGE_REF_SYNC_ONLY_TIP} commits=${CI_FINAL_MERGE_REF_SYNC_ONLY_COMMITS} files=${CI_FINAL_MERGE_REF_SYNC_ONLY_FILES} ${pin_note}"
				if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
					echo "Final CI tested base \`${sync_only_tested_base}\` of \`${base_ref}\`: the ${CI_FINAL_MERGE_REF_SYNC_ONLY_COMMITS} sync commit(s) up to \`${CI_FINAL_MERGE_REF_SYNC_ONLY_TIP}\` touch none of the PR's files (CI_FINAL_MERGE_REF outcome=stale_accepted)." >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
				fi
				return 0
			fi
			sync_only_detail_note=" detail=${CI_FINAL_MERGE_REF_SYNC_ONLY_DETAIL:-api_error}"
			;;
	esac
	if [ "${CI_FINAL_MERGE_REF_LOCAL_REASON}" = "merge_tree_unsupported" ]; then
		echo "::error::CI_FINAL_MERGE_REF: the runner's git does not support --attr-source (git 2.41 or newer is required), so the job could not build the merge of ${GITHUB_SHA} into ${merge_into} (${base_ref}) itself. This is a runner problem, not a defect in the PR."
	else
		echo "::error::CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge merges into ${first:-unknown}, not ${merge_into} (${base_ref}), and the job could not build the merge itself (${CI_FINAL_MERGE_REF_LOCAL_REASON:-unknown}). This run would test an old base, so it stops here; this is not a defect in the PR. The review sweep updates a head that is behind its base, and the next final run tests the current base."
	fi
	ci_final_merge_ref_log stale "${CI_FINAL_MERGE_REF_LOCAL_REASON:-merge_failed}" "base=${base_ref} tested_base=${first:-none} tip=${tip:-none} ${pin_note}${sync_only_detail_note}"
	return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	ci_final_merge_ref_main "$@"
fi
