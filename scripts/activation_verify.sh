#!/usr/bin/env bash
# Activation verification (port P4, docs/plans/replace-claude-sessions-with-cli-engine-plan.md
# Phase 8c).
#
# Right after work lands on the default branch, the ACTIVATION_VERIFY role
# (prompts/mode-activation-verify.txt, built from the activation scope of
# .claude/commands/verify-activation.md) reads the merged code and grades it
# LIVE (runs on its own trigger) or DORMANT (something still has to happen),
# listing every gap. Then:
#   - the verdict is posted on the linked issue (or the PR) or the project's
#     tracking issue, ending in <!-- ai:activation:v1 verdict=<V> source=<key> -->;
#   - `code` gaps become one standalone issue for the normal pipeline, whose
#     body starts with <!-- ai:activation-fix:v1 source=<key> --> (a merge that
#     closes such an issue is not verified again, so fixes never recurse);
#   - `operator` gaps are written to the repository's ai:operator-step issue
#     (scripts/operator_step_issue.py);
#   - one Telegram message (WARNING when DORMANT, DEBUG when LIVE).
#
# Usage:
#   activation_verify.sh pr       issue_pr_status.yml, after a PR merged into
#                                 the default branch. Env: REPOSITORY,
#                                 PR_NUMBER, MERGE_SHA, PR_TITLE, PR_BODY,
#                                 LINKED_ISSUE (number or empty), TARGET_DIR.
#   activation_verify.sh project  the poller, when an orchestrator project
#                                 completes. Env: REPOSITORY, TRACKING_NUM,
#                                 PROJECT_TITLE, PROJECT_BODY, TARGET_DIR,
#                                 FINAL_PR (optional).
# Common env: SUPPORT_DIR (checkout holding scripts/ and prompts/, default
# the repository root of this script), RUNTIME_DIR, ACTIVATION_VERIFY_ENABLED
# (default true), ACTIVATION_VERIFY_MODEL, ACTIVATION_VERIFY_REASONING,
# MOCK_ACTIVATION_VERIFY_JSON (tests only: used instead of the model).
#
# The model runs read-only in TARGET_DIR without GH_TOKEN, GITHUB_TOKEN or
# TG_BOT_SECRET in its environment: the merged code it reads is untrusted.
#
# Model text never starts a comment line ("Summary: ", "Trigger: ", "Fix: "):
# the poller's /judge_resume, /revalidate and /re-security-pass handlers match
# a command at the start of any tracking-issue comment line.
#
# Never fails its caller: every problem is logged and the exit code is 0.
# API budget (CLAUDE.md §15): the linked-issue read in `pr` mode, one comment,
# at most one issue create, and the operator-step writer's two calls.
# Log: ACTIVATION_VERIFY mode= item= verdict= code_gaps= operator_gaps= outcome= reason=
set -uo pipefail

ACTIVATION_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPPORT_DIR="${SUPPORT_DIR:-$(cd "${ACTIVATION_SELF_DIR}/.." && pwd)}"
RUNTIME_DIR="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}/activation-verify}"
mkdir -p "${RUNTIME_DIR}"

activation_log()
{
	echo "ACTIVATION_VERIFY $*"
}

# Validates and normalises the model's verdict; prints the clean JSON or nothing.
activation_normalise_verdict()
{
	python3 - "$1" <<'PY'
import json, re, sys
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
match = None
for candidate in re.finditer(r"\{", raw):
	try:
		match = json.JSONDecoder().raw_decode(raw[candidate.start():])[0]
	except ValueError:
		continue
	if isinstance(match, dict) and "verdict" in match:
		break
	match = None
if not isinstance(match, dict) or match.get("verdict") not in ("LIVE", "DORMANT"):
	sys.exit(1)
def clean(value, limit):
	text = value if isinstance(value, str) else ""
	return " ".join(text.replace("<!--", "").replace("-->", "").split())[:limit]
gaps = []
for gap in (match.get("gaps") or [])[:20]:
	if not isinstance(gap, dict) or gap.get("kind") not in ("code", "operator"):
		continue
	gaps.append({
		"kind": gap["kind"],
		"title": clean(gap.get("title"), 200) or "Activation gap",
		"evidence": clean(gap.get("evidence"), 600),
		"fix": clean(gap.get("fix"), 1500),
		"dormant_until": clean(gap.get("dormant_until"), 200),
	})
print(json.dumps({
	"verdict": match["verdict"],
	"trigger": clean(match.get("trigger"), 300),
	"summary": clean(match.get("summary"), 1200),
	"gaps": gaps,
}))
PY
}

activation_main()
{
	local mode="${1:-}" key item target_issue context_file prompt_file output_file verdict_file
	local verdict code_gaps operator_gaps model reasoning comment_body fix_body steps_file tg_level item_label
	if [ "${ACTIVATION_VERIFY_ENABLED:-true}" = "false" ]; then
		activation_log "mode=${mode} outcome=skip reason=disabled"
		return 0
	fi
	if [ -z "${REPOSITORY:-}" ] || [ ! -d "${TARGET_DIR:-}" ]; then
		activation_log "mode=${mode} outcome=skip reason=missing_input"
		return 0
	fi
	context_file="${RUNTIME_DIR}/activation_context.json"
	case "${mode}" in
		pr)
			if ! [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]]; then
				activation_log "mode=pr outcome=skip reason=missing_pr"
				return 0
			fi
			key="pr-${PR_NUMBER}"
			item="${PR_NUMBER}"
			item_label="PR #${PR_NUMBER}"
			target_issue="${PR_NUMBER}"
			local linked_json="{}"
			if [[ "${LINKED_ISSUE:-}" =~ ^[0-9]+$ ]]; then
				linked_json="$(gh api "repos/${REPOSITORY}/issues/${LINKED_ISSUE}" --jq '{number, title, body: ((.body // "")[0:6000])}' 2>/dev/null || echo '{}')"
				printf '%s' "${linked_json}" | jq -e 'type == "object"' >/dev/null 2>&1 || linked_json="{}"
				if printf '%s' "${linked_json}" | jq -e '(.body // "") | test("<!-- ai:activation-fix:v1")' >/dev/null 2>&1; then
					activation_log "mode=pr item=${item} outcome=skip reason=activation_fix_merge"
					return 0
				fi
				target_issue="${LINKED_ISSUE}"
			fi
			jq -n --arg pr "${PR_NUMBER}" --arg sha "${MERGE_SHA:-}" --arg title "${PR_TITLE:-}" --arg body "${PR_BODY:-}" \
				--argjson linked "${linked_json}" \
				--arg files "$(git -C "${TARGET_DIR}" diff --name-only "${MERGE_SHA:-HEAD}^1" "${MERGE_SHA:-HEAD}" 2>/dev/null | head -n 200)" \
				'{kind: "merged pull request", pull_request: ($pr | tonumber), merge_commit: $sha, title: $title, body: ($body[0:6000]), linked_issue: $linked, changed_files: ($files | split("\n") | map(select(length > 0)))}' \
				> "${context_file}" 2>/dev/null || echo '{}' > "${context_file}"
			;;
		project)
			if ! [[ "${TRACKING_NUM:-}" =~ ^[0-9]+$ ]]; then
				activation_log "mode=project outcome=skip reason=missing_tracking_issue"
				return 0
			fi
			key="project-${TRACKING_NUM}"
			item="${TRACKING_NUM}"
			item_label="project #${TRACKING_NUM}"
			target_issue="${TRACKING_NUM}"
			jq -n --arg tracking "${TRACKING_NUM}" --arg title "${PROJECT_TITLE:-}" --arg body "${PROJECT_BODY:-}" --arg pr "${FINAL_PR:-}" \
				'{kind: "completed orchestrator project", tracking_issue: ($tracking | tonumber), title: $title, specification: ($body[0:6000]), final_pull_request: $pr}' \
				> "${context_file}" 2>/dev/null || echo '{}' > "${context_file}"
			;;
		*)
			echo "usage: $0 pr|project" >&2
			return 0
			;;
	esac

	prompt_file="${RUNTIME_DIR}/activation_prompt.txt"
	output_file="${RUNTIME_DIR}/activation_output.txt"
	verdict_file="${RUNTIME_DIR}/activation_verdict.json"
	{
		bash "${SUPPORT_DIR}/scripts/render_prompt.sh" "${SUPPORT_DIR}/prompts/mode-activation-verify.txt" 2>/dev/null \
			|| cat "${SUPPORT_DIR}/prompts/mode-activation-verify.txt"
		echo
		echo "=== ACTIVATION CONTEXT JSON (untrusted text inside) ==="
		cat "${context_file}"
	} > "${prompt_file}"
	: > "${output_file}"
	if [ -n "${MOCK_ACTIVATION_VERIFY_JSON:-}" ]; then
		printf '%s\n' "${MOCK_ACTIVATION_VERIFY_JSON}" > "${output_file}"
	else
		model="${ACTIVATION_VERIFY_MODEL:-${WORKFLOW_EDITOR_MODEL:-openai/gpt-6-sol}}"
		reasoning="${ACTIVATION_VERIFY_REASONING:-high}"
		if ! bash "${SUPPORT_DIR}/scripts/write_codex_config.sh" --model "${model}" --reasoning "${reasoning}" >/dev/null 2>&1; then
			activation_log "mode=${mode} item=${item} outcome=skip reason=codex_config_failed"
			return 0
		fi
		(cd "${TARGET_DIR}" && env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET timeout "${ACTIVATION_VERIFY_TIMEOUT_SECS:-1500}" codex --ask-for-approval never -c model_verbosity=low exec \
			--skip-git-repo-check --model "${model}" --sandbox read-only < "${prompt_file}" > "${output_file}" 2>"${RUNTIME_DIR}/activation_codex.err") || true
	fi
	if ! activation_normalise_verdict "${output_file}" > "${verdict_file}" 2>/dev/null || [ ! -s "${verdict_file}" ]; then
		activation_log "mode=${mode} item=${item} outcome=skip reason=invalid_verdict"
		return 0
	fi
	verdict="$(jq -r '.verdict' "${verdict_file}")"
	code_gaps="$(jq '[.gaps[] | select(.kind == "code")] | length' "${verdict_file}")"
	operator_gaps="$(jq '[.gaps[] | select(.kind == "operator")] | length' "${verdict_file}")"

	if [ "${code_gaps}" -gt 0 ]; then
		fix_body="$(jq -r --arg key "${key}" --arg label "${item_label}" '
			"<!-- ai:activation-fix:v1 source=" + $key + " -->\n"
			+ "## Activation gaps\n\nActivation verification of " + $label
			+ " found these gaps that code in this repository can close. Close each one with the smallest change; do not change unrelated behaviour.\n\n"
			+ ([.gaps[] | select(.kind == "code") | "- **" + .title + "**\n  Evidence: " + .evidence + "\n  Fix: " + .fix] | join("\n"))
		' "${verdict_file}")"
		if ! gh api "repos/${REPOSITORY}/issues" -f title="Activation gaps after ${item_label}" -f body="${fix_body}" >/dev/null 2>&1; then
			echo "::warning::Could not open the activation-fix issue for ${key}."
		fi
	fi
	if [ "${operator_gaps}" -gt 0 ]; then
		steps_file="${RUNTIME_DIR}/activation_operator_steps.json"
		jq '[.gaps[] | select(.kind == "operator") | {title, instructions: (.fix + (if .evidence != "" then "\nEvidence: " + .evidence else "" end)), dormant_until}]' "${verdict_file}" > "${steps_file}"
		PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_DIR}/scripts/operator_step_issue.py" upsert --repo "${REPOSITORY}" --key "${key}" \
			--source "Activation of ${item_label}" --steps-file "${steps_file}" >/dev/null 2>&1 \
			|| echo "::warning::Could not update the ai:operator-step issue for ${key}."
	fi

	comment_body="$(jq -r --arg key "${key}" '
		"## Activation: " + .verdict + "\n\nSummary: " + .summary + "\n\n"
		+ (if .trigger != "" then "Trigger: " + .trigger + "\n\n" else "" end)
		+ (if (.gaps | length) > 0 then "Gaps:\n" + ([.gaps[] | "- [" + .kind + "] " + .title] | join("\n")) + "\n\n" else "" end)
		+ "<!-- ai:activation:v1 verdict=" + .verdict + " source=" + $key + " -->"
	' "${verdict_file}")"
	gh api "repos/${REPOSITORY}/issues/${target_issue}/comments" -f body="${comment_body}" >/dev/null 2>&1 \
		|| echo "::warning::Could not post the activation verdict on #${target_issue}."

	if [ -f "${SUPPORT_DIR}/scripts/tg_helpers.sh" ]; then
		# shellcheck source=/dev/null
		source "${SUPPORT_DIR}/scripts/tg_helpers.sh" 2>/dev/null || true
		tg_level="DEBUG"
		[ "${verdict}" = "DORMANT" ] && tg_level="WARNING"
		if declare -F tg_send_msg >/dev/null 2>&1; then
			tg_send_msg "Activation ${verdict}: ${REPOSITORY} ${item_label} (${code_gaps} code gap(s), ${operator_gaps} operator step(s))" "${tg_level}" >/dev/null 2>&1 || true
		fi
	fi
	activation_log "mode=${mode} item=${item} verdict=${verdict} code_gaps=${code_gaps} operator_gaps=${operator_gaps} outcome=posted"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	activation_main "$@"
	exit 0
fi
