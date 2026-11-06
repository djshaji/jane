"""Offline command-line interface."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from jane import __version__
from jane.affect.engine import Affect, AffectConfig
from jane.motivation.goals import Goal, Goals
from jane.runner import Scenario, demo_scenario, run
from jane.storage.journal import Journal


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jane", description="JANE offline foundation")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("coordinator-demo", help="run the offline J07 proposal pipeline")
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
    affect = commands.add_parser("affect", help="inspect and configure simulated affect")
    affect.add_argument("operation", choices=["status", "history", "configure", "recover", "reset"])
    affect.add_argument("database", type=Path)
    affect.add_argument("--file", type=Path)
    for mode in ("offline", "live"):
        runtime_parser = commands.add_parser(
            mode, help="run inspection tasks with " + mode + " cognition"
        )
        runtime_parser.add_argument(
            "operation", choices=["submit", "run", "status", "timeline", "pause", "resume", "stop"]
        )
        runtime_parser.add_argument("database", type=Path)
        runtime_parser.add_argument("--text")
        runtime_parser.add_argument("--object", default="cup")
        runtime_parser.add_argument("--max-steps", type=int, default=8)
        if mode == "live":
            runtime_parser.add_argument("--enable-live", action="store_true")
            runtime_parser.add_argument("--max-calls", type=int, default=8)
            runtime_parser.add_argument("--max-output-tokens", type=int, default=4096)
            runtime_parser.add_argument(
                "--reasoning", choices=["low", "medium", "high", "xhigh", "max"], default="medium"
            )
    args = parser.parse_args(argv)
    try:
        if args.command == "live" and args.operation == "run":
            import asyncio

            from jane.live import run_live

            if not args.enable_live:
                raise ValueError("live run requires --enable-live; snapshot data is sent to OpenAI")
            result = asyncio.run(
                run_live(
                    args.database,
                    args.max_steps,
                    args.max_calls,
                    args.max_output_tokens,
                    args.reasoning,
                )
            )
            sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
            return 0
        if args.command in {"offline", "live"}:
            import asyncio

            from jane.core.clock import SystemClock
            from jane.offline import OfflineRuntime

            journal = Journal(args.database)
            runtime = None
            try:
                runtime = OfflineRuntime(
                    journal, Path(str(args.database) + ".sim.db"), SystemClock()
                )
                if args.operation in {"submit", "run"}:
                    runtime.startup()
                if args.operation == "submit":
                    if not args.text:
                        raise ValueError("submit requires --text")
                    result_data: object = {"task": runtime.submit(args.text, args.object)}
                elif args.operation == "run":
                    result_data = asyncio.run(runtime.run(args.max_steps))
                elif args.operation == "timeline":
                    result_data = {
                        "events": [e.model_dump(mode="json") for e in journal.timeline()],
                        "actions": [
                            dict(row)
                            for row in journal.db.execute(
                                "SELECT * FROM action_changes ORDER BY seq"
                            )
                        ],
                    }
                else:
                    if args.operation in {"pause", "resume", "stop"}:
                        runtime.executor.control(args.operation)
                    result_data = runtime.status()
                sys.stdout.write(json.dumps(result_data, sort_keys=True) + "\n")
                return 0
            finally:
                if runtime is not None:
                    runtime.close()
                journal.close()
        if args.command == "coordinator-demo":
            import asyncio

            from jane.cognition.demo import coordinator_demo

            sys.stdout.write(asyncio.run(coordinator_demo()))
            return 0
        if args.command == "affect":
            from datetime import UTC, datetime

            journal = Journal(args.database)
            try:
                affect_service = Affect(journal)
                now = datetime.now(UTC)
                if args.operation == "configure":
                    if args.file is None:
                        raise ValueError("configure requires --file")
                    affect_service.configure(
                        AffectConfig.model_validate_json(args.file.read_text()), now
                    )
                if args.operation == "reset":
                    affect_service.reset(now)
                if args.operation == "recover":
                    affect_service.recover(now)
                output_data = (
                    affect_service.history()
                    if args.operation == "history"
                    else {
                        "state": affect_service.state(now),
                        "config": affect_service.config().model_dump(mode="json"),
                    }
                )
                sys.stdout.write(json.dumps(output_data, sort_keys=True) + "\n")
                return 0
            finally:
                journal.close()
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
