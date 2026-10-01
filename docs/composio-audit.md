# Composio architecture and reuse assessment

Audited before implementation on 30 September 2026 at repository revision
`88fae7d5574b91a156fc3ee23460d24a07eb2f8d`, from a shallow clone of
[ComposioHQ/composio](https://github.com/ComposioHQ/composio). The repository is
MIT licensed, copyright 2025 Sampark Inc. No upstream source is copied into
this project. The assessment separates reusable SDK patterns from
API-backed services that would prevent a self-hosted deployment.

| Concern | Evidence at audited revision | Actual ownership here | Decision |
|---|---|---|---|
| Sessions | Python `tool_router_session.py` and TS `ToolRouterSession.ts` expose search/execute sessions; hosted calls are made through the client. | `EnergyAgent.session` and `BoundSession` keep user/site/toolkit/action scope locally. `EnergyAgentTools.initialize()` imports configured MCP servers before sessions are created. | Keep local sessions and one common executor. |
| Toolkits and tools | Composio toolkit models and registry views describe provider tools; hosted actions execute through the API. | `Registry`, `Toolkit` and `Tool` own local schemas, handlers, versions, dependencies, capabilities and review state. | Use local manifests and validated JSON Schemas. |
| Capability discovery | `ToolRouterSession.search` accepts intent-oriented search and tool filters. | Indexed deterministic lexical search adds energy synonyms and availability hints. It is intentionally not an embedding service. | Keep a measurable local baseline; add semantic ranking only with evidence. |
| Capability resolution | Composio session filters are provider/tool selection controls, not energy measurement contracts. | Reviewed `CapabilityBinding` records exact tool, argument mapping, account/asset, kind, unit, resolution, coverage, quality and version. `CapabilityResolver` reports unavailable and ambiguous ties explicitly. | Do not equate labels or silently translate unlike schemas. |
| Accounts and auth | Composio account models and auth-link flows are open client representations; credential storage, OAuth callback/refresh and provider connections are backend services. | `AuthStore` encrypts local credential blobs with an operator Fernet key, scopes accounts to user/site, and supports configure, verify, refresh, disable, revoke, reconnect and OAuth callback operations. Environment references remain available. | Keep the vault local; do not depend on a hosted account service. |
| Execution and modifiers | Python/TS SDK modifiers and local custom functions are runnable SDK code; hosted app actions call the backend. | One local pipeline validates scope and schemas, applies before/after hooks, checks result kind/unit, redacts secrets and records execution/input provenance. | Apply hooks at the common boundary and avoid automatic action retries. |
| Providers | `core/provider/_openai.py`, `_openai_responses.py`, `base.py` and TS provider packages adapt schemas and call formats. | Small OpenAI Chat, OpenAI Responses and Anthropic formatters preserve optionality and map aliases back to canonical helper names. | Reuse the schema adaptation pattern without importing a hosted client. |
| MCP sessions | `core/models/mcp.py` and TS `toolRouterMcp.ts` model API-managed hosted MCP sessions; docs note that clients bypassing SDK-hosted MCP do not receive SDK hooks/local tools. | The official MCP Python client serves ten local helpers, and imported local/remote MCP tools pass through the same runtime policy. Imported schemas are reviewed, hashed and rechecked before each call. | Keep MCP transport self-hosted and policy-enforcing. |
| Custom MCP | TS `CustomToolkits.ts` wraps hosted custom-toolkit sync and proxy execution. | `import_mcp` discovers local stdio or remote streamable HTTP directly, with stable namespaces, version metadata, safe manifests and atomic schema-drift rejection. | Implement direct local imports; do not copy cloud sync/proxy assumptions. |
| Schemas | `json_schema.py`, `strict_schema.py` and `schema_converter.py` implement recursive transforms, refs and strict-mode compatibility checks. | Provider formatters retain original schemas and avoid lossy strict narrowing; runtime validates the original JSON Schema. | Adapt at the provider edge, preserve the source contract. |
| Policies | Composio session configuration sends allow/deny/toolkit/tag filters to hosted enforcement. | User/site/account/asset scope and action policy are checked locally for each call; default sessions exclude writes and physical control. | Keep safety enforcement in the self-hosted runtime. |
| CLI and extensions | TS CLI services cover setup, generation, validation and account selection, while API operations remain cloud-backed. | Python CLI supports `serve`, authenticated `host`, `catalogue`, `manifests`, `validate`, `scaffold` and encrypted-vault connection lifecycle commands. Plugins load through the `energy_agent_tools.connectors` entry-point group. | Keep contributor tooling local and qualification-oriented. |
| Workbench | Composio's experimental local workbench helper is open code but its documented workflow still uses an API key/hosted session; remote files and sandboxes are backend services. | Private SQLite artifacts, retention/quotas, bounded dataframe operations and lineage run locally. Trusted Python calculations are SDK-only; MCP cannot submit arbitrary code. | Build the workbench as an explicit local trust boundary. |
| Skills | Composio skill text and examples are open, while hosted discovery and execution remain platform services. | Local declarative guidance plus bounded executable skills resolve capabilities, preserve evidence and report missing connections or ambiguity. | Keep skills independent of provider hosting. |

The reusable ideas are the shape of a toolkit/action contract, a search-first
discovery surface, user-scoped sessions, connected-account pinning, provider
schema conversion and execution hooks. Energy-specific requirements change the
domain model: measurement kind, units, timezone/resolution, asset/site
ownership, assumptions, quality, input lineage and conservative control policy
are first-class. A physical energy action cannot safely be treated as a generic
application write.

The local implementation does not claim to reproduce Composio's hosted search
index, connector catalogue, credential service, OAuth provider integrations,
remote sandbox or cloud observability. It also does not claim that an audited
Composio SDK file proves the corresponding hosted service is open source.

No Composio service or API key is required to run this project.
`THIRD_PARTY_NOTICES` records the inspiration and library licences. Any future
source reuse must preserve the relevant MIT notice and be reviewed separately.

Official documentation studied:

- [How Composio works](https://docs.composio.dev/docs/how-composio-works)
- [Session configuration](https://docs.composio.dev/docs/configuring-sessions)
- [MCP sessions and hook limitations](https://docs.composio.dev/docs/sessions-via-mcp)
- [Custom MCP lifecycle](https://docs.composio.dev/docs/extending-sessions/custom-mcp)
- [Connected accounts](https://docs.composio.dev/docs/auth-configuration/connected-accounts)
