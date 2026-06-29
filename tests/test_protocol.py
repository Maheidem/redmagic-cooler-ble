import unittest

from redmagic_cooler_ble.protocol import (
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


class ProtocolTests(unittest.TestCase):
    def test_status_frames_accumulate(self):
        status = CoolerStatus()

        decode_status_frame(bytes.fromhex("04080000"), status)
        decode_status_frame(bytes.fromhex("0813ec00"), status)
        decode_status_frame(bytes.fromhex("090a0000"), status)
        decode_status_frame(bytes.fromhex("07080000"), status)
        decode_status_frame(bytes.fromhex("0500d200"), status)

        self.assertEqual(status.temperature_c, 2)
        self.assertEqual(status.rpm, 5100)
        self.assertEqual(status.power_w, 10)
        self.assertEqual(status.fan_limit, 8)
        self.assertEqual(status.protection_raw, "0500d200")
        self.assertEqual(status.last_frame, "0500d200")

    def test_negative_temperature_offset(self):
        status = decode_status_frame(bytes.fromhex("04030000"))
        self.assertEqual(status.temperature_c, -3)

    def test_fan_level_payloads(self):
        expected = ["2e", "34", "3a", "40", "44", "48", "4c", "50"]
        for level, hex_payload in enumerate(expected, start=1):
            payload = fan_level_to_payload(level)
            self.assertEqual(bytes_to_hex(payload), hex_payload)
            self.assertEqual(fan_payload_to_level(payload), level)

    def test_invalid_fan_level(self):
        with self.assertRaises(ValueError):
            fan_level_to_payload(0)
        with self.assertRaises(ValueError):
            fan_level_to_payload(9)
        self.assertIsNone(fan_payload_to_level(b"\xff"))

    def test_switch_payloads(self):
        self.assertEqual(auto_control_payload(True), b"\x01")
        self.assertEqual(auto_control_payload(False), b"\x00")
        self.assertEqual(diablo_mode_payload(True), b"\x01")
        self.assertEqual(diablo_mode_payload(False), b"\x00")
        self.assertEqual(thermal_switch_payload(True), b"\x02")
        self.assertEqual(thermal_switch_payload(False), b"\x03")

    def test_light_payloads(self):
        self.assertEqual(light_payload("off"), bytes.fromhex("06000000"))
        self.assertEqual(light_payload("colorful"), bytes.fromhex("01000000"))
        self.assertEqual(light_payload("breath"), bytes.fromhex("02000000"))
        self.assertEqual(light_payload("fixed", "#ff0000"), bytes.fromhex("04ff0000"))
        self.assertEqual(light_payload("fixed", (0, 255, 161)), bytes.fromhex("0400ffa1"))

    def test_invalid_light_payload(self):
        with self.assertRaises(ValueError):
            light_payload("fixed")
        with self.assertRaises(ValueError):
            light_payload("fixed", "#bad")
        with self.assertRaises(ValueError):
            light_payload("unknown")


if __name__ == "__main__":
    unittest.main()
