#!/usr/bin/env python3
"""Tests for scripts/stable_hold.sh and the release jobs' hold-clear step (#9342)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "stable_hold.sh"
RELEASE_WORKFLOWS = (
	REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml",
	REPO_ROOT / ".github" / "workflows" / "mark-stable.yml",
)
CONTENTS_PATH = "repos/owner/repo/contents/stable-hold.json"

# Fake GitHub contents / git-data API for the hold branch. State keys:
#   branch (bool), file (str|None), sha (str), read_error (bool),
#   raw_payload (dict), put_conflicts (int), put_error (bool), ref_race (bool)
MOCK_GH = r'''#!/usr/bin/env python3
import base64, json, os, sys
from pathlib import Path
state_path = Path(os.environ["MOCK_GH_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def save():
    state_path.write_text(json.dumps(state))
def respond(payload):
    save(); sys.stdout.write(json.dumps(payload)); sys.exit(0)
def fail(msg):
    save(); sys.stderr.write(msg + "\n"); sys.exit(1)
if args[:1] != ["api"]:
    fail("unexpected gh call " + " ".join(args))
method, path, rest, i = "GET", None, args[1:], 0
while i < len(rest):
    a = rest[i]
    if a in ("-X", "--method"):
        method = rest[i + 1]; i += 2; continue
    if a in ("-f", "-F", "--input", "-H"):
        i += 2; continue
    if path is None and not a.startswith("-"):
        path = a
    i += 1
body = json.loads(sys.stdin.read() or "null") if "--input" in args else None
def next_sha():
    state["sha_counter"] = state.get("sha_counter", 0) + 1
    return format(state["sha_counter"], "x").rjust(40, "a")
if path.startswith("repos/owner/repo/contents/stable-hold.json?ref="):
    assert path.endswith("?ref=ai%2Fstable-hold"), path
    if state.get("read_error"):
        fail("HTTP 503: Service Unavailable")
    if "raw_payload" in state:
        respond(state["raw_payload"])
    if not state.get("branch"):
        fail("gh: No commit found for the ref ai/stable-hold (HTTP 404)")
    if state.get("file") is None:
        fail("gh: Not Found (HTTP 404)")
    encoded = base64.b64encode(state["file"].encode()).decode()
    encoded = "\n".join(encoded[j:j + 60] for j in range(0, len(encoded), 60))
    respond({"sha": state["sha"], "encoding": "base64", "content": encoded})
if path == "repos/owner/repo/contents/stable-hold.json" and method == "PUT":
    state.setdefault("puts", []).append(body)
    if state.get("put_error"):
        fail("HTTP 403: Resource not accessible by integration")
    if state.get("put_conflicts", 0) > 0:
        state["put_conflicts"] -= 1
        state["sha"] = next_sha()
        fail("HTTP 409: stable-hold.json does not match " + body.get("sha", ""))
    if state.get("file") is not None and body.get("sha") != state["sha"]:
        fail("HTTP 409: sha mismatch")
    assert body["branch"] == "ai/stable-hold"
    state["file"] = base64.b64decode(body["content"]).decode()
    state["sha"] = next_sha()
    respond({"content": {"sha": state["sha"]}})
if path == "repos/owner/repo/git/ref/heads/ai/stable-hold":
    if state.get("branch"):
        respond({"object": {"sha": "c" * 40}})
    fail("gh: Not Found (HTTP 404)")
if path == "repos/owner/repo/git/trees" and method == "POST":
    state["pending_tree"] = body
    respond({"sha": "d" * 40})
if path == "repos/owner/repo/git/commits" and method == "POST":
    state["pending_commit"] = body
    respond({"sha": "e" * 40})
if path == "repos/owner/repo/git/refs" and method == "POST":
    state.setdefault("ref_creates", []).append(body)
    if state.get("ref_race"):
        state["ref_race"] = False
        state["branch"] = True
        state["file"] = None
        fail("HTTP 422: Reference already exists")
    state["branch"] = True
    state["file"] = state["pending_tree"]["tree"][0]["content"]
    state["sha"] = next_sha()
    respond({"ref": body["ref"]})
fail("unexpected api path " + str(path))
'''


def _hold(active: bool = True, to: str = "v1.45.0", **extra: object) -> str:
	doc = {
		"schema": "stable_hold.v1",
		"active": active,
		"held_at": "2026-11-12T00:00:00Z",
		"from_version": "v1.46.0",
		"to_version": to,
		"reason": "regression",
		"run_url": "https://github.com/owner/repo/actions/runs/1",
		"cleared_at": None,
		"cleared_by_version": None,
	}
	doc.update(extra)
	return json.dumps(doc) + "\n"


def _run(tmp: Path, state: dict, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
	bin_dir = tmp / "bin"
	bin_dir.mkdir(exist_ok=True)
	(bin_dir / "gh").write_text(MOCK_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	state_file = tmp / "state.json"
	state_file.write_text(json.dumps(state), encoding="utf-8")
	env = os.environ.copy()
	env.update(
		{
			"PATH": f"{bin_dir}:{env['PATH']}",
			"MOCK_GH_STATE": str(state_file),
			"GITHUB_REPOSITORY": "owner/repo",
			"GH_TOKEN": "x",
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	for key in ("BASH_ENV", "ENV", "STABLE_HOLD_BRANCH", "STABLE_HOLD_FILE"):
		env.pop(key, None)
	proc = subprocess.run(["bash", str(SCRIPT), *args], cwd=str(tmp), env=env, text=True, capture_output=True, timeout=60)
	return proc, json.loads(state_file.read_text(encoding="utf-8"))


def _read(state: dict) -> str:
	with tempfile.TemporaryDirectory() as tmp:
		proc, _ = _run(Path(tmp), state, "read")
	assert proc.returncode == 0, proc.stderr
	return proc.stdout.strip()


SET_ARGS = ("set", "--from", "v1.46.0", "--to", "v1.45.0", "--reason", "bad release", "--run-url", "https://github.com/owner/repo/actions/runs/42")


def test_read_active() -> None:
	assert _read({"branch": True, "file": _hold(), "sha": "f" * 40}) == "active v1.45.0"


def test_read_inactive_when_branch_or_file_missing_or_cleared() -> None:
	assert _read({"branch": False}) == "inactive"
	assert _read({"branch": True, "file": None}) == "inactive"
	assert _read({"branch": True, "file": _hold(active=False), "sha": "f" * 40}) == "inactive"


def test_read_unknown_fails_closed() -> None:
	assert _read({"read_error": True}) == "unknown"
	assert _read({"branch": True, "file": "{not json", "sha": "f" * 40}) == "unknown"
	assert _read({"branch": True, "file": _hold(schema="stable_hold.v2"), "sha": "f" * 40}) == "unknown"
	assert _read({"branch": True, "file": _hold(active="yes"), "sha": "f" * 40}) == "unknown"
	assert _read({"branch": True, "file": _hold(to="1.45"), "sha": "f" * 40}) == "unknown"
	assert _read({"raw_payload": {"sha": "f" * 40, "encoding": "none", "content": ""}}) == "unknown"


def test_set_bootstraps_orphan_branch() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), {"branch": False}, *SET_ARGS)
	assert proc.returncode == 0, proc.stderr
	assert "STABLE_HOLD outcome=set from=v1.46.0 to=v1.45.0" in proc.stdout
	assert state["pending_commit"]["parents"] == []
	assert state["ref_creates"] == [{"ref": "refs/heads/ai/stable-hold", "sha": "e" * 40}]
	doc = json.loads(state["file"])
	assert doc["schema"] == "stable_hold.v1" and doc["active"] is True
	assert doc["from_version"] == "v1.46.0" and doc["to_version"] == "v1.45.0"
	assert doc["reason"] == "bad release"
	assert doc["run_url"] == "https://github.com/owner/repo/actions/runs/42"
	assert doc["held_at"].endswith("Z") and doc["cleared_at"] is None
	assert not state.get("puts")


def test_set_after_branch_create_race_writes_through_contents_api() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), {"branch": False, "ref_race": True}, *SET_ARGS)
	assert proc.returncode == 0, proc.stderr
	assert len(state["puts"]) == 1
	assert "sha" not in state["puts"][0]
	assert json.loads(state["file"])["to_version"] == "v1.45.0"


def test_set_then_clear_round_trip_keeps_fields() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		proc, state = _run(tmp_path, {"branch": True, "file": _hold(active=False, to="v1.40.0"), "sha": "f" * 40}, *SET_ARGS)
		assert proc.returncode == 0, proc.stderr
		assert state["puts"][0]["sha"] == "f" * 40
		state.pop("calls", None)
		(tmp_path / "bin" / "gh").unlink()
		proc, state = _run(tmp_path, state, "clear", "--version", "v1.47.0")
	assert proc.returncode == 0, proc.stderr
	assert "STABLE_HOLD outcome=cleared version=v1.47.0" in proc.stdout
	doc = json.loads(state["file"])
	assert doc["active"] is False
	assert doc["cleared_by_version"] == "v1.47.0"
	assert doc["cleared_at"].endswith("Z")
	assert doc["to_version"] == "v1.45.0" and doc["from_version"] == "v1.46.0" and doc["reason"] == "bad release"


def test_set_same_target_writes_nothing() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), {"branch": True, "file": _hold(), "sha": "f" * 40}, *SET_ARGS)
	assert proc.returncode == 0, proc.stderr
	assert "STABLE_HOLD outcome=unchanged to=v1.45.0" in proc.stdout
	assert not state.get("puts")


def test_set_retries_once_on_conflict() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), {"branch": True, "file": _hold(active=False), "sha": "f" * 40, "put_conflicts": 1}, *SET_ARGS)
	assert proc.returncode == 0, proc.stderr
	assert len(state["puts"]) == 2
	assert json.loads(state["file"])["active"] is True


def test_set_failures_exit_nonzero() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), {"branch": True, "file": None, "put_error": True}, *SET_ARGS)
	assert proc.returncode == 1
	assert "STABLE_HOLD outcome=set_failed reason=write_failed" in proc.stdout
	with tempfile.TemporaryDirectory() as tmp:
		proc, state = _run(Path(tmp), {"read_error": True}, *SET_ARGS)
	assert proc.returncode == 1
	assert "STABLE_HOLD outcome=set_failed reason=read_unknown" in proc.stdout
	assert not state.get("puts")


def test_set_rejects_invalid_fields() -> None:
	bad = [
		("set", "--from", "1.46.0", "--to", "v1.45.0", "--reason", "r", "--run-url", "https://x/y"),
		("set", "--from", "v1.46.0", "--to", "v1.45", "--reason", "r", "--run-url", "https://x/y"),
		("set", "--from", "v1.46.0", "--to", "v1.45.0", "--reason", "   ", "--run-url", "https://x/y"),
		("set", "--from", "v1.46.0", "--to", "v1.45.0", "--reason", "x" * 201, "--run-url", "https://x/y"),
		("set", "--from", "v1.46.0", "--to", "v1.45.0", "--reason", "r", "--run-url", "http://x/y"),
	]
	for args in bad:
		with tempfile.TemporaryDirectory() as tmp:
			proc, state = _run(Path(tmp), {"branch": True, "file": None}, *args)
		assert proc.returncode == 1, args
		assert "STABLE_HOLD outcome=set_failed" in proc.stdout, args
		assert not state.get("calls"), args


def test_clear_when_not_active_writes_nothing() -> None:
	for state in ({"branch": False}, {"branch": True, "file": _hold(active=False), "sha": "f" * 40}):
		with tempfile.TemporaryDirectory() as tmp:
			proc, final = _run(Path(tmp), state, "clear", "--version", "v1.47.0")
		assert proc.returncode == 0, proc.stderr
		assert "STABLE_HOLD outcome=not_active" in proc.stdout
		assert not final.get("puts")


def test_clear_fails_on_unknown_or_write_error() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, _ = _run(Path(tmp), {"read_error": True}, "clear", "--version", "v1.47.0")
	assert proc.returncode == 1
	assert "STABLE_HOLD outcome=clear_failed reason=read_unknown" in proc.stdout
	with tempfile.TemporaryDirectory() as tmp:
		proc, _ = _run(Path(tmp), {"branch": True, "file": _hold(), "sha": "f" * 40, "put_error": True}, "clear", "--version", "v1.47.0")
	assert proc.returncode == 1
	assert "STABLE_HOLD outcome=clear_failed reason=write_failed" in proc.stdout


def _step_block(text: str, name: str) -> str:
	marker = f"      - name: {name}\n"
	assert text.count(marker) == 1, name
	start = text.index(marker)
	end = text.find("\n      - name: ", start + len(marker))
	block = text[start:end if end != -1 else len(text)]
	# Comment lines that introduce the next step belong to that step.
	lines = block.splitlines()
	while lines and lines[-1].strip().startswith("#") or (lines and not lines[-1].strip()):
		lines.pop()
	return "\n".join(lines)


def _if_line(block: str) -> str:
	return next(line.strip() for line in block.splitlines() if line.strip().startswith("if:"))


def test_release_jobs_clear_hold_after_tag_publish() -> None:
	for workflow in RELEASE_WORKFLOWS:
		text = workflow.read_text(encoding="utf-8")
		tag_block = _step_block(text, "Tag version and update stable pointer")
		clear_block = _step_block(text, "Clear stable rollback hold")
		tag_end = text.index(tag_block) + len(tag_block)
		between = text[tag_end:text.index(clear_block)]
		assert "- name:" not in between, f"{workflow.name}: the clear step must directly follow the tag step"
		assert _if_line(clear_block) == _if_line(tag_block), workflow.name
		assert "GH_TOKEN: ${{ github.token }}" in clear_block, workflow.name
		assert "RELEASE_VERSION: ${{ needs.resolve-version.outputs.version }}" in clear_block, workflow.name
		assert 'if ! bash scripts/stable_hold.sh clear --version "${RELEASE_VERSION}"; then' in clear_block, workflow.name
		assert "::warning::" in clear_block, workflow.name
		assert "continue-on-error" not in clear_block, workflow.name
		assert "${{" not in clear_block.split("run: |", 1)[1], f"{workflow.name}: no expressions inside run"
