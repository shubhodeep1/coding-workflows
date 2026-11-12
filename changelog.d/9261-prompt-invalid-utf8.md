<!-- changelog: fixed -->
- **Invalid UTF-8 no longer breaks prompt rendering or prompt-input truncation.** `load_prompt` in `scripts/render_prompt.py` now replaces invalid UTF-8 bytes in a prompt file with U+FFFD and prints one stderr warning (`WARNING: prompt file '<path>' contains invalid UTF-8 at byte <N>; replaced with U+FFFD`) instead of exiting with an uncaught `UnicodeDecodeError` traceback; valid prompts render byte-identically. `_embed_input_file` in `scripts/gh_helpers.sh` now steps a `head`-mode truncation back to a UTF-8 character boundary (at most 3 bytes, via the new private `_embed_utf8_safe_cut` helper), so a truncated prompt input never ends mid-character; the `[... TRUNCATED ...]` marker and the prompt budget count the bytes actually shown.

The fallback UTF-8 trim in `scripts/review_rb_judge.sh` was dropped from this change; that file is unchanged.

Refs #9261
