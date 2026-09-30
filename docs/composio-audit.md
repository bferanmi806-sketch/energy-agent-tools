# Composio architecture and reuse assessment

Audited before implementation, 30 September 2026. Repository revision
`88fae7d5574b91a156fc3ee23460d24a07eb2f8d`, shallow clone of
[ComposioHQ/composio](https://github.com/ComposioHQ/composio).
The repository licence is MIT, copyright 2025 Sampark Inc. No upstream source is
copied into this project. The assessment distinguishes runnable SDK code from
API client representations of backend services.

| Concern | Evidence at audited revision | Actual ownership | Decision |
|---|---|---|---|
| Sessions | `python/composio/core/models/tool_router.py:create`; `tool_router_session.py:search,execute` | Creation, search and remote execution call `client.tool_router.session.*`. SDK also dispatches registered local functions. | Implement local session scope and executor. |
| Toolkits/tools | `core/models/tools.py`, TS `models/Toolkits.ts` | Registry listing/retrieval and normal app execution call hosted API; custom functions run locally. | Local manifests, validated JSON schemas and handler registry. |
| Discovery | `tool_router_session.py:search`, TS `ToolRouterSession.ts:search` | Hosted search accepts intent queries and inline custom-tool metadata. | Bounded lexical search with energy synonyms; avoid claiming hosted semantic ranking. |
| Accounts/auth | `connected_accounts.py`, `auth_configs.py` | Client models and auth-link polling are open; credentials, OAuth callback, refresh and connections are backend services. | Environment secret references and explicit user/site/account resolution. OAuth lifecycle deferred. |
| Execution | `tools.py:_execute,execute`, TS `Tools.ts` | SDK modifiers and local custom functions are runnable; app actions call backend. SDK avoids blind retries for actions. | Single runtime pipeline; no automatic write retries. |
| Providers | `core/provider/_openai.py`, `_openai_responses.py`, `base.py`; TS provider packages | Actual framework/schema adaptation and call routing are open code. | Implement small OpenAI Chat/Responses and Anthropic formatters. |
| MCP sessions | `core/models/mcp.py`; TS `lib/toolRouterMcp.ts` | API-managed hosted MCP endpoint, origin checks in SDK. Local custom tools and SDK hooks do not run when clients bypass SDK through hosted MCP. | Serve our executor through official MCP SDK, with same hooks/policies. |
| Custom MCP | TS `models/CustomToolkits.ts:upsert,sync,delete` | Registration/sync wrappers call `client.custom.*`; remote schemas and proxy execution live in cloud. | Import local stdio and remote streamable HTTP directly. Default deny unreviewed actions. |
| Schemas | `utils/json_schema.py`, `strict_schema.py`, `schema_converter.py` | Recursive transforms, ref handling and unsupported strict-mode detection are implemented in SDK. | Keep original schemas, use non-strict provider format rather than lossy strict conversion. |
| Hooks | `core/models/_modifiers.py`, TS `Tools.ts` | before/after execution and schema modifiers are actual SDK code, not global server enforcement. | Apply hooks at our common execution boundary; validate again after before-hook. |
| Policies | Session config allow/deny/toolkit/tag types; `ToolRouterSession.ts` | SDK sends filters, hosted session enforces them. | Enforce toolkit/user/site/action scope locally on each call. |
| CLI | `ts/packages/cli/src/services`, `generation`, models | CLI setup, generation, validation and account selection implemented, API operations still cloud-backed. | Python CLI for serving, catalogue, local configuration and smoke checks. |
| Workbench | `ts/packages/experimental/src/workbench/local-workbench.ts`, shim and Python helper | Local helper generation is open, but requires API key and hosted session; remote sandbox/files are backend services. | Local SQLite artifact store and bounded dataframe operations; trusted Python SDK calculations. |
| Skills | `skills/`, official skills guide | Skill text and examples are open; platform skill discovery is tied to hosted tool search. | Local declarative guidance independent of connectors. |

Reuse MIT abstractions where useful: toolkit/action schemas, a user-scoped session,
connected-account pinning, search-first meta tools, provider conversion and hooks.
These are design adaptations, not a mechanical fork. Copying backend-facing SDK
classes would add a Composio dependency without making its services self-hostable.
No Composio service or API key is needed here. THIRD_PARTY_NOTICES records the
inspiration and library licences; source copying in future must carry the relevant
MIT notice.

Energy-specific redesign: user/site/asset ownership, explicit measurement kind,
units/timezone/resolution, assumptions/quality/provenance and conservative control
policy. Physical infrastructure cannot be represented safely as a generic app
write. Large results become local artifacts with concise previews; derived
results retain input provenance and cannot become metered by aggregation.

Official documentation studied:
- [Sessions](https://docs.composio.dev/docs/how-composio-works)
- [Session configuration](https://docs.composio.dev/docs/configuring-sessions)
- [MCP sessions and hook limitations](https://docs.composio.dev/docs/sessions-via-mcp)
- [Custom MCP lifecycle](https://docs.composio.dev/docs/extending-sessions/custom-mcp)
- [Connected accounts](https://docs.composio.dev/docs/auth-configuration/connected-accounts)

The SDK repository is not evidence that the hosted connector implementations,
search index, credential vault or sandbox orchestrator are open-source.
