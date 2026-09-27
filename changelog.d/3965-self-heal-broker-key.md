<!-- changelog: fixed -->
- **Validation self-heal runs again.** `scripts/self_heal_validation.sh` no longer stops on a missing `OPENROUTER_API_KEY` when `validate_process.sh` launches it.

`validate_process.sh` starts the model-provider broker and then unsets `OPENROUTER_API_KEY`, so the self-heal it launches never saw the key and exited on its first line, before any patch attempt (project #3965, run 36288327878). The script already reuses the caller's broker when `MODEL_PROVIDER_BROKER_TOKEN` and `MODEL_PROVIDER_BROKER_BASE_URL` are set, so the key is now required only when no broker is running and self-heal has to start its own.

What this means for operators: `MAX_SELF_HEAL_ATTEMPTS` prompt repairs happen on failed validation runs again; no configuration change is needed.
