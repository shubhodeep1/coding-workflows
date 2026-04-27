# CLAUDE.md — Project Instructions

These instructions are **mandatory** and must be followed **before any action**.

The authoritative rules live in `codex_system_instructions.md`. Read it in addition to this file. The sections below mirror that file's structure so that `§`-references used across `.github/workflows/`, `scripts/`, and `prompts/` (e.g. "CLAUDE.md §15") continue to resolve to the same rule. Each section here points at the canonical body in `codex_system_instructions.md`.

> Mode boundary: in **non-interactive / unattended** runs (PR autofix, Reviewer/Aggregator/Editor pipelines), `unattended_llm_system_instructions.md` overrides §2 below. See its §0 (Execution Mode) and §4 (Unattended Decision Policy). Interactive Claude sessions continue to follow §2 here.

---

## PRE-TASK MANDATORY CONTEXT LOADING

Before any task, read:
- `README.md`
- `agents.md`
- `codex_system_instructions.md`
- all `/db/contracts/*.yml` (or `.json`) relevant to collections that may be touched

If any are missing or unclear: **STOP and ask using the mandatory Q/A format.**
Never assume undocumented behavior.

---

## 0. Prime Directive (NON-NEGOTIABLE)

See `codex_system_instructions.md` §0.

---

## 1. Core Priorities (Strict Order)

See `codex_system_instructions.md` §1.

---

## 2. Always-On Ask-First Mode

See `codex_system_instructions.md` §2 (includes the mandatory `Q1`/`Q2` question format).
For non-interactive runs see `unattended_llm_system_instructions.md` §0 and §4 (which override this section).

---

## 3. Production Code Assumptions

See `codex_system_instructions.md` §3.

---

## 4. Environment Variables

See `codex_system_instructions.md` §4.

---

## 5. Minimal Change Set

See `codex_system_instructions.md` §5.

---

## 6. Backward Compatibility / Naming Immutability

See `codex_system_instructions.md` §6.

Section numbers in this file are also covered by §6 — they are referenced from `.github/workflows/`, `scripts/`, and `prompts/` and must not be renumbered.

---

## 7. Output Requirements

See `codex_system_instructions.md` §7.

---

## 8. Debugging & Diagnostics

See `codex_system_instructions.md` §8.

---

## 9. Code Style

See `codex_system_instructions.md` §9.

---

## 10. MongoDB Rules

See `codex_system_instructions.md` §10 (subsections A–H).

---

## 11. Task Checklist Completion Gate

See `codex_system_instructions.md` §11.

---

## 12. PR Review Mode

See `codex_system_instructions.md` §12.

---

## 13. Repository Hygiene

See `codex_system_instructions.md` §13.

---

## 14. Consumer Repo Registry

See `codex_system_instructions.md` §15.

---

## 15. GitHub API Call Hygiene (MANDATORY)

See `codex_system_instructions.md` §14.

---

## FINAL REMINDER

If uncertainty exists: **ASK (multiple-choice). DO NOT EXECUTE.**

Accuracy > speed. Safety > convenience. Backward compatibility is mandatory.
