<!-- changelog: security -->
- **Semble runs in a credential-free sandbox.** Optional Semble installation now builds a hash-locked container instead of installing packages on the runner. Indexing and queries run without network access or inherited credentials, using a repository snapshot without Git metadata. If Docker or the sandbox is unavailable, workflows continue without Semble context.
