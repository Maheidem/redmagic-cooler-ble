"""Decode captured frames without connecting to BLE hardware."""

from redmagic_cooler_ble import CoolerStatus, decode_status_frame

status = CoolerStatus()

for frame in ("04030000", "0813ec00", "090a0000", "07080000"):
    decode_status_frame(bytes.fromhex(frame), status)

print(f"temperature_c={status.temperature_c}")
print(f"rpm={status.rpm}")
print(f"power_w={status.power_w}")
print(f"fan_limit={status.fan_limit}")
