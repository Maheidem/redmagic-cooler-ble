# RedMagic Cooler BLE

Reusable Python module and command-line tools for reverse-engineered BLE control
of the RedMagic / Nubia magnetic phone cooler seen as `RM Magcooler 8pro`.

The package exposes a clean async Python API for:

- scanning for the cooler
- subscribing to live telemetry
- setting fan target level
- toggling auto control, thermal switch, and Diablo mode
- setting lighting modes and fixed RGB colors
- decoding captured status frames offline

The browser UI is included as a debug convenience. The core of this repository
is the `redmagic_cooler_ble` Python package and its documented protocol layer.

## Hardware Target

The current decoding target is the RedMagic Heat Sink 8Pro family.

Observed during reverse engineering:

| Field | Value |
| --- | --- |
| BLE advertisement name | `RM Magcooler 8pro` |
| Android app package | `cn.nubia.externdevice` |
| Android app device code | `rm_charge_clip8_pro` |
| Android-observed BLE address | `D0:00:FF:60:28:55` |
| App screen label | `Placa magnetica RedMagic Heat Sink 8Pro` |

macOS exposes a CoreBluetooth UUID instead of the Android BLE address. That is
normal.

## Safety

This cooler can reach sub-zero displayed temperatures. Start at fan level 1,
watch live temperature and power telemetry, and do not leave high fan levels or
Diablo mode enabled unattended.

The fan command is a target level, not a direct watt command. Reported watts and
RPM are telemetry reported later by the device.

Only one BLE central should be connected at a time. If the Android app is still
connected, the cooler may not advertise to your computer.

## Install

From a checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Runtime dependency:

- `bleak>=0.22`

The package metadata is in [pyproject.toml](pyproject.toml).

## Python API Quickstart

```python
import asyncio

from redmagic_cooler_ble import RedMagicCoolerClient, bytes_to_hex


def on_status(status, frame: bytes) -> None:
    print(
        "frame={frame} temp={temp}C rpm={rpm} power={power}W".format(
            frame=bytes_to_hex(frame),
            temp=status.temperature_c if status.temperature_c is not None else "--",
            rpm=status.rpm if status.rpm is not None else "--",
            power=status.power_w if status.power_w is not None else "--",
        )
    )


async def main() -> None:
    async with RedMagicCoolerClient(device_match="RM Magcooler") as cooler:
        await cooler.start_status_notifications(on_status)
        await cooler.set_fan_level(1)
        await asyncio.sleep(10)


asyncio.run(main())
```

Full API documentation: [docs/api.md](docs/api.md).

## Python API Surface

Import from the package root for normal use:

```python
from redmagic_cooler_ble import (
    Advertisement,
    CoolerStatus,
    RedMagicCoolerClient,
    auto_control_payload,
    bytes_to_hex,
    decode_status_frame,
    diablo_mode_payload,
    fan_level_to_payload,
    fan_payload_to_level,
    find_cooler,
    light_payload,
    list_advertisements,
    thermal_switch_payload,
)
```

Hardware access:

```python
async with RedMagicCoolerClient(device_match="RM Magcooler") as cooler:
    await cooler.start_status_notifications(callback)
    await cooler.set_fan_level(1)
    await cooler.set_auto_control(False)
    await cooler.set_thermal_switch(True)
    await cooler.set_diablo_mode(False)
    await cooler.set_light("fixed", "#00ffa1")
```

Offline protocol usage:

```python
from redmagic_cooler_ble import CoolerStatus, decode_status_frame, fan_level_to_payload

status = CoolerStatus()
decode_status_frame(bytes.fromhex("04030000"), status)
decode_status_frame(bytes.fromhex("0813ec00"), status)
decode_status_frame(bytes.fromhex("090a0000"), status)

assert status.temperature_c == -3
assert status.rpm == 5100
assert status.power_w == 10
assert fan_level_to_payload(1) == bytes.fromhex("2e")
```

## Examples

Hardware examples:

```sh
python examples/watch_status.py
python examples/set_safe_fan_level.py
```

Offline example, no hardware required:

```sh
python examples/offline_decode.py
```

The examples are intentionally short and use only the public package API.

## CLI Usage

The package installs one command:

```sh
redmagic-cooler --help
```

List visible BLE advertisements:

```sh
redmagic-cooler scan
```

Watch live telemetry:

```sh
redmagic-cooler watch --device "RM Magcooler"
```

Set the fan to the lowest decoded target:

```sh
redmagic-cooler set-fan 1 --device "RM Magcooler"
```

Set other fan levels:

```sh
redmagic-cooler set-fan 4 --device "RM Magcooler"
redmagic-cooler set-fan 8 --device "RM Magcooler"
```

Toggle captured switches:

```sh
redmagic-cooler set-auto on --device "RM Magcooler"
redmagic-cooler set-auto off --device "RM Magcooler"

redmagic-cooler set-thermal on --device "RM Magcooler"
redmagic-cooler set-thermal off --device "RM Magcooler"

redmagic-cooler set-diablo on --device "RM Magcooler"
redmagic-cooler set-diablo off --device "RM Magcooler"
```

Set lighting:

```sh
redmagic-cooler set-light off --device "RM Magcooler"
redmagic-cooler set-light colorful --device "RM Magcooler"
redmagic-cooler set-light breath --device "RM Magcooler"
redmagic-cooler set-light fixed --color "#ff0000" --device "RM Magcooler"
```

Run the terminal telemetry/fan slider:

```sh
redmagic-cooler terminal --device "RM Magcooler"
```

The old prototype wrapper remains for compatibility:

```sh
python cooler_test.py --level 1 --device "RM Magcooler"
python cooler_test.py --watch --device "RM Magcooler"
```

## Disconnect the Android App

If the cooler is connected to the Android app, stop the app before connecting
from your computer:

```sh
adb shell am force-stop cn.nubia.externdevice
```

If you have multiple Android devices connected, pass a serial:

```sh
adb -s 192.168.31.63:46779 shell am force-stop cn.nubia.externdevice
```

Find your serial with:

```sh
adb devices -l
```

## Decoded Protocol Summary

Primary GATT service:

```text
d52082ad-e805-9f97-9d4e-1c682d9c9ce6
```

Observed advertised service:

```text
00004a41-0000-1000-8000-00805f9b34fb
```

Characteristics:

| Purpose | UUID |
| --- | --- |
| Fan target write/read | `00001012-0000-1000-8000-00805f9b34fb` |
| Lighting write/read | `00001013-0000-1000-8000-00805f9b34fb` |
| Thermal switch write/read | `00001011-0000-1000-8000-00805f9b34fb` |
| Status notify | `00001015-0000-1000-8000-00805f9b34fb` |
| Diablo mode write | `00001017-0000-1000-8000-00805f9b34fb` |
| Auto temperature control write | `00001018-0000-1000-8000-00805f9b34fb` |

Status frames:

| Frame | Meaning |
| --- | --- |
| `04 xx 00 00` | displayed temperature is `xx - 6` Celsius |
| `07 xx .. ..` | fan limit |
| `08 hi lo 00` | fan RPM, big-endian |
| `09 xx 00 00` | reported power in watts |
| `05 .. .. ..` | protection/load frame, not fully decoded |

Fan target payloads:

| Level | Payload |
| --- | --- |
| 1 | `2e` |
| 2 | `34` |
| 3 | `3a` |
| 4 | `40` |
| 5 | `44` |
| 6 | `48` |
| 7 | `4c` |
| 8 | `50` |

Full protocol notes: [docs/protocol.md](docs/protocol.md).

## Debug Browser UI

The browser UI is a convenience for manually exercising captured functions. It
is not required for Python library use.

Use Chrome or Edge; Safari does not support Web Bluetooth.

```sh
redmagic-cooler web --port 8766 --open
```

Open:

```text
http://127.0.0.1:8766/ui.html
```

The UI includes live telemetry, fan level, switches, lighting controls, and a
raw BLE log. The fan slider does not write while dragging unless `Send on slide`
is enabled.

## Development

Run tests:

```sh
python -m unittest discover -s tests
```

Run the offline example:

```sh
python examples/offline_decode.py
```

Build the package:

```sh
python -m pip install build
python -m build
```

Project layout:

```text
src/redmagic_cooler_ble/
  client.py       Async Bleak client
  cli.py          redmagic-cooler CLI
  constants.py    UUIDs and captured constants
  protocol.py     Payload encoders and telemetry decoder
  terminal.py     Curses terminal UI
  web.py          Static debug UI server
  web/ui.html     Web Bluetooth debug UI
  py.typed        Type information marker
examples/
tests/
docs/
```

Raw captures are ignored via `.gitignore`. Keep decoded facts in docs and tests
instead of committing large logs or screenshots.

## Troubleshooting

### The cooler does not appear in scans

Stop the Android app and scan again:

```sh
adb shell am force-stop cn.nubia.externdevice
redmagic-cooler scan
```

Move the cooler closer to the computer. During testing, the real cooler appeared
as `RM Magcooler 8pro` after the Android app was stopped.

### macOS shows a UUID instead of `D0:00:FF:60:28:55`

That is expected. CoreBluetooth exposes its own per-host device UUID instead of
the BLE MAC address observed from Android.

### Browser cannot connect

Use Chrome or Edge and serve over `127.0.0.1` or `localhost`. Web Bluetooth is
available only in secure contexts; local loopback is allowed.

### Writes succeed but power does not immediately match the fan level

The fan level is a target command. The cooler reports power and RPM separately,
and those values ramp over time.

## Publishing Checklist

Before publishing this repo:

1. Add the GitHub remote URL to `pyproject.toml` once the repo exists.
2. Run `python -m unittest discover -s tests`.
3. Run `python -m build`.
4. Confirm no raw files under `captures/` are staged.
5. Test at least `redmagic-cooler scan`, `watch`, and `set-fan 1` against the device.
6. Tag the first release, for example `v0.1.0`.

## Status

This is reverse-engineered hardware support, not an official RedMagic or Nubia
project. Unknown fields are documented as unknown until they are captured and
verified.
