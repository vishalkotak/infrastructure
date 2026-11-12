"""Entry point for the `ms` command-line tool."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ms",
        description="Model Store: model registry, deployment control plane and GPU placement layer.",
    )
    parser.add_subparsers(dest="command", metavar="<command>")
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()


if __name__ == "__main__":
    main()
