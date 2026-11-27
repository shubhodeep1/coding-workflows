#!/usr/bin/env bash
# Body of the "Capture runtime context" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# at or under 380,000 bytes, well under the 430,000-byte contract-test
# ceiling and GitHub's 512,000-byte workflow file limit (#10166). The step
# sources this file in its own shell, so the step's if: stays in the
# workflow; edit it there.
set -euo pipefail

# Early guard: if the Create runtime workspace step never ran
# (e.g. a prior step failed before RUNTIME_CONTEXT_DIR was
# written to $GITHUB_ENV), this step would otherwise fail with
# "unbound variable" under set -u and mask the real root-cause
# error in the failure notification step.  Skip cleanly so the
# job's failure reason stays pinned to the actual first failure.
if [ -z "${RUNTIME_CONTEXT_DIR:-}" ]; then
  echo "::warning::RUNTIME_CONTEXT_DIR not set; runtime workspace step did not complete. Skipping runtime context capture."
  exit 0
fi

mkdir -p "${RUNTIME_CONTEXT_DIR}"

git status --short --branch > "${RUNTIME_CONTEXT_DIR}/git_status.txt" || true
git diff --stat > "${RUNTIME_CONTEXT_DIR}/git_diff_stat.txt" || true
ls -R . | sed -n "1,400p" > "${RUNTIME_CONTEXT_DIR}/shallow_tree.txt" || true
{
  for key in \
    CI \
    GITHUB_ACTION \
    GITHUB_ACTIONS \
    GITHUB_ACTOR \
    GITHUB_ACTOR_ID \
    GITHUB_API_URL \
    GITHUB_BASE_REF \
    GITHUB_ENV \
    GITHUB_EVENT_NAME \
    GITHUB_EVENT_PATH \
    GITHUB_GRAPHQL_URL \
    GITHUB_HEAD_REF \
    GITHUB_JOB \
    GITHUB_OUTPUT \
    GITHUB_PATH \
    GITHUB_REF \
    GITHUB_REF_NAME \
    GITHUB_REF_PROTECTED \
    GITHUB_REF_TYPE \
    GITHUB_REPOSITORY \
    GITHUB_REPOSITORY_ID \
    GITHUB_REPOSITORY_OWNER \
    GITHUB_REPOSITORY_OWNER_ID \
    GITHUB_RETENTION_DAYS \
    GITHUB_RUN_ATTEMPT \
    GITHUB_RUN_ID \
    GITHUB_RUN_NUMBER \
    GITHUB_SERVER_URL \
    GITHUB_SHA \
    GITHUB_STEP_SUMMARY \
    GITHUB_TRIGGERING_ACTOR \
    GITHUB_WORKFLOW \
    GITHUB_WORKFLOW_REF \
    GITHUB_WORKFLOW_SHA \
    GITHUB_WORKSPACE \
    HOME \
    LANG \
    LC_ALL \
    PATH \
    PWD \
    RUNNER_ARCH \
    RUNNER_NAME \
    RUNNER_OS \
    RUNNER_TEMP \
    RUNNER_TOOL_CACHE \
    RUNNER_WORKSPACE \
    SHELL \
    SHLVL \
    TZ \
    USER; do
    if [ "${!key+x}" = "x" ]; then
      printf '%s=%s\n' "${key}" "${!key}"
    fi
  done
} | sort > "${RUNTIME_CONTEXT_DIR}/environment_sorted.txt" || true
git log --oneline -n 20 > "${RUNTIME_CONTEXT_DIR}/recent_commits.txt" || true
git branch -a > "${RUNTIME_CONTEXT_DIR}/branches.txt" || true
cp .github/workflows/review_autofix.yml "${RUNTIME_CONTEXT_DIR}/workflow_snapshot.yml" || true
{
  [ -d "${HOME}/.npm/_logs" ] && ls -1 "${HOME}/.npm/_logs" | tail -n 5 || true
  [ -f "${GITHUB_STEP_SUMMARY:-}" ] && cat "${GITHUB_STEP_SUMMARY}" || true
} > "${RUNTIME_CONTEXT_DIR}/run_logs_best_effort.txt" || true
