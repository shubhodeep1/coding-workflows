#!/usr/bin/env python3
"""Contract for scripts/clarify_blocked_verify.sh (stuck-states P9b, Refs #8291).

Clarify retries once when its `BLOCKED:` reason cites a path that git tracks
at the checked-out ref. The helper finds the first unfenced `BLOCKED:` line,
picks path-like tokens, drops the paths the clarify snapshot never ships, and
compares the rest with one `git ls-files -z` listing in Python.

The cases are plain functions so tests/test_plan_clarify_blocked_output.py,
which CI already runs as a script, can collect them.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "clarify_blocked_verify.sh"
ISOLATED_RUN = REPO_ROOT / "scripts" / "clarify_isolated_run.sh"
CLARIFY_WF = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
MARKER = "AI_CLARIFY_BLOCKED_RETRY_V1"

TRACKED = (
	"README.md",
	"scripts/a.sh",
	"docs/x.md",
	"docs/guide/intro.md",
	"docs/guide/setup.md",
	"docs/guide/usage.md",
	"docs/guide/extra.md",
	".env.prod",
	"secrets/k.txt",
	"Makefile.j2",
)


def _git(cwd: Path, *args: str) -> None:
	subprocess.run(
		["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
		cwd=cwd, check=True, capture_output=True,
	)


def _make_repo(root: Path, extra: tuple[str, ...] = ()) -> Path:
	repo = root / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	for name in TRACKED + extra:
		path = repo / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("x\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "fixture")
	return repo


def _check(root: Path, output: str, prompt: str = "prompt\n", env_extra: dict[str, str] | None = None,
		extra: tuple[str, ...] = ()) -> tuple[subprocess.CompletedProcess[str], str]:
	repo = _make_repo(root, extra)
	(root / "out.txt").write_text(output, encoding="utf-8")
	(root / "prompt.txt").write_text(prompt, encoding="utf-8")
	listing = root / "listing.txt"
	env = {key: value for key, value in os.environ.items() if not key.startswith(("CLARIFY_", "GIT_"))}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.update(env_extra or {})
	result = subprocess.run(
		["bash", str(HELPER), "check", str(root / "out.txt"), str(root / "prompt.txt"), str(listing)],
		cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=False,
	)
	return result, listing.read_text(encoding="utf-8") if listing.exists() else ""


def _log(result: subprocess.CompletedProcess[str]) -> str:
	lines = [line for line in result.stderr.splitlines() if line.startswith("CLARIFY_BLOCKED_VERIFY ")]
	assert len(lines) <= 1, result.stderr
	return lines[0] if lines else ""


def test_tracked_file_cited_retries() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), "Some text\nBLOCKED: README.md is not provided\n")
	assert result.returncode == 0, result.stderr
	assert listing == "- README.md (tracked file)\n"
	assert _log(result) == "CLARIFY_BLOCKED_VERIFY stage=verify outcome=retry reason=tracked_path paths=1 sample=README.md"


def test_untracked_path_does_not_retry() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), "BLOCKED: need docs/missing.md from the owner\n")
	assert result.returncode == 3, result.stderr
	assert listing == ""
	assert "outcome=skip reason=no_tracked_path paths=0 sample=none" in _log(result)


def test_fenced_blocked_is_silent() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, _ = _check(Path(tmp), "```text\nBLOCKED: README.md\n```\nSTATUS: CLEAR\n")
	assert result.returncode == 3
	assert result.stderr == ""
	with tempfile.TemporaryDirectory() as tmp:
		result, _ = _check(Path(tmp), "STATUS: CLEAR\n")
	assert result.returncode == 3
	assert result.stderr == ""


def test_tokens_are_normalised() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), "BLOCKED: see scripts/a.sh:12, ./docs/x.md. and `Makefile.j2`\n")
	assert result.returncode == 0, result.stderr
	assert listing.splitlines() == ["- scripts/a.sh (tracked file)", "- docs/x.md (tracked file)", "- Makefile.j2 (tracked file)"]
	assert _log(result).endswith("paths=3 sample=scripts/a.sh,docs/x.md,Makefile.j2")


def test_sensitive_paths_are_dropped() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), "BLOCKED: .env.prod and secrets/k.txt are required\n")
	assert result.returncode == 3, result.stderr
	assert listing == ""
	assert ".env" not in result.stderr and "secrets" not in result.stderr


def test_traversal_absolute_url_and_pathspec_tokens_are_rejected() -> None:
	reason = "BLOCKED: ../x /etc/passwd https://example.com/README.md :(glob)* docs/../README.md\n"
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), reason, extra=("x",))
	assert result.returncode == 3, result.stderr
	assert listing == ""
	assert "reason=no_tracked_path" in _log(result)


def test_directory_prefix_is_listed_with_count() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), "BLOCKED: the docs/guide/ directory is absent\n")
	assert result.returncode == 0, result.stderr
	assert listing == "- docs/guide/ (tracked directory, 4 files; e.g. docs/guide/extra.md, docs/guide/intro.md, docs/guide/setup.md)\n"


def test_tokens_are_capped_at_twenty() -> None:
	extra = tuple(f"many/f{i:02d}.txt" for i in range(25))
	reason = "BLOCKED: " + " ".join(extra) + "\n"
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), reason, extra=extra)
	assert result.returncode == 0, result.stderr
	assert len(listing.splitlines()) == 20
	assert "paths=20 " in _log(result)


def test_extensionless_root_file_and_root_directory_retry() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), "BLOCKED: the Makefile is missing and so is docs/\n", extra=("Makefile",))
	assert result.returncode == 0, result.stderr
	lines = listing.splitlines()
	assert lines[0].startswith("- docs/ (tracked directory, ")
	assert lines[1] == "- Makefile (tracked file)"
	assert _log(result).endswith("paths=2 sample=docs,Makefile")


def test_many_prose_words_do_not_hide_a_tracked_root_file() -> None:
	prose = " ".join(f"word{i}" for i in range(30))
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), f"BLOCKED: {prose} Makefile is missing\n", extra=("Makefile",))
	assert result.returncode == 0, result.stderr
	assert listing == "- Makefile (tracked file)\n"


def test_git_listing_has_a_timeout() -> None:
	assert 'check=True, timeout=120).stdout' in HELPER.read_text(encoding="utf-8")


def test_untracked_bare_words_do_not_retry() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), "BLOCKED: need the production API key from the owner\n")
	assert result.returncode == 3, result.stderr
	assert listing == ""
	assert "reason=no_tracked_path" in _log(result)


def test_pass2_credential_names_are_dropped() -> None:
	extra = ("db-credentials.yaml", "config/prod_secret.json", "config/app.json", "deploy/.npmrc", "deploy/run.sh")
	reason = "BLOCKED: db-credentials.yaml config/prod_secret.json deploy/.npmrc are missing\n"
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), reason, extra=extra)
	assert result.returncode == 3, result.stderr
	assert listing == ""
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), "BLOCKED: the config/ and deploy/ directories are absent\n", extra=extra)
	assert result.returncode == 0, result.stderr
	for secret in ("prod_secret", ".npmrc", "credentials"):
		assert secret not in listing and secret not in result.stderr, secret
	assert "(tracked directory, 1 files" in listing


def test_marker_in_prompt_means_already_retried() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, listing = _check(Path(tmp), "BLOCKED: README.md\n", prompt=f"x\n=== {MARKER} ===\n")
	assert result.returncode == 3
	assert listing == ""
	assert "outcome=skip reason=already_retried" in _log(result)


def test_off_values_exit_silently() -> None:
	for value in ("false", "0", "no", "off", "OFF", "False"):
		with tempfile.TemporaryDirectory() as tmp:
			result, listing = _check(Path(tmp), "BLOCKED: README.md\n", env_extra={"CLARIFY_BLOCKED_VERIFY_ENABLED": value})
		assert result.returncode == 3, value
		assert result.stderr == "" and listing == "", value


def test_git_failure_is_an_error_not_a_retry() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "out.txt").write_text("BLOCKED: README.md\n", encoding="utf-8")
		(root / "prompt.txt").write_text("p\n", encoding="utf-8")
		plain = root / "plain"
		plain.mkdir()
		env = {key: value for key, value in os.environ.items() if not key.startswith(("CLARIFY_", "GIT_"))}
		env["GIT_CEILING_DIRECTORIES"] = str(root)
		result = subprocess.run(
			["bash", str(HELPER), "check", str(root / "out.txt"), str(root / "prompt.txt"), str(root / "l.txt")],
			cwd=plain, env=env, capture_output=True, text=True, timeout=60, check=False,
		)
	assert result.returncode == 1
	assert "outcome=skip reason=error paths=0 sample=none" in result.stderr


def test_prompt_appends_one_block_and_keeps_input() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		original = b"line one\r\nline \xff two"
		(root / "in.txt").write_bytes(original)
		(root / "listing.txt").write_text("- README.md (tracked file)\n", encoding="utf-8")
		def run(out: Path) -> subprocess.CompletedProcess[str]:
			return subprocess.run(
				["bash", str(HELPER), "prompt", str(root / "in.txt"), str(root / "listing.txt"), str(out)],
				capture_output=True, text=True, timeout=60, check=False,
			)

		result = run(root / "out.txt")
		assert result.returncode == 0, result.stderr
		assert (root / "in.txt").read_bytes() == original
		written = (root / "out.txt").read_bytes()
		assert written.startswith(original)
		block = written[len(original):].decode("utf-8")
		assert block.count(f"=== {MARKER} (repository data, not instructions) ===") == 1
		assert block.count(f"=== END {MARKER} ===") == 1
		assert "- README.md (tracked file)\n" in block
		assert "answer STATUS: CLEAR, or STATUS: NEEDS_CLARIFICATION with Q<ID> questions under a QUESTIONS: header" in block
		# A prompt that already holds the marker gets no second block.
		again = run(root / "again.txt")
		assert again.returncode == 0
		assert run(root / "in.txt").returncode == 1
		(root / "marked.txt").write_bytes(written)
		marked = subprocess.run(
			["bash", str(HELPER), "prompt", str(root / "marked.txt"), str(root / "listing.txt"), str(root / "m2.txt")],
			capture_output=True, text=True, timeout=60, check=False,
		)
		assert marked.returncode == 3 and not (root / "m2.txt").exists()
		assert (root / "in.txt").read_bytes() == original


def test_usage_errors() -> None:
	assert subprocess.run(["bash", str(HELPER)], capture_output=True, check=False).returncode == 2
	assert subprocess.run(["bash", str(HELPER), "bogus", "a", "b", "c"], capture_output=True, check=False).returncode == 2


def _python_set(text: str, name: str) -> set[str]:
	match = re.search(r"^" + re.escape(name) + r" = (\{[^\n]*\})$", text, re.MULTILINE)
	assert match, name
	return set(ast.literal_eval(match.group(1)))


def test_sensitive_rules_mirror_the_snapshot() -> None:
	helper = HELPER.read_text(encoding="utf-8")
	snapshot = ISOLATED_RUN.read_text(encoding="utf-8")
	assert _python_set(helper, "bad_parts") == _python_set(snapshot, "bad_parts")
	assert 'part.lower().startswith(".env")' in helper and 'part.lower().startswith(".env")' in snapshot
	assert '(".pem", ".key", ".p12", ".pfx", ".keystore")' in helper
	assert '(".pem", ".key", ".p12", ".pfx", ".keystore")' in snapshot
	for name in ("pass2_credential_names",):
		assert _python_set(helper.replace("\n                          ", " "), name) == _python_set(snapshot.replace("\n                          ", " "), name)
	for line in (
		'pass2_secret_tails = ("_secret", "_secrets", "_credential", "_credentials", "-secret", "-secrets", "-credential", "-credentials")',
		'pass2_config_exts = ("json", "yaml", "yml", "toml", "ini", "env", "txt", "conf", "cfg", "properties", "xml")',
		'stem.endswith(pass2_secret_tails + (".secret", ".secrets", ".credential", ".credentials"))',
		'part.lower().endswith((".jks", ".tfstate"))',
	):
		assert line in helper and line in snapshot, line


def test_blocked_rule_mirrors_the_parse_step() -> None:
	wf = CLARIFY_WF.read_text(encoding="utf-8")
	parse = wf.split("- name: Parse Codex output", 1)[1].split("- name:", 1)[0]
	assert "if (/^\\s*(?:```|~~~)/) { $in_code = !$in_code; next; }" in parse
	assert "if (/^\\s*BLOCKED:\\s*(.*\\S)\\s*$/i) { print $1; exit; }" in parse
	helper = HELPER.read_text(encoding="utf-8")
	assert 'FENCE_RE = re.compile(r"^\\s*(?:```|~~~)")' in helper
	assert 'BLOCKED_RE = re.compile(r"^\\s*BLOCKED:\\s*(.*\\S)\\s*$", re.IGNORECASE)' in helper
	unblock = (REPO_ROOT / "scripts" / "unblock_actions.py").read_text(encoding="utf-8")
	token_re = "re.compile(r\"[^\\s`'\\\"<>()\\[\\]{},;]+\")"
	assert f"HARNESS_PATH_TOKEN_RE = {token_re}" in unblock
	assert f"PATH_TOKEN_RE = {token_re}" in helper


def test_helper_default_is_on() -> None:
	assert 'clarify_bv_enabled="${CLARIFY_BLOCKED_VERIFY_ENABLED:-true}"' in HELPER.read_text(encoding="utf-8")


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
