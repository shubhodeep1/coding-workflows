#!/usr/bin/env bash
# Handle destructive-commit and scope-guard rejections after support cleanup.
# shellcheck disable=SC2153 # DCB_* and SVB_* values are workflow environment inputs.
set -euo pipefail

RUN_URL="https://github.com/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

# Scope-block branch. When either scope guard (files_touched preflight /
# commit-time, or the post-commit ai:scope label verifier) latched
# scope_violation_blocked, handle it here with a distinct
# ai:scope-blocked label + scope-specific comment/alert, then exit.
# The destructive-deletion branch below is unchanged and runs only
# when this was not a scope block.
if [ -n "${SVB_REASON:-}" ]; then
  SCOPE_HEADER="🚨 **files_touched scope guard rejected this implementation run.**"
  SCOPE_COUNT_LABEL="Out-of-scope staged paths"
  SCOPE_DETAIL_HEADING="Staged paths outside the issue's \`files_touched\` allowlist:"
  SCOPE_ALLOWLIST_HEADING="Declared \`files_touched\` allowlist:"
  SCOPE_COMMIT_STATE="The commit was **not** created and **not** pushed."
  SCOPE_REDISPATCH_HINT="If these paths are legitimately in scope, add them to the issue's \`files_touched\` list (or rerun with \`ALLOW_OUT_OF_SCOPE_FILES=true\`) and remove \`ai:scope-blocked\`. **Note:** the orchestrator judge may still regenerate this task under a different issue number — the per-issue block does not cover judge-cycle regeneration."
  SCOPE_TG_TITLE="🚨 CRITICAL: files_touched scope guard blocked implementation"
  if [ "${SVB_REASON}" = "scope-lock-label" ]; then
    SCOPE_HEADER="🚨 **Issue scope-lock rejected this implementation run.**"
    SCOPE_COUNT_LABEL="Out-of-scope committed paths"
    SCOPE_DETAIL_HEADING="Committed paths outside the active \`ai:scope:<glob>\` label:"
    SCOPE_ALLOWLIST_HEADING="Active \`ai:scope:<glob>\` glob:"
    SCOPE_COMMIT_STATE="The out-of-scope commit was created locally, rolled back before push, and **not** pushed."
    SCOPE_REDISPATCH_HINT="If these paths are legitimately in scope, update or remove the issue's \`ai:scope:<glob>\` label, then remove \`ai:scope-blocked\` and redispatch. **Note:** the orchestrator judge may still regenerate this task under a different issue number — the per-issue block does not cover judge-cycle regeneration."
    SCOPE_TG_TITLE="🚨 CRITICAL: ai:scope label blocked implementation"
  fi
  SCOPE_BLOCK_LABEL_DESCRIPTION='Implementation blocked: staged files fell outside files_touched scope; human review required'
  if ! gh label create 'ai:scope-blocked' \
    --repo "${GITHUB_REPOSITORY}" \
    --color 'b60205' \
    --description "${SCOPE_BLOCK_LABEL_DESCRIPTION}" \
    2>/dev/null \
    && ! gh label edit 'ai:scope-blocked' \
    --repo "${GITHUB_REPOSITORY}" \
    --color 'b60205' \
    --description "${SCOPE_BLOCK_LABEL_DESCRIPTION}" \
    2>/dev/null; then
    echo "::warning::Could not ensure the ai:scope-blocked label exists on ${GITHUB_REPOSITORY} — the GH_PAT may lack issues:write. The follow-up read below reports whether the issue itself ended up latched."
  fi
  gh issue edit "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" \
    --add-label 'ai:scope-blocked' \
    --remove-label 'ai:implementing' 2>/dev/null || \
  gh issue edit "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" \
    --add-label 'ai:scope-blocked' 2>/dev/null || true
  SCOPE_LATCH_STATUS_LINE="The workflow attempted to label this issue \`ai:scope-blocked\`, but the follow-up label read failed. Future redispatch is **not confirmed blocked**; re-check the label manually before redispatch."
  SCOPE_TG_LATCH_LINE="Could not verify ai:scope-blocked latch: gh issue view failed after the write attempt, so redispatch refusal is unknown until a human rechecks the label."
  if scope_latched_labels="$(gh issue view "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" --json labels -q '.labels[].name' 2>/dev/null)"; then
    if grep -qxF 'ai:scope-blocked' <<< "${scope_latched_labels}"; then
      echo "Confirmed ai:scope-blocked is latched on #${ISSUE_NUMBER}; redispatch will be refused until a human removes it."
      SCOPE_LATCH_STATUS_LINE="This issue is confirmed labeled \`ai:scope-blocked\`; the \`Validate approval phase\` step will refuse future redispatches of issue #${ISSUE_NUMBER} until a human removes the label."
      SCOPE_TG_LATCH_LINE="Confirmed ai:scope-blocked latch: future redispatch of this exact issue ID will be refused until a human removes the label."
    else
      echo "::error::FAILED to latch ai:scope-blocked on #${ISSUE_NUMBER}; the redispatch block is NOT in effect. Apply it manually: (gh label edit ai:scope-blocked --repo ${GITHUB_REPOSITORY} --color b60205 --description 'Implementation blocked: staged files fell outside files_touched scope; human review required' || gh label create ai:scope-blocked --repo ${GITHUB_REPOSITORY} --color b60205 --description 'Implementation blocked: staged files fell outside files_touched scope; human review required') && gh issue edit ${ISSUE_NUMBER} --repo ${GITHUB_REPOSITORY} --add-label ai:scope-blocked"
      SCOPE_LATCH_STATUS_LINE="The workflow attempted to label this issue \`ai:scope-blocked\`, but the follow-up label read did **not** find it. Future redispatch is **not confirmed blocked**; reapply the label manually before redispatch."
      SCOPE_TG_LATCH_LINE="FAILED to confirm ai:scope-blocked latch: redispatch refusal is NOT in effect until a human reapplies and rechecks the label."
    fi
  else
    echo "::warning::Could not verify ai:scope-blocked on #${ISSUE_NUMBER}; gh issue view failed, so the latch state is unknown. Re-check manually: gh issue view ${ISSUE_NUMBER} --repo ${GITHUB_REPOSITORY} --json labels -q '.labels[].name'"
    SCOPE_LATCH_STATUS_LINE="The workflow attempted to label this issue \`ai:scope-blocked\`, but verification failed, so the latch state is unknown. Re-check the issue labels manually before assuming the \`Validate approval phase\` redispatch block is active."
    SCOPE_TG_LATCH_LINE="Could not verify ai:scope-blocked; the latch state is unknown and must be checked manually."
  fi
  {
    echo "${SCOPE_HEADER}"
    echo
    echo "- ${SCOPE_COUNT_LABEL}: **${SVB_COUNT}**"
    echo "- Workflow run: ${RUN_URL}"
    echo
    echo "${SCOPE_COMMIT_STATE} ${SCOPE_LATCH_STATUS_LINE}"
    echo
    echo "${SCOPE_DETAIL_HEADING}"
    echo
    echo '```'
    printf '%s\n' "${SVB_FILES}" | sed '/^$/d'
    echo '```'
    echo
    echo "${SCOPE_ALLOWLIST_HEADING}"
    echo
    echo '```'
    printf '%s\n' "${SVB_ALLOWLIST}" | sed '/^$/d'
    echo '```'
    echo
    echo "${SCOPE_REDISPATCH_HINT}"
  } > /tmp/scope_comment.md
  gh issue comment "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" \
    --body-file /tmp/scope_comment.md 2>/dev/null || true
  if [ -n "${TG_BOT_SECRET:-}" ] && [ -n "${TG_ADMIN_CHAT_ID:-}" ]; then
    TG_MSG="$(printf '%s\n' \
      "${SCOPE_TG_TITLE}" \
      "repo: ${GITHUB_REPOSITORY}" \
      "issue: #${ISSUE_NUMBER}" \
      "out-of-scope paths: ${SVB_COUNT}" \
      "run: ${RUN_URL}" \
      "" \
      "${SCOPE_TG_LATCH_LINE}" \
      "The orchestrator judge may still regenerate this task under a different issue number.")"
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_SECRET}/sendMessage" \
      -d "chat_id=${TG_ADMIN_CHAT_ID}" \
      -d "disable_web_page_preview=true" \
      --data-urlencode "text=${TG_MSG}" >/dev/null 2>&1 || \
      echo "::warning::Telegram alert send failed for scope rejection on issue #${ISSUE_NUMBER}"
  else
    echo "::warning::TG_BOT_SECRET or TG_ADMIN_CHAT_ID unset; scope rejection was not sent to Telegram."
  fi
  # Preserve the non-zero exit — keep the job red and skip the
  # downstream push/PR steps, exactly like the destructive branch.
  exit 1
fi

REASON_HUMAN="${DCB_REASON}"
DCB_COUNT_LABEL="Staged deletions"
DCB_LIST_HEADING="Staged deletion list"
DCB_REDISPATCH_HINT="If these deletions are legitimate, set the repository variable \`ALLOW_WORKFLOW_EDITS=true\` (for canonical-source deletions) or \`ALLOW_BULK_DELETE=true\` (for threshold-triggered rejections) under Settings → Secrets and variables → Actions, then remove \`ai:destructive-blocked\` from the issue if it is present and redispatch. **Note:** the orchestrator judge may still regenerate this task under a different issue number — the per-issue block does not cover judge-cycle regeneration (see PRs #917/#931 for context)."
TG_COUNT_LINE="deletions: ${DCB_COUNT}"
case "${DCB_REASON}" in
  canonical-source)
    REASON_HUMAN="canonical workflow-source file deletion"
    ;;
  bulk-delete)
    if [ -n "${DCB_THRESHOLD_LABEL}" ] && [ -n "${DCB_EFFECTIVE_THRESHOLD}" ]; then
      REASON_HUMAN="bulk deletion exceeded ${DCB_THRESHOLD_LABEL}=${DCB_EFFECTIVE_THRESHOLD}"
    elif [ -n "${DCB_THRESHOLD_LABEL}" ]; then
      REASON_HUMAN="bulk deletion exceeded ${DCB_THRESHOLD_LABEL}"
    else
      REASON_HUMAN="bulk deletion exceeded BULK_DELETE_THRESHOLD"
    fi
    ;;
  unsafe-fetched-manifest)
    REASON_HUMAN="artifact-cleanup manifest contained unsafe path(s)"
    DCB_COUNT_LABEL="Unsafe manifest paths"
    DCB_LIST_HEADING="Unsafe manifest path list"
    DCB_REDISPATCH_HINT="The commit helper refused to process these manifest entries because they were not safe repo-relative cleanup paths. Inspect the runtime-fetched manifest producer or any tool that rewrote \`FETCHED_MANIFEST\`, then rerun after the manifest contains only safe relative paths. **Note:** the orchestrator judge may still regenerate this task under a different issue number — the per-issue block does not cover judge-cycle regeneration."
    TG_COUNT_LINE="unsafe_manifest_paths: ${DCB_COUNT}"
    ;;
esac

# 1. Ensure the latch label exists. Color/description are
#    hardcoded to mirror scripts/label_helpers.sh and
#    .github/ai/label_contract.v1.json without sourcing them —
#    that helper may be gone after consumer-repo cleanup.
#    `gh label create` exits non-zero (and prints to stderr) when
#    the label already exists — the normal idempotent case — so
#    fall back to `gh label edit`; only a genuine double-failure
#    (e.g. the GH_PAT lacks issues:write) reaches the warning.
#    The old `2>/dev/null || true` swallowed exactly that failure;
#    the follow-up read in step (2b) confirms the issue-level latch
#    when GitHub accepts the verification read.
if ! gh label create 'ai:destructive-blocked' \
  --repo "${GITHUB_REPOSITORY}" \
  --color 'b60205' \
  --description 'Implementation blocked for mass/destructive deletions; this issue ID now waits for human review' \
  2>/dev/null \
  && ! gh label edit 'ai:destructive-blocked' \
  --repo "${GITHUB_REPOSITORY}" \
  --color 'b60205' \
  --description 'Implementation blocked for mass/destructive deletions; this issue ID now waits for human review' \
  2>/dev/null; then
  echo "::warning::Could not ensure the ai:destructive-blocked label exists on ${GITHUB_REPOSITORY} — the GH_PAT may lack issues:write. The follow-up read below reports whether the issue itself ended up latched."
fi

# 2. Apply ai:destructive-blocked, clear ai:implementing so the
#    issue is visibly in a halt state rather than a transient
#    one. Best-effort — if gh is unavailable we still fire the
#    TG alert below.
gh issue edit "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" \
  --add-label 'ai:destructive-blocked' \
  --remove-label 'ai:implementing' 2>/dev/null || \
gh issue edit "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" \
  --add-label 'ai:destructive-blocked' 2>/dev/null || true

# 2b. Verify the latch actually engaged. The best-effort edits
#     above swallow their errors so the comment + TG alert still
#     fire, but a silent miss means the `Validate approval phase`
#     redispatch refusal never takes effect — the exact failure
#     seen on a consumer repo where the label was never created.
#     Read the post-write label set (a dedicated call: no earlier
#     step in this handler fetches it). A successful read can prove
#     the latch is present or missing; if the read itself fails,
#     warn that the state is unknown instead of falsely claiming
#     the label is absent. grep reads a here-string (not a
#     pipeline) so set -o pipefail cannot misfire on -q's early exit.
if latched_labels="$(gh issue view "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" --json labels -q '.labels[].name' 2>/dev/null)"; then
  if grep -qxF 'ai:destructive-blocked' <<< "${latched_labels}"; then
    echo "Confirmed ai:destructive-blocked is latched on #${ISSUE_NUMBER}; redispatch will be refused until a human removes it."
  else
    echo "::error::FAILED to latch ai:destructive-blocked on #${ISSUE_NUMBER}; the redispatch block is NOT in effect. Apply it manually: (gh label edit ai:destructive-blocked --repo ${GITHUB_REPOSITORY} --color b60205 --description 'Implementation blocked for mass/destructive deletions; this issue ID now waits for human review' || gh label create ai:destructive-blocked --repo ${GITHUB_REPOSITORY} --color b60205 --description 'Implementation blocked for mass/destructive deletions; this issue ID now waits for human review') && gh issue edit ${ISSUE_NUMBER} --repo ${GITHUB_REPOSITORY} --add-label ai:destructive-blocked"
  fi
else
  echo "::warning::Could not verify ai:destructive-blocked on #${ISSUE_NUMBER}; gh issue view failed, so the latch state is unknown. Re-check manually: gh issue view ${ISSUE_NUMBER} --repo ${GITHUB_REPOSITORY} --json labels -q '.labels[].name'"
fi

# 3. Post a visible issue comment describing what was rejected
#    and what the operator needs to do. Embeds the deletion
#    list in a fenced block so humans can audit which files
#    the codex run tried to unlink.
{
  echo "🚨 **Destructive-commit guard rejected this implementation run.**"
  echo
  echo "- Reason: \`${DCB_REASON}\` (${REASON_HUMAN})"
  echo "- ${DCB_COUNT_LABEL}: **${DCB_COUNT}**"
  echo "- Workflow run: ${RUN_URL}"
  echo
  echo "The commit was **not** created and **not** pushed. The workflow attempted to apply \`ai:destructive-blocked\`; verify that the label is present before relying on the \`Validate approval phase\` redispatch block. Once present, future redispatches of issue #${ISSUE_NUMBER} will be refused until a human removes the label."
  echo
  echo "${DCB_LIST_HEADING}:"
  echo
  echo '```'
  printf '%s\n' "${DCB_DELETIONS}" | sed '/^$/d'
  echo '```'
  echo
  echo "${DCB_REDISPATCH_HINT}"
} > /tmp/destructive_comment.md
gh issue comment "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" \
  --body-file /tmp/destructive_comment.md 2>/dev/null || true

# 4. Fire a CRITICAL Telegram alert (inline — tg_helpers.sh may
#    have been removed by consumer-repo cleanup). Build the
#    message body via printf so every line stays within the
#    YAML block-scalar indentation level; curl's
#    --data-urlencode then transmits the embedded newlines.
if [ -n "${TG_BOT_SECRET:-}" ] && [ -n "${TG_ADMIN_CHAT_ID:-}" ]; then
  TG_MSG="$(printf '%s\n' \
    "🚨 CRITICAL: destructive-commit guard blocked implementation" \
    "repo: ${GITHUB_REPOSITORY}" \
    "issue: #${ISSUE_NUMBER}" \
    "reason: ${DCB_REASON} (${REASON_HUMAN})" \
    "${TG_COUNT_LINE}" \
    "run: ${RUN_URL}" \
    "" \
    "Issue is now ai:destructive-blocked. A human must review and remove the label before this exact issue ID can be redispatched. The orchestrator judge may still regenerate this task under a different issue number.")"
  curl -s -X POST "https://api.telegram.org/bot${TG_BOT_SECRET}/sendMessage" \
    -d "chat_id=${TG_ADMIN_CHAT_ID}" \
    -d "disable_web_page_preview=true" \
    --data-urlencode "text=${TG_MSG}" >/dev/null 2>&1 || \
    echo "::warning::Telegram alert send failed for destructive-commit rejection on issue #${ISSUE_NUMBER}"
else
  echo "::warning::TG_BOT_SECRET or TG_ADMIN_CHAT_ID unset; destructive-commit rejection was not sent to Telegram."
fi

# Preserve the non-zero exit from commit_changes — do NOT
# swallow the failure here. The job should still fail so
# operators see a red X in Actions UI, and so downstream
# steps (Push branch, Create PR) are skipped.
exit 1
