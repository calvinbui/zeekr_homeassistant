from unittest.mock import MagicMock, AsyncMock
import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError
from custom_components.zeekr_ev.climate import ZeekrClimate, async_setup_entry
from custom_components.zeekr_ev.const import DOMAIN
from custom_components.zeekr_ev.number import CONFIG_NUMBERS


class MockVehicle:
    def __init__(self, vin):
        self.vin = vin

    def do_remote_control(self, command, service_id, setting):
        return True


class MockCoordinator:
    def __init__(self, data):
        self.data = data
        self.vehicles = {}
        self.async_inc_invoke = AsyncMock()
        self.operation_durations = {vin: {"ac": 15} for vin in data}

    def get_vehicle_by_vin(self, vin):
        return self.vehicles.get(vin)

    async def async_request_refresh(self):
        pass


class DummyHass:
    def __init__(self):
        self.data = {}
        self.loop = MagicMock()

    async def async_add_executor_job(self, func, *args, **kwargs):
        return func(*args, **kwargs)

    def async_create_task(self, coro):
        return coro


@pytest.mark.asyncio
async def test_climate_optimistic_update():
    vin = "VIN1"
    initial_data = {
        vin: {
            "additionalVehicleStatus": {
                "climateStatus": {
                    "preClimateActive": "0",  # Off
                    "interiorTemp": "20.0"
                }
            }
        }
    }

    coordinator = MockCoordinator(initial_data)
    coordinator.operation_durations[vin]["ac"] = 17
    vehicle_mock = MagicMock()
    coordinator.vehicles[vin] = vehicle_mock

    climate = ZeekrClimate(coordinator, vin)
    climate.hass = DummyHass()
    # Simple mock for async_create_task
    climate.hass.async_create_task = MagicMock()
    climate.async_write_ha_state = MagicMock()

    # Test Turn On
    await climate.async_set_hvac_mode(HVACMode.HEAT_COOL)

    # Verify remote control called
    vehicle_mock.do_remote_control.assert_called()
    args, _ = vehicle_mock.do_remote_control.call_args
    assert args[0] == "start"
    assert args[1] == "ZAF"
    assert args[2]["serviceParameters"][0]["key"] == "AC"
    assert args[2]["serviceParameters"][0]["value"] == "true"
    assert args[2]["serviceParameters"][2] == {
        "key": "AC.duration",
        "value": "17",
    }

    # Verify Optimistic Update
    climate_status = coordinator.data[vin]["additionalVehicleStatus"]["climateStatus"]
    assert climate_status["preClimateActive"] == "1"
    climate.async_write_ha_state.assert_called()

    # Verify Delayed Refresh Task Created
    assert climate.hass.async_create_task.called
    climate.hass.async_create_task.call_args[0][0].close()

    # Test Turn Off
    await climate.async_set_hvac_mode(HVACMode.OFF)

    # Verify remote control called
    vehicle_mock.do_remote_control.assert_called_with(
        "start",
        "ZAF",
        {
            "serviceParameters": [
                {
                    "key": "AC",
                    "value": "false"
                }
            ]
        }
    )

    # Verify Optimistic Update
    climate_status = coordinator.data[vin]["additionalVehicleStatus"]["climateStatus"]
    assert climate_status["preClimateActive"] == "0"
    climate.async_write_ha_state.assert_called()

    # Verify Delayed Refresh Task Created again
    assert climate.hass.async_create_task.call_count == 2
    climate.hass.async_create_task.call_args[0][0].close()

    vehicle_mock.do_remote_control.return_value = False
    write_count = climate.async_write_ha_state.call_count
    with pytest.raises(HomeAssistantError, match="Failed to set climate mode"):
        await climate.async_set_hvac_mode(HVACMode.HEAT_COOL)
    assert climate.hvac_mode == HVACMode.OFF
    assert climate.async_write_ha_state.call_count == write_count
    assert climate.hass.async_create_task.call_count == 2


@pytest.mark.asyncio
async def test_climate_set_temperature_rolls_back_on_rejection():
    vin = "VIN1"
    coordinator = MockCoordinator(
        {vin: {"additionalVehicleStatus": {"climateStatus": {"preClimateActive": "1"}}}}
    )
    vehicle_mock = MagicMock()
    vehicle_mock.do_remote_control.return_value = False
    coordinator.vehicles[vin] = vehicle_mock

    climate = ZeekrClimate(coordinator, vin)
    climate.hass = DummyHass()
    climate.async_write_ha_state = MagicMock()
    previous_target = climate.target_temperature

    # The climate is running, so the new target is re-sent; a rejection restores the old one
    with pytest.raises(HomeAssistantError, match="Failed to set climate mode"):
        await climate.async_set_temperature(temperature=previous_target + 2)

    params = vehicle_mock.do_remote_control.call_args.args[2]["serviceParameters"]
    assert params[1] == {"key": "AC.temp", "value": str(previous_target + 2)}
    assert climate.target_temperature == previous_target
    climate.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_climate_set_temperature_rolls_back_on_request_error():
    vin = "VIN1"
    coordinator = MockCoordinator(
        {vin: {"additionalVehicleStatus": {"climateStatus": {"preClimateActive": "1"}}}}
    )
    vehicle_mock = MagicMock()
    vehicle_mock.do_remote_control.side_effect = ConnectionError("boom")
    coordinator.vehicles[vin] = vehicle_mock

    climate = ZeekrClimate(coordinator, vin)
    climate.hass = DummyHass()
    climate.async_write_ha_state = MagicMock()
    previous_target = climate.target_temperature

    # The request itself failing (network/auth) must roll the target back too
    with pytest.raises(ConnectionError):
        await climate.async_set_temperature(temperature=previous_target + 2)

    assert climate.target_temperature == previous_target
    climate.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_climate_set_temperature_resends_while_running():
    vin = "VIN1"
    coordinator = MockCoordinator(
        {vin: {"additionalVehicleStatus": {"climateStatus": {"preClimateActive": "1"}}}}
    )
    vehicle_mock = MagicMock()
    vehicle_mock.do_remote_control.return_value = True
    coordinator.vehicles[vin] = vehicle_mock

    climate = ZeekrClimate(coordinator, vin)
    climate.hass = DummyHass()
    # Simple mock for async_create_task
    climate.hass.async_create_task = MagicMock()
    climate.async_write_ha_state = MagicMock()
    previous_target = climate.target_temperature

    await climate.async_set_temperature(temperature=previous_target + 2)

    assert climate.target_temperature == previous_target + 2
    params = vehicle_mock.do_remote_control.call_args.args[2]["serviceParameters"]
    assert params[1] == {"key": "AC.temp", "value": str(previous_target + 2)}
    climate.async_write_ha_state.assert_called()
    assert climate.hass.async_create_task.called
    climate.hass.async_create_task.call_args[0][0].close()


@pytest.mark.asyncio
async def test_climate_uses_default_duration_when_unset():
    vin = "VIN1"
    coordinator = MockCoordinator(
        {vin: {"additionalVehicleStatus": {"climateStatus": {"preClimateActive": "0"}}}}
    )
    coordinator.operation_durations.clear()
    vehicle_mock = MagicMock()
    coordinator.vehicles[vin] = vehicle_mock

    climate = ZeekrClimate(coordinator, vin)
    climate.hass = DummyHass()
    # Simple mock for async_create_task
    climate.hass.async_create_task = MagicMock()
    climate.async_write_ha_state = MagicMock()

    await climate.async_set_hvac_mode(HVACMode.HEAT_COOL)

    # Falls back to the CONFIG_NUMBERS default when the vehicle has no duration yet
    params = vehicle_mock.do_remote_control.call_args.args[2]["serviceParameters"]
    assert params[2] == {
        "key": "AC.duration",
        "value": str(CONFIG_NUMBERS["ac_operation_duration"][2]),
    }
    climate.hass.async_create_task.call_args[0][0].close()


@pytest.mark.asyncio
async def test_climate_properties_missing_data(hass):
    coordinator = MockCoordinator({"VIN1": {}})
    climate = ZeekrClimate(coordinator, "VIN1")
    assert climate.hvac_mode == HVACMode.OFF
    assert climate.current_temperature is None


@pytest.mark.asyncio
async def test_climate_device_info(hass):
    coordinator = MockCoordinator({"VIN1": {}})
    climate = ZeekrClimate(coordinator, "VIN1")
    assert climate.device_info["identifiers"] == {(DOMAIN, "VIN1")}


@pytest.mark.asyncio
async def test_climate_async_setup_entry(hass, mock_config_entry):
    coordinator = MockCoordinator({"VIN1": {}})
    hass.data[DOMAIN] = {mock_config_entry.entry_id: coordinator}

    async_add_entities = MagicMock()

    await async_setup_entry(hass, mock_config_entry, async_add_entities)

    assert async_add_entities.called
    # Check types
    types = [type(e) for e in async_add_entities.call_args[0][0]]
    assert ZeekrClimate in types


@pytest.mark.asyncio
async def test_climate_attributes(hass):
    vin = "VIN1"
    # Example timestamp from user: 1763418526287
    # 2025-11-17 22:28:46.287 UTC
    update_time_ms = 1763418526287
    expected_iso = "2025-11-17T22:28:46.287000+00:00"

    initial_data = {
        vin: {
            "additionalVehicleStatus": {
                "climateStatus": {
                    "updateTime": update_time_ms
                }
            }
        }
    }

    coordinator = MockCoordinator(initial_data)
    climate = ZeekrClimate(coordinator, vin)

    attrs = climate.extra_state_attributes
    assert attrs["last_updated"] == expected_iso

    # Test missing updateTime
    initial_data[vin]["additionalVehicleStatus"]["climateStatus"].pop("updateTime")
    attrs = climate.extra_state_attributes
    assert "last_updated" not in attrs

    # Test invalid updateTime
    initial_data[vin]["additionalVehicleStatus"]["climateStatus"]["updateTime"] = "invalid"
    attrs = climate.extra_state_attributes
    assert "last_updated" not in attrs
