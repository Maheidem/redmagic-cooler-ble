"""Conservative Mac thermal control policy for the cooler."""

from __future__ import annotations

import json
import math
import platform
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .mac_smc import SmcSnapshot, read_smc_snapshot
from .protocol import CoolerStatus

THERMAL_STATE_NAMES = {
    0: "nominal",
    1: "fair",
    2: "serious",
    3: "critical",
}


@dataclass(frozen=True)
class MacPowerSample:
    """Mac-side power telemetry from IOKit."""

    system_power_w: float | None = None
    system_voltage_v: float | None = None
    system_current_a: float | None = None
    adapter_watts: int | None = None
    battery_power_w: float | None = None
    source: str = "unavailable"
    error: str | None = None


@dataclass(frozen=True)
class MacThermalSample:
    """Latest Mac-side thermal reading.

    On macOS, the default sampler reads actual CPU/GPU temperature and Mac fan
    RPM through AppleSMC. The battery/thermal-state path is only a fallback.
    """

    temperature_c: float | None = None
    cpu_temperature_c: float | None = None
    gpu_temperature_c: float | None = None
    cpu_sensor_key: str | None = None
    gpu_sensor_key: str | None = None
    fan_rpm: int | None = None
    fan_rpms: tuple[int, ...] = ()
    temperature_sensors: dict[str, float] = field(default_factory=dict)
    thermal_state: str | None = None
    power: MacPowerSample = field(default_factory=MacPowerSample)
    source: str = "unavailable"
    error: str | None = None


@dataclass(frozen=True)
class FanControlConfig:
    """Policy knobs for mapping Mac/cooler telemetry to a fan level."""

    idle_level: int = 1
    max_level: int = 4
    start_temp_c: float = 65.0
    temp_step_c: float = 8.0
    fan_rpm_start: int = 3200
    fan_rpm_step: int = 900
    cooler_floor_c: float = 1.0
    cooler_soft_cap_c: float = 8.0
    cooler_soft_cap_level: int = 2
    max_without_cooler_temp: int = 2


@dataclass(frozen=True)
class FanDecision:
    """Chosen cooler fan level and the reason for it."""

    level: int
    reason: str


def read_mac_thermal_sample(
    sensor_command: Sequence[str] | None = None,
    *,
    timeout: float = 3.0,
) -> MacThermalSample:
    """Read a Mac thermal sample.

    ``sensor_command`` may point at a local command that prints JSON with any
    of these keys: ``cpu_temperature_c``, ``gpu_temperature_c``,
    ``temperature_c``, ``fan_rpm``, ``fan_rpms``, ``thermal_state``.
    """

    if sensor_command:
        return _read_external_sensor_command(sensor_command, timeout)

    if platform.system() != "Darwin":
        return MacThermalSample(error="built-in sampling is only available on macOS")

    power = _read_mac_power_sample(timeout)
    smc_snapshot = read_smc_snapshot()
    if not smc_snapshot.error and (
        smc_snapshot.cpu_temperature_c is not None
        or smc_snapshot.gpu_temperature_c is not None
        or smc_snapshot.fan_rpms
    ):
        return _sample_from_smc_snapshot(smc_snapshot, power)

    temperature_c = _read_battery_temperature_c(timeout)
    thermal_state = _read_process_thermal_state(timeout) or _read_pmset_thermal_state(timeout)

    sources: list[str] = []
    if temperature_c is not None:
        sources.append("apple-smart-battery")
    if thermal_state is not None:
        sources.append("process-thermal-state")

    if not sources:
        return MacThermalSample(error=smc_snapshot.error or "no built-in Mac thermal signal available")

    return MacThermalSample(
        temperature_c=temperature_c,
        thermal_state=thermal_state,
        power=power,
        source="+".join(sources),
    )


def decide_fan_level(
    config: FanControlConfig,
    mac_sample: MacThermalSample,
    cooler_status: CoolerStatus,
) -> FanDecision:
    """Choose the next cooler fan level from Mac and cooler telemetry."""

    idle_level = _clamp_level(config.idle_level, 1, 8)
    max_level = _clamp_level(config.max_level, idle_level, 8)

    if cooler_status.temperature_c is not None and cooler_status.temperature_c <= config.cooler_floor_c:
        return FanDecision(
            idle_level,
            f"cooler temp {cooler_status.temperature_c}C <= floor {config.cooler_floor_c:g}C",
        )

    requested_level, reason = _mac_requested_level(config, mac_sample, idle_level, max_level)
    level = requested_level

    if cooler_status.temperature_c is None:
        cap = _clamp_level(config.max_without_cooler_temp, idle_level, max_level)
        if level > cap:
            level = cap
            reason = f"{reason}; capped at {cap} until cooler temp telemetry arrives"
    elif cooler_status.temperature_c <= config.cooler_soft_cap_c:
        cap = _clamp_level(config.cooler_soft_cap_level, idle_level, max_level)
        if level > cap:
            level = cap
            reason = (
                f"{reason}; capped at {cap} while cooler temp "
                f"{cooler_status.temperature_c}C <= {config.cooler_soft_cap_c:g}C"
            )

    return FanDecision(_clamp_level(level, idle_level, max_level), reason)


def _mac_requested_level(
    config: FanControlConfig,
    sample: MacThermalSample,
    idle_level: int,
    max_level: int,
) -> tuple[int, str]:
    candidates: list[tuple[int, str]] = []

    control_temperature, control_label = _control_temperature(sample)
    if control_temperature is not None and math.isfinite(control_temperature):
        temp_level = _level_from_threshold(
            control_temperature,
            config.start_temp_c,
            config.temp_step_c,
            idle_level,
            max_level,
        )
        candidates.append((temp_level, f"{control_label} {control_temperature:.1f}C"))

    if sample.fan_rpm is not None:
        fan_level = _level_from_threshold(
            sample.fan_rpm,
            config.fan_rpm_start,
            config.fan_rpm_step,
            idle_level,
            max_level,
        )
        candidates.append((fan_level, f"Mac fan {sample.fan_rpm} RPM"))

    thermal_state = _normalize_thermal_state(sample.thermal_state)
    if thermal_state == "fair":
        candidates.append((min(max_level, idle_level + 1), "Mac thermal state fair"))
    elif thermal_state in {"serious", "critical"}:
        candidates.append((max_level, f"Mac thermal state {thermal_state}"))
    elif thermal_state == "nominal" and not candidates:
        candidates.append((idle_level, "Mac thermal state nominal"))

    if not candidates:
        if sample.error:
            return idle_level, f"Mac sensor unavailable: {sample.error}"
        return idle_level, "no Mac thermal signal"

    return max(candidates, key=lambda item: item[0])


def _level_from_threshold(
    value: float,
    start: float,
    step: float,
    idle_level: int,
    max_level: int,
) -> int:
    if value < start:
        return idle_level

    safe_step = step if step > 0 else 1.0
    level = idle_level + 1 + int((value - start) // safe_step)
    return _clamp_level(level, idle_level, max_level)


def _clamp_level(level: int, minimum: int, maximum: int) -> int:
    return min(max(int(level), int(minimum)), int(maximum))


def _read_external_sensor_command(command: Sequence[str], timeout: float) -> MacThermalSample:
    completed = _run(command, timeout)
    if completed is None:
        return MacThermalSample(source="external", error="sensor command timed out or could not start")
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip().splitlines()
        return MacThermalSample(source="external", error=message[-1] if message else "sensor command failed")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return MacThermalSample(source="external", error=f"sensor command did not return JSON: {exc.msg}")

    if not isinstance(payload, dict):
        return MacThermalSample(source="external", error="sensor command JSON must be an object")

    fan_rpms = _first_int_tuple(payload, ("fan_rpms", "fans_rpm", "mac_fan_rpms"))
    fan_rpm = _first_int(payload, ("fan_rpm", "rpm", "fan_speed_rpm", "mac_fan_rpm"))
    if fan_rpm is None and fan_rpms:
        fan_rpm = max(fan_rpms)

    cpu_temperature_c = _first_float(payload, ("cpu_temperature_c", "cpu_temp_c", "cpu_temperature"))
    gpu_temperature_c = _first_float(payload, ("gpu_temperature_c", "gpu_temp_c", "gpu_temperature"))
    temperature_c = _first_float(payload, ("temperature_c", "temp_c", "temperature"))
    if temperature_c is None:
        values = [value for value in (cpu_temperature_c, gpu_temperature_c) if value is not None]
        temperature_c = max(values) if values else None

    return MacThermalSample(
        temperature_c=temperature_c,
        cpu_temperature_c=cpu_temperature_c,
        gpu_temperature_c=gpu_temperature_c,
        cpu_sensor_key=_first_string(payload, ("cpu_sensor_key", "cpu_key")),
        gpu_sensor_key=_first_string(payload, ("gpu_sensor_key", "gpu_key")),
        fan_rpm=fan_rpm,
        fan_rpms=fan_rpms,
        thermal_state=_normalize_thermal_state(_first_value(payload, ("thermal_state", "thermal"))),
        power=MacPowerSample(
            system_power_w=_first_float(payload, ("system_power_w", "mac_power_w", "system_input_power_w")),
            system_voltage_v=_first_float(payload, ("system_voltage_v", "mac_voltage_v")),
            system_current_a=_first_float(payload, ("system_current_a", "mac_current_a")),
            adapter_watts=_first_int(payload, ("adapter_watts", "adapter_power_w")),
            battery_power_w=_first_float(payload, ("battery_power_w",)),
            source="external",
        ),
        source="external",
    )


def _read_battery_temperature_c(timeout: float) -> float | None:
    completed = _run(("ioreg", "-r", "-c", "AppleSmartBattery", "-d", "1"), timeout)
    if completed is None or completed.returncode != 0:
        return None

    match = re.search(r'"Temperature"\s*=\s*(-?\d+)', completed.stdout)
    if not match:
        return None
    return int(match.group(1)) / 100.0


def _read_mac_power_sample(timeout: float) -> MacPowerSample:
    completed = _run(("ioreg", "-r", "-n", "AppleSmartBattery"), timeout)
    if completed is None:
        return MacPowerSample(error="power telemetry command timed out or could not start")
    if completed.returncode != 0:
        return MacPowerSample(error="power telemetry unavailable")
    return _mac_power_sample_from_ioreg(completed.stdout)


def _mac_power_sample_from_ioreg(output: str) -> MacPowerSample:
    system_power_w = _ioreg_milliwatts(output, "SystemPowerIn")
    voltage_v = _ioreg_millivolts(output, "SystemVoltageIn")
    current_a = _ioreg_milliamps(output, "SystemCurrentIn")
    if system_power_w is None and voltage_v is not None and current_a is not None:
        system_power_w = voltage_v * current_a

    battery_power_w = _ioreg_milliwatts(output, "BatteryPower", signed=True)
    adapter_watts = None
    adapter_match = re.search(r'"AdapterDetails"\s*=\s*\{[^}]*"Watts"\s*=\s*(\d+)', output)
    if adapter_match:
        adapter_watts = int(adapter_match.group(1))

    if all(value is None for value in (system_power_w, voltage_v, current_a, adapter_watts, battery_power_w)):
        return MacPowerSample(error="no power telemetry fields found")

    return MacPowerSample(
        system_power_w=system_power_w,
        system_voltage_v=voltage_v,
        system_current_a=current_a,
        adapter_watts=adapter_watts,
        battery_power_w=battery_power_w,
        source="apple-smart-battery-power",
    )


def _ioreg_milliwatts(output: str, key: str, *, signed: bool = False) -> float | None:
    raw = _ioreg_int(output, key)
    if raw is None:
        return None
    if signed:
        raw = _signed_u64(raw)
    return raw / 1000.0


def _ioreg_millivolts(output: str, key: str) -> float | None:
    raw = _ioreg_int(output, key)
    return None if raw is None else raw / 1000.0


def _ioreg_milliamps(output: str, key: str) -> float | None:
    raw = _ioreg_int(output, key)
    return None if raw is None else raw / 1000.0


def _ioreg_int(output: str, key: str) -> int | None:
    match = re.search(rf'"{re.escape(key)}"\s*=\s*(-?\d+)', output)
    return None if not match else int(match.group(1))


def _signed_u64(value: int) -> int:
    return value - 2**64 if value >= 2**63 else value


def _read_process_thermal_state(timeout: float) -> str | None:
    completed = _run(
        (
            "swift",
            "-e",
            "import Foundation; print(ProcessInfo.processInfo.thermalState.rawValue)",
        ),
        timeout,
    )
    if completed is None or completed.returncode != 0:
        return None

    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        raw_value = int(lines[-1])
    except ValueError:
        return None
    return THERMAL_STATE_NAMES.get(raw_value)


def _read_pmset_thermal_state(timeout: float) -> str | None:
    completed = _run(("pmset", "-g", "therm"), timeout)
    if completed is None or completed.returncode != 0:
        return None

    lowered = completed.stdout.lower()
    if "no thermal warning" in lowered and "no performance warning" in lowered:
        return "nominal"
    if "critical" in lowered:
        return "critical"
    if "serious" in lowered or "warning" in lowered:
        return "serious"
    return None


def _run(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _first_value(payload: dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _first_float(payload: dict[str, Any], keys: Sequence[str]) -> float | None:
    value = _first_value(payload, keys)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_int(payload: dict[str, Any], keys: Sequence[str]) -> int | None:
    value = _first_value(payload, keys)
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_int_tuple(payload: dict[str, Any], keys: Sequence[str]) -> tuple[int, ...]:
    value = _first_value(payload, keys)
    if not isinstance(value, (list, tuple)):
        return ()

    items: list[int] = []
    for item in value:
        try:
            items.append(int(float(item)))
        except (TypeError, ValueError):
            continue
    return tuple(items)


def _first_string(payload: dict[str, Any], keys: Sequence[str]) -> str | None:
    value = _first_value(payload, keys)
    return str(value) if value is not None else None


def _sample_from_smc_snapshot(snapshot: SmcSnapshot, power: MacPowerSample | None = None) -> MacThermalSample:
    return MacThermalSample(
        temperature_c=snapshot.hottest_temperature_c,
        cpu_temperature_c=snapshot.cpu_temperature_c,
        gpu_temperature_c=snapshot.gpu_temperature_c,
        cpu_sensor_key=snapshot.cpu_sensor_key,
        gpu_sensor_key=snapshot.gpu_sensor_key,
        fan_rpm=snapshot.max_fan_rpm,
        fan_rpms=snapshot.fan_rpms,
        temperature_sensors=snapshot.temperature_sensors,
        power=power or MacPowerSample(),
        source=snapshot.source,
        error=snapshot.error,
    )


def _control_temperature(sample: MacThermalSample) -> tuple[float | None, str]:
    values: list[tuple[str, float]] = []
    if sample.cpu_temperature_c is not None:
        values.append(("Mac CPU", sample.cpu_temperature_c))
    if sample.gpu_temperature_c is not None:
        values.append(("Mac GPU", sample.gpu_temperature_c))
    if values:
        label, value = max(values, key=lambda item: item[1])
        return value, label
    return sample.temperature_c, "Mac temp"


def _normalize_thermal_state(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return THERMAL_STATE_NAMES.get(value)
    if isinstance(value, float) and value.is_integer():
        return THERMAL_STATE_NAMES.get(int(value))

    normalized = str(value).strip().lower()
    if normalized.isdecimal():
        return THERMAL_STATE_NAMES.get(int(normalized))
    if normalized in {"nominal", "fair", "serious", "critical"}:
        return normalized
    return None
