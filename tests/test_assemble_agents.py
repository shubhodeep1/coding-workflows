"""scripts/assemble_agents.py: fold agents.d/ fragments into agents.md (CLAUDE.md §30)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "assemble_agents.py"

AGENTS = """# Agents

Intro.

---

## Workflow architecture

First paragraph.

- `a` bullet

---

## Stable log prefixes (contractual)

Prefixes:

- `ALPHA`
- `BETA` (`scripts/b.sh`: `outcome=`)
  continued description

Prose between the registries.

LOG_PREFIX.name=ALPHA
LOG_PREFIX.name=BETA

---

## DigitalOcean resources

| Resource | Type | ID | Notes |
|---|---|---|---|
| app | app | 1 | prod |

## Repo-tree (auto-generated)

<!-- TREE:START id=workflows -->
tree
<!-- TREE:END id=workflows -->
"""


def _run(command: str, repo: Path, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
	merged = {key: value for key, value in os.environ.items() if key != "GITHUB_OUTPUT"}
	merged.update({"PYTHONDONTWRITEBYTECODE": "1", **(env or {})})
	return subprocess.run(
		[sys.executable, str(SCRIPT), command, "--repo-root", str(repo), *extra],
		capture_output=True, text=True, env=merged, check=False,
	)


def _repo(tmp_path: Path, fragments: dict[str, str], agents: str = AGENTS, name: str = "agents.md") -> Path:
	(tmp_path / name).write_text(agents, encoding="utf-8")
	fragments_dir = tmp_path / "agents.d"
	fragments_dir.mkdir()
	for file_name, body in fragments.items():
		(fragments_dir / file_name).write_text(body, encoding="utf-8")
	return tmp_path


def _assembled(tmp_path: Path, fragments: dict[str, str], **kwargs) -> tuple[str, subprocess.CompletedProcess[str]]:
	repo = _repo(tmp_path, fragments, **kwargs)
	result = _run("assemble", repo)
	assert result.returncode == 0, result.stdout + result.stderr
	return (repo / kwargs.get("name", "agents.md")).read_text(encoding="utf-8"), result


def test_no_fragments_is_a_byte_identical_noop(tmp_path: Path) -> None:
	text, result = _assembled(tmp_path, {})
	assert text == AGENTS
	assert "outcome=noop" in result.stdout


def test_prose_is_appended_at_the_end_of_its_section_before_the_rule(tmp_path: Path) -> None:
	text, result = _assembled(tmp_path, {"7100-x.md": '<!-- agents: section="Workflow architecture" -->\n\nNew paragraph.\n'})
	assert "- `a` bullet\n\nNew paragraph.\n\n---\n\n## Stable log prefixes" in text
	assert "outcome=assembled" in result.stdout and "folded=1" in result.stdout
	assert not (tmp_path / "agents.d" / "7100-x.md").exists()


def test_list_items_and_table_rows_continue_the_list_or_table(tmp_path: Path) -> None:
	text, _ = _assembled(tmp_path, {
		"7101-a.md": '<!-- agents: section="Workflow architecture" -->\n- `b` bullet\n',
		"7102-b.md": '<!-- agents: section="DigitalOcean resources" -->\n| db | db | 2 | staging |\n',
	})
	assert "- `a` bullet\n- `b` bullet\n\n---" in text
	assert "| app | app | 1 | prod |\n| db | db | 2 | staging |\n\n## Repo-tree" in text


def test_log_prefixes_extend_both_registries(tmp_path: Path) -> None:
	text, _ = _assembled(tmp_path, {"7103-p.md": (
		'<!-- agents: section="Stable log prefixes (contractual)" -->\n'
		"- `GAMMA` (`scripts/g.sh`: `outcome=`)\n"
		"  wrapped description\n"
		"LOG_PREFIX.name=GAMMA\n"
	)})
	assert "  continued description\n- `GAMMA` (`scripts/g.sh`: `outcome=`)\n  wrapped description\n\nProse between" in text
	assert "LOG_PREFIX.name=BETA\nLOG_PREFIX.name=GAMMA\n\n---" in text
	# Nothing else moved.
	assert text.replace("- `GAMMA` (`scripts/g.sh`: `outcome=`)\n  wrapped description\n", "").replace("LOG_PREFIX.name=GAMMA\n", "") == AGENTS


def test_fragments_fold_in_filename_order(tmp_path: Path) -> None:
	text, _ = _assembled(tmp_path, {
		"7200-later.md": '<!-- agents: section="Workflow architecture" -->\nSecond.\n',
		"7105-first.md": '<!-- agents: section="Workflow architecture" -->\nFirst.\n',
	})
	assert text.index("First.") < text.index("Second.")


def test_one_fragment_may_target_several_sections(tmp_path: Path) -> None:
	text, _ = _assembled(tmp_path, {"7106-multi.md": (
		'<!-- agents: section="Workflow architecture" -->\nArchitecture note.\n\n'
		'<!-- agents: section="Stable log prefixes (contractual)" -->\n- `DELTA`\nLOG_PREFIX.name=DELTA\n'
	)})
	assert "Architecture note.\n\n---" in text
	assert "- `DELTA`\n" in text and "LOG_PREFIX.name=DELTA\n" in text


def test_new_sections_need_the_new_flag_and_go_before_the_repo_tree(tmp_path: Path) -> None:
	repo = _repo(tmp_path, {"7107-typo.md": '<!-- agents: section="Workflow architectur" -->\nLost.\n'})
	result = _run("assemble", repo)
	assert result.returncode == 0
	assert "skipped=7107-typo.md reason=section_not_found" in result.stderr
	assert (repo / "agents.d" / "7107-typo.md").exists(), "an invalid fragment stays for a fix"
	assert (repo / "agents.md").read_text(encoding="utf-8") == AGENTS
	(repo / "agents.d" / "7107-typo.md").write_text('<!-- agents: section="Brand new topic" new -->\nHello.\n', encoding="utf-8")
	assert _run("assemble", repo).returncode == 0
	text = (repo / "agents.md").read_text(encoding="utf-8")
	assert "| app | app | 1 | prod |\n\n## Brand new topic\n\nHello.\n\n## Repo-tree (auto-generated)" in text


@pytest.mark.parametrize(
	("body", "reason"),
	[
		("No marker here.\n", "content_before_marker"),
		("", "no_marker"),
		('<!-- agents: section="Workflow architecture" -->\n\n', "empty_block"),
		('<!-- agents: section="Repo-tree (auto-generated)" -->\nx\n', "generated_section"),
		("<!-- agents: section=Workflow -->\nx\n", "malformed_marker"),
		('<!-- agents: section="Workflow architecture" -->\n## Sneaky heading\n', "heading_in_block"),
	],
)
def test_check_fails_and_assemble_skips_invalid_fragments(tmp_path: Path, body: str, reason: str) -> None:
	repo = _repo(tmp_path, {"7108-bad.md": body, "7109-good.md": '<!-- agents: section="Workflow architecture" -->\nGood.\n'})
	check = _run("check", repo)
	assert check.returncode == 1
	assert f"skipped=7108-bad.md reason={reason}" in check.stderr
	assert "outcome=invalid" in check.stdout
	result = _run("assemble", repo)
	assert result.returncode == 0
	assert "folded=1 skipped=1" in result.stdout
	assert "Good." in (repo / "agents.md").read_text(encoding="utf-8")
	assert (repo / "agents.d" / "7108-bad.md").exists()
	assert not (repo / "agents.d" / "7109-good.md").exists()


def test_render_prints_the_folded_text_and_writes_nothing(tmp_path: Path) -> None:
	repo = _repo(tmp_path, {"7110-r.md": '<!-- agents: section="Workflow architecture" -->\nPending note.\n'})
	result = _run("render", repo)
	assert result.returncode == 0
	assert "Pending note." in result.stdout
	assert (repo / "agents.md").read_text(encoding="utf-8") == AGENTS
	assert (repo / "agents.d" / "7110-r.md").exists()


def test_check_passes_clean_fragments_and_dry_run_writes_nothing(tmp_path: Path) -> None:
	repo = _repo(tmp_path, {"7111-c.md": '<!-- agents: section="Workflow architecture" -->\nFine.\n'})
	assert _run("check", repo).returncode == 0
	assert _run("assemble", repo, "--dry-run").returncode == 0
	assert (repo / "agents.md").read_text(encoding="utf-8") == AGENTS
	assert (repo / "agents.d" / "7111-c.md").exists()


def test_scaffolding_and_non_markdown_files_are_ignored(tmp_path: Path) -> None:
	repo = _repo(tmp_path, {"README.md": "how to write fragments", ".gitkeep": "", "notes.txt": "x"})
	assert _run("check", repo).returncode == 0
	assert _run("assemble", repo).returncode == 0
	assert (repo / "agents.md").read_text(encoding="utf-8") == AGENTS
	assert (repo / "agents.d" / "README.md").exists()


def test_consumer_casing_and_missing_file(tmp_path: Path) -> None:
	text, _ = _assembled(tmp_path, {"5-x.md": '<!-- agents: section="Workflow architecture" -->\nConsumer note.\n'}, name="AGENTS.md")
	assert "Consumer note." in text
	fresh = tmp_path / "fresh"
	fresh.mkdir()
	(fresh / "agents.d").mkdir()
	(fresh / "agents.d" / "1-a.md").write_text('<!-- agents: section="Setup" new -->\nCreated.\n', encoding="utf-8")
	assert _run("assemble", fresh).returncode == 0
	assert (fresh / "AGENTS.md").read_text(encoding="utf-8") == "## Setup\n\nCreated.\n"


def test_symlinked_agents_file_is_refused(tmp_path: Path) -> None:
	(tmp_path / "real.md").write_text(AGENTS, encoding="utf-8")
	(tmp_path / "agents.md").symlink_to(tmp_path / "real.md")
	(tmp_path / "agents.d").mkdir()
	(tmp_path / "agents.d" / "1-a.md").write_text('<!-- agents: section="Workflow architecture" -->\nx\n', encoding="utf-8")
	result = _run("assemble", tmp_path)
	assert result.returncode == 1
	assert "error=agents_unreadable" in result.stderr
	assert (tmp_path / "real.md").read_text(encoding="utf-8") == AGENTS


def test_headings_inside_code_fences_are_not_sections(tmp_path: Path) -> None:
	agents = "## Real\n\n```md\n## Workflow architecture\n```\n\nEnd.\n"
	repo = _repo(tmp_path, {"1-a.md": '<!-- agents: section="Workflow architecture" -->\nx\n'}, agents=agents)
	assert _run("check", repo).returncode == 1


def test_fenced_examples_in_fragments_are_content(tmp_path: Path) -> None:
	body = (
		'<!-- agents: section="Workflow architecture" -->\n'
		"Example:\n\n```md\n## Not a heading\n<!-- agents: section=\"Elsewhere\" -->\n```\n"
	)
	text, result = _assembled(tmp_path, {"1-fence.md": body})
	assert "folded=1 skipped=0" in result.stdout
	assert "```md\n## Not a heading\n<!-- agents: section=\"Elsewhere\" -->\n```\n\n---" in text


def test_unclosed_fence_in_fragment_is_invalid(tmp_path: Path) -> None:
	repo = _repo(tmp_path, {"1-open.md": '<!-- agents: section="Workflow architecture" -->\n```\nnever closed\n'})
	check = _run("check", repo)
	assert check.returncode == 1
	assert "reason=unclosed_fence" in check.stderr


def test_symlinked_fragments_dir_is_ignored(tmp_path: Path) -> None:
	(tmp_path / "agents.md").write_text(AGENTS, encoding="utf-8")
	docs = tmp_path / "docs"
	docs.mkdir()
	(docs / "1-a.md").write_text('<!-- agents: section="Workflow architecture" -->\nx\n', encoding="utf-8")
	(tmp_path / "agents.d").symlink_to(docs, target_is_directory=True)
	result = _run("assemble", tmp_path)
	assert result.returncode == 0
	assert "outcome=noop" in result.stdout
	assert (docs / "1-a.md").exists(), "files behind a symlinked agents.d/ are never deleted"
	assert (tmp_path / "agents.md").read_text(encoding="utf-8") == AGENTS


def test_github_outputs(tmp_path: Path) -> None:
	repo = _repo(tmp_path, {"1-a.md": '<!-- agents: section="Workflow architecture" -->\nx\n'})
	outputs = tmp_path / "out.txt"
	result = _run("assemble", repo, env={"GITHUB_OUTPUT": str(outputs)})
	assert result.returncode == 0
	emitted = outputs.read_text(encoding="utf-8")
	assert "agents_assembled=true" in emitted and "agents_fragment_count=1" in emitted and "agents_file=agents.md" in emitted


def test_real_agents_md_and_pending_fragments_fold_cleanly(tmp_path: Path) -> None:
	"""Every fragment waiting in this repository folds into the real agents.md."""
	result = _run("check", REPO_ROOT)
	assert result.returncode == 0, result.stdout + result.stderr
	repo = tmp_path / "copy"
	repo.mkdir()
	(repo / "agents.md").write_text((REPO_ROOT / "agents.md").read_text(encoding="utf-8"), encoding="utf-8")
	(repo / "agents.d").mkdir()
	(repo / "agents.d" / "1-p.md").write_text(
		'<!-- agents: section="Stable log prefixes (contractual)" -->\n- `ZZ_TEST_PREFIX`\nLOG_PREFIX.name=ZZ_TEST_PREFIX\n',
		encoding="utf-8",
	)
	assert _run("assemble", repo).returncode == 0
	text = (repo / "agents.md").read_text(encoding="utf-8")
	assert "- `VALIDATION_HARNESS_SANDBOX`\n- `ZZ_TEST_PREFIX`\n\nWhen `EVENTS_JSONL_ENABLED=true`" in text
	assert "LOG_PREFIX.name=VALIDATION_HARNESS_SANDBOX\nLOG_PREFIX.name=ZZ_TEST_PREFIX\n\n---" in text


def test_two_prs_with_fragments_merge_without_conflict(tmp_path: Path) -> None:
	"""The point of the mechanism: concurrent documentation never conflicts."""
	env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

	def git(*args: str) -> subprocess.CompletedProcess[str]:
		return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
			capture_output=True, text=True, env=env, check=False)

	git("init", "-q", "-b", "main")
	(tmp_path / "agents.md").write_text(AGENTS, encoding="utf-8")
	(tmp_path / "agents.d").mkdir()
	(tmp_path / "agents.d" / ".gitkeep").write_text("", encoding="utf-8")
	git("add", "-A")
	git("commit", "-q", "-m", "base")
	for branch, number in (("one", "11"), ("two", "12")):
		git("checkout", "-q", "-b", branch, "main")
		(tmp_path / "agents.d" / f"{number}-x.md").write_text(
			f'<!-- agents: section="Stable log prefixes (contractual)" -->\n- `P{number}`\nLOG_PREFIX.name=P{number}\n', encoding="utf-8")
		git("add", "-A")
		git("commit", "-q", "-m", branch)
	git("checkout", "-q", "main")
	assert git("merge", "-q", "--no-edit", "one").returncode == 0
	assert git("merge", "-q", "--no-edit", "two").returncode == 0
	assert _run("assemble", tmp_path).returncode == 0
	text = (tmp_path / "agents.md").read_text(encoding="utf-8")
	assert "LOG_PREFIX.name=P11\nLOG_PREFIX.name=P12\n" in text
