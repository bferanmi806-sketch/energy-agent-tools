# Scope and limitations

This project is a self-hosted integration platform. The operator owns credentials,
provider permissions, approved models, host configuration and backups.

- Authenticated hosting uses operator-provisioned bearer digests and fixed user/site
  mounts. It has expiry, revocation, rotation, request bounds and session limits.
  Public TLS, organizational identity, billing and dynamic tenant provisioning
  require deployment infrastructure. Mount topology changes require a restart.
- The encrypted vault needs an operator key. It is not a managed KMS. OAuth PKCE,
  callback, refresh and revocation are implemented; provider registration and
  entitlement remain operator responsibilities. A failed one-time code exchange
  requires a new authorization attempt. Dynamic client registration is absent.
- Generic execution requires reviewed bindings. A matching capability label does
  not establish interchangeable quantities, windows or arguments. Unit conversion
  is explicit. Grid generation cannot stand in for a building's PV meter.
- Search is an indexed lexical system with energy synonyms. The reproducible
  10,000-action benchmark measures eight curated intents, not universal relevance.
  Learned ranking, embeddings and automatic translation are absent.
- Twelve executable workflows use bounded inputs and preserve evidence. They
  need compatible sources and model parameters. Anomaly screening cannot establish
  cause. Tariff cost excludes standing charges unless separately supplied.
- Artifacts are private to user and session, with size quotas, retention and scoped
  deletion. Processes, plugins and executable adapters remain trusted operator
  integrations. CPU-heavy numerical work is not an isolated job service.
- HTTP contract fixtures verify request construction, parsing and failure handling.
  They do not verify a private installation or paid-service entitlement. Public
  probes and real local numerical tests are recorded separately.
- The real-agent benchmark uses actual Codex model calls and synthetic source data.
  Its automated scoring is heuristic. One model and one small fixture cannot
  establish reliability across providers, seasons, sites or deployment conditions.
- PyPSA and pandapower are bounded steady-state AC studies. pandapipes is hydraulic
  flow. Protection, transients, unbalanced switching, thermal transport and device
  dispatch are outside these contracts. EnergyPlus has boundary fixtures; a real
  installed engine/model qualification is still required.
- OpenDSS, OpenStudio and vendor PowerMCP tools are not validated integrations.
  An operator may import reviewed MCP tools, but that does not qualify upstream
  software or physical-control safety.
- The MIT licence applies to gateway code. Provider licences, data attribution,
  service limits and dependency licences still apply.

Broader private-account qualification, published numerical reference models,
load testing, multiple-model evaluation and long-running operational evidence
remain necessary before offering a mature managed service.
