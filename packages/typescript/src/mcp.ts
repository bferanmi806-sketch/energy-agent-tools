import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import {
  StreamableHTTPClientTransport,
  type StreamableHTTPClientTransportOptions,
} from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";

export type EnergyMcpToken = string | (() => string | Promise<string>);

export interface EnergyMcpConnectOptions {
  url: string;
  token: EnergyMcpToken;
  signal?: AbortSignal;
}

export interface EnergyMcpRequestOptions {
  signal?: AbortSignal;
}

export type EnergyMcpTool = Awaited<ReturnType<Client["listTools"]>>["tools"][number];

export type EnergyJsonValue = null | boolean | number | string | EnergyJsonValue[] | {
  [key: string]: EnergyJsonValue;
};

export type EnergyJsonObject = { [key: string]: EnergyJsonValue };

type EnergyMcpDiagnostic =
  | "MCP URL must be an absolute HTTP(S) endpoint without credentials, query, or fragment."
  | "MCP URL must end with a gateway endpoint under /mcp/{siteId}."
  | "MCP bearer token is unavailable or invalid."
  | "MCP transport attempted a request outside its configured endpoint."
  | "MCP connection was cancelled."
  | "MCP connection failed; check the gateway URL and credentials."
  | "MCP client is closed."
  | "MCP tool discovery was cancelled."
  | "MCP tool discovery failed."
  | "MCP helper call was cancelled."
  | "MCP helper call failed."
  | "MCP helper returned an error."
  | "MCP helper did not return a JSON object."
  | "MCP session close failed.";

/** A stable, credential-safe diagnostic raised by the MCP adapter. */
export class EnergyMcpError extends Error {
  constructor(message: EnergyMcpDiagnostic) {
    super(message);
    this.name = "EnergyMcpError";
  }
}

function parseEndpoint(value: string): URL {
  let endpoint: URL;
  try {
    endpoint = new URL(value);
  } catch {
    throw new EnergyMcpError(
      "MCP URL must be an absolute HTTP(S) endpoint without credentials, query, or fragment.",
    );
  }

  if (
    (endpoint.protocol !== "http:" && endpoint.protocol !== "https:") ||
    endpoint.username !== "" ||
    endpoint.password !== "" ||
    endpoint.search !== "" ||
    endpoint.hash !== "" ||
    value.includes("?") ||
    value.includes("#")
  ) {
    throw new EnergyMcpError(
      "MCP URL must be an absolute HTTP(S) endpoint without credentials, query, or fragment.",
    );
  }

  const gatewayRoute = endpoint.pathname.lastIndexOf("/mcp/");
  if (
    gatewayRoute < 0 ||
    endpoint.pathname.length <= gatewayRoute + "/mcp/".length ||
    endpoint.pathname.endsWith("/")
  ) {
    throw new EnergyMcpError("MCP URL must end with a gateway endpoint under /mcp/{siteId}.");
  }

  return endpoint;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isTextContent(value: unknown): value is { type: "text"; text: string } {
  return isObject(value) && value.type === "text" && typeof value.text === "string";
}

function parseJsonValue(value: unknown, active: WeakSet<object>): EnergyJsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("Invalid JSON number.");
    return value;
  }
  if (typeof value !== "object") throw new TypeError("Invalid JSON value.");
  if (active.has(value)) throw new TypeError("Circular JSON value.");
  active.add(value);

  try {
    if (Array.isArray(value)) return value.map((item) => parseJsonValue(item, active));

    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new TypeError("Invalid JSON object.");
    }

    const entries: [string, EnergyJsonValue][] = [];
    for (const [key, item] of Object.entries(value)) {
      entries.push([key, parseJsonValue(item, active)]);
    }
    return Object.fromEntries(entries);
  } finally {
    active.delete(value);
  }
}

function parseJsonObject(value: unknown): EnergyJsonObject {
  if (!isObject(value)) throw new EnergyMcpError("MCP helper did not return a JSON object.");
  try {
    const parsed = parseJsonValue(value, new WeakSet());
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new TypeError("Expected a JSON object.");
    }
    return parsed;
  } catch {
    throw new EnergyMcpError("MCP helper did not return a JSON object.");
  }
}

function resultObject(result: Awaited<ReturnType<Client["callTool"]>>): EnergyJsonObject {
  if (result.isError === true) throw new EnergyMcpError("MCP helper returned an error.");
  if (result.structuredContent !== undefined) return parseJsonObject(result.structuredContent);

  if (!Array.isArray(result.content)) {
    throw new EnergyMcpError("MCP helper did not return a JSON object.");
  }
  const textContent = result.content.find(isTextContent);
  if (!textContent) throw new EnergyMcpError("MCP helper did not return a JSON object.");

  let decoded: unknown;
  try {
    decoded = JSON.parse(textContent.text);
  } catch {
    throw new EnergyMcpError("MCP helper did not return a JSON object.");
  }
  return parseJsonObject(decoded);
}

function createAuthenticatedFetch(
  endpoint: URL,
  source: EnergyMcpToken,
): NonNullable<StreamableHTTPClientTransportOptions["fetch"]> {
  return async (input, init) => {
    let target: URL;
    try {
      target = new URL(input);
    } catch {
      throw new EnergyMcpError("MCP transport attempted a request outside its configured endpoint.");
    }

    if (
      target.origin !== endpoint.origin ||
      target.pathname !== endpoint.pathname ||
      target.search !== "" ||
      target.hash !== ""
    ) {
      throw new EnergyMcpError("MCP transport attempted a request outside its configured endpoint.");
    }

    let token: string;
    try {
      token = typeof source === "string" ? source : await source();
    } catch {
      throw new EnergyMcpError("MCP bearer token is unavailable or invalid.");
    }
    if (typeof token !== "string" || token.trim() === "" || /[\u0000-\u001f\u007f]/u.test(token)) {
      throw new EnergyMcpError("MCP bearer token is unavailable or invalid.");
    }

    const headers = new Headers(init?.headers);
    headers.set("authorization", `Bearer ${token}`);
    return fetch(target, { ...init, headers, redirect: "error" });
  };
}

function bridgeTransport(transport: StreamableHTTPClientTransport): Transport {
  let onclose: NonNullable<Transport["onclose"]> = () => undefined;
  let onerror: NonNullable<Transport["onerror"]> = () => undefined;
  let onmessage: NonNullable<Transport["onmessage"]> = () => undefined;

  return {
    start: () => transport.start(),
    send: (message, options) => transport.send(message, options ? {
      ...(options.resumptionToken === undefined ? {} : { resumptionToken: options.resumptionToken }),
      ...(options.onresumptiontoken === undefined ? {} : { onresumptiontoken: options.onresumptiontoken }),
    } : undefined),
    close: () => transport.close(),
    get onclose() {
      return onclose;
    },
    set onclose(handler) {
      onclose = handler;
      transport.onclose = handler;
    },
    get onerror() {
      return onerror;
    },
    set onerror(handler) {
      onerror = handler;
      transport.onerror = handler;
    },
    get onmessage() {
      return onmessage;
    },
    set onmessage(handler) {
      onmessage = handler;
      transport.onmessage = handler;
    },
    setProtocolVersion: (version) => transport.setProtocolVersion(version),
  };
}

/** An authenticated MCP connection to one site-scoped Energy Agent Tools gateway. */
export class EnergyMcpClient {
  readonly #client: Client;
  readonly #transport: StreamableHTTPClientTransport;
  #closed = false;
  #closePromise: Promise<void> | undefined;

  private constructor(client: Client, transport: StreamableHTTPClientTransport) {
    this.#client = client;
    this.#transport = transport;
  }

  static async connect(options: EnergyMcpConnectOptions): Promise<EnergyMcpClient> {
    const endpoint = parseEndpoint(options.url);
    const client = new Client({ name: "energy-agent-tools-typescript", version: "0.4.0" });
    const transport = new StreamableHTTPClientTransport(endpoint, {
      fetch: createAuthenticatedFetch(endpoint, options.token),
      redirectPolicy: "same-origin",
      requestInit: { redirect: "error" },
    });

    try {
      await client.connect(bridgeTransport(transport), options.signal ? { signal: options.signal } : {});
      return new EnergyMcpClient(client, transport);
    } catch {
      await client.close().catch(() => undefined);
      if (options.signal?.aborted) throw new EnergyMcpError("MCP connection was cancelled.");
      throw new EnergyMcpError("MCP connection failed; check the gateway URL and credentials.");
    }
  }

  async listTools(options: EnergyMcpRequestOptions = {}): Promise<EnergyMcpTool[]> {
    this.#assertOpen();
    if (options.signal?.aborted) throw new EnergyMcpError("MCP tool discovery was cancelled.");

    try {
      const result = await this.#client.listTools(undefined, options.signal ? { signal: options.signal } : {});
      return result.tools;
    } catch {
      if (options.signal?.aborted) throw new EnergyMcpError("MCP tool discovery was cancelled.");
      throw new EnergyMcpError("MCP tool discovery failed.");
    }
  }

  async callHelper(
    name: string,
    args: Record<string, unknown>,
    options: EnergyMcpRequestOptions = {},
  ): Promise<EnergyJsonObject> {
    this.#assertOpen();
    if (options.signal?.aborted) throw new EnergyMcpError("MCP helper call was cancelled.");
    if (typeof name !== "string" || name.trim() === "" || !isObject(args)) {
      throw new EnergyMcpError("MCP helper call failed.");
    }
    let jsonArguments: EnergyJsonObject;
    try {
      jsonArguments = parseJsonObject(args);
    } catch {
      throw new EnergyMcpError("MCP helper call failed.");
    }

    let result: Awaited<ReturnType<Client["callTool"]>>;
    try {
      result = await this.#client.callTool(
        { name, arguments: jsonArguments },
        undefined,
        options.signal ? { signal: options.signal } : {},
      );
    } catch {
      if (options.signal?.aborted) throw new EnergyMcpError("MCP helper call was cancelled.");
      throw new EnergyMcpError("MCP helper call failed.");
    }

    return resultObject(result);
  }

  close(): Promise<void> {
    if (this.#closePromise) return this.#closePromise;
    this.#closed = true;
    this.#closePromise = (async () => {
      let failed = false;
      try {
        await this.#transport.terminateSession();
      } catch {
        failed = true;
      }
      try {
        await this.#client.close();
      } catch {
        failed = true;
      }
      if (failed) throw new EnergyMcpError("MCP session close failed.");
    })();
    return this.#closePromise;
  }

  #assertOpen(): void {
    if (this.#closed) throw new EnergyMcpError("MCP client is closed.");
  }
}
