from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .app import build_agent, configure_mcp, load_config
from .server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Self-hosted energy tool gateway")
    parser.add_argument(
        "command",
        choices=["serve", "host", "catalogue", "manifests", "validate", "scaffold", "connections"],
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path(".energy-agent"))
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
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
    if args.command == "scaffold":
        from .connector_sdk import scaffold

        print(scaffold(args.output, args.toolkit))
        return
    config = load_config(args.config)
    data_root = Path(config["data_root"]).resolve() if config.get("data_root") else None
    agent = build_agent(args.state_dir, config, data_root=data_root)
    session = agent.session(
        config.get("user_id", os.environ.get("ENERGY_USER_ID", "local")), config.get("site_id")
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

        from .hosting import Principal, create_host

        options = config.get("hosting", {})
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
        application = create_host(
            agent,
            principals,
            max_requests_per_minute=options.get("max_requests_per_minute", 60),
            max_body_bytes=options.get("max_body_bytes", 1000000),
            max_sessions_per_user=options.get("max_sessions_per_user", 10),
            session_idle_timeout=options.get("session_idle_timeout", 1800.0),
            max_sessions_global=options.get("max_sessions_global", 1000),
        )
        try:
            uvicorn.run(application, host="127.0.0.1", port=args.port, access_log=False)
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
