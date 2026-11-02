"""PR-only tolerance for template-ahead `.claude/` drift (issue #7137, plan Q11/Q25).

The pipeline's editors cannot write `.claude/**`, so a PR that changes a
`workflow-templates/.claude/` file leaves its live copy behind until the
post-merge sync PR lands. The parity tests call `pending_template_paths()` and
skip only the drift it returns.

It returns an empty set (strict parity) unless `CLAUDE_PARITY_PR_BASE_SHA` is
a 40-hex commit. CI sets that variable only on pull-request events, so pushes
to `main` and both release gates stay strict. With it set, a drifted path is
tolerated when this PR changed the template, or when the same drift already
existed on the PR's base (a sync PR still waiting on a human merge), unless
this PR changed both copies and left them different (issue #8405: the post-merge
sync must not be the only repair for that state). Any git error yields the
empty set, so the tests fail closed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ENV_NAME = "CLAUDE_PARITY_PR_BASE_SHA"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SYNC_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_claude_live_copies.py"


def _sync_module():
	name = "claude_template_drift_sync"
	if name in sys.modules:
		return sys.modules[name]
	spec = importlib.util.spec_from_file_location(name, _SYNC_SCRIPT)
	if spec is None or spec.loader is None:
		raise ImportError("sync_claude_live_copies.py unavailable")
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
	return subprocess.run(
		["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args],
		cwd=root, capture_output=True, check=False,
	)


def _blob_at(root: Path, commit: str, path: str) -> tuple[str, bytes] | None:
	"""(mode, content) of path at commit, or None when it does not exist."""
	tree = _git(root, "ls-tree", commit, "--", path)
	if tree.returncode != 0:
		raise RuntimeError("git ls-tree failed")
	line = tree.stdout.decode("utf-8", "replace").strip()
	if not line:
		return None
	mode = line.split(None, 1)[0]
	show = _git(root, "show", f"{commit}:{path}")
	if show.returncode != 0:
		raise RuntimeError("git show failed")
	return mode, show.stdout


def _drifted_at(root: Path, commit: str, relative: str, sync) -> bool:
	template = _blob_at(root, commit, sync.TEMPLATE_PREFIX + relative)
	if template is None:
		return False
	return template != _blob_at(root, commit, sync.LIVE_PREFIX + relative)


def pending_template_paths(repo_root: Path) -> set[str]:
	base = os.environ.get(ENV_NAME, "").strip()
	if not _SHA_RE.match(base):
		return set()
	try:
		sync = _sync_module()
		drift = set(sync.mismatched(repo_root))
		if not drift:
			return set()
		diff = _git(repo_root, "diff", "--name-only", f"{base}...HEAD", "--", sync.TEMPLATE_PREFIX, sync.LIVE_PREFIX)
		if diff.returncode != 0:
			return set()
		diff_lines = diff.stdout.decode("utf-8", "replace").splitlines()
		changed = {
			line[len(sync.TEMPLATE_PREFIX):]
			for line in diff_lines
			if line.startswith(sync.TEMPLATE_PREFIX)
		}
		# `.claude/` is not a prefix of `workflow-templates/.claude/`, so a line
		# lands in at most one of the two sets.
		live_changed = {
			line[len(sync.LIVE_PREFIX):]
			for line in diff_lines
			if line.startswith(sync.LIVE_PREFIX)
		}
		return {
			relative for relative in drift
			if not (relative in changed and relative in live_changed)
			and (relative in changed or _drifted_at(repo_root, base, relative, sync))
		}
	except Exception:  # noqa: BLE001 - any failure keeps parity strict
		return set()


# Permission rules only coding-workflows' live `.claude/settings.json` carries
# (`.github/ai/claude_template_divergence.json` lists the file). The daily
# activation routine runs in a coding-workflows session and opens sessions
# without the Auto-mode check (operator decision 2026-10-26); consumer repos
# never receive the rule.
LIVE_ONLY_ALLOW_RULES = ("mcp__claude-code-remote__create_session",)


def settings_match_template(template_text: str, live_text: str) -> bool:
	"""True when the live settings are the template plus exactly the live-only rules.

	The live file must equal the template text with the live-only rules
	appended, in order, at the end of `permissions.allow`; any other
	difference, a missing rule or an extra one, is a mismatch.
	"""
	try:
		template = json.loads(template_text)
		live = json.loads(live_text)
	except ValueError:  # malformed settings JSON is a mismatch, not a traceback
		return False
	if not isinstance(template, dict) or not isinstance(live, dict) or not isinstance(live.get("permissions"), dict):
		return False  # valid JSON of the wrong shape is a mismatch too
	live_allow = live.get("permissions", {}).get("allow", [])
	if not isinstance(live_allow, list):
		return False  # a non-list allow value is a mismatch, not a TypeError
	extras = tuple(live_allow[len(live_allow) - len(LIVE_ONLY_ALLOW_RULES):])
	if extras != LIVE_ONLY_ALLOW_RULES:
		return False
	live["permissions"]["allow"] = live_allow[: len(live_allow) - len(LIVE_ONLY_ALLOW_RULES)]
	if live != template:
		return False
	# Formatting stays the template's: only the appended lines may differ.
	stripped = live_text
	for rule in LIVE_ONLY_ALLOW_RULES:
		stripped = re.sub(r",\s*\"" + re.escape(rule) + r"\"", "", stripped, count=1)
	return stripped == template_text
