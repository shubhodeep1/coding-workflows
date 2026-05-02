# Codex CLI Runner Migration Plan

> **Status**: planning — not yet executed.
> **Owner**: solo developer (sole user of the workflows in this repo).
> **Goal**: replace OpenRouter-via-GitHub-hosted-runners with a self-hosted VPS that runs Codex CLI against a ChatGPT Pro subscription, with OpenRouter as automatic fallback when the sub quota is exhausted.

## Why

Current setup pays OpenRouter rates on every CI request. Heavy use of `gpt-5.3-codex` and `gpt-5.4` makes this the dominant AI cost line. OpenAI's own Codex CLI bundles GPT-5.x usage into a flat ChatGPT Plus/Pro subscription, which is cheaper at our volume — provided we can fit within the sub's serialization rules and rate windows.

OpenRouter (the existing path) becomes the fallback tier rather than disappearing entirely, because:

- It absorbs spillover when the sub quota is exhausted.
- Its cross-tenant cache pool gives a much better effective input price than direct OpenAI BYOK on cold-cache traffic.

## Goals

- Steady-state CI traffic flows through Codex CLI + ChatGPT Pro sub.
- Quota exhaustion automatically falls back to OpenRouter (existing account, existing key).
- Stay within OpenAI's documented CI/CD auth pattern (single machine, serialized job stream, one `auth.json`).
- Preserve GitHub Actions UX: triggers, logs, status checks, branch protection — all unchanged.
- Get a clean filesystem per job (no state leakage between runs).

## Non-goals

- Multi-account pooling (TOS-prohibited at scale).
- Concurrent execution under one `auth.json` (violates OpenAI's serialization rule).
- Replacing GitHub Actions itself — only the runner that executes jobs.
- Direct OpenAI API as a third tier (sub → OpenRouter is sufficient).
- Improving GitHub's cron trigger reliability via this work (out of scope; see "Cron caveat").

## Architecture

```
GitHub event (push, PR, workflow_dispatch, [cron])
        │
        ▼
Self-hosted runner on VPS (label: codex-vps, concurrency: 1)
        │
        ▼  (per job)
docker run --rm  codex-runner:latest  ──►  codex-wrapper.sh
                                              │
                                              ├─► Tier 1: Codex CLI default profile
                                              │   (auth.json, ChatGPT Pro sub)
                                              │       on quota error ▼
                                              └─► Tier 2: Codex CLI --profile openrouter
                                                  (existing OPENROUTER_API_KEY)
```

Key properties:

- One VPS, one registered runner, `concurrency: 1` → serialized job stream by construction.
- Each job runs in a throwaway container; `auth.json` is mounted from the host so token refresh persists naturally.
- Tier transitions happen inside the wrapper, transparent to the workflow.

## Components

1. **VPS host**
   - 2–4 vCPU / 4–8 GB RAM / 40–80 GB disk.
   - Provider TBD (Hetzner / Linode / DigitalOcean — pick on cost + region).
   - Always-on; OS hardened (firewall, automatic security updates, SSH key auth only).

2. **GitHub Actions self-hosted runner**
   - Registered to this repo, label `codex-vps`.
   - Runs as a non-root system user.
   - Configured with `concurrency: 1` at the workflow level on every migrated workflow.

3. **Codex CLI installation on the host**
   - Installed once.
   - `codex login` performed once interactively to produce `~/.codex/auth.json`.
   - `~/.codex/config.toml` with two profiles:
     - default: ChatGPT sub auth, model `gpt-5.4` (or current production choice), reasoning level matching today's setup.
     - `openrouter`: `base_url=https://openrouter.ai/api/v1`, `env_key=OPENROUTER_API_KEY`, model `openai/gpt-5.4`.

4. **Runner Docker image (`codex-runner:latest`)**
   - Base: a minimal image with Node, git, ssh, jq, gh, and the Codex CLI binary preinstalled.
   - No secrets baked in; everything injected via env vars or mounts at runtime.
   - Built and pushed to a private registry (or built locally on the VPS and tagged).

5. **`codex-wrapper.sh`**
   - Reads a sentinel file (`/var/lib/codex/tier-until`) — if present and unexpired, skips Tier 1 and goes straight to Tier 2.
   - Otherwise runs Tier 1 (`codex exec ...`).
   - On quota / rate-limit error (matched by a strict regex on stderr + exit code), writes the sentinel with the reset timestamp and retries the same command on Tier 2 (`codex exec --profile openrouter ...`).
   - On any other error, propagates the failure normally — does not fall back.
   - Appends one line per run to `/var/log/codex-runs.jsonl`: `{ts, workflow, job, tier, exit, duration_s, prompt_tokens, completion_tokens}`.

6. **Spend guards**
   - OpenRouter: keep existing per-key spend cap.
   - VPS: `ulimit` / cgroup limits on the runner user to prevent runaway memory.
   - Sentinel auto-expires when the sub's reset window passes (so Tier 1 is retried, not forever-skipped after one quota event).

7. **Observability**
   - The JSONL log above is enough for a weekly summary (jobs per tier, tokens per tier, $-equivalent estimate). Summary script runs locally, no infra needed.

## Build phases

### Phase 0 — Inventory (do this before subscribing)

- List every workflow that currently calls OpenRouter.
- Record per workflow: model, reasoning level, average + peak weekly request count, average tokens per request.
- Decide Plus vs Pro based on totals (deferred — see open decision Q2).

### Phase 1 — VPS + runner skeleton

- Provision VPS, harden, install Docker.
- Register a GitHub Actions self-hosted runner with label `codex-vps`.
- Migrate one trivial workflow (e.g. a hello-world) to `runs-on: [self-hosted, codex-vps]` to prove the pipe end-to-end.

### Phase 2 — Codex CLI on the host

- Install Codex CLI on the VPS.
- `codex login` to populate `~/.codex/auth.json`.
- Manual smoke test: `codex exec` against a small prompt. Confirm token refresh writes back to `auth.json`.

### Phase 3 — Container image + OpenRouter profile

- Build `codex-runner:latest` with all required tooling.
- Add the `openrouter` profile to `~/.codex/config.toml`.
- Smoke-test `--profile openrouter` against the same prompt as Phase 2; verify feature parity (tool calls, reasoning behavior).

### Phase 4 — Wrapper

- Implement `codex-wrapper.sh` with Tier 1 only first; verify it still runs jobs end-to-end.
- Add Tier 2 fallback path. Test by temporarily forcing a quota error (e.g. invalid auth.json) and confirming Tier 2 takes over.
- Add sentinel + JSONL logging.

### Phase 5 — Pilot one workflow

- Migrate the lowest-risk OpenRouter-using workflow to call `codex-wrapper.sh`.
- Run for one week. Watch the JSONL log: tier distribution, error rate, latency.
- Stop and rethink if Tier 2 spillover exceeds ~20% of jobs (means sub tier is undersized).

### Phase 6 — Migrate the rest

- Move remaining OpenRouter-using workflows over.
- Decommission the GitHub-hosted-runner OpenRouter path.
- Document the runbook for the VPS in this repo (separate doc — operational, not architectural).

## Risks and mitigations

- **Codex CLI feature drift on OpenRouter**: Responses-API extras (reasoning summaries, etc.) may not pass through cleanly. *Mitigation*: phase 3 catches this before commitment. If a feature is missing on OpenRouter, accept degraded fallback or pin a Codex CLI version that matches OpenRouter's support.
- **`auth.json` token expiry on idle host**: tokens may expire if the host sits idle for weeks. *Mitigation*: a daily cron on the VPS runs a no-op `codex exec` to keep tokens warm.
- **Quota detection false positives**: a non-quota error misclassified as quota would burn OpenRouter credits. *Mitigation*: strict regex on the exact OpenAI quota error strings; everything else propagates as failure.
- **VPS as single point of failure**: if the VPS dies, all CI stops. *Mitigation*: runner registration tokens + a documented rebuild runbook so a fresh VPS can be online in <30 min.
- **Sub TOS interpretation drift**: OpenAI could tighten the CI/CD auth guide later. *Mitigation*: Tier 2 is already the OpenRouter path we use today, so a TOS change is a config flip (force sentinel to permanent) rather than a re-architecture.
- **Self-hosted security boundary**: fork PRs would execute arbitrary code on the VPS if ever accepted. *Mitigation*: solo-dev repo with no external contributors today; if that changes, gate fork PRs explicitly in workflow conditions.
- **State leakage between jobs**: prevented by `docker run --rm` per job. *Mitigation*: enforce in the wrapper; never let a job run on the host directly.

## Cron caveat

Switching to a self-hosted runner does **not** improve GitHub's scheduled-workflow trigger reliability. The cron is fired by GitHub's scheduler, not by the runner. If cron timing reliability matters, that needs a separate intervention (e.g. VPS cron → `gh workflow run`), tracked outside this plan.

## Open decisions

These must be answered before Phase 1 starts.

- **Q1. Cron reliability fix scope**
  - A) Move scheduled jobs to VPS cron → `gh workflow run` (keeps GH logs/UI).
  - B) Move scheduled jobs to VPS cron running work directly (loses GH UI).
  - C) Out of scope for this plan; accept GH's timing.

- **Q2. Subscription tier**
  - A) Start with Plus ($20), upgrade to Pro if Phase 5 logs show frequent spillover.
  - B) Start with Pro ($200) for headroom from day one.
  - C) Decide after Phase 0 inventory.

## Locked decisions

- **Tier 3 (direct OpenAI API)**: not included. Sub → OpenRouter is the full fallback chain.
- **Job isolation**: Docker container per job, started from the wrapper on the host. `auth.json` mounted in from the host.
- **Migration scope**: full migration off GitHub-hosted runners for AI workflows. Non-AI workflows can stay on `ubuntu-latest` if desired (decided per workflow in Phase 6).
- **TOS posture**: solo-dev, single-machine, serialized, single account. No pooling, no concurrent auth.json use.

## References

- [OpenAI Codex CLI — CI/CD auth](https://developers.openai.com/codex/auth/ci-cd-auth)
- [OpenAI Codex CLI — Authentication](https://developers.openai.com/codex/auth)
- [OpenAI Codex CLI — Models / config](https://developers.openai.com/codex/models)
- [OpenRouter — BYOK](https://openrouter.ai/docs/guides/overview/auth/byok)
- [OpenRouter — Prompt caching](https://openrouter.ai/docs/features/prompt-caching)
- [OpenAI Account Sharing Policy](https://help.openai.com/en/articles/10471989-openai-account-sharing-policy)
