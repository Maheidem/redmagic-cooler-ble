# Python API

The package is designed around two layers:

- `redmagic_cooler_ble.client`: async BLE access through Bleak.
- `redmagic_cooler_ble.protocol`: pure Python protocol encode/decode helpers.

Use the protocol layer for tests, offline log decoding, and validating payloads.
Use `RedMagicCoolerClient` when talking to real hardware.

## RedMagicCoolerClient

```python
from redmagic_cooler_ble import RedMagicCoolerClient
```

Constructor:

```python
RedMagicCoolerClient(
    device=None,
    device_match=None,
    scan_timeout=10.0,
)
```

Arguments:

- `device`: optional Bleak device object. Usually leave this as `None`.
- `device_match`: optional name/address substring, for example `"RM Magcooler"`.
- `scan_timeout`: seconds to scan before giving up.

The client is an async context manager:

```python
async with RedMagicCoolerClient(device_match="RM Magcooler") as cooler:
    ...
```

The context manager connects on entry and disconnects on exit.

## Telemetry

```python
await cooler.start_status_notifications(callback)
await cooler.stop_status_notifications()
```

The callback receives:

```python
callback(status: CoolerStatus, frame: bytes)
```

`status` is the accumulated latest state. `frame` is the raw notification that
just arrived.

`CoolerStatus` fields:

- `temperature_c`: displayed temperature in Celsius.
- `rpm`: fan speed.
- `power_w`: reported power in watts.
- `fan_limit`: fan limit value.
- `protection_raw`: latest undecoded protection/load frame.
- `last_frame`: latest raw frame as hex.
- `last_update`: Unix timestamp for latest frame.
- `recent_frames`: recent raw frames as hex.

Example:

```python
def on_status(status, frame):
    print(status.temperature_c, status.rpm, status.power_w, frame.hex())

await cooler.start_status_notifications(on_status)
```

## Fan Control

```python
payload = await cooler.set_fan_level(1)
```

Fan level must be `1` through `8`. Level `1` is the lowest decoded target.
The returned `payload` is the exact byte sequence written to the characteristic.

Read current fan level if supported by the device:

```python
level = await cooler.read_fan_level()
```

This returns an integer `1` through `8`, or `None` if the current byte is not one
of the decoded values.

## Switches

```python
await cooler.set_auto_control(True)
await cooler.set_auto_control(False)

await cooler.set_thermal_switch(True)
await cooler.set_thermal_switch(False)

await cooler.set_diablo_mode(True)
await cooler.set_diablo_mode(False)
```

These methods return the exact bytes written.

## Lighting

```python
await cooler.set_light("off")
await cooler.set_light("colorful")
await cooler.set_light("breath")
await cooler.set_light("fixed", "#ff0000")
await cooler.set_light("fixed", (0, 255, 161))
```

Valid modes:

- `off`
- `colorful`
- `breath`
- `fixed`

Fixed mode requires a color as `"#RRGGBB"` or `(r, g, b)`.

## Scanning

```python
from redmagic_cooler_ble import find_cooler, list_advertisements

device = await find_cooler("RM Magcooler")
advertisements = await list_advertisements(timeout=10)
```

`list_advertisements()` returns `Advertisement` objects with:

- `name`
- `address`
- `rssi`
- `service_uuids`

On macOS, `address` is usually a CoreBluetooth UUID, not the BLE MAC address
shown by Android.

## Protocol Helpers

These helpers do not require BLE hardware:

```python
from redmagic_cooler_ble import (
    auto_control_payload,
    decode_status_frame,
    diablo_mode_payload,
    fan_level_to_payload,
    fan_payload_to_level,
    light_payload,
    thermal_switch_payload,
)
```

Examples:

```python
assert fan_level_to_payload(1) == bytes.fromhex("2e")
assert fan_payload_to_level(bytes.fromhex("50")) == 8

status = decode_status_frame(bytes.fromhex("0813ec00"))
assert status.rpm == 5100

assert light_payload("fixed", "#ff0000") == bytes.fromhex("04ff0000")
assert thermal_switch_payload(True) == bytes.fromhex("02")
```

## Error Handling

Common failures:

- `RuntimeError("Cooler not found...")`: the cooler did not advertise during the
  scan window. Stop the Android app and scan again.
- `RuntimeError("not connected")`: a write/read was called before connection or
  after disconnect.
- `ValueError`: invalid protocol input, such as fan level `9` or malformed color.
