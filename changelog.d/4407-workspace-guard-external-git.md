<!-- changelog: fixed -->
- **Workspace reconciliation works in copied workspaces without `.git`.** The credentialless workspace guard now validates and uses the caller's external Git metadata to classify ignored files. Other sandbox roles remain isolated from that Git context, and invalid metadata fails closed.
