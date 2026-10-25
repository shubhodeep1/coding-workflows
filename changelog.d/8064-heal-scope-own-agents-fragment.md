<!-- changelog: fixed -->
- **Workflow-heal issues can ship their own `agents.d/` fragment, and an out-of-scope heal hold no longer points maintainers at `/approved`.** A heal plan that adds the documentation fragment CLAUDE.md §30 requires is now auto-approved and implemented instead of being held and then refused.

The heal scope marker only ever carried `tests/**` and `changelog.d/*.md`, so every heal whose plan added `agents.d/<issue>-<slug>.md` was held with `heal_scope_plan_out_of_scope`, and implement would still refuse the file after `/approved` (#8064). The heal plan gate, the isolated editor's copy-back and the commit-time scope guard now also accept `agents.d/<N>-*.md` for the issue's own number N. The marker format is unchanged, so issues already filed benefit. Separately, the hold comment for `heal_scope_plan_out_of_scope` used to say a maintainer can reply `/approved`; implement enforces the frozen marker regardless, so it now says `/approved` alone does not help and names `/reclarify` or a fix outside the heal pipeline instead.

| The numbers that matter | Value |
| --- | --- |
| Extra heal scope entry | `agents.d/<N>-*.md`, issue N only, top level only |
| Checks that honour it | 3 (plan gate, isolated copy-back, commit guard) |
| Hold reasons with new wording | 1 (`heal_scope_plan_out_of_scope`) |

What this means for operators: a heal issue that only adds its own agents fragment beyond the marker runs end to end without a human. A heal that needs code outside its marker still holds, and the comment now says what will actually unblock it.
