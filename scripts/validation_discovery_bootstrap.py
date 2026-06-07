#!/usr/bin/env python3
"""Codex-driven .ai/validate.yml discovery for consumer repositories.

This module is invoked by `scripts/validation_refresh_runner.py` once per
consumer repo per refresh cycle. It runs the existing
`prompts/mode-validate-discover.txt` prompt against the consumer's
checked-out tree via the OpenAI Codex CLI (`codex exec`), validates the
output against `scripts/templates/slot_manifest.schema.json`, and reports
a typed `DiscoveryResult` so the caller can decide whether to seed a new
`.ai/validate.yml` (missing-manifest path) or open a "discovery disagrees"
PR (existing-manifest path with a `type` mismatch).

The module is intentionally self-contained: it does not import the shell
runtime fallback (`validate_process.sh`) which performs the same task
inside each consumer's own CI. The shell path remains the last-resort
fallback when a consumer has no committed `.ai/validate.yml` and the
daily refresh runner has not yet opened a discovery PR.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


SUPPORTED_FAMILIES = (
	"python-mongo-flask",
	"python-mongo-repo-checks",
	"python-repo-checks",
	"node-hardhat-solidity",
	"node-runtime",
)

# Mirrors the inline validator in `scripts/validate_process.sh` so the
# refresh-runner discovery path applies the same surface checks as the
# in-consumer runtime fallback. Adding a key here MUST stay in sync with
# the `expected_key` regex in `validate_process.sh`; the schema file is
# the deeper contract enforced separately via jsonschema.
_EXPECTED_TOP_LEVEL_KEY_RE = re.compile(
	r"^(type|entry|port|health_check|services|env_overrides|custom_tests|skip_tests|slots):\s*",
	re.IGNORECASE,
)
_FENCED_BLOCK_RE = re.compile(r"```(?:yaml|yml)?\n(.*?)```", flags=re.DOTALL | re.IGNORECASE)


def _wrapped_command_failure(exc: BaseException) -> bool:
	return (
		exc.__class__.__name__ == "CommandFailure"
		and hasattr(exc, "returncode")
		and hasattr(exc, "stderr")
	)


def _command_exception_returncode(exc: BaseException) -> int | None:
	value = getattr(exc, "returncode", None)
	return value if isinstance(value, int) else None


def _command_exception_stdout(exc: BaseException) -> str:
	stdout = getattr(exc, "stdout", None)
	if isinstance(stdout, str) and stdout:
		return stdout
	output = getattr(exc, "output", None)
	if isinstance(output, str):
		return output
	return ""


def _command_exception_stderr(exc: BaseException) -> str:
	stderr = getattr(exc, "stderr", None)
	return stderr if isinstance(stderr, str) else ""


def _command_exception_text(exc: BaseException) -> str:
	parts: list[str] = []
	for part in (_command_exception_stderr(exc), _command_exception_stdout(exc)):
		if part and part not in parts:
			parts.append(part)
	if not parts:
		parts.append(str(exc))
	return "\n".join(parts)


def _known_executor_failure(exc: BaseException) -> bool:
	return isinstance(
		exc,
		(subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError),
	) or _wrapped_command_failure(exc)


def _load_yaml_module() -> tuple[Any | None, str | None]:
	try:
		import yaml
	except ImportError as exc:
		return None, f"yaml_missing: {exc}"
	return yaml, None


@dataclass(frozen=True)
class DiscoveryResult:
	"""Outcome of running codex discovery against a consumer clone."""

	outcome: str  # one of: "success" | "failed"
	manifest_yaml: str | None = None
	parsed_manifest: dict[str, Any] | None = None
	attempts_used: int = 0
	failure_reason: str | None = None


@dataclass(frozen=True)
class DisagreementResult:
	"""Pairwise comparison of committed vs discovered manifests."""

	disagrees: bool
	committed_type: str | None
	discovered_type: str | None
	# Q7:A — only `type` field mismatch triggers a "discovery-disagrees" PR;
	# other granular differences (slots, optional keys) are intentionally
	# ignored to keep PR noise low.


@dataclass(frozen=True)
class DiscoveryPrResult:
	"""Outcome of opening or reusing a discovery PR on the consumer repo."""

	pr_url: str | None
	pr_branch: str | None
	pr_was_reused: bool
	failure_reason: str | None  # populated when push/PR creation failed


class CommandExecutor:
	"""Thin subprocess wrapper used by tests to inject canned responses.

	Mirrors the shape of `validation_refresh_runner.CommandExecutor` so the
	refresh runner can pass through its own executor when wired in.
	"""

	def run(
		self,
		command: list[str],
		*,
		cwd: Path | None = None,
		check: bool = True,
		env_overrides: dict[str, str] | None = None,
		input_text: str | None = None,
		timeout: int = 600,
	) -> subprocess.CompletedProcess[str]:
		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		if env_overrides:
			env.update(env_overrides)
		proc = subprocess.run(
			command,
			cwd=str(cwd) if cwd is not None else None,
			text=True,
			capture_output=True,
			check=False,
			env=env,
			input=input_text,
			timeout=timeout,
		)
		if check and proc.returncode != 0:
			raise subprocess.CalledProcessError(
				returncode=proc.returncode,
				cmd=command,
				output=proc.stdout,
				stderr=proc.stderr,
			)
		return proc


def _extract_yaml_candidate(raw_text: str) -> str:
	"""Strip fences/whitespace from codex stdout; matches validate_process.sh."""
	text = (raw_text or "").replace("\r", "")
	candidate = text.strip()
	if "```" in text:
		match = _FENCED_BLOCK_RE.search(text)
		if match:
			candidate = match.group(1).strip()
	return candidate


def validate_discovered_manifest_yaml(
	yaml_text: str,
	*,
	schema_path: Path,
) -> tuple[bool, dict[str, Any] | None, str | None]:
	"""Validate a discovered YAML manifest end-to-end.

	Returns (ok, parsed_dict, failure_reason). On success, parsed_dict is
	the loaded manifest and failure_reason is None. The schema check uses
	jsonschema against `scripts/templates/slot_manifest.schema.json`.
	"""

	if not yaml_text or not yaml_text.strip():
		return False, None, "empty_output"

	if len(yaml_text) > 12000:
		return False, None, "output_too_large"

	stripped = yaml_text.lstrip()
	if stripped.startswith(("{", "[")):
		return False, None, "looks_like_json_not_yaml"

	non_blank_non_comment_lines = [
		line.lstrip()
		for line in yaml_text.splitlines()
		if line.strip() and not line.lstrip().startswith("#")
	]
	if not non_blank_non_comment_lines:
		return False, None, "no_yaml_content"

	first_line = non_blank_non_comment_lines[0].lower()
	if first_line.startswith(("error:", "fatal:", "traceback", "exception")):
		return False, None, "looks_like_error_output"

	if not any(_EXPECTED_TOP_LEVEL_KEY_RE.match(line) for line in non_blank_non_comment_lines):
		return False, None, "no_expected_top_level_key"

	yaml, yaml_error = _load_yaml_module()
	if yaml is None:
		return False, None, yaml_error

	try:
		parsed = yaml.safe_load(yaml_text)
	except yaml.YAMLError as exc:
		return False, None, f"yaml_parse_error: {exc}"

	if not isinstance(parsed, dict):
		return False, None, "yaml_root_not_mapping"

	manifest_type = parsed.get("type")
	if not isinstance(manifest_type, str) or manifest_type not in SUPPORTED_FAMILIES:
		return False, None, f"unsupported_type: {manifest_type!r}"

	try:
		import jsonschema  # imported lazily so the module loads in minimal envs
	except ImportError as exc:
		return False, None, f"jsonschema_missing: {exc}"

	try:
		schema = json.loads(schema_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		return False, None, f"schema_unreadable: {exc}"

	try:
		jsonschema.validate(parsed, schema)
	except jsonschema.ValidationError as exc:
		return False, None, f"schema_violation: {exc.message}"

	return True, parsed, None


def discover_manifest_via_codex(
	*,
	clone_dir: Path,
	prompt_path: Path,
	schema_path: Path,
	model: str,
	reasoning_effort: str,
	attempts: int,
	executor: CommandExecutor | None = None,
	per_call_timeout_secs: int | None = None,
	retry_backoff_base_secs: float = 5.0,
	sleep_fn: Callable[[float], None] = time.sleep,
) -> DiscoveryResult:
	"""Invoke `codex exec` against a consumer clone and validate output.

	Retries up to `attempts` times on validator rejection, with exponential
	backoff between attempts (mirrors validate_process.sh). Returns the
	first valid YAML or a failure with the last attempt's reason.

	`per_call_timeout_secs` caps the wall-clock of each individual `codex
	exec` invocation. When None (default) the executor's own default timeout
	applies — preserving the standalone behaviour. The refresh runner passes
	an explicit value so its aggregate discovery budget can reason about a
	single repo's worst-case cost (`attempts * per_call_timeout_secs`).
	"""

	if attempts < 1:
		raise ValueError("attempts must be >= 1")

	if not prompt_path.is_file():
		return DiscoveryResult(
			outcome="failed",
			attempts_used=0,
			failure_reason=f"prompt_missing: {prompt_path}",
		)

	executor = executor or CommandExecutor()
	prompt_text = prompt_path.read_text(encoding="utf-8")

	last_failure: str | None = None
	for attempt in range(1, attempts + 1):
		command = [
			"codex",
			"--ask-for-approval",
			"never",
			"-c",
			"model_verbosity=low",
			"-c",
			f"model_reasoning_effort={reasoning_effort}",
			"-c",
			"include_apply_patch_tool=true",
			"exec",
			"--skip-git-repo-check",
			"--model",
			model,
			"--sandbox",
			"danger-full-access",
		]
		try:
			run_kwargs: dict[str, Any] = {
				"cwd": clone_dir,
				"check": False,
				"input_text": prompt_text,
				"env_overrides": {"CODEX_DISABLE_TELEMETRY": "1"},
			}
			# Cap each codex attempt when the caller supplies a per-call
			# budget; when None, defer to the executor's own default timeout.
			if per_call_timeout_secs is not None:
				run_kwargs["timeout"] = per_call_timeout_secs
			proc = executor.run(command, **run_kwargs)
		except Exception as exc:
			if isinstance(exc, subprocess.TimeoutExpired) or (
				_wrapped_command_failure(exc) and _command_exception_returncode(exc) == 124
			):
				last_failure = "codex_timeout"
				proc = None
			elif isinstance(exc, FileNotFoundError) or (
				_wrapped_command_failure(exc) and _command_exception_returncode(exc) == 127
			):
				# Codex CLI not installed in this environment — surface clearly
				# so the workflow log explains the missing prerequisite without
				# tripping the validator-rejection retry path.
				return DiscoveryResult(
					outcome="failed",
					attempts_used=attempt,
					failure_reason="codex_cli_missing",
				)
			elif isinstance(exc, subprocess.CalledProcessError) or _wrapped_command_failure(exc):
				returncode = _command_exception_returncode(exc)
				if returncode is None:
					raise
				last_failure = f"codex_rc_nonzero(rc={returncode})"
				proc = None
			else:
				raise
		else:
			if proc.returncode != 0:
				last_failure = f"codex_rc_nonzero(rc={proc.returncode})"
			else:
				candidate = _extract_yaml_candidate(proc.stdout or "")
				ok, parsed, reason = validate_discovered_manifest_yaml(
					candidate, schema_path=schema_path
				)
				if ok:
					return DiscoveryResult(
						outcome="success",
						manifest_yaml=_stable_normalize_manifest_yaml(candidate),
						parsed_manifest=parsed,
						attempts_used=attempt,
						failure_reason=None,
					)
				last_failure = f"validator_rejected: {reason}"

		if attempt < attempts:
			sleep_fn(retry_backoff_base_secs * (2 ** (attempt - 1)))

	return DiscoveryResult(
		outcome="failed",
		attempts_used=attempts,
		failure_reason=last_failure or "unknown",
	)


def _stable_normalize_manifest_yaml(candidate: str) -> str:
	"""Trim trailing whitespace + ensure single trailing newline.

	We do NOT re-emit via PyYAML — that would reorder keys and lose the
	natural ordering codex emitted, hurting reviewer diff readability.
	"""

	cleaned = candidate.strip()
	return cleaned + "\n"


def classify_disagreement(
	committed_yaml_text: str | None,
	discovered_yaml_text: str,
) -> DisagreementResult:
	"""Return whether discovered manifest disagrees with committed manifest by `type`.

	Q7:A — only the top-level `type` field is considered. Optional-key
	differences (slots, custom_tests, etc.) do NOT trigger a PR.
	"""

	yaml, _yaml_error = _load_yaml_module()
	if yaml is None:
		return DisagreementResult(
			disagrees=False,
			committed_type=None,
			discovered_type=None,
		)

	discovered_type: str | None = None
	try:
		discovered_parsed = yaml.safe_load(discovered_yaml_text)
		if isinstance(discovered_parsed, dict):
			discovered_type_value = discovered_parsed.get("type")
			if isinstance(discovered_type_value, str):
				discovered_type = discovered_type_value
	except yaml.YAMLError:
		discovered_type = None

	committed_type: str | None = None
	if committed_yaml_text:
		try:
			committed_parsed = yaml.safe_load(committed_yaml_text)
			if isinstance(committed_parsed, dict):
				committed_type_value = committed_parsed.get("type")
				if isinstance(committed_type_value, str):
					committed_type = committed_type_value
		except yaml.YAMLError:
			committed_type = None

	if committed_type is None or discovered_type is None:
		# Treat unparseable / missing committed manifest as agree to avoid
		# spurious PRs when the committed YAML is malformed but present.
		# The missing-manifest path is handled by the caller before this
		# function is reached.
		return DisagreementResult(
			disagrees=False,
			committed_type=committed_type,
			discovered_type=discovered_type,
		)

	return DisagreementResult(
		disagrees=committed_type != discovered_type,
		committed_type=committed_type,
		discovered_type=discovered_type,
	)


_PR_BRANCH_LEAF_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_PR_BRANCH_PREFIX_SLUG_RE = re.compile(r"[^A-Za-z0-9._/-]+")


def _slugify_for_branch_leaf(value: str) -> str:
	slug = _PR_BRANCH_LEAF_SLUG_RE.sub("-", value or "").strip("-")
	return slug or "discovery"


def _slugify_for_branch_prefix(value: str) -> str:
	slug = _PR_BRANCH_PREFIX_SLUG_RE.sub("-", value or "").strip("-/")
	return slug or "discovery"


def build_pr_branch_name(*, prefix: str, consumer_head_sha: str, discovered_type: str) -> str:
	"""Deterministic branch name keyed by head SHA + discovered type.

	Pinning the SHA into the branch name lets the idempotency check (Q8:A)
	cleanly skip when the committed HEAD has not moved since the last
	open PR — a new SHA implies a fresh discovery PR is in scope.
	"""

	short_sha = (consumer_head_sha or "")[:12].lower() or "unknown-sha"
	type_slug = _slugify_for_branch_leaf(discovered_type)
	prefix_slug = _slugify_for_branch_prefix(prefix)
	return f"{prefix_slug}/{short_sha}/{type_slug}"


def _find_existing_open_pr_url(
	*,
	consumer_slug: str,
	base_branch: str,
	pr_branch: str,
	executor: CommandExecutor,
	fail_open: bool = False,
) -> tuple[str | None, str | None]:
	try:
		existing = executor.run(
			[
				"gh",
				"pr",
				"list",
				"--repo",
				consumer_slug,
				"--base",
				base_branch,
				"--head",
				pr_branch,
				"--state",
				"open",
				"--json",
				"url,number",
			],
			check=True,
		)
	except Exception as exc:
		if not _known_executor_failure(exc):
			raise
		if fail_open:
			return None, None
		returncode = _command_exception_returncode(exc)
		error_text = (_command_exception_text(exc).strip() or str(exc))[:300]
		rc_text = "unknown" if returncode is None else str(returncode)
		return None, f"gh_pr_list_failed(rc={rc_text}): {error_text}"

	try:
		existing_payload = json.loads((existing.stdout or "").strip() or "[]")
	except json.JSONDecodeError:
		existing_payload = []

	if isinstance(existing_payload, list):
		for item in existing_payload:
			if not isinstance(item, dict):
				continue
			url = item.get("url")
			if isinstance(url, str) and url:
				return url, None
	return None, None


def open_or_update_discovery_pr(
	*,
	consumer_slug: str,
	consumer_clone_dir: Path,
	base_branch: str,
	pr_branch: str,
	manifest_yaml: str,
	entry_script_text: str | None,
	entry_script_relative_path: str,
	pr_title: str,
	rationale_md: str,
	label: str | None,
	executor: CommandExecutor | None = None,
) -> DiscoveryPrResult:
	"""Create or reuse a discovery PR for one consumer repo (Q2:A + Q8:A).

	Behaviour:
	- If an open PR already exists for `pr_branch` against `base_branch`,
		return reused=True without touching the branch (Q8:A).
	- Otherwise, push a new branch with the manifest (+ optional entry
		script) to the consumer repo and open a PR.
	- On `git push` failure (likely scope), return `failure_reason` set
		so the caller can record `outcome=push_denied` (Q9:A).
	"""

	executor = executor or CommandExecutor()

	# Q8:A — list any existing open PRs on this branch first; skip if found.
	existing_pr_url, lookup_error = _find_existing_open_pr_url(
		consumer_slug=consumer_slug,
		base_branch=base_branch,
		pr_branch=pr_branch,
		executor=executor,
	)
	if lookup_error:
		return DiscoveryPrResult(
			pr_url=None,
			pr_branch=pr_branch,
			pr_was_reused=False,
			failure_reason=lookup_error,
		)
	if existing_pr_url:
		return DiscoveryPrResult(
			pr_url=existing_pr_url,
			pr_branch=pr_branch,
			pr_was_reused=True,
			failure_reason=None,
		)

	# Stage the manifest + entry script into the consumer clone on a new branch.
	try:
		executor.run(
			["git", "checkout", "-B", pr_branch, f"origin/{base_branch}"],
			cwd=consumer_clone_dir,
			check=True,
		)
		clone_root = consumer_clone_dir.resolve()
		manifest_path = consumer_clone_dir / ".ai" / "validate.yml"
		manifest_path.parent.mkdir(parents=True, exist_ok=True)
		manifest_path.write_text(manifest_yaml, encoding="utf-8")

		if entry_script_text:
			entry_path = (consumer_clone_dir / entry_script_relative_path).resolve()
			if not entry_path.is_relative_to(clone_root):
				return DiscoveryPrResult(
					pr_url=None,
					pr_branch=pr_branch,
					pr_was_reused=False,
					failure_reason="git_stage_failed: invalid_entry_script_path",
				)
			entry_path.parent.mkdir(parents=True, exist_ok=True)
			if not entry_path.exists():
				entry_path.write_text(entry_script_text, encoding="utf-8")
				entry_path.chmod(entry_path.stat().st_mode | 0o111)

		executor.run(
			["git", "config", "user.name", "codex-bot"],
			cwd=consumer_clone_dir,
			check=True,
		)
		executor.run(
			["git", "config", "user.email", "codex@users.noreply.github.com"],
			cwd=consumer_clone_dir,
			check=True,
		)
		paths_to_add = [".ai/validate.yml"]
		if entry_script_text:
			paths_to_add.append(entry_script_relative_path)
		executor.run(
			["git", "add", *paths_to_add],
			cwd=consumer_clone_dir,
			check=True,
		)
		executor.run(
			["git", "commit", "-m", pr_title],
			cwd=consumer_clone_dir,
			check=True,
		)
	except Exception as exc:
		if not _known_executor_failure(exc):
			raise
		return DiscoveryPrResult(
			pr_url=None,
			pr_branch=pr_branch,
			pr_was_reused=False,
			failure_reason=f"git_stage_failed: {exc}",
		)

	# Push the branch — Q9:A treats push failure as a clean per-consumer skip.
	try:
		executor.run(
			["git", "push", "-u", "origin", pr_branch],
			cwd=consumer_clone_dir,
			check=True,
		)
	except Exception as exc:
		if not _known_executor_failure(exc):
			raise
		existing_pr_url, _ = _find_existing_open_pr_url(
			consumer_slug=consumer_slug,
			base_branch=base_branch,
			pr_branch=pr_branch,
			executor=executor,
			fail_open=True,
		)
		if existing_pr_url:
			return DiscoveryPrResult(
				pr_url=existing_pr_url,
				pr_branch=pr_branch,
				pr_was_reused=True,
				failure_reason=None,
			)
		return DiscoveryPrResult(
			pr_url=None,
			pr_branch=pr_branch,
			pr_was_reused=False,
			failure_reason=f"push_denied: {_command_exception_stderr(exc).strip()[:300]}",
		)

	# Create the PR. The label is optional — if creation succeeds but
	# label application fails (label doesn't exist on the consumer),
	# downgrade to a warning and keep the PR.
	pr_create_command = [
		"gh",
		"pr",
		"create",
		"--repo",
		consumer_slug,
		"--base",
		base_branch,
		"--head",
		pr_branch,
		"--title",
		pr_title,
		"--body",
		rationale_md,
	]
	if label:
		pr_create_command.extend(["--label", label])

	try:
		create_proc = executor.run(pr_create_command, check=True)
	except Exception as exc:
		if not _known_executor_failure(exc):
			raise
		# Common case: label doesn't exist on the consumer repo. Retry
		# without the --label argument before giving up.
		error_text = _command_exception_text(exc)
		if _is_missing_pr_label_error(error_text=error_text, label=label):
			fallback_command = pr_create_command[:-2]
			try:
				create_proc = executor.run(fallback_command, check=True)
			except Exception as inner_exc:
				if not _known_executor_failure(inner_exc):
					raise
				existing_pr_url, _ = _find_existing_open_pr_url(
					consumer_slug=consumer_slug,
					base_branch=base_branch,
					pr_branch=pr_branch,
					executor=executor,
					fail_open=True,
				)
				if existing_pr_url:
					return DiscoveryPrResult(
						pr_url=existing_pr_url,
						pr_branch=pr_branch,
						pr_was_reused=True,
						failure_reason=None,
					)
				return DiscoveryPrResult(
					pr_url=None,
					pr_branch=pr_branch,
					pr_was_reused=False,
					failure_reason=f"gh_pr_create_failed: {inner_exc}",
				)
		else:
			existing_pr_url, _ = _find_existing_open_pr_url(
				consumer_slug=consumer_slug,
				base_branch=base_branch,
				pr_branch=pr_branch,
				executor=executor,
				fail_open=True,
			)
			if existing_pr_url:
				return DiscoveryPrResult(
					pr_url=existing_pr_url,
					pr_branch=pr_branch,
					pr_was_reused=True,
					failure_reason=None,
				)
			return DiscoveryPrResult(
				pr_url=None,
				pr_branch=pr_branch,
				pr_was_reused=False,
				failure_reason=f"gh_pr_create_failed: {exc}",
			)

	pr_url = (create_proc.stdout or "").strip()
	if not pr_url.startswith("https://"):
		# `gh pr create` prints URL on stdout; if it didn't, treat as failure.
		return DiscoveryPrResult(
			pr_url=None,
			pr_branch=pr_branch,
			pr_was_reused=False,
			failure_reason="gh_pr_create_no_url",
		)

	return DiscoveryPrResult(
		pr_url=pr_url,
		pr_branch=pr_branch,
		pr_was_reused=False,
		failure_reason=None,
	)


def _is_missing_pr_label_error(*, error_text: str, label: str | None) -> bool:
	if not label:
		return False
	lowered = (error_text or "").lower()
	label_lower = label.lower()
	return label_lower in lowered and (
		"could not add label" in lowered
		or "could not resolve to a label" in lowered
		or re.search(rf"label ['\"]?{re.escape(label_lower)}['\"]?.*not found", lowered) is not None
	)


def build_pr_rationale_markdown(
	*,
	consumer_slug: str,
	committed_type: str | None,
	discovered_type: str,
	consumer_head_sha: str,
	codex_model: str,
	reasoning_effort: str,
	attempts_used: int,
	is_seed: bool,
) -> str:
	"""Compose a PR body that explains why discovery proposed this manifest.

	The body intentionally does NOT use `Fixes #N` / `Closes #N` /
	`Resolves #N` (§19) — discovery PRs are seeded/updated by automation
	and must not auto-close any GitHub issue when merged.
	"""

	head_label = "(missing)" if not committed_type else f"`{committed_type}`"
	header = (
		"## Validation manifest seeded by codex discovery"
		if is_seed
		else "## Validation manifest disagreement (type mismatch)"
	)
	body_lines = [
		header,
		"",
		"Automated discovery from the workflow-templates daily refresh runner.",
		"This PR proposes a `.ai/validate.yml` for the consumer repo based on a",
		f"codex inspection of `{consumer_slug}` at commit `{consumer_head_sha[:12]}`.",
		"",
		"### Detection",
		"",
		f"- Committed `type`: {head_label}",
		f"- Discovered `type`: `{discovered_type}`",
		f"- Codex model: `{codex_model}` (reasoning effort: `{reasoning_effort}`, attempts used: {attempts_used})",
		"",
		"### Reviewer checklist",
		"",
		"- Confirm the proposed `type` matches the actual runtime / test surface of this repo.",
		"- Adjust `slots`, `custom_tests`, `skip_tests`, or `env_overrides` as needed before merging.",
		"- If discovery picked the wrong family, edit the manifest and merge — do not delete the PR.",
		"  The 7-day dedup window will keep the runner from proposing the same thing again.",
		"",
		"_Refs: workflow-templates daily validation refresh runner. This PR will be",
		"superseded automatically if discovery later reveals a different `type` at a",
		"newer head SHA._",
	]
	return "\n".join(body_lines)


@dataclass
class DiscoveryRunContext:
	"""Bundles per-cycle resolved configuration for a discovery dispatch."""

	source_root: Path
	prompt_path: Path
	schema_path: Path
	stub_entry_script_source: Path
	codex_model: str
	codex_reasoning_effort: str
	codex_attempts: int
	pr_branch_prefix: str
	pr_label: str | None
	dedup_days: int
	enabled: bool
	dry_run: bool
	# Aggregate wall-clock budget (seconds) for the codex discovery phase
	# across all consumer repos in one refresh cycle. The runner stops
	# invoking codex once the remaining budget can no longer cover a single
	# repo's worst case and degrades the rest to drift-monitoring only.
	# Default mirrors VALIDATION_DISCOVERY_BUDGET_SECS in the workflow.
	discovery_budget_secs: int = 2100
	supported_families: tuple[str, ...] = field(default_factory=lambda: SUPPORTED_FAMILIES)
