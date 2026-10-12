<!-- changelog: changed -->
- **Fixes from the unblock judge's `operator_step` verdict now ship switched on, and page a person only when a person must act.**

Until now every `operator_step` fix-up put its new code behind a flag that defaulted off. It also wrote an `ai:operator-step` entry and sent a Telegram "Operator step needed" WARNING asking someone to turn the flag on. Fixes landed dormant: the triage integration-branch line (#6710), bounded review dependency installs (#6793), and the stable release switch (#7029). One heal (#7171) produced a fix-up that waited on a maintainer grant only to add another off switch. A flag ending in `_ENABLED` is now an off switch on code that ships on: `false` turns it off, and nothing is recorded or paged. Only a placeholder ending in `_UNSET_OPERATOR_STEP`, for a secret, credential, repository setting or release decision a person must supply, still writes the operator step and sends the WARNING.

What this means for operators: fewer "Operator step needed" pages, and the ones that remain need you. Fixes the pipeline can finish itself start working when they merge.
