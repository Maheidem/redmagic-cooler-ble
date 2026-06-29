# Development

## Repository Layout

```text
src/redmagic_cooler_ble/
  client.py       Async Bleak client for the decoded GATT service
  cli.py          redmagic-cooler command
  constants.py    UUIDs and stable captured constants
  protocol.py     Offline-safe payload encoders and status decoder
  terminal.py     Curses terminal UI
  web.py          Static web UI server
  web/ui.html     Web Bluetooth UI
  py.typed        Type information marker
tests/
  test_protocol.py
examples/
docs/
  protocol.md
  api.md
```

Raw reverse-engineering captures live in `captures/` locally and are ignored by
Git. Keep decoded facts in `docs/protocol.md` instead of committing large logs or
screenshots.

## Local Setup

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

## Checks

Run offline tests:

```sh
python -m unittest discover -s tests
```

Run the hardware-free example:

```sh
python examples/offline_decode.py
```

Check the CLI:

```sh
redmagic-cooler --help
redmagic-cooler scan
```

Build package artifacts:

```sh
python -m pip install build
python -m build
```

## BLE Testing

The Android app and this tool compete for the same BLE peripheral. Stop the app
before testing from the Mac:

```sh
adb -s 192.168.31.63:46779 shell am force-stop cn.nubia.externdevice
```

Start from the lowest fan level:

```sh
redmagic-cooler set-fan 1 --device "RM Magcooler"
redmagic-cooler watch --device "RM Magcooler"
```

## Release Checklist

1. Run `python -m unittest discover -s tests`.
2. Run `python -m build`.
3. Test `redmagic-cooler scan` against the real device.
4. Confirm `README.md` points at the real GitHub URL, not the placeholder.
5. Create a tag such as `v0.1.0`.
