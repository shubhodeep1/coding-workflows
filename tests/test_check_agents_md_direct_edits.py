"""scripts/check_agents_md_direct_edits.py: CLAUDE.md §30 enforcement (Q68).

New agents.md documentation goes in agents.d/ fragments; the agents file is
edited directly only to correct or remove text, and by `make generate` in the
Repo-tree section. #6902's final PR failed on an agents.md conflict built from
direct additions on both sides.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_agents_md_direct_edits.py"

BASE_DOC = """# Agents

## Workflow architecture

Line one.
Line two.
Line three.

## Stable log prefixes (contractual)

- `OLD_PREFIX`
LOG_PREFIX.name=OLD_PREFIX

## Repo-tree (auto-generated)

<!-- TREE:START -->
scripts/a.sh
<!-- TREE:END -->
"""


def _git(repo: Path, *args: str) -> str:
	env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
	result = subprocess.run(
		["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
		cwd=repo, env=env, capture_output=True, text=True, check=True,
	)
	return result.stdout.strip()


def _repo(tmp_path: Path, name: str = "agents.md") -> tuple[Path, str]:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	(repo / name).write_text(BASE_DOC, encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base")
	return repo, _git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, name: str, text: str) -> str:
	(repo / name).parent.mkdir(parents=True, exist_ok=True)
	(repo / name).write_text(text, encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "change")
	return _git(repo, "rev-parse", "HEAD")


def _check(repo: Path, base: str, head: str, **env: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		["python3", str(SCRIPT), "--base", base, "--head", head, "--repo-root", str(repo)],
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env}, capture_output=True, text=True, check=False,
	)


def test_appended_log_prefix_is_a_violation(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path)
	head = _commit(repo, "agents.md", BASE_DOC.replace(
		"LOG_PREFIX.name=OLD_PREFIX\n", "LOG_PREFIX.name=OLD_PREFIX\nLOG_PREFIX.name=NEW_PREFIX\n"))
	result = _check(repo, base, head)
	assert result.returncode == 1, result.stdout
	assert "agents.md:13 adds 1 line(s) under '## Stable log prefixes (contractual)'" in result.stdout
	assert "AGENTS_MD_DIRECT_EDIT outcome=violation count=1" in result.stdout
	assert "agents.d/<issue-or-pr>-<slug>.md fragment" in result.stdout


def test_correction_and_removal_pass(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path)
	head = _commit(repo, "agents.md", BASE_DOC.replace("Line two.\n", "Line two, corrected.\n").replace("Line three.\n", ""))
	result = _check(repo, base, head)
	assert result.returncode == 0, result.stdout
	assert "AGENTS_MD_DIRECT_EDIT outcome=ok net_added=agents.md=-1" in result.stdout


def test_correction_that_also_appends_beyond_the_allowance_is_a_violation(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path)
	extra = "".join(f"Extra {n}.\n" for n in range(6))
	head = _commit(repo, "agents.md", BASE_DOC.replace("Line two.\n", "Line two, corrected.\n" + extra))
	result = _check(repo, base, head)
	assert result.returncode == 1, result.stdout
	assert "agents.md gains 6 line(s)" in result.stdout and "(limit 5)" in result.stdout
	assert _check(repo, base, head, AGENTS_MD_DIRECT_NET_ADD_MAX="10").returncode == 0


def test_removal_elsewhere_does_not_offset_an_addition(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path)
	extra = "".join(f"Extra {n}.\n" for n in range(6))
	head = _commit(repo, "agents.md", BASE_DOC.replace("Line two.\n", "Line two, corrected.\n" + extra).replace(
		"## Stable log prefixes (contractual)\n\n- `OLD_PREFIX`\nLOG_PREFIX.name=OLD_PREFIX\n\n", ""))
	result = _check(repo, base, head)
	assert result.returncode == 1, result.stdout
	assert "agents.md gains 6 line(s)" in result.stdout


def test_generated_repo_tree_section_is_exempt(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path)
	head = _commit(repo, "agents.md", BASE_DOC.replace("scripts/a.sh\n", "scripts/a.sh\nscripts/b.sh\nscripts/c.sh\n"))
	result = _check(repo, base, head)
	assert result.returncode == 0, result.stdout


def test_recreated_repo_tree_heading_does_not_exempt_new_prose(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path)
	head = _commit(repo, "agents.md", BASE_DOC.replace(
		"## Stable log prefixes (contractual)\n",
		"## Repo-tree (auto-generated)\n\nNew prose.\n\n## Stable log prefixes (contractual)\n"))
	result = _check(repo, base, head)
	assert result.returncode == 1, result.stdout
	assert "AGENTS_MD_DIRECT_EDIT outcome=violation" in result.stdout


def test_prose_after_tree_end_marker_is_not_exempt(tmp_path: Path) -> None:
	"""`make generate` rewrites only the lines between TREE markers, so prose
	added elsewhere in the Repo-tree section survives and must be checked."""
	repo, base = _repo(tmp_path)
	head = _commit(repo, "agents.md", BASE_DOC.replace("<!-- TREE:END -->\n", "<!-- TREE:END -->\n\nNew prose.\n"))
	result = _check(repo, base, head)
	assert result.returncode == 1, result.stdout
	assert "under '## Repo-tree (auto-generated)'" in result.stdout


def test_non_ascii_digit_limit_falls_back_to_default(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path)
	head = _commit(repo, "agents.md", BASE_DOC.replace("Line two.\n", "Line two, corrected.\n"))
	result = _check(repo, base, head, AGENTS_MD_DIRECT_NET_ADD_MAX="\u00b2")
	assert result.returncode == 0, result.stdout + result.stderr
	assert "::warning::AGENTS_MD_DIRECT_NET_ADD_MAX" in result.stdout


def test_fragment_only_change_passes(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path)
	head = _commit(repo, "agents.d/1-new.md", '<!-- agents: section="Workflow architecture" -->\nNew.\n')
	result = _check(repo, base, head)
	assert result.returncode == 0, result.stdout
	assert "outcome=ok net_added=none" in result.stdout


def test_uppercase_agents_file_is_covered(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path, "AGENTS.md")
	head = _commit(repo, "AGENTS.md", BASE_DOC.replace("Line three.\n", "Line three.\nAppended.\n"))
	result = _check(repo, base, head)
	assert result.returncode == 1, result.stdout
	assert "AGENTS.md:" in result.stdout


def test_invalid_refs_are_a_usage_error(tmp_path: Path) -> None:
	repo, _base = _repo(tmp_path)
	result = _check(repo, "main", "HEAD")
	assert result.returncode == 2
	assert "outcome=error reason=invalid_ref" in result.stdout


def test_ci_runs_the_check_on_pull_requests_except_project_final_prs() -> None:
	jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]
	step = next(step for step in jobs["static-checks"]["steps"] if step.get("name") == "agents.md direct-edit check (CLAUDE.md §30)")
	assert step["if"] == (
		"github.event_name == 'pull_request' && !(github.event.pull_request.head.repo.full_name == github.repository"
		" && github.base_ref == github.event.repository.default_branch"
		" && (startsWith(github.head_ref, 'orchestrator/project-') || startsWith(github.head_ref, 'auto/forward-merge-stable-')))"
	)
	assert 'git fetch --no-tags --depth=2 origin "${GITHUB_SHA}"' in step["run"]
	assert 'scripts/check_agents_md_direct_edits.py' in step["run"]
	assert '--base "$(git rev-parse "${GITHUB_SHA}^1")" --head "${GITHUB_SHA}"' in step["run"]


def _collapsed(text: str) -> str:
	return " ".join(text.split())


def test_prompts_the_pipeline_sends_name_the_fragment_rule() -> None:
	"""The implement prompt `implement.yml` actually sends is the inline
	`mode-implement-inline.txt` heredoc, which never mentioned agents.d/;
	`prompts/mode-implement.txt` did, but is not what the workflow sends."""
	workflow = (ROOT / ".github" / "workflows" / "implement.yml").read_text(encoding="utf-8")
	inline_parts = workflow.split('cat > "${PROMPT_TEMPLATE_FILE}" <<\'EOF\'', 1)[1].split("\n          EOF\n", 1)
	assert len(inline_parts) == 2, "inline implement prompt heredoc terminator not found"
	inline_prompt = inline_parts[0]
	editor = (ROOT / "scripts" / "review_apply_fixes.sh").read_text(encoding="utf-8")
	editor_rule = editor.split("AGENTS.MD DISCIPLINE", 1)[1].split("EDITOR EXECUTION GUARDRAILS", 1)[0]
	unattended = (ROOT / "unattended_system_instructions.md").read_text(encoding="utf-8")
	unattended_rule = unattended.split("## §25. agents.md Fragments", 1)[1]
	for label, text in (("inline implement prompt", inline_prompt), ("review editor prompt", editor_rule), ("unattended instructions", unattended_rule)):
		flat = _collapsed(text)
		assert "agents.d/<issue-or-pr>-<slug>.md" in flat, label
		assert '<!-- agents: section="<exact ## heading>" -->' in flat, label
		assert "only to correct or remove existing text" in flat, label
	# The editor prompt is an unquoted heredoc: a backtick or `$` would run.
	assert "`" not in editor_rule and "$" not in editor_rule
