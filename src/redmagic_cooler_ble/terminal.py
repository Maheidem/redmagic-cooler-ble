"""Curses-based local terminal UI."""

from __future__ import annotations

import asyncio
import curses
import time

from .client import RedMagicCoolerClient
from .constants import FAN_LEVEL_BYTES
from .protocol import CoolerStatus, bytes_to_hex


def fmt(value, suffix: str = "") -> str:
    return "--" if value is None else f"{value}{suffix}"


def draw(stdscr, status: CoolerStatus, level: int, message: str) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    slider_width = max(8, min(32, width - 18))
    filled = round(slider_width * level / len(FAN_LEVEL_BYTES))
    slider = "[" + "#" * filled + "-" * (slider_width - filled) + "]"
    updated = "--"
    if status.last_update:
        updated = f"{time.time() - status.last_update:0.1f}s ago"

    lines = [
        "RedMagic Heat Sink 8Pro BLE test",
        "",
        f"Temp:  {fmt(status.temperature_c, ' C')}",
        f"RPM:   {fmt(status.rpm)}",
        f"Power: {fmt(status.power_w, ' W')}",
        f"Fan limit: {fmt(status.fan_limit)}",
        f"Protection frame: {status.protection_raw or '--'}",
        f"Last frame: {status.last_frame or '--'}",
        f"Updated: {updated}",
        "",
        f"Fan target: level {level}/{len(FAN_LEVEL_BYTES)} value 0x{FAN_LEVEL_BYTES[level - 1]:02x}",
        slider,
        "",
        "Controls: left/right or 1-8 to set fan target, q to quit.",
        message,
    ]

    for row, line in enumerate(lines[:height]):
        stdscr.addnstr(row, 0, line, max(0, width - 1))
    stdscr.refresh()


async def run_terminal_ui(stdscr, client: RedMagicCoolerClient) -> None:
    level = await client.read_fan_level() or 1
    message = "Waiting for telemetry..."

    def on_status(status: CoolerStatus, payload: bytes) -> None:
        nonlocal message
        message = f"Telemetry frame {bytes_to_hex(payload)}"

    await client.start_status_notifications(on_status)
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    try:
        while True:
            key = stdscr.getch()
            next_level = level

            if key in (ord("q"), ord("Q")):
                break
            if key in (curses.KEY_RIGHT, ord("+"), ord("=")):
                next_level = min(len(FAN_LEVEL_BYTES), level + 1)
            elif key in (curses.KEY_LEFT, ord("-"), ord("_")):
                next_level = max(1, level - 1)
            elif ord("1") <= key <= ord(str(len(FAN_LEVEL_BYTES))):
                next_level = key - ord("0")

            if next_level != level:
                level = next_level
                try:
                    payload = await client.set_fan_level(level)
                    message = f"Wrote fan level {level} as {bytes_to_hex(payload)}"
                except Exception as exc:
                    message = f"Write failed: {exc}"

            draw(stdscr, client.status, level, message)
            await asyncio.sleep(0.1)
    finally:
        await client.stop_status_notifications()


def run_curses(device_match: str | None, scan_timeout: float) -> None:
    """Run the terminal UI synchronously for CLI usage."""

    async def run(stdscr) -> None:
        async with RedMagicCoolerClient(device_match=device_match, scan_timeout=scan_timeout) as client:
            await run_terminal_ui(stdscr, client)

    def wrapped(stdscr) -> None:
        asyncio.run(run(stdscr))

    curses.wrapper(wrapped)
