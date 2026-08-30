#!/usr/bin/env python3
"""Wiring contract for the changelog.d fragment mechanism.

The assembler is covered by tests/test_assemble_changelog.py. This file pins
the parts that only exist as workflow / prompt / instruction text, so a future
edit cannot quietly detach the mechanism from the automation that runs it —
or reintroduce the four defects it was built to close:

  1. CLAUDE.md §20 mandating a conflict-prone direct edit of CHANGELOG.md.
  2. §20 pointing at docs/changelog-style.md, which no consumer repo has.
  3. prompts/mode-implement.txt naming CHANGELOG under an anchor guardrail
     that CHANGELOG.md has never carried anchors for.
  4. review_autofix.yml's deterministic skip path running no merge-conflict
     resolution.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ASSEMBLER = REPO_ROOT / "scripts" / "assemble_changelog.py"
UPDATE_WORKFLOWS = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
MARK_STABLE = REPO_ROOT / ".github" / "workflows" / "mark-stable.yml"
TEST_AND_MARK_STABLE = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
REVIEW_AUTOFIX = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
IMPLEMENT_PROMPTS = (
	REPO_ROOT / "prompts" / "mode-implement.txt",
	REPO_ROOT / "prompts" / "_templates" / "mode-implement.txt",
)
REMOVAL_REGISTRY = REPO_ROOT / "docs" / "scripts-pending-removal.md"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _workflow_step(workflow: str, name: str) -> str:
	step_parts = workflow.split(f"      - name: {name}", 1)
	assert len(step_parts) == 2, f"workflow step {name!r} not found"
	return step_parts[1].split("\n      - name: ", 1)[0]


def _normalized_step_predicate(workflow: str, name: str) -> tuple[str, ...]:
	step = _workflow_step(workflow, name)
	match = re.search(r"^\s*if:\s*(.+)$", step, flags=re.MULTILINE)
	assert match is not None, f"{name} has no if predicate"
	return tuple(part.strip() for part in match.group(1).split("||"))


# ──────────────────────────────────────────────────────────────────
# Repo scaffolding
# ──────────────────────────────────────────────────────────────────


def test_assembler_and_fragment_directory_exist() -> None:
	assert ASSEMBLER.is_file()
	assert (REPO_ROOT / "changelog.d").is_dir()
	assert (REPO_ROOT / "changelog.d" / ".gitkeep").is_file()


def test_gitattributes_carries_the_union_backstop() -> None:
	attributes = _read(REPO_ROOT / ".gitattributes")
	assert "CHANGELOG.md merge=union" in attributes
	assert "BEGIN coding-workflows changelog assets" in attributes
	assert "END coding-workflows changelog assets" in attributes


def test_assembler_is_registered_for_future_removal() -> None:
	"""CLAUDE.md §18.F — every new long-running script gets a registry entry."""
	registry = _read(REMOVAL_REGISTRY)
	assert "scripts/assemble_changelog.py" in registry


# ──────────────────────────────────────────────────────────────────
# Defect 1 + 2 — CLAUDE.md §20
# ──────────────────────────────────────────────────────────────────


def test_section_20_is_still_numbered_20() -> None:
	"""§6: section numbers are load-bearing and must never be renumbered."""
	claude_md = _read(CLAUDE_MD)
	headings = re.findall(r"^## §(\d+)\.", claude_md, flags=re.MULTILINE)
	assert headings == sorted(headings, key=int)
	assert len(headings) == len(set(headings)), "duplicate § numbers"
	assert "20" in headings
	assert re.search(r"^## §20\. CHANGELOG Entries", claude_md, flags=re.MULTILINE)


def test_section_20_is_self_contained() -> None:
	"""Defect 2: no pointer to a file absent from every consumer repo."""
	claude_md = _read(CLAUDE_MD)
	section = claude_md.split("## §20.", 1)[1].split("\n## ", 1)[0]
	assert "docs/changelog-style.md" not in section
	# The style substance travels inline instead.
	for marker in ("Headline", "Lead paragraph", "The numbers that matter", "delve"):
		assert marker in section, f"§20 lost inlined style guidance: {marker}"


def test_section_20_states_when_an_entry_is_required() -> None:
	claude_md = _read(CLAUDE_MD)
	section = claude_md.split("## §20.", 1)[1].split("\n## ", 1)[0]
	assert "When an entry is required" in section
	assert "Never edit `CHANGELOG.md` directly" in section
	assert "changelog.d/<issue-or-pr>-<slug>.md" in section
	assert "scripts/assemble_changelog.py" in section


def test_section_20_names_every_assembly_host() -> None:
	claude_md = _read(CLAUDE_MD)
	section = claude_md.split("## §20.", 1)[1].split("\n## ", 1)[0]
	for host in ("mark-stable.yml", "test-and-mark-stable.yml", "update_workflows.yml"):
		assert host in section, f"§20 does not name assembly host {host}"


# ──────────────────────────────────────────────────────────────────
# Defect 3 — the anchor guardrail
# ──────────────────────────────────────────────────────────────────


def test_implement_prompts_drop_changelog_from_the_anchor_guardrail() -> None:
	for path in IMPLEMENT_PROMPTS:
		text = _read(path)
		anchor_line = "Prefer append-only edits in shared docs"
		assert anchor_line in text, f"{path} lost the sibling-conflict guardrail"
		clause = text.split(anchor_line, 1)[1].split("\n\n", 1)[0]
		assert "CHANGELOG" not in clause, (
			f"{path} still routes CHANGELOG through the anchor guardrail, "
			"which CHANGELOG.md has never carried anchors for"
		)


def test_implement_prompts_teach_the_fragment_workflow() -> None:
	for path in IMPLEMENT_PROMPTS:
		text = _read(path)
		assert "changelog.d/<issue-or-pr>-<slug>.md" in text
		assert "never edit `CHANGELOG.md` directly" in text
		assert "<!-- changelog: fixed -->" in text


def test_both_implement_prompt_copies_agree() -> None:
	"""The rendered prompt and its template must not drift apart."""
	rendered, template = (_read(path) for path in IMPLEMENT_PROMPTS)
	marker = "Changelog discipline:"
	assert rendered.split(marker, 1)[1].split("\n\n", 1)[0] == (
		template.split(marker, 1)[1].split("\n\n", 1)[0]
	)


# ──────────────────────────────────────────────────────────────────
# Defect 4 — conflict resolution on the deterministic skip path
# ──────────────────────────────────────────────────────────────────


def test_gate_fetches_mergeability_from_the_existing_pr_call() -> None:
	"""§15: no new API call — the existing /pulls/{n} --jq is extended."""
	workflow = _read(REVIEW_AUTOFIX)
	assert "mergeable_state: (.mergeable_state" in workflow
	assert workflow.count('gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \\') == 1


def test_gate_suppresses_the_skip_on_a_conflicted_pr() -> None:
	workflow = _read(REVIEW_AUTOFIX)
	assert "CONFLICT_SKIP_SUPPRESSED" in workflow
	assert "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=merge_conflict" in workflow
	assert 'AUTOFIX_SKIP_SUPPRESS_ON_CONFLICT:-true' in workflow
	assert (
		"AUTOFIX_SKIP_SUPPRESS_ON_CONFLICT: ${{ vars.AUTOFIX_SKIP_SUPPRESS_ON_CONFLICT }}"
		in workflow
	), "the knob must be plumbed from repo vars (§4: env vars get defaults)"


def test_skip_gate_comment_no_longer_claims_conflicts_fall_through() -> None:
	workflow = _read(REVIEW_AUTOFIX)
	assert "Merge-conflict resolution is NOT run on" not in workflow


# ──────────────────────────────────────────────────────────────────
# Assembly wiring (§18.A / §18.B — automation only, no operator action)
# ──────────────────────────────────────────────────────────────────


def test_release_workflows_assemble_before_notes_and_tag() -> None:
	for path in (MARK_STABLE, TEST_AND_MARK_STABLE):
		workflow = _read(path)
		assert "name: Assemble changelog fragments" in workflow
		assert "scripts/assemble_changelog.py assemble" in workflow
		assemble_at = workflow.index("name: Assemble changelog fragments")
		notes_at = workflow.index("name: Extract changelog for this version")
		tag_at = workflow.index("name: Tag version and update stable pointer")
		assert assemble_at < notes_at < tag_at, (
			f"{path.name}: fragments must be folded before the release notes "
			"are extracted and the version tag is cut"
		)


def test_release_assembly_fails_open() -> None:
	"""A release must never fail because of changelog bookkeeping."""
	for path in (MARK_STABLE, TEST_AND_MARK_STABLE):
		workflow = _read(path)
		step = workflow.split("name: Assemble changelog fragments", 1)[1].split(
			"      - name: ", 1
		)[0]
		assert 'if [ "${DRY_RUN}" = "true" ]' in step
		assert 'git reset --hard "origin/${SOURCE_BRANCH}"' in step
		assert "::warning::Could not push the assembled changelog" in step


def test_consumer_sync_has_the_fifth_category_and_assembly() -> None:
	workflow = _read(UPDATE_WORKFLOWS)
	assert "name: Sync changelog fragment assets from upstream" in workflow
	assert "name: Assemble changelog fragments" in workflow
	assert 'ASSEMBLER="${UPSTREAM_SCRIPTS_DIR}/assemble_changelog.py"' in workflow
	assert '"${ASSEMBLER}" ensure-assets' in workflow
	assert '"${ASSEMBLER}" assemble --repo-root .' in workflow
	# Runs upstream's copy from the @stable support checkout — the consumer
	# never carries the script, so rule and machinery cannot drift.
	assert 'UPSTREAM_SCRIPTS_DIR="$(dirname "${UPSTREAM_DIR}")/scripts"' in workflow
	# Missing upstream script must be a graceful no-op, not a hard failure.
	assert workflow.count(
		'echo "Upstream has no scripts/assemble_changelog.py at this ref'
	) == 2


def test_consumer_sync_commit_step_stages_and_reports_the_new_categories() -> None:
	workflow = _read(UPDATE_WORKFLOWS)
	commit_step = workflow.split("name: Commit and push updates", 1)[1]
	assert "steps.changelog_sync.outputs.changelog_assets_has_changes == 'true'" in workflow
	assert "steps.changelog_assemble.outputs.changelog_assembled == 'true'" in workflow
	assert "/tmp/changelog_asset_files.txt" in commit_step
	# Assembly deletes fragments, so staging must pick up removals.
	assert "git add -A -- CHANGELOG.md changelog.d" in commit_step
	assert "Changelog fragment assets:" in commit_step


def test_consumer_sync_notification_predicate_matches_commit_predicate() -> None:
	workflow = _read(UPDATE_WORKFLOWS)
	commit_predicate = _normalized_step_predicate(workflow, "Commit and push updates")
	notification_predicate = _normalized_step_predicate(workflow, "Send Telegram notification")
	assert notification_predicate == commit_predicate
	assert notification_predicate == (
		"steps.update.outputs.has_updates == 'true'",
		"steps.audit_gate.outputs.status == 'applied'",
		"steps.claude_sync.outputs.claude_has_changes == 'true'",
		"steps.claude_md_sync.outputs.claude_md_changed == 'true'",
		"steps.changelog_sync.outputs.changelog_assets_has_changes == 'true'",
		"steps.changelog_assemble.outputs.changelog_assembled == 'true'",
	)


def test_consumer_sync_notification_reports_changelog_only_updates() -> None:
	workflow = _read(UPDATE_WORKFLOWS)
	notification_step = _workflow_step(workflow, "Send Telegram notification")
	assert "/tmp/changelog_asset_files.txt" in notification_step
	assert "Changelog fragment assets changed:" in notification_step
	assert 'if [ -n "$CHANGELOG_ASSET_LIST" ]' in notification_step
	assert "${{ steps.changelog_assemble.outputs.changelog_fragment_count }}" in notification_step
	assert "${{ steps.changelog_assemble.outputs.changelog_layout }}" in notification_step
	assert 'if [ "${CHANGELOG_ASSEMBLED}" = "true" ]' in notification_step
	assert (
		"Changelog fragments assembled: ${CHANGELOG_FRAGMENT_COUNT} fragment(s), "
		"layout ${CHANGELOG_LAYOUT}."
	) in notification_step


def test_consumer_sync_notification_preserves_existing_category_text() -> None:
	notification_step = _workflow_step(
		_read(UPDATE_WORKFLOWS), "Send Telegram notification"
	)
	for text in (
		"wrapper(s) updated:",
		"new wrapper(s) added:",
		"Audit-gate assets applied:",
		".claude/ asset(s) synced:",
		"Top-level CLAUDE.md synced:",
	):
		assert text in notification_step


def test_sync_step_order_matches_the_documented_categories() -> None:
	"""The fifth category lands alongside the existing four, not before them."""
	workflow = _read(UPDATE_WORKFLOWS)
	order = [
		"name: Apply canonical audit-gate assets",
		"name: Sync .claude/ assets from upstream",
		"name: Sync top-level CLAUDE.md from upstream",
		"name: Sync changelog fragment assets from upstream",
		"name: Compare and update workflow wrappers",
	]
	positions = [workflow.index(step) for step in order]
	assert positions == sorted(positions)


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"OK: {len(tests)} changelog fragment contract tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
