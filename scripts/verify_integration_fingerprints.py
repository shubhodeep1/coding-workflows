#!/usr/bin/env python3
"""Verify that an orchestrator integration-sync resolver run preserved
merged sub-issue intent.

Reads a JSON fingerprints file (the same shape stored in the
orchestrator state field `merged_issue_fingerprints`) and walks each
sub-issue's `must_contain` / `must_not_contain` regex lists plus the
``must_not_exist`` path list against either the post-resolve working
tree (default — i.e. the cwd this script is invoked from) or a git
ref's tree (``--ref <git_ref>`` mode — used by the wave-dispatch gate
in ``scripts/orchestrate_poll_process.sh`` to verify the integration
branch HEAD without checking it out).

Three constraint kinds per merged sub-issue:

  * ``must_contain``     — list of ``{file, regex}`` that MUST match.
  * ``must_not_contain`` — list of ``{file, regex}`` that MUST NOT match.
  * ``must_not_exist``   — list of ``{file}`` whose path MUST NOT
                           exist.  Captured for every file a merged
                           sub-PR removed outright (PR diff status
                           ``removed``), path-agnostic — the deletion
                           intent is enforced regardless of where in
                           the consumer repo's tree the file lives.

Five modes:

  1. Default (verify): exit 0 if all fingerprints are satisfied, 1 on
     violation, 2 on plumbing failure. Used by the conflict-resolver
     step post-codex, pre-commit, and by the wave-dispatch gate.

  2. --list-violated-files <fingerprints.json>: print one unique file
     path per line of every file that currently fails at least one
     fingerprint check (must_contain not matching, or must_not_contain
     matching, or must_contain referenced file missing, or
     must_not_exist path present). Always exits 0 even when violations
     exist; plumbing failures still exit 2. Used by
     `scripts/review_conflict_prepare.sh` to expand the resolver's
     working set so auto-merged files with silent sub-issue regressions
     are surfaced to Codex in addition to files git marked as unmerged.

     stdout contract in this mode: file paths ONLY, one per line.
     Any diagnostic output (::warning::, ::error::, debug prints)
     MUST go to stderr so the caller can pipe stdout directly into
     the expanded-working-set artefacts without post-filtering.
      GitHub Actions captures annotations from stderr too, so routing
      warnings to stderr does not suppress the operator-visible
      annotation in the run log.

  3. --baseline-fingerprints-state <out>: capture the current per-
     fingerprint satisfaction state and write it to <out>, fail-open
     on output errors, always exit 0.

  4. --compare-against-baseline <in>: compare the current verification
     result against a previously captured baseline and only hard-fail on
     resolver-introduced regressions. Pre-existing drift emits
     PRE_EXISTING_FINGERPRINT_DRIFT_V1 markers and passes.

  5. --export-resolver-safe-fingerprints <state.json>: validate full
     authenticated orchestrator state and print only bounded, literal,
     merged-issue fingerprints that are safe to use for resolver scope
     expansion. Authentication is verified by the caller before this mode.

Exit codes:
  0 — verify: all fingerprints satisfied (or none recorded — fail-open
      empty).  list-violated-files: always (prints zero or more paths).
  1 — verify: at least one fingerprint violation. Not used in
      list-violated-files mode.
  2 — Plumbing failure (file missing, JSON unparseable). Caller is
      expected to treat exit 2 as a soft warning, not a hard reject —
      the captured fingerprints might be stale or absent for legitimate
      reasons (e.g. sub-issue merged before fingerprinting was
      enabled).

Inputs:
  Positional argument — path to the fingerprints JSON file.
  Or env INTEGRATION_FINGERPRINTS_FILE — same path. Positional wins.
  Optional flag ``--ref <git_ref>`` — verify against the tree at the
  given git ref via ``git show`` / ``git cat-file -e`` instead of the
  working tree.  Or env INTEGRATION_VERIFY_REF — same.  Flag wins.
  Optional flag ``--verification-tier <strict|ratio|count_only|warn_only>`` —
  relax per-issue ``must_contain`` enforcement for post-resolve checks;
  default ``strict`` preserves the legacy behaviour byte-for-byte.
  Optional flag ``--baseline-fingerprints-state <out>`` — capture mode.
  Optional flag ``--compare-against-baseline <in>`` — compare mode.
  Optional flag ``--export-resolver-safe-fingerprints`` — resolver-safe
  export mode; the positional input is a full orchestrator state file.
  Optional env INTEGRATION_BRANCH_NAME — included in the summary line
  for operator log readability.

This script is referenced by `.github/workflows/review_autofix.yml` in
the conflict-resolver step (post-codex, pre-commit). The resolver
safety-script bootstrap now prefers the main snapshot for this file so
wedged integration branches pick up verifier fixes from `main`, while
still failing open when neither checked-out ref ships the script.

Going-forward only: see `capture_intent_fingerprints_for_merged_subissue`
in `scripts/orchestrate_poll_process.sh` for the capture half.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(_SCRIPT_DIR))

try:
	from ai_memory import _load_quarantine_list as _ai_memory_load_quarantine_list
	from ai_memory import _persist_quarantine_list as _ai_memory_persist_quarantine_list
except Exception:  # noqa: BLE001 — quarantine helpers must stay fail-open
	_ai_memory_load_quarantine_list = None
	_ai_memory_persist_quarantine_list = None


GIT_COMMAND_TIMEOUT_SECS = 30
VERIFICATION_TIERS = ("strict", "ratio", "count_only", "warn_only")
RATIO_TIER_MIN_PERCENT = 95
FINGERPRINT_QUARANTINE_SCHEMA_VERSION = "v1"
FINGERPRINT_QUARANTINE_RUNS_DEFAULT = 3
_QUARANTINE_MARKER_CACHE: dict[str, set[str]] = {}
RESOLVER_SAFE_MAX_ISSUES = 4096
RESOLVER_SAFE_MAX_PATTERNS_PER_KIND = 4096
RESOLVER_SAFE_MAX_STRING_LENGTH = 4096
RESOLVER_SAFE_REGEX_PATH_PREFIXES = (
	".github/",
	"scripts/",
	"prompts/",
	"ai-memory/",
	"tests/",
	"workflow-templates/",
	"docs/",
	"db/contracts/",
)
RESOLVER_SAFE_REGEX_ROOT_PATHS = {"agents.md", "README.md", "CLAUDE.md"}


def _normalize_ref(ref: str | None) -> str | None:
	if ref is None:
		return None
	ref = ref.strip()
	return ref or None


def _utc_now_iso() -> str:
	return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _current_head_sha(ref: str | None = None) -> str:
	ref = _normalize_ref(ref)
	target = ref or "HEAD"
	try:
		result = subprocess.run(
			["git", "rev-parse", target],
			capture_output=True,
			check=False,
			timeout=GIT_COMMAND_TIMEOUT_SECS,
		)
	except Exception:  # noqa: BLE001 — baseline capture must stay fail-open
		return ""
	if result.returncode != 0:
		return ""
	return result.stdout.decode("utf-8", errors="replace").strip()


def _normalize_repo_path(path: str) -> tuple[str | None, str | None]:
	if not isinstance(path, str) or not path:
		return None, "fingerprint path is empty."
	repo_root_abs = os.path.abspath(os.getcwd())
	repo_root_real = os.path.realpath(repo_root_abs)
	candidate_abs = os.path.abspath(path) if os.path.isabs(path) else os.path.abspath(os.path.join(repo_root_abs, path))
	candidate_real = os.path.realpath(candidate_abs)
	try:
		inside_repo = os.path.commonpath([repo_root_real, candidate_real]) == repo_root_real
	except ValueError:
		inside_repo = False
	if not inside_repo:
		return None, "fingerprint path resolves outside repository root."
	return os.path.relpath(candidate_abs, repo_root_abs), None


def _load_fingerprints(path: str) -> tuple[dict[str, Any] | None, str | None]:
	try:
		with open(path, "r", encoding="utf-8") as fh:
			data = json.load(fh)
	except FileNotFoundError:
		return None, f"fingerprints file not found: {path}"
	except json.JSONDecodeError as exc:
		return None, f"fingerprints JSON unparseable ({exc})"
	except Exception as exc:  # noqa: BLE001 — fail-open for any IO error
		return None, f"fingerprints file unreadable ({exc})"
	if not isinstance(data, dict):
		return None, "fingerprints file is not a JSON object"
	return data, None


def _resolver_safe_path(raw_path: Any, *, regex_pattern: bool) -> str | None:
	if not isinstance(raw_path, str) or not raw_path or len(raw_path) > RESOLVER_SAFE_MAX_STRING_LENGTH:
		return None
	if re.search(r"[\x00-\x1f\x7f]", raw_path) is not None or "\\" in raw_path or raw_path.startswith("/"):
		return None
	if posixpath.normpath(raw_path) != raw_path:
		return None
	path_parts = raw_path.split("/")
	if any(part in ("", ".", "..", ".git") for part in path_parts):
		return None
	if regex_pattern and not (
		raw_path in RESOLVER_SAFE_REGEX_ROOT_PATHS
		or any(raw_path.startswith(prefix) for prefix in RESOLVER_SAFE_REGEX_PATH_PREFIXES)
	):
		return None
	return raw_path


def _resolver_safe_pattern(raw_pattern: Any) -> str | None:
	if (
		not isinstance(raw_pattern, str)
		or not raw_pattern
		or len(raw_pattern) > RESOLVER_SAFE_MAX_STRING_LENGTH
		or not _looks_like_re_escape_output(raw_pattern)
	):
		return None
	try:
		re.compile(raw_pattern)
	except re.error:
		return None
	return raw_pattern


def export_resolver_safe_fingerprints(state_document: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
	if state_document.get("schema_version") != "orchestrate_state.v1":
		return None, "unsupported orchestrator state schema"
	waves = state_document.get("waves")
	fingerprint_entries = state_document.get("merged_issue_fingerprints", {})
	if not isinstance(waves, list) or not isinstance(fingerprint_entries, dict):
		return None, "orchestrator state has invalid waves or fingerprints"
	merged_issue_numbers: set[int] = set()
	for wave in waves:
		if not isinstance(wave, dict) or not isinstance(wave.get("issues", []), list):
			continue
		for wave_issue in wave.get("issues", []):
			if not isinstance(wave_issue, dict) or wave_issue.get("status") != "merged":
				continue
			issue_number = wave_issue.get("github_issue")
			if isinstance(issue_number, int) and not isinstance(issue_number, bool) and issue_number > 0:
				merged_issue_numbers.add(issue_number)
	if len(merged_issue_numbers) > RESOLVER_SAFE_MAX_ISSUES or len(fingerprint_entries) > RESOLVER_SAFE_MAX_ISSUES:
		return None, "orchestrator state exceeds the resolver-safe issue limit"

	sanitized_entries: dict[str, Any] = {}
	for raw_issue_key, raw_entry in fingerprint_entries.items():
		if (
			not isinstance(raw_issue_key, str)
			or len(raw_issue_key) > 19
			or re.fullmatch(r"[1-9][0-9]*", raw_issue_key) is None
		):
			print("::warning::resolver-safe fingerprint export omitted an entry with an invalid issue key.", file=sys.stderr)
			continue
		issue_number = int(raw_issue_key)
		if not isinstance(raw_entry, dict) or issue_number not in merged_issue_numbers:
			print(f"::warning::resolver-safe fingerprint export omitted issue #{issue_number}: issue is not a merged wave entry.", file=sys.stderr)
			continue
		entry_issue = raw_entry.get("issue")
		pr_number = raw_entry.get("pr")
		if (
			not isinstance(entry_issue, int)
			or isinstance(entry_issue, bool)
			or entry_issue != issue_number
			or not isinstance(pr_number, int)
			or isinstance(pr_number, bool)
			or pr_number < 1
			or pr_number > 9_223_372_036_854_775_807
		):
			print(f"::warning::resolver-safe fingerprint export omitted issue #{issue_number}: issue/PR provenance is invalid.", file=sys.stderr)
			continue
		sanitized_entry: dict[str, Any] = {
			"issue": issue_number,
			"pr": pr_number,
			"must_contain": [],
			"must_not_contain": [],
			"must_not_exist": [],
		}
		captured_at = raw_entry.get("captured_at")
		if isinstance(captured_at, str) and 0 < len(captured_at) <= 100:
			sanitized_entry["captured_at"] = captured_at
		entry_is_valid = True
		for kind in ("must_contain", "must_not_contain", "must_not_exist"):
			raw_patterns = raw_entry.get(kind, [])
			if not isinstance(raw_patterns, list) or len(raw_patterns) > RESOLVER_SAFE_MAX_PATTERNS_PER_KIND:
				entry_is_valid = False
				break
			for raw_fingerprint in raw_patterns:
				if not isinstance(raw_fingerprint, dict):
					continue
				safe_path = _resolver_safe_path(
					raw_fingerprint.get("file"),
					regex_pattern=kind != "must_not_exist",
				)
				if safe_path is None:
					continue
				if kind == "must_not_exist":
					sanitized_entry[kind].append({"file": safe_path})
					continue
				safe_pattern = _resolver_safe_pattern(raw_fingerprint.get("regex"))
				if safe_pattern is not None:
					sanitized_entry[kind].append({"file": safe_path, "regex": safe_pattern})
		if not entry_is_valid:
			print(f"::warning::resolver-safe fingerprint export omitted issue #{issue_number}: pattern list exceeds structural limits.", file=sys.stderr)
			continue
		sanitized_entries[raw_issue_key] = sanitized_entry
	return sanitized_entries, None


def _read_file_result(
	path: str,
	cache: dict[str, tuple[str | None, str | None]],
	ref: str | None = None,
) -> tuple[str | None, str | None]:
	ref = _normalize_ref(ref)
	normalized_path, path_error = _normalize_repo_path(path)
	cache_path = normalized_path or path
	cache_key = f"@ref:{ref}:{cache_path}" if ref else cache_path
	if cache_key in cache:
		return cache[cache_key]
	if path_error is not None:
		print(
			f"::warning::fingerprint verifier could not read {path}: {path_error}",
			flush=True,
			file=sys.stderr,
		)
		cache[cache_key] = (None, path_error)
		return cache[cache_key]
	assert normalized_path is not None
	if ref is None:
		try:
			with open(normalized_path, "r", encoding="utf-8", errors="replace") as fh:
				result: tuple[str | None, str | None] = (fh.read(), None)
		except FileNotFoundError:
			result = (None, None)
		except Exception as exc:  # noqa: BLE001 — verifier must not crash on bad file
			print(
				f"::warning::fingerprint verifier could not read {path}: {exc}",
				flush=True,
				file=sys.stderr,
			)
			result = (None, str(exc))
	else:
		try:
			git_result = subprocess.run(
				["git", "show", f"{ref}:{normalized_path}"],
				capture_output=True,
				check=False,
				timeout=GIT_COMMAND_TIMEOUT_SECS,
			)
			if git_result.returncode != 0:
				result = (None, None)
			else:
				result = (git_result.stdout.decode("utf-8", errors="replace"), None)
		except Exception as exc:  # noqa: BLE001 — keep ref-mode unknown-ref behavior fail-open
			print(
				f"::warning::fingerprint verifier could not read {ref}:{path}: {exc}",
				flush=True,
				file=sys.stderr,
			)
			result = (None, None)
	cache[cache_key] = result
	return result


def _read_file(path: str, cache: dict[str, tuple[str | None, str | None]], ref: str | None = None) -> str | None:
	content, _read_error = _read_file_result(path, cache, ref=ref)
	return content


def _path_exists_result(
	path: str,
	exists_cache: dict[str, tuple[bool, str | None]],
	ref: str | None = None,
) -> tuple[bool, str | None]:
	"""Return True iff ``path`` exists in the verification target.

	Working-tree mode: ``os.path.exists``.
	Ref mode: ``git cat-file -e <ref>:<path>`` — exit 0 ⇒ path exists at
	the ref's tree.  Any git failure (ref unknown, repo missing) maps
	to ``False`` so a plumbing failure never produces a spurious
	must_not_exist violation; the verify-mode caller treats absence as
	"contract satisfied" exactly as it would in working-tree mode.
	"""
	ref = _normalize_ref(ref)
	normalized_path, path_error = _normalize_repo_path(path)
	cache_path = normalized_path or path
	cache_key = f"@ref:{ref}:{cache_path}" if ref else cache_path
	if cache_key in exists_cache:
		return exists_cache[cache_key]
	if path_error is not None:
		print(
			f"::warning::fingerprint verifier could not read {path}: {path_error}",
			flush=True,
			file=sys.stderr,
		)
		exists_cache[cache_key] = (False, path_error)
		return exists_cache[cache_key]
	assert normalized_path is not None
	if ref is None:
		try:
			result = (os.path.exists(normalized_path), None)
		except Exception as exc:  # noqa: BLE001 — verifier must not crash on bad file
			print(
				f"::warning::fingerprint verifier could not read {path}: {exc}",
				flush=True,
				file=sys.stderr,
			)
			result = (False, str(exc))
	else:
		try:
			git_result = subprocess.run(
				["git", "cat-file", "-e", f"{ref}:{normalized_path}"],
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
				check=False,
				timeout=GIT_COMMAND_TIMEOUT_SECS,
			)
			result = (git_result.returncode == 0, None)
		except Exception as exc:  # noqa: BLE001 — keep ref-mode unknown-ref behavior fail-open
			print(
				f"::warning::fingerprint verifier could not read {ref}:{path}: {exc}",
				flush=True,
				file=sys.stderr,
			)
			result = (False, None)
	exists_cache[cache_key] = result
	return result


def _path_exists(path: str, exists_cache: dict[str, tuple[bool, str | None]], ref: str | None = None) -> bool:
	exists, _probe_error = _path_exists_result(path, exists_cache, ref=ref)
	return exists


# Cache for _find_pr_merge_commit_for_path keyed on (ref, pr_num, normalized_path).
# Populated lazily during verify; bounded by the number of distinct
# (issue, path) pairs in the fingerprint state, which is small.
_PR_MERGE_COMMIT_CACHE: dict[tuple[str, str, str], str | None] = {}

# Cache for file content at a specific ref/path used by the partial-removal
# false-positive defense. Distinct from _read_file_result's cache because
# the defense reads at the PR's merge commit (a different ref than the
# verifier's current ref) and we want to avoid re-shelling out git show
# for the same (merge_commit, path) pair across issues / patterns.
_PARTIAL_REMOVAL_POST_MERGE_CACHE: dict[tuple[str, str], str | None] = {}

# Subject prefix the orchestrator's integration-sync conflict resolver
# stamps on the commits it creates (back-merges of `main`, conflict
# resolutions). The wave-dispatch fingerprint gate exists solely to catch
# THESE commits silently reverting merged sub-issue intent, so a
# must_contain miss is only a genuine regression when a resolver commit is
# implicated — see _is_must_contain_post_capture_evolution_false_positive.
_AI_MERGE_RESOLVE_SUBJECT_PREFIX = "[ai-merge-resolve]"

# Cache for the must_contain post-capture-evolution false-positive defense,
# keyed on (ref, normalized_path, regex_src, captured_at). Value is the
# commit SHA that removed the captured line when the miss is attributable to
# legitimate post-capture evolution, or None when the defense does not apply
# (working-tree mode, non-re.escape regex, line removed by a resolver commit
# or before capture, no attributable commit, or any plumbing/parse error —
# the defense fails CLOSED so genuine resolver regressions still fail).
_POST_CAPTURE_EVOLUTION_CACHE: dict[tuple[str, str, str, str], str | None] = {}


def _find_pr_merge_commit_for_path(ref: str | None, pr_num: Any, path: str) -> str | None:
	"""Find the commit on ``ref`` (or HEAD) that merged PR #pr_num and touched ``path``.

	Used by the must_not_contain partial-removal false-positive defense
	to look up the file's post-merge state at the captured PR's merge
	commit. Walks the integration branch history oldest-first and picks
	the first commit whose *subject* ends with the GitHub squash-merge
	marker ``(#<pr_num>)``. This avoids selecting a later follow-up
	commit that merely mentions the PR number while touching the same
	path.

	Args:
		ref: git ref to walk; if None, falls back to ``HEAD``.
		pr_num: the PR number captured in the fingerprint entry.
		path: the file path the fingerprint pattern targets.

	Returns:
		The merge commit SHA on success; None when the PR can't be
		identified, the git plumbing fails, or the inputs are invalid.
		Callers MUST treat None as "defense check inconclusive" and
		fall through to the original violation.

	API call count: zero (uses ``git`` locally). Cached per
	(ref, pr_num, normalized_path) so repeated fingerprint patterns
	on the same file share a single ``git log`` invocation. Fail-open
	on any subprocess / encoding error.
	"""
	pr_str = str(pr_num).strip() if pr_num is not None else ""
	if not pr_str.isdigit():
		return None
	normalized_path, path_error = _normalize_repo_path(path)
	if path_error is not None or normalized_path is None:
		return None
	effective_ref = _normalize_ref(ref) or "HEAD"
	merge_suffix = f"(#{pr_str})"
	cache_key = (effective_ref, pr_str, normalized_path)
	if cache_key in _PR_MERGE_COMMIT_CACHE:
		return _PR_MERGE_COMMIT_CACHE[cache_key]
	try:
		git_result = subprocess.run(
			[
				"git", "log",
				"--reverse",
				"--format=%H%x00%s",
				f"--grep={merge_suffix}",
				effective_ref,
				"--",
				normalized_path,
			],
			capture_output=True,
			check=False,
			timeout=GIT_COMMAND_TIMEOUT_SECS,
		)
	except Exception:  # noqa: BLE001 — defense check must stay fail-open
		_PR_MERGE_COMMIT_CACHE[cache_key] = None
		return None
	if git_result.returncode != 0:
		_PR_MERGE_COMMIT_CACHE[cache_key] = None
		return None
	resolved = None
	for raw_line in git_result.stdout.decode("utf-8", errors="replace").splitlines():
		sha, _sep, subject = raw_line.partition("\x00")
		if not sha or not subject:
			continue
		if subject.rstrip().endswith(merge_suffix):
			resolved = sha.strip() or None
			break
	_PR_MERGE_COMMIT_CACHE[cache_key] = resolved
	return resolved


def _read_file_at_merge_commit(merge_commit: str, path: str) -> str | None:
	"""Read file content from a specific git commit.

	Used by the must_not_contain partial-removal false-positive defense
	to inspect the file's state at the captured PR's merge commit.
	Cached per (merge_commit, normalized_path) so repeated lookups
	don't re-shell out.

	Returns None on any failure (file absent at that commit, git error,
	encoding error). Callers MUST treat None as "defense check
	inconclusive".
	"""
	if not merge_commit:
		return None
	normalized_path, path_error = _normalize_repo_path(path)
	if path_error is not None or normalized_path is None:
		return None
	cache_key = (merge_commit, normalized_path)
	if cache_key in _PARTIAL_REMOVAL_POST_MERGE_CACHE:
		return _PARTIAL_REMOVAL_POST_MERGE_CACHE[cache_key]
	try:
		git_result = subprocess.run(
			["git", "show", f"{merge_commit}:{normalized_path}"],
			capture_output=True,
			check=False,
			timeout=GIT_COMMAND_TIMEOUT_SECS,
		)
	except Exception:  # noqa: BLE001 — defense check must stay fail-open
		_PARTIAL_REMOVAL_POST_MERGE_CACHE[cache_key] = None
		return None
	if git_result.returncode != 0:
		_PARTIAL_REMOVAL_POST_MERGE_CACHE[cache_key] = None
		return None
	try:
		content = git_result.stdout.decode("utf-8", errors="replace")
	except Exception:  # noqa: BLE001 — defense check must stay fail-open
		_PARTIAL_REMOVAL_POST_MERGE_CACHE[cache_key] = None
		return None
	_PARTIAL_REMOVAL_POST_MERGE_CACHE[cache_key] = content
	return content


def _is_must_not_contain_partial_removal_false_positive(
	ref: str | None,
	pr_num: Any,
	path: str,
	pattern: "re.Pattern[str]",
) -> str | None:
	"""Detect must_not_contain capture false positives (multi-occurrence partial removal).

	Capture emits a must_not_contain pattern for every PR-removed line
	that survives the existing net-change / substring-overlap filters
	in ``capture_intent_fingerprints_for_merged_subissue``. Neither
	filter catches the case where a line existed at N>1 distinct
	positions in the file pre-PR and the PR only removed K<N of them:
	the line still exists in the post-merge file, but capture records
	a must_not_contain anyway and verify later trips on the unchanged
	occurrences as a fake "reappeared" violation.

	This defense detects the case by re-reading the file at the
	captured PR's merge commit: if the pattern still matches there,
	then the PR did not fully delete the line and the captured
	must_not_contain entry is a false positive that should be skipped
	(with a warning marker so the bug is observable).

	Returns the merge commit SHA when the pattern is confirmed as a
	false positive (caller should skip the violation); None otherwise
	(caller should surface the original violation). Fail-open on any
	plumbing error so genuine regressions still fail the gate.
	"""
	merge_commit = _find_pr_merge_commit_for_path(ref, pr_num, path)
	if not merge_commit:
		return None
	post_merge_content = _read_file_at_merge_commit(merge_commit, path)
	if post_merge_content is None:
		return None
	try:
		matched = bool(pattern.search(post_merge_content))
	except Exception:  # noqa: BLE001 — defense check must stay fail-open
		return None
	return merge_commit if matched else None


def _captured_at_to_epoch(captured_at: Any) -> int | None:
	"""Parse an ISO-8601 ``captured_at`` (e.g. ``2026-06-02T13:38:28Z``)
	into a UTC unix timestamp.

	Returns None on any missing / blank / unparseable value so the
	post-capture-evolution defense fails closed (surfaces the violation)
	rather than guessing a window.
	"""
	if not isinstance(captured_at, str):
		return None
	text = captured_at.strip()
	if not text:
		return None
	# datetime.fromisoformat only learned to accept a trailing 'Z' in
	# Python 3.11; normalise it so the defense works on the 3.10 runners too.
	if text.endswith("Z"):
		text = text[:-1] + "+00:00"
	try:
		parsed = datetime.fromisoformat(text)
	except ValueError:
		return None
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=timezone.utc)
	try:
		return int(parsed.timestamp())
	except (OverflowError, OSError, ValueError):
		return None


def _is_must_contain_post_capture_evolution_false_positive(
	ref: str | None,
	captured_at: Any,
	path: str,
	regex_src: str,
) -> str | None:
	"""Detect must_contain misses caused by legitimate post-capture evolution.

	The wave-dispatch gate captures each merged sub-issue's added lines as
	exact (``re.escape``) ``must_contain`` regexes at merge time. A line that
	is legitimately modified *after* that capture — by a later sub-issue PR
	squash-merge, an ``[ai-autofix]`` commit, an out-of-band fix PR merged
	onto the integration branch, or any other non-resolver commit — no longer
	matches the stale exact regex, so the verifier reports a fake "intent
	silently reverted" violation that wrongly blocks the next wave's dispatch
	(observed on project #3042 wave 7: PR #3076 inserted
	``--skip-git-repo-check`` into issue #3044's codex-exec lines; an
	``[ai-autofix]`` commit refactored issue #3058's GH_PAT/GH_TOKEN block
	into a ``dispatch_token`` local; later waves extended issue #3066's
	``REQUIRED_BOOTSTRAP_SCRIPTS`` list).

	The gate's sole purpose is catching the integration-sync conflict
	resolver (commits subject-prefixed ``[ai-merge-resolve]``) silently
	reverting intent. Routine resolver back-merges of ``main`` legitimately
	touch many files without reverting any specific line, so a file-level
	"did a resolver commit touch this file after capture" check is too coarse
	— it would re-block every project that ever back-merged ``main``. Instead
	this attributes the captured line's *disappearance* to a single commit:
	walk the integration branch's first-parent history with a fixed-string
	pickaxe (``git log -S<line> --first-parent``) for the exact captured line.
	The newest hit is the commit that removed the line's last occurrence (the
	line is absent at HEAD, hence the miss). The miss is legitimate
	post-capture evolution — NOT a resolver regression — exactly when that
	commit is dated after ``captured_at`` AND is not an ``[ai-merge-resolve]``
	resolver commit.

	Returns that superseding non-resolver commit SHA when the miss is
	confirmed as post-capture evolution (caller should skip the violation);
	None otherwise. Fails CLOSED — working-tree mode (no ref to walk), a
	missing/unparseable ``captured_at``, a non-``re.escape`` ``regex_src``
	(cannot be safely literalized for a fixed-string pickaxe), the line
	removed by a resolver commit or at/before capture, no attributable
	commit, or any git plumbing/encoding error all return None so genuine
	resolver regressions still hard-fail the gate.

	API call count: zero (local ``git log`` only). Cached per
	(ref, normalized_path, regex_src, captured_at).
	"""
	effective_ref = _normalize_ref(ref)
	if effective_ref is None:
		# Working-tree mode (the conflict resolver's pre-commit self-check)
		# has no single committed ref to attribute changes to, and must stay
		# strict about preserving merged intent — the defense is scoped to
		# the ref-mode wave-dispatch gate only.
		return None
	captured_epoch = _captured_at_to_epoch(captured_at)
	if captured_epoch is None:
		return None
	normalized_path, path_error = _normalize_repo_path(path)
	if path_error is not None or normalized_path is None:
		return None
	if not isinstance(regex_src, str) or not regex_src:
		return None
	# Only pure re.escape captures (the orchestrator's only producer) can be
	# safely un-escaped to a literal for a fixed-string pickaxe; a real-regex
	# fingerprint is surfaced rather than risk an unsafe literalization that
	# could mask a genuine regression.
	if not _looks_like_re_escape_output(regex_src):
		return None
	literal = re.sub(r"\\(.)", r"\1", regex_src)
	if not literal:
		return None
	cache_key = (effective_ref, normalized_path, regex_src, str(captured_at).strip())
	if cache_key in _POST_CAPTURE_EVOLUTION_CACHE:
		return _POST_CAPTURE_EVOLUTION_CACHE[cache_key]
	try:
		git_result = subprocess.run(
			[
				"git", "log",
				"-S" + literal,
				"--first-parent",
				"--max-count=1",
				"--format=%H%x00%ct%x00%s",
				effective_ref,
				"--",
				normalized_path,
			],
			capture_output=True,
			check=False,
			timeout=GIT_COMMAND_TIMEOUT_SECS,
		)
	except Exception:  # noqa: BLE001 — defense must stay fail-closed
		_POST_CAPTURE_EVOLUTION_CACHE[cache_key] = None
		return None
	if git_result.returncode != 0:
		_POST_CAPTURE_EVOLUTION_CACHE[cache_key] = None
		return None
	result: str | None = None
	for raw_line in git_result.stdout.decode("utf-8", errors="replace").splitlines():
		sha, _sep1, rest = raw_line.partition("\x00")
		ct_text, _sep2, subject = rest.partition("\x00")
		if not sha or not ct_text:
			continue
		try:
			commit_epoch = int(ct_text)
		except ValueError:
			continue
		# Newest-first: the first well-formed pickaxe hit is the commit that
		# changed the captured line's count on the verified ref — removed it
		# for must_contain, re-added it for must_not_contain — and that commit
		# alone decides the verdict.
		if commit_epoch > captured_epoch and not subject.lstrip().startswith(
			_AI_MERGE_RESOLVE_SUBJECT_PREFIX
		):
			result = sha.strip() or None
		break
	_POST_CAPTURE_EVOLUTION_CACHE[cache_key] = result
	return result


# Direction-agnostic alias for the attribution primitive above. The helper
# inspects only the commit that changed the captured line's *count* on the
# verified ref — a removal for a must_contain miss, a re-addition for a
# must_not_contain reappearance — and never the constraint direction itself
# (``git log -S`` triggers on a count change in either direction). Both
# post-capture-evolution defenses share it so the subtle fail-closed +
# pickaxe + cache logic lives in exactly one place (and shares
# ``_POST_CAPTURE_EVOLUTION_CACHE``).
_post_capture_non_resolver_line_change_commit = (
	_is_must_contain_post_capture_evolution_false_positive
)


def _is_must_not_contain_post_capture_evolution_false_positive(
	ref: str | None,
	captured_at: Any,
	path: str,
	regex_src: str,
) -> str | None:
	"""Detect must_not_contain reappearances caused by legitimate post-capture evolution.

	Symmetric counterpart of
	:func:`_is_must_contain_post_capture_evolution_false_positive`. The
	wave-dispatch gate captures each merged sub-issue's *deleted* lines as
	exact (``re.escape``) ``must_not_contain`` regexes at merge time. A line
	the sub-issue genuinely removed can later be *re-introduced* on the
	integration branch by a non-resolver commit — most commonly a back-merge
	of the default branch whose conflict resolution kept the default branch's
	still-present copy (observed on project #3042 wave 8: the ``Merge main
	into orchestrator/project-3042`` back-merge ``822f8a6`` — authored by the
	judge, NOT an ``[ai-merge-resolve]`` resolver commit — re-added
	``local stall_secs=$(( STALL_THRESHOLD_MINUTES * 60 ))`` to
	``build_active_issue_set()`` after issue #3051 deleted it, while #3051's
	actual refactor and every other must_contain fingerprint stayed intact).

	That is the same class of expected drift the must_contain defense already
	tolerates: the gate's sole purpose is catching the integration-sync
	conflict resolver (``[ai-merge-resolve]`` commits) silently reverting
	intent, so a reappearance attributable to a non-resolver commit is NOT a
	resolver regression and must not perpetually block the next wave's
	dispatch. Without this symmetric defense the must_not_contain side hard-
	failed on exactly the drift the must_contain side waves through, wedging
	the whole project.

	Returns the re-introducing non-resolver commit SHA when the reappearance
	is confirmed as post-capture evolution (caller should skip the
	violation); None otherwise. Fails CLOSED on every guard the shared helper
	enforces: working-tree mode (so the conflict resolver's own pre-commit
	self-check stays strict and still cannot silently revert a deletion), a
	missing/unparseable ``captured_at``, a non-``re.escape`` ``regex_src``, a
	re-introduction by an ``[ai-merge-resolve]`` resolver commit (a genuine
	regression), the line never having changed after capture, or any git
	plumbing error.

	API call count: zero (local ``git log`` only); shares
	``_POST_CAPTURE_EVOLUTION_CACHE`` with the must_contain defense.
	"""
	return _post_capture_non_resolver_line_change_commit(
		ref, captured_at, path, regex_src,
	)


def _fp_key(fp: Any) -> tuple[str, str] | None:
	if not isinstance(fp, dict):
		return None
	path = fp.get("file", "")
	regex_src = fp.get("regex", "")
	if not path or not regex_src:
		return None
	return (path, regex_src)


def _baseline_fp_key(kind: str, fp: Any) -> tuple[str, ...] | None:
	if kind == "must_not_exist":
		if not isinstance(fp, dict):
			return None
		path = fp.get("file", "")
		if not path:
			return None
		return (path,)
	return _fp_key(fp)


def _format_fp_key(fp_key: tuple[str, ...]) -> str:
	return json.dumps(list(fp_key), separators=(",", ":"))


def _quarantine_default_payload() -> dict[str, Any]:
	return {
		"schema_version": FINGERPRINT_QUARANTINE_SCHEMA_VERSION,
		"entries": [],
	}


def _quarantine_run_id() -> str:
	raw_run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
	if not raw_run_id:
		return "run-local"
	sanitized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw_run_id).strip("._")
	return sanitized or "run-local"


def _quarantine_threshold() -> int:
	raw_threshold = os.environ.get("FINGERPRINT_QUARANTINE_RUNS_M", str(FINGERPRINT_QUARANTINE_RUNS_DEFAULT)).strip()
	try:
		threshold = int(raw_threshold)
	except ValueError:
		print(
			f"::warning::verify_integration_fingerprints: invalid FINGERPRINT_QUARANTINE_RUNS_M={raw_threshold!r}; using default {FINGERPRINT_QUARANTINE_RUNS_DEFAULT}.",
			flush=True,
			file=sys.stderr,
		)
		return FINGERPRINT_QUARANTINE_RUNS_DEFAULT
	if threshold < 1:
		print(
			f"::warning::verify_integration_fingerprints: FINGERPRINT_QUARANTINE_RUNS_M must be >= 1; using default {FINGERPRINT_QUARANTINE_RUNS_DEFAULT}.",
			flush=True,
			file=sys.stderr,
		)
		return FINGERPRINT_QUARANTINE_RUNS_DEFAULT
	return threshold


def _quarantine_marker_path(run_id: str) -> str:
	temp_root = os.environ.get("RUNNER_TEMP", "").strip() or tempfile.gettempdir()
	cwd_hash = hashlib.sha256(os.path.abspath(os.getcwd()).encode("utf-8")).hexdigest()[:16]
	return os.path.join(temp_root, f"fingerprint-quarantine-{cwd_hash}-{run_id}.markers")


def _quarantine_marker_key(issue_key: str, fp_key: tuple[str, ...]) -> str:
	return f"{issue_key}\t{_format_fp_key(fp_key)}"


def _quarantine_mark_emitted_once(run_id: str, issue_key: str, fp_key: tuple[str, ...]) -> bool:
	marker_path = _quarantine_marker_path(run_id)
	marker_key = _quarantine_marker_key(issue_key, fp_key)
	try:
		seen_keys = _QUARANTINE_MARKER_CACHE.get(marker_path)
		if seen_keys is None:
			seen_keys = set()
			if os.path.exists(marker_path):
				with open(marker_path, "r", encoding="utf-8") as fh:
					seen_keys = {line.rstrip("\n") for line in fh if line.strip()}
			_QUARANTINE_MARKER_CACHE[marker_path] = seen_keys
		if marker_key in seen_keys:
			return False
		parent = os.path.dirname(marker_path)
		if parent:
			os.makedirs(parent, exist_ok=True)
		with open(marker_path, "a", encoding="utf-8") as fh:
			fh.write(marker_key + "\n")
		seen_keys.add(marker_key)
		return True
	except Exception:  # noqa: BLE001 — duplicate markers are lower risk than hard-failing verification
		return True


def _quarantine_entry_key(issue_key: Any, fp_key: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
	return str(issue_key), tuple(fp_key)


def _quarantine_index(payload: dict[str, Any]) -> dict[tuple[str, tuple[str, ...]], dict[str, Any]]:
	index: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
	if not isinstance(payload, dict):
		return index
	for entry in payload.get("entries") or []:
		if not isinstance(entry, dict):
			continue
		issue_key = entry.get("issue_key")
		raw_fp_key = entry.get("fp_key")
		first_seen_run_id = entry.get("first_seen_run_id")
		last_seen_run_id = entry.get("last_seen_run_id")
		consecutive_runs = entry.get("consecutive_unchanged_runs")
		if not isinstance(issue_key, str) or not issue_key:
			continue
		if not isinstance(raw_fp_key, list) or not raw_fp_key or not all(isinstance(part, str) and part for part in raw_fp_key):
			continue
		if not isinstance(first_seen_run_id, str) or not first_seen_run_id:
			continue
		if not isinstance(last_seen_run_id, str) or not last_seen_run_id:
			continue
		if not isinstance(consecutive_runs, int) or consecutive_runs < 1:
			continue
		index[(issue_key, tuple(raw_fp_key))] = {
			"issue_key": issue_key,
			"fp_key": list(raw_fp_key),
			"first_seen_run_id": first_seen_run_id,
			"last_seen_run_id": last_seen_run_id,
			"consecutive_unchanged_runs": consecutive_runs,
		}
	return index


def _quarantine_entry_is_skipped(entry: dict[str, Any] | None, threshold: int, run_id: str) -> bool:
	if not entry:
		return False
	return int(entry.get("consecutive_unchanged_runs") or 0) >= threshold and str(entry.get("last_seen_run_id") or "") != run_id


def _quarantine_entry_is_suppressed(entry: dict[str, Any] | None, threshold: int, run_id: str) -> bool:
	if not entry:
		return False
	return int(entry.get("consecutive_unchanged_runs") or 0) >= threshold and str(entry.get("last_seen_run_id") or "") == run_id


def _load_quarantine_runtime() -> dict[str, Any]:
	runtime = {
		"payload": _quarantine_default_payload(),
		"index": {},
		"threshold": _quarantine_threshold(),
		"run_id": _quarantine_run_id(),
	}
	if _ai_memory_load_quarantine_list is None:
		return runtime
	try:
		result = _ai_memory_load_quarantine_list(repo_root=Path.cwd())
	except Exception:  # noqa: BLE001 — quarantine integration must stay fail-open
		return runtime
	if not isinstance(result, dict):
		return runtime
	if result.get("error"):
		return runtime
	if result.get("warning") and result.get("enabled", True):
		return runtime
	payload = result.get("quarantine")
	if isinstance(payload, dict):
		runtime["payload"] = payload
		runtime["index"] = _quarantine_index(payload)
	return runtime


def _emit_quarantine_marker(issue_key: Any, issue_num: Any, fp_key: tuple[str, ...], runtime: dict[str, Any]) -> None:
	issue_key_text = str(issue_key)
	if not _quarantine_mark_emitted_once(runtime["run_id"], issue_key_text, fp_key):
		return
	print(
		f"::warning::FINGERPRINT_QUARANTINED_V1 fp_key={_format_fp_key(fp_key)} issue=#{issue_num}",
		flush=True,
	)


def _persist_quarantine_runtime(
	runtime: dict[str, Any],
	*,
	observed_keys: set[tuple[str, tuple[str, ...]]],
	unchanged_keys: set[tuple[str, tuple[str, ...]]],
) -> None:
	if _ai_memory_persist_quarantine_list is None:
		return
	quarantine_index = {
		key: {
			"issue_key": value["issue_key"],
			"fp_key": list(value["fp_key"]),
			"first_seen_run_id": value["first_seen_run_id"],
			"last_seen_run_id": value["last_seen_run_id"],
			"consecutive_unchanged_runs": int(value["consecutive_unchanged_runs"]),
		}
		for key, value in runtime["index"].items()
	}
	changed = False
	run_id = str(runtime["run_id"])
	for key in observed_keys:
		if key in unchanged_keys:
			continue
		if key in quarantine_index:
			del quarantine_index[key]
			changed = True
	for key in unchanged_keys:
		entry = quarantine_index.get(key)
		if entry is None:
			issue_key, fp_key = key
			quarantine_index[key] = {
				"issue_key": issue_key,
				"fp_key": list(fp_key),
				"first_seen_run_id": run_id,
				"last_seen_run_id": run_id,
				"consecutive_unchanged_runs": 1,
			}
			changed = True
			continue
		if str(entry.get("last_seen_run_id") or "") == run_id:
			continue
		entry["last_seen_run_id"] = run_id
		entry["consecutive_unchanged_runs"] = int(entry.get("consecutive_unchanged_runs") or 0) + 1
		changed = True
	if not changed:
		return
	payload = {
		"schema_version": FINGERPRINT_QUARANTINE_SCHEMA_VERSION,
		"entries": [
			{
				"fp_key": list(key[1]),
				"issue_key": value["issue_key"],
				"first_seen_run_id": value["first_seen_run_id"],
				"last_seen_run_id": value["last_seen_run_id"],
				"consecutive_unchanged_runs": int(value["consecutive_unchanged_runs"]),
			}
			for key, value in sorted(quarantine_index.items(), key=lambda item: (item[0][0], item[0][1]))
		],
	}
	try:
		result = _ai_memory_persist_quarantine_list(payload=payload, repo_root=Path.cwd())
	except Exception:  # noqa: BLE001 — quarantine persistence must stay fail-open
		return
	if not isinstance(result, dict):
		return
	if result.get("error") or result.get("warning"):
		return


def _looks_like_re_escape_output(pattern: str) -> bool:
	"""Heuristic: does ``pattern`` look like the output of :func:`re.escape`?

	The drop semantics in :func:`_substring_overlap_drops` only hold when
	both patterns are literal-string matches (i.e. re.escape outputs from
	captured diff lines), because under that invariant substring
	containment in the regex source is equivalent to substring containment
	in the matched text.  If a pattern is a real regex (e.g. with
	unescaped metachars, character classes, alternation), the drop is
	unsafe — a forbidden pattern could legitimately differ in matching
	semantics from a longer ``must_contain`` regex.  Round-trip ``pattern``
	through ``re.escape`` of its un-escaped form: only return True when
	the round-trip is a fixed point, i.e. the pattern contains no
	unescaped regex metachars beyond what ``re.escape`` itself produces.
	"""
	if not isinstance(pattern, str):
		return False
	unescaped = re.sub(r"\\(.)", r"\1", pattern)
	return re.escape(unescaped) == pattern


def _substring_overlap_drops(
	mc_with_keys: list[tuple[Any, tuple[str, str] | None]],
	mnc_with_keys: list[tuple[Any, tuple[str, str] | None]],
) -> set[tuple[str, str]]:
	# Capture-side artefact: when a sub-issue extended an existing line
	# by appending text (e.g. removed `foo bar.` and added
	# `foo bar. When baz, accepted ...`), capture wraps both via
	# re.escape() and stores the shorter regex under must_not_contain
	# and the longer one under must_contain on the same file.  Under
	# re.search (used by both verify modes here), any post-resolve tree
	# that satisfies the longer must_contain trivially matches the
	# shorter must_not_contain too, so the constraint pair is
	# structurally unsatisfiable — the resolver burns its 3-attempt
	# retry budget and times out at the step wall-clock cap on a hunk
	# it cannot make pass.  Drop the must_not_contain side; the
	# must_contain side already enforces the stronger intent (the
	# longer added line being present).  Both regexes here come from
	# re.escape on diff lines, so substring containment in the regex
	# source is equivalent to substring containment in the literal
	# matched text.  The capture half (orchestrate_poll_process.sh
	# capture_intent_fingerprints_for_merged_subissue) applies the same
	# filter at write time so freshly captured state files no longer
	# admit the bad pair; this verifier-side check covers state files
	# written before the capture-side fix landed.
	#
	# Provenance guard: only fire when BOTH patterns look like re.escape
	# outputs (no real regex metachars).  If either pattern carries real
	# regex syntax (alternation, character classes, anchors, …), the
	# drop is unsafe — substring containment in the regex source no
	# longer implies substring containment in the matched text, so a
	# legitimate forbidden pattern could be silently removed and let a
	# regression slip through.  Captures from the orchestrator pipeline
	# always use re.escape, so this guard is a no-op for the intended
	# use case while protecting against hand-edited / future fingerprint
	# entries that contain raw regex.
	drops: set[tuple[str, str]] = set()
	for _, mc_key in mc_with_keys:
		if mc_key is None:
			continue
		mc_path, mc_regex = mc_key
		if not _looks_like_re_escape_output(mc_regex):
			continue
		for _, mnc_key in mnc_with_keys:
			if mnc_key is None:
				continue
			mnc_path, mnc_regex = mnc_key
			if (
				mnc_path == mc_path
				and mnc_regex != mc_regex
				and mnc_regex in mc_regex
				and _looks_like_re_escape_output(mnc_regex)
			):
				drops.add(mnc_key)
	return drops


def _cross_issue_exact_conflict_drops(
	fingerprints: dict[str, Any],
) -> tuple[dict[str, set[tuple[str, str]]], dict[str, list[str]]]:
	"""Drop older cross-issue exact contradictions when a newer capture wins.

	Historic orchestrator state can legitimately carry stale intent for the
	same literal ``(file, regex)`` pair across different merged sub-issues:
	an older issue captured the line under ``must_contain``, then a later
	issue intentionally deleted or rewrote that same line and captured the
	identical pair under ``must_not_contain`` (or vice versa).  Those state
	entries are not both satisfiable on a single post-resolve tree.

	When every conflicting occurrence has a ``captured_at`` timestamp and one
	side is strictly newer, prefer that newer side and drop the older opposite
	intent.  If timestamps are missing or the newest timestamp is tied across
	both sides, leave the contradiction intact so verify mode surfaces the
	ambiguity instead of silently guessing.
	"""
	entries_by_key: dict[tuple[str, str], list[tuple[str, str, str, str, str]]] = {}
	seen_occurrences: set[tuple[str, str, tuple[str, str]]] = set()
	for issue_key, entry in fingerprints.items():
		if not isinstance(entry, dict):
			continue
		issue_num = str(entry.get("issue", issue_key))
		pr_num = str(entry.get("pr", "?"))
		captured_at = entry.get("captured_at", "")
		if not isinstance(captured_at, str):
			captured_at = ""
		for kind in ("must_contain", "must_not_contain"):
			for fp in entry.get(kind, []) or []:
				key = _fp_key(fp)
				if key is None:
					continue
				occurrence_key = (issue_key, kind, key)
				if occurrence_key in seen_occurrences:
					continue
				seen_occurrences.add(occurrence_key)
				entries_by_key.setdefault(key, []).append((issue_key, issue_num, pr_num, kind, captured_at))

	drops_by_issue: dict[str, set[tuple[str, str]]] = {}
	warning_counts: dict[tuple[str, str, str, str, str, str, str, tuple[tuple[str, str], ...]], int] = {}
	for key, occurrences in entries_by_key.items():
		if len({issue_key for issue_key, *_ in occurrences}) < 2:
			continue
		if {kind for _, _, _, kind, _ in occurrences} != {"must_contain", "must_not_contain"}:
			continue
		if any(not captured_at for _, _, _, _, captured_at in occurrences):
			continue
		latest_captured_at = max(captured_at for _, _, _, _, captured_at in occurrences)
		latest_kinds = {kind for _, _, _, kind, captured_at in occurrences if captured_at == latest_captured_at}
		if len(latest_kinds) != 1:
			continue
		winning_kind = next(iter(latest_kinds))
		winning_refs = tuple(
			sorted(
				{
					(issue_num, pr_num)
					for _, issue_num, pr_num, kind, captured_at in occurrences
					if kind == winning_kind and captured_at == latest_captured_at
				}
			)
		)
		for issue_key, issue_num, pr_num, kind, captured_at in occurrences:
			if kind == winning_kind or captured_at >= latest_captured_at:
				continue
			drops_by_issue.setdefault(issue_key, set()).add(key)
			warning_key = (
				issue_key,
				issue_num,
				pr_num,
				kind,
				winning_kind,
				captured_at,
				latest_captured_at,
				winning_refs,
			)
			warning_counts[warning_key] = warning_counts.get(warning_key, 0) + 1

	warnings_by_issue: dict[str, list[str]] = {}
	for warning_key, count in sorted(warning_counts.items()):
		(
			issue_key,
			issue_num,
			pr_num,
			losing_kind,
			winning_kind,
			captured_at,
			latest_captured_at,
			winning_refs,
		) = warning_key
		winners_text = ", ".join(f"#{winner_issue} (PR #{winner_pr})" for winner_issue, winner_pr in winning_refs)
		warnings_by_issue.setdefault(issue_key, []).append(
			f"::warning::Fingerprint cross-issue exact-conflict dedup for issue #{issue_num} "
			f"(PR #{pr_num}, captured_at {captured_at}): skipping {count} {losing_kind} pattern(s) "
			f"whose identical (file, regex) pair now appears under {winning_kind} in newer "
			f"capture(s) from {winners_text} at {latest_captured_at}. Keeping the newer capture."
		)
	return drops_by_issue, warnings_by_issue


def _dedup_issue_patterns(
	issue_key: str,
	entry: dict[str, Any],
	cross_issue_exact_drops: dict[str, set[tuple[str, str]]] | None = None,
) -> tuple[list[Any], list[Any], set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, str]]]:
	must_contain = entry.get("must_contain", []) or []
	must_not_contain = entry.get("must_not_contain", []) or []
	mc_with_keys = [(fp, _fp_key(fp)) for fp in must_contain]
	mnc_with_keys = [(fp, _fp_key(fp)) for fp in must_not_contain]
	mc_keys = {k for _, k in mc_with_keys if k is not None}
	mnc_keys = {k for _, k in mnc_with_keys if k is not None}
	shared_keys = mc_keys & mnc_keys
	substring_drops = _substring_overlap_drops(mc_with_keys, mnc_with_keys)
	exact_conflict_drops = set((cross_issue_exact_drops or {}).get(issue_key, set()))
	drops = shared_keys | substring_drops | exact_conflict_drops
	if drops:
		must_contain = [fp for fp, key in mc_with_keys if key not in drops]
		must_not_contain = [fp for fp, key in mnc_with_keys if key not in drops]
	return must_contain, must_not_contain, shared_keys, substring_drops, exact_conflict_drops


def _evaluate_fp_state(
	fp: Any,
	kind: str,
	issue_num: Any,
	pr_num: Any,
	file_cache: dict[str, tuple[str | None, str | None]],
	exists_cache: dict[str, tuple[bool, str | None]],
	ref: str | None = None,
	captured_at: Any = None,
) -> dict[str, Any] | None:
	if not isinstance(fp, dict):
		return None
	path = fp.get("file", "")
	if not path:
		return None
	fp_key = _baseline_fp_key(kind, fp)
	if fp_key is None:
		return None
	regex_src = ""
	if kind == "must_not_exist":
		exists, probe_error = _path_exists_result(path, exists_cache, ref=ref)
		if probe_error is not None:
			return {
				"kind": kind,
				"path": path,
				"regex": regex_src,
				"fp_key": fp_key,
				"satisfied": False,
				"check_error": probe_error,
				"violation": (
					f"issue #{issue_num} (PR #{pr_num}): must_not_exist path '{path}' "
					f"could not be checked — {probe_error}"
				),
			}
		if exists:
			return {
				"kind": kind,
				"path": path,
				"regex": regex_src,
				"fp_key": fp_key,
				"satisfied": False,
				"check_error": None,
				"violation": (
					f"issue #{issue_num} (PR #{pr_num}): must_not_exist path '{path}' "
					"reappeared after resolver — sub-issue file deletion silently reverted."
				),
			}
		return {
			"kind": kind,
			"path": path,
			"regex": regex_src,
			"fp_key": fp_key,
			"satisfied": True,
			"check_error": None,
			"violation": None,
		}

	regex_src = fp.get("regex", "")
	if not regex_src:
		return None
	try:
		pattern = re.compile(regex_src)
	except re.error as exc:
		print(
			f"::warning::fingerprint regex compile failed for issue #{issue_num} ({exc}); skipping that pattern.",
			flush=True,
			file=sys.stderr,
		)
		return None

	content, read_error = _read_file_result(path, file_cache, ref=ref)
	if kind == "must_contain":
		if read_error is not None:
			return {
				"kind": kind,
				"path": path,
				"regex": regex_src,
				"fp_key": fp_key,
				"satisfied": False,
				"check_error": read_error,
				"violation": (
					f"issue #{issue_num} (PR #{pr_num}): must_contain pattern in '{path}' "
					f"could not be checked — {read_error}"
				),
			}
		if content is None:
			return {
				"kind": kind,
				"path": path,
				"regex": regex_src,
				"fp_key": fp_key,
				"satisfied": False,
				"check_error": None,
				"violation": (
					f"issue #{issue_num} (PR #{pr_num}): must_contain pattern in '{path}' "
					"could not be checked — file does not exist in post-resolve tree."
				),
			}
		if pattern.search(content):
			return {
				"kind": kind,
				"path": path,
				"regex": regex_src,
				"fp_key": fp_key,
				"satisfied": True,
				"check_error": None,
				"violation": None,
			}
		# must_contain miss: before flagging a silent regression, check
		# whether the fingerprinted line was legitimately modified after
		# capture by a non-resolver commit (a later sub-issue PR, an
		# [ai-autofix] commit, an out-of-band fix merged onto the
		# integration branch, …). The gate only guards against the
		# integration-sync resolver reverting intent, so post-capture
		# evolution by anything else is expected drift, not a regression.
		evolution_sha = _is_must_contain_post_capture_evolution_false_positive(
			ref, captured_at, path, regex_src,
		)
		if evolution_sha:
			print(
				f"::warning::FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1 "
				f"issue=#{issue_num} pr=#{pr_num} file={path} "
				f"superseding_commit={evolution_sha[:12]} "
				f"reason=must_contain_line_modified_after_capture_by_non_resolver_commit",
				flush=True,
				file=sys.stderr,
			)
			print(
				f"::warning::issue #{issue_num} (PR #{pr_num}): must_contain pattern missing from '{path}' "
				"classified as post-capture evolution false positive — the fingerprinted line was modified "
				f"after capture by non-resolver commit {evolution_sha[:12]} on the verified ref; the "
				"pickaxe-identified line-removal commit is not an [ai-merge-resolve] commit, so this "
				f"miss is not attributable to the resolver. Skipping violation. Pattern (first 200 chars): {regex_src[:200]}",
				flush=True,
				file=sys.stderr,
			)
			return {
				"kind": kind,
				"path": path,
				"regex": regex_src,
				"fp_key": fp_key,
				"satisfied": True,
				"check_error": None,
				"violation": None,
			}
		return {
			"kind": kind,
			"path": path,
			"regex": regex_src,
			"fp_key": fp_key,
			"satisfied": False,
			"check_error": None,
			"violation": (
				f"issue #{issue_num} (PR #{pr_num}): must_contain pattern missing from '{path}' "
				"after resolver — sub-issue intent silently reverted. "
				f"Pattern (first 200 chars): {regex_src[:200]}"
			),
		}

	if read_error is not None:
		return {
			"kind": kind,
			"path": path,
			"regex": regex_src,
			"fp_key": fp_key,
			"satisfied": False,
			"check_error": read_error,
			"violation": (
				f"issue #{issue_num} (PR #{pr_num}): must_not_contain pattern in '{path}' "
				f"could not be checked — {read_error}"
			),
		}
	if content is None:
		return {
			"kind": kind,
			"path": path,
			"regex": regex_src,
			"fp_key": fp_key,
			"satisfied": True,
			"check_error": None,
			"violation": None,
		}
	if pattern.search(content):
		false_positive_merge_commit = _is_must_not_contain_partial_removal_false_positive(
			ref, pr_num, path, pattern,
		)
		if false_positive_merge_commit:
			print(
				f"::warning::FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1 "
				f"issue=#{issue_num} pr=#{pr_num} file={path} "
				f"merge_commit={false_positive_merge_commit[:12]} "
				f"reason=must_not_contain_pattern_still_present_at_pr_merge_commit",
				flush=True,
				file=sys.stderr,
			)
			print(
				f"::warning::issue #{issue_num} (PR #{pr_num}): must_not_contain pattern in '{path}' "
				"classified as capture-side false positive (line was already present in the "
				"file at the captured PR's merge commit — capture should have dropped the "
				"pattern under the multi-occurrence partial-removal filter). "
				f"Skipping violation. Pattern (first 200 chars): {regex_src[:200]}",
				flush=True,
				file=sys.stderr,
			)
			return {
				"kind": kind,
				"path": path,
				"regex": regex_src,
				"fp_key": fp_key,
				"satisfied": True,
				"check_error": None,
				"violation": None,
			}
		# must_not_contain reappearance: before flagging a silent regression,
		# check whether the deleted line was legitimately RE-ADDED after
		# capture by a non-resolver commit (most commonly a back-merge of the
		# default branch whose conflict resolution kept the default branch's
		# still-present copy). Symmetric to the must_contain post-capture-
		# evolution defense above — the gate only guards against the
		# integration-sync resolver ([ai-merge-resolve]) reverting intent, so
		# a reappearance from anything else is expected drift, not a
		# regression. Fails closed in working-tree mode, so the resolver's own
		# pre-commit self-check still cannot silently revert a deletion.
		reintroduction_sha = _is_must_not_contain_post_capture_evolution_false_positive(
			ref, captured_at, path, regex_src,
		)
		if reintroduction_sha:
			print(
				f"::warning::FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1 "
				f"issue=#{issue_num} pr=#{pr_num} file={path} "
				f"superseding_commit={reintroduction_sha[:12]} "
				f"reason=must_not_contain_line_reintroduced_after_capture_by_non_resolver_commit",
				flush=True,
				file=sys.stderr,
			)
			print(
				f"::warning::issue #{issue_num} (PR #{pr_num}): must_not_contain pattern in '{path}' "
				"classified as post-capture reintroduction false positive — the deleted line was "
				f"re-added after capture by non-resolver commit {reintroduction_sha[:12]} on the verified "
				"ref (e.g. a back-merge of the default branch); the pickaxe-identified line-reintroduction "
				"commit is not an [ai-merge-resolve] commit, so this reappearance is not attributable to "
				f"the resolver. Skipping violation. Pattern (first 200 chars): {regex_src[:200]}",
				flush=True,
				file=sys.stderr,
			)
			return {
				"kind": kind,
				"path": path,
				"regex": regex_src,
				"fp_key": fp_key,
				"satisfied": True,
				"check_error": None,
				"violation": None,
			}
		return {
			"kind": kind,
			"path": path,
			"regex": regex_src,
			"fp_key": fp_key,
			"satisfied": False,
			"check_error": None,
			"violation": (
				f"issue #{issue_num} (PR #{pr_num}): must_not_contain pattern reappeared in '{path}' "
				"after resolver — sub-issue intentional deletion silently reverted. "
				f"Pattern (first 200 chars): {regex_src[:200]}"
			),
		}
	return {
		"kind": kind,
		"path": path,
		"regex": regex_src,
		"fp_key": fp_key,
		"satisfied": True,
		"check_error": None,
		"violation": None,
	}


def _emit_must_contain_ratio(branch: str, mc_total_satisfied: int, mc_total_expected: int) -> None:
	if mc_total_expected <= 0:
		return
	ratio_pct = (mc_total_satisfied * 100) // mc_total_expected
	print(
		f"Integration fingerprint verification (branch={branch}): "
		f"must_contain satisfied {mc_total_satisfied}/{mc_total_expected} "
		f"({ratio_pct}%)",
		flush=True,
	)
	if mc_total_satisfied < mc_total_expected:
		print(
			f"::warning::Silent-regression detector: post-resolve tree contains fewer "
			f"must_contain fingerprint matches ({mc_total_satisfied}) than were captured "
			f"({mc_total_expected}). Investigating violations below.",
			flush=True,
		)


def _emit_verify_failure(violations: list[str]) -> int:
	print(
		"::error::Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent:",
		flush=True,
	)
	for violation in violations:
		print(f"::error::  - {violation}", flush=True)
	print(
		"::error::Refusing to create [ai-merge-resolve] commit. The orchestrator integration judge "
		"will be invoked on the next poll tick if INTEGRATION_SYNC_CONFLICT_MAX_RETRIES has been reached.",
		flush=True,
	)
	return 1


def _deduped_issue_entries(fingerprints: dict[str, Any]) -> list[dict[str, Any]]:
	issue_entries: list[dict[str, Any]] = []
	cross_issue_exact_drops, cross_issue_exact_warnings = _cross_issue_exact_conflict_drops(fingerprints)
	for issue_key, entry in sorted(fingerprints.items()):
		if not isinstance(entry, dict):
			continue
		issue_num = entry.get("issue", issue_key)
		pr_num = entry.get("pr", "?")
		must_contain, must_not_contain, shared_keys, substring_drops, exact_conflict_drops = _dedup_issue_patterns(
			issue_key,
			entry,
			cross_issue_exact_drops,
		)
		issue_entries.append(
			{
				"issue_key": issue_key,
				"issue_num": issue_num,
				"pr_num": pr_num,
				"captured_at": entry.get("captured_at"),
				"must_contain": must_contain,
				"must_not_contain": must_not_contain,
				"must_not_exist": entry.get("must_not_exist", []) or [],
				"shared_keys": shared_keys,
				"substring_drops": substring_drops,
				"exact_conflict_drops": exact_conflict_drops,
				"exact_conflict_warnings": cross_issue_exact_warnings.get(issue_key, []),
			}
		)
	return issue_entries


def _emit_issue_dedup_warnings(issue_entry: dict[str, Any]) -> None:
	issue_num = issue_entry["issue_num"]
	pr_num = issue_entry["pr_num"]
	shared_keys = issue_entry["shared_keys"]
	substring_drops = issue_entry["substring_drops"]
	exact_conflict_drops = issue_entry["exact_conflict_drops"]
	if shared_keys:
		print(
			f"::warning::Fingerprint cross-dedup for issue #{issue_num} (PR #{pr_num}): "
			f"skipping {len(shared_keys)} self-contradictory pattern(s) present in both "
			f"must_contain and must_not_contain (capture-side refactor false positive).",
			flush=True,
		)
	if substring_drops:
		print(
			f"::warning::Fingerprint substring-overlap dedup for issue #{issue_num} (PR #{pr_num}): "
			f"skipping {len(substring_drops)} must_not_contain pattern(s) whose regex is a literal "
			f"substring of a must_contain regex on the same file (capture-side: deleted line subsumed "
			f"by added line under re.search; the longer must_contain already enforces the stronger intent).",
			flush=True,
		)
	if exact_conflict_drops:
		for warning in issue_entry["exact_conflict_warnings"]:
			print(warning, flush=True)


def _issue_satisfies_tier(tier: str, eligible: int, satisfied: int) -> bool:
	if tier == "ratio":
		return eligible == 0 or (satisfied * 100) >= (eligible * RATIO_TIER_MIN_PERCENT)
	if tier == "count_only":
		return eligible == 0 or satisfied >= 1
	if tier == "warn_only":
		return True
	return eligible == satisfied


def _issue_tier_violation_message(
	tier: str,
	issue_num: Any,
	pr_num: Any,
	satisfied: int,
	eligible: int,
	failing_states: list[dict[str, Any]],
) -> str:
	if tier == "ratio":
		message = (
			f"issue #{issue_num} (PR #{pr_num}): verification tier 'ratio' requires >="
			f"{RATIO_TIER_MIN_PERCENT}% of eligible must_contain patterns to match, but only "
			f"{satisfied}/{eligible} matched after resolver."
		)
	else:
		tier_name = tier or "count_only"
		message = (
			f"issue #{issue_num} (PR #{pr_num}): verification tier {tier_name!r} requires at least "
			f"1 eligible must_contain pattern to match, but only {satisfied}/{eligible} matched after resolver."
		)
	if failing_states:
		first_state = failing_states[0]
		message += f" First failing path: '{first_state['path']}'."
		message += f" First failing pattern (first 200 chars): {first_state['regex'][:200]}"
	return message


def _emit_tolerated_must_contain_warning(
	tier: str,
	issue_num: Any,
	pr_num: Any,
	satisfied: int,
	eligible: int,
	tolerated_states: list[dict[str, Any]],
) -> None:
	if not tolerated_states:
		return
	print(
		f"::warning::Integration fingerprint verification tier={tier} tolerated "
		f"{len(tolerated_states)} must_contain violation(s) for issue #{issue_num} (PR #{pr_num}); "
		f"satisfied {satisfied}/{eligible} eligible patterns.",
		flush=True,
	)
	for state in tolerated_states[:3]:
		print(f"::warning::  - {state['violation']}", flush=True)
	remaining = len(tolerated_states) - 3
	if remaining > 0:
		print(
			f"::warning::  - ... {remaining} additional must_contain violation(s) suppressed by tier={tier}.",
			flush=True,
		)


def _evaluate_issue_must_contain_tier(
	tier: str,
	issue_num: Any,
	pr_num: Any,
	states: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]], int, int]:
	eligible = len(states)
	satisfied = sum(1 for state in states if state["satisfied"])
	failing_states = [state for state in states if not state["satisfied"]]
	if tier == "warn_only":
		return [], failing_states, eligible, satisfied
	if _issue_satisfies_tier(tier, eligible, satisfied):
		return [], failing_states, eligible, satisfied
	return [
		_issue_tier_violation_message(tier, issue_num, pr_num, satisfied, eligible, failing_states)
	], [], eligible, satisfied


def _emit_warn_only_marker(branch: str, suppressed_violation_count: int) -> None:
	print(
		f"::warning::FINGERPRINT_TIER_WARN_ONLY_V1 branch={json.dumps(branch or '')} "
		f"suppressed_violation_count={suppressed_violation_count}",
		flush=True,
	)


def _emit_warn_only_success(branch: str, violations: list[str]) -> int:
	_emit_warn_only_marker(branch, len(violations))
	if violations:
		print(
			"::warning::Integration fingerprint verification warn_only tier suppressed fingerprint failures:",
			flush=True,
		)
		for violation in violations:
			print(f"::warning::  - {violation}", flush=True)
	print(
		"Integration fingerprint verification PASSED in warn_only tier — fingerprint violations emitted as warnings only.",
		flush=True,
	)
	return 0


def verify_with_tier(
	fingerprints: dict[str, Any],
	branch: str,
	tier: str,
	ref: str | None = None,
) -> int:
	if tier == "strict":
		return verify(fingerprints, branch, ref=ref)

	violations: list[str] = []
	warn_only_violations: list[str] = []
	suppressed_must_contain_count = 0
	mc_total_expected = 0
	mc_total_satisfied = 0
	file_cache: dict[str, tuple[str | None, str | None]] = {}
	exists_cache: dict[str, tuple[bool, str | None]] = {}

	for issue_entry in _deduped_issue_entries(fingerprints):
		_emit_issue_dedup_warnings(issue_entry)
		issue_num = issue_entry["issue_num"]
		pr_num = issue_entry["pr_num"]
		issue_must_contain_states: list[dict[str, Any]] = []

		for fp in issue_entry["must_contain"]:
			state = _evaluate_fp_state(
				fp, "must_contain", issue_num, pr_num, file_cache, exists_cache,
				ref=ref, captured_at=issue_entry.get("captured_at"),
			)
			if state is None:
				continue
			issue_must_contain_states.append(state)
			mc_total_expected += 1
			if state["satisfied"]:
				mc_total_satisfied += 1

		for fp in issue_entry["must_not_contain"]:
			state = _evaluate_fp_state(
				fp, "must_not_contain", issue_num, pr_num, file_cache, exists_cache,
				ref=ref, captured_at=issue_entry.get("captured_at"),
			)
			if state is None or state["satisfied"]:
				continue
			if tier == "warn_only":
				warn_only_violations.append(state["violation"])
			else:
				violations.append(state["violation"])

		for fp in issue_entry["must_not_exist"]:
			state = _evaluate_fp_state(fp, "must_not_exist", issue_num, pr_num, file_cache, exists_cache, ref=ref)
			if state is None or state["satisfied"]:
				continue
			if tier == "warn_only":
				warn_only_violations.append(state["violation"])
			else:
				violations.append(state["violation"])

		issue_violations, tolerated_states, issue_eligible, issue_satisfied = _evaluate_issue_must_contain_tier(
			tier,
			issue_num,
			pr_num,
			issue_must_contain_states,
		)
		if tier == "warn_only":
			warn_only_violations.extend(state["violation"] for state in tolerated_states)
		else:
			violations.extend(issue_violations)
			suppressed_must_contain_count += len(tolerated_states)
			_emit_tolerated_must_contain_warning(
				tier,
				issue_num,
				pr_num,
				issue_satisfied,
				issue_eligible,
				tolerated_states,
			)

	_emit_must_contain_ratio(branch, mc_total_satisfied, mc_total_expected)
	if tier == "warn_only":
		return _emit_warn_only_success(branch, warn_only_violations)
	if violations:
		return _emit_verify_failure(violations)
	if suppressed_must_contain_count > 0:
		print(
			f"Integration fingerprint verification PASSED under tier '{tier}' — "
			f"{suppressed_must_contain_count} must_contain violation(s) tolerated by tier policy.",
			flush=True,
		)
		return 0
	print(
		"Integration fingerprint verification PASSED — all merged sub-issue intent preserved.",
		flush=True,
	)
	return 0


def compare_against_baseline_with_tier(
	fingerprints: dict[str, Any],
	baseline_state: dict[str, Any],
	branch: str,
	tier: str,
	ref: str | None = None,
) -> int:
	if tier == "strict":
		return compare_against_baseline(fingerprints, baseline_state, branch, ref=ref)

	baseline_index = _baseline_satisfied_index(baseline_state)
	violations: list[str] = []
	warn_only_violations: list[str] = []
	pre_existing_drift_count = 0
	suppressed_must_contain_count = 0
	mc_compare_expected = 0
	mc_compare_satisfied = 0
	file_cache: dict[str, tuple[str | None, str | None]] = {}
	exists_cache: dict[str, tuple[bool, str | None]] = {}
	quarantine_runtime = _load_quarantine_runtime()
	quarantine_observed_keys: set[tuple[str, tuple[str, ...]]] = set()
	quarantine_unchanged_keys: set[tuple[str, tuple[str, ...]]] = set()

	for issue_entry in _deduped_issue_entries(fingerprints):
		_emit_issue_dedup_warnings(issue_entry)
		issue_key = issue_entry["issue_key"]
		issue_num = issue_entry["issue_num"]
		pr_num = issue_entry["pr_num"]
		issue_must_contain_states: list[dict[str, Any]] = []

		for kind, fps in (
			("must_contain", issue_entry["must_contain"]),
			("must_not_contain", issue_entry["must_not_contain"]),
			("must_not_exist", issue_entry["must_not_exist"]),
		):
			for fp in fps:
				fp_key = _baseline_fp_key(kind, fp)
				if fp_key is None:
					continue
				quarantine_key = _quarantine_entry_key(issue_key, fp_key)
				quarantine_entry = quarantine_runtime["index"].get(quarantine_key)
				if _quarantine_entry_is_skipped(
					quarantine_entry,
					int(quarantine_runtime["threshold"]),
					str(quarantine_runtime["run_id"]),
				):
					quarantine_observed_keys.add(quarantine_key)
					quarantine_unchanged_keys.add(quarantine_key)
					_emit_quarantine_marker(issue_key, issue_num, fp_key, quarantine_runtime)
					continue
				state = _evaluate_fp_state(fp, kind, issue_num, pr_num, file_cache, exists_cache, ref=ref)
				if state is None:
					continue
				baseline_satisfied = baseline_index.get((str(issue_key), kind, state["fp_key"]))
				if quarantine_entry is not None:
					quarantine_observed_keys.add(quarantine_key)
				if baseline_satisfied is False and _quarantine_entry_is_suppressed(
					quarantine_entry,
					int(quarantine_runtime["threshold"]),
					str(quarantine_runtime["run_id"]),
				):
					if state["check_error"] is not None or not state["satisfied"]:
						quarantine_unchanged_keys.add(quarantine_key)
					continue
				if baseline_satisfied is False:
					if state["satisfied"]:
						_emit_pre_existing_drift_marker(state, issue_num, fixed_by_resolver=True)
					else:
						quarantine_unchanged_keys.add(quarantine_key)
						_emit_pre_existing_drift_marker(state, issue_num, fixed_by_resolver=False)
						pre_existing_drift_count += 1
					continue

				if kind == "must_contain":
					issue_must_contain_states.append(state)
					mc_compare_expected += 1
					if state["satisfied"]:
						mc_compare_satisfied += 1
					continue

				if state["satisfied"]:
					continue
				if tier == "warn_only":
					warn_only_violations.append(state["violation"])
				else:
					violations.append(state["violation"])

		issue_violations, tolerated_states, issue_eligible, issue_satisfied = _evaluate_issue_must_contain_tier(
			tier,
			issue_num,
			pr_num,
			issue_must_contain_states,
		)
		if tier == "warn_only":
			warn_only_violations.extend(state["violation"] for state in tolerated_states)
		else:
			violations.extend(issue_violations)
			suppressed_must_contain_count += len(tolerated_states)
			_emit_tolerated_must_contain_warning(
				tier,
				issue_num,
				pr_num,
				issue_satisfied,
				issue_eligible,
				tolerated_states,
			)

	_emit_must_contain_ratio(branch, mc_compare_satisfied, mc_compare_expected)
	_persist_quarantine_runtime(
		quarantine_runtime,
		observed_keys=quarantine_observed_keys,
		unchanged_keys=quarantine_unchanged_keys,
	)
	if tier == "warn_only":
		return _emit_warn_only_success(branch, warn_only_violations)
	if violations:
		return _emit_verify_failure(violations)
	if pre_existing_drift_count > 0 and suppressed_must_contain_count > 0:
		print(
			f"Integration fingerprint verification PASSED under tier '{tier}' with pre-existing drift — "
			f"{suppressed_must_contain_count} must_contain violation(s) tolerated by tier policy "
			f"(pre_existing_drift_count={pre_existing_drift_count}; see PRE_EXISTING_FINGERPRINT_DRIFT_V1 markers above for triage).",
			flush=True,
		)
		return 0
	if pre_existing_drift_count > 0:
		print(
			"Integration fingerprint verification PASSED with pre-existing drift — resolver did not introduce any new regressions "
			f"(pre_existing_drift_count={pre_existing_drift_count}; see PRE_EXISTING_FINGERPRINT_DRIFT_V1 markers above for triage).",
			flush=True,
		)
		return 0
	if suppressed_must_contain_count > 0:
		print(
			f"Integration fingerprint verification PASSED under tier '{tier}' — "
			f"{suppressed_must_contain_count} must_contain violation(s) tolerated by tier policy.",
			flush=True,
		)
		return 0
	print(
		"Integration fingerprint verification PASSED — all merged sub-issue intent preserved.",
		flush=True,
	)
	return 0


def list_violated_files(fingerprints: dict[str, Any], ref: str | None = None) -> list[str]:
	"""Return a sorted, de-duplicated list of file paths that currently
	fail at least one fingerprint check against the cwd tree (or the
	tree at ``ref`` when supplied).

	Mirrors the matching logic in :func:`verify` but records only the
	offending file paths (for expanding the resolver's working set
	pre-codex) — does not emit ::error:: annotations and never fails
	hard on a violation.
	"""
	violated: set[str] = set()
	file_cache: dict[str, tuple[str | None, str | None]] = {}
	exists_cache: dict[str, tuple[bool, str | None]] = {}
	cross_issue_exact_drops, _ = _cross_issue_exact_conflict_drops(fingerprints)

	for issue_key, entry in fingerprints.items():
		if not isinstance(entry, dict):
			continue
		issue_num = entry.get("issue", issue_key)
		pr_num = entry.get("pr", "?")
		# Cross-dedup: stay silent in list-violated-files mode — the stdout
		# contract is "file paths ONLY" and the verify path already emits the
		# operator-visible warnings.
		must_contain, must_not_contain, _shared_keys, _substring_drops, _exact_conflict_drops = _dedup_issue_patterns(
			issue_key,
			entry,
			cross_issue_exact_drops,
		)
		must_not_exist = entry.get("must_not_exist", []) or []

		for fp in must_contain:
			state = _evaluate_fp_state(fp, "must_contain", issue_num=issue_num, pr_num=pr_num, file_cache=file_cache, exists_cache=exists_cache, ref=ref, captured_at=entry.get("captured_at"))
			if state is None:
				continue
			if state["check_error"] is not None:
				continue
			if not state["satisfied"]:
				violated.add(state["path"])

		for fp in must_not_contain:
			state = _evaluate_fp_state(fp, "must_not_contain", issue_num=issue_num, pr_num=pr_num, file_cache=file_cache, exists_cache=exists_cache, ref=ref, captured_at=entry.get("captured_at"))
			if state is None:
				continue
			if state["check_error"] is not None:
				continue
			if not state["satisfied"]:
				violated.add(state["path"])

		for fp in must_not_exist:
			state = _evaluate_fp_state(fp, "must_not_exist", issue_num=issue_num, pr_num=pr_num, file_cache=file_cache, exists_cache=exists_cache, ref=ref)
			if state is None:
				continue
			if state["check_error"] is not None:
				continue
			if not state["satisfied"]:
				# Path the sub-issue deleted is back on the
				# verification target — surface to the resolver's
				# working set so it can be re-removed.
				violated.add(state["path"])

	return sorted(violated)


def verify(fingerprints: dict[str, Any], branch: str, ref: str | None = None) -> int:
	violations: list[str] = []
	mc_total_expected = 0
	mc_total_satisfied = 0
	file_cache: dict[str, tuple[str | None, str | None]] = {}
	exists_cache: dict[str, tuple[bool, str | None]] = {}
	cross_issue_exact_drops, cross_issue_exact_warnings = _cross_issue_exact_conflict_drops(fingerprints)

	for issue_key, entry in sorted(fingerprints.items()):
		if not isinstance(entry, dict):
			continue
		issue_num = entry.get("issue", issue_key)
		pr_num = entry.get("pr", "?")

		# Defensive cross-dedup: the extractor in
		# capture_intent_fingerprints_for_merged_subissue
		# (scripts/orchestrate_poll_process.sh) filters net-no-op lines,
		# but that capture function is idempotent per issue — already-
		# stored bad entries in an orchestrator state file persist until
		# the state is rebuilt. A line a PR both removes and re-adds
		# (e.g. wrapping a bare call in an if/else fallback) cannot
		# simultaneously be required to appear AND required to be
		# absent; skip any (file, regex) pair recorded in both sets for
		# this issue so historic bad fingerprints don't produce
		# perpetual false positives.

		must_contain, must_not_contain, shared_keys, substring_drops, exact_conflict_drops = _dedup_issue_patterns(
			issue_key,
			entry,
			cross_issue_exact_drops,
		)
		must_not_exist = entry.get("must_not_exist", []) or []
		if shared_keys:
			print(
				f"::warning::Fingerprint cross-dedup for issue #{issue_num} (PR #{pr_num}): "
				f"skipping {len(shared_keys)} self-contradictory pattern(s) present in both "
				f"must_contain and must_not_contain (capture-side refactor false positive).",
				flush=True,
			)
		if substring_drops:
			print(
				f"::warning::Fingerprint substring-overlap dedup for issue #{issue_num} (PR #{pr_num}): "
				f"skipping {len(substring_drops)} must_not_contain pattern(s) whose regex is a literal "
				f"substring of a must_contain regex on the same file (capture-side: deleted line subsumed "
				f"by added line under re.search; the longer must_contain already enforces the stronger intent).",
				flush=True,
			)
		if exact_conflict_drops:
			for warning in cross_issue_exact_warnings.get(issue_key, []):
				print(warning, flush=True)

		for fp in must_contain:
			state = _evaluate_fp_state(
				fp, "must_contain", issue_num, pr_num, file_cache, exists_cache,
				ref=ref, captured_at=entry.get("captured_at"),
			)
			if state is None:
				continue
			mc_total_expected += 1
			if state["satisfied"]:
				mc_total_satisfied += 1
			else:
				violations.append(state["violation"])

		for fp in must_not_contain:
			state = _evaluate_fp_state(
				fp, "must_not_contain", issue_num, pr_num, file_cache, exists_cache,
				ref=ref, captured_at=entry.get("captured_at"),
			)
			if state is None:
				continue
			if not state["satisfied"]:
				violations.append(state["violation"])

		# must_not_exist: a sub-issue removed these files outright;
		# their reappearance in the post-resolve tree (or at the
		# verified ref) is a silent regression by an upstream merge
		# resolver.  Path-agnostic — the contract does NOT honour the
		# capture-side ALLOWED_PREFIXES allowlist (file existence is
		# cheap to check and the deletion intent is unambiguous
		# regardless of where the file lives in the consumer repo's
		# tree).
		for fp in must_not_exist:
			state = _evaluate_fp_state(fp, "must_not_exist", issue_num, pr_num, file_cache, exists_cache, ref=ref)
			if state is None:
				continue
			if not state["satisfied"]:
				violations.append(state["violation"])

	_emit_must_contain_ratio(branch, mc_total_satisfied, mc_total_expected)

	if violations:
		return _emit_verify_failure(violations)

	print(
		"Integration fingerprint verification PASSED — all merged sub-issue intent preserved.",
		flush=True,
	)
	return 0


def _baseline_entry_from_state(state: dict[str, Any]) -> dict[str, Any]:
	entry = {
		"fp_key": list(state["fp_key"]),
		"file": state["path"],
		"satisfied": state["satisfied"],
	}
	if state["regex"]:
		entry["regex"] = state["regex"]
	return entry


def capture_baseline_state(
	fingerprints: dict[str, Any],
	out_path: str,
	branch: str,
	ref: str | None = None,
) -> int:
	file_cache: dict[str, tuple[str | None, str | None]] = {}
	exists_cache: dict[str, tuple[bool, str | None]] = {}
	cross_issue_exact_drops, _cross_issue_exact_warnings = _cross_issue_exact_conflict_drops(fingerprints)
	quarantine_runtime = _load_quarantine_runtime()
	state_doc: dict[str, Any] = {
		"schema_version": 1,
		"captured_at": _utc_now_iso(),
		"branch": branch,
		"head_sha": _current_head_sha(ref=ref),
		"fingerprints": {},
	}
	for issue_key, entry in sorted(fingerprints.items()):
		if not isinstance(entry, dict):
			continue
		issue_num = entry.get("issue", issue_key)
		pr_num = entry.get("pr", "?")
		must_contain, must_not_contain, _shared_keys, _substring_drops, _exact_conflict_drops = _dedup_issue_patterns(
			issue_key,
			entry,
			cross_issue_exact_drops,
		)
		must_not_exist = entry.get("must_not_exist", []) or []
		issue_state = {
			"issue": issue_num,
			"pr": pr_num,
			"must_contain": [],
			"must_not_contain": [],
			"must_not_exist": [],
		}
		for kind, fps in (
			("must_contain", must_contain),
			("must_not_contain", must_not_contain),
			("must_not_exist", must_not_exist),
		):
			for fp in fps:
				fp_key = _baseline_fp_key(kind, fp)
				if fp_key is None:
					continue
				quarantine_entry = quarantine_runtime["index"].get(_quarantine_entry_key(issue_key, fp_key))
				if _quarantine_entry_is_skipped(
					quarantine_entry,
					int(quarantine_runtime["threshold"]),
					str(quarantine_runtime["run_id"]),
				):
					_emit_quarantine_marker(issue_key, issue_num, fp_key, quarantine_runtime)
					continue
				state = _evaluate_fp_state(fp, kind, issue_num, pr_num, file_cache, exists_cache, ref=ref)
				if state is None:
					continue
				issue_state[kind].append(_baseline_entry_from_state(state))
		state_doc["fingerprints"][issue_key] = issue_state
	try:
		parent = os.path.dirname(out_path)
		if parent:
			os.makedirs(parent, exist_ok=True)
		with open(out_path, "w", encoding="utf-8") as fh:
			json.dump(state_doc, fh)
		os.chmod(out_path, 0o644)
	except Exception as exc:  # noqa: BLE001 — capture mode must stay fail-open
		print(f"::warning::baseline capture failed: {exc}", flush=True)
	return 0


def _load_baseline_state(path: str) -> tuple[dict[str, Any] | None, str | None]:
	try:
		with open(path, "r", encoding="utf-8") as fh:
			data = json.load(fh)
	except FileNotFoundError:
		return None, f"baseline file not found: {path}"
	except json.JSONDecodeError as exc:
		return None, f"baseline JSON unparseable ({exc})"
	except Exception as exc:  # noqa: BLE001 — compare mode falls back to absolute verify
		return None, f"baseline file unreadable ({exc})"
	if not isinstance(data, dict):
		return None, "baseline file is not a JSON object"
	if data.get("schema_version") != 1:
		return None, f"baseline schema_version unsupported ({data.get('schema_version')!r})"
	if not isinstance(data.get("fingerprints"), dict):
		return None, "baseline fingerprints is not a JSON object"
	return data, None


def _baseline_satisfied_index(baseline_state: dict[str, Any]) -> dict[tuple[str, str, tuple[str, ...]], bool]:
	baseline_index: dict[tuple[str, str, tuple[str, ...]], bool] = {}
	for issue_key, entry in baseline_state.get("fingerprints", {}).items():
		if not isinstance(entry, dict):
			continue
		for kind in ("must_contain", "must_not_contain", "must_not_exist"):
			for fp in entry.get(kind, []) or []:
				if not isinstance(fp, dict):
					continue
				raw_key = fp.get("fp_key")
				if not isinstance(raw_key, list) or not raw_key or not all(isinstance(part, str) for part in raw_key):
					continue
				baseline_index[(str(issue_key), kind, tuple(raw_key))] = bool(fp.get("satisfied"))
	return baseline_index


def _emit_pre_existing_drift_marker(state: dict[str, Any], issue_num: Any, fixed_by_resolver: bool) -> None:
	path_json = json.dumps(state["path"])
	message = (
		f"{'::notice::' if fixed_by_resolver else '::warning::'}PRE_EXISTING_FINGERPRINT_DRIFT_V1 "
		f"{'fixed_by_resolver' if fixed_by_resolver else 'unchanged'} "
		f"fp_key={_format_fp_key(state['fp_key'])} issue=#{issue_num} path={path_json}"
	)
	if state["regex"] and not fixed_by_resolver:
		message += f" pattern={json.dumps(state['regex'])}"
	if not fixed_by_resolver:
		message += f" kind={state['kind']}"
	print(message, flush=True)


def compare_against_baseline(
	fingerprints: dict[str, Any],
	baseline_state: dict[str, Any],
	branch: str,
	ref: str | None = None,
) -> int:
	baseline_index = _baseline_satisfied_index(baseline_state)
	violations: list[str] = []
	pre_existing_drift_count = 0
	mc_compare_expected = 0
	mc_compare_satisfied = 0
	file_cache: dict[str, tuple[str | None, str | None]] = {}
	exists_cache: dict[str, tuple[bool, str | None]] = {}
	cross_issue_exact_drops, cross_issue_exact_warnings = _cross_issue_exact_conflict_drops(fingerprints)
	quarantine_runtime = _load_quarantine_runtime()
	quarantine_observed_keys: set[tuple[str, tuple[str, ...]]] = set()
	quarantine_unchanged_keys: set[tuple[str, tuple[str, ...]]] = set()

	for issue_key, entry in sorted(fingerprints.items()):
		if not isinstance(entry, dict):
			continue
		issue_num = entry.get("issue", issue_key)
		pr_num = entry.get("pr", "?")
		must_contain, must_not_contain, shared_keys, substring_drops, exact_conflict_drops = _dedup_issue_patterns(
			issue_key,
			entry,
			cross_issue_exact_drops,
		)
		must_not_exist = entry.get("must_not_exist", []) or []
		if shared_keys:
			print(
				f"::warning::Fingerprint cross-dedup for issue #{issue_num} (PR #{pr_num}): "
				f"skipping {len(shared_keys)} self-contradictory pattern(s) present in both "
				f"must_contain and must_not_contain (capture-side refactor false positive).",
				flush=True,
			)
		if substring_drops:
			print(
				f"::warning::Fingerprint substring-overlap dedup for issue #{issue_num} (PR #{pr_num}): "
				f"skipping {len(substring_drops)} must_not_contain pattern(s) whose regex is a literal "
				f"substring of a must_contain regex on the same file (capture-side: deleted line subsumed "
				f"by added line under re.search; the longer must_contain already enforces the stronger intent).",
				flush=True,
			)
		if exact_conflict_drops:
			for warning in cross_issue_exact_warnings.get(issue_key, []):
				print(warning, flush=True)

		for kind, fps in (
			("must_contain", must_contain),
			("must_not_contain", must_not_contain),
			("must_not_exist", must_not_exist),
		):
			for fp in fps:
				fp_key = _baseline_fp_key(kind, fp)
				if fp_key is None:
					continue
				quarantine_key = _quarantine_entry_key(issue_key, fp_key)
				quarantine_entry = quarantine_runtime["index"].get(quarantine_key)
				if _quarantine_entry_is_skipped(
					quarantine_entry,
					int(quarantine_runtime["threshold"]),
					str(quarantine_runtime["run_id"]),
				):
					quarantine_observed_keys.add(quarantine_key)
					quarantine_unchanged_keys.add(quarantine_key)
					_emit_quarantine_marker(issue_key, issue_num, fp_key, quarantine_runtime)
					continue
				state = _evaluate_fp_state(fp, kind, issue_num, pr_num, file_cache, exists_cache, ref=ref)
				if state is None:
					continue
				baseline_satisfied = baseline_index.get((str(issue_key), kind, state["fp_key"]))
				if quarantine_entry is not None:
					quarantine_observed_keys.add(quarantine_key)
				if baseline_satisfied is False and _quarantine_entry_is_suppressed(
					quarantine_entry,
					int(quarantine_runtime["threshold"]),
					str(quarantine_runtime["run_id"]),
				):
					if state["check_error"] is not None or not state["satisfied"]:
						quarantine_unchanged_keys.add(quarantine_key)
					continue
				if state["check_error"] is not None:
					if baseline_satisfied is False:
						quarantine_unchanged_keys.add(quarantine_key)
						_emit_pre_existing_drift_marker(state, issue_num, fixed_by_resolver=False)
						pre_existing_drift_count += 1
					else:
						violations.append(state["violation"])
					continue
				if kind == "must_contain":
					if baseline_satisfied is not False:
						mc_compare_expected += 1
						if state["satisfied"]:
							mc_compare_satisfied += 1
				if baseline_satisfied is None:
					if not state["satisfied"]:
						violations.append(state["violation"])
					continue
				if baseline_satisfied:
					if not state["satisfied"]:
						violations.append(state["violation"])
					continue
				if state["satisfied"]:
					_emit_pre_existing_drift_marker(state, issue_num, fixed_by_resolver=True)
					continue
				quarantine_unchanged_keys.add(quarantine_key)
				_emit_pre_existing_drift_marker(state, issue_num, fixed_by_resolver=False)
				pre_existing_drift_count += 1

	_emit_must_contain_ratio(branch, mc_compare_satisfied, mc_compare_expected)
	_persist_quarantine_runtime(
		quarantine_runtime,
		observed_keys=quarantine_observed_keys,
		unchanged_keys=quarantine_unchanged_keys,
	)
	if violations:
		return _emit_verify_failure(violations)
	if pre_existing_drift_count > 0:
		print(
			"Integration fingerprint verification PASSED with pre-existing drift — resolver did not introduce any new regressions "
			f"(pre_existing_drift_count={pre_existing_drift_count}; see PRE_EXISTING_FINGERPRINT_DRIFT_V1 markers above for triage).",
			flush=True,
		)
		return 0
	print(
		"Integration fingerprint verification PASSED — all merged sub-issue intent preserved.",
		flush=True,
	)
	return 0


def main(argv: list[str] | None = None) -> int:
	args = list(sys.argv[1:] if argv is None else argv)

	# Optional --list-violated-files <fingerprints.json> and
	# --ref <git_ref> flags plus additive baseline-capture/compare
	# flags.  Keep the parsing deliberately minimal
	# (no argparse) so this stays a single-file utility safe to
	# bootstrap on older script refs.  Both flags may appear in any
	# order before the positional fingerprints path.
	list_mode = False
	export_safe_mode = False
	ref: str | None = None
	baseline_out_path: str | None = None
	compare_baseline_path: str | None = None
	verification_tier = "strict"
	cli_ref_supplied = False
	while args:
		if args[0] == "--export-resolver-safe-fingerprints":
			export_safe_mode = True
			args = args[1:]
			continue
		if args[0] == "--list-violated-files":
			list_mode = True
			args = args[1:]
			continue
		if args[0] == "--ref":
			if len(args) < 2:
				print(
					"::warning::verify_integration_fingerprints: --ref requires an argument; ignoring.",
					flush=True,
					file=sys.stderr,
				)
				args = args[1:]
				continue
			ref = args[1]
			cli_ref_supplied = True
			args = args[2:]
			continue
		if args[0].startswith("--ref="):
			ref = args[0][len("--ref="):]
			cli_ref_supplied = True
			args = args[1:]
			continue
		if args[0] == "--baseline-fingerprints-state":
			if len(args) < 2:
				print(
					"::error::verify_integration_fingerprints: --baseline-fingerprints-state requires an argument",
					flush=True,
					file=sys.stderr,
				)
				return 2
			baseline_out_path = args[1]
			args = args[2:]
			continue
		if args[0].startswith("--baseline-fingerprints-state="):
			baseline_out_path = args[0][len("--baseline-fingerprints-state="):]
			args = args[1:]
			continue
		if args[0] == "--compare-against-baseline":
			if len(args) < 2:
				print(
					"::error::verify_integration_fingerprints: --compare-against-baseline requires an argument",
					flush=True,
					file=sys.stderr,
				)
				return 2
			compare_baseline_path = args[1]
			args = args[2:]
			continue
		if args[0].startswith("--compare-against-baseline="):
			compare_baseline_path = args[0][len("--compare-against-baseline="):]
			args = args[1:]
			continue
		if args[0] == "--verification-tier":
			if len(args) < 2:
				print(
					"::error::verify_integration_fingerprints: --verification-tier requires an argument",
					flush=True,
					file=sys.stderr,
				)
				return 2
			verification_tier = args[1]
			args = args[2:]
			continue
		if args[0].startswith("--verification-tier="):
			verification_tier = args[0][len("--verification-tier="):]
			args = args[1:]
			continue
		break

	if verification_tier not in VERIFICATION_TIERS:
		print(
			f"::error::verify_integration_fingerprints: unsupported --verification-tier {verification_tier!r} "
			f"(expected one of {', '.join(VERIFICATION_TIERS)})",
			flush=True,
			file=sys.stderr,
		)
		return 2

	raw_ref = ref
	ref = _normalize_ref(ref)
	if raw_ref is not None and ref is None:
		print(
			"::warning::verify_integration_fingerprints: blank --ref value supplied; ignoring and falling back to working-tree mode.",
			flush=True,
			file=sys.stderr,
		)

	if ref is None and not cli_ref_supplied:
		env_ref = os.environ.get("INTEGRATION_VERIFY_REF", "").strip()
		if env_ref:
			ref = env_ref

	if baseline_out_path is not None and compare_baseline_path is not None:
		print(
			"::error::verify_integration_fingerprints: --baseline-fingerprints-state and --compare-against-baseline are mutually exclusive",
			flush=True,
			file=sys.stderr,
		)
		return 2

	if args and args[0].startswith("--"):
		print(
			f"::warning::verify_integration_fingerprints: unknown option '{args[0]}'; skipping verification.",
			flush=True,
			file=sys.stderr,
		)
		return 2

	fp_path = args[0] if args else os.environ.get("INTEGRATION_FINGERPRINTS_FILE", "")
	if len(args) > 1:
		extra_arg = args[1]
		message = (
			f"::warning::verify_integration_fingerprints: unknown option '{extra_arg}'; skipping verification."
			if extra_arg.startswith("--")
			else f"::warning::verify_integration_fingerprints: unexpected trailing argument '{extra_arg}' after fingerprints path; skipping verification."
		)
		print(message, flush=True, file=sys.stderr)
		return 2
	if not fp_path:
		# Route to stderr so the stdout contract ("file paths ONLY") holds
		# in list mode even on plumbing failures.  GitHub Actions still
		# renders ::warning:: annotations from stderr.
		print(
			"::warning::verify_integration_fingerprints: no fingerprints path supplied (positional arg or INTEGRATION_FINGERPRINTS_FILE env); skipping verification.",
			flush=True,
			file=sys.stderr,
		)
		return 2

	branch = os.environ.get("INTEGRATION_BRANCH_NAME", "")
	data, err = _load_fingerprints(fp_path)
	if err is not None:
		# Same rationale as above — stderr keeps list-mode stdout clean.
		print(f"::warning::{err}; skipping verification.", flush=True, file=sys.stderr)
		return 2
	assert data is not None  # for type narrowing
	if export_safe_mode:
		safe_fingerprints, export_error = export_resolver_safe_fingerprints(data)
		if export_error is not None:
			print(f"::warning::resolver-safe fingerprint export failed: {export_error}.", file=sys.stderr)
			return 2
		assert safe_fingerprints is not None
		print(json.dumps(safe_fingerprints, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
		return 0

	if baseline_out_path is not None:
		return capture_baseline_state(data, baseline_out_path, branch, ref=ref)

	if compare_baseline_path is not None:
		baseline_state, baseline_err = _load_baseline_state(compare_baseline_path)
		if baseline_err is not None:
			print(f"::warning::baseline malformed ({baseline_err}); using absolute verification.", flush=True)
			return verify_with_tier(data, branch, verification_tier, ref=ref)
		assert baseline_state is not None
		return compare_against_baseline_with_tier(data, baseline_state, branch, verification_tier, ref=ref)

	if list_mode:
		# Always exit 0 in list mode — the caller is collecting input
		# for the resolver's working set, not enforcing.  Empty JSON
		# simply prints nothing.
		for path in list_violated_files(data, ref=ref):
			print(path, flush=True)
		return 0

	if not data:
		print(
			"Integration fingerprints object is empty; no merged sub-issues to verify. Skipping.",
			flush=True,
		)
		return 0

	return verify_with_tier(data, branch, verification_tier, ref=ref)


if __name__ == "__main__":
	sys.exit(main())
