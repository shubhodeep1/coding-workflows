#!/usr/bin/env python3
"""Contract tests for scripts/sync_dependabot_exclusions.py (issue #9485).

The helper adds the sync-managed wrappers to ``exclude-paths`` of a consumer's
own root ``github-actions`` Dependabot entries, so Dependabot stops opening
bump PRs that ``coding-workflows/workflow-integrity`` fails by design. It
never creates the file or an entry, keeps every consumer line as written, and
leaves any layout it does not understand untouched (fail open).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "sync_dependabot_exclusions.py"
BUILDER = REPO_ROOT / "scripts" / "build_sync_manifest.py"
UPSTREAM_DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

TEMPLATES = ("ai-review.yml", "ai-plan.yml", "ai-old.yml", "ai-workflow-integrity.yml", "ai-update-workflows.yml")
PROFILES = {
	"full": "# full profile\nai-review.yml\nai-plan.yml\n\nai-workflow-integrity.yml\n",
	"core": "ai-review.yml\n",
	"empty": "# nothing\n",
}


def _templates(tmp_path: Path) -> Path:
	templates = tmp_path / "workflow-templates"
	(templates / "profiles").mkdir(parents=True)
	for name in TEMPLATES:
		(templates / name).write_text(f"name: {name}\n", encoding="utf-8")
	for name, text in PROFILES.items():
		(templates / "profiles" / f"{name}.txt").write_text(text, encoding="utf-8")
	return templates


def _consumer(tmp_path: Path, config: str | None, name: str = "dependabot.yml", updater: bool = False) -> Path:
	repo = tmp_path / "consumer"
	(repo / ".github" / "workflows").mkdir(parents=True)
	if config is not None:
		(repo / ".github" / name).write_text(config, encoding="utf-8")
	if updater:
		(repo / ".github" / "workflows" / "ai-update-workflows.yml").write_text("name: u\n", encoding="utf-8")
	return repo


def _run(repo: Path, templates: Path, profile: str = "full", tmp_path: Path | None = None, extra: tuple = ()) -> tuple:
	work = (tmp_path or repo.parent) / "work"
	work.mkdir(exist_ok=True)
	changed = work / "changed.txt"
	output = work / "github_output"
	output.write_text("", encoding="utf-8")
	env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GITHUB_OUTPUT": str(output)}
	result = subprocess.run(
		[sys.executable, str(HELPER), "--repo-root", str(repo), "--templates-dir", str(templates), "--profile", profile, "--changed-paths-file", str(changed), *extra],
		capture_output=True,
		text=True,
		env=env,
	)
	changed_text = changed.read_text(encoding="utf-8") if changed.exists() else None
	return result, changed_text, output.read_text(encoding="utf-8")


BASIC = """version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
"""

MANAGED_FULL = [
	'      - ".github/workflows/ai-plan.yml"',
	'      - ".github/workflows/ai-review.yml"',
	'      - ".github/workflows/ai-workflow-integrity.yml"',
]


def test_adds_exclude_paths_when_missing(tmp_path):
	templates = _templates(tmp_path)
	repo = _consumer(tmp_path, BASIC)
	result, changed, output = _run(repo, templates)
	assert result.returncode == 0, result.stderr
	text = (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8")
	assert text == BASIC + "    exclude-paths:\n" + "\n".join(MANAGED_FULL) + "\n"
	assert changed == ".github/dependabot.yml\n"
	assert "dependabot_has_changes=true" in output
	assert "DEPENDABOT_EXCLUDE_SYNC outcome=updated reason=excluded file=.github/dependabot.yml entries=1 managed=3" in result.stdout


def test_second_run_is_a_no_op(tmp_path):
	templates = _templates(tmp_path)
	repo = _consumer(tmp_path, BASIC)
	_run(repo, templates)
	first = (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8")
	result, changed, output = _run(repo, templates)
	assert result.returncode == 0
	assert (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8") == first
	assert changed == ""
	assert "dependabot_has_changes=false" in output
	assert "outcome=unchanged reason=up_to_date" in result.stdout


def test_no_config_is_a_no_op(tmp_path):
	templates = _templates(tmp_path)
	repo = _consumer(tmp_path, None)
	result, changed, output = _run(repo, templates)
	assert result.returncode == 0
	assert not (repo / ".github" / "dependabot.yml").exists()
	assert changed == ""
	assert "dependabot_has_changes=false" in output
	assert "outcome=skipped reason=no_config file=none" in result.stdout


def test_no_github_actions_entry_is_never_added(tmp_path):
	templates = _templates(tmp_path)
	config = "version: 2\nupdates:\n  - package-ecosystem: npm\n    directory: /\n    schedule:\n      interval: daily\n"
	repo = _consumer(tmp_path, config)
	result, changed, _ = _run(repo, templates)
	assert result.returncode == 0
	assert (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8") == config
	assert changed == ""
	assert "outcome=skipped reason=no_github_actions_entry" in result.stdout


def test_only_non_root_entry_is_left_alone(tmp_path):
	templates = _templates(tmp_path)
	config = BASIC.replace('directory: "/"', 'directory: "/tools"')
	repo = _consumer(tmp_path, config)
	result, _, _ = _run(repo, templates)
	assert (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8") == config
	assert "outcome=skipped reason=no_root_entry" in result.stdout


def test_consumer_exclude_paths_and_comments_are_kept(tmp_path):
	templates = _templates(tmp_path)
	config = (
		"# consumer config\n"
		"version: 2\n"
		"updates:\n"
		"  - package-ecosystem: github-actions # actions\n"
		"    directory: /\n"
		"    exclude-paths:\n"
		"      # keep my own\n"
		"      - 'vendor/**' # vendored\n"
		"      - .github/workflows/*.generated.yml\n"
		"    schedule:\n"
		"      interval: daily\n"
		"\n"
		"  # npm below\n"
		"  - package-ecosystem: npm\n"
		"    directory: /\n"
	)
	repo = _consumer(tmp_path, config)
	result, changed, _ = _run(repo, templates)
	assert result.returncode == 0, result.stderr
	expected = config.replace(
		"      - .github/workflows/*.generated.yml\n",
		"      - .github/workflows/*.generated.yml\n" + "\n".join(MANAGED_FULL) + "\n",
	)
	assert (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8") == expected
	assert changed == ".github/dependabot.yml\n"


def test_sync_owned_entries_follow_a_profile_change(tmp_path):
	templates = _templates(tmp_path)
	config = BASIC + (
		"    exclude-paths:\n"
		"      - \"docs/**\"\n"
		"      - \".github/workflows/ai-old.yml\"\n"
		"      - .github/workflows/ai-plan.yml\n"
		"      - \".github/workflows/consumer-own.yml\"\n"
	)
	repo = _consumer(tmp_path, config)
	result, _, _ = _run(repo, templates, profile="core")
	assert result.returncode == 0, result.stderr
	text = (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8")
	assert text == BASIC + (
		"    exclude-paths:\n"
		"      - \"docs/**\"\n"
		"      - \".github/workflows/consumer-own.yml\"\n"
		"      - \".github/workflows/ai-review.yml\"\n"
		"      - \".github/workflows/ai-workflow-integrity.yml\"\n"
	)


def test_updater_wrapper_is_managed_only_when_present(tmp_path):
	templates = _templates(tmp_path)
	repo = _consumer(tmp_path, BASIC, updater=True)
	result, _, _ = _run(repo, templates, profile="core")
	assert result.returncode == 0
	text = (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8")
	assert '- ".github/workflows/ai-update-workflows.yml"' in text
	assert "managed=3" in result.stdout
	(repo / ".github" / "workflows" / "ai-update-workflows.yml").unlink()
	_run(repo, templates, profile="core")
	assert "ai-update-workflows.yml" not in (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8")


def test_directories_list_and_two_matching_entries(tmp_path):
	templates = _templates(tmp_path)
	config = (
		"version: 2\n"
		"updates:\n"
		"- package-ecosystem: github-actions\n"
		"  directories:\n"
		"    - \"/\"\n"
		"    - \"/sub\"\n"
		"  schedule:\n"
		"    interval: weekly\n"
		"- package-ecosystem: github-actions\n"
		"  directories: [\"/sub\", \"/\"]\n"
		"  target-branch: develop\n"
		"registries: {}\n"
	)
	repo = _consumer(tmp_path, config, name="dependabot.yaml")
	result, changed, _ = _run(repo, templates, profile="core")
	assert result.returncode == 0, result.stderr
	text = (repo / ".github" / "dependabot.yaml").read_text(encoding="utf-8")
	block = '  exclude-paths:\n    - ".github/workflows/ai-review.yml"\n    - ".github/workflows/ai-workflow-integrity.yml"\n'
	assert text == config.replace("    interval: weekly\n", "    interval: weekly\n" + block).replace("  target-branch: develop\n", "  target-branch: develop\n" + block)
	assert changed == ".github/dependabot.yaml\n"
	assert "entries=2" in result.stdout


@pytest.mark.parametrize(
	"config",
	[
		BASIC.replace("\n", "\r\n"),
		BASIC.replace("    schedule:\n", "\tschedule:\n"),
		"---\n" + BASIC,
		BASIC.replace('    schedule:\n      interval: "weekly"\n', "    schedule: *weekly\n"),
		"version: 2\nupdates: [{package-ecosystem: github-actions, directory: /}]\n",
		BASIC + "    exclude-paths: [\"docs/**\"]\n",
		BASIC + "    commit-message:\n      prefix: |\n        deps\n",
		"version: 2\nupdates:\n  - package-ecosystem: 'github-actions\n    directory: /\n",
	],
	ids=["crlf", "tab", "document-marker", "alias", "flow-updates", "flow-exclude-paths", "block-scalar", "unterminated-quote"],
)
def test_unsupported_layouts_are_left_untouched(tmp_path, config):
	templates = _templates(tmp_path)
	repo = _consumer(tmp_path, None)
	target = repo / ".github" / "dependabot.yml"
	target.write_bytes(config.encode("utf-8"))
	result, changed, output = _run(repo, templates)
	assert result.returncode == 0, result.stderr
	assert target.read_bytes() == config.encode("utf-8")
	assert changed == ""
	assert "dependabot_has_changes=false" in output
	assert "outcome=skipped reason=unsupported_layout" in result.stdout


def test_symlinked_config_is_refused(tmp_path):
	templates = _templates(tmp_path)
	repo = _consumer(tmp_path, None)
	outside = tmp_path / "outside.yml"
	outside.write_text(BASIC, encoding="utf-8")
	(repo / ".github" / "dependabot.yml").symlink_to(outside)
	result, changed, _ = _run(repo, templates)
	assert result.returncode == 0
	assert outside.read_text(encoding="utf-8") == BASIC
	assert changed == ""
	assert "outcome=skipped reason=unsupported_layout" in result.stdout


def test_both_config_names_present_is_ambiguous(tmp_path):
	templates = _templates(tmp_path)
	repo = _consumer(tmp_path, BASIC)
	(repo / ".github" / "dependabot.yaml").write_text(BASIC, encoding="utf-8")
	result, changed, _ = _run(repo, templates)
	assert result.returncode == 0
	assert (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8") == BASIC
	assert changed == ""
	assert "outcome=skipped reason=ambiguous_config" in result.stdout


@pytest.mark.parametrize("profile,reason", [("../full", "profile_invalid"), ("missing", "profile_unknown"), ("empty", "profile_empty")])
def test_bad_profile_skips_without_change(tmp_path, profile, reason):
	templates = _templates(tmp_path)
	repo = _consumer(tmp_path, BASIC)
	result, changed, _ = _run(repo, templates, profile=profile)
	assert result.returncode == 0
	assert (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8") == BASIC
	assert changed == ""
	assert f"outcome=skipped reason={reason}" in result.stdout


def test_dry_run_writes_nothing(tmp_path):
	templates = _templates(tmp_path)
	repo = _consumer(tmp_path, BASIC)
	result, changed, output = _run(repo, templates, extra=("--dry-run",))
	assert result.returncode == 0
	assert (repo / ".github" / "dependabot.yml").read_text(encoding="utf-8") == BASIC
	assert changed == ""
	assert "dependabot_has_changes=false" in output


def test_bad_arguments_exit_2(tmp_path):
	result = subprocess.run([sys.executable, str(HELPER), "--repo-root", str(tmp_path)], capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
	assert result.returncode == 2


def test_result_parses_as_the_same_yaml_plus_exclusions(tmp_path):
	yaml = pytest.importorskip("yaml")
	templates = _templates(tmp_path)
	config = "version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n    exclude-paths:\n      - 'vendor/**'\n    schedule:\n      interval: daily\n"
	repo = _consumer(tmp_path, config)
	_run(repo, templates)
	before = yaml.safe_load(config)
	after = yaml.safe_load((repo / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
	before["updates"][0]["exclude-paths"] += [".github/workflows/ai-plan.yml", ".github/workflows/ai-review.yml", ".github/workflows/ai-workflow-integrity.yml"]
	assert after == before


def test_reported_paths_pass_the_sync_manifest_allowlist(tmp_path):
	spec = importlib.util.spec_from_file_location("build_sync_manifest_for_dependabot", BUILDER)
	builder = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(builder)
	compiled = [builder.glob_to_regex(entry) for entry in builder.STATIC_ALLOWED_PATHS]
	for name in ("dependabot.yml", "dependabot.yaml"):
		templates_root = tmp_path / name
		templates_root.mkdir()
		templates = _templates(templates_root)
		repo = _consumer(templates_root, BASIC, name=name)
		_, changed, _ = _run(repo, templates, tmp_path=templates_root)
		paths = [line for line in changed.splitlines() if line]
		assert paths == [f".github/{name}"]
		for path in paths:
			assert builder.path_allowed(path, compiled), path
	assert ".github/dependabot.yml" not in builder.STATIC_UPSTREAM_MANAGED_PATHS
	assert ".github/dependabot.yaml" not in builder.STATIC_UPSTREAM_MANAGED_PATHS


def test_upstream_dependabot_config_covers_root_github_actions():
	text = UPSTREAM_DEPENDABOT.read_text(encoding="utf-8")
	assert "\t" not in text
	yaml = pytest.importorskip("yaml")
	doc = yaml.safe_load(text)
	assert doc["version"] == 2
	entries = [entry for entry in doc["updates"] if entry.get("package-ecosystem") == "github-actions"]
	assert len(entries) == 1
	assert entries[0]["directory"] == "/"
	assert entries[0]["schedule"]["interval"] == "weekly"
	# Dependabot does not promise to scan plain workflow-templates/*.yml.
	assert "workflow-templates" not in text


def test_helper_never_imports_a_yaml_library():
	source = HELPER.read_text(encoding="utf-8")
	assert "import yaml" not in source
	assert "import subprocess" not in source


if __name__ == "__main__":
	sys.exit(pytest.main([__file__, "-q"]))
