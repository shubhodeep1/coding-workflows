#!/usr/bin/env bash
# alert_self_fix.sh — the dispatched self-fix of a pipeline-blocking alert
# group (alert triage plan Phase 3, Refs #9394;
# docs/plans/alert-triage-auto-heal-plan.md, "Self-fix and break-glass").
#
# Body of the `self-fix` job of .github/workflows/alert-self-fix.yml, which
# scripts/alert_triage_route.sh dispatches with the inputs `fingerprint`
# (64 hex) and `diagnosis` (the compact ALERT_TRIAGE JSON, at most 16 KiB).
# Every subcommand is one workflow step; state passes between them through
# ALERT_SELF_FIX_STATE_DIR (under RUNNER_TEMP, this job only).
#
#   prepare     validate the run context (default branch, HEAD == GITHUB_SHA,
#               the pinned SHA), the fingerprint and the diagnosis; skip when
#               an open `ai:alert-self-fix` PR already carries the marker
#               `<!-- alert-triage:fp=<fp> -->` (one list call); refuse when
#               the branch ai/alert-self-fix-<fp8>-<run_id> already exists
#               (never force-pushes); add the editor worktree at the pinned
#               SHA under RUNNER_TEMP. The workspace checkout stays the
#               trusted support root.
#   edit        run the IMPLEMENT engine role (Claude through
#               scripts/ai_engine.sh claude_run, codex fallback in the
#               isolated workspace sandbox) on the worktree. The engine never
#               gets a GitHub token, the OIDC request variables or the
#               Telegram secret. No change is a failure (no_change).
#   verify      host-side syntax checks of the changed files (no execution)
#               and the credential-free pre-push affected-test gate
#               (scripts/prepush_test_gate_run.sh with the trusted
#               .claude/hooks/prepush_affected_tests.py, whose static units
#               run the ruff / shellcheck checks of ci.yml), plus ci.yml's
#               `yamllint -s` and `actionlint -shellcheck=` on the changed
#               workflow / action / template YAML. Only `pass` or `skipped`
#               continues; anything else opens no PR.
#   publish     commit, non-force push with GH_TOKEN (through GIT_CONFIG_*
#               env, never argv), compose the PR body, lint it with
#               scripts/lint_pr_body_auto_close.py (non-zero blocks the PR),
#               open the PR labelled ai:alert-self-fix, write the ledger
#               record self_fix/pr-<n>.json on ai/alert-inbox, and enable
#               auto-merge (squash, --match-head-commit).
#   ledger-put PATH FILE
#               create PATH on ai/alert-inbox through the contents API with
#               GITHUB_TOKEN (one retry on 409). Exit 0 created, 3 the path
#               already exists, 1 failure. Shared with
#               scripts/alert_self_fix_break_glass.sh.
#   escalate    one direct Telegram message (tg_send_msg_direct, never
#               captured into the inbox) with tokens only: fingerprint,
#               stage, outcome, reason and run URL. Never model text.
#
# Variables (every one has a default): ALERT_SELF_FIX_DEFAULT_BRANCH=main,
# ALERT_SELF_FIX_EDITOR_TIMEOUT_SECS=3600, ALERT_SELF_FIX_GATE_TIMEOUT_MINUTES=20,
# ALERT_SELF_FIX_MODEL=openai/gpt-6-sol, ALERT_SELF_FIX_STATE_DIR and
# ALERT_SELF_FIX_TREE_DIR under $RUNNER_TEMP, ALERT_SELF_FIX_REMOTE_URL (this
# repository on $GITHUB_SERVER_URL), ALERT_SELF_FIX_INPUT_FINGERPRINT and
# ALERT_SELF_FIX_INPUT_DIAGNOSIS (empty: refused).
#
# Log lines (stdout, stable prefix ALERT_SELF_FIX):
#   ALERT_SELF_FIX <stage> fp=<fp12> outcome=<token> [reason=<token>] [k=v]...
#   stages: prepare|edit|verify|publish|automerge|ledger|escalate

set -euo pipefail

ASF_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASF_REPO_ROOT="$(dirname "${ASF_SCRIPT_DIR}")"
ASF_SELF_REPO="${GITHUB_REPOSITORY:-shubhodeep1/coding-workflows}"
ASF_SERVER="${GITHUB_SERVER_URL:-https://github.com}"
ASF_TMP="${RUNNER_TEMP:-/tmp}"
ASF_RUN_ID="${GITHUB_RUN_ID:-0}"
ASF_RUN_URL="${ASF_SERVER}/${ASF_SELF_REPO}/actions/runs/${ASF_RUN_ID}"
ASF_DEFAULT_BRANCH="${ALERT_SELF_FIX_DEFAULT_BRANCH:-main}"
ASF_STATE_DIR="${ALERT_SELF_FIX_STATE_DIR:-${ASF_TMP}/alert-self-fix}"
ASF_TREE="${ALERT_SELF_FIX_TREE_DIR:-${ASF_TMP}/alert-self-fix-tree}"
ASF_REMOTE_URL="${ALERT_SELF_FIX_REMOTE_URL:-${ASF_SERVER}/${ASF_SELF_REPO}.git}"
ASF_INBOX_BRANCH="ai/alert-inbox"
ASF_LABEL="ai:alert-self-fix"
ASF_DIAGNOSIS_MAX_BYTES=16384
export PYTHONDONTWRITEBYTECODE=1

ASF_FP=""
[ -f "${ASF_STATE_DIR}/fingerprint" ] && ASF_FP="$(tr -cd '0-9a-f' < "${ASF_STATE_DIR}/fingerprint" | head -c 64)"

_asf_log()
{
	local stage="$1"
	shift
	printf 'ALERT_SELF_FIX %s fp=%s %s\n' "${stage}" "${ASF_FP:0:12}" "$*"
}

_asf_output()
{
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s\n' "$1" >> "${GITHUB_OUTPUT}"
	fi
}

# _asf_fail STAGE OUTCOME REASON — record the failure for `escalate`, log it
# and stop the step (exit 1, so the workflow's escalate step runs).
_asf_fail()
{
	local stage="$1" outcome="$2" reason="$3"
	mkdir -p "${ASF_STATE_DIR}"
	printf '%s %s %s\n' "${stage}" "${outcome}" "${reason}" > "${ASF_STATE_DIR}/last"
	_asf_log "${stage}" "outcome=${outcome} reason=${reason}"
	exit 1
}

if ! [[ "${ASF_SELF_REPO}" =~ ^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$ ]] || ! [[ "${ASF_RUN_ID}" =~ ^[0-9]{1,20}$ ]]; then
	printf 'ALERT_SELF_FIX error reason=bad_run_context\n'
	exit 1
fi
if ! [[ "${ASF_DEFAULT_BRANCH}" =~ ^[A-Za-z0-9._/-]{1,100}$ ]] || [[ "${ASF_DEFAULT_BRANCH}" == *..* ]]; then
	printf 'ALERT_SELF_FIX error reason=bad_default_branch\n'
	exit 1
fi

# _asf_git_auth TOKEN ARGS — git with the token as an extra header through
# GIT_CONFIG_* (never on argv, never logged) when the remote is https.
_asf_git_auth()
{
	local token="$1"
	shift
	if [ -n "${token}" ] && [[ "${ASF_REMOTE_URL}" == https://* ]]; then
		GIT_CONFIG_COUNT=1 \
		GIT_CONFIG_KEY_0="http.${ASF_SERVER}/.extraheader" \
		GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "${token}" | base64 | tr -d '\n')" \
			git "$@"
	else
		git "$@"
	fi
}

_asf_state()
{
	local name="$1"
	[ -f "${ASF_STATE_DIR}/${name}" ] || return 1
	head -n 1 "${ASF_STATE_DIR}/${name}"
}

# --- prepare -------------------------------------------------------------------

asf_prepare()
{
	local fp="${ALERT_SELF_FIX_INPUT_FINGERPRINT:-}" diagnosis="${ALERT_SELF_FIX_INPUT_DIAGNOSIS:-}"
	local head="" pinned="${GITHUB_SHA:-}" prs="" branch="" heads="" rc=0
	rm -rf -- "${ASF_STATE_DIR}"
	mkdir -p "${ASF_STATE_DIR}"
	_asf_output "proceed=false"
	if ! [[ "${fp}" =~ ^[0-9a-f]{64}$ ]]; then
		ASF_FP=""
		_asf_fail prepare refused bad_fingerprint
	fi
	ASF_FP="${fp}"
	printf '%s\n' "${fp}" > "${ASF_STATE_DIR}/fingerprint"
	if [ "${GITHUB_REF:-}" != "refs/heads/${ASF_DEFAULT_BRANCH}" ]; then
		_asf_fail prepare refused not_default_branch
	fi
	head="$(git -C "${ASF_REPO_ROOT}" rev-parse HEAD 2>/dev/null)" || head=""
	if ! [[ "${pinned}" =~ ^[0-9a-f]{40}$ ]] || [ "${head}" != "${pinned}" ]; then
		_asf_fail prepare refused sha_mismatch
	fi
	# The diagnosis is model output: a JSON object of at most 16 KiB whose
	# fingerprint is the dispatched one. It is stored, never executed.
	if ! printf '%s' "${diagnosis}" | python3 -I -B -c '
import json, sys
raw = sys.stdin.buffer.read()
if len(raw) > int(sys.argv[1]):
	sys.exit(1)
data = json.loads(raw.decode("utf-8"))
if not isinstance(data, dict) or data.get("fingerprint") != sys.argv[2]:
	sys.exit(1)
json.dump(data, open(sys.argv[3], "w", encoding="utf-8"), sort_keys=True)
' "${ASF_DIAGNOSIS_MAX_BYTES}" "${fp}" "${ASF_STATE_DIR}/diagnosis.json" 2>/dev/null; then
		_asf_fail prepare refused bad_diagnosis
	fi
	# One list call: an open self-fix PR for this fingerprint means the work
	# is already in review. A failed list cannot tell, so it fails closed.
	if ! prs="$(GH_TOKEN="${GITHUB_TOKEN:-}" gh pr list --repo "${ASF_SELF_REPO}" --label "${ASF_LABEL}" --state open --limit 100 --json number,body 2>/dev/null)"; then
		_asf_fail prepare failed pr_list_failed
	fi
	if jq -e --arg fp "${fp}" 'any(.[]; (.body // "") | test("<!--\\s*alert-triage:fp=" + $fp + "\\s*-->"))' <<< "${prs}" >/dev/null 2>&1; then
		printf 'prepare skipped already_open\n' > "${ASF_STATE_DIR}/last"
		_asf_log prepare "outcome=skipped reason=already_open"
		_asf_output "escalate=false"
		return 0
	fi
	branch="ai/alert-self-fix-${fp:0:8}-${ASF_RUN_ID}"
	heads="$(_asf_git_auth "${GITHUB_TOKEN:-}" -C "${ASF_REPO_ROOT}" ls-remote --heads "${ASF_REMOTE_URL}" "refs/heads/${branch}" 2>/dev/null)" || rc=$?
	if [ "${rc}" -ne 0 ]; then
		_asf_fail prepare failed ls_remote
	fi
	if [ -n "${heads}" ]; then
		_asf_fail prepare refused branch_exists
	fi
	# The worktree lives under RUNNER_TEMP and belongs to this job only.
	rm -rf -- "${ASF_TREE}"
	if ! git -C "${ASF_REPO_ROOT}" worktree add -q -b "${branch}" "${ASF_TREE}" "${pinned}" >/dev/null 2>&1; then
		_asf_fail prepare failed worktree
	fi
	printf '%s\n' "${branch}" > "${ASF_STATE_DIR}/branch"
	printf '%s\n' "${pinned}" > "${ASF_STATE_DIR}/base_sha"
	_asf_log prepare "outcome=ok branch=${branch}"
	_asf_output "proceed=true"
	_asf_output "branch=${branch}"
	_asf_output "escalate=false"
}

# --- edit ------------------------------------------------------------------------

_asf_compose_prompt()
{
	local out="$1" template="${ASF_REPO_ROOT}/prompts/mode-implement.txt"
	[ -f "${template}" ] || return 1
	python3 -I -B - "${template}" "${ASF_STATE_DIR}/diagnosis.json" "${out}" <<'PY'
import json
import re
import sys

template, diagnosis_path, out = sys.argv[1:4]
text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "", open(template, encoding="utf-8").read())
diagnosis = json.load(open(diagnosis_path, encoding="utf-8"))


def scalar(item):
	# alert_triage.py emits each decision as {question, chosen, reason}.
	if isinstance(item, dict):
		parts = [str(item.get(key)) if isinstance(item.get(key), (str, int, float)) else "" for key in ("question", "chosen", "reason")]
		return f"{parts[0]} -> {parts[1]}" + (f" ({parts[2]})" if parts[2] else "")
	return str(item) if isinstance(item, (str, int, float)) else ""


def defang(text):
	# A diagnosis line starting with `===` cannot close the UNTRUSTED fence early.
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	return re.sub(r"(?m)^[ \t]*===", "= ==", text)


def field(name):
	value = diagnosis.get(name)
	if isinstance(value, list):
		return defang("\n".join(f"- {text}" for text in (scalar(item) for item in value) if text))
	return defang(str(value)) if isinstance(value, (str, int, float)) else ""


plan = "\n".join([
	"",
	"=== IMPLEMENTATION TASK ===",
	"",
	"Fix the pipeline-blocking failure the alert triage diagnosed below. This is",
	"the automation's own pipeline (alert self-fix, alert triage plan Phase 3).",
	"Make the smallest safe change that removes the failure; do not touch",
	".github/ai/**, .claude/**, CLAUDE.md, unattended_system_instructions.md or",
	"tools/**. Update or add the contract tests that cover the change.",
	"",
	"=== BEGIN UNTRUSTED DIAGNOSIS ===",
	"Summary:",
	field("summary"),
	"",
	"Proposed fix:",
	field("proposed_fix"),
	"",
	"Files:",
	field("files"),
	"",
	"Decisions:",
	field("decisions"),
	"=== END UNTRUSTED DIAGNOSIS ===",
	"",
])
with open(out, "w", encoding="utf-8") as handle:
	handle.write(text.rstrip("\n") + "\n" + plan)
PY
}

asf_edit()
{
	local engine_root="${CLAUDE_ENGINE_SUPPORT_DIR:-}" prompt="${ASF_STATE_DIR}/prompt.md" out="${ASF_STATE_DIR}/editor_output.txt"
	local wall="${ALERT_SELF_FIX_EDITOR_TIMEOUT_SECS:-3600}" model="${ALERT_SELF_FIX_MODEL:-openai/gpt-6-sol}" rc=0 engine="claude"
	[ -d "${ASF_TREE}" ] || _asf_fail edit failed no_worktree
	[[ "${wall}" =~ ^[0-9]{1,5}$ ]] && [ "$((10#${wall}))" -ge 60 ] || wall=3600
	wall=$((10#${wall}))
	[[ "${model}" =~ ^[a-zA-Z0-9/_.:-]+$ ]] || model="openai/gpt-6-sol"
	_asf_compose_prompt "${prompt}" || _asf_fail edit failed prompt
	if [ -z "${engine_root}" ] || [ ! -f "${engine_root}/scripts/ai_engine.sh" ] || [ -L "${engine_root}/scripts/ai_engine.sh" ]; then
		# No trusted engine support: the trusted checkout's own helpers run
		# the codex editor.
		engine_root="${ASF_REPO_ROOT}"
		engine="codex"
	fi
	if [ "${engine}" = "claude" ]; then
		rc=0
		(
			# The engine never gets a GitHub token, the OIDC request
			# variables or the Telegram secret.
			unset GH_TOKEN GITHUB_TOKEN GH_PAT TG_BOT_SECRET ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_ID_TOKEN_REQUEST_URL
			# Pipeline fixes may need to touch .github/workflows.
			export ALLOW_WORKFLOW_EDITS=true
			# shellcheck disable=SC2016  # $0 / $@ expand in the child bash.
			timeout -k 30 "${wall}" bash -c '
				source "$0" || exit 2
				engine="$(SUPPORT_ROOT_DIR="$1" ai_engine_for_role IMPLEMENT)" || engine="codex"
				[ "${engine}" = "claude" ] || exit 75
				claude_run IMPLEMENT "$2" "$3" "$4"
			' "${engine_root}/scripts/ai_engine.sh" "${engine_root}" "${prompt}" "${out}" "${ASF_TREE}"
		) >/dev/null 2>&1 || rc=$?
		case "${rc}" in
			0) ;;
			75) engine="codex" ;;
			124|137) _asf_fail edit failed timeout ;;
			*) _asf_fail edit failed "engine_exit_${rc}" ;;
		esac
	fi
	if [ "${engine}" = "codex" ]; then
		rc=0
		(
			unset GH_TOKEN GITHUB_TOKEN GH_PAT TG_BOT_SECRET ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_ID_TOKEN_REQUEST_URL
			timeout -k 30 "${wall}" bash "${engine_root}/scripts/codex_isolated_exec.sh" run --mode workspace --workdir "${ASF_TREE}" -- \
				--ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true \
				exec --skip-git-repo-check --model "${model}" --sandbox danger-full-access \
				< "${prompt}" > "${out}"
		) 2>/dev/null || rc=$?
		case "${rc}" in
			0) ;;
			124|137) _asf_fail edit failed codex_timeout ;;
			*) _asf_fail edit failed "codex_exit_${rc}" ;;
		esac
	fi
	printf '%s\n' "${engine}" > "${ASF_STATE_DIR}/engine"
	if [ -z "$(git -C "${ASF_TREE}" status --porcelain 2>/dev/null)" ]; then
		_asf_fail edit failed no_change
	fi
	_asf_log edit "outcome=ok engine=${engine}"
}

# --- verify ------------------------------------------------------------------------

asf_verify()
{
	local base="" changed="${ASF_STATE_DIR}/changed.txt" path="" module="" wrapper="${ASF_REPO_ROOT}/scripts/prepush_test_gate_run.sh"
	local minutes="${ALERT_SELF_FIX_GATE_TIMEOUT_MINUTES:-20}" image="" status="" exec_helper="${ASF_REPO_ROOT}/scripts/codex_isolated_exec.sh"
	local -a yaml_lint_files=() workflow_lint_files=()
	local lint_skipped=""
	base="$(_asf_state base_sha)" || _asf_fail verify failed no_state
	[ -d "${ASF_TREE}" ] || _asf_fail verify failed no_worktree
	[[ "${minutes}" =~ ^[0-9]{1,3}$ ]] && [ "$((10#${minutes}))" -ge 1 ] || minutes=20
	minutes=$((10#${minutes}))
	git -C "${ASF_TREE}" add -A >/dev/null 2>&1 || _asf_fail verify failed stage
	git -C "${ASF_TREE}" diff --cached --name-only --diff-filter=d "${base}" > "${changed}" 2>/dev/null || _asf_fail verify failed diff
	# Host-side syntax checks: parse, never execute.
	while IFS= read -r path; do
		[ -n "${path}" ] || continue
		if [ -L "${ASF_TREE}/${path}" ]; then
			_asf_fail verify failed symlink
		fi
		# The edited tree cannot bring its own lint configuration.
		if [[ "${path}" =~ ^(\.yamllint(\.ya?ml)?|\.github/actionlint\.ya?ml)$ ]]; then
			_asf_fail verify failed lint_config
		fi
		# The file sets ci.yml's "YAML lint" and actionlint steps cover.
		if [[ "${path}" =~ ^(\.github/workflows/[^/]+\.yml|\.github/actions/[^/]+/action\.yml|workflow-templates/[^/]+\.yml)$ ]]; then
			yaml_lint_files+=("${path}")
		fi
		if [[ "${path}" =~ ^(\.github/workflows/[^/]+\.yml|workflow-templates/[^/]+\.yml)$ ]]; then
			workflow_lint_files+=("${path}")
		fi
		case "${path}" in
			*.sh)
				bash -n "${ASF_TREE}/${path}" >/dev/null 2>&1 || _asf_fail verify failed syntax_sh
				;;
			*.py)
				python3 -I -B -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' "${ASF_TREE}/${path}" >/dev/null 2>&1 \
					|| _asf_fail verify failed syntax_py
				;;
			*.yml|*.yaml)
				python3 -I -B -c 'import sys, yaml; yaml.safe_load(open(sys.argv[1], encoding="utf-8"))' "${ASF_TREE}/${path}" >/dev/null 2>&1 \
					|| _asf_fail verify failed syntax_yaml
				;;
		esac
	done < "${changed}"
	# The static checks of ci.yml the pre-push gate does not run (it covers
	# ruff and shellcheck): `yamllint -s` with the default configuration and
	# `actionlint -shellcheck=`. Both only parse. A tool missing on the runner
	# is logged (lint_skipped=) and skipped; ci.yml still gates the merge.
	if [ "${#yaml_lint_files[@]}" -gt 0 ]; then
		if command -v yamllint >/dev/null 2>&1; then
			(cd "${ASF_TREE}" && yamllint -s -d default -- "${yaml_lint_files[@]}") >/dev/null 2>&1 \
				|| _asf_fail verify failed yamllint
		else
			lint_skipped="yamllint"
		fi
	fi
	if [ "${#workflow_lint_files[@]}" -gt 0 ]; then
		if command -v actionlint >/dev/null 2>&1; then
			(cd "${ASF_TREE}" && actionlint -shellcheck= "${workflow_lint_files[@]}") >/dev/null 2>&1 \
				|| _asf_fail verify failed actionlint
		else
			lint_skipped="${lint_skipped:+${lint_skipped},}actionlint"
		fi
	fi
	# The gate module and wrapper come from the trusted checkout, never from
	# the edited tree (the wrapper also refuses a module inside it).
	for module in "${ASF_REPO_ROOT}/.claude/hooks/prepush_affected_tests.py" "${ASF_REPO_ROOT}/workflow-templates/.claude/hooks/prepush_affected_tests.py"; do
		[ -f "${module}" ] && [ ! -L "${module}" ] && break
		module=""
	done
	if [ -z "${module}" ] || [ ! -f "${wrapper}" ] || [ ! -f "${exec_helper}" ]; then
		_asf_fail verify failed gate_missing
	fi
	image="$(env -i "PATH=${PATH}" "HOME=${HOME:-/tmp}" "RUNNER_TEMP=${ASF_TMP}" PYTHONDONTWRITEBYTECODE=1 bash "${exec_helper}" image </dev/null 2>/dev/null | tail -n 1)" || image=""
	[[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]] || _asf_fail verify failed image_unavailable
	rm -rf -- "${ASF_STATE_DIR}/gate"
	env -i "PATH=${PATH}" "HOME=${HOME:-/tmp}" "RUNNER_TEMP=${ASF_TMP}" PYTHONDONTWRITEBYTECODE=1 \
		bash "${wrapper}" --worktree "${ASF_TREE}" --module "${module}" --image "${image}" --base "${base}" \
		--out-dir "${ASF_STATE_DIR}/gate" --timeout-minutes "${minutes}" </dev/null >/dev/null 2>&1 || true
	status="$(python3 -I -B -c '
import json, sys
try:
	data = json.load(open(sys.argv[1], encoding="utf-8"))
	status = data.get("status") if isinstance(data, dict) else None
except Exception:
	status = None
print(status if status in ("pass", "fail", "unverified", "skipped", "timeout") else "invalid")
' "${ASF_STATE_DIR}/gate/result.json" 2>/dev/null)" || status="invalid"
	case "${status}" in
		pass|skipped) ;;
		*) _asf_fail verify failed "gate_${status}" ;;
	esac
	_asf_log verify "outcome=ok gate=${status} files=$(grep -c . "${changed}" || true)${lint_skipped:+ lint_skipped=${lint_skipped}}"
}

# --- ledger-put --------------------------------------------------------------------

asf_ledger_put()
{
	local path="${1:-}" file="${2:-}" body="" err="" attempt=0
	if ! [[ "${path}" =~ ^(self_fix|break_glass)/[A-Za-z0-9._-]{1,120}\.json$ ]] || [[ "${path}" == *..* ]] || [ ! -f "${file}" ]; then
		_asf_log ledger "outcome=failed reason=usage"
		return 1
	fi
	body="$(mktemp "${ASF_TMP}/alert-self-fix-ledger-XXXXXX")"
	err="${body}.err"
	jq -n --arg message "alert-self-fix: ${path} (run ${ASF_RUN_ID})" --arg branch "${ASF_INBOX_BRANCH}" \
		--arg content "$(base64 < "${file}" | tr -d '\n')" \
		'{message: $message, branch: $branch, content: $content}' > "${body}"
	while [ "${attempt}" -lt 2 ]; do
		attempt=$((attempt + 1))
		if GH_TOKEN="${GITHUB_TOKEN:-}" gh api -X PUT "repos/${ASF_SELF_REPO}/contents/${path}" --input "${body}" >/dev/null 2>"${err}"; then
			rm -f -- "${body}" "${err}"
			_asf_log ledger "outcome=created path=${path}"
			return 0
		fi
		# 422 without a sha: the path already exists (contents API create).
		if grep -q 'HTTP 422' "${err}" && grep -qi 'sha' "${err}"; then
			rm -f -- "${body}" "${err}"
			_asf_log ledger "outcome=duplicate path=${path}"
			return 3
		fi
		grep -q 'HTTP 409' "${err}" || break
	done
	rm -f -- "${body}" "${err}"
	_asf_log ledger "outcome=failed path=${path}"
	return 1
}

# --- publish -------------------------------------------------------------------------

_asf_compose_body()
{
	local out="$1"
	python3 -I -B - "${ASF_STATE_DIR}/diagnosis.json" "${out}" "${ASF_FP}" "${ASF_RUN_ID}" "${ASF_RUN_URL}" "${ASF_SELF_REPO}" "$(_asf_state engine || echo unknown)" <<'PY'
import json
import re
import sys

diagnosis_path, out, fp, run_id, run_url, repo, engine = sys.argv[1:8]
diagnosis = json.load(open(diagnosis_path, encoding="utf-8"))


def clean(value, limit=4000):
	# Model text: no backticks (cannot break the fence), no HTML comments
	# (cannot forge markers), no `#<n>` issue references and no linkable
	# URLs (a URL-form `fixes https://github.com/o/r/issues/<n>` would close
	# a non-tracking issue on the squash merge; the lint blocks only tracking
	# issues). The trusted `Refs:` lines never pass through clean().
	text = str(value) if isinstance(value, (str, int, float)) else ""
	# Control characters go first: stripped after the defangs, one inside
	# `https:\x01//`, `#\x0112` or `<!\x01--` would rebuild the token.
	text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
	text = text.replace("`", "'").replace("<!--", "<! --").replace("-->", "-- >")
	text = re.sub(r"#(?=[0-9])", "# ", text)
	text = re.sub(r"(?i)\b(https?):/{2}", r"\1:/ /", text)
	text = re.sub(r"(?i)\bwww\.", "www .", text)
	return text[:limit]


def as_text(item):
	# alert_triage.py emits each decision as {question, chosen, reason}.
	if isinstance(item, dict):
		parts = [str(item.get(key)) if isinstance(item.get(key), (str, int, float)) else "" for key in ("question", "chosen", "reason")]
		return f"{parts[0]} -> {parts[1]}" + (f" ({parts[2]})" if parts[2] else "")
	return item


def items(name):
	value = diagnosis.get(name)
	return [clean(as_text(item), 300) for item in value[:50]] if isinstance(value, list) else []


run_re = re.compile(r"\Ahttps://github\.com/" + re.escape(repo) + r"/actions/runs/[0-9]+(/job/[0-9]+)?\Z")
refs = [run_url] + [item for item in diagnosis.get("evidence", []) if isinstance(item, str) and run_re.fullmatch(item)][:10]
decisions = items("decisions")
lines = [
	f"<!-- alert-triage:fp={fp} -->",
	f"<!-- alert-self-fix:run_id={run_id} -->",
	"",
	f"Alert self-fix for alert group `{fp[:12]}`, opened by the alert self-fix workflow (alert triage plan Phase 3).",
	"",
	"## Diagnosis",
	"",
	"```text",
	clean(diagnosis.get("summary")),
	"```",
	"",
	"## Proposed fix",
	"",
	"```text",
	clean(diagnosis.get("proposed_fix")),
	"```",
	"",
	"## Files named by the diagnosis",
	"",
	"```text",
	*(items("files") or ["(none)"]),
	"```",
	"",
	"## Auto-decisions",
	"",
	"| # | Decision |",
	"|---|---|",
	f"| 1 | Engine: {engine}; verified by the credential-free pre-push affected-test gate before this PR opened. |",
	"| 2 | Auto-merge enabled (squash, head commit pinned); the normal review pipeline still gates the merge. |",
	*(f"| {index} | {' '.join(decision.split()).replace('|', '/')} |" for index, decision in enumerate(decisions, start=3)),
	"",
	*(f"Refs: {ref}" for ref in refs),
	"",
]
with open(out, "w", encoding="utf-8") as handle:
	handle.write("\n".join(lines))
PY
}

asf_publish()
{
	local branch="" base="" engine="" head="" body="${ASF_STATE_DIR}/pr_body.md" url="" pr="" pushed_at="" record="${ASF_STATE_DIR}/self_fix_record.json"
	local author="codex-bot" lint_rc=0 put_rc=0 failed_reason=""
	branch="$(_asf_state branch)" || _asf_fail publish failed no_state
	base="$(_asf_state base_sha)" || _asf_fail publish failed no_state
	engine="$(_asf_state engine)" || engine="codex"
	[ "${engine}" = "claude" ] && author="claude-bot"
	[ -d "${ASF_TREE}" ] || _asf_fail publish failed no_worktree
	git -C "${ASF_TREE}" add -A >/dev/null 2>&1 || _asf_fail publish failed stage
	if ! git -C "${ASF_TREE}" -c user.name="${author}" -c user.email="${author}@users.noreply.invalid" \
		commit -q --no-verify -m "alert-self-fix: group ${ASF_FP:0:12} (run ${ASF_RUN_ID})" >/dev/null 2>&1; then
		_asf_fail publish failed commit
	fi
	head="$(git -C "${ASF_TREE}" rev-parse HEAD)"
	_asf_compose_body "${body}" || _asf_fail publish failed body
	# The lint runs before the push: a blocked body leaves no branch behind.
	python3 -I -B "${ASF_REPO_ROOT}/scripts/lint_pr_body_auto_close.py" --pr-body-file "${body}" --repo "${ASF_SELF_REPO}" >/dev/null 2>&1 || lint_rc=$?
	if [ "${lint_rc}" -ne 0 ]; then
		_asf_fail publish lint_blocked "lint_exit_${lint_rc}"
	fi
	# Non-force push of a branch prepare proved absent.
	if ! _asf_git_auth "${GH_TOKEN:-}" -C "${ASF_TREE}" push -q "${ASF_REMOTE_URL}" "HEAD:refs/heads/${branch}" >/dev/null 2>&1; then
		_asf_fail publish failed push
	fi
	pushed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	_asf_log publish "outcome=pushed branch=${branch} head=${head:0:12}"
	if ! url="$(gh pr create --repo "${ASF_SELF_REPO}" --base "${ASF_DEFAULT_BRANCH}" --head "${branch}" \
		--title "alert-self-fix: pipeline fix for alert group ${ASF_FP:0:12}" --body-file "${body}" --label "${ASF_LABEL}" 2>/dev/null)"; then
		_asf_fail publish failed pr_create
	fi
	pr="$(printf '%s\n' "${url}" | sed -n 's#^.*/pull/\([0-9]\{1,10\}\)$#\1#p' | tail -n 1)"
	[[ "${pr}" =~ ^[0-9]{1,10}$ ]] || _asf_fail publish failed pr_number
	printf '%s\n' "${pr}" > "${ASF_STATE_DIR}/pr"
	_asf_log publish "outcome=opened pr=${pr}"
	# The ledger record proves to the break-glass scan that this workflow
	# opened the PR and pushed exactly this head (plan D4).
	jq -n --argjson pr "${pr}" --arg branch "${branch}" --arg head "${head}" --arg base "${base}" \
		--arg run_id "${ASF_RUN_ID}" --arg fp "${ASF_FP}" --arg pushed_at "${pushed_at}" \
		--slurpfile diagnosis "${ASF_STATE_DIR}/diagnosis.json" \
		'{schema: "alert_self_fix_record.v1", pr: $pr, branch: $branch, head_sha: $head, base_sha: $base,
		  run_id: $run_id, fingerprint: $fp, pushed_at: $pushed_at, diagnosis: $diagnosis[0]}' > "${record}"
	asf_ledger_put "self_fix/pr-${pr}.json" "${record}" || put_rc=$?
	[ "${put_rc}" -eq 0 ] || failed_reason="ledger_failed"
	if GH_TOKEN="${GH_TOKEN:-}" gh pr merge "${pr}" --repo "${ASF_SELF_REPO}" --auto --squash --match-head-commit "${head}" >/dev/null 2>&1; then
		_asf_log automerge "outcome=enabled pr=${pr}"
	else
		_asf_log automerge "outcome=failed pr=${pr}"
		echo "::warning::Alert self-fix PR ${pr}: auto-merge could not be enabled; it stays open for normal review."
		failed_reason="${failed_reason:-automerge_failed}"
	fi
	if [ -n "${failed_reason}" ]; then
		# The PR stays open for normal review; escalate.
		_asf_fail publish degraded "${failed_reason}"
	fi
	_asf_log publish "outcome=ok pr=${pr}"
}

# --- escalate ------------------------------------------------------------------------

asf_escalate()
{
	local stage="unknown" outcome="unknown" reason="unknown" msg_id="" text=""
	if [ -f "${ASF_STATE_DIR}/last" ]; then
		read -r stage outcome reason < "${ASF_STATE_DIR}/last" || true
	fi
	stage="$(printf '%s' "${stage}" | tr -cd 'a-z_' | head -c 20)"
	outcome="$(printf '%s' "${outcome}" | tr -cd 'a-z_' | head -c 30)"
	reason="$(printf '%s' "${reason}" | tr -cd 'a-z0-9_' | head -c 40)"
	if [ "${outcome}" = "skipped" ]; then
		_asf_log escalate "outcome=not_needed"
		return 0
	fi
	text="Alert self-fix needs attention: group ${ASF_FP:0:12}, stage ${stage:-unknown}, outcome ${outcome:-unknown}, reason ${reason:-unknown}.
Run: ${ASF_RUN_URL}"
	if [ -f "${ASF_SCRIPT_DIR}/tg_helpers.sh" ]; then
		msg_id="$(
			# shellcheck disable=SC1091
			source "${ASF_SCRIPT_DIR}/tg_helpers.sh" && tg_send_msg_direct "${text}" "ERROR" 2>/dev/null
		)" || msg_id=""
	fi
	if [ -n "${msg_id}" ]; then
		_asf_log escalate "outcome=sent stage=${stage} reason=${reason}"
	else
		_asf_log escalate "outcome=failed stage=${stage} reason=${reason}"
	fi
}

case "${1:-}" in
	prepare) asf_prepare ;;
	edit) asf_edit ;;
	verify) asf_verify ;;
	publish) asf_publish ;;
	ledger-put)
		shift
		asf_ledger_put "${1:-}" "${2:-}"
		;;
	escalate) asf_escalate ;;
	*)
		echo "usage: alert_self_fix.sh prepare|edit|verify|publish|ledger-put PATH FILE|escalate" >&2
		exit 2
		;;
esac
