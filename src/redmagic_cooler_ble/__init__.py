"""Public API for RedMagic / Nubia phone cooler BLE control.

Most applications should use ``RedMagicCoolerClient`` for live hardware access
and the protocol helper functions for tests or offline frame decoding.
"""

from .client import Advertisement, RedMagicCoolerClient, find_cooler, list_advertisements
from .constants import FAN_LEVEL_BYTES
from .protocol import (
    CoolerStatus,
    auto_control_payload,
    bytes_to_hex,
    decode_status_frame,
    diablo_mode_payload,
    fan_level_to_payload,
    fan_payload_to_level,
    light_payload,
    thermal_switch_payload,
)

__all__ = [
    "CoolerStatus",
    "Advertisement",
    "FAN_LEVEL_BYTES",
    "RedMagicCoolerClient",
    "auto_control_payload",
    "bytes_to_hex",
    "decode_status_frame",
    "diablo_mode_payload",
    "fan_level_to_payload",
    "fan_payload_to_level",
    "find_cooler",
    "light_payload",
    "list_advertisements",
    "thermal_switch_payload",
]

__version__ = "0.1.0"
