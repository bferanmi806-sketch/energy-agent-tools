"""Qualify one public reference day through the native gateway, without meter claims."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from energy_agent_tools import EnergyAgentTools

START = datetime(2026, 9, 20, tzinfo=UTC)
END = START + timedelta(days=1)
LATITUDE = 52.52
LONGITUDE = 13.41


async def qualify() -> dict:
    config = {
        "sites": [
            {
                "id": "public-reference",
                "user_id": "qualification",
                "name": "Public Berlin reference coordinates",
                "timezone": "UTC",
                "latitude": LATITUDE,
                "longitude": LONGITUDE,
            }
        ]
    }
    with TemporaryDirectory(prefix="energy-weather-qualification-") as directory:
        async with EnergyAgentTools(Path(directory), config) as gateway:
            response = await gateway.session("qualification", "public-reference").capability(
                "get_historical_weather", {"start": START.isoformat(), "end": END.isoformat()}
            )
            assert response["ok"], response
            result = response["result"]
            assert result["kind"] == "estimated" and result["unit"] == "degC"
            assert result["site_id"] == "public-reference"
            rows = result["data"]
            assert len(rows) == 24
            for index, row in enumerate(rows):
                point = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                assert point == START + timedelta(hours=index)
                assert not isinstance(row["temperature"], bool)
                assert math.isfinite(row["temperature"])
            assert any("gridded" in warning for warning in result["warnings"])
            return {
                "request": {
                    "latitude": LATITUDE,
                    "longitude": LONGITUDE,
                    "start": START.isoformat(),
                    "end": END.isoformat(),
                    "capability": "get_historical_weather",
                },
                "physical_thermometer": False,
                "physical_meter": False,
                "public_reference_coordinates": True,
                "validation": "24 finite hourly values with estimated kind",
                "response": response,
                "limits": [
                    "One public reference day is not representative forecast accuracy qualification.",
                    "These gridded temperatures do not qualify physical meter or thermometer access.",
                ],
            }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(qualify()), indent=2, sort_keys=True))
