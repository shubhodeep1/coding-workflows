<!-- changelog: changed -->
- **A stall-killed review editor attempt now retries on the fallback model.** When the stall guard kills an editor attempt, the next attempt runs on `MODEL_EDITOR_FALLBACK` instead of the same primary model, and later attempts stay on it.

Before this, the editor switched to the fallback only on its final attempt. Runs 35456293637 and 35473950832 each lost two stall-killed attempts on the primary model, roughly 10 to 25 minutes, before the fallback succeeded on attempt 3. The log now says `Editor attempt N: previous attempt was stall-killed; switching model to fallback <model>.` when it switches.

The switch happens only when `MODEL_EDITOR_FALLBACK` is set and differs from `MODEL_EDITOR`, and only on the OpenCode engine: on the Claude engine the sandbox picks its own model, so nothing changes there. Failures that are not stall kills still retry the primary model, and the final-attempt fallback is unchanged.

What this means for operators: a stalled editor recovers on the next attempt instead of the last one. `EDITOR_STALL_FALLBACK_ENABLED=false` in the editor step's environment turns this off; unset, empty or `true` keeps it on, and any other value turns it off with one warning. `review_autofix.yml` does not yet pass a repository variable of that name through, so setting it as a repository variable has no effect; to turn the fallback off from repository settings today, set `WORKFLOW_EDITOR_FALLBACK_MODEL` to the primary editor model, which also turns off the final-attempt fallback.
