<!-- changelog: security -->
- **Validation no longer executes review-runtime files supplied by a checkout or model-generated shell.** Review autofix emits bounded declarative assertions, signs them for the exact repository, PR, reviewed head, round, and producer run, and validation restores only that PR's authenticated cache subtree.

The trusted evaluator runs without workflow secrets or networking as UID 65534 against a read-only repository with Git metadata masked. Force-tick cooldown records now carry unique canonical claim IDs, so two claims created in the same second collide and only the matching pending claim can record its final dispatch result.
