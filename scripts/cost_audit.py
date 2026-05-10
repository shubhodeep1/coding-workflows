#!/usr/bin/env python3
"""cost_audit.py — Per-workflow LLM token-spend audit from GitHub Actions logs.

Pulls the last N runs per workflow via `gh`, fetches each run's log, and
aggregates token usage per workflow. Three patterns are recognised:

  1. Codex CLI ("tokens used\\n<N>") — emitted by clarify, plan, implement,
     validate, orchestrate*, orchestrate_clarify_respond.
  2. OpenRouter structured line ("INFO: openrouter usage ... prompt_tokens=N
     completion_tokens=N total_tokens=N cache_creation_input_tokens=N
     cache_read_input_tokens=N") — emitted by review_autofix via
     scripts/review_run_reviewers.sh.
  3. Semble telemetry (`SEMBLE_QUERY ... bytes=N` and
     `SEMBLE_FALLBACK ...`) — emitted by Semble-backed prompt-context
     helpers.

Output: stdout markdown table + per-run JSON file.

Requires only `gh` (authenticated) and Python 3.8+. The repo does NOT need to
be cloned locally; everything is fetched through the GitHub API.

Usage:
    python3 cost_audit.py [--repo OWNER/REPO] [--limit N] \\
        [--workflows w1.yml,w2.yml] [--since YYYY-MM-DD] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from typing import Dict, List, Optional

DEFAULT_WORKFLOWS = [
    "clarify.yml",
    "plan.yml",
    "implement.yml",
    "validate.yml",
    "review_autofix.yml",
    "orchestrate.yml",
    "orchestrate_poll.yml",
    "orchestrate_clarify_respond.yml",
]

# Multi-line "tokens used\n<digits>" emitted by Codex; we capture the number
# on the line following "tokens used" (case-insensitive). The single-line
# variant "tokens used 123,456" is also handled.
CODEX_TOKENS_RE = re.compile(
    r"tokens\s+used[^0-9\n]*\n?\s*([0-9][0-9,]*)", re.IGNORECASE
)

# OpenRouter structured usage line — strict field order matches the producer
# in scripts/review_run_reviewers.sh. Numeric fields tolerate "?" placeholders
# emitted when a value is unavailable.
_NUM = r"(\d+|\?)"
OPENROUTER_RE = re.compile(
    r"INFO:\s*openrouter\s+usage\s+"
    r"phase=(?P<phase>\S+)\s+"
    r"call=(?P<call>\S+)\s+"
    r"model=(?P<model>\S+)\s+"
    r"cache_enabled=\S+\s+"
    r"cache_breakpoint_enabled=\S+\s+"
    r"cache_breakpoint_fallback_retry=\S+\s+"
    r"prompt_tokens=(?P<pt>" + _NUM + r")\s+"
    r"completion_tokens=(?P<ct>" + _NUM + r")\s+"
    r"total_tokens=(?P<tt>" + _NUM + r")\s+"
    r"cache_creation_input_tokens=(?P<cw>" + _NUM + r")\s+"
    r"cache_read_input_tokens=(?P<cr>" + _NUM + r")"
)

SEMBLE_QUERY_RE = re.compile(r"(?:^|\s)SEMBLE_QUERY(?:\s|$)")
SEMBLE_FALLBACK_RE = re.compile(r"(?:^|\s)SEMBLE_FALLBACK(?:\s|$)")


def _extract_log_field(line: str, field: str) -> Optional[str]:
    m = re.search(rf"(?:^|\s){re.escape(field)}=([^\s]+)", line)
    return m.group(1) if m else None


def gh(args: List[str]) -> Optional[str]:
    """Run gh and return stdout, or None on failure (stderr to our stderr)."""
    try:
        r = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=True
        )
        return r.stdout
    except FileNotFoundError:
        sys.stderr.write("ERROR: `gh` not found in PATH. Install GitHub CLI.\n")
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(
            f"  gh failed ({' '.join(args)[:80]}...): "
            f"{(e.stderr or '').strip()[:200]}\n"
        )
        return None


def list_runs(repo: str, workflow: str, limit: int, since: Optional[str]) -> List[dict]:
    out = gh([
        "run", "list",
        "-R", repo,
        "--workflow", workflow,
        "--limit", str(limit),
        "--json",
        "databaseId,workflowName,createdAt,conclusion,event,headBranch,status",
    ])
    if not out:
        return []
    runs = json.loads(out)
    if since:
        runs = [r for r in runs if r.get("createdAt", "") >= since]
    return runs


def parse_log(log: str) -> dict:
    """Return aggregated token counts and per-call breakdown for one run."""
    out = {
        "codex_tokens_used": 0,
        "codex_calls": 0,
        "or_prompt_tokens": 0,
        "or_completion_tokens": 0,
        "or_total_tokens": 0,
        "or_cache_write_tokens": 0,
        "or_cache_read_tokens": 0,
        "or_calls": 0,
        "or_phases": defaultdict(lambda: defaultdict(int)),
        "semble_query_calls": 0,
        "semble_query_bytes": 0,
        "semble_fallbacks": 0,
        "semble_targets": defaultdict(lambda: defaultdict(int)),
    }

    for m in CODEX_TOKENS_RE.finditer(log):
        try:
            out["codex_tokens_used"] += int(m.group(1).replace(",", ""))
            out["codex_calls"] += 1
        except ValueError:
            continue

    def _to_int(v: str) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    for m in OPENROUTER_RE.finditer(log):
        pt = _to_int(m.group("pt"))
        ct = _to_int(m.group("ct"))
        tt = _to_int(m.group("tt"))
        cw = _to_int(m.group("cw"))
        cr = _to_int(m.group("cr"))
        out["or_prompt_tokens"] += pt
        out["or_completion_tokens"] += ct
        out["or_total_tokens"] += tt
        out["or_cache_write_tokens"] += cw
        out["or_cache_read_tokens"] += cr
        out["or_calls"] += 1
        phase = m.group("phase")
        out["or_phases"][phase]["prompt_tokens"] += pt
        out["or_phases"][phase]["completion_tokens"] += ct
        out["or_phases"][phase]["total_tokens"] += tt
        out["or_phases"][phase]["calls"] += 1

    for line in log.splitlines():
        if SEMBLE_QUERY_RE.search(line):
            target = _extract_log_field(line, "target") or "unknown"
            logged_bytes = _to_int(_extract_log_field(line, "bytes") or "0")
            out["semble_query_calls"] += 1
            out["semble_query_bytes"] += logged_bytes
            out["semble_targets"][target]["query_calls"] += 1
            out["semble_targets"][target]["bytes"] += logged_bytes
        elif SEMBLE_FALLBACK_RE.search(line):
            target = _extract_log_field(line, "target") or "unknown"
            out["semble_fallbacks"] += 1
            out["semble_targets"][target]["fallbacks"] += 1

    out["or_phases"] = {p: dict(v) for p, v in out["or_phases"].items()}
    out["semble_targets"] = {p: dict(v) for p, v in out["semble_targets"].items()}
    return out


def fmt(n: int) -> str:
    return f"{n:,}" if n else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--repo", default="shubhodeep1/coding-workflows",
                    help="owner/repo (default: %(default)s)")
    ap.add_argument("--limit", type=int, default=5,
                    help="runs per workflow (default: 5; logs are big — start small)")
    ap.add_argument("--workflows", default=",".join(DEFAULT_WORKFLOWS),
                    help="comma-separated workflow files (default: all AI workflows)")
    ap.add_argument("--since", help="ISO date YYYY-MM-DD; skip runs older than this")
    ap.add_argument("--json", default="cost_audit_report.json",
                    help="output JSON path (default: %(default)s)")
    args = ap.parse_args()

    workflows = [w.strip() for w in args.workflows.split(",") if w.strip()]
    per_wf: Dict[str, dict] = {}
    per_run: List[dict] = []

    for wf in workflows:
        sys.stderr.write(f"\n=== {wf} ===\n")
        runs = list_runs(args.repo, wf, args.limit, args.since)
        sys.stderr.write(f"  {len(runs)} run(s)\n")

        agg = {
            "run_count": len(runs),
            "runs_with_data": 0,
            "codex_tokens_used": 0,
            "codex_calls": 0,
            "or_prompt_tokens": 0,
            "or_completion_tokens": 0,
            "or_total_tokens": 0,
            "or_cache_write_tokens": 0,
            "or_cache_read_tokens": 0,
            "or_calls": 0,
            "or_phases": defaultdict(lambda: defaultdict(int)),
            "semble_query_calls": 0,
            "semble_query_bytes": 0,
            "semble_fallbacks": 0,
            "semble_targets": defaultdict(lambda: defaultdict(int)),
        }

        for i, r in enumerate(runs, 1):
            rid = r["databaseId"]
            sys.stderr.write(
                f"  [{i}/{len(runs)}] run {rid} {r.get('createdAt','')[:19]} "
                f"{r.get('conclusion') or r.get('status','?')} ... "
            )
            sys.stderr.flush()
            log = gh(["run", "view", str(rid), "-R", args.repo, "--log"])
            if log is None:
                sys.stderr.write("skip (log unavailable)\n")
                continue
            parsed = parse_log(log)
            if (
                parsed["codex_tokens_used"]
                or parsed["or_calls"]
                or parsed["semble_query_calls"]
                or parsed["semble_fallbacks"]
            ):
                agg["runs_with_data"] += 1
            for k in ("codex_tokens_used", "codex_calls", "or_prompt_tokens",
                      "or_completion_tokens", "or_total_tokens",
                      "or_cache_write_tokens", "or_cache_read_tokens",
                      "or_calls", "semble_query_calls",
                      "semble_query_bytes", "semble_fallbacks"):
                agg[k] += parsed[k]
            for phase, vals in parsed["or_phases"].items():
                for k, v in vals.items():
                    agg["or_phases"][phase][k] += v
            for target, vals in parsed["semble_targets"].items():
                for k, v in vals.items():
                    agg["semble_targets"][target][k] += v
            per_run.append({
                "workflow": wf,
                "run_id": rid,
                "created_at": r.get("createdAt"),
                "conclusion": r.get("conclusion"),
                "head_branch": r.get("headBranch"),
                **{k: parsed[k] for k in (
                    "codex_tokens_used", "codex_calls", "or_prompt_tokens",
                    "or_completion_tokens", "or_total_tokens",
                    "or_cache_write_tokens", "or_cache_read_tokens", "or_calls",
                    "semble_query_calls", "semble_query_bytes", "semble_fallbacks",
                )},
                "semble_targets": parsed["semble_targets"],
            })
            sys.stderr.write(
                f"codex={fmt(parsed['codex_tokens_used'])} "
                f"or_total={fmt(parsed['or_total_tokens'])} "
                f"or_calls={parsed['or_calls']} "
                f"semble_bytes={fmt(parsed['semble_query_bytes'])} "
                f"semble_fallbacks={fmt(parsed['semble_fallbacks'])}\n"
            )

        agg["or_phases"] = {p: dict(v) for p, v in agg["or_phases"].items()}
        agg["semble_targets"] = {p: dict(v) for p, v in agg["semble_targets"].items()}
        per_wf[wf] = agg

    # Markdown summary on stdout
    print("\n# Cost audit summary\n")
    print(f"- Repo: `{args.repo}`")
    print(f"- Window: last {args.limit} run(s) per workflow"
          + (f" since {args.since}" if args.since else ""))
    print()
    print("| Workflow | runs | with_data | codex_tokens | codex_calls | "
          "or_prompt | or_completion | or_total | or_cache_write | "
          "or_cache_read | or_calls |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for wf, a in per_wf.items():
        print(
            f"| {wf} | {a['run_count']} | {a['runs_with_data']} | "
            f"{fmt(a['codex_tokens_used'])} | {fmt(a['codex_calls'])} | "
            f"{fmt(a['or_prompt_tokens'])} | {fmt(a['or_completion_tokens'])} | "
            f"{fmt(a['or_total_tokens'])} | {fmt(a['or_cache_write_tokens'])} | "
            f"{fmt(a['or_cache_read_tokens'])} | {fmt(a['or_calls'])} |"
        )

    # OpenRouter phase breakdown (review_autofix only emits phases)
    or_workflows = [wf for wf, a in per_wf.items() if a["or_phases"]]
    if or_workflows:
        print("\n## OpenRouter phase breakdown\n")
        for wf in or_workflows:
            print(f"### {wf}\n")
            print("| phase | calls | prompt | completion | total |")
            print("|---|---:|---:|---:|---:|")
            phases = per_wf[wf]["or_phases"]
            for phase in sorted(phases, key=lambda p: -phases[p].get("total_tokens", 0)):
                v = phases[phase]
                print(f"| {phase} | {fmt(v.get('calls', 0))} | "
                      f"{fmt(v.get('prompt_tokens', 0))} | "
                      f"{fmt(v.get('completion_tokens', 0))} | "
                      f"{fmt(v.get('total_tokens', 0))} |")
            print()

    semble_workflows = [
        wf for wf, a in per_wf.items()
        if a["semble_query_calls"] or a["semble_fallbacks"]
    ]
    if semble_workflows:
        print("\n## Semble telemetry breakdown\n")
        print("| Workflow | query_calls | logged_bytes | fallbacks |")
        print("|---|---:|---:|---:|")
        for wf in semble_workflows:
            a = per_wf[wf]
            print(
                f"| {wf} | {fmt(a['semble_query_calls'])} | "
                f"{fmt(a['semble_query_bytes'])} | {fmt(a['semble_fallbacks'])} |"
            )

        print()
        for wf in semble_workflows:
            targets = per_wf[wf]["semble_targets"]
            if not targets:
                continue
            print(f"### {wf}\n")
            print("| target | query_calls | logged_bytes | fallbacks |")
            print("|---|---:|---:|---:|")
            ordered_targets = sorted(
                targets,
                key=lambda t: (
                    -targets[t].get("bytes", 0),
                    -targets[t].get("query_calls", 0),
                    -targets[t].get("fallbacks", 0),
                    t,
                ),
            )
            for target in ordered_targets:
                vals = targets[target]
                print(
                    f"| {target} | {fmt(vals.get('query_calls', 0))} | "
                    f"{fmt(vals.get('bytes', 0))} | "
                    f"{fmt(vals.get('fallbacks', 0))} |"
                )
            print()

    payload = {
        "repo": args.repo,
        "limit": args.limit,
        "since": args.since,
        "per_workflow": per_wf,
        "per_run": per_run,
    }
    with open(args.json, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    sys.stderr.write(f"\nWrote per-run JSON to {args.json}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
