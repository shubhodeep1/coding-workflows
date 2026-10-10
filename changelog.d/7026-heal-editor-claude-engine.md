<!-- changelog: changed -->
- **Workflow-heal implementations now run on Claude, and the review editor's Claude pool runs show up in the job log.** Heal issues no longer fall back to `openai/gpt-6-sol` by design.

`scripts/heal_isolated_implement.sh` used to log `AI_ENGINE_FALLBACK role=IMPLEMENT reason=isolated_claude_unavailable` and run codex for every `ai:workflow-heal` issue, even though `IMPLEMENT` resolves to Claude by default. When it resolves to Claude, the heal editor and its syntax-repair passes now run through `ai_engine.sh` `claude_run` on the script's own snapshot, in the same credential-free write container normal implementation uses. The snapshot then takes the unchanged syntax validation and scope-checked transfer. Claude unavailable (exit 75) runs the isolated codex editor as before; any other Claude failure fails the run without a transfer. `HEAL_ISOLATED_EDITOR ... engine=` names the engine that ran.

The review editor's stderr reaches the job log only when an attempt fails, so a successful Claude editor run left no trace of which pool account it used. `scripts/review_apply_fixes.sh` now replays the attempt's `CLAUDE_POOL` and `AI_ENGINE_*` lines with a `stage=editor` prefix, like the consolidator's `stage=consolidator stderr=` lines.

| The numbers that matter | Value |
| --- | --- |
| Heal runs that fell back to codex by design, Oct 7-9 | 8 |
| New log line | `stage=editor CLAUDE_POOL run role=REVIEW_EDITOR ...` |
| Heal wall-time limit (`HEAL_ISOLATED_EDITOR_WALL_SECS`) | now applies to the Claude run too |

What this means for operators: heal fixes add Claude pool load instead of OpenRouter spend, and the editor's pool usage is now countable from run logs alongside clarify, plan and implement.

- **Sandbox images are now pulled prebuilt from GHCR instead of built on every job.** A Docker Hub, npm or Debian mirror outage no longer pushes Claude roles onto the codex fallback.

`scripts/sandbox_image.sh` now handles every sandbox image build (`codex_isolated_exec.sh`, `clarify_isolated_run.sh`, `heal_isolated_implement.sh`, `review_untrusted_sandbox.sh`). It pulls `ghcr.io/shubhodeep1/coding-workflows-sandbox:<family>-<input hash>` and builds locally, as before, when the pull fails, times out or returns an image with the wrong input label. The new `.github/workflows/publish-sandbox-images.yml` pushes the tags whenever `main` or `stable` changes an image input, and rebuilds them weekly to pick up Debian security updates. A repo that overrides `CODEX_VERSION`, `OPENCODE_VERSION` or the Claude CLI pin finds no matching tag and keeps building locally.

| The numbers that matter | Value |
| --- | --- |
| Images per ref | 7 |
| Pull timeout (`SANDBOX_IMAGE_PULL_TIMEOUT_SECS`) | 60 s |
| Weekly rebuild | Mondays 04:23 UTC |
| Disable pulls | `SANDBOX_IMAGE_REGISTRY=off` |

What this means for operators: after the first publish, set the `coding-workflows-sandbox` package to public once. Until then, jobs log `SANDBOX_IMAGE ... outcome=built reason=pull_failed` and build as they do today.
