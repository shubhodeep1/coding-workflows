#!/usr/bin/env bash
# Run plan and implement editors against a checked snapshot, never the credentialed checkout.
set -euo pipefail

action="${1:-}"
shift || true
support="${EDITOR_ISOLATION_SUPPORT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
runner_temp="$(realpath -m -- "${RUNNER_TEMP:-/tmp}")"
docker_clean()
{
	env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker "$@"
}
fail()
{
	echo "::error::EDITOR_ISOLATION action=${action} reason=${1}" >&2
	exit 1
}
valid_root()
{
	local root="${1:-}"
	# Do not resolve untrusted paths: require a direct child and a private root.
	[[ "$(dirname -- "${root}")" == "${runner_temp}" && "$(basename -- "${root}")" =~ ^editor-isolated-[A-Za-z0-9]+$ && ! -L "${root}" && -d "${root}" && "$(stat -c %a "${root}")" == 700 ]] || fail invalid_root
}
reap()
{
	local label="coding-workflows.editor-isolation=${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}" names remaining
	if [ -n "${1:-}" ]; then
		valid_root "$1"
		label="coding-workflows.editor-isolation.root=$(basename -- "$1")"
	fi
	command -v docker >/dev/null || fail docker_missing
	names="$(docker_clean ps -aq --filter "label=${label}")" || fail reap_failed
	if [ -n "${names}" ]; then
		while IFS= read -r name; do
			[[ "${name}" =~ ^[a-zA-Z0-9_-]+$ ]] || fail reap_failed
			docker_clean rm -f "${name}" >/dev/null || fail reap_failed
		done <<< "${names}"
	fi
	remaining="$(docker_clean ps -aq --filter "label=${label}")" || fail reap_failed
	[ -z "${remaining}" ] || fail reap_failed
	echo "EDITOR_ISOLATION action=reap outcome=$([ -n "${names}" ] && echo removed || echo none) count=$([ -n "${names}" ] && printf '%s\n' "${names}" | wc -l || echo 0)" >&2
}

case "${action}" in
prepare)
	profile="${1:-}" engine="${2:-}"
	case "${profile}:${engine}" in read:codex|read:claude|write:codex|write:claude) ;; *) exit 2 ;; esac
	command -v docker >/dev/null && command -v python3 >/dev/null || fail docker_missing
	for file in editor_isolated_run.sh review_untrusted_workspace.py clarify_openrouter_broker.py claude_anthropic_relay.py ai_engine.sh claude_engine.py claude_settings.json.tmpl codex_stall_guard.sh write_codex_config.sh codex_model_catalog.json review_sandbox/Dockerfile; do
		[ -f "${support}/${file}" ] || fail support_missing
	done
	workspace="$(/bin/pwd -P)"
	if [ -n "${WORKSPACE_PATH:-}" ]; then
		workspace="$(realpath -e -- "${WORKSPACE_PATH}")" || fail workspace_rejected
		[ "$(dirname -- "${workspace}")" = "$(realpath -e -- "${RUNNER_TEMP:-/tmp}/workspaces")" ] && [ -d "${GITHUB_WORKSPACE:-/invalid}/.git" ] || fail workspace_rejected
	fi
	[ -d "${workspace}" ] && [ ! -L "${workspace}" ] || fail workspace_rejected
	version="${CODEX_VERSION:-v0.114.0}"
	[[ "${version}" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail invalid_version
	root="$(mktemp -d "${runner_temp}/editor-isolated-XXXXXXXX")"
	trap 'rm -rf -- "${root}"' EXIT
	mkdir -m 0700 "${root}/home" "${root}/socket" "${root}/bin"
	printf '%s\n' "${profile}" > "${root}/profile"
	printf '%s\n' "${engine}" > "${root}/engine"
	printf '%s\n' "${workspace}" > "${root}/workspace"
	printf '%s\n' "${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}" > "${root}/run_label"
	build_args=(--build-arg "CODEX_VERSION=${version}")
	if [ "${engine}" = claude ]; then
		# shellcheck source=ai_engine.sh
		source "${support}/ai_engine.sh"
		claude_version="$(ai_engine_cli_version)" || fail support_missing
		[[ "${claude_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail invalid_version
		build_args+=(--build-arg "CLAUDE_CLI_VERSION=${claude_version}")
	fi
	image="$(docker_clean build -q "${build_args[@]}" -f "${support}/review_sandbox/Dockerfile" "${support}/review_sandbox")" || fail image_build_failed
	[[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]] || fail image_build_failed
	printf '%s\n' "${image}" > "${root}/image"
	printf '#!/usr/bin/env bash\nexec bash %q codex-exec %q "$@"\n' "${support}/editor_isolated_run.sh" "${root}" > "${root}/bin/codex"
	chmod 0700 "${root}/bin/codex"
	trap - EXIT
	printf '%s\n' "${root}"
	;;
snapshot)
	root="${1:-}"; valid_root "${root}"
	reap "${root}"
	workspace="$(< "${root}/workspace")"
	rm -rf -- "${root}/source"
	mkdir -m 0755 "${root}/source"
	git_dir=()
	if [ -n "${WORKSPACE_PATH:-}" ]; then git_dir=("${GITHUB_WORKSPACE}/.git"); fi
	PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" snapshot "${workspace}" "${root}/source" "${root}/baseline.json" "${git_dir[@]}" --profile editor
	;;
reap) reap "${1:-}" ;;
finish)
	root="${1:-}"; valid_root "${root}"
	reap "${root}"
	if [ "$(< "${root}/profile")" = write ]; then
		PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" transfer "$(< "${root}/workspace")" "${root}/source" "${root}/baseline.json" --profile editor || fail transfer_rejected
		echo 'EDITOR_ISOLATION action=finish outcome=transferred reason=none dropped=0' >&2
	else
		echo 'EDITOR_ISOLATION action=finish outcome=reaped reason=none dropped=0' >&2
	fi
	;;
cleanup)
	root="${1:-}"; valid_root "${root}"
	reap "${root}"
	rm -rf -- "${root}"
	;;
codex-exec)
	root="${1:-}"; shift || true; valid_root "${root}"
	if [ "${1:-}" = --version ] && [ "$#" -eq 1 ]; then echo 'codex-cli (isolated)'; exit 0; fi
	if [ "${1:-}" = exec ] && [ "${2:-}" = resume ] && [ "${3:-}" = --help ] && [ "$#" -eq 3 ]; then echo 'Usage: codex exec resume'; exit 0; fi
	[ "${1:-}" = --ask-for-approval ] && [ "${2:-}" = never ] && [ "${3:-}" = -c ] && [ "${4:-}" = model_verbosity=low ] && [ "${5:-}" = -c ] && [ "${6:-}" = include_apply_patch_tool=true ] && [ "${7:-}" = exec ] || exit 2
	shift 7
	mode='exec'
	if [ "${1:-}" = resume ]; then mode=resume; shift; fi
	if [ "${1:-}" = --skip-git-repo-check ]; then shift; fi
	[ "${1:-}" = --model ] && [[ "${2:-}" =~ ^[a-zA-Z0-9/_.:-]+$ ]] && [ "${3:-}" = --sandbox ] && [[ "${4:-}" =~ ^(read-only|workspace-write|danger-full-access)$ ]] || exit 2
	model="$2"; shift 4
	if [ "${mode}" = resume ]; then
		[[ "${1:-}" =~ ^[0-9a-f-]{36}$ && "${2:-}" = - && "$#" -eq 2 ]] || exit 2
		session="$1"
	else [ "$#" -eq 0 ] || exit 2; session=""; fi
	[ -n "${OPENROUTER_API_KEY:-}" ] || fail broker_unavailable
	reasoning="${EDITOR_ISOLATION_REASONING:-${MODEL_REASONING_EFFORT:-high}}"
	[[ "${reasoning}" =~ ^(xhigh|high|medium|low|none)$ ]] || fail invalid_reasoning
	umask 077
	cat > "${root}/prompt"
	# The catalog and config writer are trusted support files, never snapshot code.
	image="$(< "${root}/image")"; profile="$(< "${root}/profile")"
	[ "${profile}" = write ] && sandbox=danger-full-access || sandbox=read-only
	[ "${profile}" = write ] && search=disabled || search=live
	container="editor-isolated-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$"
	broker_pid=""
	stop_attempt()
	{
		[ -z "${broker_pid}" ] || { kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; }
		docker_clean rm -f "${container}" >/dev/null 2>&1 || true
	}
	trap stop_attempt EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	rm -f -- "${root}/socket/provider.sock"
	env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" CLARIFY_MODEL="${model}" PYTHONDONTWRITEBYTECODE=1 python3 "${support}/clarify_openrouter_broker.py" broker "${root}/socket/provider.sock" &
	broker_pid=$!
	for _ in $(seq 1 50); do [ -S "${root}/socket/provider.sock" ] && break; kill -0 "${broker_pid}" 2>/dev/null || break; sleep 0.1; done
	[ -S "${root}/socket/provider.sock" ] || fail broker_unavailable
	source_mount="type=bind,src=${root}/source,dst=/source"
	[ "${profile}" = read ] && source_mount+=",readonly"
	cmd=(codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${model}" --sandbox "${sandbox}")
	[ "${mode}" = resume ] && cmd=(codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec resume --skip-git-repo-check --model "${model}" --sandbox "${sandbox}" "${session}" -)
	docker_clean run --rm --name "${container}" \
		--label "coding-workflows.editor-isolation=$(< "${root}/run_label")" --label "coding-workflows.editor-isolation.root=$(basename -- "${root}")" \
		--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 4g --cpus 2 \
		--tmpfs /tmp:rw,nosuid,nodev,size=128m \
		--mount "${source_mount}" --mount "type=bind,src=${root}/home,dst=/home/agent" \
		--mount "type=bind,src=${root}/socket,dst=/socket,readonly" --mount "type=bind,src=${root}/prompt,dst=/prompt,readonly" \
		--mount "type=bind,src=${support}/clarify_openrouter_broker.py,dst=/bridge.py,readonly" \
		--mount "type=bind,src=${support}/write_codex_config.sh,dst=/support/write_codex_config.sh,readonly" \
		--mount "type=bind,src=${support}/codex_model_catalog.json,dst=/support/codex_model_catalog.json,readonly" \
		--env HOME=/home/agent --env CODEX_HOME=/home/agent/.codex --env CLARIFY_PROXY_KEY=isolated-placeholder \
		--env "EDITOR_MODEL=${model}" --env "EDITOR_REASONING=${reasoning}" --env "EDITOR_SEARCH=${search}" --workdir /source "${image}" /bin/bash -c '
			set -euo pipefail
			mkdir -p "${CODEX_HOME}"
			bash /support/write_codex_config.sh --model "${EDITOR_MODEL}" --reasoning "${EDITOR_REASONING}" --web-search "${EDITOR_SEARCH}" --catalog-path /support/codex_model_catalog.json --project-path /source --config-path "${CODEX_HOME}/config.toml" --allow-elevation forbid
			sed -i -e '\''s#base_url = "https://openrouter.ai/api/v1"#base_url = "http://127.0.0.1:8765/api/v1"#'\'' -e '\''s/env_key = "OPENROUTER_API_KEY"/env_key = "CLARIFY_PROXY_KEY"/'\'' "${CODEX_HOME}/config.toml"
			python3 /bridge.py bridge /socket/provider.sock &
			bridge_pid=$!
			trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
			python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
			"$@" < /prompt
		' _ "${cmd[@]}"
	;;
claude-exec)
	root="${1:-}"; role="${2:-}"; prompt="${3:-}"; output="${4:-}"; session="${5:-}"
	valid_root "${root}"
	[[ "${role}" =~ ^(PLAN|IMPLEMENT|IMPLEMENT_REPAIR)$ ]] && [ -f "${prompt}" ] && [ -n "${output}" ] || exit 2
	[ -z "${session}" ] || [[ "${session}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || exit 2
	[ "$(< "${root}/engine")" = claude ] || fail engine_mismatch
	# shellcheck source=ai_engine.sh
	source "${support}/ai_engine.sh"
	model="$(ai_engine_model "${role}" "${AI_ENGINE_MODEL_HINT:-}")" || exit 75
	effort="$(ai_engine_effort "${role}" "${AI_ENGINE_EFFORT_HINT:-}")" || exit 75
	[[ "${model}" =~ ^[a-zA-Z0-9/_.-]+$ && "${effort}" =~ ^(low|medium|high|xhigh|max)$ ]] || exit 75
	probe_model="$(_ai_engine_py config --key probe_model)" || exit 75
	guard_hook="$(_ai_engine_py support-file --name guard-hook)" || exit 75
	instructions="$(_ai_engine_instructions_file)" || exit 75
	profile="$(< "${root}/profile")"
	settings_args=(settings --checkout /source --out "${root}/claude-settings.json" --profile "${profile}" --guard-hook /guard.py)
	[ "${ALLOW_WORKFLOW_EDITS:-false}" = true ] && settings_args+=(--allow-workflow-edits)
	_ai_engine_py "${settings_args[@]}" || exit 75
	if [ "${profile}" = read ]; then tools=Read,Grep,Glob,Bash; permission=dontAsk; else tools=Read,Grep,Glob,Bash,Edit,Write,WebFetch,WebSearch; permission=bypassPermissions; fi
	mapfile -t accounts < <(ai_engine_accounts)
	[ "${#accounts[@]}" -gt 0 ] || { ai_engine_fallback "${role}" no_credential; exit 75; }
	install -m 0600 "${prompt}" "${root}/prompt"
	image="$(< "${root}/image")"
	container="editor-isolated-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$"
	source_mount="type=bind,src=${root}/source,dst=/source"
	[ "${profile}" = read ] && source_mount+=",readonly"
	optional_mount=()
	if [ "$(_ai_engine_py config --key hide_claude_md)" = true ] && [ -f "${root}/source/CLAUDE.md" ]; then
		: > "${root}/empty-claude-md"
		optional_mount=(--mount "type=bind,src=${root}/empty-claude-md,dst=/source/CLAUDE.md,readonly")
	fi
	session_args=()
	if [ -n "${session}" ]; then
		if compgen -G "${root}/home/.claude/projects/*/${session}.jsonl" >/dev/null; then session_args=(--resume "${session}"); else session_args=(--session-id "${session}"); fi
	fi
	broker_pid=""
	stop_attempt()
	{
		[ -z "${broker_pid}" ] || { kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; }
		docker_clean rm -f "${container}" >/dev/null 2>&1 || true
	}
	trap stop_attempt EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	for account in "${accounts[@]}"; do
		rm -f -- "${root}/socket/provider.sock"
		env -i PATH="${PATH}" PYTHONDONTWRITEBYTECODE=1 python3 "${support}/claude_anthropic_relay.py" broker "${root}/socket/provider.sock" "$(ai_engine_pool_dir)/tokens/${account}" "${model},${probe_model}" &
		broker_pid=$!
		for _ in $(seq 1 50); do [ -S "${root}/socket/provider.sock" ] && break; kill -0 "${broker_pid}" 2>/dev/null || break; sleep 0.1; done
		[ -S "${root}/socket/provider.sock" ] || { stop_attempt; broker_pid=""; continue; }
		rc=0
		# The host stall guard owns the run; the container and its detached children
		# are reaped before the next account or credentialed host operation.
		bash "${support}/codex_stall_guard.sh" --phase "${role,,}" --engine claude --stdout-file "${root}/transcript.jsonl" --stderr-file "${root}/claude-stderr" -- \
			env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm --name "${container}" \
			--label "coding-workflows.editor-isolation=$(< "${root}/run_label")" --label "coding-workflows.editor-isolation.root=$(basename -- "${root}")" \
			--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 4g --cpus 2 \
			--tmpfs /tmp:rw,nosuid,nodev,size=128m --mount "${source_mount}" "${optional_mount[@]}" \
			--mount "type=bind,src=${root}/socket,dst=/socket,readonly" --mount "type=bind,src=${root}/home,dst=/home/agent" \
			--mount "type=bind,src=${root}/prompt,dst=/prompt,readonly" \
			--mount "type=bind,src=${support}/claude_anthropic_relay.py,dst=/relay.py,readonly" \
			--mount "type=bind,src=${instructions},dst=/instructions.md,readonly" \
			--mount "type=bind,src=${root}/claude-settings.json,dst=/settings.json,readonly" \
			--mount "type=bind,src=${guard_hook},dst=/guard.py,readonly" \
			--env HOME=/home/agent --env ANTHROPIC_BASE_URL=http://127.0.0.1:8765 --env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder \
			--env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 --env DISABLE_AUTOUPDATER=1 \
			--env "CLAUDE_MODEL=${model}" --env "CLAUDE_EFFORT=${effort}" --env "CLAUDE_TOOLS=${tools}" --env "CLAUDE_PERMISSION=${permission}" \
			--workdir /source "${image}" /bin/bash -c '
				set -euo pipefail
				printf "{\"projects\":{\"/source\":{\"hasTrustDialogAccepted\":true}}}\n" > "${HOME}/.claude.json"
				python3 /relay.py bridge /socket/provider.sock &
				bridge_pid=$!
				trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
				python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
				claude -p --model "${CLAUDE_MODEL}" --effort "${CLAUDE_EFFORT}" --system-prompt-file /instructions.md --setting-sources "" --settings /settings.json --strict-mcp-config --disable-slash-commands --exclude-dynamic-system-prompt-sections --tools "${CLAUDE_TOOLS}" --permission-mode "${CLAUDE_PERMISSION}" --output-format stream-json --verbose "$@" < /prompt
			' _ "${session_args[@]}" || rc=$?
		stop_attempt; broker_pid=""
		reap "${root}"
		verdict="$(_ai_engine_py classify --transcript "${root}/transcript.jsonl" --exit-code "${rc}")" || verdict='{"outcome":"crashed","reason":"classify_failed"}'
		outcome="$(_ai_engine_json_field "${verdict}" outcome)"
		echo "CLAUDE_POOL run role=${role} account=${account} outcome=${outcome} reason=$(_ai_engine_json_field "${verdict}" reason) exit_code=${rc}" >&2
		case "${outcome}" in
			success) _ai_engine_py extract --transcript "${root}/transcript.jsonl" --out "${output}"; exit $? ;;
			usage_limit|auth_failed) continue ;;
			*) _ai_engine_py extract --transcript "${root}/transcript.jsonl" --out "${output}" || true; exit 1 ;;
		esac
	done
	ai_engine_fallback "${role}" all_accounts_failed
	exit 75
	;;
*) exit 2 ;;
esac
