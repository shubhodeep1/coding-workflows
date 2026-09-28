#!/usr/bin/env python3
"""Whole-tree parity contract for `workflow-templates/.claude/` (issue #4775).

The `@stable` sync (`.github/workflows/update_workflows.yml`, "Sync .claude/
assets from upstream") copies `workflow-templates/.claude/` over every
consumer's `.claude/`, where unattended sessions follow it. Claude Code
protects only the repository root's `.claude/`, so the template tree is
protected-equivalent by rule (CLAUDE.md §23.I, §28.C), and this test makes a
template-only change fail closed:

- every template file needs a root `.claude/` twin;
- a twin is byte-identical to its root copy unless it is listed in
  `TEMPLATE_DIVERGENCE`, whose SHA-256 pins the template copy, so a change to a
  consumer-variant command also fails until the pin is updated in review;
- a listed divergence must still exist and still differ;
- the template tree holds no symlinks.

Updating `TEMPLATE_DIVERGENCE` is itself a protected-equivalent change: in an
unattended `/implement-plan-claude` project it needs a recorded
`Protected-path approval:` line (CLAUDE.md §28.C).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_CLAUDE = REPO_ROOT / ".claude"
TEMPLATE_CLAUDE = REPO_ROOT / "workflow-templates" / ".claude"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Untracked runtime output (.gitignore) that is never synced or committed.
IGNORED_DIR_NAMES = frozenset({"__pycache__"})
IGNORED_SUFFIXES = (".pyc",)

# Consumer-variant commands: the template copy differs from the root copy on
# purpose (consumer side vs. upstream library side). Key: path relative to
# `workflow-templates/.claude/`; value: SHA-256 of the template copy.
TEMPLATE_DIVERGENCE = {
	"commands/analyze-log.md": "1774b2f1d0f517380e83dff45e8cbf6f12990da2204cf4a901821b212a9881ec",
	"commands/deploy-activate.md": "fb69fe0987a119cfa0c14de1c39951876197cc73401215d30b8800d19c89563c",
	"commands/investigate-issue.md": "8132a65fb5d4e9bda482766901b5ddd5ef839abd35c18360d49033d6359e3a84",
	"commands/validate-consumer-issue.md": "3731054736887cae48db71240207b99608cfa8edc5253b547e97ef094c3df5a4",
	"commands/verify-activation.md": "98baaa82a3bc683f6c8691b4b8ffd9674b2875166c524c8fc539d180989e2478",
}

HOW_TO_FIX = (
	"workflow-templates/.claude/** is protected-equivalent (CLAUDE.md §23.I, §28.C; issue #4775): "
	"change the root .claude/ twin in the same PR, or, for a consumer variant, update "
	"TEMPLATE_DIVERGENCE in tests/test_claude_template_parity.py. An unattended project needs a "
	"recorded `Protected-path approval:` line first."
)


def _walk(base: Path) -> tuple[list[str], list[str]]:
	"""Return (regular files, symlinks) under `base` as sorted POSIX paths
	relative to it. Symlinked directories are reported, never followed."""
	files: list[str] = []
	links: list[str] = []
	for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
		current = Path(dirpath)
		for name in list(dirnames):
			if (current / name).is_symlink():
				links.append((current / name).relative_to(base).as_posix())
				dirnames.remove(name)
			elif name in IGNORED_DIR_NAMES:
				dirnames.remove(name)
		for name in filenames:
			if name.endswith(IGNORED_SUFFIXES):
				continue
			rel = (current / name).relative_to(base).as_posix()
			if (current / name).is_symlink():
				links.append(rel)
			else:
				files.append(rel)
	return sorted(files), sorted(links)


def _sha256(path: Path) -> str:
	return hashlib.sha256(path.read_bytes()).hexdigest()


def parity_violations(template_root: Path, root_claude: Path, divergence: dict[str, str]) -> list[str]:
	"""Every reason the template tree fails the contract; empty when it passes."""
	problems: list[str] = []
	if not template_root.is_dir():
		return [f"{template_root} is missing"]
	files, links = _walk(template_root)
	for rel in links:
		problems.append(f"symlink in the template tree: {rel}")
	for rel in files:
		template_file = template_root / rel
		root_file = root_claude / rel
		if not root_file.is_file() or root_file.is_symlink():
			problems.append(f"template-only file (no root .claude/ twin): {rel}")
			continue
		same = template_file.read_bytes() == root_file.read_bytes()
		if rel in divergence:
			if same:
				problems.append(f"listed divergence is now identical to its root twin; drop it from TEMPLATE_DIVERGENCE: {rel}")
			elif _sha256(template_file) != divergence[rel]:
				problems.append(f"consumer-variant template changed; its pinned SHA-256 no longer matches: {rel}")
		elif not same:
			problems.append(f"template differs from its root .claude/ twin: {rel}")
	for rel in sorted(set(divergence) - set(files)):
		problems.append(f"listed divergence has no template file: {rel}")
	return problems


def test_template_tree_matches_root_claude():
	problems = parity_violations(TEMPLATE_CLAUDE, ROOT_CLAUDE, TEMPLATE_DIVERGENCE)
	assert not problems, "\n".join(problems + [HOW_TO_FIX])


def test_template_tree_is_not_empty():
	files, _ = _walk(TEMPLATE_CLAUDE)
	assert "settings.json" in files
	assert any(rel.startswith("commands/") for rel in files)
	assert any(rel.startswith("hooks/") for rel in files)


# ──────────────────────────────────────────────────────────────────
# The checker itself fails closed (fixture trees, no repository writes)
# ──────────────────────────────────────────────────────────────────


def _tree(tmp_path: Path) -> tuple[Path, Path]:
	template = tmp_path / "workflow-templates" / ".claude"
	root = tmp_path / ".claude"
	for base in (template, root):
		(base / "commands").mkdir(parents=True)
		(base / "hooks").mkdir()
		(base / "settings.json").write_text("{}\n", encoding="utf-8")
		(base / "hooks" / "guard.py").write_text("print('ok')\n", encoding="utf-8")
		(base / "commands" / "same.md").write_text("same\n", encoding="utf-8")
	(template / "commands" / "variant.md").write_text("consumer\n", encoding="utf-8")
	(root / "commands" / "variant.md").write_text("upstream\n", encoding="utf-8")
	return template, root


def _pin(template: Path) -> dict[str, str]:
	return {"commands/variant.md": _sha256(template / "commands" / "variant.md")}


def test_checker_accepts_a_matching_tree(tmp_path):
	template, root = _tree(tmp_path)
	(root / "commands" / "root-only.md").write_text("upstream only\n", encoding="utf-8")
	(template / "hooks" / "__pycache__").mkdir()
	(template / "hooks" / "__pycache__" / "guard.cpython-311.pyc").write_bytes(b"\0")
	assert parity_violations(template, root, _pin(template)) == []


def test_checker_rejects_a_template_only_file(tmp_path):
	template, root = _tree(tmp_path)
	(template / "commands" / "new.md").write_text("injected\n", encoding="utf-8")
	assert parity_violations(template, root, _pin(template)) == ["template-only file (no root .claude/ twin): commands/new.md"]


def test_checker_rejects_a_changed_twin(tmp_path):
	template, root = _tree(tmp_path)
	for rel in ("settings.json", "hooks/guard.py", "commands/same.md"):
		(template / rel).write_bytes((root / rel).read_bytes() + b"x")
	assert parity_violations(template, root, _pin(template)) == [
		"template differs from its root .claude/ twin: commands/same.md",
		"template differs from its root .claude/ twin: hooks/guard.py",
		"template differs from its root .claude/ twin: settings.json",
	]


def test_checker_rejects_a_changed_divergence(tmp_path):
	template, root = _tree(tmp_path)
	pin = _pin(template)
	(template / "commands" / "variant.md").write_text("consumer, edited\n", encoding="utf-8")
	assert parity_violations(template, root, pin) == [
		"consumer-variant template changed; its pinned SHA-256 no longer matches: commands/variant.md",
	]


def test_checker_rejects_a_stale_divergence(tmp_path):
	template, root = _tree(tmp_path)
	(template / "commands" / "variant.md").write_text("upstream\n", encoding="utf-8")
	pin = {**_pin(template), "commands/gone.md": "0" * 64}
	assert parity_violations(template, root, pin) == [
		"listed divergence is now identical to its root twin; drop it from TEMPLATE_DIVERGENCE: commands/variant.md",
		"listed divergence has no template file: commands/gone.md",
	]


def test_checker_rejects_symlinks(tmp_path):
	template, root = _tree(tmp_path)
	(template / "commands" / "same.md").unlink()
	(template / "commands" / "same.md").symlink_to(root / "commands" / "same.md")
	(template / "linked").symlink_to(root / "hooks", target_is_directory=True)
	assert parity_violations(template, root, _pin(template)) == [
		"symlink in the template tree: commands/same.md",
		"symlink in the template tree: linked",
	]


def test_checker_rejects_a_missing_root_twin(tmp_path):
	template, root = _tree(tmp_path)
	(root / "hooks" / "guard.py").unlink()
	assert parity_violations(template, root, _pin(template)) == ["template-only file (no root .claude/ twin): hooks/guard.py"]


# ──────────────────────────────────────────────────────────────────
# CLAUDE.md wording and CI wiring
# ──────────────────────────────────────────────────────────────────


def _flat(text: str) -> str:
	return " ".join(text.split())


def test_claude_md_declares_the_template_tree_protected_equivalent():
	text = CLAUDE_MD.read_text(encoding="utf-8")
	start = text.index("### I) Permission Prompt Reports")
	section = _flat(text[start:text.index("\n## §24.", start)])
	assert "a protected-path edit (the repository root's `.claude/**`, which Claude Code itself protects)" in section
	assert "`workflow-templates/.claude/**` is not a Claude Code protected path" in section
	assert "It is still **protected-equivalent** for authorization" in section
	assert "the §28.C protected-path stop covers it" in section
	assert "`tests/test_claude_template_parity.py` fails CI on a template-only change (issue #4775)" in section
	assert "`workflow-templates/.claude/**` is not protected)" not in section


def test_claude_md_28c_stop_names_the_template_twins():
	text = CLAUDE_MD.read_text(encoding="utf-8")
	start = text.index("### C) Never auto-decided")
	section = _flat(text[start:text.index("### D) Recording", start)])
	assert "A phase that must edit `.claude/**` or its `workflow-templates/.claude/**` twins" in section
	assert "The template twins are protected-equivalent (§23.I, issue #4775)" in section


def test_ci_runs_this_contract():
	assert "tests/test_claude_template_parity.py" in CI_WORKFLOW.read_text(encoding="utf-8")
