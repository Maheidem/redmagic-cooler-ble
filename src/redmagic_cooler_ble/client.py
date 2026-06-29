"""Async BLE client built on top of Bleak."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable

from .constants import (
    ADVERTISED_SERVICE_UUID,
    AUTO_CONTROL_UUID,
    DEFAULT_SCAN_TIMEOUT_SECONDS,
    DIABLO_MODE_UUID,
    FAN_UUID,
    KNOWN_NAME_TOKENS,
    LIGHT_UUID,
    SERVICE_UUID,
    STATUS_UUID,
    THERMAL_SWITCH_UUID,
)
from .protocol import (
    CoolerStatus,
    Color,
    auto_control_payload,
    diablo_mode_payload,
    fan_level_to_payload,
    fan_payload_to_level,
    light_payload,
    thermal_switch_payload,
)

StatusCallback = Callable[[CoolerStatus, bytes], None | Awaitable[None]]


@dataclass(frozen=True)
class Advertisement:
    """BLE advertisement summary from a scan result."""

    name: str
    address: str
    rssi: int
    service_uuids: tuple[str, ...]


async def list_advertisements(timeout: float = DEFAULT_SCAN_TIMEOUT_SECONDS) -> list[Advertisement]:
    """Scan for visible BLE advertisements."""

    try:
        from bleak import BleakScanner
    except ImportError as exc:  # pragma: no cover - dependency setup path.
        raise RuntimeError("Missing dependency: install with `python -m pip install -e .`") from exc

    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    rows: list[Advertisement] = []
    for device, advertisement in discovered.values():
        rows.append(
            Advertisement(
                name=device.name or advertisement.local_name or "(unnamed)",
                address=device.address or "(no address)",
                rssi=advertisement.rssi or -999,
                service_uuids=tuple(advertisement.service_uuids or ()),
            )
        )
    return sorted(rows, key=lambda item: item.rssi, reverse=True)


async def find_cooler(device_match: str | None = None, timeout: float = DEFAULT_SCAN_TIMEOUT_SECONDS):
    """Find the most likely cooler peripheral from a scan."""

    try:
        from bleak import BleakScanner
    except ImportError as exc:  # pragma: no cover - dependency setup path.
        raise RuntimeError("Missing dependency: install with `python -m pip install -e .`") from exc

    normalized_match = device_match.lower() if device_match else None
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    candidates = []

    for device, advertisement in discovered.values():
        name = device.name or advertisement.local_name or ""
        address = device.address or ""
        lower_name = name.lower()
        services = [uuid.lower() for uuid in (advertisement.service_uuids or ())]

        matches_requested_device = bool(
            normalized_match and (normalized_match in lower_name or normalized_match in address.lower())
        )
        advertises_known_service = ADVERTISED_SERVICE_UUID in services or SERVICE_UUID in services
        looks_like_cooler = any(token in lower_name for token in KNOWN_NAME_TOKENS)

        if matches_requested_device or advertises_known_service or looks_like_cooler:
            candidates.append((device, advertisement.rssi or -999, advertises_known_service))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[2], item[1]), reverse=True)
    return candidates[0][0]


class RedMagicCoolerClient:
    """High-level async client for the decoded cooler service."""

    def __init__(
        self,
        *,
        device=None,
        device_match: str | None = None,
        scan_timeout: float = DEFAULT_SCAN_TIMEOUT_SECONDS,
    ) -> None:
        self.device = device
        self.device_match = device_match
        self.scan_timeout = scan_timeout
        self.status = CoolerStatus()
        self._client = None
        self._status_callback: StatusCallback | None = None

    @property
    def is_connected(self) -> bool:
        """Whether the underlying BLE client is connected."""

        return bool(self._client and self._client.is_connected)

    async def connect(self) -> "RedMagicCoolerClient":
        """Scan if needed and connect to the cooler."""

        try:
            from bleak import BleakClient
        except ImportError as exc:  # pragma: no cover - dependency setup path.
            raise RuntimeError("Missing dependency: install with `python -m pip install -e .`") from exc

        if self.device is None:
            self.device = await find_cooler(self.device_match, self.scan_timeout)
        if self.device is None:
            raise RuntimeError(
                "Cooler not found. Power it on, keep it nearby, and disconnect the Android app first."
            )

        self._client = BleakClient(self.device)
        await self._client.connect()
        return self

    async def disconnect(self) -> None:
        """Disconnect if connected."""

        if self._client and self._client.is_connected:
            await self.stop_status_notifications()
            await self._client.disconnect()

    async def __aenter__(self) -> "RedMagicCoolerClient":
        return await self.connect()

    async def __aexit__(self, *_exc_info) -> None:
        await self.disconnect()

    async def start_status_notifications(self, callback: StatusCallback | None = None) -> None:
        """Subscribe to telemetry notifications."""

        self._require_client()
        self._status_callback = callback
        await self._client.start_notify(STATUS_UUID, self._on_status)

    async def stop_status_notifications(self) -> None:
        """Stop telemetry notifications if the connection is active."""

        if not self._client or not self._client.is_connected:
            return
        try:
            await self._client.stop_notify(STATUS_UUID)
        except Exception:
            pass

    async def read_fan_level(self) -> int | None:
        """Read the current fan level if the characteristic is readable."""

        self._require_client()
        data = await self._client.read_gatt_char(FAN_UUID)
        return fan_payload_to_level(data)

    async def set_fan_level(self, level: int) -> bytes:
        """Set fan target level ``1`` through ``8``."""

        return await self._write(FAN_UUID, fan_level_to_payload(level))

    async def set_auto_control(self, enabled: bool) -> bytes:
        """Enable or disable automatic temperature control."""

        return await self._write(AUTO_CONTROL_UUID, auto_control_payload(enabled))

    async def set_thermal_switch(self, enabled: bool) -> bytes:
        """Enable or disable the app's thermal switch."""

        return await self._write(THERMAL_SWITCH_UUID, thermal_switch_payload(enabled))

    async def set_diablo_mode(self, enabled: bool) -> bytes:
        """Enable or disable the high-power "Diablo" mode."""

        return await self._write(DIABLO_MODE_UUID, diablo_mode_payload(enabled))

    async def set_light(self, mode: str, color: Color | None = None) -> bytes:
        """Set lighting mode."""

        return await self._write(LIGHT_UUID, light_payload(mode, color))

    async def _write(self, characteristic_uuid: str, payload: bytes) -> bytes:
        self._require_client()
        await self._client.write_gatt_char(characteristic_uuid, payload, response=True)
        return payload

    def _on_status(self, _sender, data: bytearray) -> None:
        payload = bytes(data)
        self.status.apply_frame(payload)
        if self._status_callback is None:
            return

        result = self._status_callback(self.status, payload)
        if inspect.isawaitable(result):
            asyncio.create_task(result)

    def _require_client(self) -> None:
        if not self._client or not self._client.is_connected:
            raise RuntimeError("not connected")
