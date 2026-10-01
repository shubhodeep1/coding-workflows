#!/usr/bin/env python3
"""Contract checks for update_workflows guardrail behavior."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from claude_twin_sync import GUARD_PATH_FILES, GUARD_PATH_PREFIXES, is_guard_path  # noqa: E402
from workflow_wrapper_refs import pin_reusable_workflow_refs, validate_release_sha


UPDATE_WORKFLOWS_WF = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
WORKFLOW_TEMPLATES_DIR = REPO_ROOT / "workflow-templates"
WORKFLOW_PROFILE_DIR = WORKFLOW_TEMPLATES_DIR / "profiles"
README_MD = REPO_ROOT / "README.md"
AGENTS_MD = REPO_ROOT / "agents.md"
SEED_COMMANDS = (
	REPO_ROOT / ".claude" / "commands" / "seed-repo.md",
	REPO_ROOT / "workflow-templates" / ".claude" / "commands" / "seed-repo.md",
)
VALID_RELEASE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _workflow_text() -> str:
	return UPDATE_WORKFLOWS_WF.read_text(encoding="utf-8")


def _manifest_lines(name: str) -> list[str]:
	return [
		line.strip()
		for line in (WORKFLOW_PROFILE_DIR / name).read_text(encoding="utf-8").splitlines()
		if line.strip()
	]


def _agents_profile_line(profile: str) -> str:
	return (
		f"PROFILE.name={profile} "
		f"manifest=workflow-templates/profiles/{profile}.txt "
		f"wrappers={','.join(_manifest_lines(f'{profile}.txt'))}"
	)


def test_profile_manifests_match_contracts() -> None:
	core = [
		"ai-clarify.yml",
		"ai-plan.yml",
		"ai-implement.yml",
		"ai-review.yml",
		"ai-issue-pr-status.yml",
		"ai-cancel-on-pr-close.yml",
	]
	standard = core + [
		"ai-orchestrate.yml",
		"ai-orchestrate-poll.yml",
		"ai-orchestrate-clarify-respond.yml",
		"ai-validate.yml",
		"ai-sync-labels.yml",
		"review_rb_judge_dispatch.yml",
	]
	full = sorted(path.name for path in WORKFLOW_TEMPLATES_DIR.glob("*.yml"))

	assert _manifest_lines("core.txt") == core
	assert _manifest_lines("standard.txt") == standard
	assert _manifest_lines("full.txt") == full
	assert "ai-update-workflows.yml" in _manifest_lines("full.txt")
	assert all("/" not in entry for entry in _manifest_lines("full.txt"))


def test_install_profile_docs_and_agents_contracts() -> None:
	readme = README_MD.read_text(encoding="utf-8")
	assert "#### Install profiles" in readme
	assert "`WORKFLOW_PROFILE` repository variable" in readme
	assert "[`workflow-templates/profiles/core.txt`](workflow-templates/profiles/core.txt)" in readme
	assert "[`workflow-templates/profiles/standard.txt`](workflow-templates/profiles/standard.txt)" in readme
	assert "[`workflow-templates/profiles/full.txt`](workflow-templates/profiles/full.txt)" in readme
	assert "Profile downgrades are non-destructive:" in readme

	agents = AGENTS_MD.read_text(encoding="utf-8")
	assert "## Workflow install profiles" in agents
	assert "PROFILE.default=full" in agents
	assert _agents_profile_line("core") in agents
	assert _agents_profile_line("standard") in agents
	assert _agents_profile_line("full") in agents


def test_fail_fast_validation_precedes_any_copy_mutation() -> None:
	wf = _workflow_text()
	assert '[ -n "${UPSTREAM_DIR}" ] || fail_with_reason "ERR_UPSTREAM_DIR_EMPTY"' in wf
	assert '- name: Prepare immutable workflow wrappers' in wf
	assert 'python3 "${RENDERER}"' in wf
	assert 'cp "$upstream_file" "$local_file"' in wf
	assert wf.index('- name: Prepare immutable workflow wrappers') < wf.index('- name: Apply canonical audit-gate assets')
	assert wf.index('[ -n "${UPSTREAM_DIR}" ] || fail_with_reason "ERR_UPSTREAM_DIR_EMPTY"') < wf.index('cp "$upstream_file" "$local_file"')


def test_guardrail_reason_codes_and_outputs_are_declared() -> None:
	wf = _workflow_text()
	assert 'fail_with_reason() {' in wf
	assert "local detail_sanitized=\"${detail//$'\\r'/ }\"" in wf
	assert "detail_sanitized=\"${detail_sanitized//$'\\n'/ }\"" in wf
	assert 'echo "validation_ok=false" >> "$GITHUB_OUTPUT"' in wf
	assert 'ERR_TEMPLATE_COPY_FAILED' in wf
	assert 'cp "$upstream_file" "$local_file" || fail_with_reason "ERR_TEMPLATE_COPY_FAILED"' in wf
	assert 'echo "validation_ok=true" >> "$GITHUB_OUTPUT"' in wf
	assert wf.index('cp "$upstream_file" "$local_file" || fail_with_reason "ERR_TEMPLATE_COPY_FAILED"') < wf.index('echo "validation_ok=true" >> "$GITHUB_OUTPUT"')
	assert 'echo "failure_reason_code=' in wf
	assert 'echo "failure_reason_detail=' in wf
	assert 'ERR_UPSTREAM_DIR_EMPTY' in wf
	assert 'ERR_UPSTREAM_DIR_MISSING' in wf
	assert 'ERR_UPSTREAM_TEMPLATES_EMPTY' in wf
	assert 'ERR_UPSTREAM_SELF_TEMPLATE_MISSING' in wf
	assert 'ERR_LOCAL_WORKFLOW_DIR_MISSING' in wf
	assert 'ERR_LOCAL_WORKFLOW_DIR_NOT_WRITABLE' in wf
	assert 'ERR_PROFILE_MANIFEST_DIR_MISSING' in wf
	assert 'ERR_WORKFLOW_PROFILE_UNKNOWN' in wf
	assert 'ERR_PROFILE_TEMPLATE_MISSING' in wf
	assert 'ERR_WORKFLOW_PROFILE_EMPTY' in wf
	assert 'ERR_LOCAL_TARGET_IS_DIRECTORY' in wf
	assert 'ERR_LOCAL_TARGET_NOT_WRITABLE' in wf
	assert 'if [ "$filename" = "$SELF_TEMPLATE" ] && [ ! -e "$local_file" ]; then' in wf
	directory_guard = 'if [ -e "$local_file" ] && [ -d "$local_file" ]; then'
	writable_guard = 'if [ -e "$local_file" ] && [ ! -w "$local_file" ]; then'
	assert directory_guard in wf
	assert writable_guard in wf
	assert wf.index('if [ "$filename" = "$SELF_TEMPLATE" ] && [ ! -e "$local_file" ]; then') < wf.index(directory_guard)
	assert wf.index(directory_guard) < wf.index(writable_guard)
	assert '- name: Apply canonical audit-gate assets' in wf
	assert 'python3 "${UPSTREAM_DIR}/../scripts/apply_audit_gate_assets.py"' in wf
	assert '--contract-root "${CONTRACT_DIR}"' in wf
	assert '--changed-files-file "${CHANGED_FILES_FILE}"' in wf
	assert "steps.update.outputs.has_updates == 'true' || steps.audit_gate.outputs.status == 'applied'" in wf
	assert 'git add -- "$changed_path"' in wf
	assert 'if git diff --cached --quiet; then' in wf
	assert 'if: steps.update.outputs.has_updates == \'true\' || steps.audit_gate.outputs.status == \'applied\'' in wf
	assert 'Audit-gate assets applied:' in wf
	assert 'SCRIPTS_DIR="scripts"' in wf
	assert 'git sparse-checkout set "${TEMPLATES_DIR}" "${SCRIPTS_DIR}" ".claude"' in wf


def test_profile_selection_and_non_destructive_downgrade_contracts() -> None:
	wf = _workflow_text()
	assert "SELECTED_PROFILE=\"${{ vars.WORKFLOW_PROFILE != '' && vars.WORKFLOW_PROFILE || 'full' }}\"" in wf
	assert 'PROFILE_MANIFEST_DIR="${UPSTREAM_DIR}/profiles"' in wf
	assert 'PROFILE_MANIFEST="${PROFILE_MANIFEST_DIR}/${SELECTED_PROFILE}.txt"' in wf
	assert 'manifest_templates=()' in wf
	assert 'manifest_entry="${manifest_entry%$\'\\r\'}"' in wf
	assert 'manifest_entry="${manifest_entry#"${manifest_entry%%[![:space:]]*}"}"' in wf
	assert 'manifest_entry="${manifest_entry%"${manifest_entry##*[![:space:]]}"}"' in wf
	assert 'done < "${PROFILE_MANIFEST}"' in wf
	assert wf.count('for upstream_file in "${manifest_templates[@]}"; do') == 2
	assert 'rm "$local_file"' not in wf
	assert 'rm -f "$local_file"' not in wf
	assert 'git rm' not in wf
	assert 'find "${LOCAL_DIR}"' not in wf


def test_self_updater_is_refreshed_existing_only_for_every_profile() -> None:
	wf = _workflow_text()
	assert "ai-update-workflows.yml" not in _manifest_lines("core.txt")
	assert "ai-update-workflows.yml" not in _manifest_lines("standard.txt")
	assert "ai-update-workflows.yml" in _manifest_lines("full.txt")
	assert (
		'if [ "${self_template_selected}" != "true" ] && '
		'[ -e "${LOCAL_DIR}/${SELF_TEMPLATE}" ]; then'
	) in wf
	assert 'manifest_templates+=( "${RENDERED_DIR}/${SELF_TEMPLATE}" )' in wf
	assert 'if [ "$filename" = "$SELF_TEMPLATE" ] && [ ! -e "$local_file" ]; then' in wf
	assert wf.index('SKIPPED_FILES="${SKIPPED_FILES}${filename} (self-updater absent, skipped)\\n"') < wf.index(
		'cp "$upstream_file" "$local_file" || fail_with_reason "ERR_TEMPLATE_COPY_FAILED"'
	)


def test_release_payload_is_validated_but_current_stable_wins() -> None:
	wf = _workflow_text()
	assert 'DISPATCH_SHA: ${{ github.event.client_payload.sha || \'\' }}' in wf
	assert 'TEMPLATES_REF="refs/tags/stable"' in wf
	assert 'git fetch --force --no-tags --depth 1 origin "${TEMPLATES_REF}"' in wf
	assert 'UPSTREAM_SHA="$(git rev-parse \'FETCH_HEAD^{commit}\')"' in wf
	assert '[[ ! "${DISPATCH_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]' in wf
	assert '::warning::coding-workflows-stable-released payload is missing a valid 40-character sha;' in wf
	assert '::error::coding-workflows-stable-released payload is missing' not in wf
	assert 'elif [ "${DISPATCH_SHA,,}" != "${UPSTREAM_SHA}" ]; then' in wf
	assert "current stable wins" in wf
	assert '--sha "${UPSTREAM_SHA}"' in wf


def test_failure_summary_contract_is_present() -> None:
	wf = _workflow_text()
	assert '- name: Summary' in wf
	assert 'if: always()' in wf
	assert 'ERR_TEMPLATE_FETCH_FAILED' in wf
	assert 'ERR_UNCATEGORIZED_FAILURE' in wf
	assert 'FAILURE_REASON_FILE="/tmp/update_workflows_failure_reason.txt"' in wf
	assert "printf '%s\\n%s\\n' \"${code}\" \"${detail_sanitized}\" > \"${FAILURE_REASON_FILE}\"" in wf
	assert 'IFS= read -r FAILURE_REASON_CODE < "${FAILURE_REASON_FILE}"' in wf
	assert "FAILURE_REASON_DETAIL=\"$(sed -n '2p' " in wf
	assert '"${FAILURE_REASON_FILE}" 2>/dev/null || true)"' in wf
	assert 'Failure reason code:' in wf
	assert 'Failure reason detail:' in wf
	assert 'Failure reason artifact:' in wf


def test_success_path_contracts_are_preserved() -> None:
	wf = _workflow_text()
	assert "if: ${{ inputs.allow_workflow_edits != false }}" in wf
	assert 'SELF_TEMPLATE="ai-update-workflows.yml"' in wf
	assert 'SKIPPED_FILES="${SKIPPED_FILES}${filename} (self-updater absent, skipped)\\n"' in wf
	assert '[ -e "${LOCAL_DIR}/${SELF_TEMPLATE}" ]' in wf
	assert 'manifest_templates+=( "${RENDERED_DIR}/${SELF_TEMPLATE}" )' in wf
	assert 'SKIPPED_LIST=$(cat /tmp/skipped_files.txt)' in wf
	assert "printf '%b' \"$UPDATED_FILES\" > /tmp/updated_files.txt" in wf
	assert "printf '%b' \"$CREATED_FILES\" > /tmp/created_files.txt" in wf
	assert "printf '%b' \"$SKIPPED_FILES\" > /tmp/skipped_files.txt" in wf
	assert '**Skipped files:**' in wf
	assert '**Audit gate files:**' in wf
	assert '- **Audit gate status:** ${AUDIT_GATE_STATUS:-unknown}' in wf
	assert '- **Audit gate package script action:** ${AUDIT_GATE_SCRIPT_ACTION:-unknown}' in wf
	assert '- **Audit gate changed files:** ${AUDIT_GATE_CHANGED_COUNT:-0}' in wf
	assert "if: steps.update.outputs.has_updates == 'true'" in wf
	assert "ALLOW_WORKFLOW_EDITS repository variable to '\\''false'\\''." in wf


def test_wrapper_ref_renderer_contract() -> None:
	template_text = """jobs:
  first:
    uses: shubhodeep1/coding-workflows/.github/workflows/clarify.yml@stable
  second:
    uses: shubhodeep1/coding-workflows/.github/workflows/plan.yml@stable # old marker
  third:
    uses: actions/checkout@stable
# shubhodeep1/coding-workflows/.github/workflows/comment.yml@stable
"""
	rendered_text = pin_reusable_workflow_refs(template_text, VALID_RELEASE_SHA.upper())
	expected_suffix = f"@{VALID_RELEASE_SHA} # stable"
	assert rendered_text.count(expected_suffix) == 2
	assert "uses: actions/checkout@stable" in rendered_text
	assert "# shubhodeep1/coding-workflows/.github/workflows/comment.yml@stable" in rendered_text
	assert validate_release_sha(VALID_RELEASE_SHA.upper()) == VALID_RELEASE_SHA

	for invalid_sha in ("", "a" * 39, "g" * 40, "a" * 41):
		try:
			validate_release_sha(invalid_sha)
		except ValueError:
			pass
		else:
			raise AssertionError(f"invalid SHA was accepted: {invalid_sha!r}")

	try:
		pin_reusable_workflow_refs("uses: actions/checkout@v5\n", VALID_RELEASE_SHA)
	except ValueError as exc:
		assert "no coding-workflows" in str(exc)
	else:
		raise AssertionError("template without a reusable-workflow ref was accepted")


def test_every_wrapper_template_renders_to_an_immutable_ref() -> None:
	templates = sorted(WORKFLOW_TEMPLATES_DIR.glob("*.yml"))
	assert len(templates) == 17
	for template_path in templates:
		rendered_text = pin_reusable_workflow_refs(
			template_path.read_text(encoding="utf-8"),
			VALID_RELEASE_SHA,
		)
		assert "shubhodeep1/coding-workflows/.github/workflows/" in rendered_text
		assert ".yml@stable" not in rendered_text
		assert f"@{VALID_RELEASE_SHA} # stable" in rendered_text


def test_seed_commands_require_immutable_wrapper_rendering() -> None:
	for command_path in SEED_COMMANDS[1:]:  # the twin; .claude/ follows via the sync PR (CLAUDE.md §28.C)
		command_text = command_path.read_text(encoding="utf-8")
		assert "scripts/workflow_wrapper_refs.py" in command_text
		assert "40-character" in command_text
		assert "# stable" in command_text
		assert "git fetch --force --no-tags origin refs/tags/stable" in command_text
		assert "--depth=1" not in command_text
		assert "git rev-parse 'FETCH_HEAD^{commit}'" in command_text
		assert "origin/stable" not in command_text
		assert "ref=<UPSTREAM_SHA>" in command_text
		assert "refreshes an existing copy to the current release pin" in command_text


def _claude_sync_step_script() -> str:
	workflow = yaml.safe_load(_workflow_text())
	for job in workflow["jobs"].values():
		for step in job.get("steps", []):
			if step.get("id") == "claude_sync":
				return step["run"]
	raise AssertionError("claude_sync step not found")


def test_guard_pattern_matches_claude_twin_sync_guard_paths() -> None:
	"""The shell guard pattern is the same set as claude_twin_sync.py's (issue #5607)."""
	script = _claude_sync_step_script()
	pattern = "|".join([f"{prefix}*" for prefix in GUARD_PATH_PREFIXES] + sorted(GUARD_PATH_FILES))
	assert f"{pattern})" in script, pattern
	# The literal alone does not prove the same set: run the pattern through a
	# shell `case` (where `*` also matches `/`) and compare with is_guard_path.
	samples = [
		"hooks/a.py",
		"hooks/lib/b.py",
		"hooks/a/b/c.sh",
		"settings.json",
		"settings.local.json",
		"commands/settings.json",
		"hooksx/a.py",
		"scripts/hooks/a.py",
		"commands/foo.md",
	]
	case_script = f'for rel in "$@"; do case "$rel" in {pattern}) echo "$rel";; esac; done'
	for shell in ("bash", "sh"):
		result = subprocess.run([shell, "-c", case_script, shell, *samples], capture_output=True, text=True, check=True)
		assert result.stdout.split() == [rel for rel in samples if is_guard_path(rel)], shell


def _run_claude_sync(
	twins: dict[str, str],
	reviewed: dict[str, str],
	local: dict[str, str],
	extra_path: str | None = None,
	twin_symlinks: dict[str, str] | None = None,
	local_symlinks: dict[str, str] | None = None,
	reviewed_symlinks: dict[str, str] | None = None,
	before_run: Callable[[Path], None] | None = None,
	after_run: Callable[[Path], None] | None = None,
) -> tuple[dict[str, str], str, dict[str, str]]:
	"""Run the real claude_sync step body against a fake stable checkout.

	``extra_path`` is prepended to PATH (for stub commands). ``twin_symlinks``,
	``local_symlinks``, and ``reviewed_symlinks`` map a twin, consumer, or
	stable .claude/ path to the symlink target written there. ``before_run``
	and ``after_run`` get the consumer checkout root, before the step runs
	(after the trees above are written) and after it, so a test can lay out
	and inspect paths outside the consumer .claude/ tree. Returns the consumer .claude/ tree afterwards (a symlink as
	``symlink:<target>``), the step's stdout, and its outputs.
	"""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		upstream = root / "upstream"
		consumer = root / "consumer"
		# The sparse checkout always has workflow-templates/, even when it
		# carries no .claude/ twin tree.
		(upstream / "workflow-templates").mkdir(parents=True)
		for base, links in (
			(upstream / "workflow-templates" / ".claude", twin_symlinks),
			(consumer / ".claude", local_symlinks),
			(upstream / ".claude", reviewed_symlinks),
		):
			for rel, target in (links or {}).items():
				link = base / rel
				link.parent.mkdir(parents=True, exist_ok=True)
				link.symlink_to(target)
		for base, files in ((upstream / "workflow-templates" / ".claude", twins), (upstream / ".claude", reviewed), (consumer / ".claude", local)):
			for rel, body in files.items():
				path = base / rel
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_text(body, encoding="utf-8")
		changed_list = root / "changed.txt"
		output_file = root / "github_output"
		output_file.write_text("", encoding="utf-8")
		script = _claude_sync_step_script()
		script = script.replace("${{ steps.fetch.outputs.upstream_dir }}", str(upstream / "workflow-templates"))
		script = script.replace("/tmp/claude_changed_files.txt", str(changed_list))
		assert "${{" not in script
		if before_run:
			before_run(consumer)
		run_env = {**os.environ, "GITHUB_OUTPUT": str(output_file)}
		if extra_path:
			run_env["PATH"] = f"{extra_path}{os.pathsep}{run_env.get('PATH', '')}"
		result = subprocess.run(
			["bash", "-c", script],
			cwd=consumer if consumer.exists() else root,
			env=run_env,
			capture_output=True,
			text=True,
			check=False,
		)
		assert result.returncode == 0, result.stderr
		if after_run:
			after_run(consumer)
		outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
		consumer_claude = (consumer if consumer.exists() else root) / ".claude"
		tree = {
			str(path.relative_to(consumer_claude)): (
				f"symlink:{os.readlink(path)}" if path.is_symlink() else path.read_text(encoding="utf-8")
			)
			for path in sorted(consumer_claude.rglob("*"))
			if path.is_symlink() or path.is_file()
		}
		return tree, result.stdout, outputs


def test_claude_sync_takes_guard_files_from_the_reviewed_claude_tree() -> None:
	"""Issue #5607: a guard twin that differs from .claude/ never reaches a consumer."""
	twins = {
		"commands/foo.md": "twin command\n",
		"hooks/same.py": "same hook\n",
		"hooks/pending.py": "UNREVIEWED twin\n",
		"hooks/new_only.py": "UNREVIEWED new hook\n",
		"hooks/pending_absent.py": "UNREVIEWED twin 2\n",
		"hooks/lib/nested_pending.py": "UNREVIEWED nested twin\n",
		"hooks/lib/nested_new.py": "UNREVIEWED nested new hook\n",
		"settings.json": "{\"twin\": true}\n",
	}
	reviewed = {
		"commands/foo.md": "older reviewed command\n",
		"hooks/same.py": "same hook\n",
		"hooks/pending.py": "reviewed hook\n",
		"hooks/pending_absent.py": "reviewed hook 2\n",
		"hooks/lib/nested_pending.py": "reviewed nested hook\n",
		"settings.json": "{\"reviewed\": true}\n",
	}
	local = {
		"hooks/pending.py": "consumer hook\n",
		"hooks/lib/nested_pending.py": "consumer nested hook\n",
		"settings.json": "{\"consumer\": true}\n",
		"skills/private.md": "consumer-local\n",
	}
	tree, stdout, outputs = _run_claude_sync(twins, reviewed, local)
	# Non-guard files still come from the twin.
	assert tree["commands/foo.md"] == "twin command\n"
	# Guard equal on both sides: synced.
	assert tree["hooks/same.py"] == "same hook\n"
	# Guard twin differs and the consumer has the file: kept.
	assert tree["hooks/pending.py"] == "consumer hook\n"
	assert tree["settings.json"] == "{\"consumer\": true}\n"
	# Guard twin differs and the consumer lacks the file: the reviewed copy.
	assert tree["hooks/pending_absent.py"] == "reviewed hook 2\n"
	# Guard missing from .claude/: nothing installed.
	assert "hooks/new_only.py" not in tree
	# Nested hooks are guards too.
	assert tree["hooks/lib/nested_pending.py"] == "consumer nested hook\n"
	assert "hooks/lib/nested_new.py" not in tree
	assert "::warning::claude-guard-sync: .claude/hooks/lib/nested_pending.py differs from its workflow-templates twin" in stdout
	assert "::warning::claude-guard-sync: .claude/hooks/lib/nested_new.py is not in the stable .claude/ tree" in stdout
	# Consumer-local extras stay.
	assert tree["skills/private.md"] == "consumer-local\n"
	assert "UNREVIEWED" not in "".join(tree.values())
	assert "::warning::claude-guard-sync: .claude/hooks/new_only.py is not in the stable .claude/ tree" in stdout
	assert "::warning::claude-guard-sync: .claude/hooks/pending.py differs from its workflow-templates twin" in stdout
	assert "::warning::claude-guard-sync: .claude/settings.json differs from its workflow-templates twin" in stdout
	assert outputs["claude_changed"] == "3"
	assert outputs["claude_has_changes"] == "true"


def test_claude_sync_with_matching_guards_behaves_as_before() -> None:
	"""With every guard twin equal to .claude/, the sync mirrors the twin tree."""
	twins = {"commands/foo.md": "a\n", "hooks/h.py": "h\n", "settings.json": "{}\n"}
	tree, stdout, outputs = _run_claude_sync(twins, dict(twins), {"hooks/h.py": "old\n"})
	assert tree == twins
	assert "::warning::" not in stdout
	assert outputs["claude_changed"] == "3"


def test_claude_sync_guard_compare_error_is_not_reported_as_a_difference() -> None:
	"""A `cmp` read error (exit 2) is its own warning, not "differs", and changes nothing."""
	real_cmp = shutil.which("cmp")
	assert real_cmp, "cmp not found"
	twins = {"hooks/unreadable.py": "twin\n", "hooks/unreadable_absent.py": "twin 2\n", "hooks/ok.py": "ok\n"}
	reviewed = {"hooks/unreadable.py": "reviewed\n", "hooks/unreadable_absent.py": "reviewed 2\n", "hooks/ok.py": "ok\n"}
	local = {"hooks/unreadable.py": "consumer\n"}
	with tempfile.TemporaryDirectory() as stub_dir:
		stub = Path(stub_dir) / "cmp"
		stub.write_text(
			f'#!/bin/sh\ncase "$*" in *unreadable*) echo "cmp: read error" >&2; exit 2;; esac\nexec "{real_cmp}" "$@"\n',
			encoding="utf-8",
		)
		stub.chmod(0o755)
		tree, stdout, outputs = _run_claude_sync(twins, reviewed, local, extra_path=stub_dir)
	assert tree["hooks/unreadable.py"] == "consumer\n"
	assert "hooks/unreadable_absent.py" not in tree
	assert tree["hooks/ok.py"] == "ok\n"
	for rel in ("hooks/unreadable.py", "hooks/unreadable_absent.py"):
		assert f"::warning::claude-guard-sync: could not compare .claude/{rel} with its workflow-templates twin (cmp exit 2); nothing changed." in stdout
		assert f".claude/{rel} differs from its workflow-templates twin" not in stdout
	assert outputs["claude_changed"] == "1"


def test_claude_sync_installs_reviewed_guard_whose_twin_is_missing() -> None:
	"""A guard in .claude/ with no twin counts as a differing twin (PR #5654 review)."""
	twins = {"commands/foo.md": "twin command\n", "hooks/kept.py": "kept\n"}
	reviewed = {
		"commands/foo.md": "reviewed command\n",
		"commands/only_reviewed.md": "not a guard\n",
		"hooks/kept.py": "kept\n",
		"hooks/twin_deleted.py": "reviewed hook\n",
		"hooks/lib/twin_deleted_nested.py": "reviewed nested hook\n",
		"hooks/twin_deleted_local.py": "reviewed hook 3\n",
		"settings.local.json": "{\"reviewed\": true}\n",
	}
	local = {"hooks/twin_deleted_local.py": "consumer hook\n"}
	tree, stdout, outputs = _run_claude_sync(twins, reviewed, local)
	# The consumer lacks it: the reviewed copy is installed, with a warning.
	assert tree["hooks/twin_deleted.py"] == "reviewed hook\n"
	assert tree["hooks/lib/twin_deleted_nested.py"] == "reviewed nested hook\n"
	assert tree["settings.local.json"] == "{\"reviewed\": true}\n"
	for rel in ("hooks/twin_deleted.py", "hooks/lib/twin_deleted_nested.py", "settings.local.json"):
		assert f"::warning::claude-guard-sync: .claude/{rel} has no regular-file workflow-templates twin on stable (owner sync pending, or the twin is a symlink); installed the .claude/ copy." in stdout
	# The consumer has it: kept, with a warning.
	assert tree["hooks/twin_deleted_local.py"] == "consumer hook\n"
	assert "::warning::claude-guard-sync: .claude/hooks/twin_deleted_local.py has no regular-file workflow-templates twin on stable (owner sync pending, or the twin is a symlink); kept the existing file." in stdout
	# Non-guard .claude/ files still come from the twin tree only.
	assert tree["commands/foo.md"] == "twin command\n"
	assert "commands/only_reviewed.md" not in tree
	# A guard with a twin is handled once, by the twin loop.
	assert "hooks/kept.py has no regular-file workflow-templates twin" not in stdout
	assert outputs["claude_changed"] == "5"
	changed = [line for line in stdout.splitlines() if "installed the .claude/ copy" in line]
	assert len(changed) == 3


def test_claude_sync_checks_reviewed_guards_without_a_twin_tree() -> None:
	"""No workflow-templates/.claude/ at the stable commit still runs the reviewed-guard pass (PR #5654 review)."""
	reviewed = {
		"commands/foo.md": "not a guard\n",
		"hooks/only_reviewed.py": "reviewed hook\n",
		"hooks/only_reviewed_local.py": "reviewed hook 2\n",
		"settings.json": "{\"reviewed\": true}\n",
	}
	local = {"hooks/only_reviewed_local.py": "consumer hook\n"}
	tree, stdout, outputs = _run_claude_sync({}, reviewed, local)
	assert "Upstream has no workflow-templates/.claude/ at this ref" in stdout
	assert tree["hooks/only_reviewed.py"] == "reviewed hook\n"
	assert tree["settings.json"] == "{\"reviewed\": true}\n"
	assert tree["hooks/only_reviewed_local.py"] == "consumer hook\n"
	assert "commands/foo.md" not in tree
	assert "::warning::claude-guard-sync: .claude/hooks/only_reviewed_local.py has no regular-file workflow-templates twin on stable (owner sync pending, or the twin is a symlink); kept the existing file." in stdout
	assert outputs["claude_changed"] == "2"
	assert outputs["claude_has_changes"] == "true"


def test_claude_sync_without_any_claude_tree_changes_nothing() -> None:
	"""Neither tree at the stable commit: no file is written and the outputs stay zero."""
	tree, stdout, outputs = _run_claude_sync({}, {}, {"skills/private.md": "consumer-local\n"})
	assert tree == {"skills/private.md": "consumer-local\n"}
	assert "::warning::" not in stdout
	assert outputs["claude_changed"] == "0"
	assert outputs["claude_has_changes"] == "false"


def test_claude_sync_symlink_guard_twin_is_handled_by_the_reviewed_pass() -> None:
	"""`find -type f` never lists a symlink twin, so the reviewed pass handles it once, and its warning names the symlink case."""
	twins = {"hooks/real.py": "reviewed hook\n"}
	reviewed = {"hooks/real.py": "reviewed hook\n", "hooks/linked.py": "reviewed linked hook\n"}
	tree, stdout, outputs = _run_claude_sync(twins, reviewed, {}, twin_symlinks={"hooks/linked.py": "real.py"})
	assert tree["hooks/linked.py"] == "reviewed linked hook\n"
	assert tree["hooks/real.py"] == "reviewed hook\n"
	assert stdout.count(".claude/hooks/linked.py") == 1
	assert "::warning::claude-guard-sync: .claude/hooks/linked.py has no regular-file workflow-templates twin on stable (owner sync pending, or the twin is a symlink); installed the .claude/ copy." in stdout
	assert outputs["claude_changed"] == "2"


def test_claude_sync_keeps_a_dangling_consumer_symlink_at_a_differing_guard() -> None:
	"""A dangling consumer symlink is a file the consumer has: kept, never copied through (PR #5654 review)."""
	twins = {"hooks/pending.py": "UNREVIEWED twin\n", "settings.json": "{\"twin\": true}\n", "commands/foo.md": "twin command\n"}
	reviewed = {"hooks/pending.py": "reviewed hook\n", "settings.json": "{\"reviewed\": true}\n", "hooks/twin_deleted.py": "reviewed hook 2\n"}
	local_symlinks = {
		"hooks/pending.py": "missing_target.py",
		"settings.json": "missing_settings.json",
		"hooks/twin_deleted.py": "missing_target_2.py",
	}
	tree, stdout, outputs = _run_claude_sync(twins, reviewed, {}, local_symlinks=local_symlinks)
	assert tree["hooks/pending.py"] == "symlink:missing_target.py"
	assert tree["settings.json"] == "symlink:missing_settings.json"
	assert tree["hooks/twin_deleted.py"] == "symlink:missing_target_2.py"
	assert tree["commands/foo.md"] == "twin command\n"
	assert "UNREVIEWED" not in "".join(tree.values())
	for rel in ("hooks/pending.py", "settings.json"):
		assert f"::warning::claude-guard-sync: .claude/{rel} differs from its workflow-templates twin on stable (owner sync pending); kept the existing file." in stdout
	assert "::warning::claude-guard-sync: .claude/hooks/twin_deleted.py has no regular-file workflow-templates twin on stable (owner sync pending, or the twin is a symlink); kept the existing file." in stdout
	assert outputs["claude_changed"] == "1"


def test_claude_sync_installs_a_symlinked_reviewed_guard_without_a_regular_twin() -> None:
	"""A symlink in the stable .claude/ tree is a guard the reviewed pass must see; one that is not a regular file is skipped with a warning (PR #5654 review)."""
	twins = {"hooks/real.py": "reviewed hook\n"}
	reviewed = {"hooks/real.py": "reviewed hook\n", "hooks/lib/target.py": "reviewed lib\n"}
	reviewed_symlinks = {
		"hooks/linked.py": "real.py",
		"hooks/both_linked.py": "real.py",
		"hooks/dangling.py": "missing_target.py",
		"hooks/dir_link": "lib",
	}
	tree, stdout, outputs = _run_claude_sync(
		twins,
		reviewed,
		{},
		twin_symlinks={"hooks/both_linked.py": "real.py"},
		reviewed_symlinks=reviewed_symlinks,
	)
	# The consumer gets the dereferenced content as a regular file.
	assert tree["hooks/linked.py"] == "reviewed hook\n"
	assert tree["hooks/both_linked.py"] == "reviewed hook\n"
	for rel in ("hooks/linked.py", "hooks/both_linked.py"):
		assert f"::warning::claude-guard-sync: .claude/{rel} has no regular-file workflow-templates twin on stable (owner sync pending, or the twin is a symlink); installed the .claude/ copy." in stdout
	# A dangling link or a link to a directory installs nothing and never aborts the step.
	assert "hooks/dangling.py" not in tree
	assert "hooks/dir_link" not in tree
	assert not any(rel.startswith("hooks/dir_link/") for rel in tree)
	for rel in ("hooks/dangling.py", "hooks/dir_link"):
		assert f"::warning::claude-guard-sync: .claude/{rel} in the stable .claude/ tree is a symlink that does not resolve to a regular file; nothing installed." in stdout
	# hooks/real.py (twin loop) and hooks/lib/target.py (no twin) as before.
	assert tree["hooks/real.py"] == "reviewed hook\n"
	assert tree["hooks/lib/target.py"] == "reviewed lib\n"
	assert outputs["claude_changed"] == "4"


def test_claude_sync_skips_a_reviewed_guard_that_resolves_outside_the_reviewed_tree() -> None:
	"""A guard in the stable .claude/ tree that resolves into its twin or out of the tree is not reviewed content: nothing installed (PR #5654 review)."""
	outside_file = str(Path(__file__).resolve())
	twins = {
		"hooks/real.py": "reviewed hook\n",
		"hooks/twin_link.py": "UNREVIEWED twin\n",
		"hooks/twin_link_local.py": "UNREVIEWED twin 2\n",
		"hooks/twin_dir/x.py": "UNREVIEWED dir twin\n",
		"commands/foo.md": "UNREVIEWED command twin\n",
	}
	reviewed = {"hooks/real.py": "reviewed hook\n"}
	reviewed_symlinks = {
		# First pass: the twin exists, so `cmp` sees the twin on both sides.
		"hooks/twin_link.py": "../../workflow-templates/.claude/hooks/twin_link.py",
		"hooks/twin_link_local.py": "../../workflow-templates/.claude/hooks/twin_link_local.py",
		# A directory link: the file path itself is not a symlink.
		"hooks/twin_dir": "../../workflow-templates/.claude/hooks/twin_dir",
		# Second pass: no twin at this path.
		"hooks/no_twin_link.py": "../../workflow-templates/.claude/commands/foo.md",
		"hooks/abs_outside.py": outside_file,
		# In-tree link: still installed.
		"hooks/linked.py": "real.py",
	}
	local = {"hooks/twin_link_local.py": "consumer hook\n"}
	tree, stdout, outputs = _run_claude_sync(twins, reviewed, local, reviewed_symlinks=reviewed_symlinks)
	assert "hooks/twin_link.py" not in tree
	assert tree["hooks/twin_link_local.py"] == "consumer hook\n"
	assert "hooks/twin_dir/x.py" not in tree
	assert "hooks/no_twin_link.py" not in tree
	assert "hooks/abs_outside.py" not in tree
	assert "UNREVIEWED" not in "".join(value for rel, value in tree.items() if rel != "commands/foo.md")
	for rel in (
		"hooks/twin_link.py",
		"hooks/twin_link_local.py",
		"hooks/twin_dir/x.py",
		"hooks/no_twin_link.py",
		"hooks/abs_outside.py",
	):
		assert f"::warning::claude-guard-sync: .claude/{rel} in the stable .claude/ tree resolves outside its guard paths (a symlink to its twin, to a non-guard file, or out of the tree); nothing installed." in stdout
	# In-tree guards and non-guard twins sync as before.
	assert tree["hooks/linked.py"] == "reviewed hook\n"
	assert tree["hooks/real.py"] == "reviewed hook\n"
	assert tree["commands/foo.md"] == "UNREVIEWED command twin\n"
	assert outputs["claude_changed"] == "3"


def test_claude_sync_skips_a_reviewed_guard_that_resolves_to_a_non_guard_file() -> None:
	"""A guard linked to a non-guard file in .claude/ (e.g. scripts/, which the twin sync merges without the owner) is not reviewed guard content (PR #5654 review round 5)."""
	twins = {
		"hooks/real.py": "reviewed hook\n",
		"hooks/to_script.py": "UNREVIEWED twin\n",
		"hooks/scripts_dir/s.py": "UNREVIEWED dir twin\n",
		"scripts/s.py": "twin script\n",
	}
	reviewed = {"hooks/real.py": "reviewed hook\n", "scripts/s.py": "UNREVIEWED script\n", "commands/c.md": "cmd\n"}
	reviewed_symlinks = {
		# First pass: has a twin.
		"hooks/to_script.py": "../scripts/s.py",
		# Second pass: no twin.
		"hooks/no_twin_to_command.py": "../commands/c.md",
		# A directory link onto a non-guard directory (first pass, via its twin).
		"hooks/scripts_dir": "../scripts",
		# Guard to guard: still installed.
		"settings.local.json": "hooks/real.py",
	}
	tree, stdout, outputs = _run_claude_sync(twins, reviewed, {}, reviewed_symlinks=reviewed_symlinks)
	for rel in ("hooks/to_script.py", "hooks/no_twin_to_command.py", "hooks/scripts_dir/s.py"):
		assert rel not in tree
		assert f"::warning::claude-guard-sync: .claude/{rel} in the stable .claude/ tree resolves outside its guard paths (a symlink to its twin, to a non-guard file, or out of the tree); nothing installed." in stdout
	assert "UNREVIEWED" not in "".join(tree.values())
	assert tree["settings.local.json"] == "reviewed hook\n"
	assert tree["hooks/real.py"] == "reviewed hook\n"
	assert tree["scripts/s.py"] == "twin script\n"


def test_claude_sync_installs_a_reviewed_guard_whose_twin_sits_under_a_symlinked_directory() -> None:
	"""`find` never descends a symlinked twin directory, so the reviewed pass must still install the guard (PR #5654 review round 5)."""
	twins = {"lib/x.py": "UNREVIEWED twin\n"}
	reviewed = {"hooks/lib/x.py": "reviewed hook\n", "hooks/lib/y.py": "reviewed hook 2\n"}
	local = {"hooks/lib/y.py": "consumer hook\n"}
	tree, stdout, outputs = _run_claude_sync(twins, reviewed, local, twin_symlinks={"hooks/lib": "../lib"})
	assert tree["hooks/lib/x.py"] == "reviewed hook\n"
	assert tree["hooks/lib/y.py"] == "consumer hook\n"
	assert "::warning::claude-guard-sync: .claude/hooks/lib/x.py has no regular-file workflow-templates twin on stable (owner sync pending, or the twin is a symlink); installed the .claude/ copy." in stdout
	assert "::warning::claude-guard-sync: .claude/hooks/lib/y.py has no regular-file workflow-templates twin on stable (owner sync pending, or the twin is a symlink); kept the existing file." in stdout
	assert "UNREVIEWED" not in "".join(value for rel, value in tree.items() if rel.startswith("hooks/"))


def test_claude_sync_keeps_a_dangling_consumer_symlink_at_an_equal_guard() -> None:
	"""An equal guard still never writes through a dangling consumer symlink, which would abort the step (PR #5654 review round 5)."""
	twins = {"hooks/same.py": "same hook\n", "settings.json": "{}\n", "hooks/other.py": "other\n"}
	local_symlinks = {"hooks/same.py": "missing_target.py", "settings.json": "missing_settings.json"}
	tree, stdout, outputs = _run_claude_sync(twins, dict(twins), {}, local_symlinks=local_symlinks)
	assert tree["hooks/same.py"] == "symlink:missing_target.py"
	assert tree["settings.json"] == "symlink:missing_settings.json"
	assert tree["hooks/other.py"] == "other\n"
	for rel in ("hooks/same.py", "settings.json"):
		assert f"::warning::claude-guard-sync: .claude/{rel} is a dangling symlink in this repository; kept the existing file." in stdout
	assert outputs["claude_changed"] == "1"


def test_claude_sync_reports_a_realpath_failure_on_its_own() -> None:
	"""A failing realpath is its own warning, not "resolves outside", and installs nothing (PR #5654 review round 5)."""
	twins = {"hooks/h.py": "h\n"}
	reviewed = {"hooks/h.py": "h\n", "hooks/no_twin.py": "n\n"}
	with tempfile.TemporaryDirectory() as stub_dir:
		stub = Path(stub_dir) / "realpath"
		stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
		stub.chmod(0o755)
		tree, stdout, outputs = _run_claude_sync(twins, reviewed, {}, extra_path=stub_dir)
	assert tree == {}
	for rel in ("hooks/h.py", "hooks/no_twin.py"):
		assert f"::warning::claude-guard-sync: could not resolve .claude/{rel} in the stable .claude/ tree (realpath failed); nothing installed." in stdout
		assert f".claude/{rel} in the stable .claude/ tree resolves outside" not in stdout
	assert outputs["claude_changed"] == "0"


def test_claude_sync_never_writes_a_guard_through_a_consumer_symlink() -> None:
	"""A guard destination must stay in the consumer's own .claude/ tree: never written through a symlink on the file or a directory on the way (PR #5651 review round 1)."""
	twins = {
		"hooks/same.py": "same hook\n",
		"hooks/lib/nested.py": "nested hook\n",
		"settings.json": "{\"reviewed\": true}\n",
		"settings.local.json": "{\"local\": true}\n",
		"hooks/plain.py": "plain hook\n",
		"commands/foo.md": "twin command\n",
	}
	reviewed = {**twins, "hooks/lib/no_twin.py": "reviewed no-twin hook\n"}
	del reviewed["commands/foo.md"]
	local_symlinks = {
		# A directory on the way to two guards (the twin loop and the reviewed pass).
		"hooks/lib": "../../outside_lib",
		# A live symlink at the guard file itself, pointing out of .claude/.
		"settings.json": "../outside_workflows/ci.yml",
		# A live symlink at the guard file, pointing inside .claude/.
		"settings.local.json": "settings.inside.json",
	}
	local = {"settings.inside.json": "consumer inside\n"}

	def before_run(consumer: Path) -> None:
		(consumer / "outside_lib").mkdir()
		(consumer / "outside_workflows").mkdir()
		(consumer / "outside_workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")

	def after_run(consumer: Path) -> None:
		assert sorted(path.name for path in (consumer / "outside_lib").iterdir()) == []
		assert (consumer / "outside_workflows" / "ci.yml").read_text(encoding="utf-8") == "name: ci\n"

	tree, stdout, outputs = _run_claude_sync(
		twins,
		reviewed,
		local,
		local_symlinks=local_symlinks,
		before_run=before_run,
		after_run=after_run,
	)
	assert tree["settings.json"] == "symlink:../outside_workflows/ci.yml"
	assert tree["settings.local.json"] == "symlink:settings.inside.json"
	assert tree["settings.inside.json"] == "consumer inside\n"
	for rel in ("settings.json", "settings.local.json"):
		assert f"::warning::claude-guard-sync: .claude/{rel} is a symlink in this repository and a guard is never written through one; kept the existing file." in stdout
	for rel in ("hooks/lib/nested.py", "hooks/lib/no_twin.py"):
		assert f"::warning::claude-guard-sync: .claude/{rel} sits under a symlink or a non-directory in this repository, which could carry the write out of .claude/; nothing installed." in stdout
	# Plain guards and non-guard files sync as before.
	assert tree["hooks/same.py"] == "same hook\n"
	assert tree["hooks/plain.py"] == "plain hook\n"
	assert tree["commands/foo.md"] == "twin command\n"
	assert outputs["claude_changed"] == "3"


def test_claude_sync_never_writes_a_guard_under_a_symlinked_consumer_claude_dir() -> None:
	"""A consumer whose .claude itself is a symlink gets no guard written through it (PR #5651 review round 1)."""
	twins = {"hooks/h.py": "h\n", "settings.json": "{}\n"}
	reviewed = {**twins, "hooks/no_twin.py": "n\n"}

	def before_run(consumer: Path) -> None:
		(consumer / ".claude").rename(consumer / "elsewhere")
		(consumer / ".claude").symlink_to("elsewhere")

	def after_run(consumer: Path) -> None:
		assert sorted(str(path.relative_to(consumer / "elsewhere")) for path in (consumer / "elsewhere").rglob("*")) == ["skills", "skills/private.md"]

	tree, stdout, outputs = _run_claude_sync(twins, reviewed, {"skills/private.md": "consumer-local\n"}, before_run=before_run, after_run=after_run)
	assert tree == {"skills/private.md": "consumer-local\n"}
	for rel in ("hooks/h.py", "settings.json", "hooks/no_twin.py"):
		assert f"::warning::claude-guard-sync: .claude/{rel} sits under a symlink or a non-directory in this repository, which could carry the write out of .claude/; nothing installed." in stdout
	assert outputs["claude_changed"] == "0"


def test_claude_sync_skips_a_guard_under_a_consumer_path_that_is_not_a_directory() -> None:
	"""A file where a guard's directory should be is kept, and never aborts the step in `mkdir -p` (PR #5651 review round 1)."""
	twins = {"hooks/lib/x.py": "x\n", "hooks/ok.py": "ok\n"}
	reviewed = {**twins, "hooks/lib/no_twin.py": "n\n"}
	tree, stdout, outputs = _run_claude_sync(twins, reviewed, {"hooks/lib": "consumer file\n"})
	assert tree["hooks/lib"] == "consumer file\n"
	assert tree["hooks/ok.py"] == "ok\n"
	for rel in ("hooks/lib/x.py", "hooks/lib/no_twin.py"):
		assert f"::warning::claude-guard-sync: .claude/{rel} sits under a symlink or a non-directory in this repository, which could carry the write out of .claude/; nothing installed." in stdout
	assert outputs["claude_changed"] == "1"


def main() -> int:
	test_profile_manifests_match_contracts()
	test_install_profile_docs_and_agents_contracts()
	test_fail_fast_validation_precedes_any_copy_mutation()
	test_guardrail_reason_codes_and_outputs_are_declared()
	test_profile_selection_and_non_destructive_downgrade_contracts()
	test_self_updater_is_refreshed_existing_only_for_every_profile()
	test_release_payload_is_validated_but_current_stable_wins()
	test_failure_summary_contract_is_present()
	test_success_path_contracts_are_preserved()
	test_wrapper_ref_renderer_contract()
	test_every_wrapper_template_renders_to_an_immutable_ref()
	test_seed_commands_require_immutable_wrapper_rendering()
	test_guard_pattern_matches_claude_twin_sync_guard_paths()
	test_claude_sync_takes_guard_files_from_the_reviewed_claude_tree()
	test_claude_sync_with_matching_guards_behaves_as_before()
	test_claude_sync_guard_compare_error_is_not_reported_as_a_difference()
	test_claude_sync_installs_reviewed_guard_whose_twin_is_missing()
	test_claude_sync_checks_reviewed_guards_without_a_twin_tree()
	test_claude_sync_without_any_claude_tree_changes_nothing()
	test_claude_sync_symlink_guard_twin_is_handled_by_the_reviewed_pass()
	test_claude_sync_keeps_a_dangling_consumer_symlink_at_a_differing_guard()
	test_claude_sync_installs_a_symlinked_reviewed_guard_without_a_regular_twin()
	test_claude_sync_skips_a_reviewed_guard_that_resolves_outside_the_reviewed_tree()
	test_claude_sync_skips_a_reviewed_guard_that_resolves_to_a_non_guard_file()
	test_claude_sync_installs_a_reviewed_guard_whose_twin_sits_under_a_symlinked_directory()
	test_claude_sync_keeps_a_dangling_consumer_symlink_at_an_equal_guard()
	test_claude_sync_reports_a_realpath_failure_on_its_own()
	test_claude_sync_never_writes_a_guard_through_a_consumer_symlink()
	test_claude_sync_never_writes_a_guard_under_a_symlinked_consumer_claude_dir()
	test_claude_sync_skips_a_guard_under_a_consumer_path_that_is_not_a_directory()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
