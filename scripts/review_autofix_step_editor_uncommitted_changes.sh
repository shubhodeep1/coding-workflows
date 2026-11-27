#!/usr/bin/env bash
# Body of the "Detect editor-claimed-but-uncommitted changes" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail

EDITOR_CHANGES_LOST="false"

if [ -s "${EDITOR_SUMMARY_FILE:-}" ]; then
  # Prefer the authoritative "Change status:" section emitted by
  # the editor prompt (see scripts/review_apply_fixes.sh editor
  # response format). The section contains exactly one bullet
  # with value "edited" or "not-edited" (or "not edited",
  # normalized to "not-edited"). Capture both the
  # below-header bullet form and any inline value on the header
  # line ("Change status: edited") to be robust to minor
  # formatting drift.
  change_status_section="$(awk '
    /^[[:space:]]*Change status:/ {
      header=$0
      sub(/^[[:space:]]*Change status:[[:space:]]*/, "", header)
      if (header != "") print header
      in_section=1
      next
    }
    in_section && /^[[:space:]]*[A-Za-z].*:/ { exit }
    in_section { print }
  ' "${EDITOR_SUMMARY_FILE}")"
  change_status="$(printf '%s\n' "${change_status_section}" | grep -iE '^[[:space:]]*-?[[:space:]]*(edited|not-edited|not[[:space:]]+edited)[[:space:]]*$' | head -1 | sed -E 's/^[[:space:]]*-?[[:space:]]*//; s/[[:space:]]*$//; s/^not[[:space:]]+edited$/not-edited/I' | tr '[:upper:]' '[:lower:]' || true)"

  # Extract the "Changes made:" section, stopping at the next
  # top-level section header (line starting with a non-dash
  # alphabetic word followed by a colon). Used both for the
  # heuristic fallback below and for the error-context printout
  # when the authoritative signal reports "edited".
  changes_section="$(awk '
    /^[[:space:]]*Changes made:/ { in_section=1; next }
    in_section && /^[[:space:]]*[A-Za-z].*:/ { exit }
    in_section { print }
  ' "${EDITOR_SUMMARY_FILE}")"

  edit_claim_regex='\b(modif(y|ied|ies|ying)|updat(e|ed|es|ing)|change(d|s|ing)?|add(ed|s|ing)?|remov(e|ed|es|ing)|delet(e|ed|es|ing)|renam(e|ed|es|ing)|creat(e|ed|es|ing)|fix(ed|es|ing)?|patch(ed|es|ing)?|implement(ed|s|ing)?|refactor(ed|s|ing)?|tweak(ed|s|ing)?|adjust(ed|s|ing)?|improv(e|ed|es|ing)|resolv(e|ed|es|ing))\b'
  strong_edit_claim_regex='\b(modif(y|ied|ies|ying)|change(d|s|ing)?|updat(e|ed|es|ing)|add(ed|s|ing)?|remov(e|ed|es|ing)|delet(e|ed|es|ing)|renam(e|ed|es|ing)|creat(e|ed|es|ing)|fix(ed|es|ing)?|patch(ed|es|ing)?|implement(ed|s|ing)?|refactor(ed|s|ing)?|tweak(ed|s|ing)?|adjust(ed|s|ing)?|improv(e|ed|es|ing)|resolv(e|ed|es|ing))\b'
  no_change_phrase_regex='no ((repository|repo|code|file)[[:space:]]+)?(file[[:space:]]+)?(changes?|modifications?|edits?)([[:space:]]+(were|was|are|is))?[[:space:]]+(required|needed|made|necessary)|no[[:space:]]+(repository[[:space:]]+)?files[[:space:]]+(were[[:space:]]+)?modified|no[[:space:]]+changes[[:space:]]+(were[[:space:]]+)?made|no[[:space:]]+modifications|no[[:space:]]+(repository[[:space:]]+)?files[[:space:]]+(are|is)[[:space:]]+modified|no[[:space:]]+(repository[[:space:]]+)?files[[:space:]]+(were[[:space:]]+)?changed|no[[:space:]]+changes\b'
  no_change_declaration_regex="^[[:space:]]*(-[[:space:]]*)?([^;:,]*[;:,-][[:space:]]*)?(${no_change_phrase_regex})([[:space:]]*[[:punct:]]*)?"

  has_real_changes="false"
  if [ "${change_status}" = "edited" ] || [ "${change_status}" = "not-edited" ]; then
    # Authoritative path: trust the editor's self-reported status.
    # The free-text heuristic below is only consulted when the
    # "Change status:" line is missing or not one of the two
    # known values (older cached summaries or malformed output).
    if [ "${change_status}" = "edited" ]; then
      has_real_changes="true"
    elif [ -n "${changes_section}" ] && \
      printf '%s\n' "${changes_section}" | grep -viE '^[[:space:]]*$|^[[:space:]]*-[[:space:]]*none([[:space:][:punct:]]|$)|^[[:space:]]{2,}-|^[[:space:]]*-[[:space:]]*(Validation executed|Validation limitation|Ran [^:]*(validation|check|test)|Assumptions?( applied| made)|Missing[- ]context)' | grep -viE "^[[:space:]]*-[[:space:]]*(${no_change_phrase_regex})([[:space:][:punct:]]*)$" | grep -qiE "${edit_claim_regex}"; then
      echo "::warning::Editor reported Change status: not-edited but Changes made contains edit-like claims; trusting Change status."
    fi
  elif [ -n "${changes_section}" ]; then
    # Fallback heuristic: used only when the authoritative
    # "Change status:" signal is missing/malformed.
    # If the first non-blank line is a "- none" variant or an
    # explicit "no (repository) files were modified / no changes
    # made / no modifications" declaration (with any trailing
    # description), treat the entire section as "no changes".
    # Editors may append informational bullets (validation runs,
    # missing-context notes, action summaries with file mentions)
    # below such declarations that are not real file-change claims;
    # without this guard those bullets trigger a false-positive
    # EDITOR_CHANGES_LOST error because edit_claim_regex loosely
    # matches generic edit-like substrings (for example `change`)
    # in informational text.
    first_line="$(printf '%s\n' "${changes_section}" | grep -vE '^[[:space:]]*$' | head -1 || true)"
    # Strip lines that are blank, match "- none*", are indented
    # nested sub-bullets (continuations of validation/assumption
    # headers), or are informational metadata bullets
    # (e.g. "- Validation executed").
    substantive="$(printf '%s\n' "${changes_section}" | grep -viE '^[[:space:]]*$|^[[:space:]]*-[[:space:]]*none([[:space:][:punct:]]|$)|^[[:space:]]{2,}-|^[[:space:]]*-[[:space:]]*(Validation executed|Validation limitation|Ran [^:]*(validation|check|test)|Assumptions?( applied| made)|Missing[- ]context)' || true)"
    # Stricter bullet-start edit regex for multi-line contradiction checks.
    bullet_edit_regex='^[[:space:]]*-[[:space:]]*(modif(y|ied|ies|ying)|updat(e|ed|es|ing)|change(d|s|ing)?|add(ed|s|ing)?|remov(e|ed|es|ing)|delet(e|ed|es|ing)|renam(e|ed|es|ing)|creat(e|ed|es|ing)|fix(ed|es|ing)?|patch(ed|es|ing)?|implement(ed|s|ing)?|refactor(ed|s|ing)?|tweak(ed|s|ing)?|adjust(ed|s|ing)?|improv(e|ed|es|ing)|resolv(e|ed|es|ing))([[:space:][:punct:]]|$)'
    if printf '%s\n' "${first_line}" | grep -qiE "^[[:space:]]*-[[:space:]]*(${no_change_phrase_regex})([[:space:][:punct:]].*)?$"; then
      # Explicit "no files modified / no changes made / no
      # modifications" first-bullet is a deliberate declaration
      # from the editor. Trust it and short-circuit to "no real
      # changes" — the remainder check below is intentionally
      # conservative and can match generic edit-like words in
      # purely informational bullets, and
      # would fire a false-positive EDITOR_CHANGES_LOST error.
      # Same-line contradictions ("no files modified but updated
      # X") and multi-line explicit edit bullets are still caught.
      first_line_suffix="$(printf '%s\n' "${first_line}" | sed -E "s/^[[:space:]]*-[[:space:]]*(${no_change_phrase_regex})[[:space:][:punct:]]*//I")"
      if printf '%s\n' "${first_line_suffix}" | grep -qiE "${edit_claim_regex}"; then
        has_real_changes="true"
      elif [ -n "${substantive}" ]; then
        if printf '%s\n' "${substantive}" | grep -qiE "(${no_change_phrase_regex}).*(but|however|yet|although|still|nevertheless).*(${edit_claim_regex})"; then
          has_real_changes="true"
        else
          # Multi-line contradiction detection: check remaining
          # bullets for explicit edit verbs at bullet start.
          remainder="$(printf '%s\n' "${substantive}" | grep -viE "${no_change_declaration_regex}" || true)"
          if printf '%s\n' "${remainder}" | grep -qiE "${bullet_edit_regex}"; then
            has_real_changes="true"
          fi
        fi
      fi
    elif printf '%s\n' "${first_line}" | grep -qiE '^[[:space:]]*-[[:space:]]*none([[:space:][:punct:]]|$)'; then
      # Preserve contradictory summaries such as "- none" followed
      # by "- created ...".
      if [ -n "${substantive}" ]; then
        if printf '%s\n' "${substantive}" | grep -qiE "((${strong_edit_claim_regex}).*(${no_change_phrase_regex})|(${no_change_phrase_regex}).*(${strong_edit_claim_regex}))"; then
          has_real_changes="true"
        elif printf '%s\n' "${substantive}" | grep -qiE "(${no_change_phrase_regex}).*(but|however|yet|although|still|nevertheless).*(${edit_claim_regex})"; then
          has_real_changes="true"
        else
          remainder="$(printf '%s\n' "${substantive}" | grep -viE "${no_change_declaration_regex}" || true)"
          if printf '%s\n' "${remainder}" | grep -qiE "${edit_claim_regex}"; then
            has_real_changes="true"
          fi
        fi
      fi
    else
      # Strip lines that are blank, match "- none*", are indented
      # nested sub-bullets (continuations of validation/assumption
      # headers), or are informational metadata
      # (e.g. "- Validation executed: ...") that the editor may
      # emit when no files changed. Keep explicit "no changes"
      # lines for contradiction detection.
      substantive="$(printf '%s\n' "${changes_section}" | grep -viE '^[[:space:]]*$|^[[:space:]]*-[[:space:]]*none([[:space:][:punct:]]|$)|^[[:space:]]{2,}-|^[[:space:]]*-[[:space:]]*(Validation executed|Validation limitation|Ran [^:]*(validation|check|test)|Assumptions?( applied| made)|Missing[- ]context)' || true)"
      if [ -n "${substantive}" ]; then
        if printf '%s\n' "${substantive}" | grep -qiE "((${strong_edit_claim_regex}).*(${no_change_phrase_regex})|(${no_change_phrase_regex}).*(${strong_edit_claim_regex}))"; then
          has_real_changes="true"
        elif printf '%s\n' "${substantive}" | grep -qiE "${no_change_phrase_regex}"; then
          # Preserve contradictory mixed lines ("no changes, but
          # updated ...") so they still count as real change claims.
          if printf '%s\n' "${substantive}" | grep -qiE "(${no_change_phrase_regex}).*(but|however|yet|although|still|nevertheless).*(${edit_claim_regex})"; then
            has_real_changes="true"
          else
            # Remove standalone no-modification declarations, but
            # still detect mixed sections that claim concrete edits.
            remainder="$(printf '%s\n' "${substantive}" | grep -viE "${no_change_declaration_regex}" || true)"
            if printf '%s\n' "${remainder}" | grep -qiE "${edit_claim_regex}"; then
              has_real_changes="true"
            fi
          fi
        else
          if printf '%s\n' "${substantive}" | grep -qiE "${edit_claim_regex}"; then
            has_real_changes="true"
          fi
        fi
      fi
      # Echo retained substantive content whenever this fallback
      # branch sees non-empty filtered lines. This makes future
      # false-positive investigations tractable — reviewers can see
      # exactly which bullets were considered.
      if [ -n "${substantive}" ]; then
        echo "Substantive bullets retained by detector regex:"
        printf '%s\n' "${substantive}" | head -20
      fi
    fi
  fi

  if [ "${has_real_changes}" = "true" ]; then
    # Defense-in-depth: before firing the warning and blocking
    # auto-merge, re-check the working-tree state and the
    # narrative "Changes made:" block via the shared shim. If
    # both indicate no real edits, downgrade to an
    # informational log line and do not block auto-merge.
    # Keeps the hard failure only when there is real evidence
    # of lost edits. See: fun-token-multi-chain PR #117.
    #
    # The script is bootstrapped to ${SUPPORT_SCRIPTS_DIR} by the
    # bootstrap step (REQUIRED_BOOTSTRAP_SCRIPTS).  Reading from
    # ${GITHUB_WORKSPACE}/scripts/ would silently skip the recheck
    # in every consumer repo (only the workflow-source repo
    # vendors this path), which is how the bitsafe.io PR #177 /
    # run 25653654000 false positive escaped this safety net.
    recheck_script="${SUPPORT_SCRIPTS_DIR}/detect_editor_changes_lost.sh"
    if [ -x "${recheck_script}" ]; then
      recheck_result="$(bash "${recheck_script}" "${EDITOR_SUMMARY_FILE}" 2>/dev/null || echo "true")"
      if [ "${recheck_result}" = "false" ]; then
        echo "Notice: Change status: reported edited but working tree is clean and narrative claims no concrete changes; treating as false positive (no warning, auto-merge not blocked)."
        has_real_changes="false"
      fi
    fi
  fi

  if [ "${has_real_changes}" = "true" ]; then
    if [ "${CAN_PUSH:-}" = "true" ]; then
      echo "::error::Editor claimed changes but no commit was produced. This indicates a mismatch between the editor summary and final git state (changes may have failed to persist, or may have been discarded during commit validation)."
    else
      echo "::error::Editor claimed changes but no commit was produced. Push is disabled (CAN_PUSH=${CAN_PUSH:-false}), so this does not necessarily indicate a tool persistence failure."
    fi
    echo "Editor-claimed changes:"
    printf '%s\n' "${changes_section}" | head -20
    EDITOR_CHANGES_LOST="true"
  fi
fi

echo "EDITOR_CHANGES_LOST=${EDITOR_CHANGES_LOST}" >> "$GITHUB_ENV"
