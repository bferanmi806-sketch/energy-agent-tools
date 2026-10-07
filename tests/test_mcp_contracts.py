from __future__ import annotations

import pytest
from pydantic import ValidationError

from energy_agent_tools.mcp_contracts import (
    MCPConnectionInspectionRequest,
    MCPConnectionStageRequest,
    MCPToolReview,
)
from energy_agent_tools.models import Action, DataKind


def _review():
    return {
        "name": "read_meter",
        "reviewed": True,
        "actions": ["read-only"],
        "kind": "metered",
        "unit": "kWh",
    }


def _stage():
    return {
        "url": "https://meter.example/mcp",
        "auth_scheme": "none",
        "display_name": "Meter",
        "schema_digest": "a" * 64,
        "reviews": [_review()],
    }


def test_json_review_parses_canonical_enums_without_coercing_authentication():
    request = MCPConnectionStageRequest.model_validate(_stage())
    assert request.reviews[0].actions == [Action.READ]
    assert request.reviews[0].kind == DataKind.METERED
    assert request.credential is None
    assert request.model_dump(mode="json")["reviews"] == [_review()]


@pytest.mark.parametrize("flag", [False, 1, 0, "true", None])
def test_review_requires_the_actual_boolean_true(flag):
    with pytest.raises(ValidationError):
        MCPToolReview.model_validate({**_review(), "reviewed": flag})


@pytest.mark.parametrize(
    "change",
    [
        {"actions": ["read"]},
        {"actions": ["read-only", "read-only"]},
        {"actions": [1]},
        {"kind": "measured"},
        {"unit": " "},
        {"unit": "x" * 129},
        {"credential_env": "PRIVATE_KEY"},
    ],
)
def test_review_refuses_unreviewed_aliases_coercion_and_secret_fields(change):
    with pytest.raises(ValidationError):
        MCPToolReview.model_validate({**_review(), **change})


@pytest.mark.parametrize(
    "change",
    [
        {"credential": "unexpected"},
        {"auth_scheme": "bearer"},
        {"auth_scheme": "oauth"},
        {"auth_header": "Bad Header"},
        {"allow_private": True},
        {"headers": {"Host": "foreign.example"}},
        {"url": "x" * 2049},
    ],
)
def test_inspection_refuses_invalid_auth_and_user_supplied_network_permissions(change):
    with pytest.raises(ValidationError):
        MCPConnectionInspectionRequest.model_validate(
            {"url": "https://meter.example/mcp", **change}
        )


def test_stage_rejects_duplicate_reviews_and_invalid_digest():
    for change in (
        {"reviews": [_review(), _review()]},
        {"schema_digest": "not-an-approval"},
        {"reviews": []},
        {"display_name": " "},
    ):
        with pytest.raises(ValidationError):
            MCPConnectionStageRequest.model_validate({**_stage(), **change})
