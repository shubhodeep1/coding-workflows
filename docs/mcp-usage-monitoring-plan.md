# Plan: MCP Usage Monitoring Parity (Serena + Generic MCP)

> **Status**: APPROVED — ready for AI orchestrator implementation.
> All open questions (Q1–Q4 in the prior PROPOSED revision) are answered;
> the §6 naming-immutability implication for `response_bytes` vs `bytes`
> is also recorded as a stated decision.
> **Owner**: solo developer (sole user of the workflows in this repo).
> **Goal**: bring Serena (and any future MCP server) to parity with
> Semble in the closed-loop telemetry → workflow-log-analysis →
> recommendation → consumer-repo-propagation pipeline, *including* the
> `summarize_unselected_runs.py` summarizer pass that the prior plan
> revision had not noticed as a gap.
> **Scope**: `scripts/cost_audit.py`,
> `prompts/mode-workflow-analysis.txt`,
> `scripts/summarize_unselected_runs.py` (system prompt only), and one
> new test module. No changes to the existing emitters
> (`setup_serena.sh`, `serena_stats_emit.py`, `mcp_handshake_probe.py`)
> or to `agents.md` (the three Serena prefixes are already declared
> contractual at `agents.md:144-146`).

---

## 1. Background

### 1.1 Semble — current end-to-end loop

Semble is the only MCP with full parse-and-analyse coverage today:

| Stage | File / line | What happens |
|---|---|---|
| Emit (query) | every `SEMBLE_QUERY target=… bytes=N` site (Semble helpers) | one line per query, target-tagged. |
| Emit (fallback) | every `SEMBLE_FALLBACK target=… reason=…` site | one line per fallback. |
| Parse (operator audit) | `scripts/cost_audit.py:74-75`, `cost_audit.py:167-178` | `SEMBLE_QUERY_RE` / `SEMBLE_FALLBACK_RE` accumulate counts, bytes, fallbacks, and a per-target breakdown. |
| Aggregate (operator audit) | `cost_audit.py:130-133`, `cost_audit.py:220-226` | per-run + per-workflow rollups, JSON + Markdown. |
| Surface to analyst | `prompts/mode-workflow-analysis.txt:8` (input list), `:46` (Cost), `:47` (Reliability), `:54` (Metrics Appendix), `:62` (first-class-evidence rule). |
| Feed semantic context | `prompts/mode-workflow-analysis.txt:3` (`{{SEMBLE_PREFETCH}}`). |
| Propagate fixes | analysis recommendations → issues → PRs → stable release → `repository_dispatch` to the consumer repos in `.github/ai/consumer_repos.json`. |

Note: `cost_audit.py` is a **standalone operator audit script**; it is
not invoked from any workflow under `.github/workflows/`. The actual
analysis pipeline is
`collect_workflow_logs.py` → `summarize_unselected_runs.py`
→ `analyze_workflow_logs.py` → `mode-workflow-analysis.txt`. The
plan touches both surfaces because each serves a distinct consumer
(operator vs analyst) and Serena needs parity in both.

### 1.2 Serena — what exists today

Serena is **emit-complete but parse-blind**. The contractual prefixes
in `agents.md:144-146` are:

- `SERENA_QUERY target=… tool=… calls=N response_bytes=N ms=N`
- `SERENA_FALLBACK target=… [phase=…] reason=…`
- `SERENA_PROBE target=… result=ok|failed|skipped [reason=…] [server_name=…] [server_version=…]`

Emission sites:

| Prefix | Emitter | Workflows wired |
|---|---|---|
| `SERENA_QUERY` | `scripts/serena_stats_emit.py:135` (per-tool rollup of Codex logs) | `implement.yml:4131-4155`, `validate.yml:853-872`, `review_autofix.yml:5561-5583` |
| `SERENA_FALLBACK` | `scripts/setup_serena.sh:50-56`, `scripts/validate_process.sh:555`, inline in `implement.yml:1008/1014`, `review_autofix.yml:3220/3226` | same workflows + bootstrap |
| `SERENA_PROBE` | `scripts/mcp_handshake_probe.py:85-100` | run wherever the probe is invoked (implement / validate / review_autofix) |

What's missing:

1. **`cost_audit.py` ignores all three prefixes.** `parse_log()` only
   matches Semble. Serena bytes, fallbacks, probe failures, and the
   per-tool distribution never reach the per-run JSON or the Markdown
   summary.
2. **`mode-workflow-analysis.txt` never names Serena.** Line 8's input
   list, line 46's Cost section, line 47's Reliability section, line 54's
   Metrics Appendix, and line 62's first-class-evidence rule are all
   Semble-only. The analyst has no instruction to weight Serena
   evidence even if it appeared in a deep-dive excerpt.
3. **`summarize_unselected_runs.py` system prompt ignores MCP telemetry
   entirely.** The required-signals list at lines 54–73 names
   `AI_MEMORY_TELEMETRY` as a verbatim-preserve signal but mentions
   neither `SEMBLE_*` nor `SERENA_*`. For any run not in the deep-dive
   set, the analyst's `log_summary` may drop both Semble and Serena
   evidence. This is a long-standing Semble gap that we close in the
   same pass.
4. **No generic `<NAME>_QUERY` capture.** If a third MCP lands
   (e.g., `context7`, `playwright`), the same patch has to be replicated
   end-to-end. The contract in `agents.md` is already per-server, not
   generic.
5. **`SERENA_PROBE` is unused downstream.** Handshake-failure rate is
   a useful reliability signal ("how often does Serena fail to come up
   in this workflow?") distinct from runtime fallback rate. Nothing
   aggregates it today.

---

## 2. Decisions (answers to prior Q-PLAN-* and §6 implication)

| ID | Question | Decision | Rationale |
|---|---|---|---|
| D1 | Pipeline scope — which downstream surfaces does this PR touch? | `cost_audit.py` + `mode-workflow-analysis.txt` + `summarize_unselected_runs.py` system prompt + tests. | Closes the actual closed-loop gap. The summarizer pass is the only thing that turns unselected runs into analyst-visible evidence today; leaving it Serena-blind defeats the parity goal. |
| D2 | Include the generic any-MCP catch-all regex now, or defer? | Include now. Named SEMBLE/SERENA matchers run first; generic regex captures any other `<NAME>_QUERY` / `<NAME>_FALLBACK` / `<NAME>_PROBE`. | Marginal extra code; future MCPs become visible without a patch. The skip-if-known guard keeps SEMBLE/SERENA single-counted. |
| D3 | `SERENA_PROBE` placement in the analyst prompt's Metrics Appendix. | Single "MCP availability" row in the Metrics Appendix showing `probe_ok / probe_failed / probe_skipped` per target per workflow, AND a short Reliability-section sentence tying probe failures to fallback patterns. | Mirrors how Semble fallbacks are surfaced; the dual placement is cheap because both edits are sentence-level. |
| D4 | Generic catch-all data model in `cost_audit.py`. | Nested per-server structure: `other_mcp: dict[server → {query_calls, query_bytes, query_response_bytes, fallbacks, probe_ok, probe_failed, probe_skipped}]`, mirroring the Semble/Serena shape. | Symmetric structure makes a future generic analysis run treat any new server identically, with zero shape-translation. |
| D5 (§6) | Field naming for `response_bytes` (Serena) vs `bytes` (Semble). | Preserve emit-side naming. JSON field for Serena is `serena_query_response_bytes`; Semble keeps `semble_query_bytes`. Both naming conventions are reflected in the `other_mcp` shape so any third MCP can use either. | §6 (naming immutability) forbids renaming or normalising either field. The analyst can compare across systems by reading both names. |

---

## 3. Concrete change inventory

### 3.1 `scripts/cost_audit.py`

#### 3.1.1 New regex constants (insert near `cost_audit.py:74-75`)

```python
SERENA_QUERY_RE = re.compile(r"(?:^|\s)SERENA_QUERY(?:\s|$)")
SERENA_FALLBACK_RE = re.compile(r"(?:^|\s)SERENA_FALLBACK(?:\s|$)")
SERENA_PROBE_RE = re.compile(r"(?:^|\s)SERENA_PROBE(?:\s|$)")

# Generic catch-all for any other MCP server. Named SEMBLE/SERENA
# matchers run first; this captures NAME_QUERY / NAME_FALLBACK /
# NAME_PROBE for any other `[A-Z][A-Z0-9_]*` prefix. The skip-if-known
# guard in parse_log() prevents double-counting.
MCP_QUERY_GENERIC_RE = re.compile(
    r"(?:^|\s)(?P<server>[A-Z][A-Z0-9_]*)_QUERY(?:\s|$)"
)
MCP_FALLBACK_GENERIC_RE = re.compile(
    r"(?:^|\s)(?P<server>[A-Z][A-Z0-9_]*)_FALLBACK(?:\s|$)"
)
MCP_PROBE_GENERIC_RE = re.compile(
    r"(?:^|\s)(?P<server>[A-Z][A-Z0-9_]*)_PROBE(?:\s|$)"
)

KNOWN_MCP_SERVERS = frozenset({"SEMBLE", "SERENA"})
```

#### 3.1.2 New accumulator fields in `parse_log()` (extend `cost_audit.py:120-134`)

Add alongside the existing `semble_*` block:

```python
"serena_query_calls": 0,
"serena_query_response_bytes": 0,    # emit-side name preserved
"serena_query_tool_calls": 0,        # sum of `calls=` across SERENA_QUERY lines
"serena_query_ms": 0,                # sum of `ms=`
"serena_fallbacks": 0,
"serena_probe_ok": 0,
"serena_probe_failed": 0,
"serena_probe_skipped": 0,
"serena_targets": defaultdict(lambda: defaultdict(int)),
# per-target breakdown:
#   {target → {query_calls, response_bytes, tool_calls, ms,
#              fallbacks, probe_ok, probe_failed, probe_skipped}}
"serena_tools": defaultdict(lambda: defaultdict(int)),
# per-tool breakdown:
#   {tool → {calls, response_bytes, ms}}
"other_mcp": defaultdict(lambda: defaultdict(int)),
# generic catch-all, nested:
#   {server → {query_calls, query_bytes, query_response_bytes,
#              fallbacks, probe_ok, probe_failed, probe_skipped}}
```

#### 3.1.3 Per-line parsing logic (extend the loop at `cost_audit.py:167-178`)

The existing `if/elif` chain handles SEMBLE first; add an `elif` cascade
for SERENA prefixes and a final `else` branch for the generic regexes.
Use the existing `_extract_log_field()` helper. Cascade order is
**strict**: a line that matches a named matcher must not fall through
to the generic one.

```python
for line in log.splitlines():
    if SEMBLE_QUERY_RE.search(line):
        # ... existing SEMBLE_QUERY handling ...
    elif SEMBLE_FALLBACK_RE.search(line):
        # ... existing SEMBLE_FALLBACK handling ...
    elif SERENA_QUERY_RE.search(line):
        target = _extract_log_field(line, "target") or "unknown"
        tool = _extract_log_field(line, "tool") or "unknown"
        rbytes = _to_int(_extract_log_field(line, "response_bytes") or "0")
        calls_inner = _to_int(_extract_log_field(line, "calls") or "0")
        ms = _to_int(_extract_log_field(line, "ms") or "0")
        out["serena_query_calls"] += 1
        out["serena_query_response_bytes"] += rbytes
        out["serena_query_tool_calls"] += calls_inner
        out["serena_query_ms"] += ms
        out["serena_targets"][target]["query_calls"] += 1
        out["serena_targets"][target]["response_bytes"] += rbytes
        out["serena_targets"][target]["tool_calls"] += calls_inner
        out["serena_targets"][target]["ms"] += ms
        out["serena_tools"][tool]["calls"] += 1
        out["serena_tools"][tool]["response_bytes"] += rbytes
        out["serena_tools"][tool]["ms"] += ms
    elif SERENA_FALLBACK_RE.search(line):
        target = _extract_log_field(line, "target") or "unknown"
        out["serena_fallbacks"] += 1
        out["serena_targets"][target]["fallbacks"] += 1
    elif SERENA_PROBE_RE.search(line):
        target = _extract_log_field(line, "target") or "unknown"
        result = (_extract_log_field(line, "result") or "unknown").lower()
        bucket = result if result in ("ok", "failed", "skipped") else "skipped"
        out[f"serena_probe_{bucket}"] += 1
        out["serena_targets"][target][f"probe_{bucket}"] += 1
    else:
        # Generic catch-all. Skip if the server name is in the
        # known-named set so SEMBLE/SERENA cannot fall through here.
        for regex, kind in (
            (MCP_QUERY_GENERIC_RE, "query"),
            (MCP_FALLBACK_GENERIC_RE, "fallback"),
            (MCP_PROBE_GENERIC_RE, "probe"),
        ):
            m = regex.search(line)
            if not m:
                continue
            server = m.group("server")
            if server in KNOWN_MCP_SERVERS:
                continue
            if kind == "query":
                out["other_mcp"][server]["query_calls"] += 1
                bval = _extract_log_field(line, "bytes")
                if bval is not None:
                    out["other_mcp"][server]["query_bytes"] += _to_int(bval)
                rbval = _extract_log_field(line, "response_bytes")
                if rbval is not None:
                    out["other_mcp"][server]["query_response_bytes"] += _to_int(rbval)
            elif kind == "fallback":
                out["other_mcp"][server]["fallbacks"] += 1
            elif kind == "probe":
                result = (_extract_log_field(line, "result") or "unknown").lower()
                bucket = result if result in ("ok", "failed", "skipped") else "skipped"
                out["other_mcp"][server][f"probe_{bucket}"] += 1
            break   # one regex match per line is enough
```

At the bottom of `parse_log()`, convert the new `defaultdict`s to plain
`dict`s (mirror the existing conversion at `cost_audit.py:180-181`):

```python
out["serena_targets"] = {p: dict(v) for p, v in out["serena_targets"].items()}
out["serena_tools"] = {p: dict(v) for p, v in out["serena_tools"].items()}
out["other_mcp"] = {s: dict(v) for s, v in out["other_mcp"].items()}
```

#### 3.1.4 `main()` aggregation (extend `cost_audit.py:211-227` and `:248-259`)

Extend the `agg` initialiser to add the same set of `serena_*` and
`other_mcp` keys with `defaultdict(lambda: defaultdict(int))` semantics
for the nested ones. The flat counters added to the `for k in (...)`
sum loop at `cost_audit.py:248-253` are:

```
"serena_query_calls", "serena_query_response_bytes",
"serena_query_tool_calls", "serena_query_ms",
"serena_fallbacks",
"serena_probe_ok", "serena_probe_failed", "serena_probe_skipped",
```

The `runs_with_data` test at `cost_audit.py:241-246` extends to also
treat Serena and any `other_mcp` signal as "data":

```python
if (
    parsed["codex_tokens_used"]
    or parsed["or_calls"]
    or parsed["semble_query_calls"]
    or parsed["semble_fallbacks"]
    or parsed["serena_query_calls"]
    or parsed["serena_fallbacks"]
    or any(parsed["serena_probe_ok"]
           for _ in (0,))   # always-false short-circuit safe
    or parsed["serena_probe_failed"]
    or parsed["serena_probe_skipped"]
    or parsed["other_mcp"]
):
```

The nested-dict merge loops (existing `or_phases` / `semble_targets`
pattern at `cost_audit.py:254-259`) extend to `serena_targets`,
`serena_tools`, and `other_mcp`. At the end of the per-workflow loop,
flatten the new nested defaultdicts before assigning to `per_wf[wf]`.

The per-run JSON payload (`cost_audit.py:260-273`) extends to include
the same flat `serena_*` counters and the nested `serena_targets`,
`serena_tools`, `other_mcp` dicts.

#### 3.1.5 Markdown output (new sections after the existing Semble block at `cost_audit.py:322-361`)

**`## Serena telemetry breakdown`** — gated on `serena_query_calls or
serena_fallbacks or any serena_probe_* > 0`. Three tables:

1. Per-workflow rollup:
   ```
   | Workflow | query_calls | response_bytes | tool_calls | ms | fallbacks | probe_ok | probe_failed | probe_skipped |
   ```
2. Per-target rollup (one subsection per workflow), columns:
   ```
   | target | query_calls | response_bytes | tool_calls | ms | fallbacks | probe_ok | probe_failed | probe_skipped |
   ```
3. Per-tool rollup (one subsection per workflow), columns:
   ```
   | tool | calls | response_bytes | ms |
   ```

Sort each table by `response_bytes` desc, then `calls` / `query_calls`
desc, then name asc (mirrors the existing Semble sort at
`cost_audit.py:345-353`).

**`## Other MCP servers observed`** — only emitted when at least one
workflow has a non-empty `other_mcp`. Columns:

```
| Workflow | Server | query_calls | query_bytes | query_response_bytes | fallbacks | probe_ok | probe_failed | probe_skipped |
```

This section is deliberately compact: it flags the existence of an
unknown MCP server so the operator notices the gap. It does not
attempt to interpret per-tool / per-target distributions for unknown
servers.

#### 3.1.6 Stderr trace line (extend `cost_audit.py:274-280`)

Append `serena_calls`, `serena_fallbacks`, and an `other_mcp` count to
the per-run progress line so operators can spot Serena traffic without
re-reading the JSON:

```python
sys.stderr.write(
    f"codex={fmt(parsed['codex_tokens_used'])} "
    f"or_total={fmt(parsed['or_total_tokens'])} "
    f"or_calls={parsed['or_calls']} "
    f"semble_bytes={fmt(parsed['semble_query_bytes'])} "
    f"semble_fallbacks={fmt(parsed['semble_fallbacks'])} "
    f"serena_calls={fmt(parsed['serena_query_calls'])} "
    f"serena_fallbacks={fmt(parsed['serena_fallbacks'])} "
    f"serena_probe_failed={fmt(parsed['serena_probe_failed'])} "
    f"other_mcp={len(parsed['other_mcp'])}\n"
)
```

### 3.2 `prompts/mode-workflow-analysis.txt`

All edits are sentence-level additions to existing sections. No new
section headings.

- **Line 8** — extend the input bullet:
  > `Semble query/fallback telemetry (\`SEMBLE_QUERY\`,
  > \`SEMBLE_FALLBACK\`) and Serena query/fallback/probe telemetry
  > (\`SERENA_QUERY\`, \`SERENA_FALLBACK\`, \`SERENA_PROBE\`) including
  > logged prompt bytes where available`

- **Line 46 (Cost Optimizations)** — append a Serena clause to the
  existing Semble sentence:
  > When `SERENA_QUERY` lines are present, comment on whether Serena's
  > symbolic-search tool calls are replacing whole-file reads in the
  > codex transcript; report the top tools by `response_bytes` and
  > `calls`, and flag tools whose `response_bytes/calls` ratio is
  > consistent with low-value noise.

- **Line 47 (Reliability Improvements)** — append:
  > When `SERENA_FALLBACK` or `SERENA_PROBE` lines are present,
  > distinguish handshake failures (`SERENA_PROBE result=failed`) from
  > runtime fallbacks (`SERENA_FALLBACK`). Report failed-probe rate by
  > target and quantify whether fallbacks correlate with prior probe
  > failures (masked broken rollout) or are isolated (healthy
  > fail-open).

- **Line 54 (Metrics Appendix minimum-set)** — extend the existing
  sentence:
  > …Semble query/fallback totals with logged-byte counts/rates,
  > Serena query/fallback/probe totals with response-byte and per-tool
  > breakdowns when present, and a per-target MCP availability row
  > showing `probe_ok / probe_failed / probe_skipped` counts.

- **Line 62 (first-class-evidence rule)** — extend:
  > If `SEMBLE_QUERY`, `SEMBLE_FALLBACK`, `SERENA_QUERY`,
  > `SERENA_FALLBACK`, or `SERENA_PROBE` appear in deep-dive excerpts or
  > `log_summary`, treat them as first-class evidence: cite the
  > workflow/job/step and target, quantify logged bytes and
  > fallback/probe counts, and avoid assuming a universal `phase=`
  > field when the logs only provide `target=`.

- **New rule (immediately after the extended line 62)** — generic
  catch-all guidance:
  > If a line matches `<NAME>_QUERY`, `<NAME>_FALLBACK`, or
  > `<NAME>_PROBE` for a name other than `SEMBLE` or `SERENA`, treat it
  > as an unknown MCP server's telemetry: count occurrences per server,
  > surface the names under the Metrics Appendix as "Other MCP servers
  > observed", and flag the gap in analyst coverage rather than
  > analysing it as one of the known systems.

### 3.3 `scripts/summarize_unselected_runs.py`

Extend the `SYSTEM_PROMPT` constant at `summarize_unselected_runs.py:54-73`
so the gpt-5.4-mini summarizer retains MCP telemetry in `log_summary`
entries.

**Edit target**: the "Required signals" block currently lists
`AI_MEMORY_TELEMETRY` as a verbatim-preserve signal but no MCP prefix.
Add a new bullet immediately after the `AI_MEMORY_TELEMETRY` bullet:

> - `SEMBLE_*` / `SERENA_*` lines (preserve verbatim, max 3 per prefix
>   family; include `target=`, `bytes=`/`response_bytes=`, `reason=`,
>   `result=`, and `tool=` values when present)

No other changes to this file. The summarizer model, token budget, and
fail-open behaviour are unaffected.

### 3.4 `tests/test_cost_audit_serena_metrics.py` (new file)

Model on `tests/test_cost_audit_semble_metrics.py`. Required test
cases:

1. **Multi-target, multi-tool Serena query aggregation.**
   Feed a log with three `SERENA_QUERY` lines (two targets, two tools);
   assert per-target and per-tool totals match expected sums for
   `query_calls`, `response_bytes`, `tool_calls` (sum of `calls=`),
   and `ms`.

2. **`SERENA_FALLBACK` per-target aggregation.**
   Mix `SERENA_FALLBACK` lines (with and without optional `phase=`)
   across two targets; assert `serena_fallbacks` count and
   `serena_targets[<t>]["fallbacks"]`.

3. **`SERENA_PROBE` bucketing.**
   Feed `SERENA_PROBE result=ok`, `result=failed`, `result=skipped`,
   and a malformed `result=garbage` (which must bucket into
   `probe_skipped`); assert per-target and global probe counters.

4. **Mixed Semble + Serena + Codex + OpenRouter run.**
   A single fixture combining all four signal types; assert each
   accumulator is independent (no double-counting, no cross-contamination).

5. **Generic catch-all routes a third-MCP line correctly.**
   Feed `FOO_QUERY target=alpha bytes=100`,
   `FOO_FALLBACK target=alpha reason=timeout`,
   `FOO_PROBE target=alpha result=failed`;
   assert `other_mcp["FOO"]` contains the expected nested counters
   (`query_calls`, `query_bytes`, `fallbacks`, `probe_failed`).

6. **Known-server skip guard.**
   Feed a `SEMBLE_QUERY` and a `SERENA_QUERY` line; assert
   `other_mcp` is empty (neither falls into the catch-all).

7. **Fail-open on partial lines.**
   `SERENA_QUERY` without `response_bytes=`, without `tool=`, without
   `target=` — none of these crash; missing fields default to 0 or
   `"unknown"`.

8. **Single-line containing multiple `_QUERY` substrings stays bounded.**
   A pathological line like
   `SERENA_QUERY target=x ... FOO_QUERY target=y`
   should match SERENA_QUERY only (since named matchers run first and
   the `break` after the first generic-regex hit prevents double-counting
   inside the same line); assert `other_mcp["FOO"]` is **not** populated
   for that line.

Wire the file to follow the `if __name__ == "__main__":` runner pattern
of `test_cost_audit_semble_metrics.py` so it executes the same way.

### 3.5 Verified to need no change

- `agents.md` — the three Serena prefixes are already contractual at
  `agents.md:144-146`.
- `scripts/setup_serena.sh`, `scripts/serena_stats_emit.py`,
  `scripts/mcp_handshake_probe.py` — emitters are correct.
- Workflow YAMLs (`implement.yml`, `validate.yml`, `review_autofix.yml`)
  — already invoke the emitters at the call-sites listed in §1.2.
- `workflow-templates/` — consumer repos pick up the new parse +
  analyst behaviour automatically when the workflow-log-analysis
  workflow runs next; the relevant scripts live in this repo and
  are not vendored.
- `scripts/collect_workflow_logs.py` — `extract_log_excerpts` (line
  654) has no line-level scoring to extend; it truncates each step's
  first 4 KB. SERENA_* lines survive on the same terms Semble lines do
  today. Truncation-vs-evidence is a shared risk and out of scope for
  this PR.
- `scripts/analyze_workflow_logs.py` and the `summary.json` written
  by `collect_workflow_logs.py:1483` — neither carries per-run
  Semble counts today; they are pure run-metadata aggregates.
  Extending them would be a separate piece of work and is **not**
  in scope.

---

## 4. Acceptance criteria

The implementation pass is complete when:

1. **Parser parity.** `parse_log()` returns Serena counters that match
   the Semble counters in structure (flat totals + nested per-target
   dict + new per-tool dict + new probe buckets), preserves emit-side
   naming (`response_bytes`, not `bytes` for Serena), and routes any
   non-SEMBLE/non-SERENA `<NAME>_*` lines into `other_mcp[server]`.
2. **Markdown output.** `cost_audit.py` emits a
   `## Serena telemetry breakdown` section with the three tables in
   §3.1.5 when Serena data is present, and an
   `## Other MCP servers observed` table when any catch-all server
   was seen. The existing Semble section is unchanged byte-for-byte
   when only Semble data is present.
3. **JSON payload.** Each per-run entry in
   `cost_audit_report.json` carries the new `serena_*` flat fields,
   the nested `serena_targets`, `serena_tools`, and (when non-empty)
   `other_mcp` blocks.
4. **Analyst prompt.** `mode-workflow-analysis.txt` carries the six
   edits in §3.2. The five Semble-mentioning lines all now also name
   Serena; one new generic-catch-all rule follows the first-class-
   evidence rule.
5. **Summarizer prompt.** `summarize_unselected_runs.py`'s
   `SYSTEM_PROMPT` lists `SEMBLE_*` / `SERENA_*` as verbatim-preserve
   signals, immediately after the `AI_MEMORY_TELEMETRY` bullet.
6. **Tests pass.** `python3 tests/test_cost_audit_serena_metrics.py`
   exits 0 with all eight test cases reporting PASS. Existing
   Semble tests
   (`test_cost_audit_semble_metrics.py`,
   `test_implement_semble_contract.py`,
   `test_validate_semble_contract.py`,
   `test_review_semble_contract.py`) all still pass with no
   modification.
7. **No emitter or workflow YAML diff.** `git diff --stat` shows
   changes confined to the four files listed in §3 plus the new test
   module.
8. **No `agents.md` diff.** The contractual prefix block is untouched.

---

## 5. Risks and rollback

- **Risk: regex over-match.** A literal `_QUERY` substring inside a
  Codex prompt or a quoted log line could trip the generic catch-all.
  Mitigation: the `(?:^|\s)[A-Z][A-Z0-9_]*_QUERY(?:\s|$)` pattern
  anchors on whitespace boundaries and an all-caps prefix, matching
  the existing Semble pattern's hardening.
- **Risk: regex double-count.** A SEMBLE_QUERY or SERENA_QUERY line
  also matches the generic regex. Mitigation: the `if/elif/else`
  cascade in §3.1.3 routes named matches first and uses
  `KNOWN_MCP_SERVERS` as a belt-and-suspenders guard in the catch-all
  branch.
- **Risk: `response_bytes` field-name mismatch.** Already covered by
  D5 — preserve emit-side naming, document the asymmetry in the
  generic `other_mcp` shape (which carries *both* `query_bytes` and
  `query_response_bytes` slots so either emit style is captured).
- **Risk: summarizer prompt drift.** Adding a new required-signal
  bullet could push some runs over the 12-bullet cap at
  `summarize_unselected_runs.py:69`. Mitigation: the new bullet
  caps itself to "max 3 per prefix family" and reads as a single
  bullet to the model. No change to the 12-bullet output limit.
- **Risk: analyst prompt longer / noisier.** Edits are sentence-level
  additions to existing sections, not new sections. No new section
  heading.
- **Rollback path.** All changes are additive: new regexes, new
  fields, new JSON keys, new prompt sentences, new test module.
  Reverting the diff restores prior behaviour exactly. No DB / index
  / contract changes (so §10 is not engaged).

---

## 6. Work breakdown (line-item checklist for the orchestrator)

- [ ] `scripts/cost_audit.py` — apply §3.1.1 through §3.1.6 in order.
- [ ] `prompts/mode-workflow-analysis.txt` — apply the six edits in §3.2
      (5 sentence appends + 1 new rule).
- [ ] `scripts/summarize_unselected_runs.py` — apply the single
      system-prompt edit in §3.3.
- [ ] `tests/test_cost_audit_serena_metrics.py` — create the file with
      the eight test cases in §3.4.
- [ ] Run `python3 tests/test_cost_audit_serena_metrics.py` and the
      four existing Semble test modules; all must pass.
- [ ] Commit changes on the working branch with one commit per surface
      (cost_audit, prompts, summarizer, tests) so review can isolate
      each.
- [ ] Push the working branch and open a PR. PR description must
      cite §2 (Decisions table) and §4 (Acceptance criteria); reviewers
      should be able to tick each acceptance row from the diff alone.

Estimated effort: small. Three source files + one new test file. No
DB / index / contract changes. No emitter or workflow YAML changes.
