#!/usr/bin/env python3
"""Contract tests for phase skip/gate telemetry in reusable workflows."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
CLARIFY_WF = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
IMPLEMENT_WF = REPO_ROOT / ".github" / "workflows" / "implement.yml"
ORCH_CLARIFY_RESPOND_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_clarify_respond.yml"
ORCH_PARSE_ANSWER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_parse_and_post_answer.sh"
WORKSPACE_SOURCE_MANIFEST = REPO_ROOT / ".ai" / ".workspace_source_manifest.txt"
CI_WF = REPO_ROOT / ".github" / "workflows" / "ci.yml"
AGENTS_MD = REPO_ROOT / "agents.md"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _step_block(path: Path, step_name: str) -> str:
	marker = f"- name: {step_name}"
	lines = _read(path).splitlines()
	start = next((i for i, line in enumerate(lines) if line.lstrip() == marker), -1)
	assert start != -1, f"Missing workflow step: {step_name} in {path}"
	indent = len(lines[start]) - len(lines[start].lstrip())
	block = [lines[start]]
	for line in lines[start + 1 :]:
		stripped = line.lstrip()
		line_indent = len(line) - len(stripped)
		if stripped and line_indent < indent:
			break
		if stripped.startswith("- name:") and line_indent == indent:
			break
		block.append(line)
	return "\n".join(block)


def _assert_before(block: str, earlier: str, later: str) -> None:
	earlier_idx = block.find(earlier)
	assert earlier_idx != -1, f"Missing earlier marker: {earlier}"
	later_idx = block.find(later, earlier_idx)
	assert later_idx != -1, f"Missing later marker after {earlier}: {later}"
	assert earlier_idx < later_idx, f"Expected {earlier!r} before {later!r}"


def test_clarify_route_emits_stable_gate_telemetry() -> None:
	block = _step_block(CLARIFY_WF, "Decide clarify route")

	assert "AI_PHASE_GATE_V1 phase=clarify gate=route reason=issue_closed outcome=skip issue=${ISSUE_NUMBER}" in block
	assert "AI_PHASE_GATE_V1 phase=clarify gate=route reason=untrusted_issue_author outcome=skip issue=${ISSUE_NUMBER}" in block
	assert "AI_PHASE_GATE_V1 phase=clarify gate=route reason=orchestrator_fast_path outcome=defer issue=${ISSUE_NUMBER}" in block
	assert "ISSUE_AUTHOR_TRUSTED" in block
	assert "ISSUE_META_FILE" in block
	assert "EVENT_ACTION" in block
	label_block = _step_block(CLARIFY_WF, "Set clarification phase label")
	fast_block = _step_block(CLARIFY_WF, "Orchestrator-managed fast path")
	assert "steps.clarify_route.outputs.orchestrator_fast_path == 'true'" in label_block
	assert "steps.clarify_route.outputs.orchestrator_fast_path == 'true'" in fast_block


def test_clarify_opened_route_checks_fetched_provenance() -> None:
	workflow = yaml.safe_load(_read(CLARIFY_WF))
	step = next(step for step in workflow["jobs"]["clarify"]["steps"] if step.get("name") == "Decide clarify route")
	with tempfile.TemporaryDirectory() as workdir:
		root = Path(workdir)
		meta_path = root / "issue.json"
		output_path = root / "output"
		cases = [
			("issues", "opened", "User", "OWNER", "maintainer", False, "open", False, False),
			("issues", "opened", "User", "MEMBER", "maintainer", True, "open", True, True),
			("issues", "opened", "User", "COLLABORATOR", "maintainer", False, "open", False, False),
			("issues", "opened", "Bot", "NONE", "github-actions[bot]", True, "open", True, True),
			("issues", "opened", "User", "NONE", "outsider", False, "open", True, False),
			("issues", "opened", "Bot", "OWNER", "other[bot]", True, "open", True, False),
			("issues", "opened", "User", "OWNER", "github-actions[bot]", True, "open", True, True),
			("issues", "opened", None, None, None, True, "open", True, False),
			("issues", "opened", "User", "OWNER", "maintainer", True, "closed", True, False),
			("issue_comment", "created", "User", "NONE", "outsider", False, "open", False, False),
		]
		for event_name, event_action, user_type, association, login, orchestrator, state, skip, fast_path in cases:
			payload = {"state": state, "user": {"type": user_type, "login": login}, "labels": [], "author_association": association}
			if orchestrator:
				payload["labels"] = [{"name": "ai:orchestrator-managed"}]
			meta_path.write_text(json.dumps(payload), encoding="utf-8")
			output_path.write_text("", encoding="utf-8")
			env = os.environ.copy()
			for name in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
				env.pop(name, None)
			env.update({
				"ISSUE_META_FILE": str(meta_path),
				"GITHUB_OUTPUT": str(output_path),
				"EVENT_NAME": event_name,
				"EVENT_ACTION": event_action,
				"COMMENT_BODY": "/reclarify" if event_name == "issue_comment" else "",
				"RUN_ID": "1",
				"ISSUE_NUMBER": "123",
			})
			result = subprocess.run(["bash", "-c", step["run"]], env=env, text=True, capture_output=True, check=True)
			outputs = output_path.read_text(encoding="utf-8")
			assert f"skip_codex={str(skip).lower()}" in outputs, result.stdout
			assert f"orchestrator_fast_path={str(fast_path).lower()}" in outputs, result.stdout
			if event_name == "issues" and association is None:
				assert "reason=untrusted_issue_author" in result.stdout


def _run_clarify_route(step: dict, root: Path, body: str, labels: list[str], implementer: str = "codex", issue_body: str | None = "") -> tuple[str, str]:
	"""Run the real `Decide clarify route` body for one issue comment; return (stdout, outputs)."""
	meta_path = root / "issue.json"
	output_path = root / "output"
	payload = {
		"number": 123,
		"title": "Fix it",
		"body": issue_body,
		"state": "open",
		"user": {"type": "User", "login": "maintainer"},
		"author_association": "OWNER",
		"labels": [{"name": label} for label in labels],
	}
	meta_path.write_text(json.dumps(payload), encoding="utf-8")
	output_path.write_text("", encoding="utf-8")
	env = os.environ.copy()
	for name in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
		env.pop(name, None)
	env.update({
		"ISSUE_META_FILE": str(meta_path),
		"GITHUB_OUTPUT": str(output_path),
		"EVENT_NAME": "issue_comment",
		"EVENT_ACTION": "created",
		"COMMENT_BODY": body,
		"RUN_ID": "1",
		"ISSUE_NUMBER": "123",
		"AI_ISSUE_IMPLEMENTER": implementer,
	})
	result = subprocess.run(["bash", "-c", step["run"]], env=env, text=True, capture_output=True, check=True, cwd=REPO_ROOT)
	return result.stdout, output_path.read_text(encoding="utf-8")


NOT_RECLARIFY_SKIP_LINE = "AI_PHASE_GATE_V1 phase=clarify gate=route reason=not_reclarify_command outcome=skip issue=123"

# Issue #5309: automation quotes model output and untrusted excerpts in fenced
# code blocks (the permission-prompt "Seen again" example uses a ```` fence).
FENCED_PERMISSION_EXAMPLE = (
	"Seen again.\n\n**Latest example** (untrusted data from the session):\n\n"
	"````text\ngh api repos/o/r/issues/1/comments -f body='done\n```\n/reclarify'\n````\n"
)


def test_clarify_route_gates_reclarify_comment_like_the_job_predicate() -> None:
	"""Issues #5243 / #5309: the route step repeats the job-level /reclarify rule.

	A comment that is not a /reclarify command must do nothing. On an
	orchestrator-managed issue only a first-line /reclarify counts, so model
	text in an escalation can never trigger the fast path's automatic /answer.
	"""
	workflow = yaml.safe_load(_read(CLARIFY_WF))
	step = next(step for step in workflow["jobs"]["clarify"]["steps"] if step.get("name") == "Decide clarify route")
	cases = [
		# (description, body, labels, is_command)
		("first line", "/reclarify", [], True),
		("first line mixed case", "/Reclarify please", [], True),
		("trailing line on a claude-blocked issue", "**Q1: A** done.\n\n/reclarify\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_", ["ai:claude-blocked"], True),
		("trailing line, CRLF", "Answer: A\r\n/reclarify", ["ai:claude-blocked"], True),
		("trailing line on a handoff-failed issue", "Token fixed.\n/reclarify", ["ai:claude-handoff-failed"], True),
		("trailing line on an ai:blocked issue", "Use v2.\n/reclarify", ["ai:blocked"], True),
		("trailing line, issue not waiting on an answer", "Note\n/reclarify", [], False),
		("inline mention", "please /reclarify", ["ai:claude-blocked"], False),
		("backticked mention", "comment `/reclarify` to retry", ["ai:claude-blocked"], False),
		("indented line", "Answer\n  /reclarify", ["ai:claude-blocked"], False),
		("blocked comment", "<!-- ai:claude-blocked:v1 -->\nReply, then comment:\n\n/reclarify", ["ai:claude-blocked"], False),
		("plan comment", "Implementation Plan\n\nTo restart clarification reply:\n\n/reclarify\n\n<!-- ai:implementation-plan:v1 -->", ["ai:blocked"], False),
		# Issue #5309 cases.
		("heal marker without the ai: prefix", "<!-- workflow-failure-heal:occurrence -->\nAnother occurrence\n/reclarify", ["ai:claude-blocked"], False),
		("escalation with its marker", "Autonomous resolution not possible.\n\nESCALATION: pick one\n/reclarify\n\n<!-- ai:clarify-escalation:v1 -->", ["ai:blocked"], False),
		("trailing line on a tracking issue", "Judge: retry.\n/reclarify", ["ai:orchestrator-tracking", "ai:blocked"], False),
		("fenced line, backticks", "Log:\n```\n/reclarify\n```", ["ai:claude-blocked"], False),
		("fenced line, tildes", "Log:\n~~~text\n/reclarify\n~~~", ["ai:claude-blocked"], False),
		("fenced line, shorter inner fence", FENCED_PERMISSION_EXAMPLE, ["ai:claude-blocked"], False),
		("fenced line, unclosed fence", "Log:\n```\nerror\n/reclarify", ["ai:claude-blocked"], False),
		("fenced line, tilde does not close a backtick fence", "Log:\n```\n~~~\n/reclarify\n```", ["ai:claude-blocked"], False),
		("line after a closed fence", "Log:\n```\nerror\n```\nAnswer A.\n/reclarify", ["ai:claude-blocked"], True),
		("line after an indented closing fence", "Log:\n```\nerror\n   ```\n/reclarify", ["ai:claude-blocked"], True),
		("line after a closing fence with trailing blanks", "Log:\n```\nerror\n``` \t\n/reclarify", ["ai:claude-blocked"], True),
		("fenced line, fence line with text does not close", "Log:\n```\nerror\n``` x\n/reclarify", ["ai:claude-blocked"], False),
		("line after inline code, not a fence", "```make test``` first.\n/reclarify", ["ai:claude-blocked"], True),
		("line after a backtick in the info string", "Log:\n```js`x`\n/reclarify", ["ai:claude-blocked"], True),
		("fenced line, tilde info string may hold a backtick", "Log:\n~~~js`x`\n/reclarify\n~~~", ["ai:claude-blocked"], False),
		("fenced line, longer opener needs a longer close", "Log:\n````text\n```\n/reclarify\n````", ["ai:claude-blocked"], False),
	]
	with tempfile.TemporaryDirectory() as workdir:
		root = Path(workdir)
		for description, body, labels, is_command in cases:
			stdout, outputs = _run_clarify_route(step, root, body, labels)
			assert "issue_implementer=codex" in outputs, description
			if is_command:
				assert NOT_RECLARIFY_SKIP_LINE not in stdout, description
				assert "reclarify_command=true" in stdout, description
			else:
				assert NOT_RECLARIFY_SKIP_LINE in stdout, description
				assert "skip_codex=true" in outputs, description
				assert "issue_implementer_reason=not_routed" in outputs, description
				assert "orchestrator_fast_path=false" in outputs, description
				assert "reclarify_command=false" in stdout, description
		# Orchestrator-managed issues: a first-line /reclarify keeps the fast
		# path; a later line never counts, even on ai:blocked (issue #5309).
		for description, body, is_command in (
			("first line", "/reclarify", True),
			("first line, later lines too", "/reclarify\nmore\n/reclarify", True),
			("trailing line", "Use v2.\n/reclarify", False),
			("unmarked escalation model text", "Autonomous resolution not possible.\n\nESCALATION: pick one\n/reclarify\n\n- Cycle: 3/3", False),
		):
			stdout, outputs = _run_clarify_route(step, root, body, ["ai:orchestrator-managed", "ai:blocked"])
			if is_command:
				assert NOT_RECLARIFY_SKIP_LINE not in stdout, description
				assert "orchestrator_fast_path=true" in outputs, description
			else:
				assert NOT_RECLARIFY_SKIP_LINE in stdout, description
				assert "skip_codex=true" in outputs, description
				assert "orchestrator_fast_path=false" in outputs, description
				assert "reclarify_command=false" in stdout, description
		# Review round 3: an orchestrator child issue that lost its label is
		# still recognised by its `Managed by: AI Orchestrator` body line, as
		# scripts/claude_issue_route.py does; only a first-line /reclarify
		# counts there. Case-insensitive, like the job predicate's contains.
		child_body = "## Task\n\nFix it.\n\n## Metadata\n- Managed by: AI Orchestrator\n- Parent: #41"
		for description, body, issue_body, is_command in (
			("trailing line, body-marked issue", "Use v2.\n/reclarify", child_body, False),
			("unmarked escalation model text, body-marked issue", "Autonomous resolution not possible.\n\nESCALATION: pick one\n/reclarify\n\n- Cycle: 3/3", child_body, False),
			("trailing line, body marker in another case", "Use v2.\n/reclarify", "managed BY: ai orchestrator", False),
			("first line, body-marked issue", "/reclarify", child_body, True),
			("trailing line, null issue body", "Use v2.\n/reclarify", None, True),
			("trailing line, unrelated issue body", "Use v2.\n/reclarify", "Managed by: the platform team", True),
		):
			stdout, outputs = _run_clarify_route(step, root, body, ["ai:blocked"], issue_body=issue_body)
			if is_command:
				assert NOT_RECLARIFY_SKIP_LINE not in stdout, description
				assert "reclarify_command=true" in stdout, description
			else:
				assert NOT_RECLARIFY_SKIP_LINE in stdout, description
				assert "skip_codex=true" in outputs, description
				assert "orchestrator_fast_path=false" in outputs, description
				assert "reclarify_command=false" in stdout, description


def test_clarify_route_releases_stale_claude_labels_on_switch_to_codex() -> None:
	"""Issue #5309: a Codex route clears the Claude "waiting on an answer" labels too."""
	workflow = yaml.safe_load(_read(CLARIFY_WF))
	step = next(step for step in workflow["jobs"]["clarify"]["steps"] if step.get("name") == "Decide clarify route")
	with tempfile.TemporaryDirectory() as workdir:
		root = Path(workdir)
		for labels, release in (
			(["ai:codex", "ai:claude-blocked"], True),
			(["ai:codex", "ai:claude-handoff-failed"], True),
			(["ai:codex", "ai:claude"], True),
			(["ai:codex"], False),
		):
			_, outputs = _run_clarify_route(step, root, "/reclarify", labels, implementer="")
			assert "issue_implementer=codex" in outputs, labels
			assert f"release_claude_claim={str(release).lower()}" in outputs, labels
	release_block = _step_block(CLARIFY_WF, "Release Claude claim on switch to Codex")
	for name in ("ai:claude", "ai:claude-blocked", "ai:claude-handoff-failed"):
		assert f'.name == "{name}"' in release_block, name


def _run_clarify_release_step(
	root: Path, issue: dict, graphql_ok: bool, delete_status: dict[str, int] | None = None, expect_released: bool = True
) -> tuple[list[str], list[dict], str]:
	"""Run the real release step against a stub `gh`; return (calls, graphql bodies, stdout).

	`delete_status` maps a URL-encoded label to the HTTP status its REST DELETE
	fails with (anything not listed succeeds). With `expect_released` False the
	step must exit non-zero and must not log `released`.
	"""
	workflow = yaml.safe_load(_read(CLARIFY_WF))
	step = next(step for step in workflow["jobs"]["clarify"]["steps"] if step.get("name") == "Release Claude claim on switch to Codex")
	script = step["run"].replace("${{ github.repository }}", "o/r").replace(
		"${{ steps.clarify_route.outputs.issue_implementer_reason }}", "label_override"
	)
	assert "${{" not in script
	bin_dir = root / "bin"
	bin_dir.mkdir(exist_ok=True)
	calls_path = root / "calls"
	bodies_path = root / "graphql_bodies"
	gh_stub = bin_dir / "gh"
	gh_stub.write_text(
		"#!/usr/bin/env bash\n"
		"printf '%s\\n' \"$*\" >> \"${GH_CALLS_FILE}\"\n"
		"if [ \"$1 $2\" = \"api graphql\" ]; then\n"
		"  jq -c . >> \"${GH_GRAPHQL_BODIES_FILE}\"\n"
		"  if [ \"${GH_GRAPHQL_OK}\" = \"true\" ]; then echo true; else echo 'gh: Could not resolve to a node' >&2; exit 1; fi\n"
		"fi\n"
		"if [ \"$1 $2 $3\" = \"api -X DELETE\" ]; then\n"
		"  status=\"$(jq -r --arg label \"${4##*/}\" '.[$label] // empty' <<<\"${GH_DELETE_STATUS}\")\"\n"
		"  if [ -n \"${status}\" ]; then echo '{\"message\":\"stub\"}'; echo \"gh: stub failure (HTTP ${status})\" >&2; exit 1; fi\n"
		"  echo '[]'\n"
		"fi\n",
		encoding="utf-8",
	)
	gh_stub.chmod(0o755)
	meta_path = root / "issue.json"
	meta_path.write_text(json.dumps(issue), encoding="utf-8")
	calls_path.write_text("", encoding="utf-8")
	bodies_path.write_text("", encoding="utf-8")
	env = os.environ.copy()
	for name in ("BASH_ENV", "ENV"):
		env.pop(name, None)
	env.update({
		"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
		"GH_CALLS_FILE": str(calls_path),
		"GH_GRAPHQL_BODIES_FILE": str(bodies_path),
		"GH_GRAPHQL_OK": "true" if graphql_ok else "false",
		"GH_DELETE_STATUS": json.dumps(delete_status or {}),
		"ISSUE_META_FILE": str(meta_path),
		"ISSUE_NUMBER": "123",
	})
	result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, check=False)
	if expect_released:
		assert result.returncode == 0, (issue, result.stdout, result.stderr)
		assert "CLAUDE_ISSUE_HANDOFF released issue=123" in result.stdout, issue
	else:
		assert result.returncode != 0, (issue, result.stdout)
		assert "CLAUDE_ISSUE_HANDOFF released issue=123" not in result.stdout, issue
	calls = calls_path.read_text(encoding="utf-8").splitlines()
	bodies = [json.loads(line) for line in bodies_path.read_text(encoding="utf-8").splitlines() if line.strip()]
	return calls, bodies, result.stdout


_CLARIFY_RELEASE_CASES = (
	(["ai:codex", "ai:claude-blocked"], ["ai:claude-blocked"]),
	(["ai:codex", "ai:claude-handoff-failed"], ["ai:claude-handoff-failed"]),
	(["ai:codex", "ai:claude"], ["ai:claude"]),
	(
		["ai:claude", "ai:claude-blocked", "ai:claude-handoff-failed", "ai:codex"],
		["ai:claude", "ai:claude-blocked", "ai:claude-handoff-failed"],
	),
	(["ai:codex", "ai:claude-blocked-extra"], []),
)


def _clarify_release_issue(labels: list[str], with_node_ids: bool) -> dict:
	if not with_node_ids:
		return {"number": 123, "labels": [{"name": label} for label in labels]}
	return {
		"number": 123,
		"node_id": "I_issue123",
		"labels": [{"name": label, "node_id": f"LA_{label}"} for label in labels],
	}


def test_clarify_release_step_removes_present_labels_in_one_graphql_call() -> None:
	"""Review round 4 (§15): every Claude label on the issue goes in ONE call.

	The step reads the issue metadata the route step already fetched, so it
	spends nothing on absent labels, and batches the present ones into one
	`removeLabelsFromLabelable` mutation instead of one REST DELETE each.
	"""
	for labels, expected in _CLARIFY_RELEASE_CASES:
		with tempfile.TemporaryDirectory() as workdir:
			calls, bodies, stdout = _run_clarify_release_step(Path(workdir), _clarify_release_issue(labels, True), graphql_ok=True)
		if not expected:
			assert calls == [], labels
			continue
		assert len(calls) == 1 and calls[0].startswith("api graphql --input - "), (labels, calls)
		assert len(bodies) == 1, labels
		assert "removeLabelsFromLabelable" in bodies[0]["query"], labels
		assert bodies[0]["variables"] == {"id": "I_issue123", "labelIds": [f"LA_{label}" for label in expected]}, labels
		assert "release_batch_fallback" not in stdout, labels


def test_clarify_release_step_falls_back_to_one_delete_per_present_label() -> None:
	"""A failed batch call, or a snapshot without node ids, falls back to REST.

	Fallback deletes only the Claude labels the issue carries (never a
	look-alike), one DELETE each, so a failed mutation never leaves the
	issue holding a stale Claude label.
	"""
	for graphql_ok, with_node_ids in ((False, True), (True, False)):
		for labels, expected in _CLARIFY_RELEASE_CASES:
			with tempfile.TemporaryDirectory() as workdir:
				calls, _, stdout = _run_clarify_release_step(
					Path(workdir), _clarify_release_issue(labels, with_node_ids), graphql_ok=graphql_ok
				)
			deletes = [call for call in calls if call.startswith("api -X DELETE")]
			encoded = [label.replace(":", "%3A") for label in expected]
			assert deletes == [f"api -X DELETE repos/o/r/issues/123/labels/{name}" for name in encoded], (labels, calls)
			graphql_calls = [call for call in calls if call.startswith("api graphql")]
			assert len(graphql_calls) == (1 if expected and with_node_ids else 0), (labels, calls)
			assert ("release_batch_fallback" in stdout) == bool(expected), (labels, stdout)



def test_clarify_release_step_fallback_fails_closed_when_a_delete_fails() -> None:
	"""PR #5324 Copilot finding: a failed fallback DELETE must not log `released`.

	A stale `ai:claude-blocked` / `ai:claude-handoff-failed` label keeps the
	later-line /reclarify form open while Codex posts unmarked text, so a
	DELETE that fails for any reason but 404 stops the Codex route. The other
	present labels are still attempted, and the log names what is left.
	"""
	labels = ["ai:claude", "ai:claude-blocked", "ai:claude-handoff-failed", "ai:codex"]
	for graphql_ok, with_node_ids in ((False, True), (True, False)):
		for status in (403, 502):
			with tempfile.TemporaryDirectory() as workdir:
				calls, _, stdout = _run_clarify_release_step(
					Path(workdir),
					_clarify_release_issue(labels, with_node_ids),
					graphql_ok=graphql_ok,
					delete_status={"ai%3Aclaude-blocked": status},
					expect_released=False,
				)
			deletes = [call for call in calls if call.startswith("api -X DELETE")]
			assert deletes == [
				f"api -X DELETE repos/o/r/issues/123/labels/{name}"
				for name in ("ai%3Aclaude", "ai%3Aclaude-blocked", "ai%3Aclaude-handoff-failed")
			], (status, calls)
			assert "CLAUDE_ISSUE_HANDOFF release_incomplete issue=123 labels=ai:claude-blocked" in stdout, (status, stdout)


def test_clarify_release_step_fallback_treats_404_as_already_removed() -> None:
	"""A label removed since the route step's snapshot answers 404: that is the goal, not a failure."""
	labels = ["ai:claude", "ai:claude-blocked", "ai:codex"]
	with tempfile.TemporaryDirectory() as workdir:
		_, _, stdout = _run_clarify_release_step(
			Path(workdir),
			_clarify_release_issue(labels, False),
			graphql_ok=True,
			delete_status={"ai%3Aclaude": 404, "ai%3Aclaude-blocked": 404},
		)
	assert "release_incomplete" not in stdout, stdout


def test_clarify_route_hands_trailing_reclarify_answer_to_claude() -> None:
	"""Issue #5243 incident: an answer ending in /reclarify on a blocked Claude issue resumes it."""
	workflow = yaml.safe_load(_read(CLARIFY_WF))
	step = next(step for step in workflow["jobs"]["clarify"]["steps"] if step.get("name") == "Decide clarify route")
	with tempfile.TemporaryDirectory() as workdir:
		root = Path(workdir)
		meta_path = root / "issue.json"
		output_path = root / "output"
		for labels, body, routed in (
			(["ai:claude", "ai:claude-blocked"], "**Q1: A** — done.\n\n/reclarify\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_", True),
			(["ai:claude"], "**Q1: A** — done.\n\n/reclarify", False),
		):
			payload = {
				"number": 123,
				"title": "Fix it",
				"body": "",
				"state": "open",
				"user": {"type": "User", "login": "maintainer"},
				"author_association": "OWNER",
				"labels": [{"name": label} for label in labels],
			}
			meta_path.write_text(json.dumps(payload), encoding="utf-8")
			output_path.write_text("", encoding="utf-8")
			env = os.environ.copy()
			for name in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
				env.pop(name, None)
			env.update({
				"ISSUE_META_FILE": str(meta_path),
				"GITHUB_OUTPUT": str(output_path),
				"EVENT_NAME": "issue_comment",
				"EVENT_ACTION": "created",
				"COMMENT_BODY": body,
				"RUN_ID": "1",
				"ISSUE_NUMBER": "123",
				"AI_ISSUE_IMPLEMENTER": "",
			})
			result = subprocess.run(["bash", "-c", step["run"]], env=env, text=True, capture_output=True, check=True, cwd=REPO_ROOT)
			outputs = output_path.read_text(encoding="utf-8")
			if routed:
				assert "issue_implementer=claude" in outputs, result.stdout + result.stderr
				assert "is_closed=false" in outputs
			else:
				assert "issue_implementer=codex" in outputs
				assert "reason=not_reclarify_command" in result.stdout


def test_plan_comment_marks_its_reclarify_line_as_automation() -> None:
	"""Issue #5243: the plan comment's bare /reclarify line must not start clarify."""
	block = _step_block(PLAN_WF, "Post implementation plan")
	_assert_before(block, 'echo "/reclarify"', 'echo "<!-- ai:implementation-plan:v1 -->"')
	_assert_before(block, 'echo "<!-- ai:implementation-plan:v1 -->"', '} > "${PLAN_COMMENT_FILE}"')


def test_escalation_comments_end_with_automation_markers() -> None:
	"""Issue #5309: comments that embed model text close with an `<!-- ai:` marker.

	The marker sits after the model text, so the later-line /reclarify form
	never treats a /reclarify line inside that text as a command.
	"""
	clarify_blocked_block = _step_block(CLARIFY_WF, "Handle blocked clarification output")
	_assert_before(clarify_blocked_block, "--add-label 'ai:blocked'", '"<!-- ai:clarify-blocked:v1 -->"')
	_assert_before(clarify_blocked_block, '"Reason: ${BLOCKED_REASON}"', '"<!-- ai:clarify-blocked:v1 -->"')
	_assert_before(clarify_blocked_block, '"<!-- ai:clarify-blocked:v1 -->"', '} > "${BLOCKED_COMMENT_FILE}"')

	blocked_block = _step_block(PLAN_WF, "Handle blocked planning output")
	_assert_before(blocked_block, '"Reason: ${BLOCKED_REASON}"', '"<!-- ai:plan-blocked:v1 -->"')
	_assert_before(blocked_block, '"<!-- ai:plan-blocked:v1 -->"', '} > "${PLAN_COMMENT_FILE}"')

	clarification_block = _step_block(PLAN_WF, "Post clarification questions")
	_assert_before(clarification_block, 'cat "${CODEX_OUTPUT_FILE}"', 'echo "<!-- ai:clarification-required:v1 -->"')
	_assert_before(clarification_block, 'echo "<!-- ai:clarification-required:v1 -->"', '} > "${PLAN_COMMENT_FILE}"')

	script = _read(ORCH_PARSE_ANSWER_SCRIPT)
	variants = script.split('} > "${RUNTIME_DIR}/loop_break_comment.md"')
	assert len(variants) == 3, "expected the ESCALATE and loop-guard comment variants"
	assert "${ESCALATION_SECTION}" in variants[0]
	for variant in variants[:2]:
		assert variant.rstrip().endswith('echo "<!-- ai:clarify-escalation:v1 -->"'), variant[-400:]


def test_plan_gate_steps_emit_stable_skip_and_defer_telemetry() -> None:
	validate_block = _step_block(PLAN_WF, "Validate planning phase label")
	assert "AI_PHASE_GATE_V1 phase=plan gate=trigger_validation reason=invalid_trigger_body outcome=skip issue=${ISSUE_NUMBER}" in validate_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=trigger_validation reason=issue_closed outcome=skip issue=${ISSUE_NUMBER}" in validate_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=trigger_validation reason=missing_expected_phase_label outcome=skip issue=${ISSUE_NUMBER}" in validate_block
	_assert_before(
		validate_block,
		"AI_PHASE_GATE_V1 phase=plan gate=trigger_validation reason=invalid_trigger_body outcome=skip issue=${ISSUE_NUMBER}",
		"exit 0",
	)

	existing_pr_block = _step_block(PLAN_WF, "Skip when issue already has a PR")
	assert "AI_PHASE_GATE_V1 phase=plan gate=existing_pr_check reason=existing_pr outcome=skip issue=${ISSUE_NUMBER}" in existing_pr_block

	stale_block = _step_block(PLAN_WF, "Skip stale /answer comments")
	assert "AI_PHASE_GATE_V1 phase=plan gate=comment_freshness reason=no_answer_comment outcome=skip issue=${ISSUE_NUMBER}" in stale_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=comment_freshness reason=stale_answer_comment outcome=skip issue=${ISSUE_NUMBER} trigger_comment_id=${TRIGGER_COMMENT_ID} latest_comment_id=${LATEST_ANSWER_COMMENT_ID}" in stale_block
	_assert_before(
		stale_block,
		"AI_PHASE_GATE_V1 phase=plan gate=comment_freshness reason=no_answer_comment outcome=skip issue=${ISSUE_NUMBER}",
		"exit 0",
	)

	claim_block = _step_block(PLAN_WF, "Check and claim /answer command")
	assert "AI_PHASE_GATE_V1 phase=plan gate=command_claim reason=already_processed outcome=skip issue=${ISSUE_NUMBER} comment_id=${TRIGGER_COMMENT_ID}" in claim_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=command_claim reason=claimed_elsewhere outcome=skip issue=${ISSUE_NUMBER} comment_id=${TRIGGER_COMMENT_ID}" in claim_block

	auto_answer_block = _step_block(PLAN_WF, "Evaluate orchestrator auto-answer eligibility")
	assert "AI_PHASE_GATE_V1 phase=plan gate=orchestrator_auto_answer reason=not_orchestrator_managed outcome=defer issue=${ISSUE_NUMBER}" in auto_answer_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=orchestrator_auto_answer reason=parse_failed outcome=defer issue=${ISSUE_NUMBER}" in auto_answer_block

	auto_approve_block = _step_block(PLAN_WF, "Auto-approve clear plan")
	assert "AI_PHASE_GATE_V1 phase=plan gate=auto_approve reason=issue_closed outcome=defer issue=${ISSUE_NUMBER}" in auto_approve_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=auto_approve reason=auto_implement_disabled outcome=defer issue=${ISSUE_NUMBER}" in auto_approve_block


def test_implement_gate_steps_emit_stable_skip_telemetry() -> None:
	precheck_block = _step_block(IMPLEMENT_WF, "Precheck approval phase label")
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_precheck reason=issue_closed outcome=skip issue=${ISSUE_NUMBER}" in precheck_block
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_precheck reason=already_implementing outcome=skip issue=${ISSUE_NUMBER}" in precheck_block
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_precheck reason=wrong_phase outcome=skip issue=${ISSUE_NUMBER}" in precheck_block

	existing_pr_block = _step_block(IMPLEMENT_WF, "Exit when existing PR is found")
	assert "AI_PHASE_GATE_V1 phase=implement gate=existing_pr_check reason=existing_pr outcome=skip issue=${ISSUE_NUMBER}" in existing_pr_block
	_assert_before(
		existing_pr_block,
		"AI_PHASE_GATE_V1 phase=implement gate=existing_pr_check reason=existing_pr outcome=skip issue=${ISSUE_NUMBER}",
		"exit 0",
	)

	validate_block = _step_block(IMPLEMENT_WF, "Validate approval phase label")
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_validation reason=destructive_blocked outcome=skip issue=${ISSUE_NUMBER}" in validate_block
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_validation reason=scope_blocked outcome=skip issue=${ISSUE_NUMBER}" in validate_block
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_validation reason=wrong_phase outcome=skip issue=${ISSUE_NUMBER}" in validate_block

	claim_block = _step_block(IMPLEMENT_WF, "Claim /approved command")
	assert "AI_PHASE_GATE_V1 phase=implement gate=command_claim reason=already_processed outcome=skip issue=${ISSUE_NUMBER} comment_id=${APPROVAL_COMMENT_ID}" in claim_block
	assert "AI_PHASE_GATE_V1 phase=implement gate=command_claim reason=claimed_elsewhere outcome=skip issue=${ISSUE_NUMBER} comment_id=${APPROVAL_COMMENT_ID}" in claim_block


def test_orchestrate_clarify_respond_gate_steps_emit_stable_telemetry() -> None:
	stage_block = _step_block(ORCH_CLARIFY_RESPOND_WF, "Stage workflow support files")
	assert "orchestrate_parse_and_post_answer.sh" in stage_block

	metadata_block = _step_block(ORCH_CLARIFY_RESPOND_WF, "Check orchestrator metadata")
	assert "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=orchestrator_metadata reason=not_orchestrator_managed outcome=skip issue=${ISSUE_NUMBER}" in metadata_block

	parse_block = _step_block(ORCH_CLARIFY_RESPOND_WF, "Parse and post answer")
	assert "bash scripts/orchestrate_parse_and_post_answer.sh" in parse_block

	helper_text = _read(ORCH_PARSE_ANSWER_SCRIPT)
	assert "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=command_claim reason=already_processed outcome=skip issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID}" in helper_text
	assert "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=command_claim reason=claimed_elsewhere outcome=skip issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID}" in helper_text
	assert "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=auto_answer reason=escalate_requested outcome=defer issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID} cycle=${CYCLE} max_cycles=${MAX_CYCLES}" in helper_text
	assert "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=auto_answer reason=loop_guard_blocked outcome=defer issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID} loop_reason=${LOOP_REASON} cycle=${CYCLE} max_cycles=${MAX_CYCLES}" in helper_text
	assert 'if ! [[ "${COMMENT_COUNT_GUARD}" =~ ^[0-9]+$ ]]; then' in helper_text
	_assert_before(
		helper_text,
		"AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=auto_answer reason=loop_guard_blocked outcome=defer issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID} loop_reason=${LOOP_REASON} cycle=${CYCLE} max_cycles=${MAX_CYCLES}",
		"exit 0",
	)


def test_orchestrate_clarify_respond_reuses_cached_issue_payloads_before_live_fallback() -> None:
	metadata_block = _step_block(ORCH_CLARIFY_RESPOND_WF, "Check orchestrator metadata")
	assert 'ISSUE_PAYLOAD_FILE="${EARLY_CACHE_DIR}/issue_payload.json"' in metadata_block
	assert 'TRACKING_PAYLOAD_FILE="${EARLY_CACHE_DIR}/tracking_issue_payload.json"' in metadata_block
	assert 'printf \'%s\' "${ISSUE_PAYLOAD}" > "${ISSUE_PAYLOAD_FILE}"' in metadata_block
	assert 'if TRACKING_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" 2>/dev/null)"; then' in metadata_block
	assert 'printf \'%s\' "${TRACKING_PAYLOAD}" > "${TRACKING_PAYLOAD_FILE}"' in metadata_block
	assert 'TRACKING_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${TRACKING_NUM}" 2>/dev/null || echo "")"' not in metadata_block

	fetch_block = _step_block(ORCH_CLARIFY_RESPOND_WF, "Fetch issue and tracking context")
	assert 'if [ -n "${ISSUE_PAYLOAD_FILE:-}" ] && [ -s "${ISSUE_PAYLOAD_FILE}" ]; then' in fetch_block
	assert "ISSUE_META=\"$(gh_retry gh api \"repos/${{ github.repository }}/issues/${ISSUE_NUMBER}\")\"" in fetch_block
	assert 'if [ -n "${TRACKING_PAYLOAD_FILE:-}" ] && [ -s "${TRACKING_PAYLOAD_FILE}" ] && jq -e --arg tracking_num "${TRACKING_NUM}"' in fetch_block
	assert "TRACKING_BODY=\"$(gh_retry gh api \"repos/${{ github.repository }}/issues/${TRACKING_NUM}\" --jq '.body // \"\"')\"" in fetch_block
	assert "scripts/orchestrate_parse_and_post_answer.sh" in _read(WORKSPACE_SOURCE_MANIFEST)


def test_agents_and_ci_register_phase_gate_contract() -> None:
	agents_text = _read(AGENTS_MD)
	assert "- `AI_PHASE_GATE_V1`" in agents_text

	ci_text = _read(CI_WF)
	assert "PYTHONDONTWRITEBYTECODE=1 python3 tests/test_phase_skip_gate_telemetry_contract.py" in ci_text


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
