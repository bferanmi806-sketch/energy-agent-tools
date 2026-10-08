from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any

from .app import build_agent, configure_mcp, load_config
from .server import create_server


def _load_cli_config(config_path: Path | None, state_dir: Path) -> dict[str, Any]:
    if config_path is not None:
        return load_config(config_path)
    profile_path = state_dir / "profile.json"
    return load_config(profile_path) if profile_path.is_file() else {}


def _validate_fernet_key(key: bytes) -> bytes:
    from cryptography.fernet import Fernet

    try:
        Fernet(key)
    except (TypeError, ValueError) as exc:
        raise ValueError("Vault key is invalid") from exc
    return key


def _managed_vault_config(
    state_dir: Path, config: dict[str, Any]
) -> tuple[dict[str, Any], bytes | None, Path | None]:
    """Resolve the configured vault source without creating or replacing a key."""
    import stat

    root = state_dir.resolve()
    configured = config.get("vault")
    create_default_key = False
    if configured in (None, {}):
        vault = {"master_key_file": "vault.key"}
        key_path = state_dir / "vault.key"
        updated = {**config, "vault": vault}
        create_default_key = True
    elif not isinstance(configured, dict):
        raise ValueError("Vault configuration must be an object")
    else:
        if set(configured) - {"master_key_env", "master_key_file"}:
            raise ValueError("Unknown vault configuration field")
        if "master_key_env" in configured and "master_key_file" in configured:
            raise ValueError("Choose one vault key source")
        updated = config
        if "master_key_env" in configured:
            env_name = configured["master_key_env"]
            if not isinstance(env_name, str) or not env_name:
                raise ValueError("Vault environment key name is invalid")
            environment_key = os.environ.get(env_name)
            if not environment_key:
                raise ValueError("Configured vault master key is unavailable")
            return updated, _validate_fernet_key(environment_key.encode()), None
        if "master_key_file" not in configured:
            updated = {**config, "vault": {"master_key_file": "vault.key"}}
            key_path = state_dir / "vault.key"
            create_default_key = True
        else:
            filename = configured["master_key_file"]
            if not isinstance(filename, str) or not filename:
                raise ValueError("Vault key file path is invalid")
            key_path = state_dir / filename

    try:
        resolved_path = key_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("Vault key file path is invalid") from exc
    if key_path.is_symlink() or not resolved_path.is_relative_to(root):
        raise ValueError("Vault key file must be inside the private state directory")
    if key_path.exists():
        if not stat.S_ISREG(key_path.stat().st_mode):
            raise ValueError("Vault key path must be a regular file")
        try:
            key = _validate_fernet_key(key_path.read_bytes().strip())
        except OSError as exc:
            raise ValueError("Vault key file cannot be read") from exc
        try:
            state_dir.chmod(0o700)
            key_path.parent.chmod(0o700)
            key_path.chmod(0o600)
        except OSError:
            pass
        return updated, key, None
    if not create_default_key:
        raise ValueError("Configured vault key file is unavailable")
    return updated, None, key_path


def _ensure_private_key(path: Path, state_dir: Path) -> bytes:
    """Create a Fernet key exclusively, using the private-profile file convention."""
    import os

    from cryptography.fernet import Fernet

    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        state_dir.chmod(0o700)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
    except OSError:
        # Keep compatibility with platforms where POSIX mode changes are unavailable.
        pass
    if path.is_symlink():
        raise ValueError("Vault key cannot be a symlink")
    if path.exists():
        try:
            key = _validate_fernet_key(path.read_bytes().strip())
        except OSError as exc:
            raise ValueError("Vault key file cannot be read") from exc
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return key

    key = Fernet.generate_key()
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink():
                raise ValueError("Vault key cannot be a symlink") from None
            try:
                key = _validate_fernet_key(path.read_bytes().strip())
            except OSError as exc:
                raise ValueError("Vault key file cannot be read") from exc
        try:
            path.chmod(0o600)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
        return key
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _prepare_managed_vault(
    state_dir: Path, config: dict[str, Any], *, create_key: bool
) -> tuple[dict[str, Any], bytes | None]:
    updated, key, missing_path = _managed_vault_config(state_dir, config)
    if missing_path is None:
        return updated, key
    if not create_key:
        return updated, None
    auth_db = state_dir / "vault" / "auth.sqlite3"
    if auth_db.exists() or auth_db.is_symlink():
        raise ValueError("Vault key is missing for existing encrypted state")
    return updated, _ensure_private_key(missing_path, state_dir)


def _preflight_auth_store(state_dir: Path, key: bytes) -> None:
    from .auth import AuthStore

    store = AuthStore(state_dir / "vault", key)
    try:
        store.validate_encryption_key()
    finally:
        store.close()


def _bootstrap(args: argparse.Namespace, config: dict[str, Any]) -> None:
    from .control_store import ControlStore

    for value in (args.owner_name, args.name):
        if not value.strip() or len(value) > 256:
            raise ValueError("Owner and workspace names must contain 1 to 256 characters")
    state_dir = args.state_dir
    updated_config, key = _prepare_managed_vault(state_dir, config, create_key=False)
    # Opening the control database first validates existing state before we create
    # any new vault key or publish a bootstrap credential.
    controls = ControlStore(state_dir / "control")
    try:
        if key is None:
            auth_db = state_dir / "vault" / "auth.sqlite3"
            if auth_db.exists() or auth_db.is_symlink():
                raise ValueError("Vault key is missing for existing encrypted state")
            key = _ensure_private_key(state_dir / "vault.key", state_dir)
        if key is None:
            raise ValueError("Vault master key is unavailable")
        _preflight_auth_store(state_dir, key)
        result = controls.bootstrap_workspace(args.owner_name, args.name)
        print(
            json.dumps(
                {
                    "ok": True,
                    "owner": result.user.model_dump(mode="json"),
                    "workspace": result.workspace.model_dump(mode="json"),
                    "management_key": {
                        "id": result.key.key.id,
                        "name": result.key.key.name,
                        "token": result.key.token,
                    },
                },
                indent=2,
            )
        )
    finally:
        controls.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Self-hosted energy tool gateway")
    parser.add_argument(
        "command",
        choices=[
            "serve",
            "host",
            "bootstrap",
            "catalogue",
            "manifests",
            "validate",
            "scaffold",
            "connections",
            "setup",
            "connect",
            "run-skill",
            "backup",
            "restore",
            "inspect-backup",
        ],
    )
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--include-vault-key", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path(".energy-agent"))
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind-host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--user-id", default="local")
    parser.add_argument("--site-id")
    parser.add_argument("--name", default="Home")
    parser.add_argument("--owner-name", default="Home owner")
    parser.add_argument("--managed-workspaces", action="store_true")
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--provider", choices=["octopus", "home_assistant", "emoncms"])
    parser.add_argument("--base-url")
    parser.add_argument("--mpan")
    parser.add_argument("--serial-number")
    parser.add_argument("--feed-id", type=int)
    parser.add_argument("--entity-id")
    parser.add_argument("--telemetry-role")
    parser.add_argument("--quantity-shape", choices=["interval", "instantaneous", "counter"])
    parser.add_argument("--unit")
    parser.add_argument("--credential-stdin", action="store_true")
    parser.add_argument("--skill-id")
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--output", type=Path, default=Path("manifests"))
    parser.add_argument("--toolkit", default="local_connector")
    parser.add_argument(
        "--operation",
        choices=[
            "list",
            "configure",
            "verify",
            "refresh",
            "disable",
            "revoke",
            "reconnect",
            "authorize",
        ],
        default="list",
    )
    parser.add_argument("--account-id")
    parser.add_argument("--credential-env")
    parser.add_argument("--oauth-provider", type=Path)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if args.command == "bootstrap":
        try:
            config = _load_cli_config(args.config, args.state_dir)
            _bootstrap(args, config)
        except Exception:
            print(json.dumps({"ok": False, "error": "bootstrap_failed"}))
            raise SystemExit(1) from None
        return
    if args.command in {"backup", "restore", "inspect-backup"}:
        from .maintenance import MaintenanceError, create_backup, inspect_backup, restore_backup

        if not args.archive:
            parser.error("Maintenance requires --archive")
        try:
            if args.command == "backup":
                key_path = args.state_dir / "vault.key"
                if args.include_vault_key and key_path.is_symlink():
                    parser.error("Vault key cannot be a symlink")
                key = key_path.read_bytes().strip() if args.include_vault_key else None
                manifest = create_backup(
                    args.state_dir,
                    args.archive,
                    vault_key=key,
                    include_vault_key=args.include_vault_key,
                )
            elif args.command == "restore":
                if not args.target:
                    parser.error("Restore requires a new --target directory")
                manifest = restore_backup(args.archive, args.target)
            else:
                manifest = inspect_backup(args.archive)
        except (MaintenanceError, OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": getattr(exc, "code", "maintenance_failed")}))
            raise SystemExit(1) from None
        print(json.dumps({"ok": True, "manifest": manifest.as_json()}, indent=2))
        return
    if args.command in {"setup", "connect"}:
        from .models import EnergyError
        from .onboarding import LocalProfile

        async def onboard() -> dict:
            async with LocalProfile(args.state_dir) as profile:
                if args.command == "setup":
                    site = profile.create_site(
                        args.name,
                        args.timezone,
                        user_id=args.user_id,
                        site_id=args.site_id,
                    )
                    return {"ok": True, "site": site.model_dump(mode="json")}
                if not args.provider:
                    parser.error("connect requires --provider")
                import getpass
                import sys

                if args.credential_stdin:
                    credential = sys.stdin.read(4097).strip()
                else:
                    if not sys.stdin.isatty():
                        parser.error("Use a terminal prompt or --credential-stdin")
                    credential = getpass.getpass("Provider credential: ")
                if not credential or len(credential) > 4096:
                    parser.error("A nonempty credential of at most 4096 characters is required")
                metadata = {
                    key: value
                    for key, value in {
                        "base_url": args.base_url,
                        "mpan": args.mpan,
                        "serial_number": args.serial_number,
                        "feed_id": args.feed_id,
                        "entity_id": args.entity_id,
                        "telemetry_role": args.telemetry_role,
                        "quantity_shape": args.quantity_shape,
                        "unit": args.unit,
                    }.items()
                    if value is not None
                }
                if args.telemetry_role:
                    metadata["measurement_kind"] = "metered"
                outcome = await profile.connect(
                    args.provider,
                    credential=credential,
                    user_id=args.user_id,
                    site_id=args.site_id,
                    connection_id=args.account_id,
                    metadata=metadata,
                )
                return {
                    "ok": outcome["ok"],
                    "account": outcome["account"],
                    "health": outcome["health"],
                }

        try:
            outcome = asyncio.run(onboard())
        except (EnergyError, ValueError, OSError):
            print(json.dumps({"ok": False, "error": "onboarding_failed"}))
            raise SystemExit(1) from None
        print(json.dumps(outcome, indent=2))
        if not outcome["ok"]:
            raise SystemExit(1)
        return
    if args.command == "scaffold":
        from .connector_sdk import scaffold

        print(scaffold(args.output, args.toolkit))
        return
    try:
        config = _load_cli_config(args.config, args.state_dir)
    except Exception:
        if args.command == "host" and args.managed_workspaces:
            print(json.dumps({"ok": False, "error": "managed_host_configuration_failed"}))
            raise SystemExit(1) from None
        raise
    if args.command == "host" and args.managed_workspaces:
        try:
            config, managed_key = _prepare_managed_vault(args.state_dir, config, create_key=True)
            if managed_key is None:
                raise ValueError("Vault master key is unavailable")
        except Exception:
            print(json.dumps({"ok": False, "error": "managed_host_configuration_failed"}))
            raise SystemExit(1) from None
    try:
        data_root = Path(config["data_root"]).resolve() if config.get("data_root") else None
    except (OSError, TypeError, ValueError):
        if args.command == "host" and args.managed_workspaces:
            print(json.dumps({"ok": False, "error": "managed_host_configuration_failed"}))
            raise SystemExit(1) from None
        raise
    try:
        agent = build_agent(args.state_dir, config, data_root=data_root)
    except Exception:
        if args.command == "host" and args.managed_workspaces:
            print(json.dumps({"ok": False, "error": "managed_host_configuration_failed"}))
            raise SystemExit(1) from None
        raise
    session = agent.session(
        args.user_id
        if args.user_id != "local"
        else config.get("user_id", os.environ.get("ENERGY_USER_ID", "local")),
        args.site_id or config.get("site_id"),
    )
    if config.get("mcp_servers"):
        try:
            asyncio.run(configure_mcp(agent, config))
        except BaseException:
            asyncio.run(agent.close())
            raise
    if args.command == "host":
        from datetime import datetime

        import uvicorn

        from .control_store import ControlStore
        from .hosting import Principal, create_host

        options = config.get("hosting", {})
        persistent_keys = options.get("persistent_keys", False)
        if not isinstance(persistent_keys, bool):
            parser.error("hosting.persistent_keys must be a boolean")
        managed_workspaces = args.managed_workspaces
        control_store = (
            ControlStore(args.state_dir / "control")
            if persistent_keys or managed_workspaces
            else None
        )
        principals = {
            item["user_id"]: Principal(
                item["user_id"],
                set(item["allowed_site_ids"]),
                item["token_digest"],
                expires_at=datetime.fromisoformat(item["expires_at"].replace("Z", "+00:00"))
                if item.get("expires_at")
                else None,
                revoked=item.get("revoked", False),
                token_id=item.get("token_id"),
            )
            for item in options.get("principals", [])
        }
        host_options = {
            "max_requests_per_minute": options.get("max_requests_per_minute", 60),
            "max_body_bytes": options.get("max_body_bytes", 1000000),
            "max_sessions_per_user": options.get("max_sessions_per_user", 10),
            "session_idle_timeout": options.get("session_idle_timeout", 1800.0),
            "max_sessions_global": options.get("max_sessions_global", 1000),
            "close_agent_on_shutdown": True,
            "control_store": control_store,
        }
        if managed_workspaces:
            host_options["managed_workspaces"] = True
        try:
            from pydantic import TypeAdapter

            from .cloud_oauth import CloudEnergyOAuthConfiguration
            from .managed_oauth import HomeAssistantOAuthConfiguration

            configurations = TypeAdapter(
                list[HomeAssistantOAuthConfiguration | CloudEnergyOAuthConfiguration]
            ).validate_python(options.get("managed_oauth_configurations", []), strict=True)
            if configurations:
                host_options["managed_oauth_configurations"] = tuple(configurations)
            application = create_host(agent, principals, **host_options)
            uvicorn.run(application, host=args.bind_host, port=args.port, access_log=False)
        except Exception:
            if managed_workspaces:
                print(json.dumps({"ok": False, "error": "managed_host_failed"}))
                raise SystemExit(1) from None
            raise
        finally:
            asyncio.run(agent.close())
            if control_store is not None:
                control_store.close()
    elif args.command == "run-skill":
        from .workflows import run_skill

        try:
            if not args.skill_id:
                parser.error("run-skill requires --skill-id")
            parameters = load_config(args.parameters)
            result = asyncio.run(run_skill(agent, session, args.skill_id, parameters))
            print(json.dumps(result, indent=2))
            if not result["ok"]:
                raise SystemExit(1)
        finally:
            asyncio.run(agent.close())
    elif args.command == "validate":
        from .connector_sdk import validate_registry

        report = validate_registry(agent.registry)
        print(json.dumps(report, indent=2))
        asyncio.run(agent.close())
        if not report["ok"]:
            raise SystemExit(1)
    elif args.command == "connections":
        from .models import EnergyError

        store = agent.auth_store
        if store is None:
            parser.error("Configure an encrypted vault before using connection lifecycle commands")
        user_id = session.user_id
        try:
            if args.operation == "list":
                print(
                    json.dumps(
                        [a.public() for a in store.accounts(user_id, session.site_id)], indent=2
                    )
                )
            else:
                if not args.account_id:
                    parser.error("This operation requires --account-id")
                account = agent.accounts.get(args.account_id)
                if args.operation == "configure":
                    if (
                        account is None
                        or account.user_id != user_id
                        or (session.site_id and account.site_id != session.site_id)
                    ):
                        parser.error("Account must be configured within the selected user/site")
                    credential = os.environ.get(args.credential_env or "", "")
                    if not credential and account.auth.scheme != "oauth":
                        parser.error("Supply an existing --credential-env reference")
                    store.configure(account, credential)
                    print(
                        json.dumps(store.get_account(user_id, account.id, session.site_id).public())
                    )
                elif args.operation == "authorize":
                    if not args.oauth_provider:
                        parser.error(
                            "Authorization requires an operator OAuth provider configuration"
                        )
                    import webbrowser
                    from urllib.parse import urlparse

                    import uvicorn

                    from .auth import OAuthProvider
                    from .oauth_callback import callback_app

                    spec = json.loads(args.oauth_provider.read_text())
                    secret_env = spec.pop("client_secret_env", None)
                    if secret_env:
                        spec["client_secret"] = os.environ.get(secret_env)
                    provider = OAuthProvider(**spec)
                    store.get_account(user_id, args.account_id, session.site_id)
                    authorization = store.begin_oauth(user_id, args.account_id, provider)
                    oauth_application = callback_app(
                        store, user_id, authorization, provider.redirect_uri
                    )
                    print(authorization.authorization_url, flush=True)
                    if args.open_browser:
                        webbrowser.open(authorization.authorization_url)
                    uvicorn.run(
                        oauth_application,
                        host="127.0.0.1",
                        port=urlparse(provider.redirect_uri).port or 8766,
                        access_log=False,
                    )
                else:
                    store.get_account(user_id, args.account_id, session.site_id)
                    if args.operation == "refresh":
                        account = asyncio.run(store.refresh(user_id, args.account_id))
                    else:
                        account = getattr(store, args.operation)(
                            user_id, args.account_id, session.site_id
                        )
                    print(json.dumps(account.public()))
        except (EnergyError, ValueError, TypeError) as exc:
            code = exc.code if isinstance(exc, EnergyError) else "invalid_configuration"
            print(json.dumps({"ok": False, "error": code}))
            raise SystemExit(1) from None
        finally:
            asyncio.run(agent.close())
    elif args.command == "catalogue":
        print(json.dumps(agent.catalogue(session), indent=2))
        asyncio.run(agent.close())
    elif args.command == "manifests":
        agent.write_manifests(args.output)
        asyncio.run(agent.close())
    else:
        create_server(agent, session, port=args.port).run(transport=args.transport)
