#!/usr/bin/env bash
# protected_path_gate.sh — owner-authorization gate for automated PR merges
# (security finding `template-variant-authorization-bypass`, issue #4919).
#
# Sourced by every script that runs `gh pr merge` unattended
# (review_enable_auto_merge.sh, review_rb_judge.sh,
# orchestrate_poll_process.sh). Each such call is prefixed with
# `protected_path_guarded_merge`, e.g.
#
#   protected_path_guarded_merge gh_retry gh pr merge "${PR}" --repo "${REPO}" --squash --auto
#
# The wrapper finds the PR number (the token after `pr merge`), `--repo`
# (default: GITHUB_REPOSITORY), and `--match-head-commit`, and asks
# protected_path_authorization.py whether the merge may proceed:
#
#   - a PR that changes no protected-equivalent path (`.claude/**`,
#     `workflow-templates/.claude/**`, tests/test_claude_template_parity.py,
#     and the gate's own two files) runs the command unchanged, except that a
#     call without `--match-head-commit` gets the head the check read, so a
#     push between the check and the merge cannot slip a protected path in
#     (a PR whose head SHA cannot be read is refused, protected or not);
#   - a protected PR runs only when the repository owner authorized its
#     current head with an `/authorize-protected-paths <sha>` comment; the
#     command then gets `--match-head-commit <sha>` when it has none, so a
#     later push cannot ride on the approval;
#   - otherwise the command is not run: one instruction comment per head is
#     posted, a pending auto-merge on the PR is turned off (GitHub keeps
#     auto-merge across pushes by anyone with write access, so one enabled
#     on an earlier, unprotected head would land this one), a warning is
#     logged (an error annotation when turning auto-merge off failed), and
#     the wrapper returns 3. A call bound to an older head is refused too,
#     and the PR's current head decides the auto-merge. A failed read,
#     a missing checker, or an unparseable call also returns 3 (fail closed).
#
# Callers put the PR number right after `pr merge`. A flag first
# (`gh pr merge --repo o/r 42`) is refused rather than guessed at: skipping
# flags would need every value-taking `gh pr merge` flag, and a wrong guess
# would check a different PR than the one merged.
# tests/test_protected_path_authorization.py fails CI on such a call.
#
# Every caller already treats a non-zero merge as "not merged", so a block
# takes the caller's existing failure branch.
#
# GitHub API budget (CLAUDE.md §15): the checker's reads (one PR read, one
# files page per 100 files; comments only for a protected PR). The last
# decision is cached per repo, PR, and requested head for the life of the
# shell, so an `A || B` fallback pair checks once.
#
# Logs go to stderr as `PROTECTED_PATH_GATE pr=… head=… decision=… reason=…`.

PROTECTED_PATH_GATE_DIR="${PROTECTED_PATH_GATE_DIR:-$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)}"
PROTECTED_PATH_GATE_CHECKER="${PROTECTED_PATH_GATE_CHECKER:-${PROTECTED_PATH_GATE_DIR}/protected_path_authorization.py}"
_PROTECTED_PATH_GATE_CACHE_KEY=""
_PROTECTED_PATH_GATE_CACHE_RC=""
_PROTECTED_PATH_GATE_CACHE_JSON=""

protected_path_guarded_merge()
{
	local -a _ppg_cmd=("$@")
	local _ppg_pr="" _ppg_repo="" _ppg_head="" _ppg_prev="" _ppg_prev2="" _ppg_arg
	for _ppg_arg in "${_ppg_cmd[@]}"; do
		case "${_ppg_prev}" in
			--repo|-R) _ppg_repo="${_ppg_arg}" ;;
			--match-head-commit) _ppg_head="${_ppg_arg}" ;;
		esac
		case "${_ppg_arg}" in
			--repo=*) _ppg_repo="${_ppg_arg#--repo=}" ;;
			--match-head-commit=*) _ppg_head="${_ppg_arg#--match-head-commit=}" ;;
		esac
		if [ "${_ppg_prev2}" = "pr" ] && [ "${_ppg_prev}" = "merge" ] && [ -z "${_ppg_pr}" ]; then
			_ppg_pr="${_ppg_arg}"
		fi
		_ppg_prev2="${_ppg_prev}"
		_ppg_prev="${_ppg_arg}"
	done
	_ppg_repo="${_ppg_repo:-${GITHUB_REPOSITORY:-}}"
	if ! [[ "${_ppg_pr}" =~ ^[1-9][0-9]*$ ]] || [ -z "${_ppg_repo}" ]; then
		echo "::warning::protected-path gate could not parse the PR number or repository from the merge call; refusing the merge (fail closed)." >&2
		echo "PROTECTED_PATH_GATE pr=${_ppg_pr:-unknown} head=${_ppg_head:-none} decision=block reason=unparseable_merge_call" >&2
		return 3
	fi
	if [ -n "${_ppg_head}" ] && ! [[ "${_ppg_head}" =~ ^[0-9a-f]{40}$ ]]; then
		echo "::warning::protected-path gate: PR #${_ppg_pr} merge is bound to a malformed head '${_ppg_head}'; refusing the merge (fail closed)." >&2
		echo "PROTECTED_PATH_GATE pr=${_ppg_pr} head=${_ppg_head} decision=block reason=malformed_head" >&2
		return 3
	fi

	local _ppg_key="${_ppg_repo}#${_ppg_pr}@${_ppg_head}" _ppg_json="" _ppg_rc=0
	if [ -n "${_PROTECTED_PATH_GATE_CACHE_KEY}" ] && [ "${_PROTECTED_PATH_GATE_CACHE_KEY}" = "${_ppg_key}" ]; then
		_ppg_json="${_PROTECTED_PATH_GATE_CACHE_JSON}"
		_ppg_rc="${_PROTECTED_PATH_GATE_CACHE_RC}"
	else
		local -a _ppg_check=(python3 "${PROTECTED_PATH_GATE_CHECKER}" pr --repo "${_ppg_repo}" --pr "${_ppg_pr}" --post-instructions --disable-auto-merge)
		if [ -n "${_ppg_head}" ]; then
			_ppg_check+=(--head "${_ppg_head}")
		fi
		if [ ! -f "${PROTECTED_PATH_GATE_CHECKER}" ]; then
			_ppg_json='{"decision":"block","error":"protected_path_authorization.py not found"}'
			_ppg_rc=2
		else
			_ppg_json="$(PYTHONDONTWRITEBYTECODE=1 "${_ppg_check[@]}" 2>/dev/null)" || _ppg_rc=$?
		fi
		_PROTECTED_PATH_GATE_CACHE_KEY="${_ppg_key}"
		_PROTECTED_PATH_GATE_CACHE_RC="${_ppg_rc}"
		_PROTECTED_PATH_GATE_CACHE_JSON="${_ppg_json}"
	fi

	local _ppg_decision _ppg_protected _ppg_authorized_head _ppg_reason _ppg_auto_merge
	_ppg_decision="$(printf '%s' "${_ppg_json}" | jq -r '.decision // "block"' 2>/dev/null || echo "block")"
	_ppg_protected="$(printf '%s' "${_ppg_json}" | jq -r 'if .protected == false then "false" else "true" end' 2>/dev/null || echo "true")"
	_ppg_authorized_head="$(printf '%s' "${_ppg_json}" | jq -r '.head // ""' 2>/dev/null || echo "")"
	_ppg_reason="$(printf '%s' "${_ppg_json}" | jq -r '.reason // .error // "no decision"' 2>/dev/null || echo "no decision")"
	_ppg_auto_merge="$(printf '%s' "${_ppg_json}" | jq -r '.auto_merge // "not checked"' 2>/dev/null || echo "not checked")"

	if [ "${_ppg_rc}" != "0" ] || [ "${_ppg_decision}" != "allow" ]; then
		echo "::warning::protected-path gate refused the merge of PR #${_ppg_pr}: ${_ppg_reason}. The repository owner must post '/authorize-protected-paths <head sha>' on the PR (issue #4919)." >&2
		echo "PROTECTED_PATH_GATE pr=${_ppg_pr} head=${_ppg_authorized_head:-${_ppg_head:-unknown}} decision=block rc=${_ppg_rc} auto_merge=${_ppg_auto_merge} reason=${_ppg_reason}" >&2
		case "${_ppg_auto_merge}" in
			"disable failed"*)
				# Refusing this call does not stop GitHub: the pending
				# auto-merge still lands the unauthorized head once checks pass.
				echo "::error::protected-path gate could not turn off the pending auto-merge on PR #${_ppg_pr} (${_ppg_auto_merge}); GitHub can still merge the unauthorized head once its checks pass. Turn auto-merge off on the PR, or authorize the head (issue #4919)." >&2
				;;
		esac
		return 3
	fi
	if [ "${_ppg_protected}" = "true" ]; then
		if ! [[ "${_ppg_authorized_head}" =~ ^[0-9a-f]{40}$ ]]; then
			echo "PROTECTED_PATH_GATE pr=${_ppg_pr} head=unknown decision=block reason=authorized_head_missing" >&2
			return 3
		fi
		if [ -z "${_ppg_head}" ]; then
			_ppg_cmd+=(--match-head-commit "${_ppg_authorized_head}")
		fi
		echo "PROTECTED_PATH_GATE pr=${_ppg_pr} head=${_ppg_authorized_head} decision=allow protected=true reason=${_ppg_reason}" >&2
	elif [ -z "${_ppg_head}" ]; then
		# Bind an unprotected merge to the head whose files were checked, so a
		# push that adds a protected path between the check and the merge
		# fails the merge instead of riding on this decision. Without that
		# head the merge cannot be bound, so it is refused (fail closed).
		if ! [[ "${_ppg_authorized_head}" =~ ^[0-9a-f]{40}$ ]]; then
			echo "PROTECTED_PATH_GATE pr=${_ppg_pr} head=unknown decision=block reason=checked_head_missing" >&2
			return 3
		fi
		_ppg_cmd+=(--match-head-commit "${_ppg_authorized_head}")
	fi
	"${_ppg_cmd[@]}"
}
