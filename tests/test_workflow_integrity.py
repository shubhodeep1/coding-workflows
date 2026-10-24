"""Consumer managed-wrapper integrity check (workflow_integrity.yml).

Structural tests pin the wiring (template, profiles, status context, the
verifier copy); behavioural tests run the inline checker against throwaway
git repositories.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from workflow_wrapper_refs import pin_reusable_workflow_refs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow_integrity.yml"
UPDATE_WORKFLOWS = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
TEMPLATE = REPO_ROOT / "workflow-templates" / "ai-workflow-integrity.yml"
PROFILES = REPO_ROOT / "workflow-templates" / "profiles"
RELEASE_SHA = "a" * 40
OTHER_SHA = "b" * 40


def _heredoc(text: str, tag: str) -> str:
	body = text.split(f"<<'{tag}'\n", 1)[1].split(f"\n          {tag}\n", 1)[0]
	return "\n".join(line[10:] if line.strip() else "" for line in body.split("\n")) + "\n"


@pytest.fixture()
def checker(tmp_path: Path) -> Path:
	path = tmp_path / "workflow_integrity.py"
	path.write_text(_heredoc(WORKFLOW.read_text(encoding="utf-8"), "WORKFLOW_INTEGRITY_PY"), encoding="utf-8")
	return path


def _run(checker: Path, *args: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		[sys.executable, "-I", str(checker), *args],
		capture_output=True,
		text=True,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
	)


def _render(name: str, sha: str = RELEASE_SHA) -> str:
	return pin_reusable_workflow_refs((REPO_ROOT / "workflow-templates" / name).read_text(encoding="utf-8"), sha)


def _git(repo: Path, *args: str) -> str:
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, files: dict, message: str) -> str:
	workflows = repo / ".github" / "workflows"
	workflows.mkdir(parents=True, exist_ok=True)
	for name, content in files.items():
		target = workflows / name
		if content is None:
			target.unlink()
		else:
			target.write_text(content, encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", message)
	return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def consumer(tmp_path: Path) -> Path:
	repo = tmp_path / "consumer"
	repo.mkdir()
	_git(repo, "init", "-q")
	return repo


def _profile(tmp_path: Path, names: list) -> Path:
	path = tmp_path / "profile.txt"
	path.write_text("\n".join(names) + "\n", encoding="utf-8")
	return path


def _rendered_dir(tmp_path: Path, sha: str = RELEASE_SHA) -> Path:
	out = tmp_path / f"rendered-{sha[:4]}"
	out.mkdir()
	for template in (REPO_ROOT / "workflow-templates").glob("*.yml"):
		(out / template.name).write_text(_render(template.name, sha), encoding="utf-8")
	return out


def _extract(checker: Path, repo: Path, sha: str, dest: Path) -> Path:
	result = _run(checker, "extract", str(repo), sha, str(dest))
	assert result.returncode == 0, result.stdout + result.stderr
	return dest


PRISTINE = ("ai-clarify.yml", "ai-plan.yml", "ai-update-workflows.yml", "ai-workflow-integrity.yml")


def _pristine(sha: str = RELEASE_SHA) -> dict:
	return {name: _render(name, sha) for name in PRISTINE}


# ── Structural contract ──────────────────────────────────────────────────


def test_template_runs_from_base_branch_and_forwards_only_gh_pat() -> None:
	template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
	triggers = template[True]
	assert set(triggers) == {"pull_request_target", "push"}
	assert triggers["push"]["branches"] == ["**"]
	assert template["permissions"] == {"contents": "read", "statuses": "write"}
	job = template["jobs"]["integrity"]
	assert job["uses"] == "shubhodeep1/coding-workflows/.github/workflows/workflow_integrity.yml@stable"
	assert job["secrets"] == {"GH_PAT": "${{ secrets.GH_PAT }}"}
	assert "secrets: inherit" not in TEMPLATE.read_text(encoding="utf-8")
	# The renderer pins it like every other wrapper.
	assert f"workflow_integrity.yml@{RELEASE_SHA} # stable" in _render("ai-workflow-integrity.yml")


@pytest.mark.parametrize("profile", ["core", "standard", "full"])
def test_every_install_profile_ships_the_integrity_wrapper(profile: str) -> None:
	names = (PROFILES / f"{profile}.txt").read_text(encoding="utf-8").split()
	assert "ai-workflow-integrity.yml" in names


def test_signed_manifest_verifier_is_a_copy_of_the_sync_verifier() -> None:
	ours = _heredoc(WORKFLOW.read_text(encoding="utf-8"), "SYNC_VERIFY_PY")
	sync = _heredoc(UPDATE_WORKFLOWS.read_text(encoding="utf-8"), "SYNC_VERIFY_PY")
	assert ours == sync
	text = WORKFLOW.read_text(encoding="utf-8")
	assert "--cert-identity-regex '^https://github\\.com/shubhodeep1/coding-workflows/\\.github/workflows/(test-and-mark-stable|mark-stable)\\.yml@refs/heads/stable$'" in text
	assert "--deny-self-hosted-runners" in text


def test_reusable_workflow_wiring() -> None:
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	assert set(workflow["on"]["workflow_call"]["secrets"]) == {"GH_PAT"}
	job = workflow["jobs"]["integrity"]
	assert job["env"]["INTEGRITY_STATUS_CONTEXT"] == "coding-workflows/workflow-integrity"
	steps = job["steps"]
	names = [step["name"] for step in steps]
	assert names.index("Verify signed sync manifest") < names.index("Compare managed wrappers with the pinned release")
	assert steps[-1]["name"] == "Publish integrity result"
	assert steps[-1]["if"] == "always()"
	for step in steps:
		# Event data reaches the shell only through env.
		assert "${{" not in step.get("run", ""), step["name"]
	compare = next(step for step in steps if step["name"].startswith("Compare managed"))
	assert compare["if"] == "steps.verify_manifest.outcome == 'success'"


def test_push_runs_never_post_the_pull_request_status() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	publish = text[text.index("      - name: Publish integrity result"):]
	assert 'if [ "${MODE}" = "pull_request" ]' in publish
	resolve = text[text.index("      - name: Resolve integrity inputs"):text.index("      - name: Read consumer workflow trees")]
	assert 'if [ "${MODE}" = "pull_request" ]; then' in resolve


# ── Behaviour of the inline checker ──────────────────────────────────────


def test_unchanged_managed_wrappers_are_skipped(checker, consumer, tmp_path) -> None:
	base = _commit(consumer, _pristine(), "base")
	head = _commit(consumer, {"ci.yml": "name: ci\non: push\n"}, "own workflow")
	result = _run(checker, "detect", str(_extract(checker, consumer, base, tmp_path / "b")), str(_extract(checker, consumer, head, tmp_path / "h")))
	assert result.returncode == 0
	assert result.stdout.strip() == ""


def test_detect_reports_edits_deletions_and_new_upstream_callers(checker, consumer, tmp_path) -> None:
	files = _pristine()
	base = _commit(consumer, files, "base")
	head = _commit(
		consumer,
		{
			"ai-plan.yml": files["ai-plan.yml"] + "# local tweak\n",
			"ai-clarify.yml": None,
			"mine.yml": "jobs:\n  x:\n    uses: shubhodeep1/coding-workflows/.github/workflows/review.yml@main\n",
		},
		"edits",
	)
	result = _run(checker, "detect", str(_extract(checker, consumer, base, tmp_path / "b")), str(_extract(checker, consumer, head, tmp_path / "h")))
	assert result.stdout.strip() == "ai-clarify.yml,ai-plan.yml,mine.yml"


def test_pin_reads_the_integrity_wrapper(checker, tmp_path) -> None:
	head = tmp_path / "h"
	head.mkdir()
	assert _run(checker, "pin", str(head)).stdout.strip() == "integrity_wrapper_missing"
	(head / "ai-workflow-integrity.yml").write_text(_render("ai-workflow-integrity.yml"), encoding="utf-8")
	ok = _run(checker, "pin", str(head))
	assert ok.returncode == 0 and ok.stdout.strip() == RELEASE_SHA
	(head / "ai-workflow-integrity.yml").write_text((REPO_ROOT / "workflow-templates/ai-workflow-integrity.yml").read_text(encoding="utf-8"), encoding="utf-8")
	assert _run(checker, "pin", str(head)).stdout.strip() == "pin_not_sha ref=stable"
	text = _render("ai-workflow-integrity.yml") + "  other:\n    uses: shubhodeep1/coding-workflows/.github/workflows/x.yml@" + OTHER_SHA + "\n"
	(head / "ai-workflow-integrity.yml").write_text(text, encoding="utf-8")
	assert _run(checker, "pin", str(head)).stdout.startswith("pin_ambiguous")


def test_compare_accepts_a_pristine_tree(checker, consumer, tmp_path) -> None:
	sha = _commit(consumer, _pristine(), "base")
	tree = _extract(checker, consumer, sha, tmp_path / "h")
	result = _run(checker, "compare", str(tree), str(tree), str(_rendered_dir(tmp_path)), str(_profile(tmp_path, ["ai-clarify.yml", "ai-plan.yml"])), RELEASE_SHA)
	assert result.returncode == 0, result.stdout
	assert result.stdout.strip().endswith("checked=4 problems=0")


def test_compare_rejects_edits_deletions_and_mixed_pins(checker, consumer, tmp_path) -> None:
	files = _pristine()
	base = _commit(consumer, files, "base")
	head = _commit(
		consumer,
		{
			"ai-plan.yml": files["ai-plan.yml"].replace("issues: write", "issues: write\n  pull-requests: write"),
			"ai-clarify.yml": _render("ai-clarify.yml", OTHER_SHA),
			"ai-update-workflows.yml": None,
		},
		"tamper",
	)
	result = _run(
		checker,
		"compare",
		str(_extract(checker, consumer, base, tmp_path / "b")),
		str(_extract(checker, consumer, head, tmp_path / "h")),
		str(_rendered_dir(tmp_path)),
		str(_profile(tmp_path, ["ai-clarify.yml", "ai-plan.yml"])),
		RELEASE_SHA,
	)
	assert result.returncode == 1
	lines = result.stdout.strip().splitlines()
	assert "modified file=ai-clarify.yml" in lines
	assert "modified file=ai-plan.yml" in lines
	assert "missing file=ai-update-workflows.yml" in lines


def test_compare_treats_a_never_installed_updater_as_unmanaged(checker, consumer, tmp_path) -> None:
	files = {name: content for name, content in _pristine().items() if name != "ai-update-workflows.yml"}
	sha = _commit(consumer, files, "base")
	tree = _extract(checker, consumer, sha, tmp_path / "h")
	profile = _profile(tmp_path, ["ai-clarify.yml", "ai-plan.yml", "ai-update-workflows.yml"])
	result = _run(checker, "compare", str(tree), str(tree), str(_rendered_dir(tmp_path)), str(profile), RELEASE_SHA)
	assert result.returncode == 0, result.stdout


def test_compare_requires_the_release_to_ship_every_managed_template(checker, consumer, tmp_path) -> None:
	sha = _commit(consumer, _pristine(), "base")
	tree = _extract(checker, consumer, sha, tmp_path / "h")
	rendered = _rendered_dir(tmp_path)
	(rendered / "ai-workflow-integrity.yml").unlink()
	result = _run(checker, "compare", str(tree), str(tree), str(rendered), str(_profile(tmp_path, ["ai-plan.yml"])), RELEASE_SHA)
	assert result.returncode == 1
	assert f"template_missing file=ai-workflow-integrity.yml release={RELEASE_SHA}" in result.stdout


def test_symlinked_wrapper_never_matches(checker, consumer, tmp_path) -> None:
	files = _pristine()
	base = _commit(consumer, files, "base")
	workflows = consumer / ".github" / "workflows"
	(workflows / "ai-plan.yml").unlink()
	(workflows / "real.txt").write_text(files["ai-plan.yml"], encoding="utf-8")
	os.symlink("real.txt", workflows / "ai-plan.yml")
	head = _commit(consumer, {}, "symlink")
	base_dir = _extract(checker, consumer, base, tmp_path / "b")
	head_dir = _extract(checker, consumer, head, tmp_path / "h")
	assert "ai-plan.yml" in _run(checker, "detect", str(base_dir), str(head_dir)).stdout
	result = _run(checker, "compare", str(base_dir), str(head_dir), str(_rendered_dir(tmp_path)), str(_profile(tmp_path, ["ai-plan.yml"])), RELEASE_SHA)
	assert "modified file=ai-plan.yml" in result.stdout


def test_extract_rejects_non_sha_input(checker, consumer, tmp_path) -> None:
	_commit(consumer, _pristine(), "base")
	result = _run(checker, "extract", str(consumer), "HEAD", str(tmp_path / "x"))
	assert result.returncode == 1
	assert result.stdout.strip() == "invalid_sha"
	assert re.fullmatch(r"[0-9a-f]{40}", _git(consumer, "rev-parse", "HEAD"))


# ── Sync side: ALLOW_WORKFLOW_EDITS no longer stops wrapper updates ───────

ASSET_STEPS = (
	"Apply canonical audit-gate assets",
	"Sync .claude/ assets from upstream",
	"Remove retired upstream files",
	"Sync top-level CLAUDE.md from upstream",
	"Sync changelog fragment assets from upstream",
	"Assemble changelog fragments",
	"Assemble agents.md fragments",
)


def test_sync_updates_wrappers_even_when_edits_are_opted_out() -> None:
	jobs = yaml.safe_load(UPDATE_WORKFLOWS.read_text(encoding="utf-8"))["jobs"]
	update = jobs["update-wrappers"]
	assert "if" not in update
	steps = {step["name"]: step for step in update["steps"]}
	notice = steps["Report ignored wrapper opt-out"]
	assert notice["if"] == "${{ inputs.allow_workflow_edits == false }}"
	assert "WORKFLOW_EDITS_OPT_OUT outcome=ignored scope=managed_wrappers" in notice["run"]
	for name in ASSET_STEPS:
		assert steps[name]["if"] == "${{ inputs.allow_workflow_edits != false }}", name
	for name in ("Prepare immutable workflow wrappers", "Compare and update workflow wrappers"):
		assert "if" not in steps[name], name
	assert 'if ! cmp -s "$upstream_file" "$local_file"; then' in steps["Compare and update workflow wrappers"]["run"]
	reproduce = next(step for step in jobs["verify"]["steps"] if step["name"] == "Reproduce derived consumer files")
	assert reproduce["env"]["SYNC_NON_WRAPPER_ASSETS"] == "${{ inputs.allow_workflow_edits != false }}"
	run = reproduce["run"]
	assert run.index('if [ "${SYNC_NON_WRAPPER_ASSETS:-true}" != "true" ]; then') < run.index("apply_audit_gate_assets.py")


def test_templates_no_longer_advertise_the_wrapper_opt_out() -> None:
	for template in (REPO_ROOT / "workflow-templates").glob("*.yml"):
		text = template.read_text(encoding="utf-8")
		assert "To opt out of auto-updates" not in text, template.name
		assert "# IMPORTANT: This file is managed by coding-workflows and will be overwritten\n# by the update process." in text, template.name
