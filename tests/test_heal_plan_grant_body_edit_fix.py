"""Unblock fix-up #7444 (for #7434): heal-issue plan gate and pipeline-only body edits.

Covers `workflow_failure_heal.verify_heal_scope_pipeline_edits` (and its
`heal-scope verify-edits` CLI) and the heal-scope branch of
`files_touched_scope_guard.py --plan-auto-approval-gate --heal-scope-file`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import files_touched_scope_guard as guard  # noqa: E402
import workflow_failure_heal as heal  # noqa: E402

MARKER = "<!-- ai:workflow-heal-scope:v1 paths=scripts/fix.py,tests/**,changelog.d/*.md runs=owner/repo:500 -->"
OTHER_MARKER = "<!-- ai:workflow-heal-scope:v1 paths=scripts/other.py,tests/**,changelog.d/*.md runs=owner/repo:500 -->"
BODY = "Heal this.\n" + MARKER + "\n"
GRANTED_BODY = BODY + "\n---\n<!-- ai:pipeline-plan-grant:v1 -->\nfiles_touched:\n  - scripts/fix.py\n"


def _edits(*diffs: str, editor: str = "pipeline", deleted: bool = False) -> list[dict]:
	return [{"editor": {"login": editor}, "deletedAt": "2026-10-15T00:00:00Z" if deleted else None, "diff": diff} for diff in diffs]


def _verify(edits, *, total=None, has_next=False, body=GRANTED_BODY, author="pipeline", labels=("ai:workflow-heal",), login="pipeline"):
	return heal.verify_heal_scope_pipeline_edits(body=body, author_login=author, labels=list(labels), pipeline_login=login,
		edits=edits, edits_total=len(edits) if total is None and isinstance(edits, list) else total, edits_has_next_page=has_next)


def test_pipeline_only_edits_are_accepted_case_insensitively() -> None:
	result = _verify(_edits(GRANTED_BODY, BODY, editor="Pipeline"))
	assert result["status"] == "verified"
	assert result["paths"] == ["scripts/fix.py", "tests/**", "changelog.d/*.md"]
	assert result["marker"] == MARKER
	assert result["pipeline_edits"] == 2


def test_other_editor_is_refused() -> None:
	edits = _edits(GRANTED_BODY) + _edits(BODY, editor="maintainer")
	assert _verify(edits) == {"status": "edited", "detail": "editor_untrusted", "paths": [], "runs": [], "marker": ""}
	missing_editor = [{"editor": None, "deletedAt": None, "diff": BODY}]
	assert _verify(missing_editor)["detail"] == "editor_untrusted"


def test_incomplete_history_is_refused() -> None:
	assert _verify([])["detail"] == "edit_history_unavailable"
	assert _verify(None, total=0)["detail"] == "edit_history_unavailable"
	assert _verify(_edits(BODY), has_next=True)["detail"] == "edit_history_unavailable"
	assert _verify(_edits(BODY), total=2)["detail"] == "edit_history_unavailable"
	assert _verify(_edits(BODY), total="1")["detail"] == "edit_history_unavailable"
	assert _verify(_edits(BODY), has_next=None)["detail"] == "edit_history_unavailable"


def test_redacted_revision_is_refused() -> None:
	assert _verify(_edits(GRANTED_BODY) + _edits(BODY, deleted=True))["detail"] == "edit_history_redacted"


def test_changed_or_missing_marker_in_any_revision_is_refused() -> None:
	assert _verify(_edits(GRANTED_BODY, BODY.replace(MARKER, OTHER_MARKER)))["detail"] == "marker_changed"
	assert _verify(_edits(GRANTED_BODY, "Heal this.\n"))["detail"] == "marker_changed"
	assert _verify(_edits(GRANTED_BODY, BODY + MARKER))["detail"] == "marker_changed"
	assert _verify([{"editor": {"login": "pipeline"}, "deletedAt": None, "diff": None}])["detail"] == "marker_changed"


def test_base_statuses_pass_through() -> None:
	assert _verify(_edits(BODY), body="no marker")["status"] == "missing"
	assert _verify(_edits(BODY), body=BODY + MARKER)["status"] == "duplicated"
	assert _verify(_edits(BODY), author="stranger")["status"] == "untrusted_author"
	assert _verify(_edits(BODY), labels=())["status"] == "untrusted_author"


def test_verify_edits_cli_round_trip(tmp_path: Path) -> None:
	payload = {"body": GRANTED_BODY, "author_login": "pipeline", "labels": ["ai:workflow-heal"], "pipeline_login": "pipeline",
		"edits": _edits(GRANTED_BODY, BODY), "edits_total": 2, "edits_has_next_page": False}
	result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/workflow_failure_heal.py"), "heal-scope", "verify-edits",
		"--input-json", "/dev/stdin"], input=json.dumps(payload), capture_output=True, text=True, check=True,
		env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
	data = json.loads(result.stdout)
	assert data["status"] == "verified" and data["pipeline_edits"] == 2
	payload["edits"][0]["editor"]["login"] = "maintainer"
	result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/workflow_failure_heal.py"), "heal-scope", "verify-edits",
		"--input-json", "/dev/stdin"], input=json.dumps(payload), capture_output=True, text=True, check=True,
		env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
	assert json.loads(result.stdout)["detail"] == "editor_untrusted"


# --- files_touched_scope_guard.py --heal-scope-file ---------------------------

SCOPE = "scripts/fix.py\n.github/workflows/plan.yml\ntests/**\nchangelog.d/*.md\n"


def _gate(tmp_path: Path, plan: str, *, scope: str = SCOPE, flag: str = "true", allow_edits: str = "true") -> tuple[dict, int, Path]:
	plan_file = tmp_path / "plan.txt"
	plan_file.write_text(plan, encoding="utf-8")
	scope_file = tmp_path / "scope.txt"
	scope_file.write_text(scope, encoding="utf-8")
	grant_out = tmp_path / "grant-body.txt"
	result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/files_touched_scope_guard.py"),
		"--plan-auto-approval-gate", "--plan-file", str(plan_file), "--heal-scope-file", str(scope_file),
		"--flag-enabled", flag, "--allow-workflow-edits", allow_edits, "--issue-number", "42",
		"--pipeline-grant-body-out", str(grant_out)],
		capture_output=True, text=True, check=False, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
	return json.loads(result.stdout), result.returncode, grant_out


def _plan(*paths: str) -> str:
	return "Implementation Plan\n\n## Files likely to change\n" + "".join(f"- `{p}`\n" for p in paths) + "\n## Risks\n- none\n"


def test_gate_approves_plan_inside_heal_scope(tmp_path: Path) -> None:
	record, code, grant_out = _gate(tmp_path, _plan("scripts/fix.py", ".github/workflows/plan.yml", "tests/test_fix.py", "changelog.d/1-fix.md"))
	assert code == 0
	assert record["decision"] == "approve" and record["reason"] == "heal_scope_verified"
	assert not grant_out.exists()


def test_gate_approves_the_issues_own_agents_fragment(tmp_path: Path) -> None:
	# CLAUDE.md §30 requires an agents.d fragment; the marker never lists one (#8064).
	record, code, _ = _gate(tmp_path, _plan("scripts/fix.py", "changelog.d/42-fix.md", "agents.d/42-fix.md"))
	assert code == 0, record
	assert record["decision"] == "approve" and record["reason"] == "heal_scope_verified"


def test_gate_holds_another_issues_or_nested_agents_fragment(tmp_path: Path) -> None:
	record, code, _ = _gate(tmp_path, _plan("scripts/fix.py", "agents.d/43-fix.md", "agents.d/42-x/fix.md", "agents.md"))
	assert code == guard.EXIT_AUTOMATION_UNGRANTED
	assert record["reason"] == "heal_scope_plan_out_of_scope"
	assert record["denied_paths"] == ["agents.d/43-fix.md", "agents.d/42-x/fix.md", "agents.md"]


def test_heal_own_agents_fragment_entry_shape() -> None:
	assert guard.heal_own_agents_fragment_entry("8064") == "agents.d/8064-*.md"
	assert guard.heal_own_agents_fragment_entry(42) == "agents.d/42-*.md"
	for bad in ("", None, "0", "042", "-1", "4 2", "42*", "1" * 11, "../1"):
		assert guard.heal_own_agents_fragment_entry(bad) == "", bad
	# The transfer validator and implement_heal_preflight.sh accept/emit this shape.
	assert 'printf \'agents.d/%s-*.md\\n\' "${ISSUE_NUMBER}"' in (ROOT / "scripts/implement_heal_preflight.sh").read_text()
	assert r'agents\.d/[1-9][0-9]{0,9}-\*\.md' in (ROOT / "scripts/review_untrusted_workspace.py").read_text()


def test_gate_holds_out_of_scope_path(tmp_path: Path) -> None:
	record, code, grant_out = _gate(tmp_path, _plan("scripts/fix.py", "scripts/other.sh", "README.md"))
	assert code == guard.EXIT_AUTOMATION_UNGRANTED
	assert record["reason"] == "heal_scope_plan_out_of_scope"
	assert record["denied_paths"] == ["scripts/other.sh", "README.md"]
	assert not grant_out.exists()


def test_gate_holds_directory_and_glob_entries(tmp_path: Path) -> None:
	record, code, _ = _gate(tmp_path, _plan("scripts/fix.py", "scripts/"))
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["reason"] == "heal_scope_plan_out_of_scope"
	assert "scripts/" in record["denied_paths"]
	record, _, _ = _gate(tmp_path, _plan("scripts/fix.py", "tests/**"))
	assert record["reason"] == "heal_scope_plan_out_of_scope"
	assert "tests/**" in record["denied_paths"]


def test_gate_holds_extensionless_nested_path(tmp_path: Path) -> None:
	record, code, _ = _gate(tmp_path, _plan("scripts/fix.py", "docs/guide"))
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["reason"] == "heal_scope_plan_out_of_scope"
	assert "docs/guide" in record["denied_paths"]


def test_gate_holds_root_level_glob(tmp_path: Path) -> None:
	record, code, _ = _gate(tmp_path, _plan("scripts/fix.py", "*.py"))
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["reason"] == "heal_scope_plan_out_of_scope"
	assert "*.py" in record["denied_paths"]


def test_gate_ignores_bold_prose_bullets(tmp_path: Path) -> None:
	plan = ("Implementation Plan\n\n1. **Files likely to change**\n- `scripts/fix.py`\n"
		"- **Note:** tests go under `tests/test_fix.py`\n\n## Risks\n- none\n")
	record, code, _ = _gate(tmp_path, plan)
	assert code == 0 and record["reason"] == "heal_scope_verified", record


def test_gate_reads_bold_backticked_bullets_once(tmp_path: Path) -> None:
	plan = ("Implementation Plan\n\n## Files likely to change\n- **`scripts/fix.py`** — change X\n"
		"- *Optional:* `tests/test_fix.py`\n\n## Risks\n- none\n")
	record, code, _ = _gate(tmp_path, plan)
	assert code == 0 and record["reason"] == "heal_scope_verified", record


def test_gate_holds_bare_italic_shaped_glob(tmp_path: Path) -> None:
	# `*foo*` is both italic markdown and a glob; the gate checks it (fail closed).
	for word in ("*foo*", "*data-*"):
		plan = "Implementation Plan\n\n## Files likely to change\n- `scripts/fix.py`\n- " + word + "\n\n## Risks\n- none\n"
		record, code, _ = _gate(tmp_path, plan)
		assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["reason"] == "heal_scope_plan_out_of_scope", record
		assert word in record["denied_paths"]


def test_gate_holds_bare_root_level_glob(tmp_path: Path) -> None:
	record, code, _ = _gate(tmp_path, "Implementation Plan\n\n## Files likely to change\n- `scripts/fix.py`\n- *.py\n\n## Risks\n- none\n")
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["reason"] == "heal_scope_plan_out_of_scope"
	assert "*.py" in record["denied_paths"]


def test_gate_respects_allow_workflow_edits(tmp_path: Path) -> None:
	record, code, _ = _gate(tmp_path, _plan("scripts/fix.py"), allow_edits="false")
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["reason"] == "workflow_edits_disabled"
	record, code, _ = _gate(tmp_path, _plan("tests/test_fix.py", "changelog.d/1-fix.md"), allow_edits="false")
	assert code == 0 and record["reason"] == "heal_scope_verified"


def test_gate_holds_plan_without_files_section(tmp_path: Path) -> None:
	record, code, _ = _gate(tmp_path, "Implementation Plan\n\nEdit some files.\n")
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["reason"] == "heal_scope_plan_paths_missing"


def test_gate_respects_the_automation_path_kill_switch(tmp_path: Path) -> None:
	record, code, _ = _gate(tmp_path, _plan("scripts/fix.py"), flag="false")
	assert code == guard.EXIT_AUTOMATION_UNGRANTED
	assert record["reason"] == "automation_path_plan_auto_approval_disabled"


def test_gate_holds_on_empty_scope_or_plan(tmp_path: Path) -> None:
	record, _, _ = _gate(tmp_path, _plan("scripts/fix.py"), scope="")
	assert record["reason"] == "heal_scope_check_unavailable"
	record, _, _ = _gate(tmp_path, "")
	assert record["reason"] == "plan_gate_error"


def main() -> int:
	import tempfile
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		if "tmp_path" in test.__code__.co_varnames[:test.__code__.co_argcount]:
			with tempfile.TemporaryDirectory() as tmp:
				test(Path(tmp))
		else:
			test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
