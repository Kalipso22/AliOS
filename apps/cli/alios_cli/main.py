"""CLI composition root."""

import argparse

from alios_core.config import AliOSConfig


def build_parser() -> argparse.ArgumentParser:
    """Build the initial public CLI command surface."""
    parser = argparse.ArgumentParser(prog="alios", description="AliOS command-line interface")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="show local runtime configuration")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute a CLI command and return a process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        config = AliOSConfig.from_environment()
        print(f"AliOS environment: {config.environment}")
        print(f"AliOS log level: {config.log_level}")
        print("AliOS runtime: available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
