"""Offline command-line interface."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from jane import __version__
from jane.runner import Scenario, demo_scenario, run
from jane.storage.journal import Journal


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jane", description="JANE offline foundation")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="replay the packaged deterministic scenario")
    replay = commands.add_parser("replay", help="validate and replay a JSON fixture")
    replay.add_argument("fixture", type=Path)
    store = commands.add_parser("journal", help="inspect durable storage or queue demo events")
    store.add_argument("operation", choices=["status", "seed-demo", "drain"])
    store.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "journal":
            journal = Journal(args.database)
            try:
                if args.operation == "seed-demo":
                    for event in run(demo_scenario()).events:
                        journal.accept(event)
                elif args.operation == "drain":
                    from datetime import UTC, datetime

                    while (lease := journal.claim(datetime.now(UTC).timestamp())) is not None:
                        # Internal no-op receipt processing, not external tool dispatch.
                        journal.settle(lease, datetime.now(UTC).timestamp())
                sys.stdout.write(json.dumps(journal.status(), sort_keys=True) + "\n")
                return 0
            finally:
                journal.close()
        scenario = (
            demo_scenario()
            if args.command == "demo"
            else Scenario.model_validate_json(args.fixture.read_text(encoding="utf-8"))
        )
        output = run(scenario).render()
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"jane: {error}", file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0
