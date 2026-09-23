<!-- changelog: fixed -->

- Review writer paths now run the digest-pinned workspace guard directly in a minimal credentialless environment, preventing nested systemd setup failures from blocking editor, resolver, and judge-fix attempts while model processes remain sandboxed.
