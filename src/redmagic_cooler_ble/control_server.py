"""Local browser dashboard for Mac-driven cooler control."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .client import RedMagicCoolerClient
from .mac_control import FanControlConfig, FanDecision, MacThermalSample, decide_fan_level, read_mac_thermal_sample
from .protocol import CoolerStatus


@dataclass
class DashboardState:
    config: FanControlConfig
    auto_enabled: bool = True
    manual_level: int = 1
    current_level: int = 1
    target_level: int = 1
    decision_reason: str = "starting"
    action: str = "starting"
    connected: bool = False
    dry_run: bool = False
    mac_sample: MacThermalSample | None = None
    cooler_status: CoolerStatus | None = None
    error: str | None = None
    started_at: float = 0.0
    last_update: float = 0.0
    last_write: float = 0.0


class SharedDashboard:
    def __init__(self, state: DashboardState) -> None:
        self.state = state
        self.lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            state = replace(self.state)
            config = asdict(self.state.config)
            sample = self.state.mac_sample
            cooler = self.state.cooler_status

        top_sensors: list[dict[str, Any]] = []
        if sample:
            top_sensors = [
                {"key": key, "temperature_c": round(value, 1)}
                for key, value in sorted(sample.temperature_sensors.items(), key=lambda item: item[1], reverse=True)[:24]
            ]
        power = sample.power if sample else None

        return {
            "connected": state.connected,
            "dry_run": state.dry_run,
            "auto_enabled": state.auto_enabled,
            "manual_level": state.manual_level,
            "current_level": state.current_level,
            "target_level": state.target_level,
            "decision_reason": state.decision_reason,
            "action": state.action,
            "error": state.error,
            "uptime_seconds": max(0, round(time.time() - state.started_at, 1)) if state.started_at else 0,
            "last_update": state.last_update,
            "last_write": state.last_write,
            "config": config,
            "mac": {
                "temperature_c": _round(sample.temperature_c if sample else None),
                "cpu_temperature_c": _round(sample.cpu_temperature_c if sample else None),
                "gpu_temperature_c": _round(sample.gpu_temperature_c if sample else None),
                "cpu_sensor_key": sample.cpu_sensor_key if sample else None,
                "gpu_sensor_key": sample.gpu_sensor_key if sample else None,
                "fan_rpm": sample.fan_rpm if sample else None,
                "fan_rpms": list(sample.fan_rpms) if sample else [],
                "thermal_state": sample.thermal_state if sample else None,
                "source": sample.source if sample else None,
                "error": sample.error if sample else None,
                "power": {
                    "system_power_w": _round(power.system_power_w if power else None),
                    "system_voltage_v": _round(power.system_voltage_v if power else None),
                    "system_current_a": _round(power.system_current_a if power else None),
                    "adapter_watts": power.adapter_watts if power else None,
                    "battery_power_w": _round(power.battery_power_w if power else None),
                    "source": power.source if power else None,
                    "error": power.error if power else None,
                },
                "top_sensors": top_sensors,
            },
            "cooler": {
                "temperature_c": cooler.temperature_c if cooler else None,
                "rpm": cooler.rpm if cooler else None,
                "power_w": cooler.power_w if cooler else None,
                "fan_limit": cooler.fan_limit if cooler else None,
                "last_frame": cooler.last_frame if cooler else None,
                "recent_frames": cooler.recent_frames if cooler else [],
            },
        }

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        numeric_float_fields = {
            "start_temp_c",
            "temp_step_c",
            "cooler_floor_c",
            "cooler_soft_cap_c",
        }
        numeric_int_fields = {
            "idle_level",
            "max_level",
            "fan_rpm_start",
            "fan_rpm_step",
            "cooler_soft_cap_level",
            "max_without_cooler_temp",
        }
        with self.lock:
            changes: dict[str, Any] = {}
            for field_name in numeric_float_fields:
                if field_name in payload:
                    changes[field_name] = float(payload[field_name])
            for field_name in numeric_int_fields:
                if field_name in payload:
                    value = int(payload[field_name])
                    if field_name in {"idle_level", "max_level", "cooler_soft_cap_level", "max_without_cooler_temp"}:
                        value = _clamp(value, 1, 8)
                    changes[field_name] = value

            updated_config = replace(self.state.config, **changes)
            if updated_config.max_level < updated_config.idle_level:
                updated_config = replace(updated_config, max_level=updated_config.idle_level)
            if updated_config.cooler_soft_cap_level < updated_config.idle_level:
                updated_config = replace(updated_config, cooler_soft_cap_level=updated_config.idle_level)
            if updated_config.max_without_cooler_temp < updated_config.idle_level:
                updated_config = replace(updated_config, max_without_cooler_temp=updated_config.idle_level)
            self.state.config = updated_config
            if "auto_enabled" in payload:
                self.state.auto_enabled = bool(payload["auto_enabled"])
            if "manual_level" in payload:
                self.state.manual_level = _clamp(int(payload["manual_level"]), 1, 8)
                self.state.auto_enabled = False

        return self.snapshot()


async def run_control_dashboard(client: RedMagicCoolerClient, args: argparse.Namespace) -> None:
    """Run the local dashboard and control loop until interrupted."""

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
    shared = SharedDashboard(
        DashboardState(
            config=config,
            manual_level=config.idle_level,
            current_level=config.idle_level,
            dry_run=args.dry_run,
            cooler_status=client.status,
            connected=client.is_connected,
            started_at=time.time(),
        )
    )

    httpd, port = _start_http_server(shared, args.host, args.port)
    url = f"http://{args.host}:{port}"
    print(f"Dashboard: {url}", flush=True)
    if args.open:
        webbrowser.open(url)

    try:
        await _control_loop(client, shared, args)
    finally:
        httpd.shutdown()
        httpd.server_close()


async def _control_loop(client: RedMagicCoolerClient, shared: SharedDashboard, args: argparse.Namespace) -> None:
    def on_status(_status: CoolerStatus, _payload: bytes) -> None:
        with shared.lock:
            shared.state.cooler_status = client.status
            shared.state.last_update = time.time()

    last_write_monotonic = 0.0
    last_reconnect_monotonic = 0.0
    notifications_started = False

    async def ensure_notifications() -> None:
        nonlocal notifications_started
        if notifications_started or not client.is_connected:
            return
        try:
            await client.start_status_notifications(on_status)
            notifications_started = True
            with shared.lock:
                shared.state.connected = True
                shared.state.error = None
        except Exception as exc:
            notifications_started = False
            _mark_ble_error(shared, f"status notifications failed: {exc}")

    async def ensure_connected() -> bool:
        nonlocal last_reconnect_monotonic, notifications_started
        if client.is_connected:
            await ensure_notifications()
            return client.is_connected

        notifications_started = False
        now = time.monotonic()
        if now - last_reconnect_monotonic < max(args.reconnect_interval, 1.0):
            _mark_ble_error(shared, "BLE disconnected")
            return False

        last_reconnect_monotonic = now
        _mark_ble_error(shared, "BLE disconnected; reconnecting")
        try:
            await client.connect()
            await ensure_notifications()
            with shared.lock:
                shared.state.action = "reconnected"
                shared.state.connected = True
                shared.state.error = None
            return True
        except Exception as exc:
            _mark_ble_error(shared, f"BLE reconnect failed: {exc}")
            return False

    async def safe_write(label: str, write) -> bool:
        try:
            await write()
            return True
        except Exception as exc:
            _mark_ble_error(shared, f"{label} failed: {exc}")
            return False

    try:
        await ensure_notifications()
        if args.startup_safe:
            if args.dry_run:
                with shared.lock:
                    shared.state.action = "dry startup safe"
                    shared.state.last_write = time.time()
                last_write_monotonic = time.monotonic()
            else:
                wrote_diablo = await safe_write("startup Diablo off", lambda: client.set_diablo_mode(False))
                wrote_fan = await safe_write("startup fan level", lambda: client.set_fan_level(args.idle_level))
                if wrote_diablo and wrote_fan:
                    with shared.lock:
                        shared.state.action = "startup safe"
                        shared.state.current_level = args.idle_level
                        shared.state.last_write = time.time()
                    last_write_monotonic = time.monotonic()

        while True:
            with shared.lock:
                config = shared.state.config
                auto_enabled = shared.state.auto_enabled
                manual_level = shared.state.manual_level
                current_level = shared.state.current_level

            sample = await asyncio.to_thread(read_mac_thermal_sample)
            decision = (
                decide_fan_level(config, sample, client.status)
                if auto_enabled
                else FanDecision(manual_level, f"manual level {manual_level}")
            )
            now = time.monotonic()
            wrote = False

            if decision.level != current_level and now - last_write_monotonic >= args.min_change_interval:
                if args.dry_run:
                    wrote = True
                    last_write_monotonic = now
                else:
                    if await ensure_connected():
                        wrote = await safe_write("fan write", lambda: client.set_fan_level(decision.level))
                        if wrote:
                            last_write_monotonic = now

            with shared.lock:
                shared.state.connected = client.is_connected
                shared.state.mac_sample = sample
                shared.state.cooler_status = client.status
                shared.state.target_level = decision.level
                shared.state.decision_reason = decision.reason
                if wrote:
                    shared.state.current_level = decision.level
                    shared.state.last_write = time.time()
                    shared.state.action = "set" if not args.dry_run else "would set"
                else:
                    shared.state.action = "hold"
                shared.state.last_update = time.time()
                if sample.error and shared.state.error is None:
                    shared.state.error = sample.error

            if not client.is_connected:
                await ensure_connected()

            await asyncio.sleep(max(args.interval, 0.5))
    finally:
        notifications_started = False
        with shared.lock:
            exit_level = shared.state.config.idle_level

        if args.exit_safe and not args.dry_run and client.is_connected:
            try:
                await safe_write("exit fan level", lambda: client.set_fan_level(exit_level))
                await safe_write("exit Diablo off", lambda: client.set_diablo_mode(False))
                with shared.lock:
                    shared.state.current_level = exit_level
                    shared.state.action = "exit safe"
                    shared.state.last_write = time.time()
            finally:
                await client.stop_status_notifications()
        else:
            await client.stop_status_notifications()


def _mark_ble_error(shared: SharedDashboard, message: str) -> None:
    with shared.lock:
        shared.state.connected = False
        shared.state.error = message
        shared.state.action = "disconnected"
        shared.state.decision_reason = message
        shared.state.last_update = time.time()


def _start_http_server(shared: SharedDashboard, host: str, requested_port: int) -> tuple[ThreadingHTTPServer, int]:
    handler = _make_handler(shared)
    last_error: OSError | None = None
    for port in range(requested_port, requested_port + 20):
        try:
            httpd = ThreadingHTTPServer((host, port), handler)
            httpd.daemon_threads = True
        except OSError as exc:
            last_error = exc
            continue

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, port

    raise RuntimeError(f"could not bind dashboard port: {last_error}")


def _make_handler(shared: SharedDashboard):
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/" or self.path.startswith("/index.html"):
                self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if self.path.startswith("/api/status"):
                self._send_json(200, shared.snapshot())
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            payload = self._read_json()
            if self.path.startswith("/api/config"):
                self._send_json(200, shared.update_config(payload))
                return
            if self.path.startswith("/api/auto"):
                self._send_json(200, shared.update_config({"auto_enabled": bool(payload.get("enabled", True))}))
                return
            if self.path.startswith("/api/fan"):
                self._send_json(200, shared.update_config({"manual_level": int(payload.get("level", 1))}))
                return
            if self.path.startswith("/api/safe"):
                with shared.lock:
                    idle_level = shared.state.config.idle_level
                self._send_json(200, shared.update_config({"manual_level": idle_level}))
                return
            self._send_json(404, {"error": "not found"})

        def log_message(self, _format: str, *_args) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return min(max(value, minimum), maximum)


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RedMagic Cooler Control</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f8;
      --panel: #ffffff;
      --line: #d8dde3;
      --text: #1f2429;
      --muted: #65717d;
      --accent: #0f8b6f;
      --warn: #b87503;
      --danger: #bb3030;
      --cool: #1d6fd8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: #fff;
      position: sticky;
      top: 0;
      z-index: 10;
    }
    h1 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: 0; }
    main {
      max-width: 1220px;
      margin: 0 auto;
      padding: 16px;
      display: grid;
      gap: 16px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
    }
    section, .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    section { padding: 14px; }
    .metric { padding: 12px; min-height: 94px; }
    .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .04em;
      margin-bottom: 5px;
    }
    .value { font-size: 28px; font-weight: 700; letter-spacing: 0; white-space: nowrap; }
    .sub { color: var(--muted); font-size: 12px; min-height: 18px; }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(320px, .9fr);
      gap: 16px;
    }
    h2 { margin: 0 0 12px; font-size: 15px; }
    .controls { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 16px; }
    .control { display: grid; gap: 6px; }
    .control-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .control-title { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
    .info {
      display: inline-grid;
      place-items: center;
      width: 17px;
      height: 17px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
    }
    .help-text {
      min-height: 34px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .effect {
      margin: 10px 0 12px;
      padding: 9px 10px;
      border: 1px solid #d8e8e3;
      border-radius: 8px;
      background: #f6fbf9;
      color: #315248;
      font-size: 13px;
      line-height: 1.35;
    }
    .measure-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }
    .measure-item {
      min-width: 0;
      padding: 2px 0;
    }
    .measure-value {
      font-size: 20px;
      font-weight: 700;
      white-space: nowrap;
    }
    input[type="range"] { width: 100%; accent-color: var(--accent); }
    input[type="number"] {
      width: 76px;
      padding: 6px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      font: inherit;
      background: #fff;
    }
    button {
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      cursor: pointer;
    }
    button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    button.danger { border-color: #d8a3a3; color: var(--danger); }
    .button-row { display: flex; flex-wrap: wrap; gap: 8px; }
    .levels { display: grid; grid-template-columns: repeat(8, 1fr); gap: 6px; }
    .levels button { min-width: 0; padding: 8px 0; }
    .switch { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); }
    .status-line { color: var(--muted); font-size: 13px; }
    canvas { width: 100%; height: 180px; border: 1px solid var(--line); border-radius: 8px; background: #fbfcfd; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 7px 6px; border-bottom: 1px solid #edf0f2; text-align: left; }
    th { color: var(--muted); font-weight: 600; }
    .hot { color: var(--danger); }
    .warm { color: var(--warn); }
    .cool { color: var(--cool); }
    @media (max-width: 860px) {
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .measure-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .layout { grid-template-columns: 1fr; }
      .controls { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>RedMagic Cooler Control</h1>
    <label class="switch"><input id="autoEnabled" type="checkbox" checked> Auto</label>
  </header>
  <main>
    <div class="grid">
      <div class="metric"><div class="label">CPU</div><div id="cpu" class="value">--</div><div id="cpuKey" class="sub">--</div></div>
      <div class="metric"><div class="label">GPU</div><div id="gpu" class="value">--</div><div id="gpuKey" class="sub">--</div></div>
      <div class="metric"><div class="label">Mac Fans</div><div id="macFans" class="value">--</div><div id="macFanList" class="sub">--</div></div>
      <div class="metric"><div class="label">Mac Power</div><div id="macPower" class="value">--</div><div id="macPowerSub" class="sub">--</div></div>
      <div class="metric"><div class="label">Cooler</div><div id="cooler" class="value">--</div><div id="coolerSub" class="sub">--</div></div>
    </div>

    <section>
      <h2>Measurement</h2>
      <div class="measure-grid">
        <div class="measure-item"><div class="label">CPU 60s avg</div><div id="avgCpu" class="measure-value">--</div><div id="trendCpu" class="sub">--</div></div>
        <div class="measure-item"><div class="label">GPU 60s avg</div><div id="avgGpu" class="measure-value">--</div><div id="trendGpu" class="sub">--</div></div>
        <div class="measure-item"><div class="label">Mac fan 60s avg</div><div id="avgMacFan" class="measure-value">--</div><div id="maxMacFan" class="sub">--</div></div>
        <div class="measure-item"><div class="label">Mac power 60s avg</div><div id="avgMacPower" class="measure-value">--</div><div id="maxMacPower" class="sub">--</div></div>
        <div class="measure-item"><div class="label">Cooler 60s avg</div><div id="avgCoolerPower" class="measure-value">--</div><div id="avgCoolerRpm" class="sub">--</div></div>
      </div>
      <div class="status-line" id="measureLine">Waiting for enough samples.</div>
      <table style="margin-top: 10px;">
        <thead><tr><th>Level</th><th>Samples</th><th>CPU avg</th><th>GPU avg</th><th>Mac fan avg</th><th>Mac W avg</th><th>Cooler W avg</th></tr></thead>
        <tbody id="levelRows"></tbody>
      </table>
    </section>

    <div class="layout">
      <section>
        <h2>Curve</h2>
        <canvas id="curve" width="760" height="220"></canvas>
        <div id="curveEffect" class="effect">Waiting for live sensor data.</div>
        <div class="controls">
          <label class="control">
            <span class="control-row"><span class="control-title">Start temp <span class="info" title="Temperature where the cooler first rises above idle.">i</span></span><input data-config="start_temp_c" type="number" min="30" max="110" step="1"></span>
            <input data-config="start_temp_c" type="range" min="30" max="110" step="1">
            <span class="help-text" data-help="start_temp_c"></span>
          </label>
          <label class="control">
            <span class="control-row"><span class="control-title">Temp step <span class="info" title="Degrees between each extra cooler level.">i</span></span><input data-config="temp_step_c" type="number" min="1" max="25" step="1"></span>
            <input data-config="temp_step_c" type="range" min="1" max="25" step="1">
            <span class="help-text" data-help="temp_step_c"></span>
          </label>
          <label class="control">
            <span class="control-row"><span class="control-title">Idle level <span class="info" title="Lowest cooler level used while the Mac is below the start temperature.">i</span></span><input data-config="idle_level" type="number" min="1" max="8" step="1"></span>
            <input data-config="idle_level" type="range" min="1" max="8" step="1">
            <span class="help-text" data-help="idle_level"></span>
          </label>
          <label class="control">
            <span class="control-row"><span class="control-title">Max level <span class="info" title="Highest cooler level the automatic curve may request.">i</span></span><input data-config="max_level" type="number" min="1" max="8" step="1"></span>
            <input data-config="max_level" type="range" min="1" max="8" step="1">
            <span class="help-text" data-help="max_level"></span>
          </label>
          <label class="control">
            <span class="control-row"><span class="control-title">Cooler floor <span class="info" title="Hard cold limit for the cooler's own temperature sensor.">i</span></span><input data-config="cooler_floor_c" type="number" min="-10" max="20" step="1"></span>
            <input data-config="cooler_floor_c" type="range" min="-10" max="20" step="1">
            <span class="help-text" data-help="cooler_floor_c"></span>
          </label>
          <label class="control">
            <span class="control-row"><span class="control-title">Cold cap <span class="info" title="Soft cold limit that holds the cooler to a low level while it is already cold.">i</span></span><input data-config="cooler_soft_cap_c" type="number" min="-10" max="30" step="1"></span>
            <input data-config="cooler_soft_cap_c" type="range" min="-10" max="30" step="1">
            <span class="help-text" data-help="cooler_soft_cap_c"></span>
          </label>
        </div>
      </section>

      <section>
        <h2>Control</h2>
        <div class="status-line" id="stateLine">--</div>
        <div class="button-row" style="margin: 12px 0;">
          <button id="safeBtn" class="danger">Safe</button>
        </div>
        <div class="levels" id="levels"></div>
        <h2 style="margin-top: 18px;">Hottest Sensors</h2>
        <table>
          <thead><tr><th>Key</th><th>Temp</th></tr></thead>
          <tbody id="sensorRows"></tbody>
        </table>
      </section>
    </div>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const configInputs = [...document.querySelectorAll("[data-config]")];
    let latest = null;
    let pending = null;
    const history = [];
    const historyWindowSeconds = 600;

    function fmt(value, suffix = "") {
      return value === null || value === undefined ? "--" : `${value}${suffix}`;
    }
    function fmtFixed(value, suffix = "", digits = 1) {
      return Number.isFinite(value) ? `${value.toFixed(digits)}${suffix}` : "--";
    }
    function numeric(value) {
      return Number.isFinite(Number(value)) ? Number(value) : null;
    }
    function tempClass(value) {
      if (value === null || value === undefined) return "";
      if (value >= 85) return "hot";
      if (value >= 70) return "warm";
      return "cool";
    }
    async function post(path, body) {
      const res = await fetch(path, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
      });
      latest = await res.json();
      render(latest);
    }
    function updateConfig(name, value) {
      clearTimeout(pending);
      pending = setTimeout(() => post("/api/config", {[name]: Number(value)}), 120);
    }
    function syncInputs(config) {
      for (const input of configInputs) {
        const value = config[input.dataset.config];
        if (document.activeElement !== input) input.value = value;
      }
    }
    function render(data) {
      latest = data;
      const mac = data.mac;
      const power = mac.power || {};
      const cooler = data.cooler;
      $("cpu").textContent = fmt(mac.cpu_temperature_c, " C");
      $("cpu").className = `value ${tempClass(mac.cpu_temperature_c)}`;
      $("cpuKey").textContent = mac.cpu_sensor_key || "--";
      $("gpu").textContent = fmt(mac.gpu_temperature_c, " C");
      $("gpu").className = `value ${tempClass(mac.gpu_temperature_c)}`;
      $("gpuKey").textContent = mac.gpu_sensor_key || "--";
      $("macFans").textContent = fmt(mac.fan_rpm, " RPM");
      $("macFanList").textContent = mac.fan_rpms.length ? mac.fan_rpms.map((rpm, i) => `F${i} ${rpm}`).join(" | ") : "--";
      $("macPower").textContent = fmt(power.system_power_w, " W");
      $("macPowerSub").textContent = power.adapter_watts ? `adapter ${power.adapter_watts} W` : (power.error || "--");
      $("cooler").textContent = fmt(cooler.temperature_c, " C");
      $("coolerSub").textContent = `RPM ${fmt(cooler.rpm)} | ${fmt(cooler.power_w, " W")} | level ${data.current_level}`;
      $("autoEnabled").checked = data.auto_enabled;
      $("stateLine").textContent = `${data.action} | target ${data.target_level} | ${data.decision_reason}`;
      syncInputs(data.config);
      updateCurveHelp(data);
      renderLevels(data);
      renderSensors(mac.top_sensors);
      rememberSample(data);
      renderMeasurements();
      drawCurve(data);
    }
    function renderLevels(data) {
      const levels = $("levels");
      if (!levels.children.length) {
        for (let level = 1; level <= 8; level += 1) {
          const button = document.createElement("button");
          button.textContent = String(level);
          button.addEventListener("click", () => post("/api/fan", {level}));
          levels.appendChild(button);
        }
      }
      [...levels.children].forEach((button, index) => {
        const level = index + 1;
        button.classList.toggle("active", level === data.current_level);
      });
    }
    function renderSensors(rows) {
      $("sensorRows").innerHTML = rows.map((row) => (
        `<tr><td>${row.key}</td><td class="${tempClass(row.temperature_c)}">${row.temperature_c} C</td></tr>`
      )).join("");
    }
    function rememberSample(data) {
      const mac = data.mac || {};
      const power = mac.power || {};
      const cooler = data.cooler || {};
      const ts = data.last_update || (Date.now() / 1000);
      const previous = history[history.length - 1];
      if (previous && Math.abs(previous.ts - ts) < 0.05) return;
      history.push({
        ts,
        level: data.current_level,
        cpu: numeric(mac.cpu_temperature_c),
        gpu: numeric(mac.gpu_temperature_c),
        macFan: numeric(mac.fan_rpm),
        macPower: numeric(power.system_power_w),
        coolerPower: numeric(cooler.power_w),
        coolerRpm: numeric(cooler.rpm),
        coolerTemp: numeric(cooler.temperature_c),
      });
      const cutoff = ts - historyWindowSeconds;
      while (history.length && history[0].ts < cutoff) history.shift();
    }
    function values(rows, key) {
      return rows.map((row) => row[key]).filter((value) => Number.isFinite(value));
    }
    function avg(rows, key) {
      const items = values(rows, key);
      return items.length ? items.reduce((sum, value) => sum + value, 0) / items.length : null;
    }
    function maxValue(rows, key) {
      const items = values(rows, key);
      return items.length ? Math.max(...items) : null;
    }
    function trendPerMinute(rows, key) {
      const items = rows.filter((row) => Number.isFinite(row[key]));
      if (items.length < 2) return null;
      const first = items[0];
      const last = items[items.length - 1];
      const minutes = (last.ts - first.ts) / 60;
      return minutes >= 0.5 ? (last[key] - first[key]) / minutes : null;
    }
    function trendText(rows, key, suffix) {
      const trend = trendPerMinute(rows, key);
      if (!Number.isFinite(trend)) return "--";
      const sign = trend > 0 ? "+" : "";
      return `${sign}${trend.toFixed(1)}${suffix}/min`;
    }
    function renderMeasurements() {
      if (!history.length) return;
      const now = history[history.length - 1].ts;
      const recent = history.filter((row) => row.ts >= now - 60);
      $("avgCpu").textContent = fmtFixed(avg(recent, "cpu"), " C");
      $("trendCpu").textContent = trendText(recent, "cpu", " C");
      $("avgGpu").textContent = fmtFixed(avg(recent, "gpu"), " C");
      $("trendGpu").textContent = trendText(recent, "gpu", " C");
      $("avgMacFan").textContent = fmtFixed(avg(recent, "macFan"), " RPM", 0);
      $("maxMacFan").textContent = `max ${fmtFixed(maxValue(recent, "macFan"), " RPM", 0)}`;
      $("avgMacPower").textContent = fmtFixed(avg(recent, "macPower"), " W");
      $("maxMacPower").textContent = `max ${fmtFixed(maxValue(recent, "macPower"), " W")}`;
      $("avgCoolerPower").textContent = fmtFixed(avg(recent, "coolerPower"), " W");
      $("avgCoolerRpm").textContent = `RPM ${fmtFixed(avg(recent, "coolerRpm"), "", 0)}`;
      renderLevelRows();

      const coolerPower = avg(recent, "coolerPower");
      const macPower = avg(recent, "macPower");
      const levels = [...new Set(history.map((row) => row.level).filter((level) => level !== null && level !== undefined))];
      const levelText = levels.length > 1
        ? "The table is now comparing levels seen in this page session."
        : "Use manual levels during the same workload to build an A/B comparison here.";
      $("measureLine").textContent = recent.length < 30
        ? "Collecting samples for rolling averages."
        : `Last 60s: cooler draw ${fmtFixed(coolerPower, " W")}, Mac input ${fmtFixed(macPower, " W")}. ${levelText}`;
    }
    function renderLevelRows() {
      const groups = new Map();
      for (const row of history) {
        if (row.level === null || row.level === undefined) continue;
        if (!groups.has(row.level)) groups.set(row.level, []);
        groups.get(row.level).push(row);
      }
      const rows = [...groups.entries()]
        .sort((a, b) => Number(a[0]) - Number(b[0]))
        .map(([level, items]) => (
          `<tr><td>${level}</td><td>${items.length}</td><td>${fmtFixed(avg(items, "cpu"), " C")}</td><td>${fmtFixed(avg(items, "gpu"), " C")}</td><td>${fmtFixed(avg(items, "macFan"), " RPM", 0)}</td><td>${fmtFixed(avg(items, "macPower"), " W")}</td><td>${fmtFixed(avg(items, "coolerPower"), " W")}</td></tr>`
        ));
      $("levelRows").innerHTML = rows.length ? rows.join("") : "<tr><td colspan=\"7\">No samples yet.</td></tr>";
    }
    function curveLevelAt(temp, cfg) {
      let level = cfg.idle_level;
      if (temp >= cfg.start_temp_c) {
        level = cfg.idle_level + 1 + Math.floor((temp - cfg.start_temp_c) / Math.max(1, cfg.temp_step_c));
      }
      return Math.min(Math.max(level, cfg.idle_level), cfg.max_level);
    }
    function levelStartTemp(level, cfg) {
      if (level <= cfg.idle_level) return null;
      return cfg.start_temp_c + (level - cfg.idle_level - 1) * Math.max(1, cfg.temp_step_c);
    }
    function updateHelpText(name, text) {
      const node = document.querySelector(`[data-help="${name}"]`);
      if (node) node.textContent = text;
    }
    function updateCurveHelp(data) {
      const cfg = data.config;
      const macTemp = Math.max(data.mac.cpu_temperature_c || 0, data.mac.gpu_temperature_c || 0);
      const rawLevel = macTemp ? curveLevelAt(macTemp, cfg) : cfg.idle_level;
      const maxTemp = levelStartTemp(cfg.max_level, cfg);
      const nextLevel = Math.min(data.target_level + 1, cfg.max_level);
      const nextTemp = nextLevel > data.target_level ? levelStartTemp(nextLevel, cfg) : null;
      const coldCapLevel = cfg.cooler_soft_cap_level || 2;
      const coolerTemp = data.cooler.temperature_c;

      updateHelpText("start_temp_c", `Below ${cfg.start_temp_c} C the curve stays at idle level ${cfg.idle_level}. At ${cfg.start_temp_c} C it starts asking for level ${Math.min(cfg.idle_level + 1, cfg.max_level)}.`);
      updateHelpText("temp_step_c", `Every ${cfg.temp_step_c} C above start adds one level. Smaller means more aggressive cooling.`);
      updateHelpText("idle_level", `Minimum automatic level. Safe/exit also returns to level ${cfg.idle_level}.`);
      updateHelpText("max_level", maxTemp === null ? `Automatic mode never goes above level ${cfg.max_level}.` : `Automatic mode never goes above level ${cfg.max_level}; this curve reaches it around ${maxTemp} C.`);
      updateHelpText("cooler_floor_c", `If cooler temp is ${cfg.cooler_floor_c} C or colder, force idle level ${cfg.idle_level}.`);
      updateHelpText("cooler_soft_cap_c", `If cooler temp is ${cfg.cooler_soft_cap_c} C or colder, cap the request at level ${coldCapLevel}.`);

      const coolingGuard = coolerTemp === null || coolerTemp === undefined
        ? `Cooler temp is not known yet, so requests are capped at level ${cfg.max_without_cooler_temp}.`
        : coolerTemp <= cfg.cooler_floor_c
          ? `Cooler is at ${coolerTemp} C, so the floor forces level ${cfg.idle_level}.`
          : coolerTemp <= cfg.cooler_soft_cap_c
            ? `Cooler is at ${coolerTemp} C, so cold cap limits requests to level ${coldCapLevel}.`
            : `Cooler is at ${coolerTemp} C, so no cold cap is active.`;
      const nextText = nextTemp === null ? "The target is already at the max level." : `Next level starts around ${nextTemp} C.`;
      $("curveEffect").textContent = macTemp
        ? `Current CPU/GPU max is ${macTemp.toFixed(1)} C: raw curve asks level ${rawLevel}, active target is level ${data.target_level}. ${nextText} ${coolingGuard}`
        : `${coolingGuard} Waiting for CPU/GPU temperature to calculate the active curve target.`;
    }
    function drawCurve(data) {
      const canvas = $("curve");
      const ctx = canvas.getContext("2d");
      const cfg = data.config;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = "#d8dde3";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 8; i += 1) {
        const y = 20 + i * 20;
        ctx.beginPath(); ctx.moveTo(42, y); ctx.lineTo(canvas.width - 20, y); ctx.stroke();
      }
      ctx.strokeStyle = "#0f8b6f";
      ctx.lineWidth = 3;
      ctx.beginPath();
      for (let temp = 30; temp <= 110; temp += 1) {
        const level = curveLevelAt(temp, cfg);
        const x = 42 + ((temp - 30) / 80) * (canvas.width - 70);
        const y = canvas.height - 24 - ((level - 1) / 7) * (canvas.height - 48);
        if (temp === 30) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
      const currentTemp = Math.max(data.mac.cpu_temperature_c || 0, data.mac.gpu_temperature_c || 0);
      if (currentTemp) {
        const x = 42 + ((Math.min(Math.max(currentTemp, 30), 110) - 30) / 80) * (canvas.width - 70);
        ctx.fillStyle = "#bb3030";
        ctx.beginPath(); ctx.arc(x, 18, 5, 0, Math.PI * 2); ctx.fill();
      }
      ctx.fillStyle = "#65717d";
      ctx.font = "12px system-ui";
      ctx.fillText("30 C", 32, canvas.height - 6);
      ctx.fillText("110 C", canvas.width - 56, canvas.height - 6);
      ctx.fillText("Level 8", 6, 28);
      ctx.fillText("Level 1", 6, canvas.height - 22);
    }

    configInputs.forEach((input) => {
      input.addEventListener("input", (event) => {
        const name = event.target.dataset.config;
        const value = event.target.value;
        for (const peer of configInputs.filter((item) => item.dataset.config === name && item !== event.target)) {
          peer.value = value;
        }
        updateConfig(name, value);
      });
    });
    $("autoEnabled").addEventListener("change", (event) => post("/api/auto", {enabled: event.target.checked}));
    $("safeBtn").addEventListener("click", () => post("/api/safe", {}));
    async function tick() {
      try {
        const res = await fetch("/api/status", {cache: "no-store"});
        render(await res.json());
      } catch (error) {
        $("stateLine").textContent = String(error);
      }
    }
    tick();
    setInterval(tick, 1000);
  </script>
</body>
</html>
"""
