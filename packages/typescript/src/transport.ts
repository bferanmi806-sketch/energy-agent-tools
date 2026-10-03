export type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

export interface HttpTransportOptions {
  baseUrl: string;
  token: string | (() => string | Promise<string>);
  fetch?: typeof globalThis.fetch;
  timeoutMs?: number;
  maxResponseBytes?: number;
}

export interface HttpRequest<T> {
  path: string;
  method: HttpMethod;
  body?: unknown;
  parse: (value: unknown) => T;
  signal?: AbortSignal;
}

export type EnergyTransportErrorCode = "network_error" | "timeout" | "cancelled" | "token_error";

const DEFAULT_TIMEOUT_MS = 60_000;
const DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024;
const MAX_TIMER_MS = 2_147_483_647;
const SAFE_HTTP_ERROR_CODE = /^[a-z][a-z0-9_]{0,79}$/;
const BODY_ERROR_MESSAGE = "Request body must contain only JSON-safe values.";

const TRANSPORT_MESSAGES: Record<EnergyTransportErrorCode, string> = {
  network_error: "The gateway request failed.",
  timeout: "The gateway request timed out.",
  cancelled: "The gateway request was cancelled.",
  token_error: "The authentication token could not be resolved.",
};

export class EnergyHttpError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;

  constructor(status: number, code: string, message: string, retryable: boolean) {
    super(message);
    this.name = "EnergyHttpError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

export class EnergyProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "EnergyProtocolError";
  }
}

export class EnergyTransportError extends Error {
  readonly code: EnergyTransportErrorCode;

  constructor(code: EnergyTransportErrorCode) {
    super(TRANSPORT_MESSAGES[code]);
    this.name = "EnergyTransportError";
    this.code = code;
  }
}

type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

type CloneTask =
  | { kind: "visit"; source: unknown; assign: (value: JsonValue) => void }
  | { kind: "leave"; source: object };

function validateBaseUrl(value: string): URL {
  if (
    typeof value !== "string" ||
    value.trim() !== value ||
    !/^https?:\/\//i.test(value) ||
    value.includes("\\") ||
    value.includes("?") ||
    value.includes("#")
  ) {
    throw new TypeError("baseUrl must be an HTTP or HTTPS URL without credentials, query, or fragment.");
  }

  const authorityMatch = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]*)/i.exec(value);
  if (!authorityMatch || authorityMatch[1]?.includes("@")) {
    throw new TypeError("baseUrl must be an HTTP or HTTPS URL without credentials, query, or fragment.");
  }

  let base: URL;
  try {
    base = new URL(value);
  } catch {
    throw new TypeError("baseUrl must be an HTTP or HTTPS URL without credentials, query, or fragment.");
  }

  if (
    (base.protocol !== "http:" && base.protocol !== "https:") ||
    base.username !== "" ||
    base.password !== "" ||
    base.search !== "" ||
    base.hash !== ""
  ) {
    throw new TypeError("baseUrl must be an HTTP or HTTPS URL without credentials, query, or fragment.");
  }

  if (!base.pathname.endsWith("/")) base.pathname += "/";
  return base;
}

function resolveRoute(base: URL, path: string): URL {
  if (
    typeof path !== "string" ||
    path.length === 0 ||
    path.startsWith("/") ||
    path.startsWith("\\") ||
    path.includes("\\") ||
    path.includes("?") ||
    path.includes("#") ||
    /[\u0000-\u0020\u007f]/.test(path)
  ) {
    throw new TypeError("path must be a relative route path.");
  }

  for (const segment of path.split("/")) {
    let decoded: string;
    try {
      decoded = decodeURIComponent(segment);
    } catch {
      throw new TypeError("path must be a relative route path.");
    }
    if (decoded.split(/[\\/]/).some((part) => part === "." || part === "..")) {
      throw new TypeError("path must be a relative route path.");
    }
  }

  let route: URL;
  try {
    route = new URL(path, base);
  } catch {
    throw new TypeError("path must be a relative route path.");
  }

  if (route.origin !== base.origin || !route.pathname.startsWith(base.pathname)) {
    throw new TypeError("path must be a relative route path.");
  }
  return route;
}

function cloneJsonValue(value: unknown): JsonValue {
  let result: JsonValue = null;
  const active = new WeakSet<object>();
  const tasks: CloneTask[] = [{ kind: "visit", source: value, assign: (copy) => { result = copy; } }];

  while (tasks.length > 0) {
    const task = tasks.pop();
    if (!task) continue;
    if (task.kind === "leave") {
      active.delete(task.source);
      continue;
    }

    const current = task.source;
    if (current === null || typeof current === "boolean" || typeof current === "string") {
      task.assign(current);
      continue;
    }
    if (typeof current === "number") {
      if (!Number.isFinite(current)) throw new TypeError(BODY_ERROR_MESSAGE);
      task.assign(current);
      continue;
    }
    if (typeof current !== "object") throw new TypeError(BODY_ERROR_MESSAGE);
    if (active.has(current)) throw new TypeError(BODY_ERROR_MESSAGE);
    active.add(current);

    if (Array.isArray(current)) {
      if (Object.getPrototypeOf(current) !== Array.prototype) throw new TypeError(BODY_ERROR_MESSAGE);
      const lengthDescriptor = Object.getOwnPropertyDescriptor(current, "length");
      const lengthValue = lengthDescriptor?.value;
      const ownKeys = Reflect.ownKeys(current);
      if (
        typeof lengthValue !== "number" ||
        !Number.isSafeInteger(lengthValue) ||
        ownKeys.length !== lengthValue + 1
      ) {
        throw new TypeError(BODY_ERROR_MESSAGE);
      }

      const children: Array<{ index: number; value: unknown }> = [];
      for (let index = 0; index < lengthValue; index += 1) {
        const descriptor = Object.getOwnPropertyDescriptor(current, String(index));
        if (!descriptor || !descriptor.enumerable || !("value" in descriptor)) {
          throw new TypeError(BODY_ERROR_MESSAGE);
        }
        children.push({ index, value: descriptor.value });
      }

      const copy: JsonValue[] = new Array(lengthValue);
      task.assign(copy);
      tasks.push({ kind: "leave", source: current });
      for (let index = children.length - 1; index >= 0; index -= 1) {
        const child = children[index];
        if (!child) throw new TypeError(BODY_ERROR_MESSAGE);
        tasks.push({
          kind: "visit",
          source: child.value,
          assign: (valueCopy) => { copy[child.index] = valueCopy; },
        });
      }
      continue;
    }

    const prototype = Object.getPrototypeOf(current);
    if (prototype !== Object.prototype && prototype !== null) throw new TypeError(BODY_ERROR_MESSAGE);

    const children: Array<{ key: string; value: unknown }> = [];
    for (const key of Reflect.ownKeys(current)) {
      if (typeof key !== "string") throw new TypeError(BODY_ERROR_MESSAGE);
      const descriptor = Object.getOwnPropertyDescriptor(current, key);
      if (!descriptor || !descriptor.enumerable || !("value" in descriptor)) {
        throw new TypeError(BODY_ERROR_MESSAGE);
      }
      children.push({ key, value: descriptor.value });
    }

    const copy: { [key: string]: JsonValue } = {};
    task.assign(copy);
    tasks.push({ kind: "leave", source: current });
    for (let index = children.length - 1; index >= 0; index -= 1) {
      const child = children[index];
      if (!child) throw new TypeError(BODY_ERROR_MESSAGE);
      tasks.push({
        kind: "visit",
        source: child.value,
        assign: (valueCopy) => {
          Object.defineProperty(copy, child.key, {
            value: valueCopy,
            enumerable: true,
            configurable: true,
            writable: true,
          });
        },
      });
    }
  }

  return result;
}

function serializeBody(value: unknown): string {
  try {
    const json = JSON.stringify(cloneJsonValue(value));
    if (typeof json !== "string") throw new TypeError(BODY_ERROR_MESSAGE);
    return json;
  } catch {
    throw new TypeError(BODY_ERROR_MESSAGE);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function redactToken(value: string, token: string): string {
  return token.length === 0 ? value : value.split(token).join("[redacted]");
}

function readHttpError(value: unknown, status: number, token: string): EnergyHttpError {
  const envelope = isRecord(value) ? value.error : undefined;
  const details = isRecord(envelope) ? envelope : undefined;
  const rawCode = details?.code;
  const rawMessage = details?.message;

  let code = "http_error";
  if (
    typeof rawCode === "string" &&
    !rawCode.includes(token) &&
    SAFE_HTTP_ERROR_CODE.test(rawCode)
  ) {
    code = rawCode;
  }

  let message = "The gateway returned an HTTP error.";
  if (typeof rawMessage === "string") {
    const safe = redactToken(rawMessage, token)
      .replace(/[\u0000-\u001f\u007f]/g, " ")
      .trim()
      .slice(0, 500);
    if (safe.length > 0) message = safe;
  }

  return new EnergyHttpError(
    status,
    code,
    message,
    status === 408 || status === 425 || status === 429 || status >= 500,
  );
}

async function readBoundedBody(response: Response, maxBytes: number): Promise<Uint8Array> {
  if (response.body === null) return new Uint8Array();

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      if (result.value.byteLength > maxBytes - total) {
        await reader.cancel().catch(() => undefined);
        throw new EnergyProtocolError("Gateway response exceeded the configured size limit.");
      }
      chunks.push(result.value.slice());
      total += result.value.byteLength;
    }
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function resolveTimeout(value: number | undefined): number {
  const timeoutMs = value ?? DEFAULT_TIMEOUT_MS;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > MAX_TIMER_MS) {
    throw new TypeError("timeoutMs must be a positive integer within the supported timer range.");
  }
  return timeoutMs;
}

function resolveMaxResponseBytes(value: number | undefined): number {
  const maxResponseBytes = value ?? DEFAULT_MAX_RESPONSE_BYTES;
  if (!Number.isSafeInteger(maxResponseBytes) || maxResponseBytes <= 0) {
    throw new TypeError("maxResponseBytes must be a positive safe integer.");
  }
  return maxResponseBytes;
}

function validToken(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && !/[\u0000-\u0020\u007f]/.test(value);
}

export class HttpTransport {
  readonly #baseUrl: URL;
  readonly #token: HttpTransportOptions["token"];
  readonly #fetch: typeof globalThis.fetch;
  readonly #timeoutMs: number;
  readonly #maxResponseBytes: number;

  constructor(options: HttpTransportOptions) {
    this.#baseUrl = validateBaseUrl(options.baseUrl);
    this.#token = options.token;
    this.#fetch = options.fetch ?? globalThis.fetch;
    this.#timeoutMs = resolveTimeout(options.timeoutMs);
    this.#maxResponseBytes = resolveMaxResponseBytes(options.maxResponseBytes);
    if (typeof this.#fetch !== "function") throw new TypeError("A fetch implementation is required.");
  }

  async request<T>(request: HttpRequest<T>): Promise<T> {
    if (
      request.method !== "GET" && request.method !== "POST" &&
      request.method !== "PATCH" && request.method !== "DELETE"
    ) {
      throw new TypeError("method must be GET, POST, PATCH, or DELETE.");
    }
    if (typeof request.parse !== "function") throw new TypeError("parse must be a function.");

    const route = resolveRoute(this.#baseUrl, request.path);
    const hasBody = Object.hasOwn(request, "body");
    if (hasBody && request.method === "GET") {
      throw new TypeError("GET requests cannot include a body.");
    }
    const body = hasBody ? serializeBody(request.body) : undefined;

    const controller = new AbortController();
    let cancellationCode: EnergyTransportErrorCode | null = null;
    let rejectCancellation: (reason: EnergyTransportError) => void = () => undefined;
    const cancellation = new Promise<never>((_resolve, reject) => {
      rejectCancellation = reject;
    });

    const cancel = (code: "timeout" | "cancelled"): void => {
      if (cancellationCode !== null) return;
      cancellationCode = code;
      controller.abort();
      rejectCancellation(new EnergyTransportError(code));
    };

    const parentSignal = request.signal;
    const onParentAbort = (): void => cancel("cancelled");
    if (parentSignal?.aborted) cancel("cancelled");
    else parentSignal?.addEventListener("abort", onParentAbort, { once: true });

    const timer = cancellationCode === null
      ? setTimeout(() => cancel("timeout"), this.#timeoutMs)
      : undefined;

    if (cancellationCode !== null) throw new EnergyTransportError(cancellationCode);

    let activeToken = "";
    const operation = (async (): Promise<T> => {
      let resolvedToken: unknown;
      try {
        resolvedToken = typeof this.#token === "function" ? await this.#token() : this.#token;
      } catch {
        throw new EnergyTransportError("token_error");
      }
      if (!validToken(resolvedToken)) throw new EnergyTransportError("token_error");
      activeToken = resolvedToken;
      if (controller.signal.aborted) {
        throw new EnergyTransportError(cancellationCode ?? "cancelled");
      }

      const headers: Record<string, string> = {
        authorization: `Bearer ${resolvedToken}`,
        accept: "application/json",
      };
      if (body !== undefined) headers["content-type"] = "application/json";

      const init: RequestInit = {
        method: request.method,
        headers,
        redirect: "error",
        signal: controller.signal,
      };
      if (body !== undefined) init.body = body;

      let response: Response;
      try {
        response = await this.#fetch(route, init);
      } catch {
        throw new EnergyTransportError("network_error");
      }

      const bytes = await readBoundedBody(response, this.#maxResponseBytes);
      let decoded: string;
      try {
        decoded = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      } catch {
        throw new EnergyProtocolError("Gateway response was not valid UTF-8.");
      }

      let value: unknown;
      try {
        value = JSON.parse(decoded);
      } catch {
        throw new EnergyProtocolError("Gateway response was not valid JSON.");
      }

      if (!response.ok) throw readHttpError(value, response.status, activeToken);

      try {
        return request.parse(value);
      } catch (error) {
        if (error instanceof EnergyProtocolError) {
          throw new EnergyProtocolError(redactToken(error.message, activeToken));
        }
        throw new EnergyProtocolError("Gateway response did not match its expected contract.");
      }
    })();

    try {
      return await Promise.race([operation, cancellation]);
    } catch (error) {
      if (error instanceof EnergyHttpError || error instanceof EnergyProtocolError) throw error;
      if (cancellationCode !== null) throw new EnergyTransportError(cancellationCode);
      if (error instanceof EnergyTransportError) throw error;
      throw new EnergyTransportError("network_error");
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      parentSignal?.removeEventListener("abort", onParentAbort);
    }
  }
}
