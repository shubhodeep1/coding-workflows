#!/usr/bin/env python3
"""orchestrate.yml posts the initial orchestrator state as a V2 chunked chain.

Run 40789524145 (tracking issue #8291, 31 issues in 20 waves) failed in
"Build and post initial state": the step passed a ~200 KB V1 state comment
to jq as one argv string, which Linux caps at 128 KiB ("Argument list too
long"), so gh api got an empty body and GitHub answered HTTP 422. A V1 body
that size is also over GitHub's 65,536-byte comment cap. These tests run the
step's post block with a stubbed gh_retry against a state that large and
check the poller's V2 reader gets the same state back.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "orchestrate.yml"
STATE_V2 = ROOT / "scripts" / "orchestrate_state_v2.py"
COMMENT_CAP = 65536


def _step_block(name: str) -> str:
	text = WORKFLOW.read_text(encoding="utf-8")
	start = text.index(f"      - name: {name}\n")
	end = text.find("\n      - name: ", start + 1)
	return text[start:] if end == -1 else text[start:end]


def _post_block() -> str:
	block = _step_block("Build and post initial state")
	start = block.index("          # Post state for the poller")
	end_marker = 'echo "Posted initial state to tracking issue'
	end = block.index("\n", block.index(end_marker))
	return textwrap.dedent(block[start:end]).replace("${{ github.repository }}", "o/r")


def _large_state() -> dict:
	return {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Stuck states — remainder ✓",
		"waves": [
			{
				"wave": wave,
				"issues": [
					{"id": f"item-{wave}-{i}", "github_issue": 1000 + wave * 10 + i, "status": "pending", "body": "Détails " + "x" * 2500}
					for i in range(4)
				],
			}
			for wave in range(1, 21)
		],
	}


def _run_post(state: dict, work: Path) -> tuple[subprocess.CompletedProcess, list[dict]]:
	state_file = work / "state.json"
	state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
	posted = work / "posted"
	posted.mkdir()
	# The stub records each payload and refuses what GitHub refuses.
	stub = textwrap.dedent(f"""\
		set -euo pipefail
		gh_retry() {{
			local inp="" prev="" n
			for a in "$@"; do
				if [ "$prev" = "--input" ]; then inp="$a"; fi
				prev="$a"
			done
			n=$(ls "{posted}" | wc -l)
			if [ "$inp" = "-" ]; then cat > "{posted}/$n.json"; else cp "$inp" "{posted}/$n.json"; fi
			jq -e '(.body | type == "string") and (.body | length > 0) and ((.body | utf8bytelength) <= {COMMENT_CAP})' "{posted}/$n.json" >/dev/null \\
				|| {{ echo "HTTP 422" >&2; return 1; }}
		}}
		export RUNTIME_DIR="{work}"
		export STATE_FILE="{state_file}"
		export TRACKING_ISSUE_NUMBER=8291
		cd "{ROOT}"
		""") + _post_block() + "\n"
	script = work / "post.sh"
	script.write_text(stub, encoding="utf-8")
	result = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False)
	comments = [
		{"id": 100 + i, "user": {"login": "bot"}, "body": json.loads(path.read_text(encoding="utf-8"))["body"]}
		for i, path in enumerate(sorted(posted.iterdir(), key=lambda p: int(p.stem)))
	]
	return result, comments


def _extract(comments: list[dict], work: Path) -> dict:
	comments_file = work / "comments.json"
	comments_file.write_text(json.dumps(comments), encoding="utf-8")
	result = subprocess.run(
		[sys.executable, "-I", "-B", str(STATE_V2), "extract", "--comments-json", str(comments_file)],
		capture_output=True, text=True, check=False,
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def test_large_initial_state_posts_as_v2_chunks_the_poller_can_read() -> None:
	state = _large_state()
	with tempfile.TemporaryDirectory() as tmp:
		work = Path(tmp)
		result, comments = _run_post(state, work)
		assert result.returncode == 0, result.stderr
		assert (work / "state.json").stat().st_size > 128 * 1024
		assert len(comments) > 1
		for comment in comments:
			assert len(comment["body"].encode("utf-8")) <= COMMENT_CAP
			assert comment["body"].startswith("<!-- ORCHESTRATOR_STATE_V2 part=")
		assert f"parts={len(comments)}" in result.stdout
		assert _extract(comments, work) == state


def test_small_initial_state_posts_one_v2_chunk() -> None:
	state = {"schema_version": "orchestrate_state.v1", "waves": [{"wave": 1, "issues": []}]}
	with tempfile.TemporaryDirectory() as tmp:
		work = Path(tmp)
		result, comments = _run_post(state, work)
		assert result.returncode == 0, result.stderr
		assert len(comments) == 1
		assert _extract(comments, work) == state


def test_initial_state_never_goes_through_argv_or_v1() -> None:
	block = _step_block("Build and post initial state")
	assert "--arg body" not in block
	assert "ORCHESTRATOR_STATE_V1" not in block
	assert "python3 scripts/orchestrate_state_v2.py pack" in block


def test_orchestrate_stages_the_v2_state_helper_in_the_required_loop() -> None:
	stage = _step_block("Stage workflow support files")
	required = stage.split("for f in ", 1)[1].split("; do", 1)[0].split()
	assert "orchestrate_state_v2.py" in required


def main() -> int:
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	for func in test_functions:
		func()
	print(f"OK: {len(test_functions)} orchestrate initial-state V2 tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
