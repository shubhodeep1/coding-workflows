#!/usr/bin/env python3
"""Soft-error log analyzer for the release-gate smoke test.

Fetches logs from one or more workflow runs, filters them to high-signal lines
(warnings, retries, rate-limit recoveries, codex fallbacks, summariser hard
fails, etc.), truncates each run's log to fit the analyser model's context
window, then asks an OpenRouter chat model (default openai/gpt-5.4-mini at
medium reasoning) to produce a short markdown report enumerating soft errors.

The analyser is non-blocking: if OpenRouter call fails or any run's logs are
unreachable, the script writes a stub report rather than exiting non-zero, so
the release gate's downstream Telegram notification step always has something
to attach. Hard exit codes are reserved for argument errors only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Per-run cap on the *filtered* log payload sent to the analyser. With ~5 runs
# this puts us comfortably under a 200K-token window for gpt-5.4-mini even in
# the worst case (3 chars/token estimate). Conservative on purpose: prompt
# overhead, system message, and model-specific tokenization variance can each
# eat ~10–15% of the nominal budget.
PER_RUN_CHAR_BUDGET = 60000

# Patterns that mark soft-error candidates worth surfacing. Matched
# case-insensitively. Everything else gets dropped from the per-run payload to
# keep the analyser's input dense and on-topic.
SOFT_ERROR_PATTERNS = [
	r"\bWARNING\b",
	r"\b::warning::",
	r"\bERROR\b",
	r"\b::error::",
	r"\bFAIL(ED|URE)?\b",
	r"\bretry(ing)?\b",
	r"\bfallback\b",
	r"\brate.?limit",
	r"\btimed?[ -]?out\b",
	r"\bdeadline exceeded\b",
	r"\bcodex.*(fail|error|abort|noop)",
	r"\bopenrouter\b.*(error|fail|429|5\d\d)",
	r"\bsummari[sz]er\b.*(fail|hard.?fail|abort)",
	r"\beditor.*(noop|noop[_ -]suspicious|fallback|abort)",
	r"\breviewer.*(fail|abort|fallback)",
	r"\bcache.*(miss|invalid|disabled)",
	r"\bnon.?zero exit\b",
	r"\bexit code\s+\d+",
	r"\bAI_PHASE_FAILURE_V1\b",
	r"\bSIGTERM\b|\bSIGKILL\b",
	r"\bUnauthorized\b|\b401\b|\b403\b|\b404\b|\b429\b|\b5\d\d\b",
]
SOFT_ERROR_RE = re.compile("|".join(SOFT_ERROR_PATTERNS), re.IGNORECASE)


def _gh_api(path: str, *, accept: str = "application/vnd.github+json") -> bytes:
	token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
	if not token:
		raise RuntimeError("GH_TOKEN / GITHUB_TOKEN must be set")
	url = f"https://api.github.com/{path.lstrip('/')}"
	req = urllib.request.Request(
		url,
		headers={
			"Authorization": f"Bearer {token}",
			"Accept": accept,
			"User-Agent": "coding-workflows-soft-error-analyzer",
		},
	)
	with urllib.request.urlopen(req, timeout=120) as resp:
		return resp.read()


def fetch_run_logs(repo: str, run_id: str) -> str:
	"""Return concatenated log text for a workflow run, or an explanatory stub."""
	try:
		# Prefer `gh run view --log` (already configured & rate-limit-aware on
		# GitHub-hosted runners) over the raw zip endpoint — the zip path costs
		# more API budget and forces an in-process unzip, both of which are
		# wasted work for an analyser that only reads the text content. If `gh`
		# is unavailable or fails, fall back to the zip endpoint.
		proc = subprocess.run(
			["gh", "run", "view", run_id, "--log", "--repo", repo],
			capture_output=True,
			text=True,
			timeout=180,
			check=False,
		)
		if proc.returncode == 0 and proc.stdout.strip():
			return proc.stdout
		stderr_tail = (proc.stderr or "")[-400:]
		# Fall through to zip endpoint on non-zero exit.
		print(
			f"::warning::analyze_soft_errors: gh run view failed for run "
			f"{run_id} (rc={proc.returncode}); trying zip endpoint. "
			f"stderr-tail={stderr_tail!r}",
			file=sys.stderr,
		)
	except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
		print(
			f"::warning::analyze_soft_errors: gh CLI unusable for run {run_id}: {exc}",
			file=sys.stderr,
		)

	# Zip-endpoint fallback. We can't depend on python's zipfile at runtime
	# producing perfectly-deterministic output, so just return the raw bytes
	# decoded permissively. The analyser model tolerates noisy framing.
	try:
		raw = _gh_api(f"repos/{repo}/actions/runs/{run_id}/logs")
		return raw.decode("utf-8", errors="replace")
	except urllib.error.HTTPError as exc:
		return f"[soft-error-analyzer] could not fetch logs for run {run_id}: HTTP {exc.code}"
	except Exception as exc:  # noqa: BLE001 — we deliberately swallow to stay non-blocking
		return f"[soft-error-analyzer] could not fetch logs for run {run_id}: {exc}"


def filter_soft_error_lines(text: str, char_budget: int) -> str:
	"""Keep only lines that look like soft-error signal, plus 1 line of context."""
	if not text:
		return ""
	lines = text.splitlines()
	keep = [False] * len(lines)
	for i, line in enumerate(lines):
		if SOFT_ERROR_RE.search(line):
			keep[i] = True
			if i > 0:
				keep[i - 1] = True
			if i + 1 < len(lines):
				keep[i + 1] = True

	out: list[str] = []
	last_emitted = -2
	for i, kept in enumerate(keep):
		if not kept:
			continue
		if last_emitted >= 0 and i > last_emitted + 1:
			out.append("…")
		out.append(lines[i])
		last_emitted = i

	joined = "\n".join(out)
	if len(joined) <= char_budget:
		return joined

	# Truncate from the middle so the head (early-pipeline failures) and tail
	# (post-mortem fallbacks) both survive — those are the highest-signal
	# regions for soft-error triage.
	half = char_budget // 2
	head = joined[: half - 50]
	tail = joined[-(half - 50):]
	return f"{head}\n…[truncated for context window]…\n{tail}"


def build_analyser_prompt(repo: str, runs: list[dict]) -> list[dict]:
	system = (
		"You are a release-gate observability assistant. You receive filtered "
		"log excerpts from a GitHub Actions smoke-test pipeline (clarify, "
		"plan, implement, review_autofix, orchestrate-poll). Your job is to "
		"identify SOFT errors that did not fail the workflow but indicate "
		"latent issues: rate-limit recoveries, retried LLM calls, codex "
		"fallback paths, summariser hard-fails, reviewer abort/fallback, "
		"editor no-op-suspicious flips, cache misses on hot paths, transient "
		"5xx responses, etc.\n\n"
		"Output a SHORT markdown report under 800 words with these sections "
		"(omit a section if empty):\n"
		"  ## Soft errors (per phase)\n"
		"  ## Patterns to watch\n"
		"  ## Likely benign\n"
		"For each finding cite the phase name and one short log fragment "
		"(<=120 chars). Do NOT speculate beyond the evidence in the logs. "
		"If logs are empty or only contain '[soft-error-analyzer] could not "
		"fetch logs', say so explicitly under '## Patterns to watch'."
	)

	user_parts = [f"Repository: `{repo}`", ""]
	for run in runs:
		user_parts.append(f"### {run['phase']} (run #{run['run_id']})")
		body = run["filtered"].strip() or "(no soft-error candidates after filtering)"
		user_parts.append("```log")
		user_parts.append(body)
		user_parts.append("```")
		user_parts.append("")

	return [
		{"role": "system", "content": system},
		{"role": "user", "content": "\n".join(user_parts)},
	]


def call_openrouter(
	messages: list[dict],
	*,
	model: str,
	reasoning: str,
	api_key: str,
) -> str:
	url = "https://openrouter.ai/api/v1/chat/completions"
	body = {
		"model": model,
		"messages": messages,
		"reasoning": {"effort": reasoning},
		"max_tokens": 1500,
	}
	data = json.dumps(body).encode("utf-8")
	req = urllib.request.Request(
		url,
		data=data,
		headers={
			"Authorization": f"Bearer {api_key}",
			"Content-Type": "application/json",
			"HTTP-Referer": "https://github.com/shubhodeep1/coding-workflows",
			"X-Title": "coding-workflows soft-error analyzer",
		},
	)
	with urllib.request.urlopen(req, timeout=300) as resp:
		payload = json.loads(resp.read().decode("utf-8"))
	choices = payload.get("choices") or []
	if not choices:
		raise RuntimeError(f"OpenRouter returned no choices: {payload!r}")
	return choices[0].get("message", {}).get("content") or ""


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--repo", required=True, help="owner/repo")
	parser.add_argument(
		"--run",
		action="append",
		default=[],
		metavar="PHASE=RUN_ID",
		help="Phase label and run ID, e.g. clarify=12345. Repeatable.",
	)
	parser.add_argument(
		"--model",
		default=os.environ.get("LOG_ANALYZER_MODEL", "openai/gpt-5.4-mini"),
	)
	parser.add_argument(
		"--reasoning",
		default=os.environ.get("LOG_ANALYZER_REASONING", "medium"),
	)
	parser.add_argument(
		"--output",
		required=True,
		help="Path to write the markdown report",
	)
	args = parser.parse_args()

	if not args.run:
		print("error: at least one --run PHASE=RUN_ID is required", file=sys.stderr)
		return 2

	out_path = Path(args.output)
	out_path.parent.mkdir(parents=True, exist_ok=True)

	runs: list[dict] = []
	for spec in args.run:
		if "=" not in spec:
			print(f"::warning::skipping malformed --run spec {spec!r}", file=sys.stderr)
			continue
		phase, run_id = spec.split("=", 1)
		phase = phase.strip()
		run_id = run_id.strip()
		if not phase or not run_id or not run_id.isdigit():
			print(f"::warning::skipping malformed --run spec {spec!r}", file=sys.stderr)
			continue
		raw = fetch_run_logs(args.repo, run_id)
		filtered = filter_soft_error_lines(raw, PER_RUN_CHAR_BUDGET)
		runs.append({"phase": phase, "run_id": run_id, "filtered": filtered})

	if not runs:
		out_path.write_text(
			"## Soft-error analyzer\n\nNo runs were supplied.\n",
			encoding="utf-8",
		)
		return 0

	api_key = os.environ.get("OPENROUTER_API_KEY")
	if not api_key:
		stub = (
			"## Soft-error analyzer\n\n"
			"OPENROUTER_API_KEY is not set; analyser was skipped. "
			f"Phases collected: {', '.join(r['phase'] for r in runs)}.\n"
		)
		out_path.write_text(stub, encoding="utf-8")
		return 0

	messages = build_analyser_prompt(args.repo, runs)

	try:
		report = call_openrouter(
			messages,
			model=args.model,
			reasoning=args.reasoning,
			api_key=api_key,
		)
	except Exception as exc:  # noqa: BLE001 — non-blocking by design
		stub = (
			"## Soft-error analyzer\n\n"
			f"Analyser call failed: `{exc}`. Filtered log excerpts retained "
			f"in the workflow run artifacts.\n"
		)
		out_path.write_text(stub, encoding="utf-8")
		return 0

	header = (
		f"## Soft-error analyzer (model: `{args.model}`, reasoning: "
		f"`{args.reasoning}`)\n\n"
	)
	out_path.write_text(header + report.strip() + "\n", encoding="utf-8")
	return 0


if __name__ == "__main__":
	sys.exit(main())
