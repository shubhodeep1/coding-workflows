"""Contract tests for the DEAD-001 / DEAD-002 cleanup (issue #7377, Refs #7355).

- The branch-rebuild threshold decision is logged at its caller with both
  fields the threshold function sets.
- The unused `WAVE_COUNT` (orchestrate.yml summary step) and `BLOB_CONTENT`
  (test-and-mark-stable.yml bait-injection step) values are gone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agents_doc import agents_text as _agents_text_with_fragments  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
ORCHESTRATE_YML = REPO_ROOT / ".github" / "workflows" / "orchestrate.yml"
STABLE_YML = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

LOG_KEY = "BRANCH_REBUILD_THRESHOLD_DECISION"


def _function_body(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\)\s*\{{\n(.*?)^\}}", text, re.M | re.S)
    assert match, f"function {name} not found"
    return match.group(1)


def _step_run(path: Path, needle: str) -> str:
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    runs = []
    for job in (data.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            if needle in run:
                runs.append(run)
    assert len(runs) == 1, f"expected one step containing {needle!r} in {path.name}, found {len(runs)}"
    return runs[0]


def test_caller_logs_threshold_decision_in_both_branches() -> None:
    body = _function_body(POLLER.read_text(encoding="utf-8"), "_attempt_branch_rebuild_after_escalation")
    lines = [line for line in body.splitlines() if LOG_KEY in line and "echo" in line]
    assert len(lines) >= 2
    for line in lines:
        assert "tracking_issue=" in line
        assert "skip_reason=${BRANCH_REBUILD_SKIP_REASON:-none}" in line
        assert "last_rebuild_at=${BRANCH_REBUILD_LAST_REBUILD_AT:-none}" in line

    block = re.search(
        r'if ! _check_branch_rebuild_threshold [^\n]*; then\n(.*?)\n\s*fi\n([^\n]*)\n',
        body,
        re.S,
    )
    assert block, "threshold check block not found"
    inside, after = block.group(1), block.group(2)
    assert LOG_KEY in inside
    assert inside.index(LOG_KEY) < inside.index("return 0")
    assert LOG_KEY in after


def test_threshold_function_still_sets_both_values() -> None:
    body = _function_body(POLLER.read_text(encoding="utf-8"), "_check_branch_rebuild_threshold")
    assert 'BRANCH_REBUILD_SKIP_REASON="' in body
    assert "BRANCH_REBUILD_LAST_REBUILD_AT=" in body


def test_orchestrate_summary_drops_wave_count() -> None:
    run = _step_run(ORCHESTRATE_YML, "## AI Orchestrate Summary")
    assert "WAVE_COUNT" not in run
    assert "ISSUE_COUNT" in run


def test_bait_injection_drops_blob_content() -> None:
    run = _step_run(STABLE_YML, "Fetching current canary blob")
    assert "BLOB_CONTENT" not in run
    assert "BLOB_SHA=" in run
    assert "SHA + decoded content" not in run
    # The separate, used WAVE_COUNT elsewhere in the file must survive.
    assert "WAVE_COUNT" in STABLE_YML.read_text(encoding="utf-8")


def test_agents_fragment_registers_log_prefix() -> None:
    # The registry entry may still sit in a pending agents.d/ fragment or be
    # folded into agents.md already; agents_text() covers both states.
    text = _agents_text_with_fragments()
    heading = "## Stable log prefixes (contractual)"
    assert heading in text
    # Stop at the next "## " heading so a match in a later section cannot
    # satisfy the registry check.
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    assert f"- `{LOG_KEY}`" in section
    assert f"LOG_PREFIX.name={LOG_KEY}" in section
