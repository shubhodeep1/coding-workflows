#!/usr/bin/env python3
"""Coverage for scripts/assemble_changelog.py — the changelog.d fragment assembler.

Includes the acceptance demonstration for the mechanism itself: two
simultaneously-open PRs that both add a changelog entry produce zero
conflicting paths, verified by running real `git merge` invocations rather
than by assertion.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ASSEMBLER = REPO_ROOT / "scripts" / "assemble_changelog.py"
UPSTREAM_CHANGELOG = REPO_ROOT / "CHANGELOG.md"

KAC_CHANGELOG = """# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- Existing added entry.

### Fixed

- Existing fixed entry.

## [v1.1.0] - 2026-03-22

- Released entry.
"""

DATE_CHANGELOG = """# Changelog

## 2026-08-06

- Existing same-day entry.

## 2026-08-05

- Older entry.
"""


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		[sys.executable, str(ASSEMBLER), *args],
		cwd=str(cwd) if cwd else None,
		capture_output=True,
		text=True,
		env=env,
		check=False,
	)


def _fragment(root: Path, name: str, body: str) -> Path:
	path = root / "changelog.d" / name
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(body, encoding="utf-8")
	return path


def _removed_lines(before: str, after: str) -> list[str]:
	"""Lines present in ``before`` that the assembler dropped.

	A multiset comparison, so a line that merely moved is not reported. The
	assembler's core guarantee is that assembly is purely additive.
	"""
	remaining = list(after.split("\n"))
	removed: list[str] = []
	for line in before.split("\n"):
		if line in remaining:
			remaining.remove(line)
		else:
			removed.append(line)
	return removed


# ──────────────────────────────────────────────────────────────────
# assemble — no-op and idempotency
# ──────────────────────────────────────────────────────────────────


def test_no_fragments_leaves_changelog_byte_identical() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		changelog = root / "CHANGELOG.md"
		changelog.write_text(UPSTREAM_CHANGELOG.read_text(encoding="utf-8"), encoding="utf-8")
		before = changelog.read_bytes()

		result = _run("assemble", "--repo-root", str(root))

		assert result.returncode == 0, result.stderr
		assert changelog.read_bytes() == before
		assert "changed=false" in result.stdout


def test_repeated_assembly_is_idempotent() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(KAC_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-one.md", "- New entry.\n")

		assert _run("assemble", "--repo-root", str(root)).returncode == 0
		after_first = (root / "CHANGELOG.md").read_bytes()
		assert _run("assemble", "--repo-root", str(root)).returncode == 0

		assert (root / "CHANGELOG.md").read_bytes() == after_first


# ──────────────────────────────────────────────────────────────────
# assemble — Keep a Changelog layout
# ──────────────────────────────────────────────────────────────────


def test_keep_a_changelog_appends_to_existing_subsection() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(KAC_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-fix.md", "<!-- changelog: fixed -->\n- Freshly fixed thing.\n")

		result = _run("assemble", "--repo-root", str(root))
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert result.returncode == 0, result.stderr
		assert "layout=keep-a-changelog" in result.stdout
		assert _removed_lines(KAC_CHANGELOG, text) == []
		# Landed inside the existing ### Fixed block, not a duplicate heading.
		assert text.count("### Fixed") == 1
		fixed_at = text.index("### Fixed")
		released_at = text.index("## [v1.1.0]")
		assert fixed_at < text.index("- Freshly fixed thing.") < released_at


def test_keep_a_changelog_creates_missing_subsection_in_canonical_order() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(KAC_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-sec.md", "<!-- changelog: security -->\n- Hardened thing.\n")
		_fragment(root, "11-chg.md", "<!-- changelog: changed -->\n- Altered thing.\n")

		result = _run("assemble", "--repo-root", str(root))
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert result.returncode == 0, result.stderr
		assert _removed_lines(KAC_CHANGELOG, text) == []
		# Keep a Changelog order: Added, Changed, …, Fixed, Security.
		assert text.index("### Added") < text.index("### Changed") < text.index("### Fixed")
		assert text.index("### Fixed") < text.index("### Security")
		# Both new subsections stay inside the Unreleased block.
		assert text.index("### Security") < text.index("## [v1.1.0]")


def test_keep_a_changelog_creates_unreleased_section_when_absent() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		source = "# Changelog\n\n## [v1.0.0] - 2026-01-01\n\n- Released entry.\n"
		(root / "CHANGELOG.md").write_text(source, encoding="utf-8")
		_fragment(root, "10-one.md", "- Brand new entry.\n")

		result = _run("assemble", "--repo-root", str(root))
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert result.returncode == 0, result.stderr
		assert _removed_lines(source, text) == []
		assert text.index("## [Unreleased]") < text.index("## [v1.0.0]")
		assert text.index("- Brand new entry.") < text.index("## [v1.0.0]")


def test_fragment_body_headings_are_not_treated_as_categories() -> None:
	"""Regression: §20's own template ends every entry with `### For contributors`.

	Once such a fragment is folded, a boundary scanner that accepted any
	`### Word` would treat that prose heading as a category marker — appending
	the next release's Added entry into the middle of the previous entry, and
	creating new categories out of canonical order. Two folds are required to
	surface it: the first plants the heading, the second trips over it.
	"""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(KAC_CHANGELOG, encoding="utf-8")

		_fragment(
			root,
			"10-first.md",
			"<!-- changelog: added -->\n"
			"- **First entry.**\n\nWhat this means for operators: something.\n\n"
			"### For contributors\n\nImplementation notes.\n",
		)
		assert _run("assemble", "--repo-root", str(root)).returncode == 0
		after_first = (root / "CHANGELOG.md").read_text(encoding="utf-8")
		assert "### For contributors" in after_first

		_fragment(root, "20-second.md", "<!-- changelog: added -->\n- **Second added entry.**\n")
		_fragment(root, "21-third.md", "<!-- changelog: deprecated -->\n- **Something deprecated.**\n")
		assert _run("assemble", "--repo-root", str(root)).returncode == 0
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert _removed_lines(after_first, text) == []
		# The new Added entry belongs to the Added category — above ### Changed,
		# not spliced into the previous entry's contributor notes.
		assert text.index("- **Second added entry.**") < text.index("### For contributors")
		# Deprecated lands in canonical order between Added and Fixed, not
		# wherever the prose heading happened to sit.
		assert text.index("### Added") < text.index("### Deprecated") < text.index("### Fixed")
		assert text.index("### Deprecated") < text.index("## [v1.1.0]")


def test_fragment_body_h2_does_not_truncate_the_unreleased_block() -> None:
	"""Same failure mode one level up: a `## ` line inside a folded entry."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(KAC_CHANGELOG, encoding="utf-8")
		_fragment(
			root,
			"10-first.md",
			"<!-- changelog: added -->\n- **Entry with a prose H2.**\n\n## Background\n\nDetail.\n",
		)
		assert _run("assemble", "--repo-root", str(root)).returncode == 0
		after_first = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		_fragment(root, "20-second.md", "<!-- changelog: fixed -->\n- **Later fix.**\n")
		assert _run("assemble", "--repo-root", str(root)).returncode == 0
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert _removed_lines(after_first, text) == []
		# The fix joins the real ### Fixed section, past the prose H2.
		assert text.index("## Background") < text.index("- **Later fix.**")
		assert text.index("### Fixed") < text.index("- **Later fix.**")
		assert text.index("- **Later fix.**") < text.index("## [v1.1.0]")


def test_new_categories_are_created_in_canonical_order_at_block_end() -> None:
	"""Pins the _apply_insertions tie-break: two categories, same insertion index."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		source = "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- Existing.\n\n## [v1.0.0] - 2026-01-01\n\n- Released.\n"
		(root / "CHANGELOG.md").write_text(source, encoding="utf-8")
		_fragment(root, "10-sec.md", "<!-- changelog: security -->\n- Sec entry.\n")
		_fragment(root, "11-dep.md", "<!-- changelog: deprecated -->\n- Dep entry.\n")

		assert _run("assemble", "--repo-root", str(root)).returncode == 0
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert _removed_lines(source, text) == []
		assert text.index("### Added") < text.index("### Deprecated") < text.index("### Security")
		assert text.index("### Security") < text.index("## [v1.0.0]")


def test_crlf_changelog_keeps_its_line_endings() -> None:
	"""Rewriting CRLF as LF would change every line — and, with merge=union,
	let the next merge union two files that differ everywhere."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		changelog = root / "CHANGELOG.md"
		changelog.write_bytes(KAC_CHANGELOG.replace("\n", "\r\n").encode("utf-8"))
		_fragment(root, "10-one.md", "<!-- changelog: fixed -->\n- CRLF entry.\n")

		result = _run("assemble", "--repo-root", str(root))
		raw = changelog.read_bytes()

		assert result.returncode == 0, result.stderr
		assert b"\r\n" in raw
		assert raw.replace(b"\r\n", b"") .count(b"\n") == 0, "stray LF-only line survived"
		assert "- CRLF entry." in raw.decode("utf-8")
		assert _removed_lines(KAC_CHANGELOG, raw.decode("utf-8").replace("\r\n", "\n")) == []


def test_crlf_gitattributes_keeps_its_line_endings() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		attributes = root / ".gitattributes"
		attributes.write_bytes(b"*.png binary\r\n")

		assert _run("ensure-assets", "--repo-root", str(root)).returncode == 0
		raw = attributes.read_bytes()

		assert b"\r\n" in raw
		assert raw.replace(b"\r\n", b"").count(b"\n") == 0
		assert b"CHANGELOG.md merge=union" in raw


def test_upstream_changelog_assembly_removes_no_line() -> None:
	"""The real repo CHANGELOG.md, not a fixture — additive on live content."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		source = UPSTREAM_CHANGELOG.read_text(encoding="utf-8")
		(root / "CHANGELOG.md").write_text(source, encoding="utf-8")
		_fragment(root, "10-a.md", "<!-- changelog: fixed -->\n- Entry A.\n")
		_fragment(root, "11-b.md", "- Entry B.\n")
		_fragment(root, "12-c.md", "<!-- changelog: security -->\n- Entry C.\n")

		result = _run("assemble", "--repo-root", str(root))
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert result.returncode == 0, result.stderr
		assert _removed_lines(source, text) == []
		for marker in ("- Entry A.", "- Entry B.", "- Entry C."):
			assert marker in text


# ──────────────────────────────────────────────────────────────────
# assemble — date-heading layout (consumer repos)
# ──────────────────────────────────────────────────────────────────


def test_date_headings_append_to_matching_day() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(DATE_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-one.md", "- Same-day addition.\n")

		result = _run("assemble", "--repo-root", str(root), "--date", "2026-08-06")
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert result.returncode == 0, result.stderr
		assert "layout=date-headings" in result.stdout
		assert _removed_lines(DATE_CHANGELOG, text) == []
		assert text.count("## 2026-08-06") == 1
		assert text.index("- Same-day addition.") < text.index("## 2026-08-05")


def test_date_headings_open_new_day_at_top() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(DATE_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-one.md", "- Newest entry.\n")

		result = _run("assemble", "--repo-root", str(root), "--date", "2026-08-07")
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert result.returncode == 0, result.stderr
		assert _removed_lines(DATE_CHANGELOG, text) == []
		assert text.index("## 2026-08-07") < text.index("## 2026-08-06")
		assert text.index("- Newest entry.") < text.index("## 2026-08-06")


def test_category_marker_ignored_in_date_heading_repo() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(DATE_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-one.md", "<!-- changelog: security -->\n- Hardened thing.\n")

		assert _run("assemble", "--repo-root", str(root), "--date", "2026-08-06").returncode == 0
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert "### Security" not in text
		assert "<!-- changelog:" not in text
		assert "- Hardened thing." in text


def test_missing_changelog_is_created_in_date_heading_layout() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		_fragment(root, "10-one.md", "- Bootstrap entry.\n")

		result = _run("assemble", "--repo-root", str(root), "--date", "2026-08-06")
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert result.returncode == 0, result.stderr
		assert text.startswith("# Changelog")
		assert "## 2026-08-06" in text
		assert "- Bootstrap entry." in text


# ──────────────────────────────────────────────────────────────────
# assemble — fragment discovery, parsing, lifecycle
# ──────────────────────────────────────────────────────────────────


def test_fragments_are_folded_in_filename_order() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(DATE_CHANGELOG, encoding="utf-8")
		_fragment(root, "30-third.md", "- Third.\n")
		_fragment(root, "10-first.md", "- First.\n")
		_fragment(root, "20-second.md", "- Second.\n")

		assert _run("assemble", "--repo-root", str(root), "--date", "2026-08-06").returncode == 0
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert text.index("- First.") < text.index("- Second.") < text.index("- Third.")


def test_folded_fragments_are_deleted_and_scaffolding_survives() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(DATE_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-one.md", "- Entry.\n")
		_fragment(root, ".gitkeep", "")
		_fragment(root, "README.md", "How to write a fragment.\n")
		_fragment(root, "notes.txt", "not a fragment\n")

		assert _run("assemble", "--repo-root", str(root), "--date", "2026-08-06").returncode == 0
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert not (root / "changelog.d" / "10-one.md").exists()
		assert (root / "changelog.d" / ".gitkeep").exists()
		assert (root / "changelog.d" / "README.md").exists()
		assert (root / "changelog.d" / "notes.txt").exists()
		assert "How to write a fragment." not in text
		assert "not a fragment" not in text


def test_unknown_category_marker_falls_back_to_added() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(KAC_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-one.md", "<!-- changelog: bogus -->\n- Survived the typo.\n")

		assert _run("assemble", "--repo-root", str(root)).returncode == 0
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert "### Bogus" not in text
		added_at = text.index("### Added")
		assert added_at < text.index("- Survived the typo.") < text.index("### Fixed")


def test_empty_fragment_injects_nothing_but_is_cleaned_up() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(DATE_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-empty.md", "\n\n")

		result = _run("assemble", "--repo-root", str(root), "--date", "2026-08-06")

		assert result.returncode == 0, result.stderr
		assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == DATE_CHANGELOG
		assert not (root / "changelog.d" / "10-empty.md").exists()
		# The deletion is a real working-tree change the caller must stage,
		# otherwise the consumer sync re-deletes the same file every run.
		assert "changed=true" in result.stdout
		assert "had no body" in result.stdout


def test_dry_run_writes_nothing() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(DATE_CHANGELOG, encoding="utf-8")
		fragment = _fragment(root, "10-one.md", "- Entry.\n")

		result = _run("assemble", "--repo-root", str(root), "--date", "2026-08-06", "--dry-run")

		assert result.returncode == 0, result.stderr
		assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == DATE_CHANGELOG
		assert fragment.exists()


def test_github_output_flags_are_emitted() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(DATE_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-one.md", "- Entry.\n")
		output_file = root / "gh_output"
		output_file.write_text("", encoding="utf-8")

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["GITHUB_OUTPUT"] = str(output_file)
		result = subprocess.run(
			[
				sys.executable,
				str(ASSEMBLER),
				"assemble",
				"--repo-root",
				str(root),
				"--date",
				"2026-08-06",
			],
			capture_output=True,
			text=True,
			env=env,
			check=False,
		)
		emitted = output_file.read_text(encoding="utf-8")

		assert result.returncode == 0, result.stderr
		assert "changelog_assembled=true" in emitted
		assert "changelog_fragment_count=1" in emitted
		assert "changelog_layout=date-headings" in emitted


def test_changed_paths_file_lists_staging_targets() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "CHANGELOG.md").write_text(DATE_CHANGELOG, encoding="utf-8")
		_fragment(root, "10-one.md", "- Entry.\n")
		listing = root / "changed.txt"

		result = _run(
			"assemble",
			"--repo-root",
			str(root),
			"--date",
			"2026-08-06",
			"--changed-paths-file",
			str(listing),
		)
		paths = listing.read_text(encoding="utf-8").split()

		assert result.returncode == 0, result.stderr
		assert "CHANGELOG.md" in paths
		assert "changelog.d" in paths


def test_bad_date_argument_is_rejected() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result = _run("assemble", "--repo-root", tmp, "--date", "not-a-date")
		assert result.returncode == 1
		assert "not YYYY-MM-DD" in result.stderr


# ──────────────────────────────────────────────────────────────────
# ensure-assets
# ──────────────────────────────────────────────────────────────────


def test_ensure_assets_creates_gitkeep_and_managed_block() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)

		result = _run("ensure-assets", "--repo-root", str(root))
		attributes = (root / ".gitattributes").read_text(encoding="utf-8")

		assert result.returncode == 0, result.stderr
		assert (root / "changelog.d" / ".gitkeep").is_file()
		assert "CHANGELOG.md merge=union" in attributes
		assert "BEGIN coding-workflows changelog assets" in attributes
		assert "END coding-workflows changelog assets" in attributes


def test_ensure_assets_is_idempotent() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		assert _run("ensure-assets", "--repo-root", str(root)).returncode == 0
		before = (root / ".gitattributes").read_bytes()

		result = _run("ensure-assets", "--repo-root", str(root))

		assert result.returncode == 0, result.stderr
		assert "changed=0" in result.stdout
		assert (root / ".gitattributes").read_bytes() == before


def test_ensure_assets_preserves_consumer_local_attribute_rules() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / ".gitattributes").write_text("*.png binary\n*.lock -diff\n", encoding="utf-8")

		assert _run("ensure-assets", "--repo-root", str(root)).returncode == 0
		attributes = (root / ".gitattributes").read_text(encoding="utf-8")

		assert "*.png binary" in attributes
		assert "*.lock -diff" in attributes
		assert "CHANGELOG.md merge=union" in attributes


def test_ensure_assets_refreshes_a_stale_managed_block() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / ".gitattributes").write_text(
			"*.png binary\n"
			"# BEGIN coding-workflows changelog assets — managed by update_workflows.yml\n"
			"CHANGELOG.md merge=stale-driver\n"
			"# END coding-workflows changelog assets\n",
			encoding="utf-8",
		)

		result = _run("ensure-assets", "--repo-root", str(root))
		attributes = (root / ".gitattributes").read_text(encoding="utf-8")

		assert result.returncode == 0, result.stderr
		assert "merge=stale-driver" not in attributes
		assert "CHANGELOG.md merge=union" in attributes
		assert "*.png binary" in attributes
		assert attributes.count("BEGIN coding-workflows changelog assets") == 1


def test_ensure_assets_changed_paths_file_feeds_the_sync_step() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		listing = root / "changed.txt"

		result = _run(
			"ensure-assets", "--repo-root", str(root), "--changed-paths-file", str(listing)
		)
		paths = listing.read_text(encoding="utf-8").split()

		assert result.returncode == 0, result.stderr
		assert "changelog.d/.gitkeep" in paths
		assert ".gitattributes" in paths


# ──────────────────────────────────────────────────────────────────
# Acceptance demonstration — two concurrent PRs, zero conflicting paths
# ──────────────────────────────────────────────────────────────────


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["HOME"] = str(root)
	env["GIT_CONFIG_NOSYSTEM"] = "1"
	env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "test"
	env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
	return subprocess.run(
		["git", *args],
		cwd=str(root),
		capture_output=True,
		text=True,
		env=env,
		check=False,
	)


def _init_repo(root: Path, changelog: str) -> None:
	_git(root, "init", "-q", "-b", "main")
	(root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
	_git(root, "add", "-A")
	_git(root, "commit", "-q", "-m", "base")


def _branch_commit(root: Path, branch: str, write: "callable[[Path], None]", message: str) -> None:
	_git(root, "checkout", "-q", "-b", branch, "main")
	write(root)
	_git(root, "add", "-A")
	_git(root, "commit", "-q", "-m", message)
	_git(root, "checkout", "-q", "main")


def test_concurrent_prs_using_fragments_have_zero_conflicting_paths() -> None:
	"""The acceptance criterion, demonstrated rather than asserted.

	Two PRs branch from the same base and each add a changelog entry the way
	CLAUDE.md §20 prescribes. Both merges are run for real; neither conflicts,
	and the two PRs share no changed path at all.
	"""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		_init_repo(root, DATE_CHANGELOG)

		def pr_one(repo: Path) -> None:
			_fragment(repo, "4001-first-feature.md", "- First PR entry.\n")

		def pr_two(repo: Path) -> None:
			_fragment(repo, "4002-second-feature.md", "- Second PR entry.\n")

		_branch_commit(root, "pr-one", pr_one, "pr one")
		_branch_commit(root, "pr-two", pr_two, "pr two")

		one_paths = set(_git(root, "diff", "--name-only", "main", "pr-one").stdout.split())
		two_paths = set(_git(root, "diff", "--name-only", "main", "pr-two").stdout.split())
		assert one_paths & two_paths == set(), "concurrent PRs must share no changed path"

		first = _git(root, "merge", "--no-edit", "pr-one")
		second = _git(root, "merge", "--no-edit", "pr-two")

		assert first.returncode == 0, first.stdout + first.stderr
		assert second.returncode == 0, second.stdout + second.stderr
		assert _git(root, "diff", "--name-only", "--diff-filter=U").stdout.strip() == ""

		# And assembly folds both, in filename order, into one changelog.
		assert _run("assemble", "--repo-root", str(root), "--date", "2026-08-06").returncode == 0
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
		assert text.index("- First PR entry.") < text.index("- Second PR entry.")


def test_control_direct_changelog_edits_do_conflict() -> None:
	"""The behaviour the fragment mechanism exists to eliminate.

	Same two PRs, but editing CHANGELOG.md directly the way agents did before
	§20 was rewritten. Without this control the test above would pass just as
	well against a repo where nothing conflicts anyway.
	"""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		_init_repo(root, DATE_CHANGELOG)

		def direct(entry: str) -> "callable[[Path], None]":
			def write(repo: Path) -> None:
				body = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
				head, rest = body.split("\n\n", 1)
				(repo / "CHANGELOG.md").write_text(
					f"{head}\n\n## 2026-08-07\n\n{entry}\n\n{rest}", encoding="utf-8"
				)

			return write

		_branch_commit(root, "pr-one", direct("- First PR entry."), "pr one")
		_branch_commit(root, "pr-two", direct("- Second PR entry."), "pr two")

		one_paths = set(_git(root, "diff", "--name-only", "main", "pr-one").stdout.split())
		two_paths = set(_git(root, "diff", "--name-only", "main", "pr-two").stdout.split())
		assert one_paths & two_paths == {"CHANGELOG.md"}

		assert _git(root, "merge", "--no-edit", "pr-one").returncode == 0
		second = _git(root, "merge", "--no-edit", "pr-two")

		assert second.returncode != 0, "direct CHANGELOG.md edits are expected to conflict"
		assert "CHANGELOG.md" in _git(root, "diff", "--name-only", "--diff-filter=U").stdout


def test_union_backstop_prevents_the_direct_edit_conflict() -> None:
	"""The .gitattributes safety net, on the same control scenario."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		_init_repo(root, DATE_CHANGELOG)
		assert _run("ensure-assets", "--repo-root", str(root)).returncode == 0
		_git(root, "add", "-A")
		_git(root, "commit", "-q", "-m", "changelog assets")

		def direct(entry: str) -> "callable[[Path], None]":
			def write(repo: Path) -> None:
				body = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
				head, rest = body.split("\n\n", 1)
				(repo / "CHANGELOG.md").write_text(
					f"{head}\n\n## 2026-08-07\n\n{entry}\n\n{rest}", encoding="utf-8"
				)

			return write

		_branch_commit(root, "pr-one", direct("- First PR entry."), "pr one")
		_branch_commit(root, "pr-two", direct("- Second PR entry."), "pr two")

		assert _git(root, "merge", "--no-edit", "pr-one").returncode == 0
		second = _git(root, "merge", "--no-edit", "pr-two")
		text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

		assert second.returncode == 0, second.stdout + second.stderr
		assert "- First PR entry." in text
		assert "- Second PR entry." in text
		assert "<<<<<<<" not in text and ">>>>>>>" not in text
		# The union driver aligns the identical heading as common context, so
		# this shape does not duplicate it. That is a property of the diff, not
		# a guarantee: union is textual and has no idea what a changelog entry
		# is, so entry order follows merge order rather than intent. Fragments
		# remain the mechanism; this is only the net under it.
		assert text.count("## 2026-08-07") == 1


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"OK: {len(tests)} assemble_changelog tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
