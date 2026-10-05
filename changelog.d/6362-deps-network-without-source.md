<!-- changelog: fixed -->
- Isolated implementation agents no longer expose repository source files to the networked dependency installer.

Dependency preparation stages only allowlisted Node manifests and filtered third-party Python requirements for the networked install. Editable source builds run in a separate container without network access; build outputs remain confined to the disposable sandbox.
