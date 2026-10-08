# Bounded release backend audit

## Scope and finding

This audit covers gateway discovery and execution across built-in providers,
composed workflows, managed MCP connections, authorization, and restart
recovery. The product boundary is an energy data and engineering gateway: it
discovers provider tools, applies the caller's scope and action policy, and
returns evidence-bearing results. It does not autonomously operate a site.

The inspected backend has one shared `EnergyAgent` policy and execution runtime
behind several purpose-specific MCP calls. `ENERGY_SEARCH_TOOLS` returns scoped
tool schemas and matching workflows; `ENERGY_MULTI_EXECUTE_TOOL` handles
canonical provider and workbench calls; reviewed capability bindings and
composed workflows have explicit entry points. The pending `server.py` change
clarifies these routes and the difference between discovery tags and reviewed
bindings. It changes descriptions and instructions only.

No backend logic defect was verified within this bounded scope. A missing MCP
integration check was found: provider substitution and workflow execution were
covered separately, but the same composed workflow had not been driven through
the MCP interface against all three provider contracts. The acceptance test in
[`tests/test_release_gateway_journey.py`](../tests/test_release_gateway_journey.py)
now covers that boundary and verifies a cross-user account request is rejected
before provider I/O.

## Backend evidence

- [`src/energy_agent_tools/server.py`](../src/energy_agent_tools/server.py)
  exposes bounded discovery, exact-schema lookup, ordered batches of 1–20
  canonical calls, reviewed capability resolution/execution, listed workflows,
  and simulation job controls. Provider and workbench calls share the batch
  executor. Workflows remain a separate explicit operation because they
  compose multiple reviewed reads and local analysis.
- [`src/energy_agent_tools/runtime.py`](../src/energy_agent_tools/runtime.py)
  filters discovery through session scope, validates the registered tool
  schema, checks action policy and account ownership, resolves credentials
  locally, and invokes the provider only after these checks. Batch failures
  are returned per call. Capability labels used for search do not by
  themselves create an executable binding.
- [`src/energy_agent_tools/workflows.py`](../src/energy_agent_tools/workflows.py)
  resolves sources for deterministic recipes and runs provider reads and
  workbench analysis through the same runtime. The API preserves source kind,
  site and provenance in derived results.
- [`src/energy_agent_tools/managed_mcp.py`](../src/energy_agent_tools/managed_mcp.py)
  approves and pins remote targets, stages selected tools as disabled until
  site mapping, checks their schema digest on import, and publishes an owned
  connection namespace. Recovery removes the old namespace first, restores
  active connections only after re-discovery and schema validation, and checks
  that the connection revision did not change before publication. Offline or
  changed connections remain unavailable.
- [`docs/managed-workspaces.md`](managed-workspaces.md) and
  [`docs/custom-mcp-onboarding.md`](custom-mcp-onboarding.md) state the current
  product limits: managed custom MCP OAuth and automatic MCP authorization
  discovery are not available. Supported custom MCP connections use configured
  authentication, explicit tool review, and approved endpoint policy.

The literal MCP surface is not one RPC method: direct tools, capabilities,
workflows, and durable jobs have different contracts. They share the scoped
runtime and authorization checks. Clients should use multi-execute for a
discovered canonical provider/workbench tool, run-skill for a listed composed
workflow, and the job interface for durable numerical work.

## Verification

The MCP release journey uses generated Octopus, OpenEnergyMonitor, and local CSV
fixtures. For each provider it discovers a source tool, finds the electricity
cost workflow, runs it through `ENERGY_RUN_SKILL`, checks the independently
known cost and source provenance, then confirms a Bob-scoped MCP call cannot
read Alice's Octopus account or send another provider request.

Related existing suites cover the real stdio MCP endpoint and workbench
execution, multiple provider workflows, managed MCP credentials and scoped
dispatch, tool visibility and schema drift, managed OAuth revocation cleanup,
and connection lifecycle races. The combined selected run, including the new
journey test, completed **57 passed**. These are synthetic tests. They do not
qualify a physical meter, an outside-authored connector, production-scale
load, an external identity provider, or a deployment soak.

## Follow-up boundary

No backend fix is recommended for this release slice. Keep generic custom MCP
OAuth and automatic authorization discovery as an explicit future feature;
do not imply that a manually configured custom MCP connection supports OAuth
discovery. Any external qualification should be recorded separately from these
fixture-backed results.
