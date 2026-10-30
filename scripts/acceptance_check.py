#!/usr/bin/env python3
"""Done-check (acceptance) for approved plans (issue #8427, phase P2 of
docs/plans/persistent-unattended-workflows-plan.md).

A plan names its done-check with unfenced lines after its
`## Files likely to change` heading:

	ACCEPTANCE: test <repo-path>[::<name>] [red-on-base]     (at most 10)
	ACCEPTANCE: check <check-run name>                        (at most 5)
	ACCEPTANCE: post-merge workflow <.github/workflows/f.yml> [job <job name>]
	ACCEPTANCE: post-merge none: <reason>

At least one `test` or `check` line and exactly one `post-merge` line are
required. A heal plan without ACCEPTANCE lines whose single
`HEAL_COUNTERFACTUAL: REPRODUCIBLE: <ids>` line names test ids gets
`test <id> red-on-base` items and no post-merge line (#8409 P7 owns the check
after merge).

`validate` checks the lines against the planning ref (no network) and writes
`acceptance.v1` JSON. plan.yml stores a valid result as one
`<!-- AI_ACCEPTANCE_V1 {json} -->` line directly above the host trailer
`<!-- ai:plan-proposal:v1 -->`; `parse` reads it back from that trailer
position only, so a marker the model writes into its plan text is ignored.
`render` prints the PR body's `## Acceptance` section.

Switch: PERSIST_ACCEPTANCE_ENABLED (default ``false``) is read by the calling
workflow steps and scripts/run_plan_codex.sh; this helper is a pure library
and has no API calls. An invalid done-check never holds or approves a plan:
it is logged and recorded, and done falls back to "CI green" (spec D4).

CLI (exit 0 on handled outcomes; 2 on usage or IO errors):
	validate --plan-file F --repo-root D --out F
	marker --in F
	parse --plan-comment-file F [--out F]
	render --in F [--gate-result F]
Log lines (stderr, tokens only):
	PERSIST acceptance outcome=<valid|invalid|none> errors=<n>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import partial_delivery  # noqa: E402
import targeted_file_context  # noqa: E402

try:  # PyYAML is optional: a line scanner covers 2-space workflow YAML.
	import yaml  # type: ignore
except Exception:  # noqa: BLE001
	yaml = None

SCHEMA = "acceptance.v1"
MARKER = "AI_ACCEPTANCE_V1"
PROPOSAL_TRAILER = "<!-- ai:plan-proposal:v1 -->"
MAX_TESTS = 10
MAX_CHECKS = 5
MAX_ERRORS = 20
MAX_ERROR_CHARS = 200
MAX_NAME_CHARS = 200
MAX_REASON_CHARS = 300
MAX_MARKER_BYTES = 6000
MAX_WORKFLOW_BYTES = 1024 * 1024
STATES = ("valid", "invalid", "none")
GATE_STATUSES = ("met", "unmet", "unmet (passes on base)", "not run")

ACCEPTANCE_LINE_RE = re.compile(r"^ {0,3}ACCEPTANCE:(.*)$")
TEST_RE = re.compile(r"^test[ \t]+(\S+?)(?:[ \t]+(red-on-base))?$")
TEST_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,300}$")
TEST_NAME_RE = re.compile(r"^[A-Za-z0-9_.\[\]:/-]{1,200}$")
CHECK_RE = re.compile(r"^check[ \t]+(.+)$")
WORKFLOW_PATH_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$")
POST_WORKFLOW_RE = re.compile(
	r"^post-merge[ \t]+workflow[ \t]+(\.github/workflows/[A-Za-z0-9._-]+\.ya?ml)(?:[ \t]+job[ \t]+(.+))?$"
)
POST_NONE_RE = re.compile(r"^post-merge[ \t]+none:[ \t]*(.*)$")
REPRODUCIBLE_RE = re.compile(r"^REPRODUCIBLE[ \t]*:[ \t]*(.+)$")
MARKER_LINE_RE = re.compile(r"^<!-- " + MARKER + r" (\{.*\}) -->$")
JOBS_KEY_RE = re.compile(r"^jobs:[ \t]*(#.*)?$")
JOB_ID_RE = re.compile(r"^  ([A-Za-z0-9_-]+):[ \t]*(#.*)?$")
JOB_NAME_RE = re.compile(r"^    name:[ \t]*(.+?)[ \t]*$")


def log(outcome: str, detail: str) -> None:
	print(f"PERSIST acceptance outcome={outcome} {detail}", file=sys.stderr)


def _clean_name(value: Any) -> str | None:
	"""Check, job and reason text: control characters stripped, 1-200 chars,
	no backtick or pipe. None when unusable."""
	if not isinstance(value, str):
		return None
	text = " ".join(partial_delivery.CONTROL_RE.sub(" ", value).split())
	if not text or len(text) > MAX_NAME_CHARS or "`" in text or "|" in text:
		return None
	return text


def _clean_reason(value: Any) -> str | None:
	if not isinstance(value, str):
		return None
	text = " ".join(partial_delivery.CONTROL_RE.sub(" ", value).split())
	if not text or len(text) > MAX_REASON_CHARS:
		return None
	return text


def _safe_test_path(path: Any) -> bool:
	if not isinstance(path, str) or not TEST_PATH_RE.match(path) or path.startswith("/"):
		return False
	return all(part not in ("", ".", "..") for part in path.split("/"))


def _test_item(target: str, red_on_base: bool) -> dict | None:
	path, sep, name = target.partition("::")
	if not _safe_test_path(path):
		return None
	if sep and not TEST_NAME_RE.match(name):
		return None
	return {"path": path, "name": name, "red_on_base": bool(red_on_base)}


def _section_lines(plan_text: str) -> tuple[list[str], list[str]]:
	"""(unfenced lines after the first Files-likely-to-change heading, all unfenced lines)."""
	unfenced = partial_delivery._unfenced_lines(plan_text.splitlines())
	for index, line in enumerate(unfenced):
		if targeted_file_context.MARKDOWN_HEADING_RE.match(line) and targeted_file_context.LIKELY_HEADER_RE.search(line):
			return unfenced[index + 1:], unfenced
	return [], unfenced


def parse_acceptance_lines(plan_text: str) -> tuple[list[dict], list[str]]:
	"""Items ({kind, ...}) and grammar errors for the plan's ACCEPTANCE lines."""
	lines, _ = _section_lines(plan_text)
	items: list[dict] = []
	errors: list[str] = []
	number = 0
	for line in lines:
		match = ACCEPTANCE_LINE_RE.match(line)
		if not match:
			continue
		number += 1
		body = match.group(1).strip()
		test = TEST_RE.match(body)
		if test:
			item = _test_item(test.group(1), bool(test.group(2)))
			if item is None:
				errors.append(f"unknown_form:{number}")
			else:
				items.append({"kind": "test", **item})
			continue
		check = CHECK_RE.match(body)
		if check:
			name = _clean_name(check.group(1))
			if name is None:
				errors.append(f"unknown_form:{number}")
			else:
				items.append({"kind": "check", "name": name})
			continue
		post = POST_WORKFLOW_RE.match(body)
		if post:
			job = ""
			if post.group(2) is not None:
				job = _clean_name(post.group(2)) or ""
				if not job:
					errors.append(f"unknown_form:{number}")
					continue
			items.append({"kind": "post-merge", "workflow": post.group(1), "job": job})
			continue
		none = POST_NONE_RE.match(body)
		if none:
			reason = _clean_reason(none.group(1))
			if reason is None:
				errors.append(f"unknown_form:{number}")
			else:
				items.append({"kind": "post-merge-none", "reason": reason})
			continue
		errors.append(f"unknown_form:{number}")
	if number == 0:
		return [], []
	return items, errors


def _heal_reproducible_ids(plan_text: str) -> list[str] | None:
	"""Test ids from the single `HEAL_COUNTERFACTUAL: REPRODUCIBLE: <ids>` line
	(#8409 P2 grammar), or None when the plan has no such single line."""
	_, unfenced = _section_lines(plan_text)
	found = []
	for line in unfenced:
		match = partial_delivery.HEAL_CF_LINE_RE.match(line)
		if match:
			found.append(match.group(1))
	if len(found) != 1:
		return None
	value = REPRODUCIBLE_RE.match(found[0])
	if not value:
		return None
	ids = [token for token in re.split(r"[,\s]+", value.group(1)) if token]
	return ids or None


def _unquote(value: str) -> str:
	value = value.strip()
	if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
		return value[1:-1]
	return re.sub(r"[ \t]+#.*$", "", value).strip()


def _scan_jobs(text: str) -> dict[str, str]:
	"""Job id -> job name ('' when unset), from 2-space workflow YAML."""
	jobs: dict[str, str] = {}
	in_jobs = False
	current = None
	for line in text.splitlines():
		if not line.strip() or line.lstrip().startswith("#"):
			continue
		if not line.startswith((" ", "\t")):
			in_jobs = bool(JOBS_KEY_RE.match(line))
			current = None
			continue
		if not in_jobs:
			continue
		job = JOB_ID_RE.match(line)
		if job:
			current = job.group(1)
			jobs.setdefault(current, "")
			continue
		if line.startswith("  ") and not line.startswith("   "):
			current = None
			continue
		name = JOB_NAME_RE.match(line)
		if current and name:
			jobs[current] = _unquote(name.group(1))
	return jobs


def _yaml_jobs(text: str) -> dict[str, str] | None:
	if yaml is None:
		return None
	try:
		data = yaml.safe_load(text)
	except Exception:  # noqa: BLE001 - fall back to the line scanner
		return None
	if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
		return None
	jobs: dict[str, str] = {}
	for job_id, body in data["jobs"].items():
		name = body.get("name") if isinstance(body, dict) else None
		jobs[str(job_id)] = name if isinstance(name, str) else ""
	return jobs


def _workflow_jobs(repo_root: str | Path) -> dict[str, dict]:
	"""`.github/workflows/<file>` -> {"ids": set, "names": set} for regular
	workflow files of at most 1 MB. `names` holds each job's check-run name
	(its `name:`, else its id); names with `${{` are left out."""
	root = Path(repo_root)
	directory = root / ".github" / "workflows"
	out: dict[str, dict] = {}
	if directory.is_symlink() or not directory.is_dir():
		return out
	for path in sorted(directory.iterdir()):
		if path.suffix not in (".yml", ".yaml") or path.is_symlink() or not path.is_file():
			continue
		try:
			if path.stat().st_size > MAX_WORKFLOW_BYTES:
				continue
			text = path.read_text(encoding="utf-8", errors="replace")
		except OSError:
			continue
		jobs = _yaml_jobs(text)
		if jobs is None:
			jobs = _scan_jobs(text)
		names = set()
		for job_id, name in jobs.items():
			display = name or job_id
			if "${{" not in display:
				names.add(display)
		out[f".github/workflows/{path.name}"] = {"ids": set(jobs), "names": names}
	return out


def _check_matches(name: str, workflows: dict[str, dict]) -> bool:
	if "${{" in name:
		return False
	candidates = {name}
	if " / " in name:
		candidates.add(name.split(" / ", 1)[0])
	return any(candidates & info["names"] for info in workflows.values())


def _test_exists(repo_root: str | Path, path: str) -> bool:
	root = Path(repo_root).resolve()
	target = Path(repo_root) / path
	try:
		if target.is_symlink() or not target.is_file():
			return False
		resolved = target.resolve()
	except OSError:
		return False
	return resolved == root / path or str(resolved).startswith(str(root) + os.sep)


def _empty(state: str = "none", source: str = "plan") -> dict:
	return {
		"schema": SCHEMA, "state": state, "source": source, "tests": [], "checks": [],
		"post_merge": None, "post_merge_none": "", "errors": [],
	}


def _cap_errors(errors: list[str]) -> list[str]:
	return [str(e)[:MAX_ERROR_CHARS] for e in errors][:MAX_ERRORS]


def validate(plan_text: str, repo_root: str | Path) -> dict:
	items, errors = parse_acceptance_lines(plan_text)
	result = _empty()
	if not items and not errors:
		ids = _heal_reproducible_ids(plan_text)
		if not ids:
			return result
		result["source"] = "heal"
		for token in ids:
			item = _test_item(token, True)
			if item is None:
				errors.append("unknown_form:heal")
			else:
				items.append({"kind": "test", **item})
	result["tests"] = [{k: v for k, v in i.items() if k != "kind"} for i in items if i["kind"] == "test"]
	result["checks"] = [{"name": i["name"]} for i in items if i["kind"] == "check"]
	posts = [i for i in items if i["kind"] in ("post-merge", "post-merge-none")]
	if len(posts) == 1:
		if posts[0]["kind"] == "post-merge":
			result["post_merge"] = {"workflow": posts[0]["workflow"], "job": posts[0]["job"]}
		else:
			result["post_merge_none"] = posts[0]["reason"]
	if len(result["tests"]) > MAX_TESTS:
		errors.append("too_many_tests")
	if len(result["checks"]) > MAX_CHECKS:
		errors.append("too_many_checks")
	if not result["tests"] and not result["checks"]:
		errors.append("no_test_or_check")
	if result["source"] != "heal" and len(posts) != 1:
		errors.append(f"post_merge_count:{len(posts)}")
	plan_paths = set(targeted_file_context.extract_paths_from_plan(plan_text))
	for test in result["tests"]:
		if test["path"] not in plan_paths and not _test_exists(repo_root, test["path"]):
			errors.append(f"missing_test_path:{test['path']}")
	workflows = _workflow_jobs(repo_root)
	for check in result["checks"]:
		if not _check_matches(check["name"], workflows):
			errors.append(f"unknown_check:{check['name']}")
	post_merge = result["post_merge"]
	if post_merge:
		info = workflows.get(post_merge["workflow"])
		if info is None:
			errors.append(f"missing_workflow:{post_merge['workflow']}")
		elif post_merge["job"] and post_merge["job"] not in info["ids"] and post_merge["job"] not in info["names"]:
			errors.append(f"unknown_job:{post_merge['workflow']}:{post_merge['job']}")
	result["state"] = "invalid" if errors else "valid"
	result["errors"] = _cap_errors(errors)
	if result["state"] == "valid" and len(marker_line(result).encode("utf-8")) > MAX_MARKER_BYTES:
		result["state"] = "invalid"
		result["errors"] = ["marker_too_large"]
	return result


def normalise(raw: Any) -> dict:
	"""Shape and charset re-check of a stored `acceptance.v1` object, without
	file checks. Anything that is not a valid done-check becomes state none."""
	if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or raw.get("state") != "valid":
		return _empty()
	source = raw.get("source")
	if source not in ("plan", "heal"):
		return _empty()
	tests_raw, checks_raw = raw.get("tests"), raw.get("checks")
	if not isinstance(tests_raw, list) or not isinstance(checks_raw, list):
		return _empty()
	if len(tests_raw) > MAX_TESTS or len(checks_raw) > MAX_CHECKS or not (tests_raw or checks_raw):
		return _empty()
	if raw.get("errors") not in ([], None):
		return _empty()
	tests = []
	for test in tests_raw:
		if not isinstance(test, dict) or not isinstance(test.get("red_on_base"), bool):
			return _empty()
		name = test.get("name") or ""
		item = _test_item(f"{test.get('path')}::{name}" if name else str(test.get("path")), test["red_on_base"])
		if item is None or not isinstance(test.get("path"), str):
			return _empty()
		tests.append(item)
	checks = []
	for check in checks_raw:
		name = _clean_name(check.get("name")) if isinstance(check, dict) else None
		if name is None or name != check.get("name"):
			return _empty()
		checks.append({"name": name})
	post_merge = raw.get("post_merge")
	post_none = raw.get("post_merge_none") or ""
	if post_merge is not None:
		if not isinstance(post_merge, dict) or not isinstance(post_merge.get("workflow"), str):
			return _empty()
		if not WORKFLOW_PATH_RE.match(post_merge["workflow"]):
			return _empty()
		job = post_merge.get("job") or ""
		if job and _clean_name(job) != job:
			return _empty()
		post_merge = {"workflow": post_merge["workflow"], "job": job}
	if post_none and _clean_reason(post_none) != post_none:
		return _empty()
	if source == "plan" and (post_merge is None) == (not post_none):
		return _empty()
	if source == "heal" and (post_merge is not None or post_none):
		return _empty()
	result = _empty("valid", source)
	result.update({"tests": tests, "checks": checks, "post_merge": post_merge, "post_merge_none": post_none})
	return result


def marker_line(obj: dict) -> str:
	text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
	# The JSON can never close the HTML comment: no literal <, > or & remains.
	text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
	return f"<!-- {MARKER} {text} -->"


def parse_marker(comment_text: str) -> dict:
	"""The host marker on the line directly above the last plan-proposal
	trailer; anything else (including a marker inside the plan text) is none."""
	lines = [line.rstrip("\r") for line in comment_text.split("\n")]
	trailer = None
	for index in range(len(lines) - 1, -1, -1):
		if lines[index] == PROPOSAL_TRAILER:
			trailer = index
			break
	if not trailer:
		return _empty()
	match = MARKER_LINE_RE.match(lines[trailer - 1])
	if not match:
		return _empty()
	try:
		raw = json.loads(match.group(1))
	except ValueError:
		return _empty()
	return normalise(raw)


def _cell(value: Any) -> str:
	return partial_delivery.sanitize_item(str(value)).replace("|", "\\|") or "-"


def _gate_statuses(gate_result: Any) -> dict[tuple[str, str], str]:
	statuses: dict[tuple[str, str], str] = {}
	acceptance = gate_result.get("acceptance") if isinstance(gate_result, dict) else None
	items = acceptance.get("items") if isinstance(acceptance, dict) else None
	for item in items if isinstance(items, list) else []:
		if isinstance(item, dict) and item.get("status") in GATE_STATUSES:
			statuses[(str(item.get("kind")), str(item.get("target")))] = item["status"]
	return statuses


def render(obj: dict, gate_result: Any = None) -> str:
	"""The PR body's `## Acceptance` section, or '' unless obj is valid."""
	obj = normalise(obj)
	if obj["state"] != "valid":
		return ""
	statuses = _gate_statuses(gate_result)
	lines = [
		"## Acceptance",
		"",
		"Done-check from the approved plan. Status stays \"not run\" until a gate runs it.",
		"",
		"| Kind | Target | Status |",
		"|---|---|---|",
	]
	for test in obj["tests"]:
		target = f"{test['path']}::{test['name']}" if test["name"] else test["path"]
		kind = "test (red-on-base)" if test["red_on_base"] else "test"
		lines.append(f"| {kind} | {_cell(target)} | {_cell(statuses.get(('test', target), 'not run'))} |")
	for check in obj["checks"]:
		status = statuses.get(("check", check["name"]), "not run")
		lines.append(f"| check | {_cell(check['name'])} | {_cell(status)} |")
	if obj["post_merge"]:
		post = "workflow " + obj["post_merge"]["workflow"]
		if obj["post_merge"]["job"]:
			post += " job " + obj["post_merge"]["job"]
	elif obj["post_merge_none"]:
		post = "none: " + obj["post_merge_none"]
	else:
		post = "not required (heal)"
	lines += ["", "Post-merge: " + _cell(post)]
	return "\n".join(lines) + "\n"


# --- CLI ----------------------------------------------------------------------


def _read_text(path: str) -> str:
	return Path(path).read_text(encoding="utf-8", errors="replace")


def _read_json(path: str) -> Any:
	try:
		return json.loads(_read_text(path))
	except ValueError:
		return None


def _dump(obj: dict) -> str:
	return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Done-check (AI_ACCEPTANCE_V1).")
	sub = parser.add_subparsers(dest="cmd", required=True)
	val = sub.add_parser("validate")
	val.add_argument("--plan-file", required=True)
	val.add_argument("--repo-root", required=True)
	val.add_argument("--out", required=True)
	mark = sub.add_parser("marker")
	mark.add_argument("--in", dest="in_file", required=True)
	par = sub.add_parser("parse")
	par.add_argument("--plan-comment-file", required=True)
	par.add_argument("--out")
	ren = sub.add_parser("render")
	ren.add_argument("--in", dest="in_file", required=True)
	ren.add_argument("--gate-result")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		if args.cmd == "validate":
			result = validate(_read_text(args.plan_file), args.repo_root)
			Path(args.out).write_text(_dump(result), encoding="utf-8")
			log(result["state"], f"errors={len(result['errors'])}")
		elif args.cmd == "marker":
			obj = normalise(_read_json(args.in_file))
			if obj["state"] == "valid":
				print(marker_line(obj))
		elif args.cmd == "parse":
			obj = parse_marker(_read_text(args.plan_comment_file))
			if args.out:
				Path(args.out).write_text(_dump(obj), encoding="utf-8")
			else:
				sys.stdout.write(_dump(obj))
			log(obj["state"], "reason=parse")
		elif args.cmd == "render":
			gate = _read_json(args.gate_result) if args.gate_result else None
			sys.stdout.write(render(_read_json(args.in_file), gate))
	except OSError:
		log("error", "reason=io")
		return 2
	return 0


if __name__ == "__main__":
	sys.exit(main())
