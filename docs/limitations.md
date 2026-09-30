# Scope and limitations

This is an initial self-hostable release, not an operated integration cloud.

- One MCP process serves one configured user/site. HTTP binds to loopback and has
  no built-in public ingress authentication. Use stdio locally; a network
  deployment needs an authenticated gateway and separate user-scoped services.
- Secrets are environment references, never agent tool arguments. An OS process
  administrator can read the process environment. No encrypted vault is claimed.
- OAuth account metadata and externally provisioned tokens are supported as
  credential inputs; interactive login, refresh, revocation, PKCE, dynamic client
  registration and per-provider OAuth lifecycle are deferred.
- Search uses lexical ranking and a small energy synonym map. No learned ranking,
  capability substitution planner or automatic parameter translation is claimed.
- Skills are guidance, not an autonomous scheduler. Tools with the same energy
  capability may require different arguments and represent different physical
  quantities. Cumulative counters require conversion before interval analysis.
- Workbench operations are bounded and local. Arbitrary Python runs only through
  the trusted SDK. Executable and imported MCP processes are trusted operator
  integrations, not isolated sandboxes. SQLite artifacts have no automatic TTL;
  operators should delete old state directories or use the scoped deletion API.
- A time series envelope has one primary unit. Weather and multi-quantity model
  results carry field-specific units in their data or provenance. Downstream
  code must check these rather than infer units from column names.
- Connector contract fixtures prove parsing, auth injection and error handling.
  They do not prove access to a real private smart meter or Home Assistant
  installation. Public live probes are separately recorded in verification.
- Reference agent workflows use a deterministic MCP client. No paid LLM-provider
  credentials were used, and no autonomous-model benchmark is claimed.
- Balanced AC power flow is a steady-state study, not protection, transient,
  unbalanced or switching validation. PV estimates omit system effects described
  in their assumptions. Charging plans are advice and cannot dispatch devices.
- Provider licences, data attribution and rate limits still apply. An MIT gateway
  does not relicense fetched energy datasets or proprietary engineering tools.

Deferred integrations are listed with reasons in the connector catalogue. Add
production OAuth and auth-at-ingress before offering this as a multi-user service.
For engineering, add reviewed exchange models, unbalanced systems and validation
against published reference networks before expanding solver coverage.
