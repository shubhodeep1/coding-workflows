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
# knobs above, CI_FINAL_MERGE_REF_MERGE_DEPTH (default 200).
# API budget (§15): one `GET pulls/<n>` for the base branch name (the job's
# shallow-checkout step reads the same PR later for its own exports; the
# check must run before that step, so it cannot reuse that read), one
# `GET actions/runs/<id>` for the pin only when the event payload carries no
# `repository.pushed_at` (no earlier step of the job reads the run), plus one `GET pulls/<n>` per refresh attempt, only when the merge ref
# is stale and the pin is the live tip.
# Log: `CI_FINAL_MERGE_REF pr=<n> head=<sha> outcome=fresh|refreshed|merged_locally|stale|conflict|skipped reason=<token> [base=<ref>] [tested_base=<sha>] [tip=<sha>] [attempt=<n>] [merge=<sha>] [tree=<sha>] [pin=<sha>|pin=none pin_reason=<token>]`.

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

ci_final_merge_ref_main()
{
	local depth=1 switch attempts sleep_s base_ref tip first second seen_tips attempt mergeable fetched
	local merge_depth pin pin_note merge_into merge_reason local_rc=0 head_tree fresh_tip deferred_to_pin=false
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
	base_ref="$(ci_final_merge_ref_pull '.base.ref // ""' || true)"
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
	if [ "${CI_FINAL_MERGE_REF_LOCAL_REASON}" = "merge_tree_unsupported" ]; then
		echo "::error::CI_FINAL_MERGE_REF: the runner's git does not support --attr-source (git 2.41 or newer is required), so the job could not build the merge of ${GITHUB_SHA} into ${merge_into} (${base_ref}) itself. This is a runner problem, not a defect in the PR."
	else
		echo "::error::CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge merges into ${first:-unknown}, not ${merge_into} (${base_ref}), and the job could not build the merge itself (${CI_FINAL_MERGE_REF_LOCAL_REASON:-unknown}). This run would test an old base, so it stops here; this is not a defect in the PR. The review sweep updates a head that is behind its base, and the next final run tests the current base."
	fi
	ci_final_merge_ref_log stale "${CI_FINAL_MERGE_REF_LOCAL_REASON:-merge_failed}" "base=${base_ref} tested_base=${first:-none} tip=${tip:-none} ${pin_note}"
	return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	ci_final_merge_ref_main "$@"
fi
