#!/usr/bin/env bash
# Body of the "Prepare Claude-fixer judge" step in
# .github/workflows/review_autofix.yml. The step sources this file in its own
# shell, so the step's if:, env: and continue-on-error: stay in the workflow;
# edit those there.
#
# Claude-fixer GPT judge (CLAUDE_FIXER_JUDGE_ENABLED, default true): when the
# Claude session that owns a `claude/*` PR rejects every finding of a review
# round, it posts its finding-by-finding reasons in one comment ending
#   <!-- ai:claude-fixer-rejection:v1 head=<sha> round=<r> -->
# and dispatches this workflow with `claude_fixer_judge_head=<sha>`. The gate
# accepted that dispatch for the head's newest findings hand-off (round, v2
# ledger digest, and the hand-off's run id, which is only a pointer), so the
# reviewers are skipped and "Review-blocked judge decision" runs
# scripts/review_rb_judge.sh in its Claude mode. This step collects the judge's
# inputs into CLAUDE_FIXER_JUDGE_INPUTS_DIR (default
# ${RUNTIME_DIR}/claude_fixer_judge):
#
#   * ledger.txt — the reviewer ledger, read from the hand-off run's
#     `claude-fixer-evidence-*` artifact after
#     scripts/review_claude_fixer_evidence.py verify accepted the run (outcome
#     findings, same PR, head and round, ledger digest equal to the hand-off's
#     v2 digest). Never from a comment;
#   * findings.json — the ledger's findings numbered F1..Fn
#     (scripts/review_claude_fixer_judge.py findings);
#   * rejection.txt — the newest rejection comment for this head and round by
#     an OWNER / MEMBER / COLLABORATOR posting as the PR author or this
#     workflow's GH_PAT account, passed to the model as untrusted
#     argument only. Without one the inputs are not ready (reason
#     rejection_missing, security follow-up #6061): a dispatch alone never
#     authorizes the judge;
#   * prior_rulings.json — rulings of earlier judge runs on this PR, from
#     verified judge evidence (scripts/review_claude_fixer_judge.py
#     prior-rulings, newest 3 runs).
#
# and exports CLAUDE_FIXER_JUDGE_READY=true. Unverified evidence exports
# CLAUDE_FIXER_JUDGE_READY=false with CLAUDE_FIXER_JUDGE_SKIP_REASON, and the
# judge then decides nothing (the PR is labelled ai:review-blocked). So does a
# ledger with no findings (reason no_findings): such a hand-off comes from a
# round that fell below the reviewer panel floor, and a judge that "merges"
# it would merge a head too few reviewers looked at; only a re-run of the
# reviewers can clear it.
#
# Inputs (environment): PR_NUMBER, GH_TOKEN, GITHUB_REPOSITORY, HEAD_SHA,
# HEAD_REF, DEFAULT_BRANCH, CLAUDE_FIXER_JUDGE_RUN_ID, CLAUDE_FIXER_JUDGE_ROUND,
# CLAUDE_FIXER_JUDGE_LEDGER_SHA256, PR_ISSUE_COMMENTS_FILE (the comments the
# "Collect PR metadata" step already fetched), SUPPORT_SCRIPTS_DIR,
# RUNTIME_DIR, GITHUB_ENV.
# API calls: the hand-off run's evidence verification (3 REST reads, plus one
# paginated PR files read when that run came from the PR head branch) and
# 3 reads per prior judge run (at most 3 runs). No comment is fetched again.
set -euo pipefail

claude_fixer_judge_dir="${CLAUDE_FIXER_JUDGE_INPUTS_DIR:-${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_judge}"
mkdir -p "${claude_fixer_judge_dir}"
claude_fixer_judge_round="${CLAUDE_FIXER_JUDGE_ROUND:-}"

claude_fixer_judge_not_ready()
{
  echo "CLAUDE_FIXER_JUDGE pr=${PR_NUMBER:-} head=${HEAD_SHA:-} round=${claude_fixer_judge_round:-} action=not_ready reason=$1"
  {
    echo "CLAUDE_FIXER_JUDGE_READY=false"
    echo "CLAUDE_FIXER_JUDGE_SKIP_REASON=$1"
  } >> "${GITHUB_ENV}"
  exit 0
}

if ! [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] || ! [[ "${HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]] \
  || ! [[ "${claude_fixer_judge_round}" =~ ^[1-9][0-9]*$ ]] \
  || ! [[ "${CLAUDE_FIXER_JUDGE_RUN_ID:-}" =~ ^[1-9][0-9]*$ ]] \
  || ! [[ "${CLAUDE_FIXER_JUDGE_LEDGER_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]; then
  claude_fixer_judge_not_ready "invalid_inputs"
fi
for claude_fixer_judge_helper in review_claude_fixer_evidence.py review_claude_fixer_judge.py; do
  [ -f "${SUPPORT_SCRIPTS_DIR}/${claude_fixer_judge_helper}" ] || claude_fixer_judge_not_ready "helper_missing"
done

claude_fixer_judge_verdict="$(PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_evidence.py" verify \
  --repo "${GITHUB_REPOSITORY}" \
  --pr "${PR_NUMBER}" \
  --head "${HEAD_SHA}" \
  --run-id "${CLAUDE_FIXER_JUDGE_RUN_ID}" \
  --expect-outcome findings \
  --round "${claude_fixer_judge_round}" \
  --expect-ledger "${CLAUDE_FIXER_JUDGE_LEDGER_SHA256}" \
  --ledger-out "${claude_fixer_judge_dir}/ledger.txt" \
  --pr-head-ref "${HEAD_REF:-}" \
  --default-branch "${DEFAULT_BRANCH:-}" 2>/dev/null || true)"
if [ "$(printf '%s' "${claude_fixer_judge_verdict}" | jq -r 'if .verified == true then "true" else "false" end' 2>/dev/null || echo false)" != "true" ]; then
  claude_fixer_judge_not_ready "evidence_$(printf '%s' "${claude_fixer_judge_verdict}" | jq -r '(.reason // "unparseable") | tostring | gsub("[^A-Za-z0-9_.:-]"; "_")' 2>/dev/null || echo unparseable)"
fi

PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_judge.py" findings \
  --ledger "${claude_fixer_judge_dir}/ledger.txt" > "${claude_fixer_judge_dir}/findings.json" \
  || claude_fixer_judge_not_ready "findings_unparseable"
# A findings hand-off whose ledger has no finding was not a clean round only
# because too few reviewers returned (the #5964 panel floor). Nothing is left
# to rule on, and "nothing upheld" would merge an under-reviewed head.
if [ "$(jq 'if type == "array" then length else 0 end' "${claude_fixer_judge_dir}/findings.json" 2>/dev/null || echo 0)" = "0" ]; then
  claude_fixer_judge_not_ready "no_findings"
fi

claude_fixer_judge_comments="${PR_ISSUE_COMMENTS_FILE:-}"
if [ ! -s "${claude_fixer_judge_comments}" ]; then
  claude_fixer_judge_comments="${claude_fixer_judge_dir}/no_comments.json"
  printf '[]\n' > "${claude_fixer_judge_comments}"
fi
# The rejection must come from the fixer's identity, not just any collaborator (PR #6069
# review): the PR author (the session that pushed it) or this workflow's own GH_PAT account
# (the catch-all fixer), the same identities CLAUDE.md §26.H trusts for claims. An unreadable
# identity is left out, so the filter fails closed.
claude_fixer_judge_allowed_logins="$(
  {
    jq -r '.user.login // empty' "${PR_PAYLOAD_FILE:-/nonexistent}" 2>/dev/null || true
    gh api user --jq '.login // empty' 2>/dev/null || true
  } | sed '/^$/d' | tr '[:upper:]' '[:lower:]' | jq -R . | jq -sc .
)"
[ -n "${claude_fixer_judge_allowed_logins}" ] || claude_fixer_judge_allowed_logins='[]'
# $marker and $allowed are jq variables, not shell expansions.
# shellcheck disable=SC2016
jq -r --arg marker "<!-- ai:claude-fixer-rejection:v1 head=${HEAD_SHA} round=${claude_fixer_judge_round} -->" --argjson allowed "${claude_fixer_judge_allowed_logins}" '
    [.[]? | select(type == "object" and ((.id // null) | type) == "number"
      and ((.author_association // "") | IN("OWNER", "MEMBER", "COLLABORATOR"))
      and (((.user.login // "") | ascii_downcase) as $login | $login != "" and ($allowed | index($login)) != null)
      and ((.body // "") | split("\n") | map(gsub("\r$"; "")) | index($marker) != null))]
    | max_by(.id) // empty | .body[0:30000]
  ' "${claude_fixer_judge_comments}" > "${claude_fixer_judge_dir}/rejection.txt" 2>/dev/null || : > "${claude_fixer_judge_dir}/rejection.txt"
claude_fixer_judge_rejection="found"
if [ ! -s "${claude_fixer_judge_dir}/rejection.txt" ]; then
  # Security follow-up #6061: a dispatch alone never authorizes the judge. Without the fixer's
  # rejection comment for this head and round (OWNER / MEMBER / COLLABORATOR, posted as the PR
  # author or this workflow's account), nothing was rejected, so the judge decides nothing and
  # the PR is labelled ai:review-blocked.
  claude_fixer_judge_not_ready "rejection_missing"
fi

claude_fixer_judge_prior="$(PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_judge.py" prior-rulings \
  --comments "${claude_fixer_judge_comments}" \
  --repo "${GITHUB_REPOSITORY}" \
  --pr "${PR_NUMBER}" \
  --default-branch "${DEFAULT_BRANCH:-}" \
  --out "${claude_fixer_judge_dir}/prior_rulings.json" 2>/dev/null || echo 0)"

{
  echo "CLAUDE_FIXER_JUDGE_READY=true"
  echo "CLAUDE_FIXER_JUDGE_INPUTS_DIR=${claude_fixer_judge_dir}"
} >> "${GITHUB_ENV}"
echo "CLAUDE_FIXER_JUDGE pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_judge_round} action=prepared findings=$(jq 'length' "${claude_fixer_judge_dir}/findings.json" 2>/dev/null || echo 0) prior_rulings=${claude_fixer_judge_prior:-0} rejection=${claude_fixer_judge_rejection} handoff_run=${CLAUDE_FIXER_JUDGE_RUN_ID}"
