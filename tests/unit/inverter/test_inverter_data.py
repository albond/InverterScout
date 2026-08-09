"""Tests of InverterData calculated properties."""

from tests.conftest import make_inverter_data

from inverterscout.inverter.luxpower import (
    MAX_REASONABLE_GEN_POWER,
    REG_AC_INPUT_TYPE,
    REG_GEN_POWER,
    REG_GEN_VOLTAGE,
    REG_SOC_SOH,
    REG_STATUS,
    REG_VACR,
    REG_VBAT,
    _registers_to_data,
)


class TestPowerSource:
    def test_grid_ok_220v(self):
        d = make_inverter_data(status=0x20, grid_voltage=220.0)
        assert d.power_source == "grid"

    def test_grid_ok_at_180v(self):
        """180V is exactly the border, >= 180 = grid."""
        d = make_inverter_data(status=0x20, grid_voltage=180.0)
        assert d.power_source == "grid"

    def test_low_voltage_at_179(self):
        d = make_inverter_data(status=0x20, grid_voltage=179.9)
        assert d.power_source == "low_voltage"

    def test_low_voltage_150v(self):
        d = make_inverter_data(status=0x20, grid_voltage=150.0)
        assert d.power_source == "low_voltage"

    def test_low_voltage_at_50v(self):
        """50V is the limit, >= 50 = low_voltage (not no_grid)."""
        d = make_inverter_data(status=0x20, grid_voltage=50.0)
        assert d.power_source == "low_voltage"

    def test_no_grid_at_49v(self):
        d = make_inverter_data(status=0x20, grid_voltage=49.9)
        assert d.power_source == "no_grid"

    def test_no_grid_0v(self):
        d = make_inverter_data(status=0x20, grid_voltage=0.0)
        assert d.power_source == "no_grid"

    def test_off_grid_status_0x40(self):
        """Off-grid status → no_grid even with voltage=220V."""
        d = make_inverter_data(status=0x40, grid_voltage=220.0)
        assert d.power_source == "no_grid"

    def test_off_grid_status_0x80(self):
        d = make_inverter_data(status=0x80, grid_voltage=220.0)
        assert d.power_source == "no_grid"

    def test_off_grid_status_0xC0(self):
        d = make_inverter_data(status=0xC0, grid_voltage=220.0)
        assert d.power_source == "no_grid"

    def test_off_grid_status_0x88(self):
        d = make_inverter_data(status=0x88, grid_voltage=220.0)
        assert d.power_source == "no_grid"

    def test_on_grid_status_low_voltage(self):
        """On-grid status + low voltage = low_voltage, not no_grid."""
        d = make_inverter_data(status=0x20, grid_voltage=30.0)
        assert d.power_source == "no_grid"


class TestOnBattery:
    def test_on_battery_true_for_no_grid(self):
        d = make_inverter_data(status=0x40, grid_voltage=0.0)
        assert d.on_battery is True

    def test_on_battery_false_for_low_voltage(self):
        d = make_inverter_data(status=0x20, grid_voltage=150.0)
        assert d.on_battery is False

    def test_on_battery_false_for_grid(self):
        d = make_inverter_data(status=0x20, grid_voltage=220.0)
        assert d.on_battery is False


class TestGeneratorOn:
    def test_generator_off(self):
        d = make_inverter_data(ac_input_type=0)
        assert d.generator_on is False

    def test_generator_on_bit0(self):
        d = make_inverter_data(ac_input_type=1)
        assert d.generator_on is True

    def test_generator_on_other_bits(self):
        """bit 0 set, other bits too → still True."""
        d = make_inverter_data(ac_input_type=0x03)
        assert d.generator_on is True


class TestIsValid:
    def test_valid_normal(self):
        d = make_inverter_data(battery_voltage=52.0, soc=80)
        assert d.is_valid is True

    def test_invalid_zeros(self):
        d = make_inverter_data(battery_voltage=0.0, soc=0)
        assert d.is_valid is False

    def test_valid_zero_voltage_nonzero_soc(self):
        d = make_inverter_data(battery_voltage=0.0, soc=10)
        assert d.is_valid is True

    def test_valid_nonzero_voltage_zero_soc(self):
        d = make_inverter_data(battery_voltage=48.0, soc=0)
        assert d.is_valid is True


class TestTotalPvPower:
    def test_sum(self):
        d = make_inverter_data(pv1_power=300, pv2_power=200, pv3_power=100)
        assert d.total_pv_power == 600

    def test_zeros(self):
        d = make_inverter_data(pv1_power=0, pv2_power=0, pv3_power=0)
        assert d.total_pv_power == 0


class TestHousePower:
    """house_power = grid_import - grid_export + pv + bat_discharge - bat_charge."""

    def test_on_grid_with_solar(self):
        """Grid 400 W plus solar 500 W without export gives a 900 W house load."""
        d = make_inverter_data(
            grid_power_import=400,
            grid_power_export=0,
            pv1_power=300,
            pv2_power=200,
            battery_charge=0,
            battery_discharge=0,
        )
        assert d.house_power == 900

    def test_on_grid_export_surplus(self):
        """Network 0W, sun 1000W, export 300W → house = 700W."""
        d = make_inverter_data(
            grid_power_import=0,
            grid_power_export=300,
            pv1_power=600,
            pv2_power=400,
            battery_charge=0,
            battery_discharge=0,
        )
        assert d.house_power == 700

    def test_on_grid_charging_battery(self):
        """Mains 500W, sun 300W, battery charge 200W → house = 600W."""
        d = make_inverter_data(
            grid_power_import=500,
            grid_power_export=0,
            pv1_power=300,
            pv2_power=0,
            battery_charge=200,
            battery_discharge=0,
        )
        assert d.house_power == 600

    def test_off_grid_from_battery(self):
        """No network, battery discharge 800W → house = 800W."""
        d = make_inverter_data(
            status=0x40,
            grid_voltage=0.0,
            grid_power_import=0,
            grid_power_export=0,
            pv1_power=0,
            pv2_power=0,
            battery_charge=0,
            battery_discharge=800,
        )
        assert d.house_power == 800

    def test_off_grid_solar_plus_battery(self):
        """No network, sun 400W + battery 300W → house = 700W."""
        d = make_inverter_data(
            status=0x40,
            grid_voltage=0.0,
            grid_power_import=0,
            grid_power_export=0,
            pv1_power=200,
            pv2_power=200,
            battery_charge=0,
            battery_discharge=300,
        )
        assert d.house_power == 700

    def test_all_zeros(self):
        """Everything is zero → house = 0W."""
        d = make_inverter_data(
            grid_power_import=0,
            grid_power_export=0,
            pv1_power=0,
            pv2_power=0,
            battery_charge=0,
            battery_discharge=0,
        )
        assert d.house_power == 0

    def test_never_negative(self):
        """The result cannot be negative (max 0)."""
        d = make_inverter_data(
            grid_power_import=0,
            grid_power_export=500,
            pv1_power=100,
            pv2_power=0,
            battery_charge=200,
            battery_discharge=0,
        )
        # 0 - 500 + 100 + 0 - 200 = -600 → max(0, -600) = 0
        assert d.house_power == 0


class TestGenPowerSanityCheck:
    """Sanity-cap for gen_power (register 123 sometimes returns 12-24kW garbage)."""

    def _base_regs(self) -> dict[int, int]:
        return {
            REG_STATUS: 0x40,
            REG_VBAT: 540,
            REG_SOC_SOH: 50,
            REG_VACR: 0,
            REG_AC_INPUT_TYPE: 0x01,  # generator is on
            REG_GEN_VOLTAGE: 2240,
        }

    def test_normal_value_passes_through(self):
        regs = self._base_regs()
        regs[REG_GEN_POWER] = 1500
        d = _registers_to_data(regs)
        assert d.gen_power == 1500

    def test_at_cap_passes_through(self):
        regs = self._base_regs()
        regs[REG_GEN_POWER] = MAX_REASONABLE_GEN_POWER
        d = _registers_to_data(regs)
        assert d.gen_power == MAX_REASONABLE_GEN_POWER

    def test_above_cap_clipped_to_zero(self):
        """The abnormal value (12-24 kW) is reset to zero."""
        regs = self._base_regs()
        regs[REG_GEN_POWER] = 24202
        d = _registers_to_data(regs)
        assert d.gen_power == 0

    def test_zero_value_kept(self):
        regs = self._base_regs()
        regs[REG_GEN_POWER] = 0
        d = _registers_to_data(regs)
        assert d.gen_power == 0
