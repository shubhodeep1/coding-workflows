<!-- changelog: security -->
- **The clarify and review-editor model relay now enforces the #4090 request, token, and cost budgets.** Clarify and the sandboxed review editor call the model through `scripts/clarify_openrouter_broker.py`, which previously only checked the path and the model.

The relay's `broker` and `review-broker` modes now apply the same policy as `scripts/model_provider_broker.py`. It caps the request count, per-request and total output tokens, per-request and total input tokens, and the worst-case USD cost at the provider price ceilings. It normalizes each request (token limit, `provider.max_price`, no fallback model arrays) before forwarding and settles each reservation against the provider-reported usage. An exhausted budget returns HTTP 429, and the attempt fails closed. `clarify.yml` and `orchestrate_clarify_respond.yml` export the `MODEL_PROVIDER_BROKER_MAX_*` repo variables again and stage `model_provider_broker.py` next to the relay.

| The numbers that matter | Value |
| --- | --- |
| Requests per relay process (default) | 100 (`MODEL_PROVIDER_BROKER_MAX_REQUESTS`, range 1–100) |
| Output tokens per request / total (default) | 16384 / 1638400 |
| Price ceilings (default) | 10 / 30 USD per million prompt / completion tokens, 0.10 USD per request, 1 USD per image |
| Budget scope | one relay process: one clarify attempt or one editor attempt |

What this means for operators: the `MODEL_PROVIDER_BROKER_MAX_*` repo variables you already set for the other model-facing phases now bound clarify and review editing too. An invalid value, or a support bundle without `model_provider_broker.py`, stops the attempt before any model call instead of running it unbudgeted.

### For contributors

The bridge runs alone in the container as the single mounted file `/bridge.py`, so the budget module is loaded only in the host-side broker modes, from the relay's own directory by explicit file path rather than `sys.path`. `scripts/clarify_isolated_run.sh` and `scripts/review_untrusted_sandbox.sh` pass the budget variables through their `env -i` launch. Tests: `tests/test_clarify_openrouter_broker_budgets.py`.
