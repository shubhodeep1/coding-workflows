#!/usr/bin/env python3
"""Regression tests for issue #7658: PR comment JSON must not reach jq argv.

``_gh_pr_with_all_comments_rest`` (scripts/gh_helpers.sh) built its result
with ``jq -cn --argjson comments "${comments_json}" ...``. Linux caps one
command-line argument at 131,072 bytes (MAX_ARG_STRLEN), so once a PR's issue
comments serialized past that, jq failed with "Argument list too long" (exit
126) and printed nothing. The review-blocked judge then fell back to ``{}``
and decided with no comments (tele-funtoken-msg-scoring PR #3933, run
39131897079). The JSON now goes to jq through ``--slurpfile`` with process
substitution, and the judge logs ``RB_JUDGE_CONTEXT_EMPTY`` when the context
still fails to assemble.

These tests drive the helper and the judges' context-assembly blocks with a
fake ``gh`` on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"

ARG_MAX_STRLEN = 131072
COMMENT_COUNT = 14
COMMENT_CHARS = 12000

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required to run the helpers")

# Serves canned JSON by endpoint shape. A missing file prints nothing and
# exits 0, which is how the empty-input cases are produced.
FAKE_GH = r"""#!/usr/bin/env bash
d="${FAKE_GH_DIR}"
for a in "$@"; do
	case "$a" in
		graphql) cat "$d/graphql.json" 2>/dev/null; exit 0 ;;
		*/issues/*/comments*) cat "$d/comments.json" 2>/dev/null; exit 0 ;;
		*/pulls/*/comments*) cat "$d/review.json" 2>/dev/null; exit 0 ;;
		repos/*/pulls/*) cat "$d/meta.json" 2>/dev/null; exit 0 ;;
	esac
done
exit 0
"""


def _raw_comments() -> list[dict]:
	return [
		{
			"user": {"login": f"user{i:02d}"},
			"body": f"comment-{i:02d}-" + ("x" * COMMENT_CHARS),
			"created_at": f"2026-10-18T00:{i:02d}:00Z",
		}
		for i in range(COMMENT_COUNT)
	]


def _expected_bodies() -> list[str]:
	return [c["body"] for c in _raw_comments()]


def _setup(tmp_path: Path, files: dict[str, object]) -> dict[str, str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	fake = bin_dir / "gh"
	fake.write_text(FAKE_GH, encoding="utf-8")
	fake.chmod(0o755)
	data = tmp_path / "data"
	data.mkdir()
	for name, value in files.items():
		text = value if isinstance(value, str) else json.dumps(value)
		(data / name).write_text(text, encoding="utf-8")
	tmpdir = tmp_path / "tmpdir"
	tmpdir.mkdir()
	env = {
		key: value
		for key, value in os.environ.items()
		if key not in {"TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "TG_CHAT_ID"}
	}
	env.update(
		{
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"FAKE_GH_DIR": str(data),
			"TMPDIR": str(tmpdir),
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"GH_RATE_LIMIT_BREAKER_FILE": str(tmp_path / "breaker"),
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	return env


def _run(tmp_path: Path, env: dict[str, str], body: str, source_helpers: bool = True) -> subprocess.CompletedProcess[str]:
	script = "set -euo pipefail\n"
	if source_helpers:
		script += f"source '{GH_HELPERS}'\n"
	script += "sleep() { :; }\n" + body + "\n"
	return subprocess.run(
		["bash", "-c", script], env=env, capture_output=True, text=True, cwd=tmp_path, timeout=120,
	)


def _marker_block(path: Path, begin: str, end: str) -> str:
	text = path.read_text(encoding="utf-8")
	start = text.index(begin)
	stop = text.index(end, start) + len(end)
	return text[start:stop]


def _judge_block() -> str:
	return _marker_block(
		RB_JUDGE,
		"# BEGIN rb judge context assembly (#7658)",
		"# END rb judge context assembly (#7658)",
	)


def _poller_block() -> str:
	return _marker_block(
		POLLER,
		"# Comment JSON reaches jq through --slurpfile, never argv",
		"unset _rb_ctx_rc _rb_ctx_empty_reason",
	)


def test_large_comment_history_is_over_the_argv_limit() -> None:
	payload = json.dumps(
		[{"author": c["user"]["login"], "body": c["body"], "created_at": c["created_at"]} for c in _raw_comments()],
		separators=(",", ":"),
	)
	assert len(payload.encode("utf-8")) > ARG_MAX_STRLEN


@requires_jq
def test_rest_helper_returns_every_comment_past_the_argv_limit(tmp_path: Path) -> None:
	env = _setup(tmp_path, {"comments.json": _raw_comments(), "review.json": []})
	meta = json.dumps({"title": "t", "body": "b", "head_ref": "h", "base_ref": "main", "head_sha": "abc"})
	result = _run(tmp_path, env, f"_gh_pr_with_all_comments_rest o r 1 '{meta}' > out.json")
	assert result.returncode == 0, result.stderr
	assert "Argument list too long" not in result.stderr
	out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
	assert list(out.keys()) == ["meta", "comments", "review_comments"]
	assert [c["body"] for c in out["comments"]] == _expected_bodies()
	assert out["review_comments"] == []
	assert out["meta"]["title"] == "t"


@requires_jq
def test_rest_helper_degrades_empty_inputs_to_empty_object_and_arrays(tmp_path: Path) -> None:
	# The fake gh prints nothing for every endpoint and no meta is preloaded,
	# so the metadata transform yields an empty string.
	env = _setup(tmp_path, {})
	result = _run(tmp_path, env, "_gh_pr_with_all_comments_rest o r 1")
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == '{"meta":{},"comments":[],"review_comments":[]}'


@requires_jq
def test_rest_helper_round_trips_a_large_meta_body(tmp_path: Path) -> None:
	big_body = "\n" * 70000 + "end"
	env = _setup(tmp_path, {"comments.json": [], "review.json": []})
	meta_file = tmp_path / "meta.json"
	meta_file.write_text(json.dumps({"title": "t", "body": big_body}), encoding="utf-8")
	result = _run(tmp_path, env, f"_gh_pr_with_all_comments_rest o r 1 \"$(cat '{meta_file}')\" > out.json")
	assert result.returncode == 0, result.stderr
	out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
	assert out["meta"]["body"] == big_body
	assert out["comments"] == [] and out["review_comments"] == []


@requires_jq
def test_graphql_has_next_page_fallback_returns_every_comment(tmp_path: Path) -> None:
	graphql = {
		"data": {
			"repository": {
				"pullRequest": {
					"title": "t",
					"body": "b",
					"headRefName": "h",
					"baseRefName": "main",
					"headRefOid": "abc",
					"comments": {"nodes": [], "pageInfo": {"hasNextPage": True}},
					"reviews": {"nodes": [], "pageInfo": {"hasNextPage": False}},
				}
			}
		}
	}
	meta = {"title": "t", "body": "b", "head": {"ref": "h", "sha": "abc"}, "base": {"ref": "main"}}
	env = _setup(
		tmp_path,
		{"graphql.json": graphql, "comments.json": _raw_comments(), "review.json": [], "meta.json": meta},
	)
	result = _run(tmp_path, env, "gh_pr_with_all_comments o r 1 > out.json")
	assert result.returncode == 0, result.stderr
	assert "reason=graphql_has_next_page" in result.stderr
	out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
	assert [c["body"] for c in out["comments"]] == _expected_bodies()
	assert out["meta"]["head_sha"] == "abc"


def _judge_env_prefix() -> str:
	return "REPOSITORY=o/r\nPR_NUMBER=7\nPRELOADED_PR_META='{}'\n"


def test_judge_warns_once_when_context_assembly_fails(tmp_path: Path) -> None:
	env = _setup(tmp_path, {})
	body = (
		_judge_env_prefix()
		+ "gh_pr_with_all_comments() { return 126; }\n"
		+ _judge_block()
		+ "\nprintf '%s' \"${PR_CONTEXT_JSON}\" > ctx.json\n"
	)
	result = _run(tmp_path, env, body, source_helpers=False)
	assert result.returncode == 0, result.stderr
	warnings = [l for l in result.stderr.splitlines() if "RB_JUDGE_CONTEXT_EMPTY" in l]
	assert warnings == ["::warning::RB_JUDGE_CONTEXT_EMPTY pr=7 reason=assembly_failed"]
	assert (tmp_path / "ctx.json").read_text(encoding="utf-8") == "{}"


@requires_jq
def test_judge_warns_on_invalid_shape(tmp_path: Path) -> None:
	env = _setup(tmp_path, {})
	body = (
		_judge_env_prefix()
		+ "gh_pr_with_all_comments() { printf '%s' '{\"meta\":{},\"comments\":null}'; }\n"
		+ _judge_block()
	)
	result = _run(tmp_path, env, body, source_helpers=False)
	assert result.returncode == 0, result.stderr
	assert "::warning::RB_JUDGE_CONTEXT_EMPTY pr=7 reason=invalid_shape" in result.stderr


@requires_jq
def test_judge_does_not_warn_for_a_pr_without_comments(tmp_path: Path) -> None:
	env = _setup(tmp_path, {})
	body = (
		_judge_env_prefix()
		+ "gh_pr_with_all_comments() { printf '%s' '{\"meta\":{},\"comments\":[],\"review_comments\":[]}'; }\n"
		+ _judge_block()
	)
	result = _run(tmp_path, env, body, source_helpers=False)
	assert result.returncode == 0, result.stderr
	assert "RB_JUDGE_CONTEXT_EMPTY" not in result.stderr


@requires_jq
def test_judge_inline_fallback_keeps_every_comment(tmp_path: Path) -> None:
	env = _setup(tmp_path, {"comments.json": _raw_comments(), "review.json": []})
	body = (
		"unset -f gh_pr_with_all_comments _gh_pr_with_all_comments_rest\n"
		+ _judge_env_prefix()
		+ _judge_block()
		+ "\nprintf '%s' \"${PR_CONTEXT_JSON}\" > ctx.json\n"
	)
	result = _run(tmp_path, env, body)
	assert result.returncode == 0, result.stderr
	assert "reason=helper_unavailable" in result.stderr
	assert "RB_JUDGE_CONTEXT_EMPTY" not in result.stderr
	out = json.loads((tmp_path / "ctx.json").read_text(encoding="utf-8"))
	assert [c["body"] for c in out["comments"]] == _expected_bodies()


@requires_jq
def test_poller_judge_inline_fallback_and_warning(tmp_path: Path) -> None:
	env = _setup(tmp_path, {"comments.json": _raw_comments(), "review.json": []})
	prefix = "GITHUB_REPOSITORY=o/r\nRB_PR=9\nRB_PRELOADED_META='{}'\n"
	body = (
		"unset -f gh_pr_with_all_comments _gh_pr_with_all_comments_rest\n"
		+ prefix
		+ _poller_block()
		+ "\nprintf '%s' \"${RB_PR_CONTEXT_JSON}\" > ctx.json\n"
	)
	result = _run(tmp_path, env, body)
	assert result.returncode == 0, result.stderr
	assert "RB_JUDGE_CONTEXT_EMPTY" not in result.stderr
	out = json.loads((tmp_path / "ctx.json").read_text(encoding="utf-8"))
	assert [c["body"] for c in out["comments"]] == _expected_bodies()

	failing = prefix + "gh_pr_with_all_comments() { return 126; }\n" + _poller_block()
	result = _run(tmp_path, env, failing, source_helpers=False)
	assert result.returncode == 0, result.stderr
	warnings = [l for l in result.stderr.splitlines() if "RB_JUDGE_CONTEXT_EMPTY" in l]
	assert warnings == ["::warning::RB_JUDGE_CONTEXT_EMPTY pr=9 reason=assembly_failed"]


ARGV_COMMENT_JSON = re.compile(r"--argjson\s+(meta|comments|review_comments)\b")


def test_no_comment_json_is_passed_through_argjson() -> None:
	helpers = GH_HELPERS.read_text(encoding="utf-8")
	start = helpers.index("_gh_pr_with_all_comments_rest()\n{")
	stop = helpers.index("\n}\n", start)
	assert not ARGV_COMMENT_JSON.search(helpers[start:stop])
	assert not ARGV_COMMENT_JSON.search(_judge_block())
	assert not ARGV_COMMENT_JSON.search(_poller_block())
	poller = POLLER.read_text(encoding="utf-8")
	assert '--argjson comments "${RB_ISOLATION_COMMENTS}"' not in poller
	assert '--slurpfile comments <(printf \'%s\' "${RB_ISOLATION_COMMENTS}")' in poller


def test_other_unbounded_github_json_is_not_passed_through_argjson() -> None:
	poller = POLLER.read_text(encoding="utf-8")
	assert '--argjson details "${candidate_details_json}"' not in poller
	assert '--argjson candidate_details_json "${candidate_details_json}"' not in poller
	assert '--argjson state "${state_json}"' not in poller
	assert '--slurpfile details <(printf \'%s\' "${candidate_details_json}")' in poller
	assert '--slurpfile candidate_details_json <(printf \'%s\' "${candidate_details_json}")' in poller
	helpers = GH_HELPERS.read_text(encoding="utf-8")
	assert '--argjson pr "${pr_json}"' not in helpers
	assert '--slurpfile pr <(printf \'%s\' "${pr_json}")' in helpers
	assert '--argjson pr_lookup "${pr_lookup_json}"' not in helpers
	assert '--slurpfile pr_lookup <(printf \'%s\' "${pr_lookup_json}")' in helpers


def test_both_judges_log_the_empty_context_prefix() -> None:
	assert "RB_JUDGE_CONTEXT_EMPTY" in RB_JUDGE.read_text(encoding="utf-8")
	assert "RB_JUDGE_CONTEXT_EMPTY" in POLLER.read_text(encoding="utf-8")
