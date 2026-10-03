import { HttpTransport } from "./transport.js";
import type { HttpTransportOptions } from "./transport.js";
import { parser } from "./validation.js";
import {
  ConnectionVerificationResponseSchema, ConnectionDisconnectedResponseSchema,
  IdentityResponseSchema, ConnectionSetupsResponseSchema, OctopusConnectionRequestSchema, ConnectionCreatedResponseSchema,
  SessionCreateSchema, SessionResponseSchema, SearchRequestSchema, SearchResponseSchema,
  ExecuteRequestSchema, ExecutionResponseSchema, CapabilityRequestSchema,
  CapabilityExecutionRequestSchema, ResolutionResponseSchema, ConnectionsResponseSchema,
  ArtifactsResponseSchema, SkillsResponseSchema, JobRequestSchema, JobResponseSchema,
  DeleteSessionResponseSchema, DeleteArtifactResponseSchema,
  SkillExecutionRequestSchema, WorkflowResponseSchema, ToolkitsResponseSchema,
  WorkspaceResponseSchema, WorkspaceSiteRequestSchema, WorkspaceSiteResponseSchema,
  WorkspaceSitesResponseSchema, WorkspaceAssetRequestSchema, WorkspaceAssetResponseSchema,
  WorkspaceAssetsResponseSchema, WorkspaceMapRequestSchema, WorkspaceAgentKeyRequestSchema,
  WorkspaceIssuedKeyResponseSchema, WorkspaceKeysResponseSchema, WorkspaceRevokedKeyResponseSchema,
} from "./contracts.js";
import type {
  ConnectionVerificationResponse, ConnectionDisconnectedResponse,
  IdentityResponse, ConnectionSetupsResponse, OctopusConnectionRequest, ConnectionCreatedResponse,
  SessionCreate, SessionResponse, SearchRequest, SearchResponse, ExecuteRequest, ExecutionResponse, CapabilityRequest, CapabilityExecutionRequest, ResolutionResponse, ConnectionsResponse, ArtifactsResponse, SkillsResponse, JobRequest, JobResponse, DeleteSessionResponse, DeleteArtifactResponse, SkillExecutionRequest, WorkflowResponse, ToolkitsResponse,
  WorkspaceResponse, WorkspaceSiteRequest, WorkspaceSiteResponse, WorkspaceSitesResponse,
  WorkspaceAssetRequest, WorkspaceAssetResponse, WorkspaceAssetsResponse, WorkspaceMapRequest,
  WorkspaceAgentKeyRequest, WorkspaceIssuedKeyResponse, WorkspaceKeysResponse,
  WorkspaceRevokedKeyResponse,
} from "./contracts.js";

const parseSessionCreate = parser<SessionCreate>(SessionCreateSchema);
const parseIdentity = parser<IdentityResponse>(IdentityResponseSchema);
const parseConnectionSetups = parser<ConnectionSetupsResponse>(ConnectionSetupsResponseSchema);
const parseConnectionCreate = parser<OctopusConnectionRequest>(OctopusConnectionRequestSchema);
const parseConnectionCreated = parser<ConnectionCreatedResponse>(ConnectionCreatedResponseSchema);
const parseConnectionVerification = parser<ConnectionVerificationResponse>(ConnectionVerificationResponseSchema);
const parseConnectionDisconnected = parser<ConnectionDisconnectedResponse>(ConnectionDisconnectedResponseSchema);
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
const parseWorkspace = parser<WorkspaceResponse>(WorkspaceResponseSchema);
const parseWorkspaceSiteRequest = parser<WorkspaceSiteRequest>(WorkspaceSiteRequestSchema);
const parseWorkspaceSite = parser<WorkspaceSiteResponse>(WorkspaceSiteResponseSchema);
const parseWorkspaceSites = parser<WorkspaceSitesResponse>(WorkspaceSitesResponseSchema);
const parseWorkspaceAssetRequest = parser<WorkspaceAssetRequest>(WorkspaceAssetRequestSchema);
const parseWorkspaceAsset = parser<WorkspaceAssetResponse>(WorkspaceAssetResponseSchema);
const parseWorkspaceAssets = parser<WorkspaceAssetsResponse>(WorkspaceAssetsResponseSchema);
const parseWorkspaceMapRequest = parser<WorkspaceMapRequest>(WorkspaceMapRequestSchema);
const parseWorkspaceAgentKeyRequest = parser<WorkspaceAgentKeyRequest>(WorkspaceAgentKeyRequestSchema);
const parseWorkspaceIssuedKey = parser<WorkspaceIssuedKeyResponse>(WorkspaceIssuedKeyResponseSchema);
const parseWorkspaceKeys = parser<WorkspaceKeysResponse>(WorkspaceKeysResponseSchema);
const parseWorkspaceRevokedKey = parser<WorkspaceRevokedKeyResponse>(WorkspaceRevokedKeyResponseSchema);

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

  /** Management API for the managed workspace associated with this gateway key. */
  workspace(): EnergyWorkspace {
    return new EnergyWorkspace(this.#transport);
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

/** Authenticated management operations for the current managed workspace. */
export class EnergyWorkspace {
  readonly #transport: HttpTransport;

  constructor(transport: HttpTransport) {
    this.#transport = transport;
  }

  details(options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace", method: "GET", parse: parseWorkspace, ...options });
  }

  sites(options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace/sites", method: "GET", parse: parseWorkspaceSites, ...options });
  }

  createSite(input: WorkspaceSiteRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace/sites", method: "POST",
      body: parseWorkspaceSiteRequest(input), parse: parseWorkspaceSite, ...options });
  }

  assets(options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace/assets", method: "GET", parse: parseWorkspaceAssets, ...options });
  }

  createAsset(input: WorkspaceAssetRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace/assets", method: "POST",
      body: parseWorkspaceAssetRequest(input), parse: parseWorkspaceAsset, ...options });
  }

  toolkits(options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace/toolkits", method: "GET", parse: parseToolkits, ...options });
  }

  connectionSetups(options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace/connection-setups", method: "GET",
      parse: parseConnectionSetups, ...options });
  }

  connections(options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace/connections", method: "GET",
      parse: parseConnections, ...options });
  }

  connectAccount(input: OctopusConnectionRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace/connections", method: "POST",
      body: parseConnectionCreate(input), parse: parseConnectionCreated, ...options });
  }

  mapConnection(connectionId: string, input: WorkspaceMapRequest, options: RequestOptions = {}) {
    return this.#transport.request({
      path: `workspace/connections/${identifier(connectionId)}/map`, method: "POST",
      body: parseWorkspaceMapRequest(input), parse: parseConnectionCreated, ...options,
    });
  }

  verifyConnection(connectionId: string, options: RequestOptions = {}) {
    return this.#transport.request({
      path: `workspace/connections/${identifier(connectionId)}/verify`, method: "POST",
      body: {}, parse: parseConnectionVerification, ...options,
    });
  }

  disconnectConnection(connectionId: string, options: RequestOptions = {}) {
    return this.#transport.request({
      path: `workspace/connections/${identifier(connectionId)}/disconnect`, method: "POST",
      body: {}, parse: parseConnectionDisconnected, ...options,
    });
  }

  keys(options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace/keys", method: "GET", parse: parseWorkspaceKeys, ...options });
  }

  createAgentKey(input: WorkspaceAgentKeyRequest, options: RequestOptions = {}) {
    return this.#transport.request({ path: "workspace/keys", method: "POST",
      body: parseWorkspaceAgentKeyRequest(input), parse: parseWorkspaceIssuedKey, ...options });
  }

  revokeKey(keyId: string, options: RequestOptions = {}) {
    return this.#transport.request({ path: `workspace/keys/${identifier(keyId)}`, method: "DELETE",
      parse: parseWorkspaceRevokedKey, ...options });
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

  verifyConnection(connectionId: string, options: RequestOptions = {}) {
    return this.#transport.request({path:`${this.#path}/connections/${identifier(connectionId)}/verify`,method:"POST",body:{},parse:parseConnectionVerification,...options});
  }

  disconnectConnection(connectionId: string, options: RequestOptions = {}) {
    return this.#transport.request({path:`${this.#path}/connections/${identifier(connectionId)}/disconnect`,method:"POST",body:{},parse:parseConnectionDisconnected,...options});
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
