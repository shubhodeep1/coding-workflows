#!/usr/bin/env bash
# Run one clarify attempt in a credential-free, network-isolated container.
# Only this helper (not the agent) accesses Docker and the host-side broker.
# CLARIFY_SOURCE_ROOT optionally selects the source snapshot (default: $PWD).
# CLARIFY_SNAPSHOT_OMIT_AGENT_INSTRUCTIONS=true omits agent instructions (default: false).
set -euo pipefail

prompt_file="${1:?prompt file required}"
output_file="${2:?output file required}"
log_file="${3:?log file required}"
# Optional engine (scripts/ai_engine.sh) and role; the default is the codex path.
engine="${4:-codex}"
engine_role="${5:-CLARIFY}"
case "${engine}" in codex|claude) ;; *) echo '::error::Invalid clarify engine' >&2; exit 1 ;; esac
[[ "${engine_role}" =~ ^(CLARIFY|CLARIFY_RESPOND|PLAN|UNBLOCK_JUDGE)$ ]] || { echo '::error::Invalid clarify engine role' >&2; exit 1; }
[ "${engine}" != claude ] || [[ "${engine_role}" =~ ^(CLARIFY|CLARIFY_RESPOND|PLAN|UNBLOCK_JUDGE)$ ]] || { echo '::error::Invalid Claude engine role' >&2; exit 1; }
support="scripts"
if [ -n "${CLARIFY_ISOLATION_SUPPORT_DIR:-}" ]; then
	if [[ "${CLARIFY_ISOLATION_SUPPORT_DIR}" != /* ]] || [ ! -d "${CLARIFY_ISOLATION_SUPPORT_DIR}" ]; then
		echo '::error::Invalid clarify isolation support directory' >&2
		exit 1
	fi
	support="${CLARIFY_ISOLATION_SUPPORT_DIR}"
fi
isolation_timeout="${CLARIFY_ISOLATION_TIMEOUT_SECS:-}"
if [ -n "${isolation_timeout}" ] && { [[ ! "${isolation_timeout}" =~ ^[0-9]+$ ]] || [[ ! "${isolation_timeout}" =~ [1-9] ]]; }; then
	echo '::error::Invalid clarify isolation timeout' >&2
	exit 1
fi
version="${CLARIFY_CODEX_VERSION:-v0.114.0}"
[[ "${version}" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo '::error::Invalid Codex version' >&2; exit 1; }
[[ "${MODEL_EDITOR:-}" =~ ^[a-zA-Z0-9/_.-]+$ ]] || { echo '::error::Invalid model slug' >&2; exit 1; }
[[ "${MODEL_REASONING_EFFORT:-}" =~ ^(xhigh|high|medium|low|none)$ ]] || { echo '::error::Invalid reasoning level' >&2; exit 1; }
if [ "${engine}" = codex ]; then
	[ -n "${OPENROUTER_API_KEY:-}" ] && [ -s "${prompt_file}" ] || { echo '::error::Clarify isolation preflight failed' >&2; exit 1; }
else
	[ -s "${prompt_file}" ] || { echo '::error::Clarify isolation preflight failed' >&2; exit 1; }
fi
command -v docker >/dev/null && command -v python3 >/dev/null || { echo '::error::Clarify isolation prerequisites unavailable' >&2; exit 1; }
[ -f "${support}/clarify_sandbox/Dockerfile" ] && [ -f "${support}/clarify_openrouter_broker.py" ] && [ -f "${support}/write_codex_config.sh" ] && [ -f "${support}/codex_model_catalog.json" ] || { echo '::error::Clarify isolation support missing' >&2; exit 1; }
# The source root is read as data only; host Python never imports from it.
source_root="${CLARIFY_SOURCE_ROOT:-${PWD}}"
omit_agent_instructions="${CLARIFY_SNAPSHOT_OMIT_AGENT_INSTRUCTIONS:-false}"
case "${omit_agent_instructions}" in true) ;; *) omit_agent_instructions=false ;; esac
if [ ! -d "${source_root}" ] || [ -L "${source_root}" ]; then
	echo '::error::Clarify source root unavailable' >&2
	exit 1
fi
source_root="$(cd "${source_root}" && pwd -P)" || { echo '::error::Clarify source root unavailable' >&2; exit 1; }

# The runner-owned temporary root contains no credentials. Its socket child is
# traversable by the container's matching non-root UID, not by other users.
run_root="$(mktemp -d)"
container_name="clarify-${GITHUB_RUN_ID:-local}-$$"
broker_pid=""
cleanup()
{
	if [ -n "${broker_pid}" ]; then kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; fi
	env -u OPENROUTER_API_KEY -u GH_TOKEN -u GITHUB_TOKEN docker rm -f "${container_name}" >/dev/null 2>&1 || true
	rm -rf -- "${run_root}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -m 0700 "${run_root}/socket"
mkdir -m 0755 "${run_root}/source" "${run_root}/results"

# Include only regular, tracked source files with safe path classes. Never
# follow a symlink (including parent directories); do not include .git,
# support checkouts, runner configuration, env files or private keys.
# triage-host-python-import-shadowing: -I excludes both cwd and the script directory.
PYTHONDONTWRITEBYTECODE=1 python3 -I -B - "${source_root}" "${run_root}/source" "${omit_agent_instructions}" "${CLARIFY_ISOLATION_SUPPORT_DIR:-}" <<'PY'
import os
import pathlib
import stat
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
dest = pathlib.Path(sys.argv[2])
omit_agent_instructions = sys.argv[3] == "true"
roots = {"src", "scripts", "tests", "prompts", "docs", "app", "lib", "workflow-templates", "validation", "db", "ai-memory", "changelog.d"}
root_files = {"README.md", "agents.md", "AGENTS.md", "package.json", "pyproject.toml", "go.mod", "Cargo.toml"}
agent_instruction_names = {"agents.md", "agents.override.md", "claude.md", "claude.local.md"}
suffixes = {".py", ".sh", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".json", ".md", ".yml", ".yaml", ".toml", ".txt", ".css", ".html", ".sql"}
bad_parts = {".git", ".ai", ".codex", ".codex-workflow-src", ".codex-workflow-src-main", ".env", "secrets", "credentials", "__pycache__"}
count = 0
total = 0
omitted = 0

def copy(path):
    global count, total, omitted
    parts = pathlib.PurePosixPath(path).parts
    if (not parts or any(part.lower() in bad_parts or part.lower().startswith(".env") for part in parts)
            or any(part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".keystore")) for part in parts)
            or (path not in root_files and parts[0] not in roots and parts[:2] not in ((".github", "workflows"), (".github", "actions")))
            or (path not in root_files and pathlib.PurePosixPath(path).suffix.lower() not in suffixes and parts[-1] != "Dockerfile")):
        return
    if omit_agent_instructions and parts[-1].lower() in agent_instruction_names:
        omitted += 1
        return
    node = root
    for part in parts:
        node = node / part
        info = node.lstat()
        if stat.S_ISLNK(info.st_mode) and node == root / path:
            return  # A tracked symlink is not part of the snapshot.
        if stat.S_ISLNK(info.st_mode) or (node != root / path and not stat.S_ISDIR(info.st_mode)):
            raise ValueError("unsafe source path")
    if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
        raise ValueError("unsafe source file")
    count += 1
    total += info.st_size
    if count > 5000 or total > 64 * 1024 * 1024:
        raise ValueError("source snapshot limit exceeded")
    target = dest.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Fail if a path is swapped for a symlink between lstat and open.
    fd = os.open(node, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source, target.open("xb") as sink:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
            raise ValueError("source changed during snapshot")
        data = source.read(2 * 1024 * 1024 + 1)
        if len(data) != info.st_size:
            raise ValueError("source changed during snapshot")
        sink.write(data)
    os.chmod(target, 0o644)

try:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
    for entry in tracked:
        if entry:
            copy(entry.decode("utf-8"))
    if omitted:
        print(f"CLARIFY_SNAPSHOT_AGENT_INSTRUCTIONS_OMITTED count={omitted}", file=sys.stderr)
    # Consumer checkouts do not track the staged writer/catalog; these are
    # fixed support files, never read from the issue prompt or user input.
    if not sys.argv[4]:
        for required in ("scripts/write_codex_config.sh", "scripts/codex_model_catalog.json"):
            if not (dest / required).exists():
                copy(required)
except (OSError, ValueError, UnicodeError) as exc:
    raise SystemExit("clarify source snapshot rejected") from None
PY

# A separate support checkout must supply the executable config writer and
# catalog, not similarly named files in the untrusted target snapshot.
if [ -n "${CLARIFY_ISOLATION_SUPPORT_DIR:-}" ]; then
	install -D -m 0644 "${support}/write_codex_config.sh" "${run_root}/source/scripts/write_codex_config.sh"
	install -D -m 0644 "${support}/codex_model_catalog.json" "${run_root}/source/scripts/codex_model_catalog.json"
fi

# Claude engine branch: the same isolation, with the Claude Code CLI inside
# the container and scripts/claude_anthropic_relay.py on the host. The real
# OAuth token stays in the host relay; the container sees a placeholder.
# Exit 75 means Claude is unavailable and the caller runs the codex path (D1).
if [ "${engine}" = claude ]; then
	engine_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
	for required in ai_engine.sh claude_engine.py claude_anthropic_relay.py claude_settings.json.tmpl; do
		[ -f "${engine_dir}/${required}" ] || { echo "AI_ENGINE_FALLBACK role=${engine_role} reason=support_missing" >&2; exit 75; }
	done
	# shellcheck source=ai_engine.sh
	source "${engine_dir}/ai_engine.sh"
	claude_model="$(ai_engine_model "${engine_role}" "${MODEL_EDITOR}")"
	claude_effort="$(ai_engine_effort "${engine_role}" "${MODEL_REASONING_EFFORT}")"
	claude_version="$(ai_engine_cli_version)"
	probe_model="$(_ai_engine_py config --key probe_model)"
	guard_hook="$(_ai_engine_py support-file --name guard-hook)" || { ai_engine_fallback "${engine_role}" policy_unavailable; exit 75; }
	instructions="$(_ai_engine_instructions_file)" || { ai_engine_fallback "${engine_role}" instructions_missing; exit 75; }
	_ai_engine_py settings --checkout /source --out "${run_root}/claude-settings.json" --profile read --guard-hook /guard.py || { ai_engine_fallback "${engine_role}" policy_unavailable; exit 75; }
	chmod 0644 "${run_root}/claude-settings.json"
	mapfile -t claude_accounts < <(ai_engine_accounts)
	[ "${#claude_accounts[@]}" -gt 0 ] || { ai_engine_fallback "${engine_role}" no_credential; exit 75; }
	if ! image="$(env -u OPENROUTER_API_KEY -u GH_TOKEN -u GITHUB_TOKEN docker build -q --build-arg "CODEX_VERSION=${version}" --build-arg "CLAUDE_CLI_VERSION=${claude_version}" -f "${support}/clarify_sandbox/Dockerfile" "${support}/clarify_sandbox")"; then
		ai_engine_fallback "${engine_role}" image_build_failed
		exit 75
	fi
	[ -n "${image}" ] || { ai_engine_fallback "${engine_role}" image_build_failed; exit 75; }
	for account in "${claude_accounts[@]}"; do
		rm -f -- "${run_root}/results/transcript.jsonl" "${run_root}/results/stderr" "${run_root}/socket/provider.sock"
		env -i PATH="${PATH}" PYTHONDONTWRITEBYTECODE=1 \
			python3 -I -B "${engine_dir}/claude_anthropic_relay.py" broker "${run_root}/socket/provider.sock" "$(ai_engine_pool_dir)/tokens/${account}" "${claude_model},${probe_model}" &
		broker_pid=$!
		for _ in $(seq 1 50); do
			[ -S "${run_root}/socket/provider.sock" ] && break
			kill -0 "${broker_pid}" 2>/dev/null || break
			sleep 0.1
		done
		if [ ! -S "${run_root}/socket/provider.sock" ]; then
			echo "CLAUDE_POOL run role=${engine_role} account=${account} outcome=crashed reason=relay_unavailable" >&2
			kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; broker_pid=""
			continue
		fi
		run_rc=0
		env -u OPENROUTER_API_KEY -u GH_TOKEN -u GITHUB_TOKEN docker run --rm \
			--name "${container_name}" --user "$(id -u):$(id -g)" \
			--network none --read-only --cap-drop ALL --security-opt no-new-privileges \
			--pids-limit 128 --memory 2g --cpus 2 \
			--tmpfs /tmp:rw,nosuid,nodev,size=64m --tmpfs /home/agent:rw,nosuid,nodev,size=64m,mode=1777 \
			--mount "type=bind,src=${run_root}/source,dst=/source,readonly" \
			--mount "type=bind,src=$(realpath "${prompt_file}"),dst=/prompt,readonly" \
			--mount "type=bind,src=${run_root}/socket,dst=/socket,readonly" \
			--mount "type=bind,src=${engine_dir}/claude_anthropic_relay.py,dst=/relay.py,readonly" \
			--mount "type=bind,src=$(realpath "${instructions}"),dst=/instructions.md,readonly" \
			--mount "type=bind,src=${run_root}/claude-settings.json,dst=/settings.json,readonly" \
			--mount "type=bind,src=$(realpath "${guard_hook}"),dst=/guard.py,readonly" \
			--mount "type=bind,src=${run_root}/results,dst=/results" \
			--env HOME=/home/agent \
			--env ANTHROPIC_BASE_URL=http://127.0.0.1:8765 \
			--env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder \
			--env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 --env DISABLE_AUTOUPDATER=1 \
			--env "CLAUDE_MODEL=${claude_model}" --env "CLAUDE_EFFORT=${claude_effort}" \
			--env "CLARIFY_ISOLATION_TIMEOUT_SECS=${isolation_timeout}" \
			--workdir /source "${image}" /bin/bash -c '
				set -euo pipefail
				printf "{\"projects\":{\"/source\":{\"hasTrustDialogAccepted\":true}}}\n" > "${HOME}/.claude.json"
				python3 /relay.py bridge /socket/provider.sock &
				bridge_pid=$!
				trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
				python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
				if [ -n "${CLARIFY_ISOLATION_TIMEOUT_SECS}" ]; then
					timeout "${CLARIFY_ISOLATION_TIMEOUT_SECS}" claude -p --model "${CLAUDE_MODEL}" --effort "${CLAUDE_EFFORT}" \
						--system-prompt-file /instructions.md \
						--setting-sources "" --settings /settings.json \
						--strict-mcp-config --disable-slash-commands \
						--exclude-dynamic-system-prompt-sections \
						--tools Read,Grep,Glob --permission-mode dontAsk \
						--output-format stream-json --verbose < /prompt > /results/transcript.jsonl 2> /results/stderr
				else
					claude -p --model "${CLAUDE_MODEL}" --effort "${CLAUDE_EFFORT}" \
						--system-prompt-file /instructions.md \
						--setting-sources "" --settings /settings.json \
						--strict-mcp-config --disable-slash-commands \
						--exclude-dynamic-system-prompt-sections \
						--tools Read,Grep,Glob --permission-mode dontAsk \
						--output-format stream-json --verbose < /prompt > /results/transcript.jsonl 2> /results/stderr
				fi
			' || run_rc=$?
		kill "${broker_pid}" 2>/dev/null || true; wait "${broker_pid}" 2>/dev/null || true; broker_pid=""
		[ ! -f "${run_root}/results/stderr" ] || tee -a "${log_file}" < "${run_root}/results/stderr" >&2
		verdict="$(_ai_engine_py classify --transcript "${run_root}/results/transcript.jsonl" --exit-code "${run_rc}")" || verdict='{"outcome":"crashed","reason":"classify_failed"}'
		outcome="$(_ai_engine_json_field "${verdict}" outcome)"
		echo "CLAUDE_POOL run role=${engine_role} account=${account} outcome=${outcome} reason=$(_ai_engine_json_field "${verdict}" reason) exit_code=${run_rc}" >&2
		case "${outcome}" in
			success)
				_ai_engine_py extract --transcript "${run_root}/results/transcript.jsonl" --out "${run_root}/results/output" >&2 || exit 1
				install -m 0600 "${run_root}/results/output" "${output_file}"
				exit 0
				;;
			usage_limit|auth_failed)
				continue
				;;
			*)
				exit 1
				;;
		esac
	done
	ai_engine_fallback "${engine_role}" all_accounts_failed
	exit 75
fi

# Nothing from the privileged checkout, HOME or runtime workspace is mounted.
# The Docker build context contains only the pinned Dockerfile.
image="$(env -u OPENROUTER_API_KEY -u GH_TOKEN -u GITHUB_TOKEN docker build -q --build-arg "CODEX_VERSION=${version}" -f "${support}/clarify_sandbox/Dockerfile" "${support}/clarify_sandbox")"
[ -n "${image}" ] || { echo '::error::Clarify image build failed' >&2; exit 1; }
env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" CLARIFY_MODEL="${MODEL_EDITOR}" PYTHONDONTWRITEBYTECODE=1 \
	python3 -I -B "${support}/clarify_openrouter_broker.py" broker "${run_root}/socket/provider.sock" &
broker_pid=$!
for _ in $(seq 1 50); do
	[ -S "${run_root}/socket/provider.sock" ] && break
	kill -0 "${broker_pid}" 2>/dev/null || { echo '::error::Clarify broker failed' >&2; exit 1; }
	sleep 0.1
done
[ -S "${run_root}/socket/provider.sock" ] || { echo '::error::Clarify broker unavailable' >&2; exit 1; }

env -u OPENROUTER_API_KEY -u GH_TOKEN -u GITHUB_TOKEN docker run --rm \
	--name "${container_name}" --user "$(id -u):$(id -g)" \
	--network none --read-only --cap-drop ALL --security-opt no-new-privileges \
	--pids-limit 128 --memory 2g --cpus 2 \
	--tmpfs /tmp:rw,nosuid,nodev,size=64m --tmpfs /home/agent:rw,nosuid,nodev,size=64m,mode=1777 \
	--mount "type=bind,src=${run_root}/source,dst=/source,readonly" \
	--mount "type=bind,src=$(realpath "${prompt_file}"),dst=/prompt,readonly" \
	--mount "type=bind,src=${run_root}/socket,dst=/socket,readonly" \
	--mount "type=bind,src=$(realpath "${support}/clarify_openrouter_broker.py"),dst=/bridge.py,readonly" \
	--mount "type=bind,src=${run_root}/results,dst=/results" \
	--env HOME=/home/agent --env CODEX_HOME=/home/agent/.codex \
	--env CLARIFY_PROXY_KEY=isolated-placeholder \
	--env "CLARIFY_MODEL=${MODEL_EDITOR}" --env "CLARIFY_REASONING=${MODEL_REASONING_EFFORT}" \
	--env "CLARIFY_ISOLATION_TIMEOUT_SECS=${isolation_timeout}" \
	--workdir /source "${image}" /bin/bash -c '
		set -euo pipefail
		mkdir -p "${CODEX_HOME}"
		bash /source/scripts/write_codex_config.sh --model "${CLARIFY_MODEL:-openai/gpt-6-sol}" --reasoning "${CLARIFY_REASONING:-high}" --catalog-path /source/scripts/codex_model_catalog.json --project-path /source --config-path "${CODEX_HOME}/config.toml" --allow-elevation forbid
		sed -i -e '\''s#base_url = "https://openrouter.ai/api/v1"#base_url = "http://127.0.0.1:8765/api/v1"#'\'' -e '\''s/env_key = "OPENROUTER_API_KEY"/env_key = "CLARIFY_PROXY_KEY"/'\'' "${CODEX_HOME}/config.toml"
		python3 /bridge.py bridge /socket/provider.sock &
		bridge_pid=$!
		trap '\''kill "${bridge_pid}" 2>/dev/null || true'\'' EXIT
		# Bridge startup is bounded; model traffic stays on loopback.
		python3 -c '\''import socket,time; [(time.sleep(.1) if s.connect_ex(("127.0.0.1",8765)) else exit(0)) for s in (socket.socket() for _ in range(50))]; exit(1)'\''
		if [ -n "${CLARIFY_ISOLATION_TIMEOUT_SECS}" ]; then
			timeout "${CLARIFY_ISOLATION_TIMEOUT_SECS}" codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${CLARIFY_MODEL:-openai/gpt-6-sol}" --sandbox read-only < /prompt > /results/output 2> /results/stderr
		else
			codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${CLARIFY_MODEL:-openai/gpt-6-sol}" --sandbox read-only < /prompt > /results/output 2> /results/stderr
		fi
	' || {
		[ ! -f "${run_root}/results/stderr" ] || tee -a "${log_file}" < "${run_root}/results/stderr" >&2
		exit 1
	}
install -m 0600 "${run_root}/results/output" "${output_file}"
tee -a "${log_file}" < "${run_root}/results/stderr" >&2
