"""Command-line interface for the RedMagic cooler BLE tools."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from .client import RedMagicCoolerClient, list_advertisements
from .constants import DEFAULT_DEVICE_MATCH, DEFAULT_SCAN_TIMEOUT_SECONDS, FAN_LEVEL_BYTES, LIGHT_MODES
from .protocol import bytes_to_hex
from .terminal import run_curses
from .web import serve_web_ui


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--device", default=DEFAULT_DEVICE_MATCH, help="BLE name/address substring to prefer.")
    common.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT_SECONDS, help="Scan timeout seconds.")

    parser = argparse.ArgumentParser(
        prog="redmagic-cooler",
        description="Control and inspect RedMagic / Nubia phone coolers over BLE.",
    )
    parser.add_argument("--device", default=DEFAULT_DEVICE_MATCH, help=argparse.SUPPRESS)
    parser.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT_SECONDS, help=argparse.SUPPRESS)
    parser.add_argument("--watch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--level", type=int, choices=range(1, len(FAN_LEVEL_BYTES) + 1), help=argparse.SUPPRESS)
    parser.add_argument("--list-devices", action="store_true", help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("scan", parents=[common], help="List visible BLE advertisements.")
    subparsers.add_parser("watch", parents=[common], help="Print live telemetry until interrupted.")

    terminal = subparsers.add_parser("terminal", parents=[common], help="Run the terminal slider UI.")
    terminal.set_defaults(command="terminal")

    set_fan = subparsers.add_parser("set-fan", parents=[common], help="Set fan level 1-8.")
    set_fan.add_argument("level", type=int, choices=range(1, len(FAN_LEVEL_BYTES) + 1))

    for name in ("auto", "thermal", "diablo"):
        command = subparsers.add_parser(f"set-{name}", parents=[common], help=f"Set {name} switch on or off.")
        command.add_argument("state", choices=("on", "off"))

    set_light = subparsers.add_parser("set-light", parents=[common], help="Set lighting mode.")
    set_light.add_argument("mode", choices=LIGHT_MODES)
    set_light.add_argument("--color", default="#00ffa1", help="RGB color for fixed mode, e.g. #ff0000.")

    web = subparsers.add_parser("web", help="Serve the packaged Web Bluetooth UI.")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8766)
    web.add_argument("--open", action="store_true", help="Open the UI in the default browser.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_devices:
        args.command = "scan"
    elif args.level is not None:
        args.command = "set-fan"
    elif args.watch:
        args.command = "watch"
    elif args.command is None:
        args.command = "terminal"

    try:
        if args.command == "web":
            serve_web_ui(args.host, args.port, args.open)
            return 0
        if args.command == "terminal":
            run_curses(args.device, args.scan_timeout)
            return 0
        return asyncio.run(run_async_command(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


async def run_async_command(args: argparse.Namespace) -> int:
    if args.command == "scan":
        rows = await list_advertisements(args.scan_timeout)
        if not rows:
            print("No BLE advertisements visible.")
            return 0
        for row in rows:
            print(f"{row.rssi:>4} dBm  {row.address}  {row.name}")
            if row.service_uuids:
                print(f"          services: {','.join(row.service_uuids)}")
        return 0

    async with RedMagicCoolerClient(device_match=args.device, scan_timeout=args.scan_timeout) as client:
        device_name = getattr(client.device, "name", None) or "(unnamed)"
        device_address = getattr(client.device, "address", None) or "(no address)"
        print(f"Connected to {device_name} [{device_address}]")

        if args.command == "watch":
            await watch(client)
            return 0
        if args.command == "set-fan":
            await watch_after_write(client, f"fan level {args.level}", lambda: client.set_fan_level(args.level))
            return 0
        if args.command == "set-auto":
            enabled = args.state == "on"
            await watch_after_write(client, f"auto {args.state}", lambda: client.set_auto_control(enabled))
            return 0
        if args.command == "set-thermal":
            enabled = args.state == "on"
            await watch_after_write(client, f"thermal {args.state}", lambda: client.set_thermal_switch(enabled))
            return 0
        if args.command == "set-diablo":
            enabled = args.state == "on"
            await watch_after_write(client, f"diablo {args.state}", lambda: client.set_diablo_mode(enabled))
            return 0
        if args.command == "set-light":
            color = args.color if args.mode == "fixed" else None
            await watch_after_write(client, f"light {args.mode}", lambda: client.set_light(args.mode, color))
            return 0

    raise RuntimeError(f"unknown command: {args.command}")


async def watch(client: RedMagicCoolerClient) -> None:
    stop = asyncio.Event()

    def on_status(status, payload: bytes) -> None:
        print(
            f"temp={_fmt(status.temperature_c, 'C')} rpm={_fmt(status.rpm)} "
            f"power={_fmt(status.power_w, 'W')} frame={bytes_to_hex(payload)}",
            flush=True,
        )

    await client.start_status_notifications(on_status)
    try:
        await stop.wait()
    finally:
        await client.stop_status_notifications()


async def watch_after_write(client: RedMagicCoolerClient, label: str, write_factory) -> None:
    def on_status(status, payload: bytes) -> None:
        print(
            f"temp={_fmt(status.temperature_c, 'C')} rpm={_fmt(status.rpm)} "
            f"power={_fmt(status.power_w, 'W')} frame={bytes_to_hex(payload)}",
            flush=True,
        )

    await client.start_status_notifications(on_status)
    try:
        payload = await write_factory()
        print(f"Wrote {label}: {bytes_to_hex(payload)}")
        await asyncio.sleep(5)
    finally:
        await client.stop_status_notifications()


def _fmt(value, suffix: str = "") -> str:
    return "--" if value is None else f"{value}{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
