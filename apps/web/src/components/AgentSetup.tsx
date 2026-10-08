"use client";

import { useId, useState } from "react";
import styles from "./AgentSetup.module.css";

type GatewayEndpoint = {
  name: string;
  url: string;
};

type CopyState = "idle" | "copied" | "error";
type AgentClient = "codex" | "claude" | "other";

type AgentSetupProps = {
  gatewayUrl: string | null;
  siteIds: string[];
  token?: string;
};

const clients = [
  { id: "codex", label: "Codex CLI" },
  { id: "claude", label: "Claude Code" },
  { id: "other", label: "Other HTTP" },
] as const satisfies readonly { id: AgentClient; label: string }[];

const clientLabels: Record<AgentClient, string> = {
  codex: "Codex CLI",
  claude: "Claude Code",
  other: "Other HTTP",
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

function buildConfiguration(endpoints: GatewayEndpoint[], authorizationValue: string): string {
  const mcpServers = Object.fromEntries(endpoints.map(({ name, url }) => [
    name,
    {
      type: "http",
      url,
      headers: { Authorization: `Bearer ${authorizationValue}` },
    },
  ]));
  return JSON.stringify({ mcpServers }, null, 2);
}

export function AgentSetup({ gatewayUrl, siteIds, token }: AgentSetupProps) {
  const idPrefix = useId();
  const [client, setClient] = useState<AgentClient>("codex");
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const endpoints = buildEndpoints(gatewayUrl, siteIds);
  const codexCommands = endpoints?.map(({ name, url }) =>
    `codex mcp add ${name} --url ${quoteForShell(url)} --bearer-token-env-var ENERGY_AGENT_TOKEN`,
  ).join("\n") ?? null;
  const claudeConfiguration = endpoints
    ? buildConfiguration(endpoints, "${ENERGY_AGENT_TOKEN}")
    : null;
  const otherConfiguration = endpoints
    ? buildConfiguration(endpoints, "YOUR_AGENT_KEY")
    : null;

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
  }

  function selectClient(nextClient: AgentClient) {
    setClient(nextClient);
    setCopyState("idle");
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

  const panel = (() => {
    switch (client) {
      case "codex":
        return (
          <>
            <p>Set <code>ENERGY_AGENT_TOKEN</code> in the environment that launches Codex. Use a private secret store; do not paste the token into shell history.</p>
            <p>Run these commands to add one endpoint for each granted site:</p>
            <pre><code>{codexCommands}</code></pre>
            <button className={styles.copyButton} type="button" onClick={() => void copy(codexCommands ?? "")}>
              Copy Codex commands
            </button>
            <p>Run <code>codex mcp list</code> to inspect the configured servers. Adding them here does not confirm that Codex has connected.</p>
            <a href="https://developers.openai.com/codex/mcp" target="_blank" rel="noreferrer">Codex MCP setup docs</a>
          </>
        );
      case "claude":
        return (
          <>
            <p>Set <code>ENERGY_AGENT_TOKEN</code> in the environment that launches Claude Code, then add these entries to a private or project <code>.mcp.json</code>. The file uses the environment variable, so it does not contain the token.</p>
            <pre><code>{claudeConfiguration}</code></pre>
            <button className={styles.copyButton} type="button" onClick={() => void copy(claudeConfiguration ?? "")}>
              Copy Claude Code configuration
            </button>
            <p>Run <code>claude mcp list</code> or open <code>/mcp</code> to check server status. A saved configuration alone does not confirm a connection.</p>
            <a href="https://code.claude.com/docs/en/mcp" target="_blank" rel="noreferrer">Claude Code MCP setup docs</a>
          </>
        );
      case "other":
        return (
          <>
            <p>For each URL below, configure Streamable HTTP (sometimes called HTTP) and the request header <code>Authorization: Bearer YOUR_AGENT_KEY</code>. Replace <code>YOUR_AGENT_KEY</code> with your saved token.</p>
            <pre><code>{otherConfiguration}</code></pre>
            <button className={styles.copyButton} type="button" onClick={() => void copy(otherConfiguration ?? "")}>
              Copy HTTP configuration
            </button>
            <p>Each endpoint is limited to one of the sites granted to this key. Client configuration formats vary.</p>
            <a href="https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization" target="_blank" rel="noreferrer">MCP authorization specification</a>
            <details className={styles.unsupported}>
              <summary>ChatGPT custom MCP apps</summary>
              <p>This key uses static bearer authentication. ChatGPT custom MCP apps currently require OAuth or no authentication, so this key cannot connect directly. This gateway does not provide the OAuth flow ChatGPT requires.</p>
              <a href="https://developers.openai.com/api/docs/guides/custom-mcp-server" target="_blank" rel="noreferrer">ChatGPT custom MCP setup</a>
            </details>
          </>
        );
      default: {
        const _exhaustive: never = client;
        return _exhaustive;
      }
    }
  })();

  return (
    <div className={styles.setup}>
      <div className={styles.heading}>
        <div>
          <h3>Connect an agent</h3>
          <p>Choose your client to see setup steps for only the sites granted to this key.</p>
        </div>
        <span className={styles.transport}>Streamable HTTP</span>
      </div>

      <p className={styles.tokenState} role="status">
        {token
          ? "The one-time token is available above. The Codex and Claude examples use ENERGY_AGENT_TOKEN; the Other HTTP example uses a YOUR_AGENT_KEY placeholder. These instructions do not repeat the token."
          : "These setup snippets do not include the token. Use ENERGY_AGENT_TOKEN where shown, or replace YOUR_AGENT_KEY with a private copy you saved."}
      </p>

      <div className={styles.clientChoices} role="group" aria-label="Choose agent client">
        {clients.map((option) => (
          <button
            aria-pressed={client === option.id}
            aria-controls={`${idPrefix}-panel`}
            className={client === option.id ? styles.clientChoiceSelected : styles.clientChoice}
            key={option.id}
            onClick={() => selectClient(option.id)}
            type="button"
          >
            {option.label}
          </button>
        ))}
      </div>

      <section
        aria-labelledby={`${idPrefix}-${client}-tab`}
        className={styles.clientPanel}
        id={`${idPrefix}-panel`}
        role="region"
      >
        <h4 id={`${idPrefix}-${client}-tab`}>{clientLabels[client]} setup</h4>
        {panel}
        {copyState === "copied" ? <p className={styles.feedback} role="status">Copied to clipboard.</p> : null}
        {copyState === "error" ? <p className={styles.error} role="alert">Clipboard access failed. Select the text and copy it.</p> : null}
      </section>
    </div>
  );
}
