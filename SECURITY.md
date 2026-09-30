# Security

Use the service locally over stdio or loopback. Public ingress is outside the
current security model. One process is one trusted operator context; user IDs
passed to the Python SDK must originate from your authenticated application.

Configure credential environment references outside agent context. Do not place
secrets in URLs, command arguments, MCP registration metadata or account settings.
The runtime redacts known configured credential values, but this is defense in
depth rather than permission to return credentials from connectors.

Imported schemas, descriptions and responses are untrusted content for an LLM.
Review imported actions and semantics. Never grant control permissions solely
because a remote server labels an action read-only. Review executable argv and
MCP process configuration as you would executable application code.

SQLite state directories contain private results. The service creates directories
with mode 0700 and the artifact database with mode 0600 on POSIX. Keep backups
private. Local CSV imports are constrained to an operator-approved directory,
including symlink resolution. Sensitive credentials and meter data must not be
committed to Git.

Please report vulnerabilities privately using GitHub's security reporting
mechanism when available, or contact the repository owner without publishing
secrets or exploit data. Do not test against real infrastructure without consent.
