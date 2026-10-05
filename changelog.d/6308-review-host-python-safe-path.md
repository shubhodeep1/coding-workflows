<!-- changelog: fixed -->
- **Review autofix no longer imports PR files as host Python modules.** The credential-bearing review job excludes the PR checkout and its workspace copy from Python's implicit import path. Workspace initialization also uses isolated Python with bytecode disabled, and the memory CLI loads its sibling modules from the trusted support directory.
