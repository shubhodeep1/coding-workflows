<!-- changelog: fixed -->
- Dependency installs for isolated implement and review agents can no longer reach arbitrary network services from attacker-controlled build backends.

Both dependency containers now run with `--network none`. An allowlisted HTTPS CONNECT proxy on the host permits only public registry addresses. Repositories can replace the default PyPI/npm/Yarn allowlist with `DEPENDENCY_PROXY_ALLOWED_HOSTS`; unlisted or privately resolved mirrors fail to install rather than falling back to unrestricted egress. On runners that require an upstream corporate proxy, installs may be unavailable and affected checks remain unverified.
