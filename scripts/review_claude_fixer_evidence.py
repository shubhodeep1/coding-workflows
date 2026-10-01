#!/usr/bin/env python3
"""Write and verify Claude-fixer review evidence (workflow-run artifacts).

The review workflow (`.github/workflows/review_autofix.yml`), GH_PAT-driven
Claude sessions and the second operator account all comment as the same
GitHub login, so a PR-comment marker proves nothing about what a review run
concluded. Claude-fixer mode therefore records each review round's outcome in
a workflow-run artifact, `claude-fixer-evidence-<run_id>-<run_attempt>`, and
later decisions (the checks-pending merge check, and later the GPT judge) only
trust evidence read back from a run that is verified through the API. Comment
markers merely point at run ids.

Subcommands
-----------

``write`` (called by scripts/review_autofix_step_claude_fixer_handoff.sh and
scripts/review_autofix_step_claude_fixer_merge_check.sh) writes
``<dir>/evidence.json``::

	{"schema": 1, "pr": 42, "head_sha": "<40 hex>", "round": 2,
	"outcome": "clean" | "checks-pending" | "findings" | "conflict",
	"ledger_sha256": "<64 hex>" | "", "finding_count": 0,
	"failed_checks": ["ci / lint"], "judge": null}

and, with ``--ledger-file``, copies the ledger next to it as
``reviewer_consensus.txt``. ``--judge-file`` (a JSON object
``{"decision": ..., "rulings": [...]}`` written by the review-blocked judge's
Claude mode, scripts/review_claude_fixer_judge.py) fills ``judge``. No API
calls.

``verify --repo --pr --head --run-id [--expect-outcome] [--round]
[--pr-head-ref] [--default-branch] [--changed-files-file]`` prints one JSON
line ``{"verified": bool, "reason": str, "evidence": {...} | null}`` and
always exits 0 (2 only for a usage error). The run is accepted only when all
of these hold:

(a) ``repository.full_name`` is ``--repo``;
(b) the caller is trusted: the run was started from the default branch, or
    from ``--pr-head-ref`` of this repository (not a fork) at exactly
    ``--head`` with a caller workflow file
    (``path``) the PR does not change (``--changed-files-file``, else one
    paginated ``GET /pulls/{n}/files``);
(c) it ran the library review workflow: ``path`` is
    ``.github/workflows/review_autofix.yml`` of the library repository itself,
    or ``referenced_workflows`` holds that file at ``refs/heads/main``,
    ``refs/tags/stable`` or a 40-hex release pin (consumer wrappers pin the
    release SHA; the pin sits in the caller file trusted by (b));
(d) ``status`` is ``completed``;
(e) its newest ``claude-fixer-evidence-<run_id>-<attempt>`` artifact has not
    expired and holds an ``evidence.json`` whose ``pr``, ``head_sha`` (and
    ``outcome`` / ``round`` when asked) match.

Optional stricter checks: ``--expect-ledger <sha256>`` requires the
evidence's ``ledger_sha256`` to equal it; ``--ledger-out <path>`` requires the
artifact's ``reviewer_consensus.txt`` to hash to ``ledger_sha256`` and writes
it to ``<path>`` (the GPT judge reads the ledger from here, never from a
comment); ``--require-judge`` requires ``judge`` to hold a ``rulings`` list
(the sticky-ruling loader reads prior judge rulings this way).

API budget (CLAUDE.md §15): ``GET /actions/runs/{id}``, ``GET
/actions/runs/{id}/artifacts``, and the artifact download; plus one
paginated ``GET /pulls/{n}/files`` when the run came from the PR head branch
and no changed-files list was passed; plus ``GET /repos/{repo}`` when
``--default-branch`` is empty. Nothing is cached: each ``verify`` call
reads its own run (callers verify each run id at most once). Any API failure, missing artifact, or malformed evidence returns
``verified: false`` (callers fail closed to the pre-evidence behaviour).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable


EVIDENCE_SCHEMA = 1
EVIDENCE_OUTCOMES = ("clean", "checks-pending", "findings", "conflict")
EVIDENCE_ARTIFACT_PREFIX = "claude-fixer-evidence-"
EVIDENCE_FILE_NAME = "evidence.json"
LEDGER_FILE_NAME = "reviewer_consensus.txt"
LIBRARY_REPOSITORY = "shubhodeep1/coding-workflows"
LIBRARY_REVIEW_WORKFLOW = ".github/workflows/review_autofix.yml"
TRUSTED_LIBRARY_REFS = ("refs/heads/main", "refs/tags/stable")
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_BYTES = 1024 * 1024

HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

ApiCall = Callable[[str, bool], "bytes | None"]


def _gh_api(path: str, paginate: bool = False) -> bytes | None:
	"""GET *path* through `gh api`; None on any failure."""
	command = ["gh", "api", "-X", "GET", path]
	if paginate:
		command[2:2] = ["--paginate", "--slurp"]
	try:
		proc = subprocess.run(command, check=False, capture_output=True, timeout=120)
	except (OSError, subprocess.SubprocessError):
		return None
	if proc.returncode != 0:
		return None
	return proc.stdout


def _json(raw: bytes | None) -> Any:
	if raw is None:
		return None
	try:
		return json.loads(raw.decode("utf-8"))
	except (UnicodeDecodeError, ValueError):
		return None


def build_evidence(
	*,
	pr: int,
	head_sha: str,
	round_number: int,
	outcome: str,
	ledger_sha256: str,
	finding_count: int,
	failed_checks: list[str],
	judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
	if outcome not in EVIDENCE_OUTCOMES:
		raise ValueError(f"unknown outcome {outcome!r}")
	if not HEX40_RE.match(head_sha):
		raise ValueError("head_sha must be 40 hex characters")
	if ledger_sha256 and not HEX64_RE.match(ledger_sha256):
		raise ValueError("ledger_sha256 must be empty or 64 hex characters")
	if judge is not None and (not isinstance(judge, dict) or not isinstance(judge.get("rulings"), list)):
		raise ValueError("judge must be an object with a rulings list")
	return {
		"schema": EVIDENCE_SCHEMA,
		"pr": pr,
		"head_sha": head_sha,
		"round": round_number,
		"outcome": outcome,
		"ledger_sha256": ledger_sha256,
		"finding_count": finding_count,
		"failed_checks": [name for name in failed_checks if name],
		"judge": judge,
	}


def write_evidence(out_dir: Path, evidence: dict[str, Any], ledger_file: Path | None) -> Path:
	out_dir.mkdir(parents=True, exist_ok=True)
	target = out_dir / EVIDENCE_FILE_NAME
	tmp = out_dir / f".{EVIDENCE_FILE_NAME}.tmp"
	tmp.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
	tmp.replace(target)
	if ledger_file is not None and ledger_file.is_file():
		shutil.copyfile(ledger_file, out_dir / LEDGER_FILE_NAME)
	return target


def _strip_ref_suffix(path: str) -> str:
	return path.split("@", 1)[0]


def _trusted_library_reference(entry: Any) -> bool:
	if not isinstance(entry, dict):
		return False
	path = str(entry.get("path") or "")
	if "@" not in path:
		return False
	target, suffix = path.split("@", 1)
	if target != f"{LIBRARY_REPOSITORY}/{LIBRARY_REVIEW_WORKFLOW}":
		return False
	ref = str(entry.get("ref") or "")
	if ref:
		return ref in TRUSTED_LIBRARY_REFS
	return suffix in ("main", "stable") or bool(HEX40_RE.match(suffix))


def _latest_evidence_artifact(artifacts: Any, run_id: str) -> dict[str, Any] | None:
	if not isinstance(artifacts, dict):
		return None
	name_re = re.compile(rf"^{re.escape(EVIDENCE_ARTIFACT_PREFIX)}{re.escape(run_id)}-([0-9]+)$")
	best: tuple[int, dict[str, Any]] | None = None
	for artifact in artifacts.get("artifacts") or []:
		if not isinstance(artifact, dict) or artifact.get("expired"):
			continue
		match = name_re.match(str(artifact.get("name") or ""))
		if not match:
			continue
		attempt = int(match.group(1))
		if best is None or attempt > best[0]:
			best = (attempt, artifact)
	return None if best is None else best[1]


def _read_evidence_zip(raw: bytes) -> dict[str, Any] | None:
	result = _read_evidence_zip_with_ledger(raw)
	return None if result is None else result[0]


def _read_evidence_zip_with_ledger(raw: bytes) -> tuple[dict[str, Any], bytes | None] | None:
	"""Return (evidence, ledger bytes or None); None when the zip is unusable."""
	if len(raw) > MAX_ARTIFACT_BYTES:
		return None
	try:
		with zipfile.ZipFile(io.BytesIO(raw)) as archive:
			names = [name for name in archive.namelist() if name.rsplit("/", 1)[-1] == EVIDENCE_FILE_NAME]
			if len(names) != 1:
				return None
			info = archive.getinfo(names[0])
			if info.file_size > MAX_EVIDENCE_BYTES:
				return None
			payload = json.loads(archive.read(names[0]).decode("utf-8"))
			ledger_names = [name for name in archive.namelist() if name.rsplit("/", 1)[-1] == LEDGER_FILE_NAME]
			ledger: bytes | None = None
			if len(ledger_names) == 1 and archive.getinfo(ledger_names[0]).file_size <= MAX_ARTIFACT_BYTES:
				ledger = archive.read(ledger_names[0])
	except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, ValueError, OSError):
		return None
	return (payload, ledger) if isinstance(payload, dict) else None


def _changed_files(repo: str, pr: int, changed_files_file: str, api: ApiCall) -> set[str] | None:
	if changed_files_file:
		try:
			text = Path(changed_files_file).read_text(encoding="utf-8")
		except OSError:
			return None
		return {line.strip() for line in text.splitlines() if line.strip()}
	pages = _json(api(f"repos/{repo}/pulls/{pr}/files?per_page=100", True))
	if not isinstance(pages, list):
		return None
	files: set[str] = set()
	for page in pages:
		if not isinstance(page, list):
			return None
		for entry in page:
			if not isinstance(entry, dict):
				return None
			for key in ("filename", "previous_filename"):
				if entry.get(key):
					files.add(str(entry[key]))
	return files


def verify_evidence(
	*,
	repo: str,
	pr: int,
	head_sha: str,
	run_id: str,
	expect_outcome: str = "",
	expect_round: int | None = None,
	pr_head_ref: str = "",
	default_branch: str = "",
	changed_files_file: str = "",
	expect_ledger: str = "",
	ledger_out: str = "",
	require_judge: bool = False,
	api: ApiCall = _gh_api,
) -> dict[str, Any]:
	def reject(reason: str) -> dict[str, Any]:
		return {"verified": False, "reason": reason, "evidence": None}

	if not re.match(r"^[1-9][0-9]*$", run_id or ""):
		return reject("invalid_run_id")
	if not HEX40_RE.match(head_sha or ""):
		return reject("invalid_head")
	run = _json(api(f"repos/{repo}/actions/runs/{run_id}", False))
	if not isinstance(run, dict):
		return reject("run_unavailable")
	run_repo = str(((run.get("repository") or {}) if isinstance(run.get("repository"), dict) else {}).get("full_name") or "")
	if run_repo.lower() != repo.lower():
		return reject("run_repository_mismatch")
	if run.get("status") != "completed":
		return reject("run_not_completed")
	if not default_branch:
		repo_meta = _json(api(f"repos/{repo}", False))
		default_branch = str(repo_meta.get("default_branch") or "") if isinstance(repo_meta, dict) else ""
		if not default_branch:
			return reject("default_branch_unavailable")
	run_path = _strip_ref_suffix(str(run.get("path") or ""))
	run_branch = str(run.get("head_branch") or "")
	if not run_path:
		return reject("run_path_unavailable")
	if run_branch == default_branch:
		pass
	elif pr_head_ref and run_branch == pr_head_ref and str(run.get("head_sha") or "") == head_sha:
		# A missing head repository (a deleted fork) proves nothing about the caller: fail closed.
		head_repo = run.get("head_repository") if isinstance(run.get("head_repository"), dict) else {}
		if str(head_repo.get("full_name") or "").lower() != repo.lower():
			return reject("untrusted_caller_ref")
		changed = _changed_files(repo, pr, changed_files_file, api)
		if changed is None:
			return reject("pr_files_unavailable")
		if run_path in changed:
			return reject("caller_workflow_changed_by_pr")
	else:
		return reject("untrusted_caller_ref")
	direct_library_run = repo.lower() == LIBRARY_REPOSITORY and run_path == LIBRARY_REVIEW_WORKFLOW and run_branch == default_branch
	if not direct_library_run and not any(_trusted_library_reference(entry) for entry in run.get("referenced_workflows") or []):
		return reject("untrusted_review_workflow")
	artifact = _latest_evidence_artifact(_json(api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100", False)), run_id)
	if artifact is None or not artifact.get("id"):
		return reject("artifact_missing")
	raw = api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", False)
	if raw is None:
		return reject("artifact_download_failed")
	unpacked = _read_evidence_zip_with_ledger(raw)
	if unpacked is None:
		return reject("evidence_malformed")
	evidence, ledger_bytes = unpacked
	if evidence.get("schema") != EVIDENCE_SCHEMA or evidence.get("outcome") not in EVIDENCE_OUTCOMES:
		return reject("evidence_malformed")
	if evidence.get("pr") != pr or evidence.get("head_sha") != head_sha:
		return reject("evidence_pr_or_head_mismatch")
	if expect_outcome and evidence.get("outcome") != expect_outcome:
		return reject("evidence_outcome_mismatch")
	if expect_round is not None and evidence.get("round") != expect_round:
		return reject("evidence_round_mismatch")
	if expect_ledger and evidence.get("ledger_sha256") != expect_ledger:
		return reject("evidence_ledger_mismatch")
	if require_judge and not (isinstance(evidence.get("judge"), dict) and isinstance(evidence["judge"].get("rulings"), list)):
		return reject("evidence_judge_missing")
	if ledger_out:
		digest = str(evidence.get("ledger_sha256") or "")
		if ledger_bytes is None or not HEX64_RE.match(digest):
			return reject("evidence_ledger_missing")
		if hashlib.sha256(ledger_bytes).hexdigest() != digest:
			return reject("evidence_ledger_digest_mismatch")
		Path(ledger_out).write_bytes(ledger_bytes)
	return {"verified": True, "reason": "ok", "evidence": evidence}


def _cmd_write(args: argparse.Namespace) -> int:
	failed = [name.strip() for name in (args.failed_checks or "").split(",") if name.strip()]
	judge = None
	if args.judge_file:
		judge = json.loads(Path(args.judge_file).read_text(encoding="utf-8"))
	evidence = build_evidence(
		pr=args.pr,
		head_sha=args.head,
		round_number=args.round,
		outcome=args.outcome,
		ledger_sha256=args.ledger_sha256 or "",
		finding_count=args.finding_count,
		failed_checks=failed,
		judge=judge,
	)
	ledger = Path(args.ledger_file) if args.ledger_file else None
	print(write_evidence(Path(args.out_dir), evidence, ledger))
	return 0


def _cmd_verify(args: argparse.Namespace) -> int:
	try:
		result = verify_evidence(
			repo=args.repo,
			pr=args.pr,
			head_sha=args.head,
			run_id=args.run_id,
			expect_outcome=args.expect_outcome or "",
			expect_round=args.round,
			pr_head_ref=args.pr_head_ref or "",
			default_branch=args.default_branch or "",
			changed_files_file=args.changed_files_file or "",
			expect_ledger=args.expect_ledger or "",
			ledger_out=args.ledger_out or "",
			require_judge=bool(args.require_judge),
		)
	except Exception as exc:  # noqa: BLE001 - verify must never fail open or crash the caller
		result = {"verified": False, "reason": f"internal_error:{type(exc).__name__}", "evidence": None}
	print(json.dumps(result, sort_keys=True))
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
	sub = parser.add_subparsers(dest="command", required=True)
	write = sub.add_parser("write")
	write.add_argument("--out-dir", required=True)
	write.add_argument("--pr", type=int, required=True)
	write.add_argument("--head", required=True)
	write.add_argument("--round", type=int, required=True)
	write.add_argument("--outcome", choices=EVIDENCE_OUTCOMES, required=True)
	write.add_argument("--ledger-sha256", default="")
	write.add_argument("--ledger-file", default="")
	write.add_argument("--finding-count", type=int, default=0)
	write.add_argument("--failed-checks", default="")
	write.add_argument("--judge-file", default="")
	verify = sub.add_parser("verify")
	verify.add_argument("--repo", required=True)
	verify.add_argument("--pr", type=int, required=True)
	verify.add_argument("--head", required=True)
	verify.add_argument("--run-id", required=True)
	verify.add_argument("--expect-outcome", default="")
	verify.add_argument("--round", type=int, default=None)
	verify.add_argument("--pr-head-ref", default="")
	verify.add_argument("--default-branch", default=os.environ.get("DEFAULT_BRANCH", ""))
	verify.add_argument("--changed-files-file", default="")
	verify.add_argument("--expect-ledger", default="")
	verify.add_argument("--ledger-out", default="")
	verify.add_argument("--require-judge", action="store_true")
	args = parser.parse_args(argv)
	if args.command == "write":
		try:
			return _cmd_write(args)
		except (ValueError, OSError) as exc:
			print(f"::error::review_claude_fixer_evidence write: {exc}", file=sys.stderr)
			return 2
	return _cmd_verify(args)


if __name__ == "__main__":
	raise SystemExit(main())
