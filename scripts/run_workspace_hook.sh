#!/usr/bin/env bash
set -euo pipefail

usage()
{
	cat <<'USAGE' >&2
Usage: run_workspace_hook.sh <phase> <hook>

Hooks:
	after_create
	before_run
	after_run
	before_remove
USAGE
}

fail()
{
	printf 'workspace_hook: %s\n' "$*" >&2
	exit 1
}

is_falsey()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		0|false|no|off)
			return 0
			;;
	esac
	return 1
}

is_fatal_hook()
{
	case "${1:-}" in
		after_create|before_run)
			return 0
			;;
	esac
	return 1
}

emit_log_tail()
{
	local log_file="$1"
	local max_bytes="${2:-10240}"
	local total_bytes="0"

	if [ ! -f "${log_file}" ]; then
		printf '%s\n' '(no workspace hook log captured)' >&2
		return 0
	fi

	if ! total_bytes="$(wc -c < "${log_file}" 2>/dev/null | tr -d '[:space:]')" || ! [[ "${total_bytes}" =~ ^[0-9]+$ ]]; then
		total_bytes="0"
	fi
	if [ "${total_bytes}" -gt "${max_bytes}" ]; then
		printf '%s\n' "--- last ${max_bytes} bytes of ${log_file} (${total_bytes} total bytes) ---" >&2
	else
		printf '%s\n' "--- ${log_file} (${total_bytes} bytes) ---" >&2
	fi
	tail -c "${max_bytes}" "${log_file}" >&2 || true
	printf '\n%s\n' '--- end workspace hook log ---' >&2
}

report_hook_failure()
{
	local phase="$1"
	local hook="$2"
	local message="$3"
	local log_file="${4:-}"
	local exit_code="${5:-1}"

	if is_fatal_hook "${hook}" || { [ "${phase}" = "validate" ] && [ "${exit_code}" -eq 125 ]; }; then
		printf '::error::Workspace hook %s/%s %s\n' "${phase}" "${hook}" "${message}" >&2
		if [ -n "${log_file}" ]; then
			emit_log_tail "${log_file}" "10240"
		fi
		exit "${exit_code}"
	fi

	printf '::warning::Workspace hook %s/%s %s\n' "${phase}" "${hook}" "${message}" >&2
	if [ -n "${log_file}" ]; then
		emit_log_tail "${log_file}" "10240"
	fi
	return 0
}

# Validation hooks are branch-authored code. Stage only screened regular files,
# never the host checkout, environment, Docker socket, or runner home.
run_validate_hook_isolated()
{
	local workspace_path="$1"
	local hook="$2"
	local timeout_seconds="$3"
	local runner_temp="$4"
	local isolated_root isolated_status container_name
	command -v docker >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 || return 125
	isolated_root="$(mktemp -d "${runner_temp}/validate-hook-XXXXXXXX")" || return 125
	container_name="validate-hook-$$"
	cleanup_validate_isolation()
	{
		env -i PATH="${PATH}" HOME=/nonexistent DOCKER_CONFIG=/nonexistent \
			docker rm -f "${container_name}" >/dev/null 2>&1 || true
		rm -rf -- "${isolated_root}"
	}
	trap 'cleanup_validate_isolation; exit 143' TERM
	# Snapshot and manifest are separate; the container can only write the copy.
	if ! env -u GH_TOKEN -u GH_PAT -u GITHUB_TOKEN -u PYTHONPATH -u GITHUB_ENV -u GITHUB_OUTPUT -u GITHUB_PATH \
		PYTHONDONTWRITEBYTECODE=1 python3 -I - "${workspace_path}" "${isolated_root}" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
stage = Path(sys.argv[2]) / "source"
stage.mkdir(mode=0o700)
excluded = {".git", ".ai", ".ssh", ".aws", ".config", ".codex", ".codex-workflow-src", ".codex-workflow-src-main", "node_modules", "__pycache__", "secrets", "credentials"}
bad_suffixes = (".pem", ".key", ".p12", ".pfx", ".keystore", ".pyc")
manifest = {}
total = 0

def allowed(parts):
    return (not any(part.lower() in excluded or part.lower().startswith((".env", "secret", "credential"))
                    or part.lower().endswith(bad_suffixes)
                    or (part.startswith(".") and part != ".github") for part in parts)
            and (parts[0] != ".github" or parts[:3] == (".github", "ai", "workspace_hooks")
                 or parts in ((".github",), (".github", "ai"))))

for current, dirs, files in os.walk(root, followlinks=False):
    relative = Path(current).relative_to(root)
    dirs[:] = [name for name in dirs if allowed(relative.parts + (name,))]
    for name in files:
        parts = relative.parts + (name,)
        if not allowed(parts):
            continue
        source = Path(current) / name
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
            raise SystemExit("validation hook snapshot rejected")
        if len(manifest) >= 5000 or total + info.st_size > 64 * 1024 * 1024:
            raise SystemExit("validation hook snapshot limit exceeded")
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
                raise SystemExit("validation hook source changed")
            data = stream.read(2 * 1024 * 1024 + 1)
        if len(data) != info.st_size:
            raise SystemExit("validation hook source changed")
        target = stage.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        mode = stat.S_IMODE(info.st_mode) & 0o755
        target.chmod(mode)
        manifest["/".join(parts)] = {"digest": hashlib.sha256(data).hexdigest(), "mode": mode}
        total += len(data)

(stage.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
PY
	then
		cleanup_validate_isolation
		return 125
	fi
	# The screened copy is read-only. A size-limited tmpfs holds mutations;
	# neither the writable workspace nor the runner's filesystem is mounted.
	if ! env -i PATH="${PATH}" HOME=/nonexistent DOCKER_CONFIG=/nonexistent \
		docker image inspect python:3.12-slim >/dev/null 2>&1; then
		if ! env -i PATH="${PATH}" HOME=/nonexistent DOCKER_CONFIG=/nonexistent \
			docker pull --quiet python:3.12-slim >/dev/null; then
			cleanup_validate_isolation
			return 125
		fi
	fi
	set +e
	(
		ulimit -f 90000
		env -i PATH="${PATH}" HOME=/nonexistent DOCKER_CONFIG=/nonexistent docker run --rm \
			--pull=never \
			--name "${container_name}" \
			--user "$(id -u):$(id -g)" --network none --read-only \
			--cap-drop ALL --security-opt no-new-privileges --pids-limit 128 \
			--memory 512m --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,size=32m \
			--tmpfs /workspace:rw,nosuid,nodev,size=80m,mode=1777 \
			--mount "type=bind,src=${isolated_root}/source,dst=/source,readonly" \
			--env HOME=/tmp --env PYTHONDONTWRITEBYTECODE=1 \
			--env "VALIDATE_HOOK_NAME=${hook}" --workdir /workspace \
			python:3.12-slim /bin/bash -c '
				set -euo pipefail
				cp -R /source/. /workspace/
				bash ".github/ai/workspace_hooks/validate/${VALIDATE_HOOK_NAME}.sh" >&2
				tar -C /workspace -cf - .
			' > "${isolated_root}/result.tar"
	)
	isolated_status=$?
	set -e
	if [ "${isolated_status}" -ne 0 ]; then
		# Docker's own infrastructure failures use 125; other nonzero statuses
		# retain the ordinary hook fatality contract.
		cleanup_validate_isolation
		return "${isolated_status}"
	fi
	# Never extract an untrusted tar entry directly onto the host workspace.
	if ! env -u GH_TOKEN -u GH_PAT -u GITHUB_TOKEN -u PYTHONPATH -u GITHUB_ENV -u GITHUB_OUTPUT -u GITHUB_PATH \
		PYTHONDONTWRITEBYTECODE=1 python3 -I - "${isolated_root}" <<'PY'
import os
from pathlib import Path, PurePosixPath
import tarfile
import sys

stage_root = Path(sys.argv[1])
returned = stage_root / "returned"
returned.mkdir(mode=0o700)
with tarfile.open(stage_root / "result.tar", "r:") as archive:
    entries = archive.getmembers()
    if len(entries) > 10000 or sum(entry.size for entry in entries) > 64 * 1024 * 1024:
        raise SystemExit("validation hook archive limit exceeded")
    names = set()
    for entry in entries:
        parts = PurePosixPath(entry.name).parts
        if parts and parts[0] == ".":
            parts = parts[1:]
        if not parts:
            continue
        if (any(part in ("", ".", "..") for part in parts) or entry.name.startswith("/")
                or not (entry.isdir() or entry.isfile()) or parts in names):
            raise SystemExit("validation hook archive contains unsafe entry")
        names.add(parts)
        target = returned.joinpath(*parts)
        if entry.isdir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            if entry.size > 2 * 1024 * 1024:
                raise SystemExit("validation hook returned oversized file")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(entry) as stream, target.open("xb") as sink:
                sink.write(stream.read(2 * 1024 * 1024 + 1))
            target.chmod(entry.mode & 0o755)
PY
	then
		cleanup_validate_isolation
		return 125
	fi
	# Validate the entire returned tree before changing the host workspace.
	if ! env -u GH_TOKEN -u GH_PAT -u GITHUB_TOKEN -u PYTHONPATH -u GITHUB_ENV -u GITHUB_OUTPUT -u GITHUB_PATH \
		PYTHONDONTWRITEBYTECODE=1 python3 -I - "${workspace_path}" "${isolated_root}" "${hook}" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

root = Path(sys.argv[1])
stage_root = Path(sys.argv[2])
hook_name = sys.argv[3]
stage = stage_root / "returned"
original = json.loads((stage_root / "manifest.json").read_text(encoding="utf-8"))
excluded = {".git", ".ai", ".ssh", ".aws", ".config", ".codex", ".codex-workflow-src", ".codex-workflow-src-main", "node_modules", "__pycache__", "secrets", "credentials"}
bad_suffixes = (".pem", ".key", ".p12", ".pfx", ".keystore", ".pyc")
updates = {}
total = 0

for current, dirs, files in os.walk(stage, followlinks=False):
    relative = Path(current).relative_to(stage)
    for name in dirs + files:
        parts = relative.parts + (name,)
        if (any(part.lower() in excluded or part.lower().startswith((".env", "secret", "credential"))
                or part.lower().endswith(bad_suffixes)
                or (part.startswith(".") and part != ".github") for part in parts)
                or (parts[0] == ".github" and parts[:3] != (".github", "ai", "workspace_hooks")
                    and parts not in ((".github",), (".github", "ai")))):
            raise SystemExit("validation hook returned forbidden path")
        node = Path(current) / name
        info = node.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit("validation hook returned symlink")
        if name in files:
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                raise SystemExit("validation hook returned unsafe file")
            with os.fdopen(os.open(node, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                data = stream.read(2 * 1024 * 1024 + 1)
            if len(data) != info.st_size:
                raise SystemExit("validation hook output changed")
            updates["/".join(parts)] = (data, stat.S_IMODE(info.st_mode) & 0o755)
            total += len(data)
            if len(updates) > 5000 or total > 64 * 1024 * 1024:
                raise SystemExit("validation hook output limit exceeded")

changes = {name: result for name, result in updates.items()
           if {"digest": hashlib.sha256(result[0]).hexdigest(), "mode": result[1]} != original.get(name)}
deletions = set(original) - set(updates)
# Only inert, hook-owned data may cross from the untrusted container into the
# credentialed host workspace. Refuse the entire result before any replay.
if hook_name not in ("after_create", "before_run", "after_run", "before_remove"):
    raise SystemExit("validation hook name is invalid")
allowed_prefix = f"validation/hook-output/{hook_name}/"
for name in changes.keys() | deletions:
    if not name.startswith(allowed_prefix) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.txt", name[len(allowed_prefix):]):
        raise SystemExit("validation hook returned disallowed mutation")
for data, guest_mode in changes.values():
    if guest_mode & 0o111:
        raise SystemExit("validation hook returned executable output")

# Open every parent relative to the same workspace fd, rejecting symlinks at
# preflight and again at mutation time. Dirfd operations also keep a parent
# symlink swap from redirecting an unlink or atomic replacement outside it.
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
workspace_fd = os.open(root, directory_flags)
def checked_parent(name, create=False):
    parent_fd = os.dup(workspace_fd)
    try:
        for component in name.split("/")[:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    os.close(parent_fd)
                    return None
                os.mkdir(component, mode=0o755, dir_fd=parent_fd)
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise

try:
    for name in changes.keys() | deletions:
        parent_fd = checked_parent(name)
        if parent_fd is None:
            continue
        try:
            try:
                target_info = os.stat(name.rsplit("/", 1)[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(target_info.st_mode):
                raise SystemExit("validation hook target is unsafe")
        finally:
            os.close(parent_fd)

    for name in deletions:
        parent_fd = checked_parent(name)
        if parent_fd is None:
            continue
        try:
            os.unlink(name.rsplit("/", 1)[-1], dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    for name, (data, guest_mode) in changes.items():
        parent_fd = checked_parent(name, create=True)
        filename = name.rsplit("/", 1)[-1]
        temporary = ".validate-hook-" + secrets.token_hex(16)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.chmod(temporary, guest_mode & 0o666, dir_fd=parent_fd, follow_symlinks=False)
            os.replace(temporary, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
finally:
    os.close(workspace_fd)
PY
	then
		cleanup_validate_isolation
		return 125
	fi
	cleanup_validate_isolation
}

main()
{
	if [ "$#" -ne 2 ]; then
		usage
		exit 64
	fi

	local phase="$1"
	local hook="$2"
	local timeout_seconds="${WORKSPACE_HOOK_TIMEOUT_SECONDS:-600}"
	local script_dir repo_root hook_path runner_temp workspace_path log_dir log_file
	local run_status="0"
	local run_started_at="0"
	local run_elapsed="0"
	local status_text

	case "${phase}" in
		''|*[!A-Za-z0-9._-]*)
			fail "unsupported phase: ${phase}"
			;;
	esac
	case "${hook}" in
		after_create|before_run|after_run|before_remove)
			;;
		*)
			fail "unsupported hook: ${hook}"
			;;
	esac

	if [ "${hook}" = "after_create" ] && is_falsey "${CREATED_NOW:-}"; then
		exit 0
	fi

	script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
	repo_root="$(cd "${script_dir}/.." && pwd -P)"
	if [ "${phase}" = "validate" ]; then
		hook_path="${WORKSPACE_PATH:-}/.github/ai/workspace_hooks/${phase}/${hook}.sh"
	else
		hook_path="${repo_root}/.github/ai/workspace_hooks/${phase}/${hook}.sh"
	fi
	if [ ! -s "${hook_path}" ]; then
		exit 0
	fi

	if ! [[ "${timeout_seconds}" =~ ^[0-9]+$ ]] || [ "${timeout_seconds}" -le 0 ]; then
		printf '::warning::Invalid WORKSPACE_HOOK_TIMEOUT_SECONDS=%s; defaulting to 600.\n' "${timeout_seconds}" >&2
		timeout_seconds="600"
	fi

	runner_temp="${RUNNER_TEMP:-}"
	if [ -z "${runner_temp}" ]; then
		report_hook_failure "${phase}" "${hook}" 'could not start because RUNNER_TEMP is unset.' "" 1
	fi

	workspace_path="${WORKSPACE_PATH:-}"
	if [ -z "${workspace_path}" ] || [ ! -d "${workspace_path}" ]; then
		report_hook_failure "${phase}" "${hook}" "could not start because WORKSPACE_PATH is missing or does not exist: ${workspace_path:-<unset>}." "" 1
	fi

	log_dir="${runner_temp}/workspace-hooks"
	log_file="${log_dir}/${phase}-${hook}.log"
	mkdir -p "${log_dir}"
	: > "${log_file}"

	run_started_at="$(date +%s)"
	set +e
	(
		if [ "${phase}" = "validate" ]; then
			timeout --kill-after=10s "${timeout_seconds}" bash "${BASH_SOURCE[0]}" --isolated \
				"${workspace_path}" "${hook}" "${timeout_seconds}" "${runner_temp}"
		else
			cd "${workspace_path}" && \
			timeout --kill-after=10s "${timeout_seconds}" bash -lc 'bash "$1"' bash "${hook_path}"
		fi
	) >"${log_file}" 2>&1
	run_status="$?"
	set -e
	run_elapsed=$(( $(date +%s) - run_started_at ))

	if [ "${run_status}" -eq 0 ]; then
		exit 0
	fi

	status_text="failed with exit code ${run_status}. Log: ${log_file}."
	if [ "${run_status}" -eq 124 ]; then
		status_text="timed out after ${timeout_seconds} seconds. Log: ${log_file}."
	elif [ "${run_status}" -eq 137 ] && [ "${run_elapsed}" -ge "${timeout_seconds}" ]; then
		status_text="timed out after ${timeout_seconds} seconds (SIGKILL after grace period). Log: ${log_file}."
	fi
	report_hook_failure "${phase}" "${hook}" "${status_text}" "${log_file}" "${run_status}"
}

if [ "${1:-}" = "--isolated" ]; then
	shift
	run_validate_hook_isolated "$@"
else
	main "$@"
fi
