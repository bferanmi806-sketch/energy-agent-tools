import type {
  ConnectionSetupsResponse,
  ExecutionLogResponse,
  ConnectionsResponse,
  IdentityResponse,
  SkillsResponse,
  ToolkitsResponse,
  ArtifactsResponse,
  WorkspaceAssetsResponse,
  WorkspaceKeysResponse,
  WorkspaceMembersResponse,
  WorkspaceOAuthConfigurationsResponse,
  WorkspaceResponse,
  WorkspaceSitesResponse,
} from "@energy-agent-tools/sdk";

export type ExecutionActivityState =
  | { kind: "ready"; page: ExecutionLogResponse }
  | { kind: "unavailable" };

interface DashboardBase {
  identity: IdentityResponse;
  publicGatewayUrl: string | null;
  activity: ExecutionActivityState;
}

export interface OperatorDashboardData extends DashboardBase {
  kind: "operator";
  siteId: string | null;
  connectionSetups: ConnectionSetupsResponse["setups"];
  toolkits: ToolkitsResponse["toolkits"];
  connections: ConnectionsResponse["connections"];
  skills: SkillsResponse["skills"];
  artifacts: ArtifactsResponse["artifacts"];
}

export interface ManagedDashboardData extends DashboardBase {
  kind: "managed";
  authConfigurations: WorkspaceOAuthConfigurationsResponse["configurations"];
  workspace: WorkspaceResponse["workspace"];
  sites: WorkspaceSitesResponse["sites"];
  assets: WorkspaceAssetsResponse["assets"];
  keys: WorkspaceKeysResponse["keys"];
  members: WorkspaceMembersResponse["members"];
  skills: SkillsResponse["skills"];
  connectionSetups: ConnectionSetupsResponse["setups"];
  toolkits: ToolkitsResponse["toolkits"];
  connections: ConnectionsResponse["connections"];
}

export type DashboardData = OperatorDashboardData | ManagedDashboardData;

export type DashboardState =
  | { kind: "signed-out"; message: string | null }
  | { kind: "ready"; data: DashboardData }
  | { kind: "unavailable"; message: string };
