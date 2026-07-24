#!/usr/bin/env python
"""Run the agent eval harness — the gate for a prompt or model change (spec §12.2).

    python scripts/run_evals.py                            # every suite, on config's models
    python scripts/run_evals.py --suite core               # the offline smoke run
    python scripts/run_evals.py --model claude-sonnet-4-5  # score a candidate model
    python scripts/run_evals.py --json evals-baseline.json # save a baseline
    python scripts/run_evals.py --baseline evals-baseline.json   # regression check

Exits non-zero when the run is below the bar or has regressed, so it can gate a
release without anyone having to read the table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from localpulse.config import Settings
from localpulse.evals.dataset import ALL_CASES, SUITES
from localpulse.evals.models import Dimension, EvalBar, compare_to_baseline
from localpulse.evals.runner import EvalRunner, model_map_for, select


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--model",
        help="model id to score on every agent (default: whatever config maps each agent to)",
    )
    parser.add_argument(
        "--suite", action="append", choices=SUITES, help="limit to a suite (repeatable)"
    )
    parser.add_argument(
        "--agent", action="append", help="limit to an agent: content, reputation, engagement"
    )
    parser.add_argument(
        "--bar",
        action="append",
        metavar="DIMENSION=SCORE",
        help="override a pass threshold, e.g. --bar coverage=0.9",
    )
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--baseline", type=Path, help="compare against a previous --json report")
    parser.add_argument("--quiet", action="store_true", help="print only the verdict")
    return parser.parse_args(argv)


def build_bar(overrides: list[str] | None) -> EvalBar:
    bar = EvalBar()
    for raw in overrides or []:
        name, _, value = raw.partition("=")
        try:
            dimension = Dimension(name.strip())
            bar = bar.with_override(dimension, float(value))
        except ValueError as exc:
            raise SystemExit(f"bad --bar {raw!r}: {exc}") from exc
    return bar


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Evals never touch a real database: an in-memory one per case, always.
    settings = Settings(database_url="sqlite:///:memory:")
    model_map = model_map_for(args.model, settings) if args.model else settings.model_map()

    cases = select(ALL_CASES, suites=args.suite or (), agents=args.agent or ())
    if not cases:
        raise SystemExit("no cases matched that filter")

    runner = EvalRunner(settings=settings, model_map=model_map, bar=build_bar(args.bar))
    report = runner.run(cases)

    if not args.quiet:
        print(report.render())

    if args.json:
        args.json.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n")
        print(f"\nwrote {args.json}")

    regressed = False
    if args.baseline:
        baseline = json.loads(args.baseline.read_text())
        regressions = compare_to_baseline(report, baseline)
        print(f"\nbaseline: {args.baseline} ({baseline.get('model_map')})")
        if regressions:
            regressed = True
            for regression in regressions:
                print(
                    f"  REGRESSION {regression.dimension.value}: "
                    f"{regression.before:.2f} -> {regression.after:.2f} "
                    f"({regression.delta:+.2f})"
                )
        else:
            print("  no dimension regressed")

    if not report.passed:
        return 1
    return 2 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
