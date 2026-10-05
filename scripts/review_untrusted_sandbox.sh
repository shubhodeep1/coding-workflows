#!/usr/bin/env bash
# Isolate PR dependency builds and OpenCode writer/test subprocesses.
set -euo pipefail

action="${1:-}"
support="${SUPPORT_SCRIPTS_DIR:-scripts}"
root="${REVIEW_SANDBOX_ROOT:-}"
workspace="${GITHUB_WORKSPACE:-$PWD}"
case "${action}" in prepare|prepare-ephemeral|run|cleanup) ;; *) exit 2 ;; esac
command -v docker >/dev/null && command -v python3 >/dev/null || { echo '::error::Review isolation requires Docker and Python' >&2; exit 1; }
[ -f "${support}/review_untrusted_workspace.py" ] && [ -f "${support}/clarify_openrouter_broker.py" ] && [ -f "${support}/review_sandbox/Dockerfile" ] || { echo '::error::Review isolation support missing' >&2; exit 1; }

# Only the trusted prepare step may select a root; never accept a path from
# the PR checkout or a model-controlled environment variable.
if [ "${action}" = prepare ] || [ "${action}" = prepare-ephemeral ]; then
	root="$(mktemp -d "${RUNNER_TEMP:-/tmp}/review-isolated-XXXXXXXX")"
	dep_container="review-deps-$$"
	trap 'env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker rm -f "${dep_container}" >/dev/null 2>&1 || true; rm -rf -- "${root}"' EXIT
	mkdir -m 0700 "${root}/socket" "${root}/home"
	mkdir -m 0755 "${root}/source"
	# The editor step works in the per-PR workspace: review_autofix.yml's
	# "Activate workspace shell context" cds every later step into
	# WORKSPACE_PATH and points GIT_WORK_TREE at it, while GITHUB_WORKSPACE
	# keeps only the Git database. Snapshot and transfer the tree the commit
	# step reads, or every validated edit lands where nothing commits it
	# (issues #4580, #6055). workspace_init.sh creates it only under
	# ${RUNNER_TEMP}/workspaces; without one the checkout stays the target.
	snapshot_git_dir=()
	if [ -n "${WORKSPACE_PATH:-}" ]; then
		workspace_root="$(realpath -e -- "${RUNNER_TEMP:-/tmp}/workspaces" 2>/dev/null || echo /invalid)"
		workspace="$(realpath -e -- "${WORKSPACE_PATH}" 2>/dev/null || echo /invalid)"
		checkout_git_dir="$(realpath -e -- "${GITHUB_WORKSPACE:-/invalid}/.git" 2>/dev/null || echo /invalid)"
		[ -d "${workspace}" ] && [[ "${workspace}" != *$'\n'* ]] && [ "$(dirname -- "${workspace}")" = "${workspace_root}" ] && [ -d "${checkout_git_dir}" ] || { echo '::error::Review workspace path rejected' >&2; exit 1; }
		 snapshot_git_dir=("${checkout_git_dir}")
	fi
	if [ "${action}" = prepare-ephemeral ] && [ "$(realpath -e -- "$PWD" 2>/dev/null || echo /invalid)" != "${workspace}" ]; then
		echo '::error::Review ephemeral workspace mismatch' >&2
		exit 1
	fi
	printf '%s\n' "${workspace}" > "${root}/workspace"
	PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" snapshot "${workspace}" "${root}/source" "${root}/baseline.json" "${snapshot_git_dir[@]}"
	version="${OPENCODE_VERSION:-1.18.23}"
	[[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo '::error::Invalid review OpenCode version' >&2; exit 1; }
	# `prepare claude` (scripts/ai_engine.sh) adds the pinned Claude Code CLI
	# to the same image; plain `prepare` builds the OpenCode image unchanged.
	if { [ "${2:-codex}" = claude ] || [ "${action}" = prepare-ephemeral ]; } && [ -f "${support}/ai_engine.sh" ] && [ -f "${support}/claude_engine.py" ]; then
		# shellcheck source=ai_engine.sh
		source "${support}/ai_engine.sh"
		claude_cli_version="$(ai_engine_cli_version)"
		[[ "${claude_cli_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo '::error::Invalid review Claude CLI version' >&2; exit 1; }
		image="$(timeout --signal=TERM --kill-after=10s 900s env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker build -q --build-arg "OPENCODE_VERSION=${version}" --build-arg "CLAUDE_CLI_VERSION=${claude_cli_version}" -f "${support}/review_sandbox/Dockerfile" "${support}/review_sandbox")"
		printf 'claude\n' > "${root}/engine"
	else
		[ "${action}" != prepare-ephemeral ] || { echo '::error::Review Claude support missing' >&2; exit 1; }
		image="$(env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker build -q --build-arg "OPENCODE_VERSION=${version}" -f "${support}/review_sandbox/Dockerfile" "${support}/review_sandbox")"
	fi
	[ -n "${image}" ] || exit 1
	printf '%s\n' "${image}" > "${root}/image"
	if [ "${action}" = prepare-ephemeral ]; then
		printf '%s\n' "${root}"
		trap - EXIT
		exit 0
	fi
	# Network access is confined to the credential-free dependency phase.
	# The user cannot specify the image, executable, mounts or Docker flags.
	timeout --signal=TERM --kill-after=10s 900s env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm --name "${dep_container}" --user "$(id -u):$(id -g)" \
		--cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 3g --cpus 2 \
		--mount "type=bind,src=${root}/source,dst=/source" \
		--workdir /source "${image}" /bin/bash -c '
			set -u
			install_failed=false
			python3 -m venv --system-site-packages /source/.review-venv || exit 1
			export PATH=/source/.review-venv/bin:$PATH
			if [ -f package-lock.json ]; then
				echo "Found package-lock.json — running npm ci"
				npm ci --ignore-scripts 2>&1 || install_failed=true
			elif [ -f yarn.lock ]; then
				echo "Found yarn.lock — running yarn install"
				yarn install --frozen-lockfile --ignore-scripts 2>&1 || install_failed=true
			elif [ -f pnpm-lock.yaml ]; then
				echo "Found pnpm-lock.yaml — running pnpm install"
				npx pnpm install --frozen-lockfile --ignore-scripts 2>&1 || install_failed=true
			elif [ -f package.json ]; then
				echo "Found package.json (no lockfile) — running npm install"
				npm install --ignore-scripts 2>&1 || install_failed=true
			fi
			if [ -f requirements.txt ]; then
				echo "Found requirements.txt — running pip install"
				pip install -r requirements.txt 2>&1 || install_failed=true
			elif [ -f pyproject.toml ] && [ ! -f package.json ]; then
				echo "Found pyproject.toml — running pip install"
				pip install -e ".[dev]" 2>&1 || pip install -e . 2>&1 || install_failed=true
			fi
			[ "$install_failed" = false ] || echo "::warning::Some project dependencies could not be installed. Editor validation may be limited."
			pytest_bootstrap_wanted=false
			if [ -f pytest.ini ] || [ -f conftest.py ] || [ -f tests/conftest.py ]; then
				pytest_bootstrap_wanted=true
			elif [ -f pyproject.toml ] && grep -q "^\[tool\.pytest\.ini_options\]" pyproject.toml; then
				pytest_bootstrap_wanted=true
			elif [ -f setup.cfg ] && grep -q "^\[tool:pytest\]" setup.cfg; then
				pytest_bootstrap_wanted=true
			elif [ -f tox.ini ] && grep -q "^\[pytest\]" tox.ini; then
				pytest_bootstrap_wanted=true
			fi
			if [ "$pytest_bootstrap_wanted" = true ]; then
				if python3 -c "import pytest" >/dev/null 2>&1; then
					echo "Repository declares pytest configuration and pytest is already importable."
				else
					echo "Repository declares pytest configuration but pytest is not importable — installing pytest"
					python3 -m pip install pytest 2>&1 || python3 -m pip install --user --break-system-packages pytest 2>&1 || true
					if python3 -c "import pytest" >/dev/null 2>&1; then
						echo "pytest bootstrap succeeded."
					else
						echo "::warning::pytest is declared by this repository but could not be installed; the editor cannot run its pytest suites and will fall back to ad-hoc validation."
					fi
				fi
			fi
		' || { echo '::error::Review dependency isolation failed' >&2; exit 1; }
	# PR build backends may write source files. Never publish their writes as
	# editor output: restore the exact host snapshot before launching the model.
	PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" refresh "${workspace}" "${root}/source" "${root}/baseline.json"
	printf 'REVIEW_SANDBOX_ROOT=%s\n' "${root}" >> "${GITHUB_ENV:?}"
	trap - EXIT
	exit 0
fi

[[ "${root}" == "${RUNNER_TEMP:-/tmp}"/review-isolated-* ]] && \
	[ "$(dirname "$(realpath -e -- "${root}" 2>/dev/null || echo /invalid)")" = "$(realpath -e -- "${RUNNER_TEMP:-/tmp}")" ] && \
	[ -f "${root}/image" ] && [ -f "${root}/baseline.json" ] || { echo '::error::Review sandbox not prepared' >&2; exit 1; }
if [ "${action}" = cleanup ]; then
	if [ -f "${root}/active-container" ]; then
		active_container="$(< "${root}/active-container")"
		[[ "${active_container}" =~ ^review-editor-[0-9]+$ ]] && env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker rm -f "${active_container}" >/dev/null 2>&1 || true
	fi
	rm -rf -- "${root}"
	exit 0
fi
# Transfer only into the workspace prepare validated and recorded.
[ -f "${root}/workspace" ] || { echo '::error::Review sandbox not prepared' >&2; exit 1; }
workspace="$(< "${root}/workspace")"
[ -d "${workspace}" ] || { echo '::error::Review workspace path rejected' >&2; exit 1; }

[ "$#" -ge 6 ] && [ "$#" -le 9 ] || exit 2
prompt="$2"
output="$3"
model="$4"
variant="$5"
config="$6"
engine="${7:-codex}"
case "${engine}" in codex|claude) ;; *) exit 2 ;; esac
claude_role="${8:-REVIEW_EDITOR}"
case "${claude_role}" in REVIEW_EDITOR|REVIEW_CONSOLIDATOR|RB_JUDGE|CONFLICT_RESOLVER|WAVE_JUDGE|STALL_JUDGE|INTEGRATION_JUDGE|SECURITY_JUDGE) ;; *) exit 2 ;; esac
claude_access="${9:-write}"
if [ "$#" -lt 9 ] && [ "${claude_role}" = REVIEW_CONSOLIDATOR ]; then
	claude_access=read
fi
case "${claude_access}" in read|write) ;; *) exit 2 ;; esac
# Poller judges (WAVE/STALL/INTEGRATION/SECURITY) are read-only sandbox roles; never transfer.
case "${claude_role}" in
	WAVE_JUDGE|STALL_JUDGE|INTEGRATION_JUDGE|SECURITY_JUDGE)
		[ "${claude_access}" = read ] || exit 2 ;;
esac

# Claude engine branch (scripts/ai_engine.sh): the same container, mounts and
# transfer, with the Claude Code CLI behind scripts/claude_anthropic_relay.py.
# <model> and <variant> are the D3 hints and <config> is unused. Exit 75 means
# Claude is unavailable and the caller runs the OpenCode path (D1).
if [ "${engine}" = claude ]; then
	[ "$(cat "${root}/engine" 2>/dev/null)" = claude ] || { echo "AI_ENGINE_FALLBACK role=${claude_role} reason=sandbox_not_prepared" >&2; exit 75; }
	for required in ai_engine.sh claude_engine.py claude_anthropic_relay.py claude_settings.json.tmpl; do
		[ -f "${support}/${required}" ] || { echo "AI_ENGINE_FALLBACK role=${claude_role} reason=support_missing" >&2; exit 75; }
	done
	[ -s "${prompt}" ] || { echo '::error::Review relay preflight failed' >&2; exit 1; }
	# shellcheck source=ai_engine.sh
	source "${support}/ai_engine.sh"
	claude_model="$(ai_engine_model "${claude_role}" "${model}")"
	claude_effort="$(ai_engine_effort "${claude_role}" "${variant}")"
	probe_model="$(_ai_engine_py config --key probe_model)"
	guard_hook="$(_ai_engine_py support-file --name guard-hook)" || { ai_engine_fallback "${claude_role}" policy_unavailable; exit 75; }
	instructions="$(_ai_engine_instructions_file)" || { ai_engine_fallback "${claude_role}" instructions_missing; exit 75; }
	settings_args=(settings --checkout /source --out "${root}/claude-settings.json" --profile "${claude_access}" --guard-hook /guard.py)
	[ "${ALLOW_WORKFLOW_EDITS:-false}" = "true" ] && settings_args+=(--allow-workflow-edits)
	_ai_engine_py "${settings_args[@]}" || { ai_engine_fallback "${claude_role}" policy_unavailable; exit 75; }
	mapfile -t claude_accounts < <(ai_engine_accounts)
	[ "${#claude_accounts[@]}" -gt 0 ] || { ai_engine_fallback "${claude_role}" no_credential; exit 75; }
	claude_home="${root}/home"
	claude_tools='Read,Grep,Glob,Bash,Edit,Write,WebFetch,WebSearch'
	claude_permissions=bypassPermissions
	claude_source_mount="type=bind,src=${root}/source,dst=/source"
	if [ "${claude_access}" = read ]; then
		claude_home="$(mktemp -d "${root}/home-read-XXXXXXXX")"
		claude_tools='Read,Grep,Glob'
		claude_permissions=dontAsk
		claude_source_mount+=',readonly'
	fi
	install -m 0600 "${prompt}" "${root}/prompt"
	image="$(< "${root}/image")"
	[[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo '::error::Invalid review image ID' >&2; exit 1; }
	container="review-editor-$$"
	printf '%s\n' "${container}" > "${root}/active-container"
	broker_pid=""
	progress_pid=""
	claude_finish()
	{
		[ -z "${progress_pid}" ] || { kill "${progress_pid}" 2>/dev/null || true; wait "${progress_pid}" 2>/dev/null || true; }
		[ -z "${broker_pid}" ] || { kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; }
		env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker rm -f "${container}" >/dev/null 2>&1 || true
		rm -f -- "${root}/active-container" "${root}/socket/provider.sock"
		if [ "${claude_access}" = read ]; then rm -rf -- "${claude_home}"; fi
	}
	trap claude_finish EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	rc="${_AI_ENGINE_EXIT_FALLBACK}"
	for account in "${claude_accounts[@]}"; do
		rm -f -- "${root}/socket/provider.sock" "${root}/transcript.jsonl"
		env -i PATH="${PATH}" PYTHONDONTWRITEBYTECODE=1 \
			python3 "${support}/claude_anthropic_relay.py" broker "${root}/socket/provider.sock" "$(ai_engine_pool_dir)/tokens/${account}" "${claude_model},${probe_model}" &
		broker_pid=$!
		for _ in $(seq 1 50); do
			[ -S "${root}/socket/provider.sock" ] && break
			kill -0 "${broker_pid}" 2>/dev/null || break
			sleep 0.1
		done
		if [ ! -S "${root}/socket/provider.sock" ]; then
			echo "CLAUDE_POOL run role=${claude_role} account=${account} outcome=crashed reason=relay_unavailable" >&2
			kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; broker_pid=""
			continue
		fi
		# The CLI streams to the transcript, not stderr, so the editor's idle
		# watchdog (review_apply_fixes.sh, EDITOR_IDLE_TIMEOUT) would see no
		# output. Report progress on stderr whenever the transcript grows; a
		# run that stops producing output still trips the watchdog.
		: > "${root}/transcript.jsonl"
		(
			last_size=0
			trap 'kill "${progress_sleep_pid:-}" 2>/dev/null || true' TERM
			while :; do
				sleep "${REVIEW_SANDBOX_PROGRESS_SECS:-60}" </dev/null >/dev/null 2>&1 &
				progress_sleep_pid=$!
				wait "${progress_sleep_pid}" || break
				size="$(stat -c %s "${root}/transcript.jsonl" 2>/dev/null || echo 0)"
				if [ "${size}" != "${last_size}" ]; then
					echo "CLAUDE_ENGINE progress role=${claude_role} transcript_bytes=${size}" >&2
					last_size="${size}"
				fi
			done
		) &
		progress_pid=$!
		# No host checkout, HOME, Docker socket, tokens or Git remote is mounted.
		run_rc=0
		env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm --name "${container}" \
			--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL \
			--security-opt no-new-privileges --pids-limit 128 --memory 3g --cpus 2 \
			--tmpfs /tmp:rw,nosuid,nodev,size=128m \
			--mount "${claude_source_mount}" \
			--mount "type=bind,src=${root}/socket,dst=/socket,readonly" \
			--mount "type=bind,src=${claude_home},dst=/home/agent" \
			--mount "type=bind,src=${root}/prompt,dst=/prompt,readonly" \
			--mount "type=bind,src=$(realpath "${support}/claude_anthropic_relay.py"),dst=/relay.py,readonly" \
			--mount "type=bind,src=$(realpath "${instructions}"),dst=/instructions.md,readonly" \
			--mount "type=bind,src=${root}/claude-settings.json,dst=/settings.json,readonly" \
			--mount "type=bind,src=$(realpath "${guard_hook}"),dst=/guard.py,readonly" \
			--env HOME=/home/agent --env ANTHROPIC_BASE_URL=http://127.0.0.1:8765 \
			--env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder \
			--env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 --env DISABLE_AUTOUPDATER=1 \
			--env "CLAUDE_MODEL=${claude_model}" --env "CLAUDE_EFFORT=${claude_effort}" \
			--env "CLAUDE_TOOLS=${claude_tools}" --env "CLAUDE_PERMISSIONS=${claude_permissions}" --workdir /source "${image}" /bin/bash -c '
				set -euo pipefail
				printf "{\"projects\":{\"/source\":{\"hasTrustDialogAccepted\":true}}}\n" > "${HOME}/.claude.json"
				python3 /relay.py bridge /socket/provider.sock &
				bridge_pid=$!
				trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
				python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
				export PATH=/source/.review-venv/bin:$PATH NO_COLOR=1
				claude -p --model "${CLAUDE_MODEL}" --effort "${CLAUDE_EFFORT}" \
					--system-prompt-file /instructions.md \
					--setting-sources "" --settings /settings.json \
					--strict-mcp-config --disable-slash-commands \
					--exclude-dynamic-system-prompt-sections \
					--tools "${CLAUDE_TOOLS}" --permission-mode "${CLAUDE_PERMISSIONS}" \
					--output-format stream-json --verbose < /prompt
			' > "${root}/transcript.jsonl" || run_rc=$?
		kill "${progress_pid}" 2>/dev/null || true; wait "${progress_pid}" 2>/dev/null || true; progress_pid=""
		kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; broker_pid=""
		verdict="$(_ai_engine_py classify --transcript "${root}/transcript.jsonl" --exit-code "${run_rc}")" || verdict='{"outcome":"crashed","reason":"classify_failed"}'
		outcome="$(_ai_engine_json_field "${verdict}" outcome)"
		echo "CLAUDE_POOL run role=${claude_role} account=${account} outcome=${outcome} reason=$(_ai_engine_json_field "${verdict}" reason) exit_code=${run_rc}" >&2
		case "${outcome}" in
			success)
				_ai_engine_py extract --transcript "${root}/transcript.jsonl" --out "${output}" >&2 && rc=0 || rc=1
				break
				;;
			usage_limit|auth_failed)
				continue
				;;
			*)
				rc=1
				break
				;;
		esac
	done
	[ "${rc}" -ne "${_AI_ENGINE_EXIT_FALLBACK}" ] || ai_engine_fallback "${claude_role}" all_accounts_failed
	# Never transfer on a failed model invocation or a swapped host baseline.
	if [ "${rc}" -eq 0 ] && [ "${claude_access}" = write ]; then
		: > "${RUNTIME_DIR:?}/review_sandbox_transfer_failed"
		if PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" transfer "${workspace}" "${root}/source" "${root}/baseline.json" 2> "${RUNTIME_DIR}/review_sandbox_transfer_reason_${output##*/}"; then
			rm -f "${RUNTIME_DIR}/review_sandbox_transfer_failed"
		else
			rc=1
		fi
	fi
	exit "${rc}"
fi
[[ "${model}" =~ ^[a-zA-Z0-9._:+-]+/[a-zA-Z0-9._:+/-]+$ ]] && [[ "${variant}" =~ ^(none|low|medium|high|xhigh)$ ]] || exit 2
[ -n "${OPENROUTER_API_KEY:-}" ] && [ -s "${prompt}" ] || { echo '::error::Review relay preflight failed' >&2; exit 1; }

# Never mount a host-generated config with other providers or host paths.
if ! PYTHONDONTWRITEBYTECODE=1 python3 - "${config}" "${root}/config.json" "${model}" "${claude_access}" "${claude_role}" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
	config = json.load(handle)
assert config["provider"]["openrouter"]["options"]["baseURL"] == "https://openrouter.ai/api/v1"
assert config["model"] == "openrouter/" + sys.argv[3]
config["provider"]["openrouter"]["options"] = {"baseURL": "http://127.0.0.1:8765/api/v1", "apiKey": "{env:OPENROUTER_API_KEY}"}
config.pop("mcp", None)  # Serena runs only on the host, never inside the writer.
if sys.argv[4] == "read" and sys.argv[5] in {"WAVE_JUDGE", "STALL_JUDGE", "INTEGRATION_JUDGE", "SECURITY_JUDGE", "RB_JUDGE"}:
	# OpenCode snapshots write to the private /source/.git; the read role's
	# source is mounted read-only and the trusted host snapshot already exists.
	config["snapshot"] = False
with open(sys.argv[2], "w", encoding="utf-8") as handle:
	json.dump(config, handle)
PY
then
	echo '::error::Review isolation config rejected' >&2
	exit 1
fi
install -m 0600 "${prompt}" "${root}/prompt"
image="$(< "${root}/image")"
[[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo '::error::Invalid review image ID' >&2; exit 1; }
container="review-editor-$$"
printf '%s\n' "${container}" > "${root}/active-container"
broker_pid=""
finish()
{
	[ -z "${broker_pid}" ] || { kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; }
	env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker rm -f "${container}" >/dev/null 2>&1 || true
	rm -f "${root}/active-container" "${root}/socket/provider.sock"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" CLARIFY_MODEL="${model}" PYTHONDONTWRITEBYTECODE=1 \
	python3 "${support}/clarify_openrouter_broker.py" review-broker "${root}/socket/provider.sock" &
broker_pid=$!
for _ in $(seq 1 50); do
	[ -S "${root}/socket/provider.sock" ] && break
	kill -0 "${broker_pid}" 2>/dev/null || exit 1
	sleep 0.1
done
[ -S "${root}/socket/provider.sock" ] || exit 1

# No host checkout, HOME, Docker socket, tokens or Git remote is mounted.
rc=0
opencode_source_mount="type=bind,src=${root}/source,dst=/source"
opencode_agent=writer
case "${claude_role}" in
	WAVE_JUDGE|STALL_JUDGE|INTEGRATION_JUDGE|SECURITY_JUDGE) opencode_source_mount+=',readonly' ;;
	RB_JUDGE)
		if [ "${claude_access}" = read ]; then
			opencode_source_mount+=',readonly'
			opencode_agent=reviewer
		fi
		;;
esac
env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm --name "${container}" \
	--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL \
	--security-opt no-new-privileges --pids-limit 128 --memory 3g --cpus 2 \
	--tmpfs /tmp:rw,nosuid,nodev,size=128m \
	--mount "${opencode_source_mount}" \
	--mount "type=bind,src=${root}/socket,dst=/socket,readonly" \
	--mount "type=bind,src=${root}/home,dst=/home/agent" \
	--mount "type=bind,src=${root}/config.json,dst=/config.json,readonly" \
	--mount "type=bind,src=${root}/prompt,dst=/prompt,readonly" \
	--mount "type=bind,src=$(realpath "${support}/clarify_openrouter_broker.py"),dst=/bridge.py,readonly" \
	--env HOME=/home/agent --env OPENROUTER_API_KEY=isolated-placeholder \
	--env "MODEL=${model}" --env "VARIANT=${variant}" --env "OPENCODE_AGENT=${opencode_agent}" --workdir /source "${image}" /bin/bash -c '
		set -euo pipefail
		python3 /bridge.py review-bridge /socket/provider.sock &
		bridge_pid=$!
		trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
		python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
		export PATH=/source/.review-venv/bin:$PATH OPENCODE_CONFIG=/config.json NO_COLOR=1
		opencode run --dir /source -m "openrouter/${MODEL}" --agent "${OPENCODE_AGENT}" --variant "${VARIANT}" --title coding-workflows-agent-run --print-logs --log-level INFO --auto < /prompt
	' > "${output}" || rc=$?
	# Never transfer on a failed model invocation or a swapped host baseline.
	# Arg 9 (read) applies to both engines; read-only roles never transfer edits back.
	if [ "${rc}" -eq 0 ] && [ "${claude_access}" = write ]; then
		# The marker survives a killed/incomplete transfer. The editor wrapper
		# fails the step instead of treating a partial host edit as a retry.
		: > "${RUNTIME_DIR:?}/review_sandbox_transfer_failed"
		# Only the trusted transfer helper writes this attempt-scoped diagnostic.
		if PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" transfer "${workspace}" "${root}/source" "${root}/baseline.json" 2> "${RUNTIME_DIR}/review_sandbox_transfer_reason_${output##*/}"; then
			rm -f "${RUNTIME_DIR}/review_sandbox_transfer_failed"
		else
			rc=1
		fi
	fi
	exit "${rc}"
