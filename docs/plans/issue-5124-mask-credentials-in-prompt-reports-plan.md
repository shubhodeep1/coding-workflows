# Mask credentials in permission-prompt reports before they are posted

Source issue: shubhodeep1/coding-workflows#5124 (https://github.com/shubhodeep1/coding-workflows/issues/5124)
Base branch: claude/implement-plan-issue-4755-report-blocking-permission-prompts
Security pass: skip (ai:security: automation-produced issue)

## Summary

`permission_prompts.py` posts the command behind a permission prompt to GitHub: as the example in `ai:permission-prompt` issues (`file`, and `report-now` in coding-workflows), and as an immediate report on a consumer repository's PR or issue (`report-now`, `_report_to_own_thread`). Its redactor only masks token-shaped strings, so `curl -u deploy:mycustompwd …`, a `Authorization: Basic …` header, or `https://user:pass@host` is posted as is. This plan puts a fail-closed parse in front of every posted command: credential-bearing flag values, header values, URL credentials, and credential-named assignments are masked, and a command that cannot be parsed, or whose credential cannot be masked exactly, is withheld and only its shape is posted. The shape itself stops carrying raw text.

## Context

- Security audit finding `immediate-report-credential-disclosure` (A02:2021-Cryptographic Failures, high, confidence 9/10) at `.claude/scripts/permission_prompts.py:791` on the issue #4755 project branch, filed by `.github/workflows/security-audit.yml` (tracker #3576). Recommendation: publish command shape by default; include command text only after fail-closed parsing that removes credential-bearing flags, headers, and URLs.
- On the base branch head `bf032b5e`, every posted command goes through `record_example()` (line 326): heredoc bodies stripped, `redact()` (the `REDACTION_PATTERNS`, line 163), truncation to 2,000 characters. `redact()` masks `gh*_`, `github_pat_`, `sk-`, `xox*-`, `AKIA`, `Bearer <x>`, `token=`/`password=`-style assignments, and 40+-character random strings. It does not mask `-u user:pass`, `Authorization: Basic <b64>`, `Cookie:` values, URL userinfo, or `GH_TOKEN=<short value>` (no word boundary before `TOKEN`).
- `record_example()` feeds `_occurrence_block()` (issue bodies and "Seen again" comments, `file` and `report-now` in coding-workflows) and `immediate_block()` (the immediate report, posted in coding-workflows and, via `_report_to_own_thread()` line 791, on a consumer repository's PR or issue).
- The shape leaks raw text too: `command_shape()` falls back to `"unparseable: " + <first line>` (line 279), and `_segment_shape()` keeps a short flag with an attached value whole (`-udeploy:pwd`, line 251). The shape is posted in the issue title, the `**Pattern:**` line, and the immediate block.
- `redact()` also covers the prompt `reasons` and the recorded session title.
- `.claude/scripts/permission_prompts.py` is a protected path (CLAUDE.md §28.C). The interim twin-first default applies (CLAUDE.md §28.C, `/implement-plan-claude` step 4): the change is made in `workflow-templates/.claude/scripts/permission_prompts.py` only and copied into `.claude/` by the operator's `[claude-twin-sync]` commit.
- Sibling security follow-ups on the same file and base, each its own project: #5125 (reported-state keying), #5126 (lookup scan), #5127 (report code fence). They touch other functions; conflicts are resolved at the step 2 sync.

## Goals

- G1: A Bash command is posted only after a shell-token parse succeeds. The parse masks, with `***`: the value of every credential-bearing flag (`-u`/`-U`/`-b`/`-E` for curl, `-p` for the mysql family and `sshpass`, `-a` for httpie and `redis-cli`, `-p` for `docker`/`podman login`, and every long flag whose name names a credential, such as `--user`, `--password`, `--token`, `--cookie`, `--auth`, `--api-key`, but not `--author`). A credential short flag counts at the start of a flag cluster or after curl's boolean flags only (`-sSu` yes, `-XPUT` no), the value of every credential header (`Authorization`, `Proxy-Authorization`, `Cookie`, and names carrying token, key, secret, auth, or session), URL userinfo, and the value of every `NAME=value` word whose name names a credential.
- G2: Fail closed: when the command cannot be parsed, or a value to mask does not occur verbatim in the posted text, or is shorter than 4 characters, the command text is withheld and the posted example is `<command withheld: …>` followed by its shape.
- G3: The shape never carries raw text: the unparseable fallback is `unparseable: <command word>`, and an attached credential short flag becomes `-u*`.
- G4: `redact()` (reasons, titles, and the final example, as defence in depth) also masks URL userinfo, `Basic <b64>`, credential header values, `-u`/`--user` `name:secret` values, and credential query parameters.
- G5: Non-Bash tool input shows `***` for every value whose key names a credential.
- G6: Commands without credentials are posted exactly as before, and signatures change only for unparseable commands and attached credential short flags.

## Non-goals

- The fence around the command (#5127), reported-state keying (#5125), and `lookup`'s scan window (#5126).
- An opt-in switch to post raw commands, or a new env var or CLI flag.
- Secrets that no rule names (for example a positional password for a tool this plan does not list). The 40+-character masking stays, and the residual risk is recorded in the changelog.

## Constraints

- §1: security first; the parse fails closed.
- §5: one module, its tests, docs, and a changelog fragment.
- §6: no identifier renamed or removed. New names (`_sanitize_bash_command`, `_mask_credential_keys`, `_CREDENTIAL_NAME_RE`, `_CREDENTIAL_LONG_FLAG_RE`, `_CREDENTIAL_SHORT_FLAGS`, `_CREDENTIAL_HEADER_RE`, `_CREDENTIAL_HEADER_FLAGS`, `MIN_MASKED_VALUE_CHARS`, `WITHHELD_COMMAND_TEMPLATE`) were checked unused in `.claude/`, `workflow-templates/.claude/`, `tests/`, and `scripts/`.
- §7: `agents.md` and CLAUDE.md §23.I describe the sanitizing; §20: `changelog.d/5124-mask-credentials-in-prompt-reports.md` (`security`).
- §9: tabs in Python. §15: no new GitHub API call.
- §28.C interim twin-first rule: edit only `workflow-templates/.claude/**`; the twin-parity test stays red until the `[claude-twin-sync]` copy.

## Approach

- `_sanitize_bash_command(display, parse_text)`: tokenize `parse_text` (heredoc bodies and delimiters removed, line continuations joined) with the same `shlex` settings as `command_shape()`; on `ValueError` return None. Walk each segment (split at shell operators), take its command word (basename) and subcommand, and collect the values G1 names. Every collected value must occur in `display` and be at least `MIN_MASKED_VALUE_CHARS` (4) long, else return None; otherwise replace each (longest first) with `***` and return the text (AD-1).
- `record_example()`: for Bash, `display` is the heredoc-stripped text as today; when `_sanitize_bash_command()` returns None the example is `WITHHELD_COMMAND_TEMPLATE` with the command's shape. `redact()` and truncation run after, as today. For other tools, `_mask_credential_keys()` masks credential-named keys (and credential headers inside a `headers` mapping) before the JSON dump (AD-4).
- `command_shape()` unparseable fallback keeps only the first command word (AD-3); `_segment_shape()` cuts an attached credential short flag to `<flag>*`.
- `REDACTION_PATTERNS` gains the G4 patterns.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for a standalone issue.

1. **Phase 1 — sanitize every posted command fail-closed.** Files: `workflow-templates/.claude/scripts/permission_prompts.py` (twin of `.claude/scripts/permission_prompts.py`), `tests/test_permission_prompts.py`, `agents.md`, `CLAUDE.md` (§23.I, one sentence), `changelog.d/5124-mask-credentials-in-prompt-reports.md`. Protected paths: `.claude/scripts/permission_prompts.py` (twin-first). Done when: the new tests pass against the twin, the rest of `tests/test_permission_prompts.py` passes except the twin-parity check that waits on the sync, `ruff check` is clean, and the issue's exploit command (`curl -u deploy:mycustompwd …`) and a Basic-auth header produce no credential in `issue_body()`, `immediate_block()`, or the shape. Rollback: revert the phase PR (and its `[claude-twin-sync]` commit).

## Implementation Steps

1. `workflow-templates/.claude/scripts/permission_prompts.py`: add the constants and `_sanitize_bash_command()`, `_mask_credential_keys()`; call them from `record_example()`; add the G4 `REDACTION_PATTERNS`; change the unparseable fallback in `command_shape()` and the attached-flag case in `_segment_shape()`; update the module docstring's "Issue text is untrusted data" paragraph.
2. `tests/test_permission_prompts.py`: load the twin as a second module and add tests for G1–G6: the issue's exploit, each flag family, attached and `=` forms, headers, URL userinfo, assignments, withheld output on unparseable text, escaped values, and short values; non-Bash keys; the unparseable and attached-flag shapes; unchanged examples and signatures for credential-free commands; no credential in `issue_body()`, `immediate_block()`, or the title. Move the existing `unparseable` shape case from the `.claude` test to a twin test.
3. `agents.md` (the `file` bullet) and CLAUDE.md §23.I: say what is masked and that an unparseable command is withheld.
4. `changelog.d/5124-mask-credentials-in-prompt-reports.md` (`<!-- changelog: security -->`).

## Files & Modules

- `workflow-templates/.claude/scripts/permission_prompts.py` (twin; `.claude/scripts/permission_prompts.py` via `[claude-twin-sync]`)
- `tests/test_permission_prompts.py`
- `agents.md`, `CLAUDE.md` (also `workflow-templates/CLAUDE.md`, a symlink to it)
- `changelog.d/5124-mask-credentials-in-prompt-reports.md` [new]
- `docs/plans/issue-5124-mask-credentials-in-prompt-reports-plan.md` [new], `docs/implement-plan/issue-5124-mask-credentials-in-prompt-reports.md` [new]

## Data Model / Index Changes

None. No MongoDB collection, state file, or marker format changes.

## Tests

- Unit (new, against the twin), as in step 2.
- Existing: the full `tests/test_permission_prompts.py`; the twin-parity test fails until the `[claude-twin-sync]` copy, as for every twin-first phase.
- Lint: `ruff check`.

## Risks & Mitigations

- Over-masking hides a non-secret (`--user 1000`, `git commit --author …`). ACCEPTED: the pattern shape and signature are unchanged, and a masked value never leaks.
- A tool that takes a secret in a form no rule names still posts it. ACCEPTED, documented in the changelog: the rules cover the flags, headers, URLs, and assignments the finding names, plus the 40+-character masking.
- Signature change for unparseable commands and attached credential short flags opens a new `ai:permission-prompt` issue once per such pattern. ACCEPTED (AD-3): those shapes carried raw text.

## Rollout

Ships with the issue #4755 project: this project's final PR merges into its branch, and that project's final PR carries it to `main` and to consumers on the next `@stable` sync. No flag; revert the phase PR to roll back.

## Auto-decisions

- AD-1 [plan, 2026-09-29] How is a posted command made safe? — Picked: A — a fail-closed shell-token parse that masks credential-bearing flag values, header values, URL userinfo, and credential-named assignments, and withholds the text (shape only) when the parse fails or a value cannot be masked exactly. Alternatives: B — post the shape only, always (the #4755 report and `lookup` lose the command the operator needs); C — add regex patterns only (not fail-closed: an unrecognized form still leaks). Why: it is the issue's own recommendation and keeps the command where it is safe to (§1, §5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Fix only the consumer-thread post at line 791, or every posted command? — Picked: A — `record_example()`, which feeds line 791, the immediate report in coding-workflows, and every `ai:permission-prompt` issue body and comment. Alternatives: B — line 791 only. Why: the same command text reaches the other sites unmasked (§1). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] The shape (issue title, Pattern line, the withheld fallback) carries raw text for unparseable commands and attached short flags; change it? — Picked: A — keep only the command word for unparseable commands and cut an attached credential short flag to `<flag>*`, accepting a signature change for those patterns. Alternatives: B — redact the unparseable first line with `redact()` (keeps signatures, but `-u deploy:pwd` still leaks). Why: the fallback G2 relies on must itself be safe. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Mask credential-named keys in non-Bash tool input too? — Picked: A — yes, recursively, plus credential headers inside a `headers` mapping. Alternatives: B — leave non-Bash input to `redact()`. Why: the same leak through a different tool, in the same function (§12.B latent bug in the touched flow). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Add a switch to post raw or shape-only commands? — Picked: A — no switch; the parse decides. Alternatives: B — a new env var for shape-only. Why: §5 and §4 (no new env var needed); the fail-closed default already posts the shape when unsure. Applied in: no code change. Status: pending review

## Notes

- Security pass: skip (`security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Coordination: #5125, #5126, and #5127 change other functions of the same file on the same base branch; whichever merges first, the others merge it at their step 2 sync.

## References

- Issue #5124; security audit tracker #3576; issue #4755 project (final PR #4773) and its plan `docs/plans/issue-4755-report-blocking-permission-prompts-plan.md`.
