# BLE Protocol Notes

These notes document the behavior observed from the Android app and confirmed
with direct BLE writes from macOS. The protocol is not vendor-published, so treat
unknown fields as provisional until more captures are available.

## Device

Observed product names:

- App screen: `Placa magnetica RedMagic Heat Sink 8Pro`
- BLE advertisement on macOS: `RM Magcooler 8pro`
- Android app package: `cn.nubia.externdevice`
- Android app device code: `rm_charge_clip8_pro`

The Android BLE address observed during capture was `D0:00:FF:60:28:55`.
macOS exposes a CoreBluetooth UUID instead of this address.

## Service and Characteristics

| Purpose | UUID |
| --- | --- |
| Advertised service | `00004a41-0000-1000-8000-00805f9b34fb` |
| Primary GATT service | `d52082ad-e805-9f97-9d4e-1c682d9c9ce6` |
| Fan target write/read | `00001012-0000-1000-8000-00805f9b34fb` |
| Lighting write/read | `00001013-0000-1000-8000-00805f9b34fb` |
| Thermal switch write/read | `00001011-0000-1000-8000-00805f9b34fb` |
| Status notify | `00001015-0000-1000-8000-00805f9b34fb` |
| Diablo mode write | `00001017-0000-1000-8000-00805f9b34fb` |
| Auto temperature control write | `00001018-0000-1000-8000-00805f9b34fb` |

## Status Notifications

The status characteristic sends short independent frames. Keep the latest value
for each field and update it as frames arrive.

| Frame | Meaning | Decode |
| --- | --- | --- |
| `04 xx 00 00` | Temperature | display Celsius is `xx - 6` |
| `07 xx .. ..` | Fan limit | `xx` as integer |
| `08 hi lo 00` | Fan speed | big-endian RPM, `(hi << 8) | lo` |
| `09 xx 00 00` | Reported power | `xx` watts |
| `05 .. .. ..` | Protection/load state | not fully decoded |

Examples:

| Frame | Decoded |
| --- | --- |
| `04080000` | `2 C` |
| `04030000` | `-3 C` |
| `0813ec00` | `5100 RPM` |
| `090a0000` | `10 W` |
| `07080000` | fan limit `8` |

## Fan Target

The Android UI calls this `Nivel de refrigeracao`. The value is a target level,
not a direct watt setting. The power/RPM telemetry changes after the write.

| UI level | Payload |
| --- | --- |
| 1 | `2e` |
| 2 | `34` |
| 3 | `3a` |
| 4 | `40` |
| 5 | `44` |
| 6 | `48` |
| 7 | `4c` |
| 8 | `50` |

Confirmed direct write examples:

- Level 1: `2e`, observed power falling toward `10 W`
- Level 7: `4c`, observed power frames around `18 W` to `31 W`
- Level 8: `50`, observed RPM around `5100`

## Switches

| App label | Characteristic | On | Off |
| --- | --- | --- | --- |
| Controlo auto | `00001018-0000-1000-8000-00805f9b34fb` | `01` | `00` |
| Interruptor termico | `00001011-0000-1000-8000-00805f9b34fb` | `02` | `03` |
| Modo Deus da Destruicao / Diablo mode | `00001017-0000-1000-8000-00805f9b34fb` | `01` | `00` |

The Android logs name the thermal switch operation as `HALL_W`, but the visible
app label is the thermal switch.

## Lighting

Lighting commands write four bytes to `00001013-0000-1000-8000-00805f9b34fb`.

| Mode | Payload |
| --- | --- |
| Off | `06000000` |
| Colorful/rainbow | `01000000` |
| Breath full color | `02000000` |
| Fixed color | `04RRGGBB` |

Examples:

- Red: `04ff0000`
- Green/cyan capture example: `0400ffa1`
- Yellow/orange capture example: `04ffcc00`

## Safety Notes

The cooler can reach sub-zero displayed temperatures. Start testing at fan level
1, watch temperature/power telemetry, and avoid leaving Diablo mode or high fan
levels active unattended.

Only one central should be connected at a time. If the Android app is connected,
the device may stop advertising to macOS. Force-stop the Android package before
testing from the Mac:

```sh
adb -s 192.168.31.63:46779 shell am force-stop cn.nubia.externdevice
```
