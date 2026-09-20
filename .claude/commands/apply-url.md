Read the URL(s) in `$ARGUMENTS`, follow same-domain links/pagination a bounded depth until you have a **full grasp** of the content, then report — **read-only** — what from it could **improve the existing code** or **add new features** to the repo this command is invoked from. `$ARGUMENTS` may contain **one or more URLs** (newline- or whitespace-separated; mix and match accepted) and an **optional free-form focus** (which parts to weight, what the repo is trying to get better at, a suspected angle). The focus may appear before, between, or after the URLs. This command makes **no** code changes, writes **no** files, and opens **no** PR — its deliverable is the chat report. It mirrors the URL-driven fetch/follow loop of `/analyze-log` and the repo-context grounding of `/apply-analysis`, but its job is to map an external resource onto *this* repo and surface concrete, repo-anchored improvements.

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`.** Extract every `https?://...` token as a source URL; treat all remaining (non-URL) text as the user's free-form focus. Save the focus verbatim — it shapes prioritisation in steps 5–6 (which sections to weight, what the repo cares about) but never invents applicability that the repo + source don't support. If `$ARGUMENTS` contains **zero URLs**, stop and ask the user for at least one URL (§0/§2) — this command is seed-URL-driven, not open-ended research (for that, use `/deep-research`; for a GitHub issue/PR/run reference, use `/investigate-issue`).

2. **Load repo context first — this is the "linked to the repo" half.** Read `README.md`, `agents.md`, and `CLAUDE.md` at the repo root, plus any `/db/contracts/*.yml` relevant if the source touches data/collections (§10). Build a working model of **what this repo actually is**: its purpose, stack, conventions, and the capabilities it already has. Without this you cannot say what is applicable — a recommendation that is not anchored to something this repo does (or could plausibly do) is noise. Do this **before** mapping, so every later finding lands against real code, not a guess.

3. **Fetch the seed URL(s).** Pick the tool by page type (§17 Preferred Tools):
   - **Public, text-ish pages** → `WebFetch` (free, text-only) — the default.
   - **PDFs** → download then `pdftotext` (not the `Read` tool), e.g. `curl --fail-with-body -sSL -o /tmp/apply-url-<n>.pdf <url> && pdftotext -layout /tmp/apply-url-<n>.pdf -`.
   - **JS-rendered / auth-walled / dynamic pages** → the `agent-browser` CLI.
   Use a **distinct temp name per URL** so concurrent fetches don't overwrite each other. Retry transient errors (5xx, 429, timeouts, DNS, connection reset) with exponential backoff (up to 4 retries: 2s, 4s, 8s, 16s). Hard failures (401/403/404/410, auth-walled with no `agent-browser` access) → record the URL as inaccessible; stop and report only if **all** seed URLs are inaccessible. Do not brute past auth walls or obvious ToS/robots gates.

4. **Follow through multiple pages — bounded crawl until full grasp.** Determine whether the content is paginated or multi-part: numbered pages, "next"/"previous" links, a docs table-of-contents, a multi-section guide, a blog series, or an API reference split across sub-pages. Follow **in-content** links on the **same registrable domain**, up to **depth 2** from each seed, deduped, capped at **~15 pages total**. Stop early when coverage plateaus (new pages stop adding material — diminishing returns). Off-domain links: fetch **at most one** when it is essential to understanding a referenced concept; do **not** deep-crawl off-domain. **Do not map from a partial read** — keep pulling pages until you genuinely grasp the resource or hit the cap, then say how far you got.

5. **Extract the source's ideas.** Enumerate the concrete things the source teaches — techniques, patterns, practices, features, config, snippets, pitfalls, defaults. Each idea carries a **source citation** (which page / section, with a short quote or tight paraphrase). No citation → it is not a claim.

6. **Map each idea onto the repo.** For every extracted idea, classify it against the repo model from step 2 using the [Classification](#classification) buckets below, grounding each in **both** the source (page/section) **and** the repo (`file:line`). Rank the actionable ones by §1 priority order (security & correctness before compatibility, clarity, performance, speed), then by value-vs-effort. Apply the §6/§10/§18 framing in [Rules](#rules) as you write each recommendation.

7. **Report — read-only.** Emit the [Output Format](#output-format). Make **no** edits, write **no** files, open **no** PR, make **no** commits. Point the user at the natural next step (`/write-plan` to turn a recommendation into a plan; then `/implement-plan-claude` or `/apply-analysis` to ship it).

## Classification

Sort every extracted idea into exactly one bucket:

- **IMPROVEMENT** — changes or strengthens something the repo **already does**. Must cite the existing `file:line` it would touch and what changes. (e.g. "harden the retry loop at `scripts/foo.sh:120` with the backoff-with-jitter the source recommends.")
- **NEW-FEATURE** — a net-new capability the repo lacks but that **fits** its purpose and conventions. Must cite the repo anchor it slots next to (the module/flow it would extend) so it is concrete, not abstract.
- **ALREADY-PRESENT** — the repo already does this; cite where (`file:line`). Surface briefly so the user sees the source was checked against reality, not silently dropped.
- **NOT-APPLICABLE** — wrong stack, out of scope, or conflicts with a repo convention/contract. One-line why.

A source idea that cannot be anchored to a repo `file:line` (existing code to improve, or a concrete place a new feature would land) goes under **NOT-APPLICABLE** with a one-line reason — never as a floating, repo-less suggestion. **No repo citation → no recommendation.**

## Output Format

Keep it tight. No prose padding. Omit empty sections.

```
Source: <seed URL(s)> — <N pages read, max depth reached>; focus: <user's focus in a phrase, or "none">
Grasp: <2–4 lines: what the source is about and its key takeaways>

Repo: <one line: what this repo does — the anchor the mapping is measured against>

Recommendations (ranked):
1. [IMPROVEMENT|NEW-FEATURE] <title> — priority: <security|correctness|compat|clarity|perf|speed>
   Source: <page/section> — "<short quote or paraphrase>"
   Repo:   <file:line> — <what exists today / where a new feature would land>
   Change: <concrete 1–3 line description of what to do>
   Effort: <S|M|L>   Risk: <low|med|high>   <§6/§10/§18 note if the change implies a rename, a /db/contracts/* update, or scheduler wiring>
2. ...

Already present (skip):
- <idea> — repo already does this at <file:line>

Not applicable:
- <idea> — <one-line why: wrong stack / out of scope / conflicts with convention>

Next step: <e.g. "/write-plan on rec #1 to turn it into a plan", then "/implement-plan-claude" to ship it>
```

## Tool Access

- **`WebFetch`** — primary fetch for public text pages (§17). Free and text-only; use it for the seed and for each followed page.
- **`pdftotext`** (via `Bash`) — for PDF URLs; do not use the `Read` tool on PDFs (§17).
- **`agent-browser` CLI** (via `Bash`) — for JS-rendered, dynamic, or auth-walled pages where `WebFetch` returns shell HTML or a login wall (§17).
- **`Read` / `Grep` / `Glob`** — load repo context (step 2) and find the exact `file:line` anchors that ground every recommendation. **No edits.**
- **`WebSearch`** — optional, and only to resolve a redirect / shortlink to its canonical page or to locate the right entry point when the seed URL is ambiguous. Not for broad open-ended research — this command stays anchored to the seed URL(s).
- **`Bash`** — only for `curl`/`pdftotext`/`agent-browser` fetches and temp-file handling. This command performs **no** git writes.

## Rules

- **Read-only — no edits, no files, no PR, no commits.** The deliverable is the chat report. Turning a recommendation into code is a separate, explicit step (`/write-plan` → `/implement-plan-claude`, or the `/apply-analysis` orchestrator hand-off). If you find yourself editing source or opening a PR, you are in the wrong command.
- **Every recommendation is anchored to THIS repo.** Cite a concrete `file:line` for what to improve, or the place a new feature would land. A source idea with no repo anchor goes under **Not applicable**, never as a floating suggestion. No repo citation → no recommendation.
- **Full grasp before mapping.** Follow pagination / multi-part content to depth 2, same registrable domain, until coverage plateaus or the ~15-page cap is hit. Do not recommend from a partial read; if you stopped early, say so and why.
- **Bounded crawl.** Same registrable domain, depth ≤2, ~15-page cap, dedupe, stop on diminishing returns. Off-domain: at most one essential fetch, no deep crawl. Respect auth walls / obvious ToS-robots gates — do not brute past them.
- **§1 ranking is binding.** Security and correctness outrank performance and speed. Never headline a perf idea that risks correctness; if the source pushes a risky optimisation, rank it low and flag the tradeoff.
- **§6 naming immutability.** If a recommendation implies renaming or removing an existing identifier, frame it as "add alongside / alias the old name", never an in-place rename, and flag it as §6-gated.
- **§10 MongoDB contracts.** A data / collection / index recommendation must note the matching `/db/contracts/*` update it would require — do not propose a schema/index change without it.
- **§18 automation bias.** A recommendation that adds a recurring operation must note that it should be wired into an existing scheduler/workflow (no standalone manual script), and DB work should run from code behind a gate — call this out in the recommendation's note rather than implying an operator runs it by hand.
- **Distinguish IMPROVEMENT from NEW-FEATURE** in every recommendation so the user can triage "make what we have better" vs "add something new" at a glance.
- **Stop and ask/report (§0/§2)** if `$ARGUMENTS` has no URL, every URL is inaccessible or empty, or the content is too thin to map to the repo with evidence — don't guess.
- **Cleanup:** delete every temp file (PDF dumps, fetched HTML) written under `/tmp/` during the fetch when done.
