# Sites, assets and accounts

A site belongs to one `user_id` and has an IANA timezone. Optional coordinates
provide weather defaults. An account belongs to one user and optionally one
existing site of that user. Identifiers must be unique in an agent instance.

An asset belongs to one existing site. Its `kind` is an extensible string, such
as `heat-pump`, `industrial-meter` or `thermal-store`. `parent_id` describes a
same-site hierarchy; cycles are rejected. `account_ids` links the asset to
accounts at that site. `metadata` holds operator descriptions, not secrets.

```json
{
  "sites": [{"id": "home", "user_id": "alice", "name": "Home", "timezone": "Europe/London"}],
  "accounts": [{
    "id": "meter-account", "user_id": "alice", "site_id": "home",
    "toolkit": "octopus-energy-account",
    "auth": {"scheme": "basic", "credential_env": "OCTOPUS_API_KEY"},
    "settings": {"mpan": "OPERATOR_MPAN", "serial_number": "OPERATOR_SERIAL"}
  }],
  "assets": [{
    "id": "main-meter", "site_id": "home", "kind": "electricity-meter",
    "name": "Main meter", "account_ids": ["meter-account"]
  }]
}
```

A session scopes a user, optional site, toolkit allowlist, action policy and
per-toolkit account pins. Site selection rejects another user's site. Execution
and resolution reject foreign accounts and assets. An asset binding may further
pin a specific account; both the account's site and the asset's links must agree.
Equal available sources remain ambiguous until the caller chooses a tool or
account. The authenticated host establishes identity before creating sessions.
An in-process SDK session is an operator API, not a login credential.

A reviewed capability binding can identify an asset and declare quantity
contracts, argument mapping, coverage, quality and source preference. Defaults
supply optional inputs. `fixed_arguments` prevents changing a semantic selector,
such as an ENTSO-E document type or Elexon dataset, under the same capability.
Execution rechecks returned kind, unit and declared resolution before persistence.

Asset names do not establish a measurement's physical meaning. A cumulative
meter reading needs explicit counter differencing. A power series needs explicit
integration. Grid power in MW is distinct from a site's PV energy in kWh.
`get_grid_generation` therefore has a separate reviewed grid contract from
site-oriented `get_generation` telemetry.

Local calendar workflows use the site's timezone to derive offset-aware UTC
windows. A DST day may contain 23 or 25 hours. Source intervals, missing values,
original units and derived lineage remain evidence rather than being filled or
relabelled implicitly. See [workflows](workflows.md).
