import type { IdentityResponse, ConnectionSetupsResponse, ToolkitsResponse, ConnectionsResponse, SkillsResponse, ArtifactsResponse } from "@energy-agent-tools/sdk";
export interface DashboardData {
  identity: IdentityResponse;
  siteId: string | null;
  connectionSetups: ConnectionSetupsResponse["setups"];
  toolkits: ToolkitsResponse["toolkits"];
  connections: ConnectionsResponse["connections"];
  skills: SkillsResponse["skills"];
  artifacts: ArtifactsResponse["artifacts"];
  publicGatewayUrl: string | null;
}
export type DashboardState =
  | {kind:"signed-out"; message:string | null}
  | {kind:"ready"; data:DashboardData}
  | {kind:"unavailable"; message:string};
