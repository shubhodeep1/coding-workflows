# Implement-Plan Log — Claude-fixer PRs converge unattended

- Plan: docs/plans/claude-fixer-unattended-convergence-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-claude-fixer-unattended-convergence   Final PR: #4648 draft
- Status: IN_PROGRESS
- Stage: phase 1/4
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: phase 1 (checks pending) implemented and verified locally; phase PR opened against the project branch

## Phases
1. [ ] Phase 1 — Checks-pending, not a hand-off (evidence artifact + helper, checks-pending marker, merge-check gate and step)   — PR open (waiting); review rounds: 0; interventions: 0
2. [ ] Phase 2 — GPT judge for Claude-fixer PRs (judge dispatch input, Claude mode in review_rb_judge.sh, sticky rulings, fixer docs REST dispatch)
3. [ ] Phase 3 — Checkers never ask; reasoning before holds; held backoff (retry_after_minutes)
4. [ ] Phase 4 — Session janitor (stale_sessions.py run by the hourly Claude issue pickup)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [phase 1/4, 2026-09-27] Which library refs does evidence verification trust for the reusable `review_autofix.yml`, given no trusted-ref helper exists and consumer wrappers pin `@<40-hex sha> # stable`? — Picked: A — `refs/heads/main`, `refs/tags/stable`, or a 40-hex SHA pin (the pin lives in the caller wrapper, which the caller-ref check already trusts). Alternatives: B — only `refs/heads/main` and `refs/tags/stable` (plan fallback; every consumer run would fail verification); C — resolve the current `stable` SHA through the API and accept only that (one more call per verify, and older pins fail after each release). Why: matches the fingerprint-cap support check (`^(refs/heads/main|refs/tags/stable|[0-9a-f]{40})$`) and keeps consumers working. Applied in: phase 1 PR. Status: pending review
- AD-2 [phase 1/4, 2026-09-27] Which non-ready check snapshots become checks pending? — Picked: A — `ready` (with no check runs yet), `timeout`, and `api_error` for the same head with no failure; `disabled`, `unavailable`, `writer_error`, a head mismatch, or a missing collector keep the zero-finding hand-off. Alternatives: B — every non-ready snapshot (a disabled collector would then wait forever without a hand-off); C — only `timeout`. Why: only states a later merge check can resolve should wait; the rest keep today's behaviour so a round can never stall silently. Applied in: phase 1 PR. Status: pending review
- AD-3 [phase 1/4, 2026-09-27] How does the gate choose between the awaiting-session skip and the merge check when a head has both a hand-off and a checks-pending comment? — Picked: A — the newest workflow-authored marker (by comment id) decides; the checks-pending comment is updated in place only while it is that newest marker, otherwise a new one is posted. Alternatives: B — any hand-off on the head wins (a force-review re-run that came back clean would then wait on a session forever); C — any checks-pending comment wins (an older clean round would override a newer hand-off). Why: the latest round's outcome is the current state. Applied in: phase 1 PR. Status: pending review
- AD-4 [phase 1/4, 2026-09-27] Does the merge check run on a PR GitHub reports as conflicted? — Picked: A — no; a PR with `mergeable=false` or `mergeable_state=dirty` takes the normal review, whose pre-review topology gate hands the conflict to the Claude session. Alternatives: B — run the merge check anyway (auto-merge could never land and the conflict would never be handed off). Why: the conflict hand-off already exists on the normal path. Applied in: phase 1 PR. Status: pending review
- AD-5 [phase 1/4, 2026-09-27] Where does the evidence upload step go now that the merge-check step also writes evidence? — Picked: A — right after "Claude-fixer merge check", which itself sits right after the hand-off step, so one upload carries whichever step ran. Alternatives: B — directly after the hand-off step, with a second upload after the merge check (two artifacts with the same name conflict). Why: one artifact per run keeps `claude-fixer-evidence-<run_id>-<attempt>` unique. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] Consumer wrappers pin the library workflow by 40-hex release SHA (`@<sha> # stable`), not `@stable`, so any `referenced_workflows` trust check must accept SHA pins or every consumer run fails verification. (files: scripts/review_claude_fixer_evidence.py, scripts/workflow_wrapper_refs.py)

## Notes
- Started 2026-09-27 by a stage-start trigger from the supervising session session_01VwSvLnEGmUoaQD42DKapiU. Plan decisions D1–D10 were answered with the operator and are not re-opened. Per D8, a phase PR that ends on the no-verdict-bot hold is reported BLOCKED with the finding-by-finding reasoning on the PR; the supervising session obtains the operator's merge approval.
