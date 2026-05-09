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
  3. Semble retrieval telemetry ("SEMBLE_QUERY ... bytes=N ..." and
     "SEMBLE_FALLBACK ...") — emitted by scripts/install_semble.sh and
     scripts/semble_helpers.sh.

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

SEMBLE_FIELD_RE = re.compile(
    r"(?:^|\s)(?P<key>target|phase|chunks|bytes|ms|reason|detail|path|status|version|source)="
)


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


def _to_int(v: Optional[str]) -> int:
    try:
        return int((v or "").replace(",", ""))
    except (AttributeError, TypeError, ValueError):
        return 0


def _parse_semble_fields(payload: str) -> Dict[str, str]:
    """Parse key=value telemetry where values may contain spaces.

    Semble log lines keep a stable key order and may include human-readable
    targets such as `target=Implement Context`. We therefore slice from each
    recognised key marker to the next marker instead of splitting on
    whitespace, which would corrupt spaced target values.
    """
    matches = list(SEMBLE_FIELD_RE.finditer(payload))
    fields: Dict[str, str] = {}
    for i, match in enumerate(matches):
        value_start = match.end()
        value_end = matches[i + 1].start() if i + 1 < len(matches) else len(payload)
        value = payload[value_start:value_end].strip()
        if value:
            fields[match.group("key")] = value
    return fields


def _semble_bucket(fields: Dict[str, str]) -> str:
    """Normalise Semble scope, preferring phase=<...> over target=<...>."""
    phase = (fields.get("phase") or "").strip()
    if phase:
        return f"phase={phase}"
    target = (fields.get("target") or "").strip()
    if target:
        return f"target={target}"
    return "unscoped"


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
        "semble_query_bytes": 0,
        "semble_query_events": 0,
        "semble_fallbacks": 0,
        "semble_buckets": defaultdict(lambda: defaultdict(int)),
    }

    for m in CODEX_TOKENS_RE.finditer(log):
        try:
            out["codex_tokens_used"] += int(m.group(1).replace(",", ""))
            out["codex_calls"] += 1
        except ValueError:
            continue

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
        if "SEMBLE_QUERY " in line:
            payload = line.split("SEMBLE_QUERY ", 1)[1].strip()
            fields = _parse_semble_fields(payload)
            if "bytes" not in fields:
                continue
            try:
                query_bytes = int(fields["bytes"].replace(",", ""))
            except (AttributeError, TypeError, ValueError):
                continue
            bucket = _semble_bucket(fields)
            out["semble_query_bytes"] += query_bytes
            out["semble_query_events"] += 1
            out["semble_buckets"][bucket]["query_bytes"] += query_bytes
            out["semble_buckets"][bucket]["query_events"] += 1
            continue

        if "SEMBLE_FALLBACK " in line:
            payload = line.split("SEMBLE_FALLBACK ", 1)[1].strip()
            fields = _parse_semble_fields(payload)
            if not any(fields.get(k) for k in ("target", "phase", "reason")):
                continue
            bucket = _semble_bucket(fields)
            out["semble_fallbacks"] += 1
            out["semble_buckets"][bucket]["fallbacks"] += 1

    out["or_phases"] = {p: dict(v) for p, v in out["or_phases"].items()}
    out["semble_buckets"] = {p: dict(v) for p, v in out["semble_buckets"].items()}
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
            "semble_query_bytes": 0,
            "semble_query_events": 0,
            "semble_fallbacks": 0,
            "semble_buckets": defaultdict(lambda: defaultdict(int)),
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
                or parsed["semble_query_events"]
                or parsed["semble_fallbacks"]
            ):
                agg["runs_with_data"] += 1
            for k in ("codex_tokens_used", "codex_calls", "or_prompt_tokens",
                      "or_completion_tokens", "or_total_tokens",
                      "or_cache_write_tokens", "or_cache_read_tokens",
                      "or_calls", "semble_query_bytes",
                      "semble_query_events", "semble_fallbacks"):
                agg[k] += parsed[k]
            for phase, vals in parsed["or_phases"].items():
                for k, v in vals.items():
                    agg["or_phases"][phase][k] += v
            for bucket, vals in parsed["semble_buckets"].items():
                for k, v in vals.items():
                    agg["semble_buckets"][bucket][k] += v
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
                    "semble_query_bytes", "semble_query_events",
                    "semble_fallbacks", "semble_buckets",
                )},
            })
            sys.stderr.write(
                f"codex={fmt(parsed['codex_tokens_used'])} "
                f"or_total={fmt(parsed['or_total_tokens'])} "
                f"or_calls={parsed['or_calls']} "
                f"semble_bytes={fmt(parsed['semble_query_bytes'])} "
                f"semble_fallbacks={fmt(parsed['semble_fallbacks'])}\n"
            )

        agg["or_phases"] = {p: dict(v) for p, v in agg["or_phases"].items()}
        agg["semble_buckets"] = {p: dict(v) for p, v in agg["semble_buckets"].items()}
        per_wf[wf] = agg

    # Markdown summary on stdout
    print("\n# Cost audit summary\n")
    print(f"- Repo: `{args.repo}`")
    print(f"- Window: last {args.limit} run(s) per workflow"
          + (f" since {args.since}" if args.since else ""))
    print()
    print("| Workflow | runs | with_data | codex_tokens | codex_calls | "
          "or_prompt | or_completion | or_total | or_cache_write | "
          "or_cache_read | or_calls | semble_bytes | semble_queries | "
          "semble_fallbacks |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for wf, a in per_wf.items():
        print(
            f"| {wf} | {a['run_count']} | {a['runs_with_data']} | "
            f"{fmt(a['codex_tokens_used'])} | {fmt(a['codex_calls'])} | "
            f"{fmt(a['or_prompt_tokens'])} | {fmt(a['or_completion_tokens'])} | "
            f"{fmt(a['or_total_tokens'])} | {fmt(a['or_cache_write_tokens'])} | "
            f"{fmt(a['or_cache_read_tokens'])} | {fmt(a['or_calls'])} | "
            f"{fmt(a['semble_query_bytes'])} | {fmt(a['semble_query_events'])} | "
            f"{fmt(a['semble_fallbacks'])} |"
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

    semble_workflows = [wf for wf, a in per_wf.items() if a["semble_buckets"]]
    if semble_workflows:
        print("\n## Semble retrieval breakdown\n")
        for wf in semble_workflows:
            print(f"### {wf}\n")
            print("| bucket | query_events | query_bytes | fallbacks |")
            print("|---|---:|---:|---:|")
            buckets = per_wf[wf]["semble_buckets"]
            for bucket in sorted(
                buckets,
                key=lambda b: (
                    -buckets[b].get("query_bytes", 0),
                    -buckets[b].get("fallbacks", 0),
                    b,
                ),
            ):
                v = buckets[bucket]
                print(
                    f"| {bucket} | {fmt(v.get('query_events', 0))} | "
                    f"{fmt(v.get('query_bytes', 0))} | {fmt(v.get('fallbacks', 0))} |"
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
