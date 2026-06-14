#!/usr/bin/env bash
# review_enable_auto_merge.sh — enable or suppress PR auto-merge in
# review_autofix.yml.
#
# Extracted from the workflow's "Enable auto-merge on PR" step so the
# policy stays local to one helper while preserving the existing env/log
# contract.
#
# Inputs (environment):
#   GITHUB_REPOSITORY
#   PR_NUMBER
#   ENABLE_AUTO_MERGE
#   FORWARD_MERGE_FALLBACK_AUTO_MERGE
#   ORCH_INTEGRATION_BRANCH_PATTERN
#   GH_TOKEN

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true

type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

if [ "${ENABLE_AUTO_MERGE}" != "true" ]; then
	echo "Auto-merge disabled (set ENABLE_AUTO_MERGE=true to enable)."
	exit 0
fi

# Scoped opt-out for the e2e smoke test. The implement workflow
# tags PRs born from `[E2E Smoke Test]` issues with the
# `e2e-smoke-test` label. Without this guard the editor's
# bait-removal commit would race the e2e gate's verify-bait-
# removed step: auto-merge could fire the moment the editor
# commit lands, closing the PR before the e2e gate has read
# the post-edit canary blob. Repo-wide ENABLE_AUTO_MERGE stays
# untouched so non-e2e PRs keep auto-merging as before.
#
# Fail-closed on API failure: if gh_retry exhausts its
# retries, we cannot tell whether the PR carries
# `e2e-smoke-test`. Defaulting to "no label → auto-merge"
# would silently bypass this guard for an e2e PR (consensus
# finding from the multi-reviewer claude-branch-review on
# this PR — when a transient API blip persists past 5
# retries, auto-merge would race the e2e gate again, the
# exact regression this guard was added to prevent). Skip
# auto-merge with a clear ::warning:: instead — non-e2e PRs
# lose auto-merge for one review_autofix cycle on a
# persistent outage, but the next sync event (next push,
# next reviewer commit) re-runs review_autofix and picks it
# up. This trade is asymmetric in our favour: a missed
# auto-merge on a non-e2e PR is recoverable; an e2e PR that
# auto-merges mid-test is not.
# `--paginate ?per_page=100` so a PR carrying many labels
# (e.g., orchestrator-managed PRs that accumulate phase
# labels + review-blocked + e2e-smoke-test, easily >30) can
# still surface `e2e-smoke-test` on page ≥2. Without the
# override this endpoint defaults to 30/page and a label on
# page 2 is silently dropped, allowing auto-merge — the
# exact race this guard prevents (Copilot review #3 on
# PR #1823). With `--paginate`, gh emits one JSON array per
# page; `--jq '.[].name'` flattens to one label per line so
# the downstream `grep -qx` works regardless of page count.
_label_err_file="$(mktemp 2>/dev/null || echo /dev/null)"
if PR_LABELS_RAW="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" --jq '.[].name' 2>"${_label_err_file}")"; then
	[ "${_label_err_file}" = /dev/null ] || rm -f "${_label_err_file}"
	if printf '%s\n' "${PR_LABELS_RAW}" | grep -qx 'e2e-smoke-test'; then
		echo "PR #${PR_NUMBER} carries 'e2e-smoke-test' label — auto-merge suppressed for the e2e gate's lifecycle."
		exit 0
	fi
else
	_label_err="$(cat "${_label_err_file}" 2>/dev/null || true)"
	[ "${_label_err_file}" = /dev/null ] || rm -f "${_label_err_file}"
	echo "::warning::Could not fetch labels for PR #${PR_NUMBER} (gh_retry exhausted): ${_label_err}. Failing closed: skipping auto-merge enablement so an e2e-smoke-test PR cannot race the e2e gate. Next sync event will re-run review_autofix and retry the merge enablement."
	exit 0
fi

# Scoped opt-out for orchestrator integration PRs (head ref matches
# ORCH_INTEGRATION_BRANCH_PATTERN; default `^orchestrator/project-`).
# The canonical `orchestrator/project-<N>` form still gets the
# tracking-number body cross-check below, but any configured
# integration-branch naming must suppress auto-merge too so the
# safeguard remains effective for customised repos.
#
# The orchestrator's
# integration-conflict self-healing path dispatches review_autofix
# on the eager final PR to resolve drift (see
# scripts/orchestrate_poll_process.sh: _dispatch_review_for_conflicts);
# without this guard, the auto-merge step would ship the integration
# branch partway through the project as soon as Copilot review
# passes — stranding subsequent wave PRs on the integration branch
# with no path to default. The orchestrator's
# mark_validation_complete → finalize_integration_merge_if_needed
# owns the legitimate final merge via a synchronous `gh pr merge
# --squash --delete-branch`, so auto-merge is never required for
# these PRs. See shubhodeep1/binance-blessings#135 for the
# regression case.
#
# PR body cross-check (defense in depth): when the head-ref regex
# matches we first look for a standalone `Refs #<N>` / `Refs: #<N>`
# line in the PR body and then fall back to a standalone
# `Closes/Fixes/Resolves #<N>` / `Closes/Fixes/Resolves: #<N>` line
# before logging a mismatch
# warning if the tracking number disagrees with the head ref — an
# operational signal that something unexpected created an
# orchestrator-shaped head ref. Auto-merge is still suppressed
# whenever the head-ref pattern matches (head-ref evidence is
# sufficient on its own; the body check is informational).
#
# Fail-closed on PR metadata fetch failure: skip auto-merge so a
# transient API blip cannot inadvertently ship an orchestrator
# integration PR. Mirrors the e2e-smoke-test guard above.
_orch_pr_meta_err_file="$(mktemp 2>/dev/null || true)"
_orch_pr_meta_err=""
if [ -z "${_orch_pr_meta_err_file}" ]; then
	# gh_retry logs transient retry diagnostics to stderr before a
	# later attempt can succeed, so the mktemp-failed fallback still
	# needs a separate stderr sink. Merging stderr into stdout would
	# pollute the JSON response with retry warnings and spuriously
	# trip the fail-closed parse path.
	_orch_pr_meta_err_file="${RUNNER_TEMP:-${GITHUB_WORKSPACE:-$PWD}}/review-autofix-pr-meta-${PR_NUMBER}-$$.err"
	if ! : > "${_orch_pr_meta_err_file}" 2>/dev/null; then
		echo "::warning::Could not create a stderr capture file for PR metadata lookup on #${PR_NUMBER} (mktemp failed and fallback path '${_orch_pr_meta_err_file}' was unwritable). Failing closed: skipping auto-merge enablement so an orchestrator integration PR cannot be inadvertently shipped via auto-merge on a transient runner filesystem issue. Next sync event will re-run review_autofix and retry."
		exit 0
	fi
fi
if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" 2>"${_orch_pr_meta_err_file}")"; then
	_orch_pr_meta_err="$(cat "${_orch_pr_meta_err_file}" 2>/dev/null || true)"
	rm -f "${_orch_pr_meta_err_file}"
	echo "::warning::Could not fetch PR metadata for #${PR_NUMBER} (gh_retry exhausted): ${_orch_pr_meta_err:-gh_retry error details unavailable}. Failing closed: skipping auto-merge enablement so an orchestrator integration PR cannot be inadvertently shipped via auto-merge on a transient API blip. Next sync event will re-run review_autofix and retry."
	exit 0
else
	rm -f "${_orch_pr_meta_err_file}"
fi

_orch_pr_head_ref="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.head.ref // ""' 2>/dev/null || echo "")"
if [ -z "${_orch_pr_head_ref}" ]; then
	echo "::warning::Could not determine PR head ref for #${PR_NUMBER} from fetched metadata (empty/null .head.ref). Failing closed: skipping auto-merge enablement so an orchestrator integration PR cannot be inadvertently shipped when PR metadata is incomplete or the head branch was deleted. Next sync event will re-run review_autofix and retry."
	exit 0
fi

# Scoped opt-out for forward-merge fallback PRs opened by
# forward-merge-stable-to-main.yml — these are routed AWAY from the
# `--squash --auto` tail below. Head ref is hard-coded as
# `auto/forward-merge-stable-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}`
# at forward-merge-stable-to-main.yml:255. These PRs MUST land as a
# "Create a merge commit" so the 2-parent merge keeps stable's tip
# reachable from main; the squash auto-merge would silently strip
# stable's commits from main's ancestry, after which
# promote-main-to-stable.yml's pre-flight
# `git merge-base --is-ancestor HEAD origin/main` refuses to
# fast-forward stable on the next promote run, with the exact
# "squash/rebase strips ancestry" error at
# promote-main-to-stable.yml:122. By default
# (FORWARD_MERGE_FALLBACK_AUTO_MERGE='true') we therefore call
# `gh pr merge --merge --auto` — the unattended equivalent of the
# manual "Create a merge commit" the PR-body CAUTION banner
# (forward-merge-stable-to-main.yml:265-270) asks for, which
# preserves ancestry. Set the var to any non-'true' value to suppress
# auto-merge entirely and leave the PR for a manual merge commit. The
# head-ref pattern is hard-coded (not configurable) because the branch
# prefix is owned by forward-merge-stable-to-main.yml and never varies
# per repo; only the auto-merge-vs-manual choice is var-gated.
if printf '%s\n' "${_orch_pr_head_ref}" | grep -Eq '^auto/forward-merge-stable-'; then
	# forward-merge fallback PRs (opened by forward-merge-stable-to-main.yml)
	# MUST land as a real 2-parent merge commit so stable's commits stay
	# reachable from main. A squash/rebase strips stable's ancestry even
	# though the file content is forwarded, which then trips
	# promote-main-to-stable.yml's pre-flight
	# `git merge-base --is-ancestor HEAD origin/main` check
	# (promote-main-to-stable.yml:115-126). `gh pr merge --merge --auto`
	# is the unattended equivalent of the manual "Create a merge commit"
	# the PR-body CAUTION banner asks for, so it preserves ancestry and
	# satisfies that check — unlike the `--squash --auto` tail below.
	#
	# Gated by FORWARD_MERGE_FALLBACK_AUTO_MERGE (default 'true'); set the
	# repo var to any non-'true' value to fall back to the previous
	# behaviour of leaving these PRs for a manual merge commit.
	if [ "${FORWARD_MERGE_FALLBACK_AUTO_MERGE}" = "true" ]; then
		echo "Enabling auto-merge (merge commit) on forward-merge fallback PR #${PR_NUMBER} (head ref '${_orch_pr_head_ref}')..."
		if gh_retry gh pr merge "${PR_NUMBER}" --repo "${GITHUB_REPOSITORY}" --merge --auto; then
			echo "Auto-merge (merge commit) enabled. PR will merge once all required checks pass, preserving stable's ancestry on main."
		else
			echo "::warning::Could not enable auto-merge (merge commit) on forward-merge fallback PR #${PR_NUMBER}. Check that 'Allow merge commits' and 'Allow auto-merge' are enabled in repo settings and branch protection is configured. The PR remains open for manual 'Create a merge commit'."
		fi
	else
		echo "PR #${PR_NUMBER} head ref '${_orch_pr_head_ref}' matches forward-merge fallback pattern '^auto/forward-merge-stable-' and FORWARD_MERGE_FALLBACK_AUTO_MERGE != 'true' — auto-merge suppressed. Merge manually via 'Create a merge commit' (NOT squash/rebase) so stable's commits remain in main's ancestry; promote-main-to-stable.yml's pre-flight 'git merge-base --is-ancestor HEAD origin/main' check refuses otherwise (see promote-main-to-stable.yml:115-126 and the CAUTION banner in the PR body)."
	fi
	exit 0
fi

_orch_pr_body="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.body // ""' 2>/dev/null || echo "")"
_orch_is_integration_pr="false"
if [ -n "${ORCH_INTEGRATION_BRANCH_PATTERN}" ]; then
	if printf '%s\n' "${_orch_pr_head_ref}" | grep -Eq -- "${ORCH_INTEGRATION_BRANCH_PATTERN}"; then
		_orch_is_integration_pr="true"
	else
		_orch_pattern_match_rc=$?
		if [ "${_orch_pattern_match_rc}" -gt 1 ]; then
			echo "::warning::ORCH_INTEGRATION_BRANCH_PATTERN is not a valid POSIX ERE (grep exit ${_orch_pattern_match_rc}); falling back to canonical '^orchestrator/project-([0-9]+)$' auto-merge suppressor for this run."
		fi
	fi
else
	echo "::warning::ORCH_INTEGRATION_BRANCH_PATTERN is empty; falling back to canonical '^orchestrator/project-([0-9]+)$' auto-merge suppressor for this run."
fi
if [ "${_orch_is_integration_pr}" != "true" ] && [[ "${_orch_pr_head_ref}" =~ ^orchestrator/project-([0-9]+)$ ]]; then
	_orch_is_integration_pr="true"
fi
if [ "${_orch_is_integration_pr}" = "true" ]; then
	if [[ "${_orch_pr_head_ref}" =~ ^orchestrator/project-([0-9]+)$ ]]; then
		_orch_ref_tracking_num="${BASH_REMATCH[1]}"
		_orch_body_tracking_num="$(printf '%s' "${_orch_pr_body}" | grep -im1 -oE '^[[:space:]]*refs:?[[:space:]]*#[0-9]+' | grep -oE '[0-9]+' || echo "")"
		if [ -z "${_orch_body_tracking_num}" ]; then
			_orch_body_tracking_num="$(printf '%s' "${_orch_pr_body}" | grep -im1 -oE '^[[:space:]]*(closes|fixes|resolves):?[[:space:]]*#[0-9]+' | grep -oE '[0-9]+' || echo "")"
		fi
		if [ -n "${_orch_body_tracking_num}" ] && [ "${_orch_body_tracking_num}" != "${_orch_ref_tracking_num}" ]; then
			echo "::warning::PR #${PR_NUMBER} head ref '${_orch_pr_head_ref}' implies orchestrator tracking issue #${_orch_ref_tracking_num} but the PR body references #${_orch_body_tracking_num}. Suppressing auto-merge anyway — head-ref pattern matches an orchestrator integration PR; the orchestrator will land this via finalize_integration_merge_if_needed when the project is genuinely complete."
		else
			echo "PR #${PR_NUMBER} is the orchestrator integration PR for tracking #${_orch_ref_tracking_num} (head ref '${_orch_pr_head_ref}') — auto-merge suppressed. finalize_integration_merge_if_needed handles the legitimate final merge synchronously once the project is complete and the integration tip is contained in the default branch."
		fi
	else
		echo "PR #${PR_NUMBER} head ref '${_orch_pr_head_ref}' matches ORCH_INTEGRATION_BRANCH_PATTERN='${ORCH_INTEGRATION_BRANCH_PATTERN}' — auto-merge suppressed. finalize_integration_merge_if_needed handles the legitimate final merge synchronously once the project is complete and the integration tip is contained in the default branch."
	fi
	exit 0
fi

echo "Enabling auto-merge (squash) on PR #${PR_NUMBER}..."
if gh_retry gh pr merge "${PR_NUMBER}" --repo "${GITHUB_REPOSITORY}" --squash --auto; then
	echo "Auto-merge enabled. PR will merge once all required checks pass."
else
	echo "::warning::Could not enable auto-merge on PR #${PR_NUMBER}. Check that 'Allow auto-merge' is enabled in repo settings and branch protection is configured."
fi
