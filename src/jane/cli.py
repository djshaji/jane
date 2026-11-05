"""Offline command-line interface."""

import argparse
import sys
from pathlib import Path
from typing import Sequence

from jane import __version__
from jane.runner import Scenario, demo_scenario, run


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jane", description="JANE offline foundation")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="replay the packaged deterministic scenario")
    replay = commands.add_parser("replay", help="validate and replay a JSON fixture")
    replay.add_argument("fixture", type=Path)
    args = parser.parse_args(argv)
    try:
        scenario = (
            demo_scenario()
            if args.command == "demo"
            else Scenario.model_validate_json(args.fixture.read_text(encoding="utf-8"))
        )
        output = run(scenario).render()
    except (OSError, ValueError) as error:
        print(f"jane: {error}", file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0
