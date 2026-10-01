# v0.1 platform audit

Audited baseline: 667bce02c814ea3fe32d4312c32cb9b32e845fe7.

The common execution path already enforces action policies, validates schemas, scopes account selection and artifacts, redacts environment credentials and bounds persisted results. HTTP, Python, fixed local executables and imported MCP use this path. These are preserved.

## Gaps to repair

- Capability labels have no argument or measurement compatibility contract. Search cannot determine whether a connected source satisfies a request.
- Asset kinds are a closed enum; assets do not bind to accounts or telemetry.
- Results lack explicit provider/site/asset/time coverage and original-unit fields.
- Search rebuilds every document on every query and omits live connection availability.
- Environment-only credentials lack refresh, revocation, verification and encrypted persistence.
- The HTTP MCP server has a fixed session and no authenticated multi-user ingress. Session IDs are caller-owned SDK objects; they are not authentication credentials.
- Skills describe operations but cannot run them. Several important cost, baseline and comparison workflows are absent.
- Summaries return zero for entirely missing columns. Counter, power integration and missing-coverage analysis are missing.
- SDK batching accepts less strict shapes than MCP batching. Arbitrary provider error strings are safely suppressed but there is no structured execution telemetry.
- Imported MCP has safe defaults but no inspected schema digest, explicit review/version lifecycle or drift contract.
- Deterministic MCP workflows are tested; autonomous model behavior is unmeasured.

## Verification boundaries

Private HTTP parsers are fixture-qualified. Public APIs have recorded live probes. Engineering tools execute real installed numerical libraries. Operator executables are trusted processes, not a sandbox. Multi-user production readiness requires ingress identity, durable scoped sessions, rate/resource limits, credential separation and deployment guidance beyond a loopback example.
