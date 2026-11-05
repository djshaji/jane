"""Offline command-line interface."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from jane import __version__
from jane.motivation.goals import Goal, Goals
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
    store.add_argument(
        "operation", choices=["status", "seed-demo", "drain", "world", "timeline", "rebuild"]
    )
    store.add_argument("database", type=Path)
    goals = commands.add_parser("goals", help="manage persistent goals without model calls")
    goals.add_argument("operation", choices=["add", "list", "review", "cancel"])
    goals.add_argument("database", type=Path)
    goals.add_argument("--file", type=Path)
    goals.add_argument("--id")
    goals.add_argument("--allow", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "goals":
            from datetime import UTC, datetime

            journal = Journal(args.database)
            try:
                service = Goals(journal)
                if args.operation == "add":
                    if args.file is None:
                        raise ValueError("add requires --file")
                    service.add(Goal.model_validate_json(args.file.read_text()))
                elif args.operation == "cancel":
                    if args.id is None:
                        raise ValueError("cancel requires --id")
                    service.change(args.id, "cancel", datetime.now(UTC))
                elif args.operation == "review":
                    sys.stdout.write(
                        json.dumps(service.review(datetime.now(UTC), tuple(args.allow))) + "\n"
                    )
                    return 0
                sys.stdout.write(
                    json.dumps([g.model_dump(mode="json") for g in service.list()]) + "\n"
                )
                return 0
            finally:
                journal.close()
        if args.command == "journal":
            journal = Journal(args.database)
            try:
                if args.operation == "world":
                    from datetime import UTC, datetime

                    sys.stdout.write(
                        json.dumps(journal.world(datetime.now(UTC)), sort_keys=True) + "\n"
                    )
                    return 0
                if args.operation == "timeline":
                    sys.stdout.write(
                        json.dumps([e.model_dump(mode="json") for e in journal.timeline()]) + "\n"
                    )
                    return 0
                if args.operation == "rebuild":
                    journal.rebuild()
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
