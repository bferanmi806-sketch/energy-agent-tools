Turn `https://github.com/bferanmi806-sketch/energy-agent-tools` into a genuinely usable open-source, self-hostable **Composio for energy**.

Continue from the current `main`. Do not restart or rewrite the project unnecessarily.

### Phase 1: finish the energy-native core

First complete the existing backend roadmap and close every realistically addressable gap discovered in the current audit.

This includes, where not already completed:

- supported spike explanations using available weather/equipment evidence
- meaningful combined grid analysis
- representative large-site/time-series processing
- complete workflow and failure coverage
- robust provider substitution
- real private/site qualification where legitimate access can be obtained
- stronger connection onboarding and provider verification
- autonomous legitimate credential/application acquisition where providers permit it
- 100+ actual agent-evaluation tasks with held-out evaluation
- at least two model families when legitimately accessible
- independently reviewed discovery evaluation
- external connector authoring through the documented connector SDK
- sustained deployment/recovery/load/soak evidence where the environment permits it
- remaining resilience, upgrade, backup/restore and operational gaps
- honest connector and engine qualification

Add a first-class provider-independent **energy-consumption forecasting capability**, capable of using historical usage and relevant contextual data to produce future interval forecasts with uncertainty, provenance and assumptions.

Build the composed workflow needed for questions such as:

“Using my previous three months of electricity consumption, forecast the next eight days and estimate my bill.”

Keep historical meter data `METERED`, future consumption `FORECAST`, and resulting cost `CALCULATED FROM FORECAST`.

Do not mark unavailable external dependencies as completed. Continue working on independent tasks instead.

When the backend contracts are stable and the current roadmap gates are substantially cleared, publish a coherent new release with reproducible evidence.

### Phase 2: build the actual developer product

Then evolve the project from a Python integration runtime into the complete developer experience.

Keep the Python energy/engineering core. Do **not** rewrite it in TypeScript simply for language parity.

Add a first-class **TypeScript SDK** over the stable REST/MCP contracts.

Build a TypeScript/React or Next.js web application with a polished experience similar to modern integration platforms:

- Connect Apps
- Connected Apps
- shared connections
- Sites & Assets
- Skills
- Add Custom MCP
- Connect My Agent
- connection health
- account management
- execution logs
- jobs
- settings

The app catalogue must come from structured toolkit metadata rather than hard-coded UI entries.

### Phase 3: connection and hosted control plane

Create a frictionless connection experience.

For supported providers, target:

`Connect → authorize if unavoidable → securely store → verify → map site/assets → Active`

Users should not normally need to edit Python, JSON files or environment variables.

Implement or strengthen:

- project/workspace abstraction
- users and API keys
- auth configurations
- connected accounts
- managed OAuth/custom OAuth configuration
- encrypted secret storage
- connection health and refresh
- shared connections with explicit ACLs
- dynamic session creation
- hosted MCP endpoints
- one-click/copyable agent connection configuration
- execution logs and observability
- safe event/trigger support
- persistent multi-tenant state
- deployment migrations and operational tooling

Self-hosting must remain first-class even if a hosted deployment mode is added.

### Phase 4: expand the energy ecosystem

Grow the connector ecosystem based on useful energy journeys rather than arbitrary connector count.

Prioritise strong integrations across:

- electricity meters and retailers
- Home Assistant / OpenEnergyMonitor
- grid/system operators
- tariffs and carbon
- weather/resource data
- PV/inverters
- batteries
- EV chargers
- heat pumps
- building/BMS systems
- industrial/site telemetry where legitimately accessible
- energy databases/files
- PyPSA
- pandapower
- OpenDSS
- EnergyPlus/OpenStudio
- pandapipes
- other high-quality engineering software
- reviewed external MCP servers

New connectors must satisfy the same contracts for auth, scoping, semantics, provenance, testing and qualification.

### Product standard

The target experience is:

1. A user opens Energy Agent Tools.
2. They browse **Connect Apps**.
3. They connect their energy systems.
4. They associate connections with sites/assets.
5. They click **Connect my agent** and connect Codex, ChatGPT, Claude or another MCP/SDK client.
6. The agent interacts only with the Energy Agent Tools gateway.
7. The gateway discovers and resolves the appropriate providers/tools.
8. The user asks normal energy questions without knowing provider APIs or tool names.

For example:

“Forecast my energy use for the next eight days and estimate my bill from my previous three months of usage.”

The gateway should autonomously discover the meter, retrieve history, obtain relevant forecast/context data, run the forecasting capability, resolve tariffs, calculate the estimate and return evidence, uncertainty and provenance.

### Definition of done

Do not call this goal complete because there are many tests, files or connectors.

It is complete when the project behaves as a coherent energy integration platform:

- one gateway for heterogeneous energy tools/data
- provider-independent capabilities
- real connection lifecycle
- sites/assets/accounts
- Python and TypeScript SDKs
- MCP as a first-class agent interface
- polished Connect Apps web experience
- hosted/self-hosted deployment
- robust workflow composition
- scalable artifact/time-series processing
- forecasting and engineering capabilities
- real-provider evidence
- broad agent evaluation
- connector contribution ecosystem
- operational reliability
- accurate documentation and release evidence

Work autonomously through the phases. Research, implement, test, review, fix and continue.

Do not stop after completing one milestone or release. Stop only when the goal is genuinely satisfied 

Start from the current repository state immediately.