"""Command-line interface for the RedMagic cooler BLE tools."""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
import time
from collections.abc import Sequence

from .client import RedMagicCoolerClient, list_advertisements
from .constants import DEFAULT_DEVICE_MATCH, DEFAULT_SCAN_TIMEOUT_SECONDS, FAN_LEVEL_BYTES, LIGHT_MODES
from .control_server import run_control_dashboard
from .mac_control import FanControlConfig, FanDecision, MacThermalSample, decide_fan_level, read_mac_thermal_sample
from .protocol import CoolerStatus, bytes_to_hex
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

    mac_auto = subparsers.add_parser(
        "mac-auto",
        parents=[common],
        help="Run a conservative Mac thermal control loop.",
    )
    mac_auto.add_argument("--interval", type=float, default=5.0, help="Seconds between control samples.")
    mac_auto.add_argument("--duration", type=float, help="Stop after this many seconds.")
    mac_auto.add_argument("--dry-run", action="store_true", help="Print decisions without writing fan commands.")
    mac_auto.add_argument("--idle-level", type=int, default=1, choices=range(1, len(FAN_LEVEL_BYTES) + 1))
    mac_auto.add_argument("--max-level", type=int, default=4, choices=range(1, len(FAN_LEVEL_BYTES) + 1))
    mac_auto.add_argument("--start-temp", type=float, default=65.0, help="Mac temperature that starts level increases.")
    mac_auto.add_argument("--temp-step", type=float, default=8.0, help="Celsius per additional fan level.")
    mac_auto.add_argument("--fan-rpm-start", type=int, default=3200, help="Mac fan RPM that starts level increases.")
    mac_auto.add_argument("--fan-rpm-step", type=int, default=900, help="Mac fan RPM per additional level.")
    mac_auto.add_argument("--cooler-floor", type=float, default=1.0, help="Force idle at or below this cooler temp.")
    mac_auto.add_argument("--cooler-soft-cap", type=float, default=8.0, help="Limit fan while cooler is this cold.")
    mac_auto.add_argument("--cooler-soft-cap-level", type=int, default=2, choices=range(1, len(FAN_LEVEL_BYTES) + 1))
    mac_auto.add_argument("--max-without-cooler-temp", type=int, default=2, choices=range(1, len(FAN_LEVEL_BYTES) + 1))
    mac_auto.add_argument("--min-change-interval", type=float, default=15.0, help="Minimum seconds between writes.")
    mac_auto.add_argument(
        "--sensor-command",
        help="External command returning JSON with CPU/GPU temperature, fan RPM, or thermal_state.",
    )
    mac_auto.add_argument("--no-startup-safe", dest="startup_safe", action="store_false", default=True)
    mac_auto.add_argument("--no-exit-safe", dest="exit_safe", action="store_false", default=True)

    control_ui = subparsers.add_parser(
        "control-ui",
        parents=[common],
        help="Run a local dashboard with Mac sensors and curve controls.",
    )
    control_ui.add_argument("--host", default="127.0.0.1")
    control_ui.add_argument("--port", type=int, default=8767)
    control_ui.add_argument("--open", action="store_true", help="Open the dashboard in the default browser.")
    control_ui.add_argument("--interval", type=float, default=1.0, help="Seconds between control samples.")
    control_ui.add_argument("--dry-run", action="store_true", help="Show decisions without writing fan commands.")
    control_ui.add_argument("--idle-level", type=int, default=1, choices=range(1, len(FAN_LEVEL_BYTES) + 1))
    control_ui.add_argument("--max-level", type=int, default=4, choices=range(1, len(FAN_LEVEL_BYTES) + 1))
    control_ui.add_argument("--start-temp", type=float, default=65.0)
    control_ui.add_argument("--temp-step", type=float, default=8.0)
    control_ui.add_argument("--fan-rpm-start", type=int, default=3200)
    control_ui.add_argument("--fan-rpm-step", type=int, default=900)
    control_ui.add_argument("--cooler-floor", type=float, default=1.0)
    control_ui.add_argument("--cooler-soft-cap", type=float, default=8.0)
    control_ui.add_argument("--cooler-soft-cap-level", type=int, default=2, choices=range(1, len(FAN_LEVEL_BYTES) + 1))
    control_ui.add_argument("--max-without-cooler-temp", type=int, default=2, choices=range(1, len(FAN_LEVEL_BYTES) + 1))
    control_ui.add_argument("--min-change-interval", type=float, default=5.0, help="Minimum seconds between writes.")
    control_ui.add_argument("--reconnect-interval", type=float, default=10.0, help="Seconds between BLE reconnect attempts.")
    control_ui.add_argument("--no-startup-safe", dest="startup_safe", action="store_false", default=True)
    control_ui.add_argument("--no-exit-safe", dest="exit_safe", action="store_false", default=True)

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
        print(f"Connected to {device_name} [{device_address}]", flush=True)

        if args.command == "watch":
            await watch(client)
            return 0
        if args.command == "mac-auto":
            await mac_auto(client, args)
            return 0
        if args.command == "control-ui":
            await run_control_dashboard(client, args)
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


async def mac_auto(client: RedMagicCoolerClient, args: argparse.Namespace) -> None:
    config = FanControlConfig(
        idle_level=args.idle_level,
        max_level=args.max_level,
        start_temp_c=args.start_temp,
        temp_step_c=args.temp_step,
        fan_rpm_start=args.fan_rpm_start,
        fan_rpm_step=args.fan_rpm_step,
        cooler_floor_c=args.cooler_floor,
        cooler_soft_cap_c=args.cooler_soft_cap,
        cooler_soft_cap_level=args.cooler_soft_cap_level,
        max_without_cooler_temp=args.max_without_cooler_temp,
    )
    sensor_command = shlex.split(args.sensor_command) if args.sensor_command else None
    current_level = config.idle_level
    last_write_at = 0.0
    started_at = time.monotonic()

    def on_status(_status: CoolerStatus, _payload: bytes) -> None:
        return None

    await client.start_status_notifications(on_status)
    try:
        if args.startup_safe:
            if args.dry_run:
                print(f"Dry run: would disable Diablo mode and set fan level {config.idle_level}")
                last_write_at = time.monotonic()
            else:
                await client.set_diablo_mode(False)
                await client.set_fan_level(config.idle_level)
                last_write_at = time.monotonic()
                print(f"Startup safe state: Diablo off, fan level {config.idle_level}")
        else:
            current_level = await client.read_fan_level() or current_level

        while True:
            if args.duration is not None and time.monotonic() - started_at >= args.duration:
                break

            sample = await asyncio.to_thread(read_mac_thermal_sample, sensor_command)
            decision = decide_fan_level(config, sample, client.status)
            now = time.monotonic()
            wrote = False

            if decision.level != current_level and now - last_write_at >= args.min_change_interval:
                if args.dry_run:
                    wrote = True
                    current_level = decision.level
                    last_write_at = now
                else:
                    await client.set_fan_level(decision.level)
                    wrote = True
                    current_level = decision.level
                    last_write_at = now

            print(_mac_auto_line(sample, client.status, decision, current_level, wrote, args.dry_run), flush=True)
            await asyncio.sleep(max(args.interval, 0.5))
    finally:
        if args.exit_safe and not args.dry_run and client.is_connected:
            try:
                await client.set_fan_level(config.idle_level)
                await client.set_diablo_mode(False)
                print(f"Exit safe state: fan level {config.idle_level}, Diablo off")
            finally:
                await client.stop_status_notifications()
        else:
            await client.stop_status_notifications()


def _fmt(value, suffix: str = "") -> str:
    return "--" if value is None else f"{value}{suffix}"


def _fmt_float(value: float | None, suffix: str = "") -> str:
    return "--" if value is None else f"{value:.1f}{suffix}"


def _mac_auto_line(
    sample: MacThermalSample,
    status: CoolerStatus,
    decision: FanDecision,
    current_level: int,
    wrote: bool,
    dry_run: bool,
) -> str:
    action = "would-set" if wrote and dry_run else "set" if wrote else "hold"
    sensor_error = f" sensor_error={sample.error!r}" if sample.error else ""
    return (
        f"[{time.strftime('%H:%M:%S')}] {action} level={current_level} target={decision.level} "
        f"mac_temp={_fmt_float(sample.temperature_c, 'C')} mac_fan={_fmt(sample.fan_rpm, 'rpm')} "
        f"thermal={sample.thermal_state or '--'} source={sample.source}{sensor_error} "
        f"cooler_temp={_fmt(status.temperature_c, 'C')} cooler_rpm={_fmt(status.rpm, 'rpm')} "
        f"cooler_power={_fmt(status.power_w, 'W')} reason={decision.reason}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
