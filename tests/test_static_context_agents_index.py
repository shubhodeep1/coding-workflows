"""`build_static_context.sh agents-index` writes a safe, capped agents.md index (issue #8254)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_static_context.sh"
ASSEMBLER = REPO_ROOT / "scripts" / "assemble_agents.py"


def _safe_env(**extra: str) -> dict[str, str]:
	env = os.environ.copy()
	for key in (
		"BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE",
		"GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
		"GIT_ALTERNATE_OBJECT_DIRECTORIES", "STATIC_CONTEXT_AGENTS_MODE", "RUNTIME_DIR",
	):
		env.pop(key, None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.update(extra)
	return env


def _repo(tmp: str, script_source: Path = SCRIPT) -> Path:
	root = Path(tmp)
	(root / "scripts").mkdir()
	shutil.copy2(script_source, root / "scripts" / "build_static_context.sh")
	shutil.copy2(ASSEMBLER, root / "scripts" / "assemble_agents.py")
	return root


def _run(root: Path, phase: str, out: str = "out.txt", **env: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["bash", "scripts/build_static_context.sh", phase, out],
		cwd=root, env=_safe_env(**env), capture_output=True, text=True, check=False,
	)


def _index(root: Path, **env: str) -> tuple[str, str]:
	result = _run(root, "agents-index", **env)
	assert result.returncode == 0, result.stderr
	return (root / "out.txt").read_text(encoding="utf-8"), result.stderr


def _item_lines(text: str) -> list[str]:
	return [line for line in text.splitlines() if line.startswith("- ")]


AGENTS_FIXTURE = (
	"# agents.md\n"  # 1
	"\n"  # 2
	"## First section\n"  # 3
	"text\n"  # 4
	"### Sub heading\n"  # 5
	"```bash\n"  # 6
	"## not a heading\n"  # 7
	"```\n"  # 8
	"## Second section\r\n"  # 9
	"~~~\n"  # 10
	"## fenced too\n"  # 11
	"~~~\n"  # 12
	"#### Variables\n"  # 13
	"## Third section\n"  # 14
)


def test_index_lists_h2_headings_with_raw_line_numbers() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text(AGENTS_FIXTURE, encoding="utf-8")
		text, _ = _index(root)
		lines = text.splitlines()
		assert lines[0] == "=== AGENTS.MD INDEX ==="
		assert "The full agents.md is not inlined" in lines[1]
		assert "file tools" in lines[1] and "working tree" in lines[1]
		assert _item_lines(text) == [
			"- L3: First section",
			"- L9: Second section",
			"- L14: Third section",
		]
		assert "not a heading" not in text and "fenced too" not in text
		assert "Sub heading" not in text
		# Deterministic output for the same input.
		again, _ = _index(root)
		assert again == text


def test_empty_agents_file_gives_header_only() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("", encoding="utf-8")
		text, _ = _index(root)
		assert text.startswith("=== AGENTS.MD INDEX ===\n")
		assert _item_lines(text) == []


def test_agents_md_fallback_and_precedence() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "AGENTS.md").write_text("## Upper only\n", encoding="utf-8")
		text, _ = _index(root)
		assert "The full AGENTS.md is not inlined" in text
		assert _item_lines(text) == ["- L1: Upper only"]
		lower = root / "agents.md"
		lower.write_text("\n## Lower wins\n", encoding="utf-8")
		names = {entry.name for entry in root.iterdir()}
		if "AGENTS.md" not in names or "agents.md" not in names:
			pytest.skip("case-insensitive filesystem")
		text, _ = _index(root)
		assert "The full agents.md is not inlined" in text
		assert _item_lines(text) == ["- L2: Lower wins"]


def test_symlinked_agents_file_is_refused() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		git_dir = root / ".git"
		git_dir.mkdir()
		(git_dir / "config").write_text("## x-access-token:SENTINEL\n", encoding="utf-8")
		(root / "agents.md").symlink_to(git_dir / "config")
		text, stderr = _index(root)
		assert text == ""
		assert "SENTINEL" not in text
		assert "::warning::agents.md is a symbolic link" in stderr


def test_no_agents_file_writes_empty_output_without_pipeline_inputs() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		text, _ = _index(root)
		assert (root / "out.txt").exists()
		assert text == ""
		target = root / "target.md"
		target.write_text("x\n", encoding="utf-8")
		(root / "unattended_system_instructions.md").symlink_to(target)
		(root / "ai_pipeline.md").symlink_to(target)
		text, _ = _index(root)
		assert text == ""


def test_agents_d_fragments_listed_with_sections() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("## Workflow architecture\n## Utility helpers\n", encoding="utf-8")
		fragments = root / "agents.d"
		fragments.mkdir()
		(fragments / "b-two.md").write_text(
			'<!-- agents: section="Workflow architecture" -->\ntext\n'
			"```\n"
			'<!-- agents: section="Fenced example" -->\n'
			"```\n"
			'<!-- agents: section="Brand new" new -->\nmore\n',
			encoding="utf-8",
		)
		(fragments / "a-one.md").write_text('<!-- agents: section="Utility helpers" -->\nx\n', encoding="utf-8")
		(fragments / "README.md").write_text('<!-- agents: section="Readme" -->\n', encoding="utf-8")
		(fragments / "notes.txt").write_text("ignored\n", encoding="utf-8")
		outside = root / "outside.md"
		outside.write_text('<!-- agents: section="Linked" -->\nx\n', encoding="utf-8")
		(fragments / "d-link.md").symlink_to(outside)
		text, _ = _index(root)
		assert "appended to the named sections at release time" in text
		assert "not yet in agents.md" in text
		assert _item_lines(text) == [
			"- L1: Workflow architecture",
			"- L2: Utility helpers",
			'- agents.d/a-one.md: section="Utility helpers"',
			'- agents.d/b-two.md: section="Workflow architecture", section="Brand new"',
		]
		assert "Linked" not in text and "Readme" not in text and "Fenced example" not in text


def test_symlinked_agents_d_lists_no_fragments() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("## Only\n", encoding="utf-8")
		real = root / "elsewhere"
		real.mkdir()
		(real / "x.md").write_text('<!-- agents: section="Only" -->\nx\n', encoding="utf-8")
		(root / "agents.d").symlink_to(real)
		text, _ = _index(root)
		assert _item_lines(text) == ["- L1: Only"]
		assert "agents.d/" not in text


def test_unparseable_fragment_drops_fragment_block_only() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("## Kept\n", encoding="utf-8")
		fragments = root / "agents.d"
		fragments.mkdir()
		(fragments / "a-good.md").write_text('<!-- agents: section="Kept" -->\nx\n', encoding="utf-8")
		(fragments / "b-bad.md").write_bytes(b'<!-- agents: section="Kept" -->\n\xff\xfe bad\n')
		text, _ = _index(root)
		assert _item_lines(text) == ["- L1: Kept"]
		assert "agents.d/" not in text


@pytest.mark.parametrize(
	"bad_fragment",
	[
		"no marker here\n",
		'stray text\n<!-- agents: section="Kept" -->\nx\n',
		'<!-- agents: section="Kept" -->\nx\n<!-- agents: broken -->\n',
		'<!-- agents: section="Kept" -->\nx\n## Split heading\n',
		'<!-- agents: section="Kept" -->\n```\nnever closed\n',
		'<!-- agents: section="Kept" -->\n\n',
		'<!-- agents: section="Repo-tree (auto-generated)" -->\nx\n',
	],
	ids=["no_marker", "content_before_marker", "malformed_marker", "heading_in_block", "unclosed_fence", "empty_block", "generated_section"],
)
def test_fragment_the_assembler_rejects_drops_fragment_block(bad_fragment: str) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("## Kept\n", encoding="utf-8")
		fragments = root / "agents.d"
		fragments.mkdir()
		(fragments / "a-good.md").write_text('<!-- agents: section="Kept" -->\nx\n', encoding="utf-8")
		(fragments / "b-bad.md").write_text(bad_fragment, encoding="utf-8")
		text, _ = _index(root)
		assert _item_lines(text) == ["- L1: Kept"]
		assert "agents.d/" not in text


def test_fragment_for_missing_section_drops_fragment_block() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("## Kept\n", encoding="utf-8")
		fragments = root / "agents.d"
		fragments.mkdir()
		(fragments / "a-good.md").write_text('<!-- agents: section="Kept" -->\nx\n', encoding="utf-8")
		(fragments / "b-missing.md").write_text('<!-- agents: section="Not there" -->\nx\n', encoding="utf-8")
		text, _ = _index(root)
		assert _item_lines(text) == ["- L1: Kept"]
		assert "agents.d/" not in text


def test_section_created_by_earlier_new_fragment_is_known() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("##\tTabbed\n", encoding="utf-8")
		fragments = root / "agents.d"
		fragments.mkdir()
		(fragments / "a.md").write_text('<!-- agents: section="Fresh" new -->\nx\n', encoding="utf-8")
		(fragments / "b.md").write_text('<!-- agents: section="Fresh" -->\ny\n<!-- agents: section="Tabbed" -->\nz\n', encoding="utf-8")
		text, _ = _index(root)
		assert _item_lines(text) == [
			"- L1: Tabbed",
			'- agents.d/a.md: section="Fresh"',
			'- agents.d/b.md: section="Fresh", section="Tabbed"',
		]


def test_item_cap_never_leaves_an_empty_fragments_note() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("".join(f"## H{n}\n" for n in range(300)), encoding="utf-8")
		fragments = root / "agents.d"
		fragments.mkdir()
		(fragments / "a.md").write_text('<!-- agents: section="H1" -->\nx\n', encoding="utf-8")
		text, _ = _index(root)
		assert len(_item_lines(text)) == 300
		assert "Pending agents.d/ fragments" not in text
		assert text.splitlines()[-1] == "... (1 more agents.d/ fragments)"


def test_item_cap_counts_headings_and_fragments_separately() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("".join(f"## H{n}\n" for n in range(310)), encoding="utf-8")
		fragments = root / "agents.d"
		fragments.mkdir()
		(fragments / "a.md").write_text('<!-- agents: section="H1" -->\nx\n', encoding="utf-8")
		(fragments / "b.md").write_text('<!-- agents: section="H2" -->\nx\n', encoding="utf-8")
		text, _ = _index(root)
		assert text.splitlines()[-1] == "... (10 more headings, 2 more agents.d/ fragments)"


def test_headings_and_sections_are_neutralised() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		long_heading = "A" * 500
		(root / "agents.md").write_bytes(
			b"## ::error::pwn\n"
			b"## \x1b[31mred\x07bell\xc2\x9bcsi\n"
			b"## " + long_heading.encode() + b"\n"
		)
		fragments = root / "agents.d"
		fragments.mkdir()
		(fragments / "x.md").write_text('<!-- agents: section="::error::bad\x1b[0m" new -->\nx\n', encoding="utf-8")
		text, _ = _index(root)
		for line in text.splitlines():
			assert not line.startswith("::"), line
			assert not line.startswith("##["), line
			assert not re.search("[\x00-\x1f\x7f-\x9f]", line), repr(line)
			assert len(line) <= 200, len(line)
		items = _item_lines(text)
		assert items[0] == "- L1: ::error::pwn"
		assert items[1] == "- L2: [31mredbellcsi"
		assert items[2].startswith("- L3: AAAA") and len(items[2]) == 200
		assert items[3] == '- agents.d/x.md: section="::error::bad[0m"'


def test_index_is_capped_by_item_count() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("".join(f"## H{n}\n" for n in range(400)), encoding="utf-8")
		text, _ = _index(root)
		assert len(_item_lines(text)) == 300
		assert text.splitlines()[-1] == "... (100 more headings)"


def test_index_is_capped_by_size() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("".join(f"## {n} {'x' * 300}\n" for n in range(250)), encoding="utf-8")
		text, _ = _index(root)
		assert len(text.encode("utf-8")) <= 32 * 1024
		printed = len(_item_lines(text))
		assert 0 < printed < 250
		assert text.splitlines()[-1] == f"... ({250 - printed} more headings)"


def _mode_function() -> str:
	source = SCRIPT.read_text(encoding="utf-8")
	match = re.search(r"^_static_context_agents_mode\(\) \{\n.*?^\}\n", source, re.DOTALL | re.MULTILINE)
	assert match, "_static_context_agents_mode not found"
	return match.group(0)


@pytest.mark.parametrize(
	("value", "expected", "warns"),
	[
		(None, "index", False),
		("", "index", False),
		("index", "index", False),
		("INDEX", "index", False),
		("inline", "inline", False),
		("INLINE", "inline", False),
		("garbage$(id)", "inline", True),
	],
)
def test_agents_mode_parser(value: str | None, expected: str, warns: bool) -> None:
	env = _safe_env()
	if value is not None:
		env["STATIC_CONTEXT_AGENTS_MODE"] = value
	result = subprocess.run(
		["bash", "-c", _mode_function() + "_static_context_agents_mode"],
		env=env, capture_output=True, text=True, check=False,
	)
	assert result.returncode == 0
	assert result.stdout == expected + "\n"
	warnings = [line for line in result.stderr.splitlines() if line.startswith("::warning::")]
	assert len(warnings) == (1 if warns else 0)
	if value:
		assert value not in result.stderr


def test_agents_index_phase_ignores_inline_switch() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("## One\n", encoding="utf-8")
		text, _ = _index(root, STATIC_CONTEXT_AGENTS_MODE="inline")
		assert text.startswith("=== AGENTS.MD INDEX ===")


def test_unknown_phase_lists_agents_index() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		_existing_phase_fixture(root)
		result = _run(root, "bogus")
		assert result.returncode == 2
		assert "agents-index" in result.stderr


def _run_named(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["bash", "scripts/build_static_context.sh", *args],
		cwd=root, env=_safe_env(), capture_output=True, text=True, check=False,
	)


def test_agents_index_named_file_overrides_precedence() -> None:
	# Issue #8269: callers that read AGENTS.md first pass it explicitly.
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("## Lower\n", encoding="utf-8")
		(root / "AGENTS.md").write_text("\n## Upper\n", encoding="utf-8")
		names = {entry.name for entry in root.iterdir()}
		if "AGENTS.md" not in names or "agents.md" not in names:
			pytest.skip("case-insensitive filesystem")
		result = _run_named(root, "agents-index", "out.txt", "AGENTS.md")
		assert result.returncode == 0, result.stderr
		text = (root / "out.txt").read_text(encoding="utf-8")
		assert "The full AGENTS.md is not inlined" in text
		assert _item_lines(text) == ["- L2: Upper"]
		result = _run_named(root, "agents-index", "out.txt", "agents.md")
		assert result.returncode == 0, result.stderr
		assert _item_lines((root / "out.txt").read_text(encoding="utf-8")) == ["- L1: Lower"]
		# Two arguments keep the old choice: agents.md first.
		before = (root / "out.txt").read_bytes()
		result = _run_named(root, "agents-index", "out.txt")
		assert result.returncode == 0, result.stderr
		assert (root / "out.txt").read_bytes() == before


@pytest.mark.parametrize(
	"args",
	[
		("agents-index", "out.txt", "README.md"),
		("agents-index", "out.txt", "../agents.md"),
		("agents-index", "out.txt", "agents.md", "extra"),
		("readme", "out.txt", "agents.md"),
	],
)
def test_agents_index_rejects_bad_third_argument(args: tuple[str, ...]) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		(root / "agents.md").write_text("## One\n", encoding="utf-8")
		result = _run_named(root, *args)
		assert result.returncode == 2
		assert "agents-index <output-file> [agents.md|AGENTS.md]" in result.stderr
		assert not (root / "out.txt").exists()


def test_agents_index_named_symlink_or_missing_file_is_empty() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		git_dir = root / ".git"
		git_dir.mkdir()
		(git_dir / "config").write_text("## x-access-token:SENTINEL\n", encoding="utf-8")
		(root / "agents.md").write_text("## Regular\n", encoding="utf-8")
		result = _run_named(root, "agents-index", "out.txt", "AGENTS.md")
		assert result.returncode == 0, result.stderr
		assert (root / "out.txt").read_text(encoding="utf-8") == ""
		assert result.stderr.count("::warning::") == 1
		names = {entry.name for entry in root.iterdir()}
		if "AGENTS.md" in names:
			pytest.skip("case-insensitive filesystem")
		(root / "AGENTS.md").symlink_to(git_dir / "config")
		result = _run_named(root, "agents-index", "out.txt", "AGENTS.md")
		assert result.returncode == 0, result.stderr
		assert (root / "out.txt").read_text(encoding="utf-8") == ""
		assert "SENTINEL" not in result.stdout + result.stderr
		assert "::warning::Static context agents index omitted; AGENTS.md" in result.stderr


def _existing_phase_fixture(root: Path) -> str:
	(root / "unattended_system_instructions.md").write_text("system rules\n", encoding="utf-8")
	(root / "ai_pipeline.md").write_text(
		"## Shared Runtime Behavior\nshared\n## Phase 1\none\n## Phase 2\ntwo\n## Phase 3\nthree\n## Failure Handling\nf\n",
		encoding="utf-8",
	)
	agents_text = "# agents\n\n## Section A\nbody a\n\n## Section B\nbody b\n"
	(root / "agents.md").write_text(agents_text, encoding="utf-8")
	(root / "README.md").write_text("# Readme\ntext\n", encoding="utf-8")
	return agents_text


@pytest.mark.parametrize(
	("phase", "modes"),
	[
		# clarify and plan inline agents.md only in inline mode since #8260.
		("clarify", ("inline", "junk", "INLINE")),
		("plan", ("inline", "junk", "INLINE")),
		# implement too since #8262.
		("implement", ("inline", "junk", "INLINE")),
	],
)
def test_existing_phases_still_inline_full_agents_md(phase: str, modes: tuple) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = _repo(tmp)
		agents_text = _existing_phase_fixture(root)
		outputs = []
		for mode in modes:
			env = {} if mode is None else {"STATIC_CONTEXT_AGENTS_MODE": mode}
			result = _run(root, phase, **env)
			assert result.returncode == 0, result.stderr
			outputs.append((root / "out.txt").read_bytes())
		assert all(output == outputs[0] for output in outputs)
		text = outputs[0].decode("utf-8")
		header = "=== REPO-SPECIFIC AGENTS.MD ===\n" if phase == "implement" else "=== AGENTS.MD ===\n"
		assert header + agents_text in text
		assert "=== AGENTS.MD INDEX ===" not in text


def _base_script() -> Path | None:
	"""The script from the merge base with the default branch, when git can find it."""
	for ref in ("origin/main", "isolated-base"):
		base = subprocess.run(
			["git", "merge-base", "HEAD", ref], cwd=REPO_ROOT, env=_safe_env(),
			capture_output=True, text=True, check=False,
		)
		if base.returncode != 0 or not base.stdout.strip():
			continue
		shown = subprocess.run(
			["git", "show", f"{base.stdout.strip()}:scripts/build_static_context.sh"], cwd=REPO_ROOT,
			env=_safe_env(), capture_output=True, check=False,
		)
		if shown.returncode != 0 or b"agents-index" in shown.stdout:
			continue
		with tempfile.NamedTemporaryFile(prefix="build_static_context_base", suffix=".sh", delete=False) as handle:
			handle.write(shown.stdout)
		return Path(handle.name)
	return None


@pytest.mark.parametrize("phase", ["clarify", "plan", "implement", "readme"])
def test_existing_phases_byte_identical_to_base(phase: str) -> None:
	base_script = _base_script()
	if base_script is None:
		pytest.skip("no base revision of build_static_context.sh without agents-index")
	try:
		with tempfile.TemporaryDirectory() as tmp_before, tempfile.TemporaryDirectory() as tmp_after:
			before_root = _repo(tmp_before, script_source=base_script)
			after_root = _repo(tmp_after)
			for root in (before_root, after_root):
				_existing_phase_fixture(root)
				fragments = root / "agents.d"
				fragments.mkdir()
				(fragments / "1-x.md").write_text('<!-- agents: section="Section A" -->\nadded\n', encoding="utf-8")
			# The base script ignores the switch; inline keeps clarify/plan unchanged.
			before = _run(before_root, phase, STATIC_CONTEXT_AGENTS_MODE="inline")
			after = _run(after_root, phase, STATIC_CONTEXT_AGENTS_MODE="inline")
			assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
			assert (before_root / "out.txt").read_bytes() == (after_root / "out.txt").read_bytes()
	finally:
		base_script.unlink()
