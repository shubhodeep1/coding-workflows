<!-- changelog: fixed -->
- **Invalid UTF-8 no longer breaks prompt rendering or prompt-input truncation.** `load_prompt` in `scripts/render_prompt.py` now replaces invalid UTF-8 bytes in a prompt file with U+FFFD and prints one stderr warning (`WARNING: prompt file '<path>' contains invalid UTF-8 at byte <N>; replaced with U+FFFD`) instead of exiting with an uncaught `UnicodeDecodeError` traceback; valid prompts render byte-identically. `_embed_input_file` in `scripts/gh_helpers.sh` now steps a `head`-mode truncation back to a UTF-8 character boundary (at most 3 bytes, via the new private `_embed_utf8_safe_cut` helper), so a truncated prompt input never ends mid-character; the `[... TRUNCATED ...]` marker and the prompt budget count the bytes actually shown.

A persona source (`prompts/_prelude_role_persona.txt`) or a prompt contract that is not valid UTF-8 stays a hard error, because both are trusted config, but it now prints one `ERROR: Unable to decode persona source '<path>' as UTF-8: ...` or `ERROR: Unable to decode contract '<path>': ...` line instead of a traceback. The existing `Unable to read ...` messages are unchanged.

`scripts/render_prompt.sh` now reads the prompt for its placeholder scan with `errors="replace"` (placeholder names are ASCII, so replacing bad bytes cannot invent or hide one). A non-UTF-8 prompt therefore renders through `render_prompt.py` with the one `WARNING:` line above and exits 0, and env-derived placeholders in a trusted template still render. This supersedes the exit-1 `ERROR: collect_prompt_placeholders UnicodeDecodeError:` behaviour described in `changelog.d/9341-render-prompt-readable-errors.md`, and for `scripts/render_prompt.py` the exit-1 `ERROR: Unable to decode prompt file ...` behaviour; the `ERROR: collect_prompt_placeholders` line still reports a prompt that cannot be read.

The fallback `_embed_input_file` in `scripts/review_rb_judge.sh` (used only when `gh_helpers.sh` is not loaded) now steps its cut back to a UTF-8 character boundary by at most 3 bytes, like `_embed_utf8_safe_cut`, so a malformed run of continuation bytes can no longer empty the prefix. The emitted bytes, the `[... TRUNCATED ...]` marker and the prompt budget all use the trimmed count, and it falls back to the plain byte cut if `python3` fails.

A regression test now locks in that the `REVIEW_HEAD_CHECKOUT` timers in `scripts/review_autofix_step_checkout_pr_head_branch.sh` read bash's `EPOCHREALTIME` before `date`, so a `date` clock that goes backwards cannot make `fetch_ms` / `switch_ms` negative; the comment there now explains the order.

Refs #9261
Refs #9375
Refs #9949
