# Plan: MCP Usage Monitoring Parity (Serena + Generic MCP)

> **Status**: PROPOSED — no code changes yet. Awaiting decisions on
> Q-PLAN-A, Q-PLAN-B, Q-PLAN-C below before any implementation pass.
> **Owner**: solo developer (sole user of the workflows in this repo).
> **Goal**: bring Serena (and any future MCP server) to parity with
> Semble in the closed-loop telemetry → workflow-log-analysis →
> recommendation → consumer-repo-propagation pipeline.
> **Scope**: `scripts/cost_audit.py`, `prompts/mode-workflow-analysis.txt`,
> tests, and (optionally) `scripts/collect_workflow_logs.py` /
> `scripts/analyze_workflow_logs.py` if their `summary.json` surface
> needs new fields. No changes to the existing emitters
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
| Parse | `scripts/cost_audit.py:74-75`, `cost_audit.py:167-178` | `SEMBLE_QUERY_RE` / `SEMBLE_FALLBACK_RE` accumulate counts, bytes, fallbacks, and a per-target breakdown. |
| Aggregate | `cost_audit.py:130-133`, `cost_audit.py:220-226` | per-run + per-workflow rollups, JSON + Markdown. |
| Surface to analyst | `prompts/mode-workflow-analysis.txt:8` (input list), `:46` (Cost), `:47` (Reliability), `:54` (Metrics Appendix), `:62` (first-class-evidence rule). |
| Feed semantic context | `prompts/mode-workflow-analysis.txt:3` (`{{SEMBLE_PREFETCH}}`). |
| Propagate fixes | analysis recommendations → issues → PRs → stable release → `repository_dispatch` to the 11 repos in `.github/ai/consumer_repos.json`. |

### 1.2 Serena — what exists today

Serena is **emit-complete but parse-blind**. The contractual prefixes
in `agents.md:142-146` are:

- `SERENA_QUERY target=… tool=… calls=N response_bytes=N ms=N`
- `SERENA_FALLBACK target=… [phase=…] reason=…`
- `SERENA_PROBE target=… result=ok|failed|skipped reason=… server_name=… server_version=…`

Emission sites:

| Prefix | Emitter | Workflows wired |
|---|---|---|
| `SERENA_QUERY` | `scripts/serena_stats_emit.py:135` (per-tool rollup of Codex logs) | `implement.yml:4131-4155`, `validate.yml:853-872`, `review_autofix.yml:5561-5583` |
| `SERENA_FALLBACK` | `scripts/setup_serena.sh:43-50`, `scripts/validate_process.sh:555`, inline in `implement.yml:1008/1014`, `review_autofix.yml:3220/3226` | same workflows + bootstrap |
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
3. **No generic `<MCP_NAME>_QUERY` capture.** If a third MCP lands
   (e.g., `context7`, `playwright`), the same patch has to be replicated
   end-to-end. The contract in `agents.md` is already per-server, not
   generic.
4. **`SERENA_PROBE` is unused downstream.** Handshake-failure rate is
   a useful reliability signal ("how often does Serena fail to come up
   in this workflow?") distinct from runtime fallback rate. Nothing
   aggregates it today.

---

## 2. Approach options

### Option A — Minimal Serena parity

Mirror the Semble pattern in `cost_audit.py` and add four sentences
to `mode-workflow-analysis.txt`. No abstraction.

- Pros: smallest diff; cheapest to review; honours §5 (minimal change).
- Cons: a third MCP next month means another full pass through both
  files.

### Option B — Generic any-MCP framework

One regex captures `(?P<server>[A-Z][A-Z0-9_]*)_QUERY|_FALLBACK|_PROBE`.
Aggregation becomes a `dict[server_name → metrics]`. Markdown / JSON
output enumerates whatever servers appear.

- Pros: future MCPs cost zero parser work; analyst prompt can speak
  generically ("any `*_QUERY` line").
- Cons: weaker readability of `cost_audit.py` output (everything
  pivots on a dict); the analyst still needs server-specific guidance
  to interpret `tool=` distributions (Serena tools ≠ Semble targets).
  Generalising preemptively for ONE upcoming server arguably violates
  §5; the SEMBLE-only code is barely a year old.

### Option C — Named-first + generic catch-all (RECOMMENDED)

Treat Semble and Serena as first-class named systems in `cost_audit.py`
output (preserves existing Semble table; adds a parallel Serena table).
Behind the same parsing pass, add a generic `MCP_*_QUERY` capture that
records any other-named server under `other_mcp` so future emitters
become visible immediately — even if their human-readable analysis
waits for a follow-up. Analyst prompt gets explicit Serena guidance
plus one generic line ("if any other `*_QUERY`/`*_FALLBACK` lines
appear, treat them as MCP telemetry of unknown provenance and call out
the gap").

- Pros: keeps existing Semble UX; brings Serena to full parity now;
  makes any new MCP visible (not analysed, but discoverable) without
  another patch.
- Cons: marginally more code than Option A; the catch-all section is
  empty until a third MCP exists.

---

## 3. Concrete change inventory (assumes Option C)

### 3.1 `scripts/cost_audit.py`

New regex constants near the existing Semble ones (cost_audit.py:74-75):

- `SERENA_QUERY_RE` — matches `(?:^|\s)SERENA_QUERY(?:\s|$)`.
- `SERENA_FALLBACK_RE` — matches `(?:^|\s)SERENA_FALLBACK(?:\s|$)`.
- `SERENA_PROBE_RE` — matches `(?:^|\s)SERENA_PROBE(?:\s|$)`.
- `MCP_QUERY_GENERIC_RE` — matches
  `(?:^|\s)(?P<server>[A-Z][A-Z0-9_]*)_QUERY(?:\s|$)` (catch-all; the
  named matchers run first and pre-empt this for SEMBLE/SERENA).

`parse_log()` new accumulator fields (parallel to the existing
`semble_*` block at cost_audit.py:130-133):

```
serena_query_calls       int
serena_query_response_bytes  int   # Serena emits `response_bytes`, not `bytes`
serena_query_tool_calls  int       # sum of `calls=` across SERENA_QUERY lines
serena_query_ms          int       # sum of `ms=` across SERENA_QUERY lines
serena_fallbacks         int
serena_probe_ok          int
serena_probe_failed      int
serena_probe_skipped     int
serena_targets           dict[target → {query_calls, response_bytes,
                                        tool_calls, ms, fallbacks,
                                        probe_ok, probe_failed,
                                        probe_skipped}]
serena_tools             dict[tool → {calls, response_bytes, ms}]
other_mcp_query_calls    dict[server → count]   # generic catch-all
other_mcp_fallbacks      dict[server → count]
```

Per-line parsing logic (extends the loop at cost_audit.py:167-178):

- `SERENA_QUERY`: extract `target=`, `tool=`, `calls=`, `response_bytes=`,
  `ms=`. Sum into both per-target and per-tool dicts.
- `SERENA_FALLBACK`: extract `target=`, `reason=` (reason currently
  unused but worth keeping in JSON for the analyst). Sum per-target.
- `SERENA_PROBE`: extract `target=`, `result=`. Bucket into
  `probe_ok|failed|skipped`. Sum per-target.
- Catch-all only matches the generic regex when none of the named
  patterns hit, so `SEMBLE_QUERY` and `SERENA_QUERY` never double-count.

`main()` aggregation block (cost_audit.py:211-227 + 248-259) extends
to include the new fields. Markdown output gets a new
`## Serena telemetry breakdown` section mirroring the existing
Semble one at cost_audit.py:322-361, plus a small
`## Other MCP servers observed` table when `other_mcp_*` is non-empty.

Per-run JSON payload (cost_audit.py:260-273) extends symmetrically.

**Field-name note (Q-PLAN-B):** Semble logs `bytes=N`; Serena logs
`response_bytes=N`. §6 forbids renaming either contract. The parser
must accept both. The aggregated JSON field for Serena will be
`serena_query_response_bytes` (preserve emit-side naming) — not
`serena_query_bytes`. The analyst can compare across systems by name.

### 3.2 `prompts/mode-workflow-analysis.txt`

Concrete edits (line numbers refer to the current file):

- **Line 8** — extend the input bullet:
  > `Semble query/fallback telemetry (\`SEMBLE_QUERY\`, \`SEMBLE_FALLBACK\`)
  > and Serena query/fallback/probe telemetry (\`SERENA_QUERY\`,
  > \`SERENA_FALLBACK\`, \`SERENA_PROBE\`) including logged prompt bytes
  > where available`
- **Line 46 (Cost Optimizations)** — append a Serena clause:
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
- **Line 54 (Metrics Appendix)** — extend the minimum-set:
  > …Semble query/fallback totals with logged-byte counts/rates and
  > Serena query/fallback/probe totals with response-byte and per-tool
  > breakdowns when present.
- **Line 62 (first-class-evidence rule)** — extend:
  > If `SEMBLE_QUERY`, `SEMBLE_FALLBACK`, `SERENA_QUERY`,
  > `SERENA_FALLBACK`, or `SERENA_PROBE` appear in deep-dive excerpts or
  > `log_summary`, treat them as first-class evidence: cite the
  > workflow/job/step and target, quantify logged bytes and
  > fallback/probe counts, and avoid assuming a universal `phase=`
  > field when the logs only provide `target=`.
- **New rule (after line 62)** — generic catch-all guidance:
  > If a line matches `<NAME>_QUERY` or `<NAME>_FALLBACK` for a name
  > other than `SEMBLE` or `SERENA`, treat it as an unknown MCP server's
  > telemetry: count occurrences per server, surface the names under
  > the Metrics Appendix's "Other MCP" table, and flag the gap in
  > analyst coverage rather than analysing it as one of the known
  > systems.

### 3.3 Tests

New `tests/test_cost_audit_serena_metrics.py` mirroring the existing
`tests/test_cost_audit_semble_metrics.py`:

- Fixture with sample log lines covering SERENA_QUERY (multi-tool,
  multi-target), SERENA_FALLBACK, SERENA_PROBE (ok/failed/skipped).
- Assert per-target, per-tool, and probe-bucket aggregations.
- Edge case: a malformed SERENA_QUERY (missing `response_bytes=`)
  should not crash; missing fields become 0.
- Edge case: a `FOO_QUERY` line falls into `other_mcp_query_calls`
  with `server=FOO`; SEMBLE/SERENA lines never do.

No changes needed to existing Semble tests (`test_cost_audit_semble_metrics.py`,
`test_implement_semble_contract.py`, `test_validate_semble_contract.py`,
`test_review_semble_contract.py`) — they're already covered.

### 3.4 Downstream collectors (verify, don't necessarily change)

`scripts/collect_workflow_logs.py` and `scripts/analyze_workflow_logs.py`
build the workflow-log folder and `analysis_context.json` consumed by
the Codex analyst. Two checks before merging:

1. **Deep-dive excerpt inclusion.** Confirm that the line-scoring in
   the collector treats `SERENA_QUERY`/`_FALLBACK`/`_PROBE` lines as
   evidence-worthy (so they survive into `errors/`, `slow/`, or
   `recent/` excerpts). If the scorer is regex-based and only knows
   about `SEMBLE_`, extend it.
2. **`summary.json` rollups.** If `summary.json` carries per-run
   Semble counts, add Serena counts alongside so the analyst sees them
   even for runs that didn't get a deep-dive folder.

These are read-and-confirm steps; both files may already be
prefix-agnostic. Will verify during implementation, not before.

### 3.5 Out of scope

- `agents.md`: no change. The three Serena prefixes are already
  contractual at `agents.md:142-146`.
- `setup_serena.sh`, `serena_stats_emit.py`, `mcp_handshake_probe.py`:
  no change. Emitters are correct; the gap is only downstream.
- Workflow YAMLs (`implement.yml`, `validate.yml`, `review_autofix.yml`):
  no change. They already invoke the emitters.
- `workflow-templates/`: no change. Consumer repos pick up the new
  parse + analyst behaviour automatically on next stable release
  dispatch, because `cost_audit.py` and `mode-workflow-analysis.txt`
  live in this repo and run from the analysis workflow that pulls
  consumer logs (per `.github/workflows/workflow-log-analysis.yml`),
  not on the consumers themselves.

---

## 4. Risks and rollback

- **Risk: regex over-match.** A literal `_QUERY` substring inside a
  Codex prompt or a quoted log line could trip the generic catch-all.
  Mitigation: require `[A-Z][A-Z0-9_]*_QUERY` plus a leading
  whitespace or start-of-line anchor (matches existing Semble pattern).
- **Risk: `response_bytes` field-name mismatch.** Already covered
  in §3.1 — preserve emit-side naming, don't normalise.
- **Risk: extending the analyst prompt makes it longer and noisier.**
  Mitigation: edits are sentence-level additions to existing sections,
  not new sections. No new section heading; the existing structure
  already covers Cost / Reliability / Metrics Appendix.
- **Rollback path.** All changes are additive: the new regexes,
  fields, JSON keys, and prompt sentences are net-new. Reverting the
  diff restores prior behaviour exactly. No DB / index / contract
  changes (so §10 is not engaged).

---

## 5. Open questions (must answer before implementation)

> **Q-PLAN-A: Include the generic any-MCP catch-all now, or defer?**
>
> Choices:
> - **A** — Include the catch-all (Option C as written) (RECOMMENDED).
>   Marginal extra code; future MCPs become visible without a patch.
> - **B** — Defer the catch-all; Serena-only this round (Option A).
>   Smallest diff; revisit when a third MCP actually lands.
>
> Reply: `Q-PLAN-A: A`

> **Q-PLAN-B: Field-name handling for `response_bytes` vs `bytes`.**
>
> Choices:
> - **A** — Preserve emit-side naming. JSON gets
>   `semble_query_bytes` and `serena_query_response_bytes` as
>   distinct fields (RECOMMENDED). Honours §6.
> - **B** — Normalise both to `_bytes` in the aggregated JSON so
>   cross-system comparisons read uniformly. Add a JSON-schema
>   migration note in the analyst prompt.
>
> Reply: `Q-PLAN-B: A`

> **Q-PLAN-C: How to track `SERENA_PROBE` in the Metrics Appendix.**
>
> Choices:
> - **A** — Single "MCP availability" row showing
>   `probe_ok / probe_failed / probe_skipped` per target per workflow
>   (RECOMMENDED).
> - **B** — Skip probe metrics in Metrics Appendix; surface only in
>   the Reliability section narrative.
> - **C** — Both: appendix row plus reliability narrative.
>
> Reply: `Q-PLAN-C: A`

---

## 6. Work breakdown (line-item checklist)

Once Q-PLAN-A/B/C are answered, the implementation pass is:

- [ ] `scripts/cost_audit.py` — add `SERENA_QUERY_RE`,
      `SERENA_FALLBACK_RE`, `SERENA_PROBE_RE`, and (if Q-PLAN-A=A)
      `MCP_QUERY_GENERIC_RE`; extend `parse_log()` accumulators;
      extend `main()` aggregation + Markdown output + JSON payload.
- [ ] `prompts/mode-workflow-analysis.txt` — apply the line-by-line
      edits in §3.2 (5 edits + 1 new rule).
- [ ] `tests/test_cost_audit_serena_metrics.py` — new file modelled on
      `test_cost_audit_semble_metrics.py`.
- [ ] Verify `scripts/collect_workflow_logs.py` deep-dive scoring
      treats `SERENA_*` as evidence-worthy; extend if not.
- [ ] Verify `scripts/analyze_workflow_logs.py` / `summary.json`
      rollups carry Serena counters; extend if not.
- [ ] Run the full test suite locally before push.
- [ ] Push branch `claude/mcp-usage-monitoring-ytkni`; open PR with
      this plan linked from the PR description.

Estimated effort: small. Two source files + one test file + two
verify-and-maybe-touch files. No DB / index / contract changes.
