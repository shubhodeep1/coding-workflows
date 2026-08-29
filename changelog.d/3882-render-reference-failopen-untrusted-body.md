<!-- changelog: fixed -->
- **Reviewer runs no longer fail when a PR diff mentions a nonexistent `REFERENCE_*` placeholder.** `render_prompt.py` now fails open on unresolvable reference placeholders when rendering untrusted assembled prompt bodies.

Run 33245886964 killed all reviewers in the `Run reviewer models` step because the reviewed PR was a plan doc containing a literal braced `REFERENCE_SECURITY_MONEY_LENS` token for a reference file that does not exist yet. The reviewer prompt body embeds the raw PR diff, and reference-placeholder hydration scanned that untrusted content strictly, hard-failing the render on the missing `prompts/references/security-money-lens.txt`. On the `--input-already-assembled --skip-syntax-validation` untrusted assembled-body path the renderer now leaves such tokens unhydrated so they render verbatim, emits a stderr warning, and still hydrates resolvable references like the reviewer checklist's severity-classification block. Trusted template renders keep the strict hard failure, so real prompt-authoring mistakes still fail loudly. This closes the third gap in the same seam, after include-assembly (run 29182737982) and the template-syntax gate (run 28936678508).

| The numbers that matter | Value |
| --- | --- |
| Failing run | 33245886964 |
| Renderer path affected | `--input-already-assembled --skip-syntax-validation` |
| Behaviour on missing reference (untrusted body) | warn + render token verbatim |
| Behaviour on missing reference (trusted template) | unchanged hard failure |

What this means for operators: a docs-only or plan PR that merely mentions a future `REFERENCE_*` placeholder no longer takes down its own review_autofix run; the token passes through to the reviewer prompt as plain text.

### For contributors

`hydrate_reference_placeholders()` gained a `strict` keyword (default `True`); `main()` enables leniency only when both `--input-already-assembled` and `--skip-syntax-validation` identify an untrusted assembled body. Regression test: `test_render_prompt_py_fails_open_on_missing_reference_in_untrusted_assembled_body`.
