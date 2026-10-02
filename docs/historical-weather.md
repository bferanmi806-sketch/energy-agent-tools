# Read hourly historical temperature

`open_meteo.get_historical_temperature` provides the
`get_historical_weather` capability. It reads hourly 2 m air temperature from
Open-Meteo's Historical Weather API and returns an `estimated` result labeled
as gridded historical analysis/reanalysis. These values are not physical
thermometer measurements. The capability does not require a provider account
or credential.

## Request a bounded time range

Use a gateway session to request a public reference location for one day. The
end time is exclusive.

```python
from energy_agent_tools import EnergyAgentTools


async def fetch_historical_weather() -> str:
    async with EnergyAgentTools(".energy-agent") as tools:
        gateway = tools.session("example-user")
        result = await gateway.capability(
            "get_historical_weather",
            {
                "latitude": 52.52,
                "longitude": 13.41,
                "start": "2026-09-20T00:00:00Z",
                "end": "2026-09-21T00:00:00Z",
            },
            persist=True,
        )
        if not result["ok"]:
            raise RuntimeError(result["error"]["message"])
        return result["result"]["data"]["artifact_id"]
```

`latitude` and `longitude` are finite WGS84 coordinates in the ranges -90 to 90
and -180 to 180. `start` and `end` are ISO 8601 timestamps with explicit UTC
offsets. The connector converts them to UTC and accepts a span of at most 366
days. It includes rows at `start` and excludes rows at `end`.

The connector requests `temperature_2m` in Celsius with UTC Unix timestamps. It
uses the UTC date containing `start` for `start_date` and the date containing
the last included instant for `end_date`, then filters the returned hours to
the exact half-open range.

## Interpret the result

Rows have the form `{"timestamp": "2026-09-20T00:00:00Z", "temperature": 16.7}`.
Timestamps use UTC; values use `degC`, with `field_units.temperature` also set
to `degC`. The result has `1h` resolution and an `instantaneous` quantity shape.
The kind is `estimated`, the source and provider are `open-meteo`, and the
quality label is `gridded historical analysis/reanalysis`. Provenance records
`Best Match (provider default)`; it does not guarantee one fixed model identity.
The grid cell can be several kilometres from the requested coordinate, and the
result includes no uncertainty interval. See the [Open-Meteo Historical Weather
API documentation](https://open-meteo.com/en/docs/historical-weather-api) for
provider details.

Null temperatures remain `null` and add a warning. Missing hours and the full
requested range are not filled. Direct capability results therefore may not
cover every requested instant. The consumption forecast workflow requires full
context coverage; in `auto` mode it records unavailable context and can use its
weekly calendar baseline instead. See [consumption forecasting](consumption-forecasting.md)
for automatic context retrieval, alignment assumptions and the distinction
between this estimated historical series and `forecast` future weather.

A one-day live request at public Berlin reference coordinates returned 24 finite
hourly `estimated` values. This checks provider retrieval and response shape;
it does not qualify a physical thermometer, a site meter or forecast accuracy.
