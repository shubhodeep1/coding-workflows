#!/usr/bin/env bash
# Run one clarify attempt in a credential-free, network-isolated container.
# Only this helper (not the agent) accesses Docker and the host-side broker.
set -euo pipefail

prompt_file="${1:?prompt file required}"
output_file="${2:?output file required}"
log_file="${3:?log file required}"
version="${CLARIFY_CODEX_VERSION:-v0.114.0}"
[[ "${version}" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo '::error::Invalid Codex version' >&2; exit 1; }
[[ "${MODEL_EDITOR:-}" =~ ^[a-zA-Z0-9/_.-]+$ ]] || { echo '::error::Invalid model slug' >&2; exit 1; }
[[ "${MODEL_REASONING_EFFORT:-}" =~ ^(xhigh|high|medium|low|none)$ ]] || { echo '::error::Invalid reasoning level' >&2; exit 1; }
[ -n "${OPENROUTER_API_KEY:-}" ] && [ -s "${prompt_file}" ] || { echo '::error::Clarify isolation preflight failed' >&2; exit 1; }
command -v docker >/dev/null && command -v python3 >/dev/null || { echo '::error::Clarify isolation prerequisites unavailable' >&2; exit 1; }
[ -f scripts/clarify_sandbox/Dockerfile ] && [ -f scripts/clarify_openrouter_broker.py ] && [ -f scripts/write_codex_config.sh ] && [ -f scripts/codex_model_catalog.json ] || { echo '::error::Clarify isolation support missing' >&2; exit 1; }

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
# Implement checkouts may keep the Git database outside the working tree.
# Pass that runner context as positional data, not as interpreter environment.
git_context=()
if [ -n "${GIT_DIR:-}" ] || [ -n "${GIT_WORK_TREE:-}" ]; then
	[ -n "${GIT_DIR:-}" ] && [ -n "${GIT_WORK_TREE:-}" ] || { echo '::error::Clarify Git context incomplete' >&2; exit 1; }
	git_context=("${GIT_DIR}" "${GIT_WORK_TREE}")
fi
env -i PATH=/usr/local/bin:/usr/bin:/bin python3 -I -B - "${run_root}/source" "${git_context[@]}" <<'PY'
import os
import pathlib
import stat
import subprocess
import sys

root = pathlib.Path.cwd()
dest = pathlib.Path(sys.argv[1])
git_command = ["git"]
if len(sys.argv) == 4:
    git_dir = pathlib.Path(sys.argv[2])
    work_tree = pathlib.Path(sys.argv[3])
    if not git_dir.is_absolute() or not git_dir.is_dir() or not work_tree.is_absolute() or work_tree.resolve() != root.resolve():
        raise SystemExit("clarify Git context rejected")
    git_command += ["--git-dir", str(git_dir), "--work-tree", str(root)]
roots = {"src", "scripts", "tests", "prompts", "docs", "app", "lib", "workflow-templates", "validation", "db", "ai-memory", "changelog.d"}
root_files = {"README.md", "agents.md", "AGENTS.md", "package.json", "pyproject.toml", "go.mod", "Cargo.toml"}
suffixes = {".py", ".sh", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".json", ".md", ".yml", ".yaml", ".toml", ".txt", ".css", ".html", ".sql"}
bad_parts = {".git", ".ai", ".codex", ".codex-workflow-src", ".codex-workflow-src-main", ".env", "secrets", "credentials", "__pycache__"}
count = 0
total = 0

def copy(path):
    global count, total
    parts = pathlib.PurePosixPath(path).parts
    if (not parts or any(part.lower() in bad_parts or part.lower().startswith(".env") for part in parts)
            or any(part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".keystore")) for part in parts)
            or (path not in root_files and parts[0] not in roots and parts[:2] not in ((".github", "workflows"), (".github", "actions")))
            or (path not in root_files and pathlib.PurePosixPath(path).suffix.lower() not in suffixes and parts[-1] != "Dockerfile")):
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
    tracked = subprocess.check_output([*git_command, "ls-files", "-z"], cwd=root).split(b"\0")
    for entry in tracked:
        if entry:
            copy(entry.decode("utf-8"))
    # Consumer checkouts do not track the staged writer/catalog; these are
    # fixed support files, never read from the issue prompt or user input.
    for required in ("scripts/write_codex_config.sh", "scripts/codex_model_catalog.json"):
        if not (dest / required).exists():
            copy(required)
except (OSError, ValueError, UnicodeError) as exc:
    raise SystemExit("clarify source snapshot rejected") from None
PY

# Nothing from the privileged checkout, HOME or runtime workspace is mounted.
# The Docker build context contains only the pinned Dockerfile.
image="$(env -u OPENROUTER_API_KEY -u GH_TOKEN -u GITHUB_TOKEN docker build -q --build-arg "CODEX_VERSION=${version}" -f scripts/clarify_sandbox/Dockerfile scripts/clarify_sandbox)"
[ -n "${image}" ] || { echo '::error::Clarify image build failed' >&2; exit 1; }
# env -i drops everything else; the #4090 budgets must reach the broker.
broker_budget_env=()
for budget_var in MAX_REQUESTS MAX_OUTPUT_TOKENS MAX_TOTAL_OUTPUT_TOKENS MAX_INPUT_TOKENS MAX_TOTAL_INPUT_TOKENS MAX_TOTAL_COST_USD MAX_PROMPT_PRICE MAX_COMPLETION_PRICE MAX_REQUEST_PRICE MAX_IMAGE_PRICE; do
	budget_var="MODEL_PROVIDER_BROKER_${budget_var}"
	[ -z "${!budget_var:-}" ] || broker_budget_env+=("${budget_var}=${!budget_var}")
done
env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" CLARIFY_MODEL="${MODEL_EDITOR}" PYTHONDONTWRITEBYTECODE=1 "${broker_budget_env[@]}" \
	python3 scripts/clarify_openrouter_broker.py broker "${run_root}/socket/provider.sock" &
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
	--mount "type=bind,src=$(realpath scripts/clarify_openrouter_broker.py),dst=/bridge.py,readonly" \
	--mount "type=bind,src=${run_root}/results,dst=/results" \
	--env HOME=/home/agent --env CODEX_HOME=/home/agent/.codex \
	--env CLARIFY_PROXY_KEY=isolated-placeholder \
	--env "CLARIFY_MODEL=${MODEL_EDITOR}" --env "CLARIFY_REASONING=${MODEL_REASONING_EFFORT}" \
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
		codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${CLARIFY_MODEL:-openai/gpt-6-sol}" --sandbox read-only < /prompt > /results/output 2> /results/stderr
	' || {
		[ ! -f "${run_root}/results/stderr" ] || tee -a "${log_file}" < "${run_root}/results/stderr" >&2
		exit 1
	}
install -m 0600 "${run_root}/results/output" "${output_file}"
tee -a "${log_file}" < "${run_root}/results/stderr" >&2
