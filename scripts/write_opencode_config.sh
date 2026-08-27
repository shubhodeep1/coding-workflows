#!/usr/bin/env bash
# Write a role-scoped OpenCode configuration without resolving provider secrets.

set -euo pipefail

usage()
{
	cat <<'EOF'
Usage: write_opencode_config.sh --role reviewer|writer --model <slug>
                                --project-path <path> --config-path <path>
                                --serena on|off
EOF
}

fail()
{
	printf '::error::write_opencode_config.sh: %s\n' "$1" >&2
	exit 2
}

require_value()
{
	if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
		fail "$1 requires a non-empty value"
	fi
}

role=""
model_slug=""
project_path=""
config_path=""
serena_mode=""

while [ "$#" -gt 0 ]; do
	case "$1" in
		--role)
			require_value "$@"
			role="$2"
			shift 2
			;;
		--model)
			require_value "$@"
			model_slug="$2"
			shift 2
			;;
		--project-path)
			require_value "$@"
			project_path="$2"
			shift 2
			;;
		--config-path)
			require_value "$@"
			config_path="$2"
			shift 2
			;;
		--serena)
			require_value "$@"
			serena_mode="$2"
			shift 2
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			fail "unknown argument '$1'"
			;;
	esac
done

[ -n "${role}" ] || fail "--role is required"
[ -n "${model_slug}" ] || fail "--model is required"
[ -n "${project_path}" ] || fail "--project-path is required"
[ -n "${config_path}" ] || fail "--config-path is required"
[ -n "${serena_mode}" ] || fail "--serena is required"

case "${role}" in
	reviewer|writer) ;;
	*) fail "invalid --role '${role}' (expected reviewer|writer)" ;;
esac

case "${serena_mode}" in
	on|off) ;;
	*) fail "invalid --serena '${serena_mode}' (expected on|off)" ;;
esac

if [[ ! "${model_slug}" =~ ^[A-Za-z0-9][A-Za-z0-9._:+-]*(/[A-Za-z0-9][A-Za-z0-9._:+-]*)+$ ]]; then
	fail "invalid --model '${model_slug}'"
fi

[ -d "${project_path}" ] || fail "--project-path is not a directory: ${project_path}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
catalog_path="${OPENCODE_MODEL_CATALOG_PATH:-${script_dir}/codex_model_catalog.json}"
models_path="${OPENCODE_MODELS_PATH:-${XDG_CACHE_HOME:-${HOME}/.cache}/opencode/models.json}"

[ -r "${catalog_path}" ] || fail "model catalog is not readable: ${catalog_path}"
[ -r "${models_path}" ] || fail "models.dev cache is not readable: ${models_path}; run 'opencode models --refresh' first"

serena_bin=""
if [ "${serena_mode}" = "on" ]; then
	serena_bin="$(command -v serena || true)"
	if [ -z "${serena_bin}" ] || [ ! -x "${serena_bin}" ]; then
		fail "--serena on requires an executable serena binary on PATH"
	fi
fi

config_dir="$(dirname "${config_path}")"
mkdir -p "${config_dir}"

python3 - "${role}" "${model_slug}" "${project_path}" "${config_path}" \
	"${catalog_path}" "${models_path}" "${serena_bin}" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path


role, model_slug, project_path, config_path, catalog_path, models_path, serena_bin = sys.argv[1:]


def load_json(path: str, label: str) -> object:
	try:
		with open(path, encoding="utf-8") as handle:
			return json.load(handle)
	except (OSError, UnicodeError, json.JSONDecodeError) as exc:
		raise SystemExit(f"::error::write_opencode_config.sh: invalid {label} at {path}: {exc}") from exc


catalog = load_json(catalog_path, "model catalog")
if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
	raise SystemExit("::error::write_opencode_config.sh: model catalog has no models array")

catalog_rows = [row for row in catalog["models"] if isinstance(row, dict) and row.get("slug") == model_slug]
if len(catalog_rows) != 1:
	raise SystemExit(f"::error::write_opencode_config.sh: model '{model_slug}' is missing or duplicated in the model catalog")

context_limit = catalog_rows[0].get("context_window")
if not isinstance(context_limit, int) or isinstance(context_limit, bool) or context_limit <= 0:
	raise SystemExit(f"::error::write_opencode_config.sh: model '{model_slug}' has no positive context_window")

models_dev = load_json(models_path, "models.dev cache")
try:
	output_limit = models_dev["openrouter"]["models"][model_slug]["limit"]["output"]
except (KeyError, TypeError) as exc:
	raise SystemExit(
		f"::error::write_opencode_config.sh: model '{model_slug}' has no models.dev OpenRouter output limit"
	) from exc
if not isinstance(output_limit, (int, float)) or isinstance(output_limit, bool) or output_limit <= 0:
	raise SystemExit(f"::error::write_opencode_config.sh: model '{model_slug}' has an invalid models.dev output limit")

reviewer_permission = {
	"read": "allow",
	"glob": "allow",
	"grep": "allow",
	"list": "allow",
	"bash": "allow",
	"lsp": "allow",
	"todowrite": "allow",
	"skill": "allow",
	"edit": "deny",
	"task": "deny",
	"webfetch": "deny",
	"websearch": "deny",
	"question": "deny",
}
reviewer_tools = {
	"read": True,
	"glob": True,
	"grep": True,
	"list": True,
	"bash": True,
	"lsp": True,
	"todowrite": True,
	"skill": True,
	"edit": False,
	"write": False,
	"patch": False,
	"apply_patch": False,
	"task": False,
	"webfetch": False,
	"websearch": False,
	"question": False,
}
writer_permission = "allow"
writer_tools = {name: True for name in reviewer_tools}

permission = reviewer_permission if role == "reviewer" else writer_permission
tools = reviewer_tools if role == "reviewer" else writer_tools
qualified_model = f"openrouter/{model_slug}"

config = {
	"$schema": "https://opencode.ai/config.json",
	"autoupdate": False,
	"share": "disabled",
	"model": qualified_model,
	"small_model": qualified_model,
	"default_agent": role,
	"permission": permission,
	"tools": tools,
	"agent": {
		role: {
			"description": f"coding-workflows {role} role",
			"mode": "primary",
			"model": qualified_model,
			"permission": permission,
			"tools": tools,
		}
	},
	"enabled_providers": ["openrouter"],
	"provider": {
		"openrouter": {
			"npm": "@openrouter/ai-sdk-provider",
			"options": {
				"baseURL": "https://openrouter.ai/api/v1",
				"apiKey": "{env:OPENROUTER_API_KEY}",
			},
			"models": {
				model_slug: {
					"id": model_slug,
					"limit": {
						"context": context_limit,
						"output": output_limit,
					},
					"variants": {
						effort: {"reasoning": {"effort": effort}}
						for effort in ("none", "low", "medium", "high", "xhigh")
					},
				}
			},
		}
	},
}

if serena_bin:
	config["mcp"] = {
		"serena": {
			"type": "local",
			"command": [
				serena_bin,
				"start-mcp-server",
				"--context=codex",
				"--project-from-cwd",
				"--transport",
				"stdio",
			],
			"cwd": project_path,
			"enabled": True,
		}
	}

target = Path(config_path)
temporary_path = ""
try:
	with tempfile.NamedTemporaryFile(
		mode="w",
		encoding="utf-8",
		dir=target.parent,
		prefix=f".{target.name}.",
		suffix=".tmp",
		delete=False,
	) as temporary:
		temporary_path = temporary.name
		json.dump(config, temporary, indent=2, sort_keys=True)
		temporary.write("\n")
		temporary.flush()
		os.fsync(temporary.fileno())
	os.replace(temporary_path, target)
except (OSError, TypeError, ValueError) as exc:
	if temporary_path:
		try:
			os.unlink(temporary_path)
		except OSError:
			pass
	raise SystemExit(f"::error::write_opencode_config.sh: failed to write {target}: {exc}") from exc
PY
