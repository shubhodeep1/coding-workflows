#!/usr/bin/env python3
"""Soft-error log analyzer for the release-gate smoke test.

Fetches logs from one or more workflow runs, filters them to high-signal lines
(warnings, retries, rate-limit recoveries, codex fallbacks, summariser hard
fails, etc.), truncates each run's log to fit the analyser model's context
window, then asks an OpenRouter chat model (default openai/gpt-5.6-luna at
medium reasoning per the OpenAI prompt guide for cross-run log
triage) to produce a short markdown report enumerating soft errors.

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

# Per-run cap on the *filtered* log payload sent to the analyser.
#
# Set to `None` to disable input-side truncation entirely — the
# release-gate operator chose this so per-phase analyser invocations
# get the complete soft-error signal even when reviewer/codex log
# volume is high. The downstream OpenRouter call still has its own
# context-window limit (gpt-5.6-luna = 1.05M tokens); on overflow the
# existing `call_failed` fallback path in `main()` writes a stub
# report rather than crashing the workflow, so the operator can
# safely opt for "no truncation" without breaking the release gate.
PER_RUN_CHAR_BUDGET: int | None = None

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

# Lines that look noisy under SOFT_ERROR_PATTERNS but are actually
# structured-telemetry success signals. Matched case-insensitively against
# the *whole* line; on a hit the line is excluded from the analyser payload
# even when one of the inclusion patterns also matches. Use this for known
# JSON event shapes the workflows emit during normal operation, where the
# raw "FAIL" / "ERROR" / numeric-status keywords appear inside JSON values
# (e.g. `"last_error":""`, `"http_status":200`, `"event":"batch_noop"`)
# rather than as actual error indicators. Reviewer-flagged false-positive
# mitigation; conservative on purpose — only adds known-safe sigils.
SOFT_ERROR_EXCLUDE_PATTERNS = [
	# Healthy structured telemetry events with no real failure.
	r'"event"\s*:\s*"batch_noop"',
	r'"event"\s*:\s*"codex_contract_noop"',
	r'"event"\s*:\s*"telemetry"\b.*"status"\s*:\s*"ok"',
	# JSON keys that contain failure-keyword substrings but are field
	# names rather than incident indicators.
	r'"(last_error|prev_error|previous_failure|max_retries|retry_count|cache_misses|fallback_used|error_count|warning_count)"\s*:\s*("?(?:0|"\s*"|"")"?|null|false)\b',
	# `gh api` HTTP envelopes that print the matched status code in the
	# 2xx range — these are healthy responses but the line trips the
	# numeric-status soft-error pattern.
	r"\bHTTP/[12]\.[01]\s+(20\d|30\d)\b",
	# Single-line `STATUS=success conclusion=success` summaries from the
	# orphan-workflows-test dispatch loops, which contain literal
	# "success" but also reference status keywords.
	r"\bSTATUS=completed\b.*\bCONCLUSION=success\b",
]
SOFT_ERROR_EXCLUDE_RE = re.compile("|".join(SOFT_ERROR_EXCLUDE_PATTERNS), re.IGNORECASE)


# GitHub Actions step source preamble.
#
# When a `run:` step starts, the runner emits a `##[group]Run …` header,
# echoes every line of the shell script source bracketed by ANSI cyan-bold
# (`\033[36;1m...\033[0m`) plus the `shell:` and `env:` blocks, then closes
# with `##[endgroup]` *before* the actual step output begins. None of those
# preamble lines are real workflow events — they are verbatim copies of the
# step source. Without filtering them out the SOFT_ERROR_RE matcher trips
# on every literal `echo "::warning::Codex returned empty output on attempt
# ${attempt}"` etc. inside the script source, producing false-positive
# findings for retries that never fired (the unexpanded `${attempt}` /
# `$rc` shell variable references in the matched lines are the giveaway).
#
# `strip_source_echo` runs over `gh run view --log` output (which
# preserves the `\033[36;1m` color sequences) and removes:
#   1. Everything between `##[group]Run …` and the matching `##[endgroup]`.
#   2. As a defensive second pass, any line whose body still contains
#      `\033[36;1m` (handles malformed/nested groups).
GROUP_RUN_START_RE = re.compile(r"##\[group\]Run\b")
GROUP_END_RE = re.compile(r"##\[endgroup\]")
ANSI_CYAN_BOLD_RE = re.compile(r"\x1b\[36;1m")


def strip_source_echo(text: str) -> str:
	"""Drop GH Actions step source preamble lines from log text.

	Removes the `##[group]Run … ##[endgroup]` block (script source +
	`shell:` + `env:` echo) so SOFT_ERROR_RE does not match against
	echoed `echo "...failed..."` strings inside step source code.
	"""
	if not text:
		return text
	out: list[str] = []
	in_group_run = False
	for line in text.splitlines():
		if in_group_run:
			if GROUP_END_RE.search(line):
				in_group_run = False
			continue
		if GROUP_RUN_START_RE.search(line):
			in_group_run = True
			continue
		if ANSI_CYAN_BOLD_RE.search(line):
			# Defensive: catches stray cyan-bold echoes outside a
			# detected ##[group]Run … ##[endgroup] envelope.
			continue
		out.append(line)
	return "\n".join(out)


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


def filter_soft_error_lines(text: str, char_budget: int | None) -> str:
	"""Keep only lines that look like soft-error signal, plus 1 line of context.

	`char_budget=None` disables truncation entirely (operator opt-in for
	complete signal at the cost of larger OpenRouter payloads). The
	caller-supplied integer cap, when set, truncates from the middle so
	both the head (early-pipeline failures) and tail (post-mortem
	fallbacks) survive.
	"""
	if not text:
		return ""
	# Drop GH Actions step source preamble first so SOFT_ERROR_RE does
	# not match against echoed `echo "...failed..."` strings inside the
	# step source. The unexpanded `${attempt}` / `$rc` variables in
	# such echoes were producing false-positive "retry fired" findings
	# even when no retry actually happened.
	text = strip_source_echo(text)
	lines = text.splitlines()
	keep = [False] * len(lines)
	for i, line in enumerate(lines):
		# Inclusion gate: line matches a soft-error keyword pattern.
		if not SOFT_ERROR_RE.search(line):
			continue
		# Exclusion gate: the line is structured telemetry that happens
		# to share substrings with the inclusion patterns. Drop it so
		# the analyser's input is not poisoned with false-positive
		# success events.
		if SOFT_ERROR_EXCLUDE_RE.search(line):
			continue
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
	if char_budget is None or len(joined) <= char_budget:
		return joined

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
	# `max_tokens` deliberately omitted — release-gate operator opted out
	# of an output cap. OpenRouter / the underlying model uses its
	# default response limit, which is sufficient for the bounded
	# markdown report this prompt requests (system prompt enforces
	# "<800 words" and three-section structure).
	body = {
		"model": model,
		"messages": messages,
		"reasoning": {"effort": reasoning},
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
		default=os.environ.get("LOG_ANALYZER_MODEL", "openai/gpt-5.6-luna"),
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

	# Status header is parseable: "## Soft-error analyzer (status: <code>, ...)".
	# Codes:
	#   - no_runs          : caller supplied no --run specs (caller bug).
	#   - api_skipped      : OPENROUTER_API_KEY missing → analyser did not
	#                         call the model. Different from a crash.
	#   - call_failed      : analyser called the model but the call raised.
	#   - analyser_empty   : model returned empty content (suspicious — model
	#                         output failure, not a "no-findings" signal).
	#   - ok               : model returned content. Findings are inside;
	#                         the body itself disambiguates "no findings"
	#                         from "many findings".
	def _write_report(status: str, body: str) -> None:
		header = (
			f"## Soft-error analyzer (status: `{status}`, model: "
			f"`{args.model}`, reasoning: `{args.reasoning}`)\n\n"
		)
		out_path.write_text(header + body.strip() + "\n", encoding="utf-8")

	if not runs:
		_write_report("no_runs", "No runs were supplied.")
		return 0

	api_key = os.environ.get("OPENROUTER_API_KEY")
	if not api_key:
		_write_report(
			"api_skipped",
			"OPENROUTER_API_KEY is not set; analyser was skipped. "
			f"Phases collected: {', '.join(r['phase'] for r in runs)}.",
		)
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
		_write_report(
			"call_failed",
			f"Analyser call failed: `{exc}`. Filtered log excerpts "
			"retained in the workflow run artifacts.",
		)
		return 0

	if not report.strip():
		_write_report(
			"analyser_empty",
			"Model returned empty content. Treat as analyser failure, "
			"not as 'no soft errors found'.",
		)
		return 0

	_write_report("ok", report)
	return 0


if __name__ == "__main__":
	sys.exit(main())
