"""Operator commands run inside the container: python -m app clear-lockout | restore."""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from app.auth.throttle import KeyedLocks, LoginThrottleService
from app.config import ConfigError, Settings, load_settings
from app.db.engine import Database
from app.db.repositories.clocked import utc_now
from app.db.repositories.login_throttle import LoginThrottleRepository
from app.services.activity import CLEAR_LOCKOUT_ACTION, ActivityRecorder, AnonymousActor
from app.services.restore_command import add_parser as add_restore_parser
from app.services.restore_command import execute as execute_restore

OPERATOR_SOURCE = "operator_command"


async def clear_lockout(settings: Settings, subject: str) -> int:
    """Lift every sign-in lock held on a username or address; return how many were lifted."""
    database = Database.from_settings(settings)
    try:
        async with database.session() as db:
            activity = ActivityRecorder(db)
            throttle = LoginThrottleService(
                LoginThrottleRepository(db),
                utc_now,
                max_failures=settings.login_max_failures,
                window=settings.login_lockout_window,
                enforce_address=bool(settings.trusted_proxies),
                locks=KeyedLocks(),
            )
            async with db.transaction():
                removed = await throttle.clear(subject)
                await activity.record_event(
                    AnonymousActor(),
                    action=CLEAR_LOCKOUT_ACTION,
                    target_type="user",
                    result="success",
                    detail={"source": OPERATOR_SOURCE, "subject": subject.strip()[:128]},
                )
    finally:
        await database.aclose()
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested operator command and return the process exit status."""
    parser = argparse.ArgumentParser(prog="python -m app", description="dockmin operator commands")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    lockout = commands.add_parser(
        "clear-lockout", help="lift the sign-in lock held on a username or an address"
    )
    lockout.add_argument(
        "subject", metavar="SUBJECT", help="a username or an address whose lock should be lifted"
    )
    add_restore_parser(commands)
    arguments = parser.parse_args(argv)
    if arguments.command == "restore":
        return execute_restore(arguments)
    try:
        settings = load_settings()
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    if not settings.db_path.is_file():
        print(f"no database at {settings.db_path}", file=sys.stderr)
        return 2
    removed = asyncio.run(clear_lockout(settings, arguments.subject))
    print(f"lifted {removed} lock(s)" if removed else "no lock was held on that subject")
    return 0


if __name__ == "__main__":
    sys.exit(main())
