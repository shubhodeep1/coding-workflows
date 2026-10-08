#!/usr/bin/env bash
# propagate_consumer_secrets.sh — copy the library's consumer-facing Actions
# secrets into consumer repositories.
#
# Runs from .github/workflows/propagate-consumer-secrets.yml: on every push to
# main that changes .github/ai/consumer_repos.json (CLAUDE.md §14), and on
# workflow_dispatch. For each target repository it writes every secret named
# in CONSUMER_SECRET_NAMES whose value is present in this process's
# environment, through `gh secret set` (gh performs the libsodium sealed-box
# encryption; the value travels on stdin and never appears in an argument,
# a log line, or an annotation), then confirms the names with
# `gh secret list`. Repository secrets on a personal account exist per
# repository, so a repo seeded later still needs its own copies; this script
# is what makes that step unattended (plan docs/plans/unattended-claude-
# pipeline-completion-plan.md, Q15).
#
# Inputs (environment):
#   GH_TOKEN              — the library's GH_PAT; must hold `repo` scope on
#                           every consumer (the same scope mark-stable.yml's
#                           repository_dispatch already needs). Required.
#   CONSUMER_REPOS_FILE   — registry path (default .github/ai/consumer_repos.json).
#   PROPAGATE_TARGETS     — whitespace-separated owner/repo list. Empty means
#                           every registry entry. A target that is not in the
#                           registry is refused (status=skipped_unregistered);
#                           this script never writes to an unregistered repo.
#   CONSUMER_SECRET_NAMES — whitespace-separated secret names to copy
#                           (default: CHECK_TRIAGE_ISSUES_TOKEN GH_PAT
#                           OPENROUTER_API_KEY TG_BOT_SECRET). Each value is
#                           read from the environment variable of the same
#                           name; an empty value is skipped (status=skipped_empty).
#
# Output: one `CONSUMER_SECRETS_PROPAGATE repo=<owner/repo> secret=<NAME>
# status=<set|skipped_empty|failed|verify_missing>` line per secret (plus
# `repo=<owner/repo> status=verify_list_failed` when the post-write listing
# itself fails) and a final `CONSUMER_SECRETS_PROPAGATE summary ...` line.
# Per-repo failures do
# not stop the loop (fail open across repos, like
# scripts/workflow_retro_fanout.sh), but the exit status is 1 when any
# secret could not be set or verified, so the workflow run goes red and the
# workflow-failure heal intake sees it.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

CONSUMER_REPOS_FILE="${CONSUMER_REPOS_FILE:-${REPO_ROOT}/.github/ai/consumer_repos.json}"
PROPAGATE_TARGETS="${PROPAGATE_TARGETS:-}"
CONSUMER_SECRET_NAMES="${CONSUMER_SECRET_NAMES:-CHECK_TRIAGE_ISSUES_TOKEN GH_PAT OPENROUTER_API_KEY TG_BOT_SECRET}"

log()
{
	echo "CONSUMER_SECRETS_PROPAGATE $*"
}

if [ -z "${GH_TOKEN:-}" ]; then
	echo "::error::propagate-consumer-secrets: GH_TOKEN is empty; the library GH_PAT is required to write consumer secrets."
	exit 1
fi
for tool in gh jq; do
	if ! command -v "${tool}" >/dev/null 2>&1; then
		echo "::error::propagate-consumer-secrets: ${tool} is not installed."
		exit 1
	fi
done
if [ ! -f "${CONSUMER_REPOS_FILE}" ]; then
	echo "::error::propagate-consumer-secrets: ${CONSUMER_REPOS_FILE} not found."
	exit 1
fi
if ! REGISTRY="$(jq -er 'if type == "array" and all(.[]; type == "string") then .[] else error("registry must be a JSON array of strings") end' "${CONSUMER_REPOS_FILE}" 2>/dev/null)"; then
	echo "::error::propagate-consumer-secrets: ${CONSUMER_REPOS_FILE} is not a JSON array of owner/repo strings."
	exit 1
fi

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

registry_has()
{
	printf '%s\n' "${REGISTRY}" | grep -Fxq -- "$1"
}

# set_consumer_secret NAME OWNER/REPO — one `gh secret set` attempt. The value
# is read from the environment variable NAME and piped on stdin, so gh
# encrypts it with the repository's public key and it never appears in an
# argument or in gh_retry's retry diagnostics (which only echo the command
# words). gh_retry invokes this function once per attempt, so every attempt
# gets a fresh stdin; piping into `gh_retry gh secret set` directly would
# hand attempt 2 an already-consumed pipe (review finding on PR #6709).
set_consumer_secret()
{
	printf '%s' "${!1}" | gh secret set "$1" --repo "$2"
}

if [ -n "${PROPAGATE_TARGETS// /}" ]; then
	# shellcheck disable=SC2086
	TARGETS="$(printf '%s\n' ${PROPAGATE_TARGETS})"
else
	TARGETS="${REGISTRY}"
fi

targets_total=0
set_count=0
skipped_count=0
failed_count=0
secret_name_count=0
for secret_name in ${CONSUMER_SECRET_NAMES}; do
	if ! [[ "${secret_name}" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
		echo "::error::propagate-consumer-secrets: invalid secret name '${secret_name}' in CONSUMER_SECRET_NAMES."
		exit 1
	fi
	secret_name_count=$((secret_name_count + 1))
	if [ -z "${!secret_name:-}" ]; then
		echo "::warning::propagate-consumer-secrets: ${secret_name} is empty in this environment; it will be skipped for every target."
	fi
done
if [ "${secret_name_count}" -eq 0 ]; then
	echo "::error::propagate-consumer-secrets: CONSUMER_SECRET_NAMES is empty."
	exit 1
fi

while IFS= read -r target; do
	[ -n "${target}" ] || continue
	# Owner: alphanumerics and hyphens, leading alphanumeric (GitHub's rule).
	# Repo: GitHub also allows dots and underscores, so reject the two path
	# components that would otherwise pass as a name.
	if ! [[ "${target}" =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$ ]] \
	   || [ "${target##*/}" = "." ] || [ "${target##*/}" = ".." ]; then
		log "repo=${target} status=skipped_invalid_slug"
		failed_count=$((failed_count + 1))
		continue
	fi
	if ! registry_has "${target}"; then
		log "repo=${target} status=skipped_unregistered"
		failed_count=$((failed_count + 1))
		continue
	fi
	targets_total=$((targets_total + 1))
	set_names=""
	for secret_name in ${CONSUMER_SECRET_NAMES}; do
		if [ -z "${!secret_name:-}" ]; then
			log "repo=${target} secret=${secret_name} status=skipped_empty"
			skipped_count=$((skipped_count + 1))
			continue
		fi
		if gh_retry set_consumer_secret "${secret_name}" "${target}" >/dev/null; then
			log "repo=${target} secret=${secret_name} status=set"
			set_count=$((set_count + 1))
			set_names="${set_names} ${secret_name}"
		else
			log "repo=${target} secret=${secret_name} status=failed"
			failed_count=$((failed_count + 1))
		fi
	done
	if [ -n "${set_names// /}" ]; then
		if listed="$(gh_retry gh secret list --repo "${target}" --json name --jq '.[].name' 2>/dev/null)"; then
			for secret_name in ${set_names}; do
				if ! printf '%s\n' "${listed}" | grep -Fxq -- "${secret_name}"; then
					log "repo=${target} secret=${secret_name} status=verify_missing"
					failed_count=$((failed_count + 1))
				fi
			done
		else
			# An unverifiable write counts as a failure: a green run must mean
			# the names were seen on the consumer, not merely that the set
			# calls returned 0 (review finding on PR #6709).
			log "repo=${target} status=verify_list_failed"
			failed_count=$((failed_count + 1))
		fi
	fi
done <<< "${TARGETS}"

log "summary targets=${targets_total} set=${set_count} skipped=${skipped_count} failed=${failed_count}"
if [ "${failed_count}" -gt 0 ]; then
	echo "::error::propagate-consumer-secrets: ${failed_count} secret write(s) failed or could not be verified; see the CONSUMER_SECRETS_PROPAGATE lines above."
	exit 1
fi
