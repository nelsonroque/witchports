from __future__ import annotations

import argparse
import sys

from .listeners import (
    Listener,
    ListenerError,
    PortRange,
    display,
    find_listeners,
    stop_listener,
)


def _port_range(value: str) -> PortRange:
    try:
        return PortRange.parse(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _port(value: str) -> int:
    ports = _port_range(value)
    if "-" in value:
        raise argparse.ArgumentTypeError("stop requires one port")
    return ports.first


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="witchports", description="Find and stop local TCP listeners."
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("tui", help="open the interactive dashboard (default)")
    listing = commands.add_parser("list", help="list visible TCP listeners")
    listing.add_argument(
        "--ports", type=_port_range, default=PortRange(), metavar="PORT[-PORT]"
    )
    scan = commands.add_parser("scan", help="find listeners in a local port or range")
    scan.add_argument("ports", type=_port_range, metavar="PORT[-PORT]")
    for command in (listing, scan):
        command.add_argument(
            "--web",
            action="store_true",
            help="show recognized web frameworks and servers",
        )
    stop = commands.add_parser(
        "stop", aliases=["kill"], help="stop a process listening on a port"
    )
    stop.add_argument("port", type=_port)
    stop.add_argument(
        "--pid", type=int, help="choose the owner when several processes share a port"
    )
    stop.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )
    stop.add_argument(
        "--force", action="store_true", help="send SIGKILL instead of SIGTERM"
    )
    return parser.parse_args(argv)


def _print_listeners(listeners: list[Listener]) -> None:
    if not listeners:
        print("No matching TCP listeners found.")
        return

    rows = [("PORT", "PID", "APP / PROCESS", "BIND ADDRESS", "COMMAND")]
    rows.extend(
        (
            str(listener.port),
            str(listener.pid),
            display(listener.label),
            display(", ".join(listener.addresses)),
            display(listener.command),
        )
        for listener in listeners
    )
    widths = [max(map(len, column)) for column in list(zip(*rows))[:-1]]
    for *cells, command in rows:
        print(
            "  ".join(
                [cell.ljust(width) for cell, width in zip(cells, widths)] + [command]
            )
        )


def _stop(args: argparse.Namespace) -> int:
    listeners = find_listeners(PortRange(args.port, args.port))
    if args.pid is not None:
        listeners = [listener for listener in listeners if listener.pid == args.pid]
    if not listeners:
        owner = f" for PID {args.pid}" if args.pid is not None else ""
        raise ListenerError(f"No TCP listener found on port {args.port}{owner}.")
    if len(listeners) > 1:
        choices = ", ".join(f"{item.pid} ({display(item.label)})" for item in listeners)
        raise ListenerError(
            f"Port {args.port} has multiple owners. Choose --pid from {choices}."
        )

    listener = listeners[0]
    sig = "SIGKILL" if args.force else "SIGTERM"
    if not args.yes:
        if not sys.stdin.isatty():
            raise ListenerError(
                "Use --yes to confirm a stop in a non-interactive terminal."
            )
        print(display(listener.command))
        answer = input(
            f"Send {sig} to {display(listener.label)} (PID {listener.pid}, port {listener.port})? [y/N] "
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            print("Cancelled.")
            return 0

    if not stop_listener(listener, force=args.force):
        raise ListenerError(
            f"Sent {sig}. PID {listener.pid} is still listening on port {listener.port}."
        )
    print(f"PID {listener.pid} stopped listening on port {listener.port} after {sig}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command in {"list", "scan"}:
            listeners = find_listeners(args.ports)
            _print_listeners(
                [item for item in listeners if item.framework]
                if args.web
                else listeners
            )
        elif args.command in {"stop", "kill"}:
            return _stop(args)
        else:
            from .tui import WitchportsApp

            WitchportsApp().run()
    except ListenerError as error:
        print(f"witchports: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130
    return 0
