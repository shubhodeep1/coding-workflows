<!-- changelog: security -->
- **Claude read roles cannot write through `gh api`.** Read-profile model processes omit write-capable GitHub and other runner credentials, and their hook approves only GET requests to REST endpoints on `github.com`. Refs #6216; related to #3576.

Read-role audits no longer expose the security-audit job's issue-write and workflow-dispatch credentials to model tools. An empty per-run `gh` config and cleared `GH_HOST` prevent saved logins and host overrides from restoring access. API writes, absolute URL endpoints, GraphQL calls, HEAD requests and unapproved compound commands are denied by the read-profile hook, including in echo substitutions. Other read-only GitHub commands remain available when they do not require authentication; private-repository reads are unavailable until a separately provisioned read-only identity is safely wired in. Write-profile and interactive sessions retain their existing policy.

What this means for operators: a read-role model cannot use the job's privileged token to change GitHub state, while private-repository lookups need a dedicated read-only credential before they can succeed.
