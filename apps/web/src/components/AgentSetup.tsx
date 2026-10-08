"use client";

import { useId, useState } from "react";
import styles from "./AgentSetup.module.css";

type GatewayEndpoint = {
  name: string;
  url: string;
};

type CopyState = "idle" | "copied" | "error";

type AgentSetupProps = {
  gatewayUrl: string | null;
  siteIds: string[];
  token?: string;
};

function buildEndpoints(gatewayUrl: string | null, siteIds: string[]): GatewayEndpoint[] | null {
  if (!gatewayUrl) return null;
  try {
    const url = new URL(gatewayUrl);
    if (
      (url.protocol !== "http:" && url.protocol !== "https:") || url.username !== "" ||
      url.password !== "" || url.search !== "" || url.hash !== ""
    ) return null;
    const basePath = url.pathname.replace(/\/+$/, "");
    return siteIds.map((siteId, index) => ({
      name: `energy-agent-tools-${index + 1}`,
      url: `${url.origin}${basePath}/mcp/${encodeURIComponent(siteId)}`,
    }));
  } catch {
    return null;
  }
}

function quoteForShell(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

function buildConfiguration(endpoints: GatewayEndpoint[], token: string): string {
  const mcpServers = Object.fromEntries(endpoints.map(({ name, url }) => [
    name,
    {
      type: "http",
      url,
      headers: { Authorization: `Bearer ${token}` },
    },
  ]));
  return JSON.stringify({ mcpServers }, null, 2);
}

export function AgentSetup({ gatewayUrl, siteIds, token }: AgentSetupProps) {
  const idPrefix = useId();
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const endpoints = buildEndpoints(gatewayUrl, siteIds);
  const configuration = endpoints ? buildConfiguration(endpoints, token ?? "${ENERGY_AGENT_TOKEN}") : null;
  const codexCommands = endpoints?.map(({ name, url }) =>
    `codex mcp add ${name} --url ${quoteForShell(url)} --bearer-token-env-var ENERGY_AGENT_TOKEN`,
  ).join("\n") ?? null;

  async function copyConfiguration() {
    if (!configuration) return;
    try {
      await navigator.clipboard.writeText(configuration);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
  }

  if (!gatewayUrl || endpoints === null) {
    return (
      <p className={styles.notice} role="status">
        The public gateway URL is unavailable. Ask the workspace administrator to configure the gateway before connecting an agent.
      </p>
    );
  }

  if (endpoints.length === 0) {
    return <p className={styles.notice} role="status">This key has no site grants. Create a key with at least one site to connect an agent.</p>;
  }

  return (
    <div className={styles.setup}>
      <div className={styles.heading}>
        <div>
          <h3>Connect an agent</h3>
          <p>Use one endpoint for each site you selected. Each endpoint discovers the providers available to that site.</p>
        </div>
        <span className={styles.transport}>Streamable HTTP</span>
      </div>

      <section className={styles.client} aria-labelledby={`${idPrefix}-codex`}>
        <h4 id={`${idPrefix}-codex`}>Codex CLI</h4>
        <ol>
          <li>Make <code>ENERGY_AGENT_TOKEN</code> available to the environment that launches Codex. Keep the key out of shell history.</li>
          <li>Run these commands to add the selected site endpoints:</li>
        </ol>
        <pre><code>{codexCommands}</code></pre>
        <p>Check the connection with <code>codex mcp list</code>. The add command reads the bearer key from the environment variable.</p>
        <a href="https://developers.openai.com/codex/mcp" target="_blank" rel="noreferrer">Codex MCP setup docs</a>
      </section>

      <section className={styles.client} aria-labelledby={`${idPrefix}-claude`}>
        <h4 id={`${idPrefix}-claude`}>Claude Code</h4>
        <p>Add a remote HTTP MCP server with a bearer <code>Authorization</code> header. {token ? "The JSON configuration below includes this key; keep it private and out of source control." : "The JSON configuration below reads ENERGY_AGENT_TOKEN from the client environment."}</p>
        <a href="https://code.claude.com/docs/en/mcp" target="_blank" rel="noreferrer">Claude Code MCP setup docs</a>
      </section>

      <section className={styles.client} aria-labelledby={`${idPrefix}-generic`}>
        <h4 id={`${idPrefix}-generic`}>Other HTTP MCP clients</h4>
        <p>Choose Streamable HTTP (sometimes labelled HTTP), then configure each site URL with the request header <code>Authorization: Bearer &lt;agent key&gt;</code>. Client configuration formats vary.</p>
        <a href="https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization" target="_blank" rel="noreferrer">MCP authorization specification</a>
      </section>

      {configuration ? (
        <details className={styles.configuration}>
          <summary>View the ready-to-use MCP configuration</summary>
          <p>{token ? "This block contains the one-time key. Store it only in a private settings file." : "This block contains an environment variable reference. Make the agent key available to the client as ENERGY_AGENT_TOKEN."}</p>
          <pre><code>{configuration}</code></pre>
          <button type="button" onClick={() => void copyConfiguration()}>Copy configuration</button>
          {copyState === "copied" ? <span className={styles.feedback} role="status">Configuration copied.</span> : null}
          {copyState === "error" ? <span className={styles.error} role="alert">Clipboard access failed. Select the configuration and copy it.</span> : null}
        </details>
      ) : null}

      <p className={styles.chatgptNote}>
        <strong>ChatGPT custom MCP apps:</strong> this gateway key uses static bearer authentication. ChatGPT custom MCP setup currently offers OAuth or no authentication, so this key cannot connect directly. The gateway does not provide the OAuth flow ChatGPT requires.
        {" "}<a href="https://developers.openai.com/api/docs/guides/custom-mcp-server" target="_blank" rel="noreferrer">ChatGPT custom MCP setup</a>{" "}
        and <a href="https://developers.openai.com/plugins/build/auth" target="_blank" rel="noreferrer">authentication requirements</a>.
      </p>
    </div>
  );
}
