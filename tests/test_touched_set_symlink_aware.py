"""The resolver / editor touched-set detection must compare symlinks by their
link text, never by the file they point to.

Regression for PR #4443 (runs 36101772827 … 36120214576): merging main
conflicted in ``CLAUDE.md``. The resolver fixed it, but
``workflow-templates/CLAUDE.md`` is a symlink to ``../CLAUDE.md`` and the
pre/post snapshot hashed it with ``git hash-object -- <path>``, which follows
the link. The symlink therefore "changed" with its target, the path landed in
the touched set, ``check_resolver_diff.sh`` rejected the correct resolution as
out-of-scope, and every retry failed identically until the fingerprint cap
labelled the PR ``ai:review-blocked``.

The tests extract the snapshot loop (``review_conflict_prepare.sh`` and the
pre-editor snapshot in ``review_autofix.yml``) and the compare loop
(``review_conflict_resolve.sh`` and ``review_commit_changes.sh``) and run them
against a throwaway repo.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PREPARE = ROOT / "scripts" / "review_conflict_prepare.sh"
RESOLVE = ROOT / "scripts" / "review_conflict_resolve.sh"
COMMIT = ROOT / "scripts" / "review_commit_changes.sh"
AUTOFIX = ROOT / ".github" / "workflows" / "review_autofix.yml"

SNAPSHOT_RE = re.compile(
	r"(?P<block>[ \t]*while IFS= read -r -d '' snap_path; do\n.*?[ \t]*done < <\(git ls-files -z \| sort -zu\)\n)",
	re.DOTALL,
)


def _snapshot_block(path: Path, state_var: str) -> str:
	text = path.read_text(encoding="utf-8")
	for m in SNAPSHOT_RE.finditer(text):
		if f'"${{{state_var}}}"' in m.group("block"):
			return _dedent(m.group("block"))
	raise AssertionError(f"snapshot loop for {state_var} not found in {path.name}")


def _compare_block(path: Path, state_var: str) -> str:
	text = path.read_text(encoding="utf-8")
	m = re.search(
		r"([ \t]*while IFS=\$'\\t' read -r diff_kind diff_a diff_b diff_c; do\n.*?[ \t]*done < \"\$\{"
		+ state_var
		+ r"\}\"\n)",
		text,
		re.DOTALL,
	)
	assert m, f"compare loop for {state_var} not found in {path.name}"
	return _dedent(m.group(1))


def _dedent(block: str) -> str:
	lines = block.splitlines()
	indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
	return "\n".join(line[indent:] for line in lines) + "\n"


def _git(repo: Path, *args: str) -> str:
	return subprocess.run(
		["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
		cwd=repo,
		check=True,
		capture_output=True,
		text=True,
	).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	(repo / "CLAUDE.md").write_text("rules v1\n")
	(repo / "workflow-templates").mkdir()
	(repo / "workflow-templates" / "CLAUDE.md").symlink_to("../CLAUDE.md")
	(repo / "other.md").write_text("other\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "init")
	return repo


def _touched(repo: Path, snapshot: str, compare: str, state_var: str, out_var: str, mutate: str) -> list[str]:
	script = f"""set -euo pipefail
{state_var}="$PWD/../state.tsv"
: > "${{{state_var}}}"
{snapshot}
{mutate}
{out_var}="$PWD/../touched.txt"
: > "${{{out_var}}}"
PRE_UNTRACKED_LIST="$PWD/../pre_untracked.txt"
: > "${{PRE_UNTRACKED_LIST}}"
{compare}
sort -u "${{{out_var}}}"
"""
	result = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	return [line for line in result.stdout.splitlines() if line]


SITES = [
	pytest.param(PREPARE, "PRE_RESOLVER_STATE_FILE", RESOLVE, "RESOLVER_TOUCHED_FILE", id="resolver"),
	pytest.param(AUTOFIX, "PRE_EDITOR_STATE_FILE", COMMIT, "CODEX_TOUCHED_FILE", id="editor"),
]


@pytest.mark.parametrize(("snap_src", "state_var", "cmp_src", "out_var"), SITES)
def test_symlink_to_edited_file_is_not_touched(repo, snap_src, state_var, cmp_src, out_var):
	touched = _touched(
		repo,
		_snapshot_block(snap_src, state_var),
		_compare_block(cmp_src, state_var),
		state_var,
		out_var,
		"printf 'rules v2\\n' > CLAUDE.md",
	)
	assert touched == ["CLAUDE.md"]


@pytest.mark.parametrize(("snap_src", "state_var", "cmp_src", "out_var"), SITES)
def test_retargeted_symlink_is_touched(repo, snap_src, state_var, cmp_src, out_var):
	touched = _touched(
		repo,
		_snapshot_block(snap_src, state_var),
		_compare_block(cmp_src, state_var),
		state_var,
		out_var,
		"ln -sfn ../other.md workflow-templates/CLAUDE.md",
	)
	assert touched == ["workflow-templates/CLAUDE.md"]


@pytest.mark.parametrize(("snap_src", "state_var", "cmp_src", "out_var"), SITES)
def test_deleted_symlink_is_touched(repo, snap_src, state_var, cmp_src, out_var):
	touched = _touched(
		repo,
		_snapshot_block(snap_src, state_var),
		_compare_block(cmp_src, state_var),
		state_var,
		out_var,
		"rm workflow-templates/CLAUDE.md",
	)
	assert touched == ["workflow-templates/CLAUDE.md"]


def test_symlink_snapshot_hash_matches_git_index(repo):
	snapshot = _snapshot_block(PREPARE, "PRE_RESOLVER_STATE_FILE")
	script = f'set -euo pipefail\nPRE_RESOLVER_STATE_FILE="$PWD/../s.tsv"\n: > "$PRE_RESOLVER_STATE_FILE"\n{snapshot}cat "$PRE_RESOLVER_STATE_FILE"\n'
	out = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=True).stdout
	rows = {line.split("\t")[3]: line.split("\t")[1:3] for line in out.splitlines()}
	index_sha = _git(repo, "ls-files", "-s", "workflow-templates/CLAUDE.md").split()[1]
	assert rows["workflow-templates/CLAUDE.md"] == [index_sha, "0"]


def test_snapshot_loops_stay_identical():
	# The resolver prepare step and the pre-editor snapshot must hash the tree
	# the same way; a fix to one without the other reopens this bug.
	resolver = _snapshot_block(PREPARE, "PRE_RESOLVER_STATE_FILE").replace("PRE_RESOLVER_STATE_FILE", "STATE")
	editor = _snapshot_block(AUTOFIX, "PRE_EDITOR_STATE_FILE").replace("PRE_EDITOR_STATE_FILE", "STATE")
	assert resolver == editor


def test_no_symlink_blind_hash_object_on_tree_paths():
	# Every `git hash-object -- <tree path>` in these touched-set paths must sit
	# in the non-symlink branch of an `if [ -L … ]`.
	for path in (PREPARE, RESOLVE, COMMIT, AUTOFIX):
		text = path.read_text(encoding="utf-8")
		for m in re.finditer(r'git hash-object -- "\$\{(snap_path|diff_path)\}"', text):
			window = text[max(0, m.start() - 600) : m.start()]
			assert 'if [ -L "${' + m.group(1) + '}" ]; then' in window, (path.name, m.group(0))
