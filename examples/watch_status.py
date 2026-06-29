"""Print live cooler telemetry.

Run with:

    python examples/watch_status.py

Stop with Ctrl-C.
"""

from __future__ import annotations

import asyncio

from redmagic_cooler_ble import RedMagicCoolerClient, bytes_to_hex


def print_status(status, frame: bytes) -> None:
    print(
        "frame={frame} temp={temp}C rpm={rpm} power={power}W".format(
            frame=bytes_to_hex(frame),
            temp=status.temperature_c if status.temperature_c is not None else "--",
            rpm=status.rpm if status.rpm is not None else "--",
            power=status.power_w if status.power_w is not None else "--",
        ),
        flush=True,
    )


async def main() -> None:
    async with RedMagicCoolerClient(device_match="RM Magcooler") as cooler:
        await cooler.start_status_notifications(print_status)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
