"""Command-line entry points shared by native execution and Docker."""

import argparse
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(prog="metric-detective")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    generate = commands.add_parser("generate")
    generate.add_argument("--output", type=Path, default=Path("data"))
    generate.add_argument("--seed", type=int, default=17)
    local = commands.add_parser(
        "local", help="Start, stop or inspect the independent local supervisor"
    )
    local.add_argument("action", choices=["start", "stop", "status", "restart"])
    local.add_argument("--open", action="store_true", dest="open_browser")
    local.add_argument("--runtime-dir", type=Path)
    local.add_argument("--api-port", type=int)
    local.add_argument("--ui-port", type=int)
    local.add_argument("--postgres-bin", type=Path)
    local.add_argument("--postgres-data", type=Path)
    local.add_argument("--postgres-port", type=int)
    local.add_argument("--start-ollama", action="store_true")
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
    elif args.command == "local":
        from metric_detective.local_runtime import (
            LocalOptions,
            RuntimeFailure,
            local_status,
            start_local,
            stop_local,
        )

        load_dotenv(Path.cwd() / ".env", override=False)
        folder = Path(args.runtime_dir or os.getenv("MD_RUNTIME_DIR", "runtime/local")).resolve()
        try:
            if args.action in {"stop", "restart"}:
                state = stop_local(folder)
            if args.action in {"start", "restart"}:
                options = LocalOptions(
                    root=str(Path.cwd()),
                    runtime_dir=str(folder),
                    api_port=args.api_port or int(os.getenv("MD_LOCAL_API_PORT", "18000")),
                    ui_port=args.ui_port or int(os.getenv("MD_LOCAL_UI_PORT", "18501")),
                    postgres_bin=str(args.postgres_bin)
                    if args.postgres_bin
                    else os.getenv("MD_POSTGRES_BIN"),
                    postgres_data=str(args.postgres_data)
                    if args.postgres_data
                    else os.getenv("MD_POSTGRES_DATA"),
                    postgres_port=args.postgres_port or int(os.getenv("MD_POSTGRES_PORT", "55432")),
                    start_ollama=args.start_ollama
                    or os.getenv("MD_START_OLLAMA", "false").lower() == "true",
                )
                state = start_local(options, open_browser=args.open_browser)
            elif args.action == "status":
                state = local_status(folder)
            print(
                json.dumps(
                    {key: value for key, value in state.items() if key != "token"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        except RuntimeFailure as error:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "code": str(error),
                        "help": "Check .env, PostgreSQL and runtime/local/*.log",
                    }
                )
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
