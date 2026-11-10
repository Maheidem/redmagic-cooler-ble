import unittest

from redmagic_cooler_ble.mac_control import (
    FanControlConfig,
    MacThermalSample,
    _mac_power_sample_from_ioreg,
    decide_fan_level,
)
from redmagic_cooler_ble.protocol import CoolerStatus


class MacControlTests(unittest.TestCase):
    def test_hot_mac_temperature_increases_level(self):
        decision = decide_fan_level(
            FanControlConfig(),
            MacThermalSample(cpu_temperature_c=90.0, source="test"),
            CoolerStatus(temperature_c=12),
        )

        self.assertEqual(decision.level, 4)
        self.assertIn("CPU", decision.reason)

    def test_cooler_floor_forces_idle(self):
        decision = decide_fan_level(
            FanControlConfig(),
            MacThermalSample(cpu_temperature_c=90.0, thermal_state="serious", source="test"),
            CoolerStatus(temperature_c=0),
        )

        self.assertEqual(decision.level, 1)
        self.assertIn("floor", decision.reason)

    def test_missing_cooler_temperature_caps_level(self):
        decision = decide_fan_level(
            FanControlConfig(),
            MacThermalSample(cpu_temperature_c=90.0, source="test"),
            CoolerStatus(),
        )

        self.assertEqual(decision.level, 2)
        self.assertIn("telemetry", decision.reason)

    def test_soft_cap_limits_level_while_cooler_is_cold(self):
        decision = decide_fan_level(
            FanControlConfig(),
            MacThermalSample(cpu_temperature_c=90.0, source="test"),
            CoolerStatus(temperature_c=4),
        )

        self.assertEqual(decision.level, 2)
        self.assertIn("capped", decision.reason)

    def test_mac_fan_rpm_can_drive_level(self):
        decision = decide_fan_level(
            FanControlConfig(),
            MacThermalSample(fan_rpm=5100, source="test"),
            CoolerStatus(temperature_c=12),
        )

        self.assertEqual(decision.level, 4)

    def test_nominal_state_holds_idle_without_temperature(self):
        decision = decide_fan_level(
            FanControlConfig(),
            MacThermalSample(thermal_state="nominal", source="test"),
            CoolerStatus(temperature_c=12),
        )

        self.assertEqual(decision.level, 1)

    def test_mac_power_sample_parses_ioreg_telemetry(self):
        sample = _mac_power_sample_from_ioreg(
            '''
            "AdapterDetails" = {"Watts"=140,"Description"="pd charger"}
            "PowerTelemetryData" = {"SystemPowerIn"=130643,"SystemVoltageIn"=27222,
            "SystemCurrentIn"=4799,"BatteryPower"=18446744073709549844}
            '''
        )

        self.assertEqual(sample.adapter_watts, 140)
        self.assertAlmostEqual(sample.system_power_w, 130.643)
        self.assertAlmostEqual(sample.system_voltage_v, 27.222)
        self.assertAlmostEqual(sample.system_current_a, 4.799)
        self.assertAlmostEqual(sample.battery_power_w, -1.772)


if __name__ == "__main__":
    unittest.main()
