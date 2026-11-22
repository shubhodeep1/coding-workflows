"""scripts/lint_gh_calls_in_loops.py: CLAUDE.md §15 enforcement, warning only (#9929).

The lint flags GitHub API calls a pull request adds inside shell loops, in
`*.sh` / `*.bash` files and workflow `run:` blocks. It reads the local git
diff (no API call), honours `# gh-loop-ok: <reason>` and the batched/cached
helper allow-list, and always exits 0.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "lint_gh_calls_in_loops.py"

_SPEC = importlib.util.spec_from_file_location("lint_gh_calls_in_loops", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
lint_mod = importlib.util.module_from_spec(_SPEC)
# dataclasses look the module up in sys.modules while the class is built.
sys.modules[_SPEC.name] = lint_mod
_SPEC.loader.exec_module(lint_mod)

GH_API = "gh " + "api"


def _git(repo: Path, *args: str) -> str:
	env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
	result = subprocess.run(
		["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
		cwd=repo, env=env, capture_output=True, text=True, check=True,
	)
	return result.stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	(repo / "README").write_text("base\n", encoding="utf-8")
	for name, text in files.items():
		(repo / name).parent.mkdir(parents=True, exist_ok=True)
		(repo / name).write_text(text, encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base")
	return repo, _git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, files: dict[str, str]) -> str:
	for name, text in files.items():
		(repo / name).parent.mkdir(parents=True, exist_ok=True)
		(repo / name).write_text(text, encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "change")
	return _git(repo, "rev-parse", "HEAD")


def _run(repo: Path, base: str, head: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		["python3", "-I", str(SCRIPT), "--base", base, "--head", head, "--repo-root", str(repo)],
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True,
		encoding="utf-8", check=False,
	)


def _warned_lines(path: str, text: str) -> list[int]:
	"""Lines that warn when every line of `text` is newly added."""
	added = set(range(1, text.count("\n") + 2))
	return [warning.line for warning in lint_mod.scan_file(path, text, added)]


def test_added_call_inside_while_loop_warns_and_outside_does_not(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path, {"scripts/a.sh": "#!/bin/bash\necho start\n"})
	head = _commit(repo, {"scripts/a.sh": (
		"#!/bin/bash\necho start\n"
		"while read -r n; do\n"
		f"  {GH_API} repos/o/r/issues/$n\n"
		"done < list.txt\n"
		f"{GH_API} repos/o/r\n"
	)})
	result = _run(repo, base, head)
	assert result.returncode == 0, result.stderr
	warnings = [line for line in result.stdout.splitlines() if line.startswith("::warning ")]
	assert len(warnings) == 1, result.stdout
	assert warnings[0].startswith("::warning file=scripts/a.sh,line=4::§15: GitHub API call inside a loop ("), warnings[0]
	assert "# gh-loop-ok: <reason>" in warnings[0]
	assert "GH_LOOP_CALL_LINT warnings=1 files=1" in result.stdout


def test_one_liner_loop_warns_but_a_call_feeding_a_loop_does_not() -> None:
	assert _warned_lines("a.sh", "for x in a; do gh pr view 1; done\n") == [1]
	assert _warned_lines("a.sh", f"{GH_API} x | while read -r l; do echo; done\n") == []
	assert _warned_lines("a.sh", f"while read -r l; do echo; done < <({GH_API} x)\n") == []


def test_existing_call_in_an_edited_loop_does_not_warn(tmp_path: Path) -> None:
	before = f"while true; do\n  {GH_API} repos/o/r\n  sleep 5\ndone\n"
	repo, base = _repo(tmp_path, {"a.sh": before})
	head = _commit(repo, {"a.sh": before.replace("sleep 5", "sleep 10")})
	result = _run(repo, base, head)
	assert result.returncode == 0
	assert "::warning" not in result.stdout, result.stdout
	assert "GH_LOOP_CALL_LINT warnings=0 files=1" in result.stdout


def test_annotation_suppresses_only_with_a_reason() -> None:
	same_line = f"while true; do\n  {GH_API} x  # gh-loop-ok: bounded to 3 retries\ndone\n"
	line_above = f"while true; do\n  # gh-loop-ok: polls once a minute\n  {GH_API} x\ndone\n"
	no_reason = f"while true; do\n  {GH_API} x  # gh-loop-ok:\ndone\n"
	assert _warned_lines("a.sh", same_line) == []
	assert _warned_lines("a.sh", line_above) == []
	assert _warned_lines("a.sh", no_reason) == [2]


def test_allow_listed_helpers_do_not_warn() -> None:
	for helper in ("_fetch_linked_pr_status_graphql", "gh_review_pr_state", "gh_read"):
		text = f"for n in 1 2; do\n  out=\"$({helper} gh pr view $n)\"\ndone\n"
		assert _warned_lines("a.sh", text) == [], helper


def test_heredoc_bodies_and_comments_do_not_warn() -> None:
	text = (
		"while true; do\n"
		"  cat <<EOF\n"
		f"{GH_API} in a heredoc\n"
		"EOF\n"
		"\tcat <<-'END'\n"
		f"\t{GH_API} in a quoted heredoc\n"
		"\tEND\n"
		f"  # {GH_API} in a comment\n"
		f"  echo '{GH_API} in single quotes'\n"
		f"  {GH_API} after-heredocs\n"
		"done\n"
	)
	assert _warned_lines("a.sh", text) == [10]


def test_done_as_an_argument_does_not_close_the_loop() -> None:
	text = f"while true; do\n  echo done\n  {GH_API} x\ndone\n{GH_API} y\n"
	assert _warned_lines("a.sh", text) == [3]


def test_arithmetic_shift_is_not_a_heredoc() -> None:
	text = f"for i in 1; do\n  z=$((a<<b))\n  {GH_API} x\ndone\n"
	assert _warned_lines("a.sh", text) == [3]


def test_every_api_call_form_warns_in_a_loop() -> None:
	for call in (
		"curl -s https://api.github.com/repos/o/r",
		"curl -s 'https://api.github.com/repos/o/r'",
		"gh_retry gh pr list",
		"_safe_gh_jq repos/o/r .x",
		"gh_api_retry repos/o/r",
		"gh issue list --label x",
		"gh run view 1",
		"gh pr checks 1",
	):
		assert _warned_lines("a.sh", f"for n in 1; do\n  {call}\ndone\n") == [2], call


def test_function_definitions_and_other_gh_commands_do_not_warn() -> None:
	text = "for n in 1; do\n  gh_retry() { :; }\n  gh pr create --fill\n  curl https://example.com\ndone\n"
	assert _warned_lines("a.sh", text) == []


def test_workflow_run_block_maps_to_the_yaml_line(tmp_path: Path) -> None:
	workflow = (
		"name: x\n"                                   # 1
		"on: push\n"                                  # 2
		"jobs:\n"                                     # 3
		"  a:\n"                                      # 4
		"    runs-on: ubuntu-latest\n"                # 5
		"    steps:\n"                                # 6
		"      - name: loop\n"                        # 7
		"        run: |\n"                            # 8
		"          for n in 1 2; do\n"                # 9
		f"            {GH_API} repos/o/r/issues/$n\n"  # 10
		"          done\n"                            # 11
		f"          {GH_API} repos/o/r\n"             # 12
		"      - name: one\n"                         # 13
		f"        run: {GH_API} repos/o/r\n"          # 14
		f"    # {GH_API} outside any run block in a loop do\n"  # 15
	)
	repo, base = _repo(tmp_path, {})
	head = _commit(repo, {".github/workflows/x.yml": workflow, "workflow-templates/x.yml": workflow, "docs/x.yml": workflow})
	result = _run(repo, base, head)
	assert result.returncode == 0, result.stderr
	warnings = sorted(line for line in result.stdout.splitlines() if line.startswith("::warning "))
	assert [line.split("::")[1] for line in warnings] == [
		"warning file=.github/workflows/x.yml,line=10",
		"warning file=workflow-templates/x.yml,line=10",
	], result.stdout
	assert "GH_LOOP_CALL_LINT warnings=2 files=2" in result.stdout


def test_workflow_run_block_extraction_keeps_line_numbers() -> None:
	lines = ["steps:", "  - run: |", "      a", "", "      b", "  - name: x", "    run: echo hi"]
	assert lint_mod.workflow_run_blocks(lines) == [(3, ["a", "", "b"]), (7, ["echo hi"])]


def test_untrusted_values_are_escaped() -> None:
	warning = lint_mod.LoopCallFinding(path="a,b:c%.sh", line=3, snippet="gh api 100%\nx")
	line = lint_mod.format_warning(warning)
	assert line.startswith("::warning file=a%2Cb%3Ac%25.sh,line=3::"), line
	assert "100%25%0Ax" in line
	assert "\n" not in line


def test_unknown_base_is_skipped_and_exits_zero(tmp_path: Path) -> None:
	repo, base = _repo(tmp_path, {"a.sh": "echo\n"})
	result = _run(repo, "0" * 40, base)
	assert result.returncode == 0
	assert "GH_LOOP_CALL_LINT outcome=skipped reason=no_base" in result.stdout


def test_main_in_process_exits_zero_with_warnings(tmp_path: Path, capsys) -> None:
	repo, base = _repo(tmp_path, {"a.sh": "echo\n"})
	head = _commit(repo, {"a.sh": f"echo\nuntil false; do {GH_API} x; done\n", "b.bash": "for i in 1; do gh run list; done\n"})
	assert lint_mod.main(["--base", base, "--head", head, "--repo-root", str(repo)]) == 0
	out = capsys.readouterr().out
	assert "::warning file=a.sh,line=2::" in out
	assert "::warning file=b.bash,line=1::" in out
	assert "GH_LOOP_CALL_LINT warnings=2 files=2" in out


def test_internal_error_is_skipped_and_exits_zero(tmp_path: Path, capsys, monkeypatch) -> None:
	def boom(*_args: object) -> None:
		raise ValueError("unexpected")

	monkeypatch.setattr(lint_mod, "lint", boom)
	assert lint_mod.main(["--base", "x", "--repo-root", str(tmp_path)]) == 0
	assert "GH_LOOP_CALL_LINT outcome=skipped reason=internal_error" in capsys.readouterr().out


def test_case_pattern_named_done_does_not_close_the_loop() -> None:
	text = (
		"while true; do\n"
		"  case \"$stage\" in\n"
		"    fetch|done) ;;\n"
		"    (done) echo ;;\n"
		"    *) stage=other ;;\n"
		"  esac\n"
		f"  {GH_API} x\n"
		"done\n"
		f"{GH_API} y\n"
	)
	assert _warned_lines("a.sh", text) == [7]


def test_loop_inside_double_quoted_command_substitution_warns() -> None:
	one_line = f"out=\"$(for n in 1 2; do {GH_API} \"repos/o/r/issues/$n\"; done)\"\n{GH_API} z\n"
	assert _warned_lines("a.sh", one_line) == [1]
	multi = (
		"out=\"$(\n"
		"  printf '%s\\n' \"x\" | while read -r n; do\n"
		f"    {GH_API} \"repos/o/r/issues/$n\"\n"
		"  done\n"
		")\"\n"
		f"{GH_API} z\n"
	)
	assert _warned_lines("a.sh", multi) == [3]


def test_multi_line_curl_to_the_api_host_warns_on_the_curl_line() -> None:
	text = (
		"for n in 1; do\n"
		"  out=\"$(curl -fsS -H \"Accept: application/vnd.github+json\" \\\n"
		"    \"https://api.github.com/repos/o/r/issues/$n\")\"\n"
		"  curl -fsS \\\n"
		"    https://example.com/x\n"
		"done\n"
	)
	assert _warned_lines("a.sh", text) == [2]


def test_global_options_before_the_subcommand_still_match() -> None:
	for call in (
		"gh -R o/r pr view 1", "gh --repo=o/r issue list", "gh --hostname h.example api x",
		"gh -R 'o/r' pr view 1", "gh --repo='o/r' issue list", 'gh --repo="o/r" issue list',
	):
		assert _warned_lines("a.sh", f"for n in 1; do\n  {call}\ndone\n") == [2], call


def test_suppression_needs_a_real_comment_or_a_wrapping_helper() -> None:
	quoted_annotation = f"while true; do\n  echo '# gh-loop-ok: example'\n  {GH_API} x\ndone\n"
	assert _warned_lines("a.sh", quoted_annotation) == [3]
	helper_after = f"for n in 1; do\n  {GH_API} x; gh_read y\ndone\n"
	assert _warned_lines("a.sh", helper_after) == [2]
	helper_other_command = f"for n in 1; do\n  out=\"$(gh_read x)\"; {GH_API} y\ndone\n"
	assert _warned_lines("a.sh", helper_other_command) == [2]
	sibling_substitution = f"for n in 1; do\n  if [ \"$(gh_read x)\" = \"$({GH_API} y)\" ]; then :; fi\ndone\n"
	assert _warned_lines("a.sh", sibling_substitution) == [2]
	helper_as_argument = f"for n in 1; do\n  printf '%s' gh_read \"$({GH_API} y)\"\ndone\n"
	assert _warned_lines("a.sh", helper_as_argument) == [2]


def test_one_unreadable_path_keeps_other_findings(tmp_path: Path, monkeypatch) -> None:
	repo, base = _repo(tmp_path, {})
	head = _commit(repo, {"a.sh": f"for n in 1; do {GH_API} x; done\n", "b.sh": f"for n in 1; do {GH_API} x; done\n"})
	real_git = lint_mod._git

	def flaky(root: Path, *args: str) -> str:
		if args[:1] == ("show",) and args[1].endswith(":a.sh"):
			raise RuntimeError("unreadable")
		return real_git(root, *args)

	monkeypatch.setattr(lint_mod, "_git", flaky)
	warnings, files, skip = lint_mod.lint(repo, base, head)
	assert skip is None
	assert [(w.path, w.line) for w in warnings] == [("b.sh", 1)]
	assert files == 1, "the unreadable a.sh is not counted as scanned"
