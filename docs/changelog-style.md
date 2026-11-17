# CHANGELOG release-summary style guide

This guide applies to new `CHANGELOG.md` entries. It is going-forward only, and it keeps the existing Keep a Changelog section layout intact.

## Entry structure

Each new entry should follow this order when the information exists:

1. **Headline**
   - Open with 1 to 2 sentences.
   - Name what shipped and the main user-visible change.
2. **Lead paragraph**
   - Write 3 to 5 sentences.
   - Lead with what changed for users or operators, not with implementation detail.
   - Name the real workflow, command, file, issue, or path when it helps the reader.
3. **The numbers that matter**
   - Add a short table when the change has real measurable details.
   - Use exact counts, limits, schedules, file paths, or issue numbers.
   - Omit the table when nothing measurable improves the entry.
4. **Audience closing**
   - End with a short "What this means for <audience>" paragraph.
   - Make the operational takeaway explicit.
5. **For contributors**
   - Add this final subsection only when contributor-facing detail would distract from the lead.
   - Keep implementation notes, follow-up details, or operator-only caveats here.

## Voice rules

- Keep the lead user-facing. Put contributor-only detail at the bottom.
- Use real numbers, real filenames, real workflow names, and real labels when they matter.
- Keep claims concrete and verifiable.
- Prefer commas or periods where an em dash would only add drama.
- Avoid AI-generic filler vocabulary and stock phrases, including:
  - `delve`
  - `robust`
  - `comprehensive`
  - `nuanced`
  - `fundamental`
  - `Here's the kicker`
  - `The bottom line`

## Do not do this

- Do not mention branch-internal version bumps unless they changed shipped behavior.
- Do not narrate the PR's revision history.
- Do not post-hoc rationalize why the scope ended up where it did.
- Do not invent numbers, vague placeholders, or generic filenames.

## Lightweight template

Use this as a starting point for future entries:

```md
- <Headline sentence. Optional second sentence.>

<Lead paragraph of 3 to 5 sentences. Start with user or operator impact. Include exact workflow names, paths, numbers, and filenames when they matter.>

| The numbers that matter | Value |
| --- | --- |
| <metric> | <real value> |

What this means for <audience>: <closing paragraph.>

### For contributors

<Optional contributor-only details that do not belong in the lead.>
```
