<!-- changelog: security -->
- Review/autofix now stages executable support only from the reusable workflow's immutable definition SHA, avoids privileged installation of PR dependencies, and runs the editor as an unprivileged process with an allowlisted environment and protected Git/runner control paths.
- Review-blocked successor issues now require authenticated intent plus creator, label, action, type, head, and payload-digest validation; refusal latches require a keyring-signed V2 envelope while retaining V1 markers only for compatibility.
