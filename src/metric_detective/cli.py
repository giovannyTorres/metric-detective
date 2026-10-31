"""Command-line entry points shared by native execution and Docker."""

import argparse
import logging
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(prog="metric-detective")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    generate = commands.add_parser("generate")
    generate.add_argument("--output", type=Path, default=Path("data"))
    generate.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "init-db":
        from metric_detective.api import initialize_demo
        from metric_detective.settings import Settings
        from metric_detective.store import Store

        settings = Settings.from_env()
        store = Store(settings)
        store.initialize()
        initialize_demo(store, settings)
        print("Database and synthetic demonstration datasets initialized.")
    elif args.command == "worker":
        from metric_detective.worker import run_worker

        run_worker(once=args.once)
    elif args.command == "generate":
        from metric_detective.synthetic import generate_all

        generate_all(args.output, args.seed)
        print("Synthetic datasets generated.")


if __name__ == "__main__":
    main()
