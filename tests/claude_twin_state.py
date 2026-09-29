"""Sync-state assertions between `.claude/` and its twin (CLAUDE.md §28.C).

Unattended sessions edit only `workflow-templates/.claude/**`;
`.github/workflows/claude-twin-sync.yml` copies each change into `.claude/**`
through a sync PR after it merges (issue #4785). So on a PR the twin may be
ahead of `.claude/` (awaiting its sync PR), but `.claude/` may never be ahead
of the twin. Behaviour tests therefore load the twin, and every former
byte-parity assert calls `assert_claude_not_ahead` instead:

* an upstream-only path (`UPSTREAM_ONLY_PATHS`, not a twin) → pass, the
  same exemption as CI's `claude_twin_sync.py check`;
* the two copies are identical → pass;
* the `.claude/` copy is missing while the twin exists → pass (a new twin
  file awaiting sync);
* the `.claude/` copy equals a version the twin had in git history (the twin
  moved on) → pass;
* otherwise → fail: `.claude/` was changed to content its twin never held.

In a shallow clone (CI's depth-1 checkout) the history is not available, so a
differing pair is skipped here; CI's "Claude twin sync state" step enforces
the same rule on the commit range with `scripts/claude_twin_sync.py check`.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("claude_twin_sync_for_tests", ROOT / "scripts" / "claude_twin_sync.py")
_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sync)

TWIN_DIR = ROOT / _sync.TWIN_ROOT
CLAUDE_DIR = ROOT / _sync.CLAUDE_ROOT


def _git(*args: str) -> subprocess.CompletedProcess:
	return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def claude_ahead_reason(rel: str) -> tuple[str, str]:
	"""(`ok` | `skip` | `ahead`, message) for `.claude/<rel>` against its twin (see module doc)."""
	claude_path = CLAUDE_DIR / rel
	twin_path = TWIN_DIR / rel
	if rel in _sync.UPSTREAM_ONLY_PATHS:
		return "ok", ""  # not a twin: exempt, as in `claude_twin_sync.py check`
	if not twin_path.is_file():
		return "ahead", f"{twin_path.relative_to(ROOT)} is missing: edit the twin, never .claude/ alone"
	if not claude_path.exists() or claude_path.read_bytes() == twin_path.read_bytes():
		return "ok", ""
	shallow = _git("rev-parse", "--is-shallow-repository")
	if shallow.returncode != 0 or shallow.stdout.strip() != "false":
		return "skip", f".claude/{rel} differs from its twin; history is unavailable here, so CI's twin sync state step decides"
	blob = _git("hash-object", str(claude_path)).stdout.strip()
	if blob in _sync.twin_history_blobs(str(ROOT), "HEAD", rel):
		return "ok", ""
	return "ahead", (
		f".claude/{rel} is ahead of {_sync.TWIN_ROOT}/{rel}: it holds content the twin never had. "
		"Edit the twin and let claude-twin-sync.yml copy it (CLAUDE.md §28.C)."
	)


def assert_claude_not_ahead(rel: str) -> None:
	"""`.claude/<rel>` equals its twin or an earlier version of it (see module doc)."""
	import pytest

	status, message = claude_ahead_reason(rel)
	if status == "skip":
		pytest.skip(message)
	assert status == "ok", message
