#!/usr/bin/env python3
"""Host-side Python launches never import from the checkout (issue #4568).

A host `python3 -` or `python3 <script>` puts the working directory (or the
script's directory) first on sys.path and honours PYTHONPATH and user
site-packages. Run from a checkout of a same-repository branch, a hostile
`pathlib.py`, `json.py` or `sitecustomize.py` there executes before any
sandbox and can read the credentials in the environment. Every launch below
runs with `-I -B`: the working directory, the script directory, PYTHON*
variables and user site-packages stay off the import path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

# file -> (isolated launches it must contain, count of `python3 -I -B - ` heredocs)
ISOLATED_LAUNCHES: dict[str, tuple[tuple[str, ...], int]] = {
	"scripts/clarify_isolated_run.sh": (
		("python3 -I -B scripts/clarify_openrouter_broker.py broker",), 0,
	),
	"scripts/review_untrusted_sandbox.sh": (
		('python3 -I -B "${support}/clarify_openrouter_broker.py" review-broker',), 0,
	),
	"scripts/workspace_init.sh": ((), 2),
	"scripts/run_workspace_hook.sh": ((), 3),
	"scripts/write_guard.sh": ((), 1),
	"scripts/review_conflict_resolve.sh": (('python3 -I -B - "${_state_action}"',), 2),
	"scripts/ledger_emit_substate.sh": ((), 2),
	"scripts/review_agents_md_materiality.sh": ((), 1),
	"scripts/review_filter_uninteresting_files.sh": ((), 1),
	"scripts/review_reject_verify.sh": ((), 1),
	"scripts/post_review_comment.sh": ((), 2),
	"scripts/drift_audit.sh": ((), 1),
	"scripts/check_resolver_diff.sh": (
		(
			'python3 -I -B -m py_compile "${touched}"',
			'python3 -I -B -c "import json,sys; json.load(open(sys.argv[1]))" "${touched}"',
			'python3 -I -B "${checker}"',
		),
		0,
	),
}


def test_host_python_launches_use_isolated_mode() -> None:
	for rel_path, (launches, heredocs) in ISOLATED_LAUNCHES.items():
		text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
		for launch in launches:
			assert launch in text, f"{rel_path}: missing isolated launch {launch!r}"
		assert text.count("python3 -I -B - ") >= heredocs, rel_path
		# -I ignores PYTHONDONTWRITEBYTECODE; a leftover prefix marks a launch
		# that was not converted.
		assert "PYTHONDONTWRITEBYTECODE=1 python3 - " not in text, rel_path
		assert "PYTHONDONTWRITEBYTECODE=1 python3 -m " not in text, rel_path


def test_brokers_receive_the_credential_but_run_isolated() -> None:
	clarify = (REPO_ROOT / "scripts/clarify_isolated_run.sh").read_text(encoding="utf-8")
	review = (REPO_ROOT / "scripts/review_untrusted_sandbox.sh").read_text(encoding="utf-8")
	for text in (clarify, review):
		broker_line = next(line for line in text.splitlines() if 'OPENROUTER_API_KEY="${OPENROUTER_API_KEY}"' in line)
		assert broker_line.startswith("env -i PATH=")
		assert "PYTHONDONTWRITEBYTECODE" not in broker_line


def test_ledger_memory_child_runs_isolated_with_its_support_directory() -> None:
	text = (REPO_ROOT / "scripts/ledger_emit_substate.sh").read_text(encoding="utf-8")
	child = text[text.index("\tcmd = [\n\t\tsys.executable,\n"):]
	child = child[:child.index("ai_memory_script,")]
	assert '"-I",' in child and '"-B",' in child
	assert "sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0])))" in child
	assert "runpy.run_path(sys.argv[0], run_name='__main__')" in child


def test_heredoc_helper_ignores_hostile_checkout_modules(tmp_path: Path) -> None:
	checkout = tmp_path / "checkout"
	checkout.mkdir()
	marker = tmp_path / "hijacked"
	hostile = (
		"import os\n"
		"open(os.environ['HIJACK_MARKER'], 'w').write(os.environ.get('OPENROUTER_API_KEY', ''))\n"
	)
	# Neither module is imported at interpreter startup, so a plain `python3 -`
	# would load these from the working directory.
	for name in ("argparse.py", "fnmatch.py", "sitecustomize.py"):
		(checkout / name).write_text(hostile, encoding="utf-8")
	diff_file = tmp_path / "input.diff"
	diff_file.write_text("", encoding="utf-8")
	env = os.environ.copy()
	env.update({
		"HIJACK_MARKER": str(marker),
		"OPENROUTER_API_KEY": "test-only-credential",
		"PYTHONPATH": str(checkout),
	})
	result = subprocess.run(
		[
			"bash", str(REPO_ROOT / "scripts/review_filter_uninteresting_files.sh"),
			"--diff-file", str(diff_file),
			"--output-diff", str(tmp_path / "out.diff"),
			"--kept-paths-file", str(tmp_path / "kept.txt"),
			"--skipped-paths-file", str(tmp_path / "skipped.txt"),
			"--repo-root", str(checkout),
		],
		cwd=checkout, env=env, capture_output=True, text=True, timeout=60,
	)
	assert result.returncode == 0, result.stderr
	assert not marker.exists(), "host Python imported a module from the checkout"
	assert "test-only-credential" not in result.stdout + result.stderr


def main() -> int:
	import inspect

	for name, func in sorted(globals().items()):
		if name.startswith("test_") and callable(func):
			if "tmp_path" in inspect.signature(func).parameters:
				with tempfile.TemporaryDirectory() as fixture_tmp_dir:
					func(tmp_path=Path(fixture_tmp_dir))
			else:
				func()
	print("OK: host Python launches are isolated")
	return 0


if __name__ == "__main__":
	sys.exit(main())
