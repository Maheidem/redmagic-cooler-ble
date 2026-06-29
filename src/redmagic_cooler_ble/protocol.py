"""Protocol encoding and telemetry decoding for the cooler GATT service."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Iterable

from .constants import FAN_LEVEL_BYTES, LIGHT_MODES

Color = tuple[int, int, int] | str


@dataclass
class CoolerStatus:
    """Latest decoded telemetry state.

    The status characteristic sends small independent frames. A single frame
    only updates one field, so callers should keep one ``CoolerStatus`` instance
    and apply every notification to it.
    """

    temperature_c: int | None = None
    rpm: int | None = None
    power_w: int | None = None
    fan_limit: int | None = None
    protection_raw: str | None = None
    last_frame: str | None = None
    last_update: float | None = None
    recent_frames: list[str] = field(default_factory=list)

    def apply_frame(self, data: bytes | bytearray | memoryview) -> "CoolerStatus":
        """Decode one notification frame into this status object."""

        payload = bytes(data)
        if not payload:
            return self

        self.last_frame = bytes_to_hex(payload)
        self.last_update = time.time()
        self.recent_frames.append(self.last_frame)
        self.recent_frames = self.recent_frames[-10:]

        frame_type = payload[0]
        if frame_type == 0x04 and len(payload) >= 2:
            self.temperature_c = int(payload[1]) - 6
        elif frame_type == 0x07 and len(payload) >= 2:
            self.fan_limit = int(payload[1])
        elif frame_type == 0x08 and len(payload) >= 3:
            self.rpm = (int(payload[1]) << 8) | int(payload[2])
        elif frame_type == 0x09 and len(payload) >= 2:
            self.power_w = int(payload[1])
        elif frame_type == 0x05:
            self.protection_raw = self.last_frame

        return self


def decode_status_frame(data: bytes | bytearray | memoryview, status: CoolerStatus | None = None) -> CoolerStatus:
    """Decode one status notification frame.

    Pass an existing ``CoolerStatus`` to accumulate values across frames. If no
    status is supplied, a new one is created.
    """

    target = status if status is not None else CoolerStatus()
    return target.apply_frame(data)


def bytes_to_hex(data: Iterable[int] | bytes | bytearray | memoryview) -> str:
    """Return compact lowercase hexadecimal for a byte sequence."""

    return bytes(data).hex()


def fan_level_to_payload(level: int) -> bytes:
    """Encode fan level ``1`` through ``8`` for the fan target characteristic."""

    if level < 1 or level > len(FAN_LEVEL_BYTES):
        raise ValueError(f"fan level must be 1-{len(FAN_LEVEL_BYTES)}")
    return bytes([FAN_LEVEL_BYTES[level - 1]])


def fan_payload_to_level(payload: bytes | bytearray | memoryview) -> int | None:
    """Decode a fan target byte back to a 1-based fan level if recognized."""

    data = bytes(payload)
    if not data:
        return None
    try:
        return FAN_LEVEL_BYTES.index(data[0]) + 1
    except ValueError:
        return None


def auto_control_payload(enabled: bool) -> bytes:
    """Encode the auto temperature control switch."""

    return bytes([0x01 if enabled else 0x00])


def diablo_mode_payload(enabled: bool) -> bytes:
    """Encode the high-power "Diablo" mode switch."""

    return bytes([0x01 if enabled else 0x00])


def thermal_switch_payload(enabled: bool) -> bytes:
    """Encode the app's thermal switch.

    The Android log names the internal operation as ``HALL_W``. The visible app
    label is "Interruptor termico"; observed payloads are ``02`` for on and
    ``03`` for off.
    """

    return bytes([0x02 if enabled else 0x03])


def light_payload(mode: str, color: Color | None = None) -> bytes:
    """Encode a lighting command.

    ``mode`` must be one of ``off``, ``colorful``, ``breath``, or ``fixed``.
    ``fixed`` requires an RGB color as ``"#RRGGBB"`` or ``(r, g, b)``.
    """

    normalized = mode.strip().lower()
    if normalized not in LIGHT_MODES:
        raise ValueError(f"light mode must be one of: {', '.join(LIGHT_MODES)}")

    if normalized == "off":
        return bytes([0x06, 0x00, 0x00, 0x00])
    if normalized == "colorful":
        return bytes([0x01, 0x00, 0x00, 0x00])
    if normalized == "breath":
        return bytes([0x02, 0x00, 0x00, 0x00])

    red, green, blue = parse_rgb(color)
    return bytes([0x04, red, green, blue])


def parse_rgb(color: Color | None) -> tuple[int, int, int]:
    """Parse and validate RGB color input."""

    if color is None:
        raise ValueError("fixed light mode requires a color")

    if isinstance(color, str):
        match = re.fullmatch(r"#?([0-9a-fA-F]{6})", color.strip())
        if not match:
            raise ValueError("color must be #RRGGBB")
        value = match.group(1)
        return (
            int(value[0:2], 16),
            int(value[2:4], 16),
            int(value[4:6], 16),
        )

    if len(color) != 3:
        raise ValueError("RGB color tuple must contain exactly 3 values")

    red, green, blue = color
    for channel in (red, green, blue):
        if channel < 0 or channel > 255:
            raise ValueError("RGB channels must be 0-255")
    return int(red), int(green), int(blue)
