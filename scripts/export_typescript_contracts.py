"""Generate SDK schemas from production Python request/result models.

Wire envelopes mirror the host's routes. Live SDK acceptance checks them against
that host. Run with --check in verification to refuse stale generated contracts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from energy_agent_tools.capabilities import CapabilityRequest
from energy_agent_tools.connection_contracts import (
    ConnectionSetupsResponse,
    OctopusConnectionRequest,
)
from energy_agent_tools.control_contracts import (
    WorkspaceAgentKeyRequest,
    WorkspaceAssetRequest,
    WorkspaceAuthorizationResponse,
    WorkspaceDetails,
    WorkspaceHomeAssistantAuthorizationRequest,
    WorkspaceMapRequest,
    WorkspaceOAuthCleanupRequest,
    WorkspaceOAuthCleanupResponse,
    WorkspaceOAuthCompleteRequest,
    WorkspaceOAuthConfigurationsResponse,
    WorkspaceSiteRequest,
)
from energy_agent_tools.control_store import IssuedKey, KeyRecord
from energy_agent_tools.hosting import (
    IdentityResponse,
    _CapabilityExecutionRequest,
    _ExecuteRequest,
    _JobRequest,
    _SearchRequest,
    _SessionCreate,
    _SkillExecutionRequest,
)
from energy_agent_tools.models import Asset, EnergyResult, Site, Tool, Toolkit
from energy_agent_tools.workspace_access import (
    WorkspaceMember,
    WorkspaceMemberGrants,
    WorkspaceMemberRequest,
)


def expand_model(model: Any) -> dict[str, Any]:
    schema = model.model_json_schema()
    definitions = schema.get("$defs", {})

    def expand(value: Any) -> Any:
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            reference = value["$ref"]
            if not reference.startswith("#/$defs/"):
                raise ValueError("Only local model references are supported")
            return expand(
                {
                    **definitions[reference.removeprefix("#/$defs/")],
                    **{k: v for k, v in value.items() if k != "$ref"},
                }
            )
        return {key: expand(item) for key, item in value.items() if key != "$defs"}

    return expand(schema)


def obj(properties: dict[str, Any], required: list[str], *, extra: bool = True) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": extra,
    }


def schemas() -> dict[str, dict[str, Any]]:
    text = {"type": "string"}
    boolean = {"type": "boolean"}
    nullable_text = {"anyOf": [text, {"type": "null"}]}
    record = {"type": "object", "additionalProperties": True}

    def array(item: dict[str, Any]) -> dict[str, Any]:
        return {"type": "array", "items": item}

    error = obj({"code": text, "message": text, "retryable": boolean}, ["code", "message"])
    energy = expand_model(EnergyResult)
    tool = expand_model(Tool)
    tool["additionalProperties"] = True
    tool["properties"]["connection_available"] = boolean
    execution = {
        "oneOf": [
            obj(
                {"ok": {"const": True}, "execution_id": text, "result": energy},
                ["ok", "execution_id", "result"],
            ),
            obj(
                {
                    "ok": {"const": False},
                    "execution_id": text,
                    "error": error,
                    "resolution": record,
                },
                ["ok", "error"],
            ),
        ]
    }
    connection = obj(
        {
            "id": text,
            "toolkit": text,
            "site_id": nullable_text,
            "enabled": boolean,
            "auth_scheme": text,
            "display_name": text,
            "state": text,
            "verified": boolean,
        },
        ["id", "toolkit", "site_id", "enabled", "auth_scheme", "state", "verified"],
    )
    skill = obj(
        {
            "id": text,
            "intent": text,
            "capabilities": array(text),
            "sequence": array(text),
            "supporting_tools": array(text),
            "pitfalls": array(text),
        },
        ["id", "intent", "capabilities", "sequence", "pitfalls"],
    )
    return {
        "IdentityResponse": expand_model(IdentityResponse),
        "WorkspaceResponse": obj({"workspace": expand_model(WorkspaceDetails)}, ["workspace"]),
        "WorkspaceSiteRequest": expand_model(WorkspaceSiteRequest),
        "WorkspaceSiteResponse": obj({"site": expand_model(Site)}, ["site"]),
        "WorkspaceSitesResponse": obj({"sites": array(expand_model(Site))}, ["sites"]),
        "WorkspaceAssetRequest": expand_model(WorkspaceAssetRequest),
        "WorkspaceAssetResponse": obj({"asset": expand_model(Asset)}, ["asset"]),
        "WorkspaceAssetsResponse": obj({"assets": array(expand_model(Asset))}, ["assets"]),
        "WorkspaceMapRequest": expand_model(WorkspaceMapRequest),
        "WorkspaceAgentKeyRequest": expand_model(WorkspaceAgentKeyRequest),
        "WorkspaceIssuedKeyResponse": expand_model(IssuedKey),
        "WorkspaceKeysResponse": obj({"keys": array(expand_model(KeyRecord))}, ["keys"]),
        "WorkspaceRevokedKeyResponse": obj({"revoked": {"const": True}}, ["revoked"]),
        "WorkspaceMemberRequest": expand_model(WorkspaceMemberRequest),
        "WorkspaceMemberGrants": expand_model(WorkspaceMemberGrants),
        "WorkspaceMemberResponse": obj({"member": expand_model(WorkspaceMember)}, ["member"]),
        "WorkspaceMembersResponse": obj(
            {"members": array(expand_model(WorkspaceMember))}, ["members"]
        ),
        "WorkspaceMemberRemovedResponse": obj({"removed": {"const": True}}, ["removed"]),
        "WorkspaceOAuthConfigurationsResponse": expand_model(WorkspaceOAuthConfigurationsResponse),
        "WorkspaceHomeAssistantAuthorizationRequest": expand_model(
            WorkspaceHomeAssistantAuthorizationRequest
        ),
        "WorkspaceOAuthCompleteRequest": expand_model(WorkspaceOAuthCompleteRequest),
        "WorkspaceOAuthCleanupRequest": expand_model(WorkspaceOAuthCleanupRequest),
        "WorkspaceOAuthCleanupResponse": expand_model(WorkspaceOAuthCleanupResponse),
        "WorkspaceAuthorizationResponse": expand_model(WorkspaceAuthorizationResponse),
        "ConnectionSetupsResponse": expand_model(ConnectionSetupsResponse),
        "OctopusConnectionRequest": expand_model(OctopusConnectionRequest),
        "SessionCreate": expand_model(_SessionCreate),
        "SearchRequest": expand_model(_SearchRequest),
        "ExecuteRequest": expand_model(_ExecuteRequest),
        "CapabilityRequest": expand_model(CapabilityRequest),
        "CapabilityExecutionRequest": expand_model(_CapabilityExecutionRequest),
        "JobRequest": expand_model(_JobRequest),
        "SkillExecutionRequest": expand_model(_SkillExecutionRequest),
        "WorkflowResponse": {
            "oneOf": [
                obj({"ok": {"const": True}}, ["ok"]),
                obj({"ok": {"const": False}, "error": error}, ["ok", "error"]),
            ]
        },
        "EnergyResult": energy,
        "Toolkit": expand_model(Toolkit),
        "ToolkitsResponse": obj({"toolkits": array(expand_model(Toolkit))}, ["toolkits"]),
        "SessionResponse": obj(
            {"session_id": text, "site_id": nullable_text}, ["session_id", "site_id"]
        ),
        "SearchResponse": obj({"tools": array(tool)}, ["tools"]),
        "ExecutionResponse": execution,
        "ResolutionResponse": obj(
            {
                "capability": text,
                "candidates": array(record),
                "selected": {"anyOf": [record, {"type": "null"}]},
                "status": {"enum": ["resolved", "ambiguous", "unavailable"]},
                "note": text,
            },
            ["capability", "candidates", "selected", "status", "note"],
        ),
        "ConnectionCreatedResponse": obj(
            {
                "ok": {"const": True},
                "account": connection,
                "health": obj(
                    {
                        "connection_id": text,
                        "provider": {"enum": ["octopus", "home_assistant"]},
                        "status": {"const": "healthy"},
                        "checked_at": text,
                        "probe": {"const": "provider-read"},
                        "message": text,
                    },
                    ["connection_id", "provider", "status", "checked_at", "probe", "message"],
                ),
            },
            ["ok", "account", "health"],
        ),
        "ConnectionVerificationResponse": obj(
            {
                "ok": {"const": True},
                "account": connection,
                "health": obj(
                    {
                        "connection_id": text,
                        "provider": {"enum": ["octopus", "home_assistant"]},
                        "status": {"enum": ["healthy", "unhealthy"]},
                        "checked_at": text,
                        "probe": {"const": "provider-read"},
                        "message": text,
                    },
                    ["connection_id", "provider", "status", "checked_at", "probe", "message"],
                ),
            },
            ["ok", "account", "health"],
        ),
        "ConnectionDisconnectedResponse": obj(
            {
                "ok": {"const": True},
                "account": connection,
                "upstream_revoked": {"anyOf": [boolean, {"type": "null"}]},
            },
            ["ok", "account"],
        ),
        "ConnectionsResponse": obj({"connections": array(connection)}, ["connections"]),
        "ArtifactsResponse": obj({"artifacts": array(record)}, ["artifacts"]),
        "SkillsResponse": obj({"skills": array(skill)}, ["skills"]),
        "JobResponse": {
            "oneOf": [
                obj({"ok": {"const": True}}, ["ok"]),
                obj({"ok": {"const": False}, "error": error}, ["ok", "error"]),
            ]
        },
        "DeleteSessionResponse": obj(
            {"deleted": {"const": True}, "session_id": text}, ["deleted", "session_id"]
        ),
        "DeleteArtifactResponse": obj(
            {"deleted": {"const": True}, "artifact_id": text}, ["deleted", "artifact_id"]
        ),
    }


def render() -> str:
    lines = [
        "// Generated by scripts/export_typescript_contracts.py. Do not edit.",
        'import type { FromSchema } from "json-schema-to-ts";',
        "",
    ]
    for name, schema in schemas().items():
        lines.append(f"export const {name}Schema = {json.dumps(schema, indent=2)} as const;")
        lines.append(
            f"export type {name} = FromSchema<typeof {name}Schema, {{ keepDefaultedPropertiesOptional: true }}>;"
        )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / "packages/typescript/src/contracts.ts"
    expected = render()
    if args.check:
        if not target.exists() or target.read_text() != expected:
            raise SystemExit(
                "Generated SDK contracts are stale; run scripts/export_typescript_contracts.py"
            )
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(expected)


if __name__ == "__main__":
    main()
