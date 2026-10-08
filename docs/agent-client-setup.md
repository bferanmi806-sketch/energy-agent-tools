# Connect an agent to a workspace gateway

An agent key is shown once when it is created. Copy it somewhere private before closing the setup panel. The key grants access only to the sites selected when it was issued; revoke it from the **Agent keys** list if it is no longer needed.

Each site has one MCP endpoint:

```text
<gateway-url>/mcp/<site-id>
```

That endpoint discovers the providers and tools available to that site. If a key grants access to several sites, add one endpoint per site and use the same key for each. The key cannot access sites outside its grants.

Use the public HTTPS gateway URL shown by the workspace. Do not put the key in source control, shared project settings, or logs.

## Codex CLI

Codex CLI supports Streamable HTTP MCP servers and can read the bearer token from an environment variable. Set `ENERGY_AGENT_TOKEN` in the environment that launches Codex, then add one entry for each allowed site:

```sh
codex mcp add energy-agent-tools-1 \
  --url 'https://gateway.example.com/mcp/site-id' \
  --bearer-token-env-var ENERGY_AGENT_TOKEN
```

Use a different MCP server name for each site. Then run `codex mcp list` to confirm the entries are configured. The token value is not part of the add command or Codex's MCP URL. Make sure the Codex process can read the variable; a desktop app launched outside your terminal may not inherit a variable exported only in that terminal.

The local `codex mcp add --help` was checked on October 8, 2026. It lists `--url` and `--bearer-token-env-var` for Streamable HTTP servers. See the [Codex MCP guide](https://developers.openai.com/codex/mcp) and [Codex configuration reference](https://developers.openai.com/codex/config-reference/) for current CLI and configuration details.

## Claude Code

Claude Code accepts remote HTTP MCP servers with custom request headers. For a project-level `.mcp.json`, use environment-variable expansion for the secret:

```json
{
  "mcpServers": {
    "energy-agent-tools-1": {
      "type": "http",
      "url": "https://gateway.example.com/mcp/site-id",
      "headers": {
        "Authorization": "Bearer ${ENERGY_AGENT_TOKEN}"
      }
    }
  }
}
```

Set `ENERGY_AGENT_TOKEN` in the environment used to launch Claude Code. Add another `mcpServers` entry for each granted site. The JSON contains only the variable name, so the project configuration can be shared without sharing the key. See Anthropic's [Claude Code MCP guide](https://code.claude.com/docs/en/mcp) for HTTP servers, headers, and `.mcp.json` variable expansion.

## Other HTTP MCP clients

Choose **Streamable HTTP** (sometimes labelled **HTTP**) in the client and configure each allowed site URL with this request header:

```text
Authorization: Bearer <agent-key>
```

Some clients accept an `mcpServers` JSON object like the Claude Code example; others use different settings screens or file formats. The client must support a custom bearer authorization header. The [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) describes OAuth-based authorization discovery; this gateway's agent keys are static bearer keys and the gateway does not implement that OAuth flow.

## ChatGPT custom MCP apps

The gateway's static bearer key cannot currently be used directly as a ChatGPT custom MCP app credential. ChatGPT's custom app setup uses OAuth or no authentication, and the gateway does not provide the OAuth authorization server and discovery endpoints that ChatGPT requires. See OpenAI's [ChatGPT custom MCP setup](https://developers.openai.com/api/docs/guides/custom-mcp-server) and [MCP authentication guide](https://developers.openai.com/plugins/build/auth) for the supported authentication flow.
