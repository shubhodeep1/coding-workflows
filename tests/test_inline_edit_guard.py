#!/usr/bin/env python3
"""Behaviour and wiring contract for the inline-edit guard (CLAUDE.md §23.I, issue #4858).

Covers the pieces that can silently detach the mechanism:
  1. Detection: inline-interpreter writes are denied with the redirect
     message; reads, `pytest`, scripts run from a file, and interpreter text
     that is only data get no decision.
  2. The fail-open contract, the kill switch, and the deny signals (stderr
     log key and the permission-prompt log record).
  3. The settings.json wiring, template parity, and the prose in CLAUDE.md,
     agents.md, seed-repo, and ci.yml.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_PATH = REPO_ROOT / ".claude" / "hooks" / "inline_edit_guard.py"
TEMPLATE_GUARD_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "inline_edit_guard.py"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
TEMPLATE_SETTINGS_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"
PROMPTS_SCRIPT_PATH = REPO_ROOT / ".claude" / "scripts" / "permission_prompts.py"
TEMPLATE_PROMPTS_SCRIPT_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "scripts" / "permission_prompts.py"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
AGENTS_MD = REPO_ROOT / "agents.md"
README_MD = REPO_ROOT / "README.md"
SEED_REPO_COMMAND = REPO_ROOT / ".claude" / "commands" / "seed-repo.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The deny reason, verbatim from issue #4858.
ISSUE_MESSAGE = (
	"Edit files with the Edit tool (exact old_string/new_string) or the Write tool, not an inline "
	"interpreter. These paths are not protected and will not prompt. For a protected `.claude/**` "
	"file, edit its `workflow-templates/.claude/**` twin instead (CLAUDE.md §28.C twin-first)."
)


def _load_guard():
	spec = importlib.util.spec_from_file_location("inline_edit_guard", GUARD_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


guard = _load_guard()


def _evaluate(command: str, environ=None):
	return guard.evaluate({"tool_name": "Bash", "tool_input": {"command": command}}, environ or {})


def _run_hook(stdin_text: str, home: Path, extra_env=None) -> subprocess.CompletedProcess:
	env = dict(os.environ)
	env.pop(guard.ENV_KILL_SWITCH, None)
	env.update({"PYTHONDONTWRITEBYTECODE": "1", "HOME": str(home)})
	env.update(extra_env or {})
	return subprocess.run(
		[sys.executable, str(GUARD_PATH)],
		input=stdin_text,
		capture_output=True,
		text=True,
		env=env,
		check=False,
		cwd=str(REPO_ROOT),
	)


def _payload(command: str) -> str:
	return json.dumps(
		{
			"hook_event_name": "PreToolUse",
			"session_id": "sess-4858",
			"tool_name": "Bash",
			"tool_input": {"command": command},
			"cwd": str(REPO_ROOT),
			"permission_mode": "auto",
		}
	)


# ──────────────────────────────────────────────────────────────────
# Denied
# ──────────────────────────────────────────────────────────────────

# The outer shapes of the inline-interpreter prompts filed on 2026-09-28 and
# 2026-09-29 (`ai:permission-prompt` records #4843, #4857, #4881, #4905, from
# the #4755, #4786, #4791 and convergence sessions). The records omit heredoc
# bodies, so each carries a representative write to the file it targeted.
OBSERVED_PROMPTS = {
	"validate.yml heredoc, then git diff and sed -n (#4857)": (
		"python3 - <<'EOF'\n"
		"from pathlib import Path\n"
		"p = Path('.github/workflows/validate.yml')\n"
		"s = p.read_text()\n"
		"p.write_text(s.replace('old', 'new', 1))\n"
		"EOF\n"
		"git diff --stat; sed -n 222,235p .github/workflows/validate.yml; sed -n 300,316p .github/workflows/validate.yml"
	),
	"docs heredoc, then git diff | head (#4881)": (
		"python3 - <<'EOF'\n"
		"import re\n"
		"path = 'docs/implement-plan/issue-4786.md'\n"
		"text = open(path).read()\n"
		"with open(path, 'w') as handle:\n"
		"\thandle.write(re.sub('a', 'b', text))\n"
		"EOF\n"
		"git diff docs/ | head -60"
	),
	"template twin heredoc with an argument, then grep (#4905)": (
		"F=workflow-templates/.claude/commands/implement-plan-claude.md && python3 - \"$F\" <<'EOF'\n"
		"import sys, pathlib\n"
		"p = pathlib.Path(sys.argv[1])\n"
		"p.write_text(p.read_text().replace('x', 'y'))\n"
		"EOF\n"
		"grep -n \"Fill every\" $F"
	),
	"changelog heredoc, then ls and cat (#4843)": (
		"python3 - <<'PY'\n"
		"import os\n"
		"os.replace('changelog.d/a.md.tmp', 'changelog.d/a.md')\n"
		"PY\n"
		"ls changelog.d | head -5; cat \"changelog.d/$(ls changelog.d | grep -v README | head -1)\" | head -30"
	),
}


@pytest.mark.parametrize("name", sorted(OBSERVED_PROMPTS))
def test_observed_prompts_are_denied(name):
	decision, reason, kind = _evaluate(OBSERVED_PROMPTS[name])
	assert (decision, kind) == ("deny", "python")
	assert reason == ISSUE_MESSAGE


@pytest.mark.parametrize(
	"command, kind",
	[
		("sed -i 's/a/b/' tests/test_x.py", "sed"),
		("sed -i.bak -e 's/a/b/' CLAUDE.md", "sed"),
		("sed 's/a/b/' -i CLAUDE.md", "sed"),
		("sed --in-place=.bak 's/a/b/' f", "sed"),
		("sed -Ei 's/a/b/' f", "sed"),
		("perl -pi -e 's/a/b/' f", "perl"),
		("perl -i.bak -pe 's/a/b/' f", "perl"),
		("perl -0777 -pi -e 's/a/b/' f", "perl"),
		("ruby -i -pe '$_.upcase!' f", "ruby"),
		("gawk -i inplace '{print}' f", "awk"),
		("awk -i inplace '{print}' f", "awk"),
		("gawk -i inplace.awk '{print}' f", "awk"),
		("awk -i inplace.awk '{print}' f", "awk"),
		("python3 -c \"open('x','w').write('y')\"", "python"),
		("python3 -c \"open('x', mode='a').write('y')\"", "python"),
		("python -Bc \"import shutil; shutil.copy('a', 'b')\"", "python"),
		("python3.11 -c \"import os; os.remove('x')\"", "python"),
		("python3 <<'EOF'\nfrom pathlib import Path\nPath('x').write_bytes(b'')\nEOF", "python"),
		("python3 - <<'EOF'\nfrom pathlib import Path\nwith Path('x').open('w') as f:\n\tf.write('y')\nEOF", "python"),
		("python3 - <<'EOF'\nfrom pathlib import Path\nPath('x').unlink()\nEOF", "python"),
		("PYTHONDONTWRITEBYTECODE=1 python3 - <<'EOF'\nopen('x', 'r+').write('y')\nEOF", "python"),
		("cd /repo && sed -i 's/a/b/' f", "sed"),
		("env LC_ALL=C sed -i 's/a/b/' f", "sed"),
		("env -i python3 -c \"open('x','w').write('y')\"", "python"),
		("env -u HOME sed -i 's/a/b/' f", "sed"),
		("env --unset=HOME -- perl -pi -e 's/a/b/' f", "perl"),
		("env -C /repo LC_ALL=C sed -i 's/a/b/' f", "sed"),
		("/usr/bin/env python3 - <<'EOF'\nopen('x', 'w')\nEOF", "python"),
		("time env -i sed -i 's/a/b/' f", "sed"),
		("sudo -u me sed -i 's/a/b/' f", "sed"),
		("timeout 30 perl -pi -e 's/a/b/' f", "perl"),
		# `env` options after another wrapper (conformance run 3).
		("sudo -u root env -i python3 -c \"open('x','w').write('y')\"", "python"),
		("sudo env -u HOME sed -i 's/a/b/' f", "sed"),
		("timeout 30 env -i perl -pi -e 's/a/b/' f", "perl"),
		("nice -n 5 env -u HOME sed -i 's/a/b/' f", "sed"),
		("doas -u root env -i sed -i 's/a/b/' f", "sed"),
		# `sudo` long options that take a separate value.
		("sudo --user root sed -i 's/a/b/' f", "sed"),
		("sudo --chdir /repo --user root sed -i 's/a/b/' f", "sed"),
		# `open(` whose first argument nests calls two and three deep.
		("python3 -c \"import os; open(os.path.join(os.getcwd(), 'x'), 'w').write('y')\"", "python"),
		(
			"python3 - <<'EOF'\nimport os\n"
			"with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out.json'), 'w') as f:\n"
			"\tf.write('{}')\nEOF",
			"python",
		),
		("python3 -c \"open(os.path.join(str(d), 'x'), encoding='utf-8', mode='a')\"", "python"),
		("echo start; python3 - <<'EOF' && git diff\nopen('x', 'w')\nEOF", "python"),
		# `env -` is POSIX `env -i` (final PR #4877 review round 1).
		("env - python3 -c \"open('x','w').write('y')\"", "python"),
		("env - LC_ALL=C sed -i 's/a/b/' f", "sed"),
		("sudo env - sed -i 's/a/b/' f", "sed"),
		# A substitution Bash runs inside one word of another command.
		("echo \"$(python3 -c 'open(\"x\",\"w\").write(\"y\")')\"", "python"),
		("out=\"$(sed -i 's/a/b/' f)\"", "sed"),
		("echo \"done: `perl -pi -e 's/a/b/' f`\"", "perl"),
		("echo \"$(echo `sed -i s/a/b/ f`)\"", "sed"),
		# The rest of the mutating `shutil` calls, and `os.rename`.
		("python3 -c \"import shutil; shutil.make_archive('a', 'zip', '.')\"", "python"),
		("python3 -c \"import shutil; shutil.unpack_archive('a.zip', 'd')\"", "python"),
		("python3 -c \"import shutil; shutil.chown('a', 'me')\"", "python"),
		("python3 -c \"import os; os.rename('a', 'b')\"", "python"),
		("python3 -c \"import os; os.renames('a', 'b/c')\"", "python"),
		# Prefix words with options (final PR #4877 review round 4).
		("command -p python3 -c \"open('x','w').write('y')\"", "python"),
		("command -p sed -i 's/a/b/' f", "sed"),
		("command -- perl -pi -e 's/a/b/' f", "perl"),
		("exec -a name python3 -c \"open('x','w').write('y')\"", "python"),
		("exec -c sed -i 's/a/b/' f", "sed"),
		("time -p sed -i 's/a/b/' f", "sed"),
		("/usr/bin/time -o t.log perl -pi -e 's/a/b/' f", "perl"),
		("sudo command -p sed -i 's/a/b/' f", "sed"),
		# Perl's `-I` takes a separate directory (final PR #4877 review round 5).
		("perl -I lib -pi -e 's/a/b/' f", "perl"),
		("perl -pI lib -i -e 's/a/b/' f", "perl"),
		("perl -Ilib -pi -e 's/a/b/' f", "perl"),
		# pathlib's siblings of `os.replace` / `os.rename`.
		("python3 -c \"from pathlib import Path; Path('a').replace('b')\"", "python"),
		("python3 -c \"import pathlib; pathlib.Path(os.path.join(d, 'a')).rename('b')\"", "python"),
		# GNU `env -S` / `--split-string` runs the words its string splits into
		# (final PR #4877 review round 6).
		("env -S 'python3 -c \"import os; os.remove(p)\"'", "python"),
		("env -S 'sed -i s/a/b/ f'", "sed"),
		("env -iS'perl -pi -e s/a/b/ f'", "perl"),
		("env --split-string='sed -i s/a/b/ f'", "sed"),
		("env --split-string 'ruby -i -pe sub(?a,?b) f'", "ruby"),
		("env --sp 'sed -i s/a/b/ f'", "sed"),
		("env -S '-i LC_ALL=C sed -i s/a/b/ f'", "sed"),
		("env -S \"-S 'sed -i s/a/b/ f'\"", "sed"),
		("sudo env -S 'sed -i s/a/b/ f'", "sed"),
		("env -S 'python3 -' <<'EOF'\nopen('x', 'w')\nEOF", "python"),
		# `-uS` unsets `S`; it is not the split-string option.
		("env -uS sed -i s/a/b/ f", "sed"),
		# A heredoc inside a substitution Bash runs within one word keeps its
		# body (final PR #4877 review round 7).
		("echo \"$(python3 - <<'PY'\nfrom pathlib import Path\nPath('x').write_text('y')\nPY\n)\"", "python"),
		("echo `python3 - <<'PY'\nPath('x').write_text('y')\nPY\n`", "python"),
		("out=\"$(python3 <<-'PY'\n\topen('x', 'w').write('y')\n\tPY\n)\"", "python"),
		("echo \"$(cat <<'A'\nhello\nA\n)\" \"$(python3 - <<'B'\nimport os\nos.remove('x')\nB\n)\"", "python"),
		("echo \"$(echo `python3 - <<'PY'\nPath('x').unlink()\nPY\n`)\"", "python"),
	],
)
def test_inline_writes_are_denied(command, kind):
	decision, reason, got_kind = _evaluate(command)
	assert (decision, got_kind) == ("deny", kind)
	assert reason == ISSUE_MESSAGE


def test_deny_message_is_the_issue_text():
	assert guard.DENY_MESSAGE == ISSUE_MESSAGE


# ──────────────────────────────────────────────────────────────────
# No decision
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
	"command",
	[
		"python3 - <<'EOF'\nimport json\nprint(json.load(open('x')))\nEOF",
		"python3 - <<'EOF'\nprint(open('x', 'r').read())\nEOF",
		"python3 -c \"print(open('x').read())\"",
		"pytest -q tests/test_inline_edit_guard.py",
		"python3 -m pytest -q",
		"PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_inline_edit_guard.py",
		"python3 scripts/x.py --write",
		"python3 .claude/scripts/permission_prompts.py file --session-label s",
		"git commit -m \"fix python3 - <<EOF write_text\"",
		"git commit -m 'guard sed -i and perl -pi'",
		"git commit -m \"subject\n\npython3 - <<'EOF'\np.write_text('x')\nEOF\n\"",
		"echo \"sed -i\"",
		"grep -n 'sed -i' CLAUDE.md",
		"sed -n 1,20p CLAUDE.md",
		"env -u HOME sed -n 1p f",
		"env -i python3 -c \"print(1)\"",
		"env -i",
		"sudo env -u HOME sed -n 1p f",
		"sudo --user root sed -n 1p f",
		"python3 -c \"print(open(os.path.join(os.getcwd(), 'w')).read())\"",
		"python3 -c \"print(open(os.path.join(os.path.dirname(p), 'a', 'x')).read())\"",
		"sed -e 's/a/b/' f",
		"perl -ne 'print if /x/' f",
		"awk '{print $1}' f",
		"awk -i lib.awk '{print}' f",
		"cat f | python3 -",
		"cat <<'EOF' > script.py\nopen('x', 'w')\nEOF\npython3 script.py",
		"ls -la",
		"env -",
		"env - sed -n 1p f",
		"echo \"$(python3 -c 'print(open(\"x\").read())')\"",
		"git commit -m 'fix $(sed -i s/a/b/ f)'",
		"echo \"\\$(sed -i s/a/b/ f)\"",
		"python3 - <<'EOF'\nprint(`x`)\nEOF",
		"python3 -c \"import shutil; print(shutil.get_archive_formats())\"",
		# `command -v` / `-V` only looks the command up; nothing runs.
		"command -v python3 >/dev/null && python3 -c \"print(1)\"",
		"command -V sed -i",
		"command -pv perl -pi",
		"command -p sed -n 1p f",
		"time -p python3 -c \"print(1)\"",
		# `perl -I -pi` reads `-pi` as the directory; the other value letters and
		# Ruby's never take a separate argument, so the next word is the script.
		"perl -I -pi -e 's/a/b/' f",
		"perl -M strict -pi -e 's/a/b/' f",
		"perl -F : -pi -e 's/a/b/' f",
		"ruby -F : -i -pe '$_.upcase!' f",
		"ruby -x lib -i -pe '$_.upcase!' f",
		# `str.replace`, and a `Path` method other than replace / rename.
		"python3 -c \"print('a.txt'.replace('a', 'b'))\"",
		"python3 -c \"from pathlib import Path; print(Path('a').read_text().replace('x', 'y'))\"",
		"python3 -c \"from pathlib import Path; print(str(Path('a')).replace('a', 'b'))\"",
		# `env -S` with a read, without its string, or with an unterminated quote
		# (which `env` refuses, running nothing).
		"env -S 'sed -n 1p f'",
		"env -S 'python3 -c \"print(1)\"'",
		"env -S",
		"env -S \"sed -i 's/a/b/ f\"",
		# A heredoc inside a substitution with a read, and heredoc text that is
		# data: a quoted body inside or outside a substitution is never scanned.
		"echo \"$(python3 - <<'PY'\nprint(open('x').read())\nPY\n)\"",
		"gh pr create --body \"$(cat <<'EOF'\nGuard `sed -i` and Path('x').write_text('y')\nEOF\n)\"",
		"git commit -F - <<'EOF'\nfix: `sed -i s/a/b/ f` and \"$(perl -pi -e x f)\"\nEOF",
	],
)
def test_reads_scripts_and_data_get_no_decision(command):
	assert _evaluate(command) == (None, "", "")


def test_non_bash_tools_get_no_decision():
	assert guard.evaluate({"tool_name": "Edit", "tool_input": {"command": "sed -i s/a/b/ f"}}, {}) == (None, "", "")


def test_unparseable_command_gets_no_decision():
	assert _evaluate("sed -i 's/a/b/ f") == (None, "", "")


@pytest.mark.parametrize("value", ["off", "OFF", " Off "])
def test_kill_switch_turns_the_guard_off(value):
	assert _evaluate("sed -i s/a/b/ f", {guard.ENV_KILL_SWITCH: value}) == (None, "", "")


@pytest.mark.parametrize("value", ["", "on", "0", "false"])
def test_guard_is_on_unless_the_switch_says_off(value):
	assert _evaluate("sed -i s/a/b/ f", {guard.ENV_KILL_SWITCH: value})[0] == "deny"


# ──────────────────────────────────────────────────────────────────
# Hook process: deny output, signals, fail open
# ──────────────────────────────────────────────────────────────────


def test_hook_process_denies_logs_and_records(tmp_path):
	result = _run_hook(_payload("sed -i 's/a/b/' CLAUDE.md"), tmp_path)
	assert result.returncode == 0
	output = json.loads(result.stdout)["hookSpecificOutput"]
	assert output == {
		"hookEventName": "PreToolUse",
		"permissionDecision": "deny",
		"permissionDecisionReason": ISSUE_MESSAGE,
	}
	assert "INLINE_EDIT_GUARD action=deny kind=sed session=sess-4858" in result.stderr
	lines = (tmp_path / ".claude" / "permission-prompts" / "sess-4858.jsonl").read_text(encoding="utf-8").splitlines()
	assert len(lines) == 1
	record = json.loads(lines[0])
	assert record["event"] == "PermissionDenied"
	assert record["source"] == "inline_edit_guard" and record["kind"] == "sed"
	assert record["reason"] == ISSUE_MESSAGE
	assert record["tool_input"] == {"command": "sed -i 's/a/b/' CLAUDE.md"}


def test_hook_process_still_denies_when_the_record_cannot_be_written(tmp_path):
	home = tmp_path / "home-is-a-file"
	home.write_text("", encoding="utf-8")
	result = _run_hook(_payload("perl -pi -e 's/a/b/' f"), home)
	assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_process_is_silent_for_other_commands(tmp_path):
	result = _run_hook(_payload("python3 -m pytest -q"), tmp_path)
	assert result.returncode == 0
	assert result.stdout == "" and result.stderr == ""
	assert not (tmp_path / ".claude").exists()


def test_hook_process_honours_the_kill_switch(tmp_path):
	result = _run_hook(_payload("sed -i s/a/b/ f"), tmp_path, {guard.ENV_KILL_SWITCH: "off"})
	assert result.returncode == 0 and result.stdout == ""


@pytest.mark.parametrize("hook_path", [GUARD_PATH, TEMPLATE_GUARD_PATH], ids=["root", "template"])
def test_hook_process_writes_no_bytecode_beside_the_hooks(tmp_path, hook_path: Path):
	"""settings.json runs the hook without PYTHONDONTWRITEBYTECODE; loading its siblings must not leave .pyc files."""
	hooks_dir = tmp_path / "hooks"
	hooks_dir.mkdir()
	for name in ("inline_edit_guard.py", "gh_api_write_guard.py", "permission_prompt_logger.py"):
		(hooks_dir / name).write_bytes((hook_path.parent / name).read_bytes())
	env = dict(os.environ)
	env.pop(guard.ENV_KILL_SWITCH, None)
	env.pop("PYTHONDONTWRITEBYTECODE", None)
	env["HOME"] = str(tmp_path)
	result = subprocess.run(
		[sys.executable, str(hooks_dir / "inline_edit_guard.py")],
		input=_payload("sed -i s/a/b/ f"),
		capture_output=True,
		text=True,
		env=env,
		check=False,
		cwd=str(tmp_path),
	)
	assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
	assert (tmp_path / ".claude" / "permission-prompts" / "sess-4858.jsonl").exists()
	assert not any(path.name == "__pycache__" for path in hooks_dir.rglob("*"))


@pytest.mark.parametrize("stdin_text", ["", "   \n"])
def test_empty_payload_is_allowed_silently(tmp_path, stdin_text):
	result = _run_hook(stdin_text, tmp_path)
	assert result.returncode == 0 and result.stdout == "" and result.stderr == ""


@pytest.mark.parametrize("stdin_text", ["not json", "[1, 2]", "5"])
def test_malformed_payload_fails_open_with_a_warning(tmp_path, stdin_text):
	result = _run_hook(stdin_text, tmp_path)
	assert result.returncode == 0
	output = json.loads(result.stdout)
	assert "hookSpecificOutput" not in output
	assert output["systemMessage"].startswith("Inline-edit guard skipped:")


def test_internal_exception_fails_open(monkeypatch, capsys):
	def boom(*_args, **_kwargs):
		raise RuntimeError("kaboom")

	monkeypatch.setattr(guard, "inline_edit_kind", boom)
	monkeypatch.setattr(sys, "stdin", _Stdin(_payload("sed -i s/a/b/ f")))
	monkeypatch.delenv(guard.ENV_KILL_SWITCH, raising=False)
	assert guard.main() == 0
	output = json.loads(capsys.readouterr().out)
	assert "hookSpecificOutput" not in output and "kaboom" in output["systemMessage"]


def test_missing_tokenizer_fails_open(monkeypatch, tmp_path, capsys):
	monkeypatch.setattr(guard, "_TOKENIZER_PATH", tmp_path / "missing.py")
	monkeypatch.setattr(guard, "_modules", {})
	monkeypatch.setattr(sys, "stdin", _Stdin(_payload("sed -i s/a/b/ f")))
	monkeypatch.delenv(guard.ENV_KILL_SWITCH, raising=False)
	assert guard.main() == 0
	output = json.loads(capsys.readouterr().out)
	assert "hookSpecificOutput" not in output and output["systemMessage"].startswith("Inline-edit guard skipped:")


class _Stdin:
	def __init__(self, text: str):
		self._text = text

	def read(self) -> str:
		return self._text


def test_hook_issues_no_api_calls_and_starts_no_subprocess():
	source = GUARD_PATH.read_text(encoding="utf-8")
	assert "import subprocess" not in source
	assert "api.github.com" not in source
	assert '["gh"' not in source


def test_hook_reuses_the_gh_api_guard_tokenizer():
	source = GUARD_PATH.read_text(encoding="utf-8")
	assert '"gh_api_write_guard.py"' in source
	assert "def shell_segments" not in source and "def strip_heredoc_bodies" not in source


@pytest.mark.parametrize("hook_path", [GUARD_PATH, TEMPLATE_GUARD_PATH], ids=["root", "template"])
def test_tokenizer_api_the_hook_calls_exists(hook_path: Path):
	"""Pin every `tokenizer.<name>` the hook calls to the sibling gh_api_write_guard.py.

	A rename there raises AttributeError inside the hook, which the fail-open path
	swallows, so the guard would stop denying with only a systemMessage warning.
	"""
	source = hook_path.read_text(encoding="utf-8")
	called = sorted(set(re.findall(r"\btokenizer\.([A-Za-z_]\w*)", source)))
	assert {"_command_word_index", "shell_segments", "strip_heredoc_bodies"} <= set(called)
	tokenizer_path = hook_path.parent / "gh_api_write_guard.py"
	spec = importlib.util.spec_from_file_location(f"_tokenizer_api_{hook_path.parent.parent.parent.name}", tokenizer_path)
	assert spec is not None and spec.loader is not None
	tokenizer = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(tokenizer)
	missing = [name for name in called if not callable(getattr(tokenizer, name, None))]
	assert not missing, f"{tokenizer_path} no longer defines {missing}, which {hook_path.name} calls"


@pytest.mark.parametrize("hook_path", [GUARD_PATH, TEMPLATE_GUARD_PATH], ids=["root", "template"])
def test_logger_api_the_hook_calls_exists(hook_path: Path):
	"""Pin every `logger.<name>` the hook calls to the sibling permission_prompt_logger.py.

	`record_deny` runs inside `except Exception: pass`, so a rename there would keep
	the deny but silently drop its `source: inline_edit_guard` record.
	"""
	source = hook_path.read_text(encoding="utf-8")
	called = sorted(set(re.findall(r"\blogger\.([A-Za-z_]\w*)", source)))
	assert {"append_record", "build_record", "log_dir"} <= set(called)
	logger_path = hook_path.parent / "permission_prompt_logger.py"
	spec = importlib.util.spec_from_file_location(f"_logger_api_{hook_path.parent.parent.parent.name}", logger_path)
	assert spec is not None and spec.loader is not None
	logger = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(logger)
	missing = [name for name in called if not callable(getattr(logger, name, None))]
	assert not missing, f"{logger_path} no longer defines {missing}, which {hook_path.name} calls"


@pytest.mark.parametrize("hook_path", [GUARD_PATH, TEMPLATE_GUARD_PATH], ids=["root", "template"])
def test_deny_message_matches_the_filing_exclusion_prefix(hook_path: Path):
	"""`permission_prompts.py` recognises a guard deny without `source` by its reason prefix.

	Rewording `DENY_MESSAGE` so it no longer starts with one of
	`EXPECTED_DENY_REASON_PREFIXES` would file every such deny as a new
	`ai:permission-prompt` pattern.
	"""
	spec = importlib.util.spec_from_file_location(f"_guard_msg_{hook_path.parent.parent.parent.name}", hook_path)
	assert spec is not None and spec.loader is not None
	hook = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(hook)
	script_path = hook_path.parent.parent / "scripts" / "permission_prompts.py"
	spec = importlib.util.spec_from_file_location(f"_pp_prefix_{hook_path.parent.parent.parent.name}", script_path)
	assert spec is not None and spec.loader is not None
	prompts = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(prompts)
	assert hook.DENY_MESSAGE.startswith(prompts.EXPECTED_DENY_REASON_PREFIXES)
	assert prompts.is_expected_deny({"event": "PermissionDenied", "reason": hook.DENY_MESSAGE})
	assert hook.RECORD_SOURCE in prompts.EXPECTED_DENY_SOURCES


@pytest.mark.parametrize("hook_path", [GUARD_PATH, TEMPLATE_GUARD_PATH], ids=["root", "template"])
def test_heredoc_operator_regex_matches_the_tokenizer(hook_path: Path):
	"""Pin `_HEREDOC_OPERATOR_RE` to the regex `strip_heredoc_bodies` uses.

	The hook pairs its Nth operator match in the stripped command with the Nth
	heredoc the tokenizer returns. If the tokenizer's regex changed and the hook's
	did not, `_restore_heredocs` would put a body back into the wrong
	substitution, and the guard would deny the wrong command or miss a write.
	"""
	spec = importlib.util.spec_from_file_location(f"_guard_heredoc_{hook_path.parent.parent.parent.name}", hook_path)
	assert spec is not None and spec.loader is not None
	hook = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(hook)
	tokenizer_path = hook_path.parent / "gh_api_write_guard.py"
	spec = importlib.util.spec_from_file_location(f"_tokenizer_heredoc_{hook_path.parent.parent.parent.name}", tokenizer_path)
	assert spec is not None and spec.loader is not None
	tokenizer = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(tokenizer)
	tokenizer_source = inspect.getsource(tokenizer.strip_heredoc_bodies)
	assert f'r"{hook._HEREDOC_OPERATOR_RE.pattern}"' in tokenizer_source, (
		f"{tokenizer_path.name}'s strip_heredoc_bodies no longer uses the heredoc operator regex "
		f"{hook_path.name} copies as _HEREDOC_OPERATOR_RE; update both together"
	)
	commands = [
		"cat <<EOF\nbody\nEOF",
		"cat <<-'EOF' | python3 -\n\tbody\n\tEOF",
		'python3 - <<"PY" && cat << A\nx\nPY\ny\nA',
		"cat <<A <<B\na\nA\nb\nB",
		'echo "$(python3 - <<\'PY\'\nPath(\'x\').write_text(\'y\')\nPY\n)"',
		"cat <<EOF\nno delimiter",
		"echo a<<b",
	]
	for command in commands:
		stripped, heredocs = tokenizer.strip_heredoc_bodies(command)
		operators = [match for line in stripped.split("\n") for match in hook._HEREDOC_OPERATOR_RE.finditer(line)]
		assert len(operators) == len(heredocs), command


# ──────────────────────────────────────────────────────────────────
# Wiring and docs
# ──────────────────────────────────────────────────────────────────


def _guard_entries(settings: dict) -> list[dict]:
	return [
		entry
		for entry in settings["hooks"]["PreToolUse"]
		if any("inline_edit_guard.py" in hook.get("command", "") for hook in entry.get("hooks", []))
	]


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_settings_wire_the_guard_once_under_bash(path):
	settings = json.loads(path.read_text(encoding="utf-8"))
	entries = _guard_entries(settings)
	assert len(entries) == 1
	entry = entries[0]
	assert entry["matcher"] == guard.SETTINGS_MATCHER == "Bash"
	assert entry["hooks"] == [
		{"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/inline_edit_guard.py', "timeout": 30}
	]


@pytest.mark.parametrize("path", [SETTINGS_PATH, TEMPLATE_SETTINGS_PATH])
def test_other_bash_guards_stay_wired(path):
	settings = json.loads(path.read_text(encoding="utf-8"))
	commands = [
		hook["command"]
		for entry in settings["hooks"]["PreToolUse"]
		if entry.get("matcher") == "Bash"
		for hook in entry["hooks"]
	]
	assert any("pr_merge_status_guard.py" in command for command in commands)
	assert any("gh_api_write_guard.py" in command for command in commands)


def test_template_parity():
	assert TEMPLATE_GUARD_PATH.read_text(encoding="utf-8") == GUARD_PATH.read_text(encoding="utf-8")
	assert TEMPLATE_SETTINGS_PATH.read_text(encoding="utf-8") == SETTINGS_PATH.read_text(encoding="utf-8")
	assert TEMPLATE_PROMPTS_SCRIPT_PATH.read_text(encoding="utf-8") == PROMPTS_SCRIPT_PATH.read_text(encoding="utf-8")


def test_claude_md_documents_the_guard():
	text = CLAUDE_MD.read_text(encoding="utf-8")
	assert ".claude/hooks/inline_edit_guard.py" in text
	assert "CLAUDE_INLINE_EDIT_GUARD=off" in text
	assert "INLINE_EDIT_GUARD action=deny" in text
	assert "tests/test_inline_edit_guard.py" in text


def test_agents_md_documents_the_guard():
	text = AGENTS_MD.read_text(encoding="utf-8")
	assert "inline_edit_guard.py" in text and "CLAUDE_INLINE_EDIT_GUARD" in text


def test_readme_lists_the_guard_among_the_synced_hooks():
	text = README_MD.read_text(encoding="utf-8")
	start = text.index("**Interactive session hooks delivered by the `.claude/` sync:**")
	section = text[start : text.index("\n\n", start)]
	assert "`hooks/inline_edit_guard.py`" in section
	assert "CLAUDE_INLINE_EDIT_GUARD=off" in section
	assert "ships five `PreToolUse`" in section


def test_seed_repo_command_ships_the_hook():
	assert "hooks/inline_edit_guard.py" in SEED_REPO_COMMAND.read_text(encoding="utf-8")


def test_ci_runs_this_file():
	assert "tests/test_inline_edit_guard.py" in CI_WORKFLOW.read_text(encoding="utf-8")
