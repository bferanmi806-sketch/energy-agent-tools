# Deployment and operations

Energy Agent Tools ships a small container image and a Compose file for a
single self-hosted gateway. The image contains the core dependencies and the
authenticated host. It runs as the unprivileged `energy` user, keeps durable
state in `/var/lib/energy-agent`, and does not include an operator config or a
credential. `compose.yaml` publishes the gateway on `127.0.0.1:8765` on the
machine running Compose. Inside the container the host listens on the bridge so
the published port can reach it; keep the host port loopback-only unless an
operator has deliberately placed a TLS reverse proxy in front of it.

## Start the gateway

Create the operator-owned configuration directory and write a non-secret host
configuration. The token digest is the SHA-256 digest of a bearer token held by
your secret manager. Do not put the raw token in this file.

```sh
mkdir -p config backups
cat > config/host-config.json <<'JSON'
{
  "sites": [
    {
      "id": "home",
      "user_id": "local",
      "name": "Home",
      "timezone": "Europe/London"
    }
  ],
  "hosting": {
    "principals": [
      {
        "user_id": "local",
        "allowed_site_ids": ["home"],
        "token_digest": "REPLACE_WITH_SHA256_DIGEST",
        "token_id": "local-2026-10"
      }
    ]
  }
}
JSON
```

Build and start it with:

```sh
docker compose build gateway
docker compose up -d gateway
docker compose ps
```

The state is stored in the named `energy_state` volume. The configuration is a
read-only bind mount. If the configuration enables the encrypted vault, pass
the operator-managed Fernet key through the environment before starting:

```sh
export ENERGY_AUTH_MASTER_KEY='YOUR_FERNET_KEY_FROM_SECRET_STORAGE'
docker compose up -d gateway
```

The Compose file passes an empty value when the vault is not configured. A vault
configuration still fails closed when its key is absent. Keep the key separate
from both the host configuration and any state backup unless you explicitly
choose to include it.

The first run can be checked without a provider or credential. The smoke
profile creates a synthetic meter result and an encrypted connection, backs up
the state, restores it into a new directory, and reads both records after the
restore:

```sh
docker compose --profile smoke run --rm deployment-smoke
```

## Stop before state maintenance

Stop the gateway before creating or restoring state. SQLite's backup API gives
each database a consistent snapshot, but only a stopped deployment provides a
consistent pair of databases and a stable operator configuration.

```sh
docker compose stop gateway
```

The maintenance API recognizes only these durable files:

| File | Contents |
| --- | --- |
| `artifacts.sqlite3` | User and session-scoped workbench artifacts |
| `vault/auth.sqlite3` | Connection metadata and encrypted credential blobs |
| `profile.json` | Local connection/site/asset configuration, when present |
| `jobs/jobs.sqlite3` and bounded job JSON payloads | Durable numerical jobs and results |

It does not copy external `host-config.json`, environment variables, logs, provider
tokens outside the encrypted vault, or arbitrary files from the state volume.
The backup archive is a gzip tar containing those databases and
`manifest.json`. Every database snapshot has a SHA-256 hash and a recognized
schema recorded in the manifest. Archives and restored files are mode `0600`;
the state directory is mode `0700`.

For an application-managed state directory, call the API while the process is
stopped:

```python
from pathlib import Path

from energy_agent_tools.maintenance import create_backup, restore_backup

state = Path(".energy-agent")
archive = Path("backups/energy-state-2026-10-01.tar.gz")
create_backup(state, archive)

# Restore only to a new path. Existing paths are refused and left untouched.
restore_backup(archive, Path(".energy-agent-restored"))
```

The default backup never includes the Fernet key. Keep the key in the same
secret-management system used for the running service and restore it before
opening `vault/auth.sqlite3`. If an offline recovery package must contain the
key, make the choice explicit in code and protect the resulting archive as a
secret:

```python
import os

create_backup(
    state,
    archive,
    vault_key=os.environ["ENERGY_AUTH_MASTER_KEY"],
    include_vault_key=True,
)
```

Restore verifies member paths, rejects absolute paths, traversal, duplicate
members, symlinks, hard links, oversized members, unknown schemas, malformed
SQLite files, and hash mismatches. It extracts into a temporary sibling and
publishes the new directory only after all checks pass. It never overwrites an
existing target. Treat a failed restore as an untrusted archive and obtain a
fresh copy rather than weakening these checks.

After a restore, point the host at the restored state directory and start it:

```sh
docker compose start gateway
```

For a named Compose volume, perform the API call from a short-lived image with
the gateway stopped. Mount the volume read-only for a backup and mount a host
backup directory for the archive. The exact volume name can be read with
`docker volume ls` (Compose normally prefixes it with the project name). A
deployment wrapper should keep this command in its own reviewed script so the
volume name and archive destination cannot be supplied by an agent.

## Recovery sequence

Use this sequence when moving a deployment or recovering from a failed host:

1. Stop the gateway and preserve the original state volume.
2. Verify the archive with `inspect_backup()` or restore it into a new directory.
3. Restore the operator configuration separately and re-inject the vault key.
4. Start the gateway against the restored state.
5. Confirm the authenticated session endpoint, list the expected connections,
   and read a known artifact before removing the old state.

The host does not issue bearer tokens or OAuth client secrets. Rotate those
outside the image, update the ignored operator configuration or secret source,
and restart when changing site topology. Use the existing authenticated host
limits for body size, session count, request rate, and artifact retention.

## Image and network boundary

The image uses Python 3.12 slim, installs the package without engineering
extras, and runs as UID/GID `10001`. `/var/lib/energy-agent` is the only
writable persistent path. `/tmp` is a bounded temporary filesystem in Compose;
the root filesystem and config mount are read-only. The image does not contain
provider credentials or a TLS private key.

Compose binds the host port explicitly to loopback:

```text
127.0.0.1:${ENERGY_HOST_PORT:-8765}:8765
```

Set `ENERGY_HOST_PORT` only when another local service owns 8765. To expose the
gateway beyond the machine, put a TLS reverse proxy on the host, forward the
`Authorization` header, enforce the same body limit, and keep the upstream on
loopback. Do not trust a forwarded user identity or a client-supplied user ID.

## Boundaries of this deployment

This packaging makes a reproducible local deployment and a recoverable state
format. It does not create a hosted account service, a managed key store,
automatic provider registration, or a physical-device control plane. Provider
credentials and OAuth grants remain operator responsibilities. The image also
does not install optional numerical engines; add those dependencies in a
separate reviewed image variant when the deployment actually needs them.

## Maintenance CLI

Stop the gateway and close its job manager before maintenance. For a local
profile, explicitly including its key produces a complete portable backup:

```sh
energy-agent backup --state-dir .energy-agent --archive backups/state.tar.gz --include-vault-key
energy-agent inspect-backup --archive backups/state.tar.gz
energy-agent restore --archive backups/state.tar.gz --target restored-state
```

The key is excluded by default. Keep a separate recoverable key when using that
default. Archives containing a key must be kept in private secret storage.
The restore target must be new. Numerical job paths are regenerated under the
restored directory; a completed result never depends on the old directory.

## Published-version upgrade check

The upgrade check installs the published v0.2.0 wheel in a separate environment.
That installed package creates the old artifact database and encrypted vault.
The current package must read both, enforce ownership, back them up, and read
both again after restore and deletion of the original state directory.

```sh
mkdir -p work/upgrade-baseline
gh release download v0.2.0 --repo bferanmi806-sketch/energy-agent-tools --pattern 'energy_agent_tools-0.2.0-py3-none-any.whl' --dir work/upgrade-baseline
uv venv work/upgrade-baseline/venv --python 3.12
uv pip install --python work/upgrade-baseline/venv/bin/python work/upgrade-baseline/energy_agent_tools-0.2.0-py3-none-any.whl
uv run python scripts/upgrade_smoke.py --baseline-python work/upgrade-baseline/venv/bin/python --baseline-wheel work/upgrade-baseline/energy_agent_tools-0.2.0-py3-none-any.whl
```

The script verifies the baseline wheel against its published SHA-256 digest.
Generated development credentials stay in private temporary files and are
removed after the check. The recorded result is in
`docs/evidence/state-upgrade-v020-to-v030.json`. This qualifies the artifact
and vault formats with synthetic data. REST sessions, physical connections,
and arbitrary operator configurations need separate qualification.
