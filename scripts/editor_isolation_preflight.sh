#!/usr/bin/env bash
# Editor isolation preflight — sourced (never executed) by
# scripts/review_run_reviewers.sh and scripts/review_apply_fixes.sh.
#
# Why this exists
# ---------------
# The review editor launches as EDITOR_ISOLATION_USER (default `nobody`)
# under `sudo -n -u <user> env -i ...`. Review runs 34304993091 and
# 34320556598 on PR #4057 each burned three editor attempts on
#
#   opencode-editor: line 1: .../scripts/opencode_helpers.sh: Permission denied
#
# after 1.5-2.5 hours of reviewer fan-out, and nothing in the log named the
# path component the unprivileged identity could not traverse. This helper
# probes every path the isolated editor must reach *as that identity* and,
# on denial, walks the path from `/` downward and reports the first
# component that identity cannot traverse or read, with its mode and owner.
#
# Two entry points are shared by both callers:
#
#   editor_isolation_prepare_shared_paths
#     Grants traverse/read on the runner-owned, secret-free paths the
#     isolated editor needs: `o+rX` on the staged support bundle
#     (SUPPORT_ROOT_DIR: scripts, prompts, ai-memory schemas — all committed
#     repository content) and 0711 on RUNTIME_DIR (traverse only, no
#     listing; matches the `chmod 0711 "${RUNTIME_DIR}"` the workflow
#     definition applies once it runs from this commit). Idempotent. Never
#     touches GITHUB_WORKSPACE, the source checkout, `.git`, or the runner
#     command-file directory — those stay closed to the editor identity.
#
#   editor_isolation_preflight_probe
#     Returns 0 when every required path is reachable as the isolation
#     identity and prints one `EDITOR_ISOLATION_PREFLIGHT_OK` line.
#     Returns 1 otherwise and prints one `::error::` line per denied path:
#
#       EDITOR_ISOLATION_PREFLIGHT_DENIED user=<u> kind=<k> path=<p>
#         first_denied=<component> mode=<perms> owner=<user:group>
#
#     `first_denied` is the actual blocker; `path` is only what the editor
#     asked for. A preflight run before reviewer fan-out turns a multi-hour
#     cycle into a seconds-long diagnosis that names the exact directory
#     to fix.
#
# Inputs (environment): EDITOR_ISOLATION_USER, SUPPORT_SCRIPTS_DIR,
#   SUPPORT_ROOT_DIR, RUNTIME_DIR, WORKSPACE_PATH, OPENCODE_HELPERS_PATH,
#   EDITOR_CODEX_PATH (PATH the editor is launched with; defaults to PATH).
# Kill switch: EDITOR_ISOLATION_PREFLIGHT_ENABLED=false (default true).
#
# Every probe is a `sudo -n -u <user> -- test ...`; no GitHub API calls, no
# network, no writes other than the chmod calls in the prepare function.

editor_isolation_preflight_enabled()
{
	case "$(printf '%s' "${EDITOR_ISOLATION_PREFLIGHT_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		0|false|no|off) return 1 ;;
	esac
	return 0
}

# Resolve the support bundle root: SUPPORT_ROOT_DIR is exported by
# stage_workflow_support.sh; fall back to the parent of SUPPORT_SCRIPTS_DIR.
_editor_isolation_support_root()
{
	if [ -n "${SUPPORT_ROOT_DIR:-}" ] && [ -d "${SUPPORT_ROOT_DIR}" ]; then
		printf '%s' "${SUPPORT_ROOT_DIR}"
		return 0
	fi
	if [ -n "${SUPPORT_SCRIPTS_DIR:-}" ] && [ -d "${SUPPORT_SCRIPTS_DIR}" ]; then
		dirname -- "${SUPPORT_SCRIPTS_DIR}"
		return 0
	fi
	return 1
}

editor_isolation_prepare_shared_paths()
{
	local support_root
	if support_root="$(_editor_isolation_support_root)"; then
		# Committed repository content only (scripts/, prompts/, ai-memory
		# schemas, unattended_system_instructions.md). No secrets are staged
		# here; the editor identity must read these to run at all.
		chmod -R o+rX -- "${support_root}" 2>/dev/null || {
			echo "::warning::editor_isolation_prepare_shared_paths: could not chmod o+rX ${support_root}; the isolation preflight will report the consequence." >&2
		}
	fi
	if [ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ]; then
		# Traverse-only: the editor reads its 0444 config and its sandbox
		# home beneath RUNTIME_DIR but must not be able to list the
		# runner-owned prompt/diff/ledger files that live beside them.
		chmod 0711 -- "${RUNTIME_DIR}" 2>/dev/null || {
			echo "::warning::editor_isolation_prepare_shared_paths: could not chmod 0711 ${RUNTIME_DIR}; the isolation preflight will report the consequence." >&2
		}
	fi
	return 0
}

# _editor_isolation_test_as <user> <kind> <path>
#   kind: traverse (dir, needs x) | dir (needs r+x) | file (needs r) |
#         exec (needs x)
_editor_isolation_test_as()
{
	local user="$1" kind="$2" path="$3"
	case "${kind}" in
		traverse) sudo -n -u "${user}" -- test -d "${path}" -a -x "${path}" ;;
		dir) sudo -n -u "${user}" -- test -d "${path}" -a -r "${path}" -a -x "${path}" ;;
		file) sudo -n -u "${user}" -- test -f "${path}" -a -r "${path}" ;;
		exec) sudo -n -u "${user}" -- test -f "${path}" -a -x "${path}" ;;
		*) return 2 ;;
	esac
}

# _editor_isolation_first_denied <user> <kind> <path>
#   Prints the first path component (walking from `/`) that <user> cannot
#   traverse, or the final path itself when only the leaf check fails.
_editor_isolation_first_denied()
{
	local user="$1" kind="$2" path="$3"
	local resolved rest comp acc=""
	resolved="$(realpath -m -- "${path}")"
	rest="${resolved#/}"
	while [ -n "${rest}" ]; do
		comp="${rest%%/*}"
		if [ "${rest}" = "${comp}" ]; then
			rest=""
		else
			rest="${rest#*/}"
		fi
		acc="${acc}/${comp}"
		if [ "${acc}" = "${resolved}" ]; then
			break
		fi
		if ! sudo -n -u "${user}" -- test -x "${acc}"; then
			printf '%s' "${acc}"
			return 0
		fi
	done
	printf '%s' "${resolved}"
}

_editor_isolation_describe_path()
{
	local path="$1"
	if [ -e "${path}" ]; then
		stat -c 'mode=%A owner=%U:%G' -- "${path}" 2>/dev/null || printf 'mode=? owner=?'
	else
		printf 'mode=missing owner=missing'
	fi
}

# editor_isolation_preflight_probe [user]
editor_isolation_preflight_probe()
{
	local user="${1:-${EDITOR_ISOLATION_USER:-nobody}}"
	local probe_path probe_kind first_denied description
	local -a probe_specs=()
	local failures=0
	local editor_path="${EDITOR_CODEX_PATH:-${PATH}}"
	local opencode_bin="" node_bin=""

	command -v sudo >/dev/null 2>&1 || {
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_DENIED user=${user} reason=sudo_missing" >&2
		return 1
	}
	sudo -n true >/dev/null 2>&1 || {
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_DENIED user=${user} reason=sudo_requires_password" >&2
		return 1
	}
	id "${user}" >/dev/null 2>&1 || {
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_DENIED user=${user} reason=identity_missing" >&2
		return 1
	}

	if [ -n "${OPENCODE_HELPERS_PATH:-}" ]; then
		probe_specs+=("file|${OPENCODE_HELPERS_PATH}")
	elif [ -n "${SUPPORT_SCRIPTS_DIR:-}" ]; then
		probe_specs+=("file|${SUPPORT_SCRIPTS_DIR}/opencode_helpers.sh")
	fi
	if [ -n "${SUPPORT_SCRIPTS_DIR:-}" ]; then
		probe_specs+=("dir|${SUPPORT_SCRIPTS_DIR}")
	fi
	if [ -n "${RUNTIME_DIR:-}" ]; then
		probe_specs+=("traverse|${RUNTIME_DIR}")
	fi
	if [ -n "${WORKSPACE_PATH:-}" ] && [ -d "${WORKSPACE_PATH}" ]; then
		probe_specs+=("dir|${WORKSPACE_PATH}")
	fi
	opencode_bin="$(PATH="${editor_path}" command -v opencode 2>/dev/null || true)"
	if [ -n "${opencode_bin}" ]; then
		probe_specs+=("exec|${opencode_bin}")
	else
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_DENIED user=${user} reason=opencode_not_on_editor_path" >&2
		failures=$((failures + 1))
	fi
	node_bin="$(PATH="${editor_path}" command -v node 2>/dev/null || true)"
	if [ -n "${node_bin}" ]; then
		probe_specs+=("exec|${node_bin}")
	fi

	for spec in "${probe_specs[@]}"; do
		probe_kind="${spec%%|*}"
		probe_path="${spec#*|}"
		if _editor_isolation_test_as "${user}" "${probe_kind}" "${probe_path}"; then
			continue
		fi
		failures=$((failures + 1))
		first_denied="$(_editor_isolation_first_denied "${user}" "${probe_kind}" "${probe_path}")"
		description="$(_editor_isolation_describe_path "${first_denied}")"
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_DENIED user=${user} kind=${probe_kind} path=${probe_path} first_denied=${first_denied} ${description}" >&2
	done

	if [ "${failures}" -gt 0 ]; then
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_FAILED user=${user} denied=${failures}: the review editor cannot run as the unprivileged identity in this job; fix the first_denied component(s) above or set EDITOR_ISOLATION_PREFLIGHT_ENABLED=false to skip this probe." >&2
		return 1
	fi
	echo "EDITOR_ISOLATION_PREFLIGHT_OK user=${user} probes=${#probe_specs[@]}"
	return 0
}
