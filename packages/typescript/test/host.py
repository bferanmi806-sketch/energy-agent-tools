"""Real authenticated REST host used by the TypeScript SDK acceptance tests.

Run from the repository root with::

    python packages/typescript/test/host.py --port 8765 --state-dir /tmp/energy-sdk-host

The accepted bearer token is read from ``ENERGY_AGENT_TEST_TOKEN``. Set
``ENERGY_AGENT_TEST_FOREIGN_TOKEN`` as well to enable a second principal for
session-ownership checks. Neither token is written to disk or logged here.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from energy_agent_tools.capabilities import CapabilityBinding  # noqa: E402
from energy_agent_tools.hosting import (  # noqa: E402
    AuthenticatedHost,
    Principal,
    create_host,
    token_digest,
)
from energy_agent_tools.models import (  # noqa: E402
    Action,
    AuthConfig,
    ConnectedAccount,
    DataKind,
    EnergyResult,
    Site,
    Tool,
    Toolkit,
)
from energy_agent_tools.registry import Registry  # noqa: E402
from energy_agent_tools.runtime import EnergyAgent  # noqa: E402

TOKEN_ENV = "ENERGY_AGENT_TEST_TOKEN"
FOREIGN_TOKEN_ENV = "ENERGY_AGENT_TEST_FOREIGN_TOKEN"
USER_ID = "sdk-user"
SITE_ID = "sdk-site"
TOOL_NAME = "FIXTURE_CALCULATE"
CAPABILITY_NAME = "calculate_fixture_value"


def create_app(state_dir: Path | str) -> AuthenticatedHost:
    """Build the production authenticated host with a synthetic local site."""

    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise RuntimeError(f"Set {TOKEN_ENV} in the environment before starting the fixture.")

    registry = Registry()
    registry.add_toolkit(
        Toolkit(
            id="fixture_local",
            name="Local SDK fixture",
            description="Deterministic local calculation for SDK acceptance tests.",
            runtime="native",
            status="stable",
        )
    )

    async def calculate(arguments: dict[str, Any], context: Any) -> EnergyResult:
        value = arguments["base"] * arguments["multiplier"]
        return EnergyResult(
            data={"value": value},
            kind=DataKind.CALCULATED,
            unit="fixture-units",
            source="synthetic_local_calculation",
            site_id=context.site_id,
        )

    registry.add(
        Tool(
            name=TOOL_NAME,
            toolkit="fixture_local",
            description="Calculate a deterministic fixture energy value locally.",
            input_schema={
                "type": "object",
                "properties": {
                    "base": {"type": "number"},
                    "multiplier": {"type": "number"},
                },
                "required": ["base", "multiplier"],
                "additionalProperties": False,
            },
            capabilities=[CAPABILITY_NAME],
            actions={Action.CALCULATE},
            result_kind=DataKind.CALCULATED,
            result_unit="fixture-units",
        ),
        calculate,
    )

    sites = [Site(id=SITE_ID, user_id=USER_ID, name="Synthetic SDK Site", timezone="UTC")]
    principals = {
        USER_ID: Principal(USER_ID, {SITE_ID}, token_digest(token), token_id="sdk-test-token")
    }
    foreign_token = os.environ.get(FOREIGN_TOKEN_ENV)
    if foreign_token:
        foreign_user_id = "sdk-foreign-user"
        foreign_site_id = "sdk-foreign-site"
        sites.append(
            Site(
                id=foreign_site_id,
                user_id=foreign_user_id,
                name="Synthetic Foreign Site",
                timezone="UTC",
            )
        )
        principals[foreign_user_id] = Principal(
            foreign_user_id,
            {foreign_site_id},
            token_digest(foreign_token),
            token_id="sdk-foreign-test-token",
        )

    local_account = ConnectedAccount(
        id="fixture-local-connection",
        user_id=USER_ID,
        toolkit="fixture_local",
        site_id=SITE_ID,
        auth=AuthConfig(scheme="local"),
        settings={},
    )
    agent = EnergyAgent(
        registry,
        Path(state_dir),
        accounts=[local_account],
        sites=sites,
        bindings=[
            CapabilityBinding(
                capability=CAPABILITY_NAME,
                tool=TOOL_NAME,
                kind=DataKind.CALCULATED,
                unit="fixture-units",
                reviewed=True,
            )
        ],
    )
    return create_host(agent, principals, close_agent_on_shutdown=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True, help="Loopback HTTP port for uvicorn.")
    parser.add_argument(
        "--state-dir", type=Path, required=True, help="Caller-owned directory for local host state."
    )
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        create_app(args.state_dir),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
