# Manage local connections and OAuth

Use this guide to configure provider credentials for a self-hosted instance.
The encrypted vault belongs to the operator. Agents see connection metadata, never
access tokens, refresh tokens, client secrets, or environment variable values.

The command examples use `--state-dir .energy-agent`. The vault database is stored
at `.energy-agent/vault/auth.sqlite3`.

## Configure the encrypted vault

Generate a Fernet key once and store it in your secret manager. Keep the key out of
Git, configuration files, shell history, and agent prompts.

```sh
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
export ENERGY_AUTH_MASTER_KEY='replace-with-the-generated-key'
```

Set `vault.master_key_env` in the local configuration. The default environment
variable name is `ENERGY_AUTH_MASTER_KEY`, so the `vault` object may be omitted only
when the application creates the `AuthStore` itself.

```json
{
  "user_id": "local",
  "site_id": "home",
  "sites": [
    {
      "id": "home",
      "user_id": "local",
      "name": "Home",
      "timezone": "Europe/London"
    }
  ],
  "accounts": [
    {
      "id": "octopus-home",
      "user_id": "local",
      "site_id": "home",
      "toolkit": "octopus-energy-account",
      "auth": {"scheme": "basic"},
      "settings": {
        "mpan": "operator-configured-meter-id",
        "serial_number": "operator-configured-meter-serial"
      }
    }
  ],
  "vault": {"master_key_env": "ENERGY_AUTH_MASTER_KEY"}
}
```

Copy this file to an ignored path such as `local-config.json`. The settings above
identify the provider account. They do not contain its secret.

Configure the account from an environment reference. The CLI reads the value once,
encrypts it, and stores only encrypted credential material. It does not store the
name of `OCTOPUS_API_KEY` in the public connection record.

```sh
export OCTOPUS_API_KEY='load-this-from-your-secret-manager'
uv run energy-agent connections \
  --config local-config.json \
  --state-dir .energy-agent \
  --operation configure \
  --account-id octopus-home \
  --credential-env OCTOPUS_API_KEY
```

List the connections for the configured user and site. The output contains the
account ID, toolkit, site, authentication scheme, state, and verification flag.

```sh
uv run energy-agent connections \
  --config local-config.json \
  --state-dir .energy-agent \
  --operation list
```

The vault refuses a missing or invalid Fernet key. It never falls back to plaintext
storage. The key is held in process memory, so an operating-system administrator can
still inspect a running process. If you lose the key, create a new vault and
configure each connection again.

## Use environment references without a vault

An account may still use `auth.credential_env` when the process itself owns the
credential. This mode is useful for a short-lived local process, but the environment
is visible to a process administrator and the runtime cannot refresh OAuth tokens.
Use the vault for persistent local connections.

```json
{
  "id": "home-assistant",
  "user_id": "local",
  "site_id": "home",
  "toolkit": "home-assistant",
  "auth": {
    "scheme": "bearer",
    "credential_env": "HOME_ASSISTANT_TOKEN"
  },
  "settings": {"base_url": "http://homeassistant.local:8123"}
}
```

Never place the credential value in `settings`, tool arguments, URLs, MCP metadata,
or an agent prompt. `ConnectedAccount` rejects credential-shaped settings, and the
runtime redacts configured values from results and events.

## Run connection lifecycle commands

The `connections` command requires a configured vault. Use these operations with
`--account-id`:

```sh
# Check the local encrypted record and credential state.
uv run energy-agent connections --config local-config.json --operation verify --account-id octopus-home

# Refresh an OAuth access token when its expiry is near.
uv run energy-agent connections --config local-config.json --operation refresh --account-id octopus-home

# Stop execution while retaining the encrypted credential.
uv run energy-agent connections --config local-config.json --operation disable --account-id octopus-home

# Remove encrypted credential material and mark the connection revoked.
uv run energy-agent connections --config local-config.json --operation revoke --account-id octopus-home

# Re-enable a disabled connection that still has its credential.
uv run energy-agent connections --config local-config.json --operation reconnect --account-id octopus-home
```

`verify` performs local validation. It decrypts the credential and checks that the
connection is active. It does not contact the provider and does not set
`last_verified_at`.

`reconnect` re-enables a disabled connection. A revoked connection has no stored
credential and must be configured again. `revoke` changes only the local vault. It
does not call a provider revocation endpoint, so revoke the grant in the provider's
account console when the provider offers that control.

## Complete an OAuth authorization-code flow

Register an OAuth account without a placeholder token. The account remains `pending`
and cannot execute tools until the callback stores an access token.

```json
{
  "id": "provider-home",
  "user_id": "local",
  "site_id": "home",
  "toolkit": "provider-energy",
  "auth": {"scheme": "oauth"},
  "settings": {"base_url": "https://provider.example"}
}
```

Create an operator-owned provider file. Use an environment reference for a
confidential client secret. The CLI removes `client_secret_env` before it constructs
`OAuthProvider`.

```json
{
  "authorization_endpoint": "https://provider.example/oauth/authorize",
  "token_endpoint": "https://provider.example/oauth/token",
  "client_id": "energy-agent-tools",
  "client_secret_env": "PROVIDER_OAUTH_CLIENT_SECRET",
  "redirect_uri": "http://127.0.0.1:8766/oauth/callback",
  "scopes": ["meter.read", "tariff.read"]
}
```

Use HTTPS for authorization and token endpoints in a deployment. `OAuthProvider`
also permits an HTTP endpoint on `localhost`, `127.0.0.1`, or `::1` for a local
provider test. The CLI callback is the fixed loopback workflow: its URI must use
`127.0.0.1`, include a port, and match the URI registered with the provider exactly.

Configure the pending account, then start authorization.

```sh
export ENERGY_AUTH_MASTER_KEY='replace-with-the-generated-key'
export PROVIDER_OAUTH_CLIENT_SECRET='load-this-from-your-secret-manager'

uv run energy-agent connections \
  --config local-config.json \
  --state-dir .energy-agent \
  --operation configure \
  --account-id provider-home

uv run energy-agent connections \
  --config local-config.json \
  --state-dir .energy-agent \
  --operation authorize \
  --account-id provider-home \
  --oauth-provider oauth-provider.json \
  --open-browser
```

Load `PROVIDER_OAUTH_CLIENT_SECRET` from your secret manager instead of putting a
client secret in this file, Git, or shell history. If you choose another name,
make it exactly match the provider file's `client_secret_env` value.

`begin_oauth()` creates a short-lived, one-time state and an S256 PKCE verifier. The
store hashes the state and encrypts the verifier with the pending connection record.
The callback checks the user, state, expiry, and exact redirect URI before it sends
the authorization code to the token endpoint. The callback returns connection
metadata only.

If the provider rejects the exchange, the token endpoint has a transient failure,
or the process stops after the callback consumes its state, that state cannot be
replayed. Run the `authorize` command again to create a new state. A failed
exchange leaves the connection pending without restoring a previous token.

The runtime refreshes an OAuth connection when its expiry is within 60 seconds. You
can also call `AuthStore.refresh(user_id, connection_id)` from trusted application
code or use the CLI operation above. Refresh requests are serialized per connection,
and a disable or revoke that occurs during a provider request prevents late tokens
from being stored.

## Verify a provider from trusted application code

Use `AuthStore.verify_provider()` when you need a provider request. The probe is
application code supplied by the operator. The agent cannot provide the probe or see
the credential.

```python
import httpx

from energy_agent_tools.auth import AuthStore


async def probe(connection, credential: str) -> bool:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(
            "https://provider.example/api/account",
            headers={"Authorization": f"Bearer {credential}"},
        )
    return response.status_code == 200


account = await store.verify_provider(
    "local",
    "provider-home",
    probe,
    site_id="home",
)
```

The store sets `last_verified_at` only when the probe returns `True`. Provider
responses and probe exceptions become safe structured errors. A local `verify()` call
does not make a provider verification claim.

## Understand the boundaries

The vault scopes every account by `user_id`, `site_id`, and connection ID. The
runtime adds toolkit and session policy checks before it injects a credential into
trusted connector code. `ConnectedAccount.public()` omits settings and credential
material.

The current implementation does not provide a hosted OAuth login, dynamic client
registration, a central secret service, or hardware verification. The loopback
callback is an operator workflow for one local host. Multi-user HTTP authentication
uses operator-managed bearer principals. See [self-hosting](self-hosting.md) for
that deployment path.
