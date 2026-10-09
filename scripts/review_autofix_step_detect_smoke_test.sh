#!/usr/bin/env bash
# Body of the "Detect smoke test and tune LLM settings" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail
# The step passes github.repository as SMOKE_DETECT_REPOSITORY; fall back to
# the runner's GITHUB_REPOSITORY when a caller does not set it.
SMOKE_DETECT_REPOSITORY="${SMOKE_DETECT_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
PR_TITLE=$(jq -r '.title // ""' "${PR_PAYLOAD_FILE}")
PR_BODY=$(jq -r '.body // ""' "${PR_PAYLOAD_FILE}")

# PR_TITLE is retained deliberately. Free-text fixture-tag
# matching on the PR title/body was removed (see below), so
# nothing in this step reads it any more, but it is an existing
# identifier and CLAUDE.md §6 forbids removing one without the
# ask flow. PR_BODY is still used by the linked-issue lookup.

IS_SMOKE=false

# Primary signal: the `e2e-smoke-test` label.
#
# This replaces a free-text scan of the PR title and body for a
# `[E2E <variant>]` fixture tag. That scan misclassified any real
# PR that merely *discussed* the fixture tags -- documentation
# changes, and changes to this detection logic itself. The
# consequences were not cosmetic: a misclassified PR had its
# reviewer/editor reasoning pinned, its review alerts silenced,
# and IS_SMOKE_TEST exported, which makes
# scripts/review_apply_fixes.sh append a "must call apply_patch
# on tests/e2e_smoke_canary.txt" directive to the editor prompt
# -- i.e. it could push the autofix editor to touch the canary
# file on an unrelated PR.
#
# The label is a reliable primary because implement.yml applies
# it atomically at `gh pr create` (see its "Atomically apply
# force-review + e2e-smoke-test labels at PR creation time"
# comment), specifically so it is already in the
# `pull_request: opened` payload this gate snapshots -- a label
# added post-creation cannot rewind the gate decision. Confirmed
# on release run 35672590166: PR #4252 (from the
# `[E2E Smoke Test]` canary fixture) and PR #4253 (from the
# `[E2E Smoke Test alt-model]` fixture) both carried
# `force-review` + `e2e-smoke-test`.
if jq -e '[.labels[]?.name] | index("e2e-smoke-test") != null' "${PR_PAYLOAD_FILE}" >/dev/null 2>&1; then
  IS_SMOKE=true
fi

# Fallback: the linked issue's title. Kept as a second signal so
# a smoke PR whose label somehow did not land is still detected,
# and safe to keep because it is anchored and resolves a real
# issue rather than scanning prose -- a documentation PR's linked
# issue title does not begin with the fixture tag.
#
# Only honour GitHub's closing-keyword
# pattern (Closes/Fixes/Resolves + #N or issue URL, case-insensitive)
# to avoid false-matching loose "issue #N" mentions inside log
# citations or backticked fragments quoted in the PR body — which
# would otherwise trigger phantom `gh api issues/<N>` 404s. The
# implement and orchestrator workflows both emit closing keywords
# when they create PRs, so this still resolves the linked issue
# for every auto-generated smoke-test PR.
# The URL alternative is restricted to the current repository so a
# cross-repo "Closes <other-repo>/issues/N" reference cannot route
# an unrelated number into this repo's issues lookup.
if [ "$IS_SMOKE" = "false" ]; then
  ISSUE_NUM=$(echo "${PR_BODY}" | grep -oiPm1 '\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?):?\s+(?:https?://[^[:space:]]+/'"${SMOKE_DETECT_REPOSITORY}"'/issues/|#)\K\d+\b' || true)
  if [ -n "${ISSUE_NUM:-}" ]; then
    ISSUE_TITLE=$(_safe_gh_jq "repos/${SMOKE_DETECT_REPOSITORY}/issues/${ISSUE_NUM}" --jq '.title // ""' || echo "")
    # Anchored, matching the `^\[E2E ` convention used by
    # clarify.yml, plan.yml and implement.yml: real AI issue
    # titles never begin with the fixture tag, so this cannot
    # fire on a production issue that merely quotes one.
    if echo "${ISSUE_TITLE}" | grep -qiE '^\[E2E '; then
      IS_SMOKE=true
    fi
  fi
fi

if [ "$IS_SMOKE" = "true" ]; then
  # Reasoning overrides for smoke runs.
  # Reviewers are pinned to low; editor is pinned to medium.
  #
  # History of reasoning levels on smoke runs (editor). The runs
  # cited below predate the gpt-5.4 cutover and were observed on
  # the legacy editor default; the rationale (medium beats
  # extremes for execution-heavy editing tasks) still stands per
  # the gpt-5.4 prompt guide:
  #   low  → run 25254574828: OSCILLATION GUARD activated because the
  #           bait commit appeared in LAST_RUN_DIFF (since fixed at
  #           L2249-2260 to only walk [ai-autofix] commits — bait
  #           commits never match, so the guard no longer fires).
  #   xhigh → run 25249170035 / PR #1982: budget burns on tool calls +
  #           reasoning summaries, editor exits rc=0 with 0 file changes
  #           (6 attempts, all empty, ~81k tokens on attempt 1).
  #   none  → run 25305535590: the legacy editor default at reasoning=none runs one
  #           read tool call, exits cleanly rc=0 with empty stdout, and
  #           the in-step retry loop bails after MAX_ATTEMPTS with no
  #           edits (same failure mode as the conflict resolver at none,
  #           documented at the "Resolve merge conflicts" step below).
  #           Reviewer manifest validation then fails because the editor
  #           lists aggregate files instead of individual reviewer files.
  #   low   → run 25308327160 (gate run 25308071039): the legacy editor default at
  #           reasoning=low also exits rc=0 with empty stdout on all
  #           3 attempts per round (same failure mode as none — pattern
  #           extends across both none and low for this model+task).
  #           "low is sufficient" assumption was wrong.
  # medium is the only untried level; reviewers stay at low (all 6
  # succeeded on attempt 1 at low in run 25308327160). LAST_RUN_DIFF
  # already excludes bait commits so the oscillation guard cannot
  # fire regardless of editor reasoning level.
  # Non-smoke PRs continue to use the workflow defaults
  # (THINKING_LEVEL_REVIEWER / THINKING_LEVEL_EDITOR).
  echo "REVIEWER_REASONING_EFFORT=low" >> "$GITHUB_ENV"
  echo "EDITOR_REASONING_EFFORT=medium" >> "$GITHUB_ENV"
  echo "🔬 Smoke test PR detected — reviewer reasoning=low, editor reasoning=medium (Phase 4b fix)"
  # Suppress per-phase Telegram alerts; the test-and-mark-stable
  # gate emits a single combined pass/fail notification at the
  # end via raw curl. SILENT > CRITICAL so all levels filter.
  echo "ALERT_MSG_LEVEL=SILENT" >> "$GITHUB_ENV"
  # Surface the smoke-test signal to scripts/review_apply_fixes.sh
  # so the editor prompt can append a short, explicit
  # "must call apply_patch on tests/e2e_smoke_canary.txt"
  # directive. The repeated empty-output failures (runs
  # 25305535590 / 25308327160 / 25310399716) all share the
  # signature: codex reads the file, decides the change is
  # trivial, and exits with 0-byte stdout because nothing in
  # the production prompt forces an apply_patch invocation
  # on a single-line removal. Only the smoke fixture sets this
  # env var; real PR autofixes never see the smoke override
  # and continue under the production prompt unchanged.
  echo "IS_SMOKE_TEST=true" >> "$GITHUB_ENV"
else
  echo "Standard PR — using default LLM settings"
fi
