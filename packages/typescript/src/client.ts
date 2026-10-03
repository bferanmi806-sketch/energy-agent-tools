import { HttpTransport } from "./transport.js";
import type { HttpTransportOptions } from "./transport.js";
import { parser } from "./validation.js";
import {
  IdentityResponseSchema, ConnectionSetupsResponseSchema, OctopusConnectionRequestSchema, ConnectionCreatedResponseSchema,
  SessionCreateSchema, SessionResponseSchema, SearchRequestSchema, SearchResponseSchema,
  ExecuteRequestSchema, ExecutionResponseSchema, CapabilityRequestSchema,
  CapabilityExecutionRequestSchema, ResolutionResponseSchema, ConnectionsResponseSchema,
  ArtifactsResponseSchema, SkillsResponseSchema, JobRequestSchema, JobResponseSchema,
  DeleteSessionResponseSchema, DeleteArtifactResponseSchema,
  SkillExecutionRequestSchema, WorkflowResponseSchema, ToolkitsResponseSchema,
} from "./contracts.js";
import type {
  IdentityResponse, ConnectionSetupsResponse, OctopusConnectionRequest, ConnectionCreatedResponse,
  SessionCreate, SessionResponse, SearchRequest, SearchResponse, ExecuteRequest, ExecutionResponse, CapabilityRequest, CapabilityExecutionRequest, ResolutionResponse, ConnectionsResponse, ArtifactsResponse, SkillsResponse, JobRequest, JobResponse, DeleteSessionResponse, DeleteArtifactResponse, SkillExecutionRequest, WorkflowResponse, ToolkitsResponse
} from "./contracts.js";

const parseSessionCreate = parser<SessionCreate>(SessionCreateSchema);
const parseIdentity = parser<IdentityResponse>(IdentityResponseSchema);
const parseConnectionSetups = parser<ConnectionSetupsResponse>(ConnectionSetupsResponseSchema);
const parseConnectionCreate = parser<OctopusConnectionRequest>(OctopusConnectionRequestSchema);
const parseConnectionCreated = parser<ConnectionCreatedResponse>(ConnectionCreatedResponseSchema);
const parseSession = parser<SessionResponse>(SessionResponseSchema);
const parseSearchRequest = parser<SearchRequest>(SearchRequestSchema);
const parseSearch = parser<SearchResponse>(SearchResponseSchema);
const parseExecuteRequest = parser<ExecuteRequest>(ExecuteRequestSchema);
const parseExecution = parser<ExecutionResponse>(ExecutionResponseSchema);
const parseCapabilityRequest = parser<CapabilityRequest>(CapabilityRequestSchema);
const parseCapabilityExecutionRequest = parser<CapabilityExecutionRequest>(CapabilityExecutionRequestSchema);
const parseResolution = parser<ResolutionResponse>(ResolutionResponseSchema);
const parseToolkits = parser<ToolkitsResponse>(ToolkitsResponseSchema);
const parseConnections = parser<ConnectionsResponse>(ConnectionsResponseSchema);
const parseArtifacts = parser<ArtifactsResponse>(ArtifactsResponseSchema);
const parseSkills = parser<SkillsResponse>(SkillsResponseSchema);
const parseJobRequest = parser<JobRequest>(JobRequestSchema);
const parseJob = parser<JobResponse>(JobResponseSchema);
const parseSkillExecutionRequest = parser<SkillExecutionRequest>(SkillExecutionRequestSchema);
const parseWorkflow = parser<WorkflowResponse>(WorkflowResponseSchema);
const parseDeleteSession = parser<DeleteSessionResponse>(DeleteSessionResponseSchema);
const parseDeleteArtifact = parser<DeleteArtifactResponse>(DeleteArtifactResponseSchema);

export interface RequestOptions { signal?: AbortSignal }

function identifier(value: string): string {
  if (!value || value.length > 256) throw new TypeError("Provide a nonempty gateway identifier.");
  return encodeURIComponent(value);
}

/** One authenticated gateway; providers and credentials stay behind it. */
export class EnergyAgentTools {
  readonly #transport: HttpTransport;

  constructor(options: HttpTransportOptions) {
    this.#transport = new HttpTransport(options);
  }

  identity(options: RequestOptions = {}) {
    return this.#transport.request({ path: "me", method: "GET", parse: parseIdentity, ...options });
  }

  async createSession(input: SessionCreate = {}, options: RequestOptions = {}) {
    const response = await this.#transport.request({
      path: "sessions", method: "POST", body: parseSessionCreate(input), parse: parseSession,
      ...options,
    });
    return new EnergySession(this.#transport, response.session_id, response.site_id);
  }

  /** Attach to an existing session. The host still checks ownership and expiry. */
  session(input: { sessionId: string; siteId?: string | null }) {
    identifier(input.sessionId);
    return new EnergySession(this.#transport, input.sessionId, input.siteId ?? null);
  }
}

export class EnergySession {
  readonly #transport: HttpTransport;
  readonly #path: string;
  readonly id: string;
  readonly siteId: string | null;

  constructor(transport: HttpTransport, sessionId: string, siteId: string | null) {
    this.#transport = transport;
    this.#path = `sessions/${identifier(sessionId)}`;
    this.id = sessionId;
    this.siteId = siteId;
  }

  search(input: SearchRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/search`, method: "POST",
      body: parseSearchRequest(input), parse: parseSearch, ...options });
  }

  execute(input: ExecuteRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/execute`, method: "POST",
      body: parseExecuteRequest(input), parse: parseExecution, ...options });
  }

  resolve(input: CapabilityRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/resolve`, method: "POST",
      body: parseCapabilityRequest(input), parse: parseResolution, ...options });
  }

  capability(input: CapabilityExecutionRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/capability`, method: "POST",
      body: parseCapabilityExecutionRequest(input), parse: parseExecution, ...options });
  }

  toolkits(options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/toolkits`, method: "GET",
      parse: parseToolkits, ...options });
  }

  connectionSetups(options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/connection-setup`, method: "GET",
      parse: parseConnectionSetups, ...options });
  }

  connectAccount(input: OctopusConnectionRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/connections`, method: "POST",
      body: parseConnectionCreate(input), parse: parseConnectionCreated, ...options });
  }

  connections(options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/connections`, method: "GET",
      parse: parseConnections, ...options });
  }

  artifacts(options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/artifacts`, method: "GET",
      parse: parseArtifacts, ...options });
  }

  skills(input?: SearchRequest, options: RequestOptions = {}) {
    if (input) return this.#transport.request({ path: `${this.#path}/skills`, method: "POST",
      body: parseSearchRequest(input), parse: parseSkills, ...options });
    return this.#transport.request({ path: `${this.#path}/skills`, method: "GET",
      parse: parseSkills, ...options });
  }

  runSkill(input: SkillExecutionRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/skills/run`, method: "POST",
      body: parseSkillExecutionRequest(input), parse: parseWorkflow, ...options });
  }

  job(input: JobRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/jobs`, method: "POST",
      body: parseJobRequest(input), parse: parseJob, ...options });
  }

  deleteArtifact(artifactId: string, options: RequestOptions = {}) {
    return this.#transport.request({ path: `${this.#path}/artifacts/${identifier(artifactId)}`,
      method: "DELETE", parse: parseDeleteArtifact, ...options });
  }

  /** Closing a REST session deletes its scoped artifacts on the server. */
  close(options: RequestOptions = {}) {
    return this.#transport.request({ path: this.#path, method: "DELETE",
      parse: parseDeleteSession, ...options });
  }
}
