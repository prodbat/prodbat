"""python -m prodbat_server run                  migrate, then serve and watch, in one process
python -m prodbat_server add-site yourapp.com --email you@example.com
python -m prodbat_server migrate | serve | worker    the same pieces, separately
python -m prodbat_server probe                     checks from another region (PRODBAT_REGION)
python -m prodbat_server vapid-keys                a key pair for phone notifications (web push)
python -m prodbat_server telegram-webhook          tell Telegram where to send the bot's messages
"""

import argparse
import asyncio
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import Settings
from .models import Project, hash_secret, new_secret, normalize_domain


async def add_site(domain: str, email: str | None, new_key: bool) -> None:
    from fastapi import HTTPException

    from .api import create_project, script_tag

    settings = Settings()
    engine = create_async_engine(settings.database_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        project = await db.scalar(select(Project).where(Project.domain == normalize_domain(domain)))
        if project is None:
            try:
                made = await create_project(db, settings, domain, email)
            except HTTPException as exc:
                raise SystemExit(exc.detail) from None
            tag, secret, page = made["script_tag"], made["secret_key"], made["page_url"]
            print(f"{made['domain']} is set up.")
        else:
            secret = None
            if new_key:
                secret = new_secret()
                project.secret_hash = hash_secret(secret)
            if email:
                project.alert_email = email
            await db.commit()
            tag, page = script_tag(settings, project), f"{settings.base}/d/{project.page_token}"
            print(f"{project.domain} was already set up.")
    await engine.dispose()
    print(f"\nAdd this to every page:\n\n  {tag}\n")
    print(f"Your page (keep this link private):\n\n  {page}\n")
    if secret:
        print("Secret key, for your server and the CLI. It is shown once; set it as PRODBAT_KEY:\n")
        print(f"  {secret}\n")


async def telegram_webhook() -> None:
    import httpx

    from .telegram import register_webhook

    settings = Settings()
    if not settings.telegram_configured:
        raise SystemExit("Set PRODBAT_TELEGRAM_BOT_TOKEN, PRODBAT_TELEGRAM_BOT_NAME and "
                         "PRODBAT_TELEGRAM_WEBHOOK_SECRET first.")
    async with httpx.AsyncClient(timeout=20) as http:
        try:
            said = await register_webhook(http, settings)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None
    print(f"{said}. Telegram will send the bot's messages to {settings.base}/telegram/webhook.")


def migrate() -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    command.upgrade(config, "head")


async def serve(host: str, port: int, with_worker: bool) -> None:
    import uvicorn

    from .app import create_app
    from .worker import worker

    server = uvicorn.Server(uvicorn.Config(create_app(), host=host, port=port, log_level="info"))
    watching = asyncio.create_task(worker()) if with_worker else None
    try:
        await server.serve()
    finally:
        if watching:
            watching.cancel()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="prodbat_server")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add-site", help="register a site and print its script tag, page link and key")
    add.add_argument("domain")
    add.add_argument("--email", help="where alerts and the weekly email go")
    add.add_argument("--new-key", action="store_true", help="replace the secret key of a site that already exists")
    commands.add_parser("worker", help="outside checks, problem detection and email; run one alongside the server")
    commands.add_parser("migrate", help="create or update the database tables")
    commands.add_parser("probe", help="check sites from another region (set PRODBAT_REGION); never alerts")
    commands.add_parser("vapid-keys", help="print a new key pair for phone notifications (web push)")
    commands.add_parser("telegram-webhook", help="point the Telegram bot at this server (the worker also does this)")
    for name, text in (("serve", "the web server only"), ("run", "migrate, then the web server and the worker together")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--host", default="0.0.0.0")
        sub.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args(argv)
    if args.command == "add-site":
        asyncio.run(add_site(args.domain, args.email, args.new_key))
    elif args.command == "worker":
        from .worker import worker

        asyncio.run(worker())
    elif args.command == "migrate":
        migrate()
    elif args.command == "vapid-keys":
        from .push import new_vapid_keys

        public, private = new_vapid_keys()
        print("Set these where the server and the worker read their settings. Keep the private key secret;")
        print("if you change the pair later, every device has to turn notifications on again.\n")
        print(f"PRODBAT_VAPID_PUBLIC_KEY={public}")
        print(f"PRODBAT_VAPID_PRIVATE_KEY={private}")
        print("PRODBAT_VAPID_SUBJECT=mailto:you@example.com")
    elif args.command == "telegram-webhook":
        asyncio.run(telegram_webhook())
    elif args.command == "probe":
        from .probe import probe

        asyncio.run(probe())
    elif args.command in ("serve", "run"):
        if args.command == "run":
            migrate()
        asyncio.run(serve(args.host, args.port, with_worker=args.command == "run"))


if __name__ == "__main__":
    main()
