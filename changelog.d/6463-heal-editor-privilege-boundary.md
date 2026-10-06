<!-- changelog: fixed -->
- **Workflow failure heal no longer exposes repository credentials to its editor.** Heal issues now receive verified, redacted failure evidence, an intake-authored file scope and a disposable editor container. An unverified scope stops implementation at `ai:needs-human` instead of running an untrusted editor on the host.
