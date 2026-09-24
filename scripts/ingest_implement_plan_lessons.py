#!/usr/bin/env python3
"""Ingest `/implement-plan-claude` progress-log lessons into AI memory.

Wired into `.github/workflows/issue_pr_status.yml`: when a PR from a
`claude/implement-plan-*` or `claude/verify-activation-*` branch merges, the
workflow runs this script against the merge commit. It reads every
`docs/implement-plan/*.md` progress log at that commit (except `README.md`),
parses each log's `## Lessons` section, and writes one
`lessons_learned_record.v1` record per lesson to the `ai-memory` branch.

Lesson line format (see the Progress Log section of
`.claude/commands/implement-plan-claude.md`):

	- [source:<source>] <lesson text> (files: <path>, <path>)

`<source>` is one of LESSON_SOURCES; the `(files: ...)` suffix is optional.
Lines that do not match are ignored.

Records use phase `implement_plan`, kind `project_retrospective`, and tags
`source:<source>`, `plan:<slug>`, and `file:<path>`. Record ids are derived
from the slug, source, and whitespace-normalized text, and a record whose
file already exists is skipped, so re-ingesting a log is a no-op.

Kill switches: `AI_MEMORY_ENABLED` and `LESSONS_LEARNED_ENABLED` (both default
true). The script is fail-open: any error is reported as a workflow warning
plus an `AI_MEMORY_TELEMETRY` line, and the exit code stays 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
	import ai_memory_lib
except ModuleNotFoundError:
	from scripts import ai_memory_lib


LESSON_SOURCES = (
	"conformance",
	"security",
	"validation",
	"intervention",
	"plan-deviation",
	"activation",
)
LESSONS_HEADING = "## Lessons"
LESSON_LINE_RE = re.compile(
	r"^- \[source:(?P<source>" + "|".join(re.escape(s) for s in LESSON_SOURCES) + r")\]\s+"
	r"(?P<text>.+?)"
	r"(?:\s+\(files:\s*(?P<files>.*?)\))?\s*$"
)
LOG_DIR_DEFAULT = "docs/implement-plan"
LESSON_PHASE = "implement_plan"
LESSON_KIND = "project_retrospective"
TELEMETRY_OP = "ingest_implement_plan_lessons"


def is_truthy(value: str | None, default: bool = True) -> bool:
	if value is None or not str(value).strip():
		return default
	return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_lessons(markdown: str) -> list[dict[str, Any]]:
	"""Return the lessons in a progress log's `## Lessons` section.

	Each item is `{"source": str, "text": str, "files": [str, ...]}`. Lines
	inside fenced code blocks and lines that do not match the format are
	skipped.
	"""
	lessons: list[dict[str, Any]] = []
	in_section = False
	in_fence = False
	for raw_line in markdown.splitlines():
		line = raw_line.rstrip()
		if line.lstrip().startswith("```"):
			in_fence = not in_fence
			continue
		if in_fence:
			continue
		if line.startswith("## "):
			in_section = line.strip() == LESSONS_HEADING
			continue
		if not in_section:
			continue
		match = LESSON_LINE_RE.match(line.strip())
		if not match:
			continue
		text = " ".join(match.group("text").split())
		if not text:
			continue
		files: list[str] = []
		for item in (match.group("files") or "").split(","):
			path = item.strip().strip("`")
			if path and path not in files:
				files.append(path)
		lessons.append({"source": match.group("source"), "text": text, "files": files})
	return lessons


def lesson_record_id(slug: str, source: str, text: str) -> str:
	return ai_memory_lib.make_deterministic_record_id("lesson-implement-plan", slug, source, text)


def lesson_payload(slug: str, lesson: dict[str, Any]) -> dict[str, Any]:
	tags = [f"source:{lesson['source']}", f"plan:{slug}"]
	for path in lesson["files"]:
		tag = f"file:{path}"
		if tag not in tags:
			tags.append(tag)
	return {"lesson_kind": LESSON_KIND, "lesson_text": lesson["text"], "tags": tags}


def _git(repo_root: Path, args: list[str]) -> str:
	result = subprocess.run(
		["git", *args],
		cwd=str(repo_root),
		check=True,
		capture_output=True,
		text=True,
	)
	return result.stdout


def read_logs_at_ref(repo_root: Path, ref: str, log_dir: str) -> dict[str, str]:
	"""Return `{slug: markdown}` for every progress log under `log_dir` at `ref`."""
	listing = _git(repo_root, ["ls-tree", "--name-only", ref, "--", f"{log_dir.rstrip('/')}/"])
	logs: dict[str, str] = {}
	for path in listing.splitlines():
		name = Path(path).name
		if not name.endswith(".md") or name.lower() == "readme.md":
			continue
		logs[name[: -len(".md")]] = _git(repo_root, ["show", f"{ref}:{path}"])
	return logs


def collect_lessons(logs: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
	"""Return `(lesson payloads, record ids)` across all logs, de-duplicated by id."""
	payloads: list[dict[str, Any]] = []
	record_ids: list[str] = []
	for slug in sorted(logs):
		for lesson in parse_lessons(logs[slug]):
			record_id = lesson_record_id(slug, lesson["source"], lesson["text"])
			if record_id in record_ids:
				continue
			record_ids.append(record_id)
			payloads.append(lesson_payload(slug, lesson))
	return payloads, record_ids


def _positive_int(value: str | None) -> int | None:
	try:
		parsed = int(str(value or "").strip())
	except ValueError:
		return None
	return parsed if parsed > 0 else None


def emit_telemetry(payload: dict[str, Any]) -> None:
	print(f"AI_MEMORY_TELEMETRY: {json.dumps(payload, ensure_ascii=True, sort_keys=True)}", file=sys.stderr)


def run(args: argparse.Namespace) -> dict[str, Any]:
	telemetry: dict[str, Any] = {
		"op": TELEMETRY_OP,
		"ok": True,
		"phase": LESSON_PHASE,
		"pr_number": args.pr_number,
		"ref": args.ref,
		"logs": 0,
		"parsed": 0,
		"written": 0,
		"did_push": False,
	}
	if not is_truthy(os.environ.get("AI_MEMORY_ENABLED")) or not is_truthy(os.environ.get("LESSONS_LEARNED_ENABLED")):
		telemetry["enabled"] = False
		return telemetry

	repo_root = Path(args.repo_root).resolve()
	logs = read_logs_at_ref(repo_root, args.ref, args.log_dir)
	payloads, record_ids = collect_lessons(logs)
	telemetry["logs"] = len(logs)
	telemetry["parsed"] = len(payloads)
	if not payloads or args.dry_run:
		telemetry["dry_run"] = bool(args.dry_run)
		return telemetry

	def operation(clone_dir: Path) -> dict[str, Any]:
		memory_root = ai_memory_lib.resolve_memory_root_dir(clone_dir, args.memory_root)
		records = ai_memory_lib.record_lessons_learned(
			memory_root,
			issue_number=None,
			pr_number=args.pr_number,
			phase=LESSON_PHASE,
			lessons=payloads,
			record_ids=record_ids,
		)
		return {"records": records}

	result = ai_memory_lib.persist_memory_operation(
		repo_root,
		memory_branch=args.memory_branch,
		memory_root_relative=args.memory_root,
		push_retries=args.push_retries,
		commit_message=f"ai-memory: record implement-plan lessons [PR #{args.pr_number or 'unknown'}]",
		operation=operation,
	)
	records = (result.get("operation_result") or {}).get("records") or []
	telemetry["written"] = len(records)
	telemetry["did_push"] = bool(result.get("did_push", False))
	return telemetry


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--ref", required=True, help="git ref to read the progress logs at (the merge commit)")
	parser.add_argument("--repo-root", default=".", help="repository checkout (default: cwd)")
	parser.add_argument("--pr-number", type=_positive_int, default=None, help="merged PR number recorded on new lessons")
	parser.add_argument("--log-dir", default=LOG_DIR_DEFAULT, help=f"progress-log directory (default: {LOG_DIR_DEFAULT})")
	parser.add_argument("--memory-branch", default=os.environ.get("AI_MEMORY_BRANCH") or "ai-memory")
	parser.add_argument("--memory-root", default=os.environ.get("AI_MEMORY_ROOT") or "ai-memory")
	parser.add_argument("--push-retries", type=int, default=_positive_int(os.environ.get("AI_MEMORY_PUSH_RETRIES")) or 16)
	parser.add_argument("--dry-run", action="store_true", help="parse and report only; write nothing")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		telemetry = run(args)
	except Exception as exc:  # fail-open: lessons are best-effort bookkeeping
		print(f"::warning::implement-plan lessons ingestion failed; continuing fail-open: {exc}", file=sys.stderr)
		emit_telemetry({"op": TELEMETRY_OP, "ok": False, "fail_open": True, "phase": LESSON_PHASE, "ref": args.ref})
		return 0
	emit_telemetry(telemetry)
	print(
		f"implement-plan lessons: logs={telemetry['logs']} parsed={telemetry['parsed']} "
		f"written={telemetry['written']} did_push={str(telemetry['did_push']).lower()}"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
