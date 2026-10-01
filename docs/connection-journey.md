# Connection journey

`LocalProfile` provides the first-run path for a self-hosted installation:

```text
create profile → create site → connect → verify → resolve → use
```

The CLI and SDK collect provider credentials without placing them in a
configuration file. The CLI prompts without echoing the token and passes it
directly to `LocalProfile.connect`.
The onboarding service does not print, log, return, or persist the token in
plain text.

## Create a local profile

```python
import getpass
from pathlib import Path

from energy_agent_tools.onboarding import LocalProfile

profile = LocalProfile(Path(".energy-agent"))
profile.create_site("Home", "Europe/London", site_id="home")
```

The first construction creates:

```text
.energy-agent/
├── profile.json       # sites, assets, and safe connection metadata
├── vault.key          # Fernet key, mode 0600
└── vault/
    └── auth.sqlite3   # encrypted credential blobs, directory mode 0700
```

The profile directory is mode `0700`. `profile.json` is written with an
atomic replace and mode `0600`; it contains the relative vault key reference
`{"master_key_file": "vault.key"}` for the extended `build_agent` loader.
The key is generated locally and is never printed. Losing it makes the
encrypted credentials unrecoverable, so operators should back it up using
their normal protected filesystem procedure.

## Connect and verify

The supported first-run providers are:

| Provider | Credential scheme | Required non-secret metadata | Reviewed mapping |
| --- | --- | --- | --- |
| Octopus Energy | HTTP Basic username, empty password | `mpan`, `serial_number` | Metered `kWh` consumption |
| Home Assistant | Bearer token | `base_url` | Only explicit telemetry claims |
| Emoncms / OpenEnergyMonitor | API key | `base_url`, numeric `feed_id` | Only explicit telemetry claims |

Octopus always uses the official `api.octopus.energy` origin. Home Assistant
and Emoncms accept an HTTPS self-hosted base URL; loopback HTTP is available
for local fixtures and development services.

```python
result = await profile.connect(
    "octopus",
    credential=getpass.getpass("Octopus API key: "),
    site_id="home",
    connection_id="octopus-home",
    metadata={
        "mpan": "operator-meter-point",
        "serial_number": "operator-meter-serial",
    },
)

assert result["health"]["status"] in {"healthy", "unhealthy"}
safe_build_agent_config = result["config"]
```

`connect` stores the credential in the encrypted vault, performs a real read
probe through the provider adapter, records `last_verified_at` only after a
successful response, and returns a safe object with `account`, `health`, and
`config`. The health response has an explicit `healthy` or `unhealthy` status,
the provider-read probe name, and a generic message. It excludes credentials,
provider response bodies, and authenticated query strings. Provider failures
are reported as an unhealthy result so a UI can explain the next action
without exposing response data.

The injected `httpx.AsyncClient` makes this path testable with a real adapter
request and keeps the default client local to the profile:

```python
import httpx

profile = LocalProfile(
    Path(".energy-agent"),
    http=httpx.AsyncClient(timeout=15, follow_redirects=False),
)
```

For a normal installation the provider base URLs are HTTPS. HTTP is accepted
only for loopback fixtures and self-hosted development services. Base URLs
cannot contain userinfo, query strings, or fragments.

## Reviewed telemetry mappings

Provider metadata is kept separate from credentials. A mapping is added to
`account.settings["capability_bindings"]` only when the operator gives the
semantic role, measurement kind, and unit explicitly.

Home Assistant deserves a strict boundary here. A sensor with
`state_class="total_increasing"` is a cumulative counter; its raw state is
not an interval consumption reading. Onboarding therefore does not create a
`get_energy_consumption` mapping for an arbitrary entity. To review an entity
as interval energy, the metadata must say:

```python
metadata = {
    "base_url": "https://ha.example",
    "entity_id": "sensor.interval_energy",
    "telemetry_role": "consumption_interval",
    "measurement_kind": "metered",
    "unit": "kWh",
    "quantity_shape": "interval",
    "measurement_semantics": "interval_energy",
}
```

The `quantity_shape` field makes the distinction explicit: use `interval` for
energy in a time interval and `instantaneous` for power or a present storage
state. Generation is reviewed as `kWh` intervals or `W`, `kW`, or `MW`
instantaneous power. An export interval uses `kWh` and `interval`. Storage
state accepts instantaneous `%` or `kWh`. Current-power and storage-state
readings use the provider's current-state endpoint; interval readings use the
history endpoint and therefore require a requested time window when resolved.
Leave the mapping unreviewed for a cumulative counter. If these declarations
are absent, the connection remains usable for direct provider reads but
capability resolution will not pretend to know the telemetry semantics.

## Restart and handoff to the agent

The profile can be reopened by a new process. The encrypted store restores the
credential, while `profile.config()` returns only account metadata and the
vault file reference:

```python
reopened = LocalProfile(Path(".energy-agent"))
accounts = reopened.connections("local", "home")
config = reopened.config()
```

The extended application loader resolves `vault.master_key_file` relative to
the profile root, opens the encrypted vault, and loads the safe metadata. An
agent can then resolve the reviewed generic capability and execute the
provider-independent workflow:

```python
session = agent.session("local", "home")
resolution = agent.capability(
    session,
    "get_energy_consumption",
)
result = await agent.dispatch(
    session,
    "get_energy_consumption",
)
```

The resulting energy data keeps its provider, unit, metered/calculated kind,
time coverage, and provenance. Onboarding only establishes the connection and
the reviewed source selection; it does not infer a physical meaning for
provider data that was not declared or validated.

## Failure and secret handling

Invalid providers, malformed metadata, missing sites, unsafe base URLs, and
empty credentials fail before storing a connection. Provider status errors are
generic and contain no URL or response body. The profile and vault are
scoped to the local installation, and every credential lookup remains scoped
to user, site, and connection ID by `AuthStore`.

## CLI journey

```sh
energy-agent setup --state-dir .energy-agent --site-id home --name Home --timezone Europe/London
energy-agent connect --state-dir .energy-agent --site-id home --provider octopus --mpan YOUR_MPAN --serial-number YOUR_SERIAL
energy-agent run-skill --state-dir .energy-agent --site-id home --skill-id yesterday-consumption
```

The second command prompts for the API key without echoing it. For Home
Assistant, use `--provider home_assistant --base-url https://YOUR_INSTANCE`
with the reviewed `--entity-id`, `--telemetry-role`, `--quantity-shape`, and
`--unit` options. `--credential-stdin` supports a pipe from approved secret
storage; it never accepts a credential on the command line.
`EnergyAgentTools(".energy-agent")` and the CLI load the saved profile directly.
Current-power requests reject stale or undated observations. Home Assistant
counters remain counters even if a sensor changes after its mapping is reviewed.
