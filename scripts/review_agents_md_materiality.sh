#!/usr/bin/env bash
set -euo pipefail

PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path, PurePosixPath


COMMENT_MARKER = "<!-- AI_MATERIALITY_ADVISORY_V1 -->"
ADVISORY_HEADING = "## AI Materiality Advisory"
ROOT_HIGH_PATHS = {"package.json", "pyproject.toml", "Cargo.toml", "go.mod"}
HIGH_BUILD_TEST_BASENAMES = {
	"pytest.ini",
	"tox.ini",
	"noxfile.py",
	"jest.config.js",
	"jest.config.cjs",
	"jest.config.mjs",
	"jest.config.ts",
	"vitest.config.js",
	"vitest.config.cjs",
	"vitest.config.mjs",
	"vitest.config.ts",
	"playwright.config.js",
	"playwright.config.cjs",
	"playwright.config.mjs",
	"playwright.config.ts",
	"cypress.config.js",
	"cypress.config.cjs",
	"cypress.config.mjs",
	"cypress.config.ts",
	"webpack.config.js",
	"webpack.config.cjs",
	"webpack.config.mjs",
	"webpack.config.ts",
	"vite.config.js",
	"vite.config.cjs",
	"vite.config.mjs",
	"vite.config.ts",
	"turbo.json",
	"go.work",
}
MEDIUM_DEPENDENCY_BASENAMES = {
	"package-lock.json",
	"bun.lock",
	"bun.lockb",
	"yarn.lock",
	"pnpm-lock.yaml",
	"Cargo.lock",
	"poetry.lock",
	"uv.lock",
	"go.sum",
	"Pipfile",
	"Pipfile.lock",
}
MEDIUM_LINT_BASENAMES = {
	".eslintrc",
	".eslintrc.json",
	".eslintrc.js",
	".eslintrc.cjs",
	".eslintrc.yaml",
	".eslintrc.yml",
	"eslint.config.js",
	"eslint.config.cjs",
	"eslint.config.mjs",
	"eslint.config.ts",
	".prettierrc",
	".prettierrc.json",
	".prettierrc.js",
	".prettierrc.cjs",
	".stylelintrc",
	".stylelintrc.json",
	".stylelintrc.js",
	"stylelint.config.js",
	"stylelint.config.cjs",
	"stylelint.config.mjs",
	"stylelint.config.ts",
	"ruff.toml",
	".ruff.toml",
	".flake8",
	"pylintrc",
	"biome.json",
	"biome.jsonc",
}
CLIENT_WRAPPER_BASENAMES = {
	"client.py",
	"client.ts",
	"client.js",
	"client.tsx",
	"client.jsx",
	"client.go",
	"client.rb",
	"client.java",
}
CLIENT_WRAPPER_TOKENS = (
	"api_client",
	"api-client",
	"http_client",
	"http-client",
	"github_client",
	"github-client",
	"rest_client",
	"rest-client",
	"sdk_client",
	"sdk-client",
)
DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def normalize_bool(value: str | None) -> bool:
	return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def ensure_parent(path: Path) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)


def write_outputs(*, result_path: Path, comment_path: Path, result: dict[str, object], comment_body: str) -> None:
	ensure_parent(result_path)
	ensure_parent(comment_path)
	result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	comment_path.write_text(comment_body, encoding="utf-8")


def load_changed_paths(*, changed_files_path: Path, diff_path: Path | None) -> list[str]:
	paths: list[str] = []
	seen: set[str] = set()

	if changed_files_path.is_file():
		for raw_line in changed_files_path.read_text(encoding="utf-8", errors="replace").splitlines():
			path = raw_line.strip()
			if not path or path in seen:
				continue
			seen.add(path)
			paths.append(path)
	if paths:
		return paths

	if diff_path is None or not diff_path.is_file():
		return []

	for raw_line in diff_path.read_text(encoding="utf-8", errors="replace").splitlines():
		match = DIFF_HEADER_RE.match(raw_line)
		if not match:
			continue
		path = match.group(2).strip()
		if not path or path in seen:
			continue
		seen.add(path)
		paths.append(path)
	return paths


def looks_like_client_wrapper(path: str) -> bool:
	pure = PurePosixPath(path)
	basename = pure.name.lower()
	if basename in CLIENT_WRAPPER_BASENAMES:
		parent_parts = [part.lower() for part in pure.parts[:-1]]
		if any(part in {"api", "apis", "client", "clients", "http", "sdk"} for part in parent_parts):
			return True
	if any(token in basename for token in CLIENT_WRAPPER_TOKENS):
		return True
	return False


def classify_path(path: str) -> dict[str, str] | None:
	pure = PurePosixPath(path)
	basename = pure.name
	if path in ROOT_HIGH_PATHS:
		return {
			"severity": "high",
			"rule": "root-manifest",
			"path": path,
			"detail": "root package/build manifest changed",
		}
	if path.startswith(".github/workflows/"):
		return {
			"severity": "high",
			"rule": "workflow",
			"path": path,
			"detail": "workflow definition changed",
		}
	if "/" not in path and basename in HIGH_BUILD_TEST_BASENAMES:
		return {
			"severity": "high",
			"rule": "build-test-config",
			"path": path,
			"detail": "build/test framework config changed",
		}
	if basename in MEDIUM_DEPENDENCY_BASENAMES or (basename.startswith("requirements") and basename.endswith(".txt")) or (basename.startswith("constraints") and basename.endswith(".txt")):
		return {
			"severity": "medium",
			"rule": "dependency-config",
			"path": path,
			"detail": "dependency lockfile or requirements manifest changed",
		}
	if basename in MEDIUM_LINT_BASENAMES:
		return {
			"severity": "medium",
			"rule": "lint-config",
			"path": path,
			"detail": "lint or formatting config changed",
		}
	if looks_like_client_wrapper(path):
		return {
			"severity": "medium",
			"rule": "api-client-wrapper",
			"path": path,
			"detail": "API-client wrapper path changed",
		}
	return None


def detect_new_top_level_dirs(repo_root: Path, base_branch: str) -> list[str]:
	if not base_branch:
		return []
	if not (repo_root / ".git").exists():
		return []
	base_ref = f"origin/{base_branch}"
	verify = subprocess.run(
		["git", "rev-parse", "--verify", base_ref],
		cwd=repo_root,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	if verify.returncode != 0:
		return []
	status = subprocess.run(
		["git", "diff", "--name-status", "-M", "--diff-filter=AR", f"{base_ref}...HEAD"],
		cwd=repo_root,
		capture_output=True,
		text=True,
	)
	if status.returncode != 0:
		return []
	new_dirs: list[str] = []
	seen: set[str] = set()
	for raw_line in status.stdout.splitlines():
		parts = raw_line.split("\t")
		if len(parts) < 2:
			continue
		code = parts[0]
		if code.startswith("R") and len(parts) >= 3:
			candidate_path = parts[2].strip()
		elif code.startswith("A"):
			candidate_path = parts[1].strip()
		else:
			continue
		if "/" not in candidate_path:
			continue
		top_level = candidate_path.split("/", 1)[0]
		if not top_level or top_level in seen:
			continue
		check = subprocess.run(
			["git", "cat-file", "-e", f"{base_ref}:{top_level}"],
			cwd=repo_root,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
		if check.returncode == 0:
			continue
		seen.add(top_level)
		new_dirs.append(top_level)
	return new_dirs


def build_comment_body(*, materiality: str, matches: list[dict[str, str]], run_url: str) -> str:
	lines = [
		COMMENT_MARKER,
		ADVISORY_HEADING,
		"",
		f"This PR looks **{materiality}** materiality under the deterministic AGENTS.md path rules, but root `agents.md` is unchanged.",
		"",
		"Signals:",
	]
	for match in matches[:5]:
		lines.append(f"- {match['detail']} (`{match['path']}`)")
	if len(matches) > 5:
		lines.append(f"- ... plus {len(matches) - 5} more matched path(s)")
	lines.extend([
		"",
		"This advisory is informational only and does not block merge.",
		"If the operator-visible behavior is already documented elsewhere, no action is required in this PR.",
	])
	if run_url:
		lines.extend(["", f"Run: {run_url}"])
	return "\n".join(lines) + "\n"


def main() -> int:
	result_path = Path(os.environ.get("AGENTS_MD_MATERIALITY_RESULT_FILE") or os.devnull)
	comment_path = Path(os.environ.get("AGENTS_MD_MATERIALITY_COMMENT_FILE") or os.devnull)
	repo_root = Path(os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
	changed_files_path = Path(os.environ.get("PR_CHANGED_FILES_FILE") or "")
	diff_path_env = os.environ.get("PR_DIFF_FILE") or ""
	diff_path = Path(diff_path_env) if diff_path_env else None
	base_branch = str(os.environ.get("BASE_BRANCH") or "").strip()
	repository = str(os.environ.get("REPOSITORY") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
	pr_number = str(os.environ.get("PR_NUMBER") or "").strip()
	run_id = str(os.environ.get("GITHUB_RUN_ID") or "").strip()
	server_url = str(os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
	enabled = normalize_bool(os.environ.get("AGENTS_MD_MATERIALITY_ENABLED"))
	llm_fallback_requested = normalize_bool(os.environ.get("AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED"))

	run_url = ""
	if repository and run_id:
		run_url = f"{server_url}/{repository}/actions/runs/{run_id}"

	result: dict[str, object] = {
		"version": 1,
		"classifier_mode": "deterministic-path-glob-v1",
		"comment_marker": COMMENT_MARKER,
		"advisory_heading": ADVISORY_HEADING,
		"enabled": enabled,
		"materiality": "low",
		"advisory_required": False,
		"agents_md_changed": False,
		"repo_agents_md_present": False,
		"matched_rules": [],
		"changed_paths": [],
		"reason": "disabled",
		"llm_fallback_requested": llm_fallback_requested,
		"llm_fallback_used": False,
		"llm_fallback_model": str(os.environ.get("AGENTS_MD_MATERIALITY_MODEL") or "openai/gpt-5.6-luna"),
		"llm_fallback_reasoning": str(os.environ.get("AGENTS_MD_MATERIALITY_REASONING") or "medium"),
	}
	comment_body = ""

	if not enabled:
		write_outputs(result_path=result_path, comment_path=comment_path, result=result, comment_body=comment_body)
		print("AGENTS_MD_MATERIALITY: disabled advisory=false materiality=low")
		return 0

	result["repo_agents_md_present"] = (repo_root / "agents.md").is_file()
	if not bool(result["repo_agents_md_present"]):
		result["reason"] = "repo_agents_md_missing"
		write_outputs(result_path=result_path, comment_path=comment_path, result=result, comment_body=comment_body)
		print("AGENTS_MD_MATERIALITY: repo agents.md missing advisory=false materiality=low")
		return 0

	changed_paths = load_changed_paths(changed_files_path=changed_files_path, diff_path=diff_path)
	result["changed_paths"] = changed_paths
	if not changed_paths:
		result["reason"] = "no_changed_paths"
		write_outputs(result_path=result_path, comment_path=comment_path, result=result, comment_body=comment_body)
		print("AGENTS_MD_MATERIALITY: no changed paths advisory=false materiality=low")
		return 0

	result["agents_md_changed"] = "agents.md" in changed_paths

	matched_rules: list[dict[str, str]] = []
	seen_matches: set[tuple[str, str, str]] = set()
	for path in changed_paths:
		match = classify_path(path)
		if not match:
			continue
		match_key = (match["severity"], match["rule"], match["path"])
		if match_key in seen_matches:
			continue
		seen_matches.add(match_key)
		matched_rules.append(match)
	for top_level in detect_new_top_level_dirs(repo_root, base_branch):
		match = {
			"severity": "high",
			"rule": "new-top-level-directory",
			"path": f"{top_level}/",
			"detail": "new top-level directory added",
		}
		match_key = (match["severity"], match["rule"], match["path"])
		if match_key in seen_matches:
			continue
		seen_matches.add(match_key)
		matched_rules.append(match)
	result["matched_rules"] = matched_rules

	materiality = "low"
	if any(match["severity"] == "high" for match in matched_rules):
		materiality = "high"
	elif any(match["severity"] == "medium" for match in matched_rules):
		materiality = "medium"
	result["materiality"] = materiality

	if llm_fallback_requested:
		print("AGENTS_MD_MATERIALITY: LLM fallback flag is reserved in deterministic v1; continuing without a model call.")

	advisory_required = materiality in {"high", "medium"} and not bool(result["agents_md_changed"])
	result["advisory_required"] = advisory_required
	if advisory_required:
		result["reason"] = f"{materiality}_materiality_without_agents_md_update"
		comment_body = build_comment_body(materiality=materiality, matches=matched_rules, run_url=run_url)
	elif bool(result["agents_md_changed"]):
		result["reason"] = "agents_md_changed"
	else:
		result["reason"] = "low_materiality"

	write_outputs(result_path=result_path, comment_path=comment_path, result=result, comment_body=comment_body)
	print(
		"AGENTS_MD_MATERIALITY: "
		f"materiality={materiality} advisory={'true' if advisory_required else 'false'} "
		f"agents_md_changed={'true' if result['agents_md_changed'] else 'false'} matched={len(matched_rules)}"
	)
	if advisory_required and pr_number:
		print(f"AGENTS_MD_MATERIALITY: advisory prepared for PR #{pr_number}")
	return 0


try:
	raise SystemExit(main())
except SystemExit:
	raise
except Exception as exc:  # pragma: no cover - fail-open guard
	result_path = Path(os.environ.get("AGENTS_MD_MATERIALITY_RESULT_FILE") or os.devnull)
	comment_path = Path(os.environ.get("AGENTS_MD_MATERIALITY_COMMENT_FILE") or os.devnull)
	print(f"::warning::review_agents_md_materiality.sh failed open: {exc.__class__.__name__}: {exc}", file=sys.stderr)
	traceback.print_exc(file=sys.stderr)
	fallback_result = {
		"version": 1,
		"classifier_mode": "deterministic-path-glob-v1",
		"comment_marker": COMMENT_MARKER,
		"advisory_heading": ADVISORY_HEADING,
		"enabled": normalize_bool(os.environ.get("AGENTS_MD_MATERIALITY_ENABLED")),
		"materiality": "low",
		"advisory_required": False,
		"agents_md_changed": False,
		"repo_agents_md_present": False,
		"matched_rules": [],
		"changed_paths": [],
		"reason": f"internal_error:{exc.__class__.__name__}",
		"llm_fallback_requested": normalize_bool(os.environ.get("AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED")),
		"llm_fallback_used": False,
		"llm_fallback_model": str(os.environ.get("AGENTS_MD_MATERIALITY_MODEL") or "openai/gpt-5.6-luna"),
		"llm_fallback_reasoning": str(os.environ.get("AGENTS_MD_MATERIALITY_REASONING") or "medium"),
	}
	write_outputs(result_path=result_path, comment_path=comment_path, result=fallback_result, comment_body="")
	print("AGENTS_MD_MATERIALITY: internal failure advisory=false materiality=low")
	raise SystemExit(0)
PY
