#!/usr/bin/env bash
# build_semble_wrapper.sh — fail-soft, network-less Semble index and query launcher.
set -euo pipefail

log()
{
	printf 'build_semble_wrapper: %s\n' "$*" >&2
}

write_env_kv()
{
	local key="${1:?write_env_kv: key required}"
	local value="${2:?write_env_kv: value required}"
	if [ -n "${GITHUB_ENV:-}" ]; then
		printf '%s=%s\n' "${key}" "${value}" >> "${GITHUB_ENV}" 2>/dev/null || log "unable to write ${key} to GITHUB_ENV; continuing."
	fi
}

append_path_dir()
{
	if [ -n "${GITHUB_PATH:-}" ]; then
		printf '%s\n' "$1" >> "${GITHUB_PATH}" 2>/dev/null || log "unable to append wrapper directory to GITHUB_PATH; continuing."
	fi
}

resolve_paths()
{
	local default_root=""
	repo_root="${GITHUB_WORKSPACE:-${PWD}}"
	default_root="${RUNTIME_DIR:-${RUNNER_TEMP:-${PWD}}}"
	index_path="${SEMBLE_INDEX_PATH:-${default_root}/.semble-index}"
	wrapper_dir="${SEMBLE_WRAPPER_DIR:-$(dirname -- "${index_path}")/semble/bin}"
	wrapper_path="${wrapper_dir}/semble"
}

mark_unavailable()
{
	log "Semble wrapper unavailable: ${1:-unknown}"
	rm -rf -- "${index_path}" "${wrapper_path}" || true
	write_env_kv "SEMBLE_AVAILABLE" "false"
	write_env_kv "SEMBLE_INDEX_AVAILABLE" "false"
	write_env_kv "SEMBLE_INDEX_PATH" "${index_path}"
	exit 0
}

snapshot_repo()
{
	local src="$1" dst="$2" relative_runtime=""
	# GNU tar excludes nested .git too. Never follow symlinks during snapshot.
	# Runtime artifacts and the support checkouts may contain credentials.
	if [ -n "${RUNTIME_DIR:-}" ] && [[ "${RUNTIME_DIR}" == "${src}"/* ]]; then
		relative_runtime="./${RUNTIME_DIR#"${src}"/}"
	fi
	if [ -n "${relative_runtime}" ]; then
		tar -C "${src}" --exclude-vcs --exclude='./.codex-workflow-src*' --exclude="${relative_runtime}" -cf - . | tar -C "${dst}" -xf -
	else
		tar -C "${src}" --exclude-vcs --exclude='./.codex-workflow-src*' -cf - . | tar -C "${dst}" -xf -
	fi
}

cleanup_index_build()
{
	if [ -n "${index_container:-}" ]; then
		env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker rm -f "${index_container}" >/dev/null 2>&1 || true
	fi
	[ -z "${snapshot_dir:-}" ] || rm -rf -- "${snapshot_dir}"
	[ -z "${output_dir:-}" ] || rm -rf -- "${output_dir}"
	trap - TERM INT
}

build_index()
{
	local snapshot_dir="" output_dir="" index_container=""
	# trap RETURN also handles timeouts/failures before publishing the pickle.
	trap cleanup_index_build RETURN
	trap 'cleanup_index_build; exit 143' TERM
	trap 'cleanup_index_build; exit 130' INT
	snapshot_dir="$(mktemp -d)" || return 1
	output_dir="$(mktemp -d)" || return 1
	chmod 0700 "${snapshot_dir}" "${output_dir}" || return 1
	snapshot_repo "${repo_root}" "${snapshot_dir}" || return 1
	index_container="semble-index-$$-${RANDOM}"
	if ! timeout --kill-after=10s 600s env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm \
		--name "${index_container}" --network none --read-only --cap-drop ALL \
		--security-opt no-new-privileges --user "$(id -u):$(id -g)" \
		--pids-limit 128 --memory 3g --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,size=128m \
		--mount "type=bind,src=${snapshot_dir},dst=/repo,readonly" \
		--mount "type=bind,src=${output_dir},dst=/out" \
		"${image_id}" python /opt/semble/semble_index.py /repo /out/index.pkl; then
		return 1
	fi
	[ -f "${output_dir}/index.pkl" ] && [ ! -L "${output_dir}/index.pkl" ] && [ -s "${output_dir}/index.pkl" ] || return 1
	mkdir -p -- "$(dirname -- "${index_path}")" || return 1
	rm -rf -- "${index_path}" || return 1
	mv -- "${output_dir}/index.pkl" "${index_path}" || return 1
}

write_wrapper_body()
{
	local index_name=""
	index_name="$(basename -- "${index_path}")"
	printf '#!/usr/bin/env bash\nset -euo pipefail\n'
	printf 'image_id=%q\nindex_path=%q\nindex_name=%q\n' "${image_id}" "${index_path}" "${index_name}"
	cat <<'BASH'
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
	printf 'semble 0.1.3\n'
	exit 0
fi
if [ "$#" -lt 8 ] || [ "$1" != "query" ] || [ "$3" != "--index" ] ||
   [ "$5" != "--top-k" ] || [ "$7" != "--format" ] ||
   [ "$4" != "${index_path}" ] || [ "$8" != "text" ] ||
   [[ ! "$6" =~ ^[1-9][0-9]*$ ]]; then
	printf 'invalid semble query arguments\n' >&2
	exit 2
fi
query_text="$2"
top_k="$6"
container_name="semble-q-$$-${RANDOM}"
# Mount the single opaque index, not its directory: RUNTIME_DIR can also
# contain provider credentials and prompt artifacts from the caller.
if [ ! -f "${index_path}" ] || [ -L "${index_path}" ]; then
	printf 'Semble index is missing or is a symlink\n' >&2
	exit 1
fi
cleanup_query()
{
	env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker rm -f "${container_name}" >/dev/null 2>&1 || true
}
trap 'cleanup_query; exit 143' TERM
trap 'cleanup_query; exit 130' INT
trap cleanup_query EXIT
env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm \
	--name "${container_name}" --network none --read-only --cap-drop ALL \
	--security-opt no-new-privileges --user "$(id -u):$(id -g)" \
	--pids-limit 64 --memory 1g --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=32m \
	--mount "type=bind,src=${index_path},dst=/index/${index_name},readonly" \
	"${image_id}" python /opt/semble/semble_query.py query -- "${query_text}" \
	--index "/index/${index_name}" --top-k "${top_k}" --format text
BASH
}

write_wrapper()
{
	mkdir -p -- "${wrapper_dir}" || return 1
	rm -rf -- "${wrapper_path}" || return 1
	write_wrapper_body > "${wrapper_path}" || return 1
	chmod +x "${wrapper_path}" || return 1
}

main()
{
	local repo_root="" index_path="" wrapper_dir="" wrapper_path="" image_id=""
	resolve_paths
	if ! command -v docker >/dev/null 2>&1 || ! command -v timeout >/dev/null 2>&1; then
		mark_unavailable "docker-unavailable"
	fi
	# Inspect even a supplied ID to ensure it refers to a local built image.
	image_id="$(env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker image inspect --format '{{.Id}}' \
		"${SEMBLE_SANDBOX_IMAGE_ID:-${SEMBLE_SANDBOX_IMAGE:-coding-workflows-semble-sandbox:0.1.3}}" 2>/dev/null)" || mark_unavailable "sandbox-image-missing"
	[[ "${image_id}" =~ ^sha256:[a-f0-9]{64}$ ]] || mark_unavailable "sandbox-image-missing"
	# Bind mounts cannot contain commas/newlines. Keep the index path fixed
	# in the launcher rather than accepting a caller-controlled mount.
	[[ "${index_path}" == /* && "${repo_root}" == /* && "${wrapper_dir}" == /* ]] || mark_unavailable "invalid-path"
	[[ "${index_path}${repo_root}${wrapper_dir}" != *$'\n'* && "${index_path}${repo_root}${wrapper_dir}" != *,* ]] || mark_unavailable "invalid-path"
	if ! build_index || ! write_wrapper; then
		mark_unavailable "index-build-failed"
	fi
	append_path_dir "${wrapper_dir}"
	write_env_kv "SEMBLE_AVAILABLE" "true"
	write_env_kv "SEMBLE_INDEX_AVAILABLE" "true"
	write_env_kv "SEMBLE_INDEX_PATH" "${index_path}"
	write_env_kv "SEMBLE_BIN" "${wrapper_path}"
	log "Semble sandbox wrapper ready."
}

main "$@"
