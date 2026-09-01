import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from custom_components.zeekr_ev.number import (
    ZeekrChargingLimitNumber,
    ZeekrConfigNumber,
    _migrate_legacy_config_numbers,
    async_setup_entry,
)
from custom_components.zeekr_ev.const import DOMAIN


@pytest.fixture(autouse=True)
def _instant_sleep():
    """Make the post-write reconcile delay instant in tests."""
    with patch("custom_components.zeekr_ev.number.asyncio.sleep", new=AsyncMock()):
        yield


class MockVehicle:
    def __init__(self, vin):
        self.vin = vin
        self.do_remote_control = MagicMock()


class MockCoordinator:
    def __init__(self, vehicles):
        self.vehicles = vehicles
        self.data = {v.vin: {} for v in vehicles}
        self.async_inc_invoke = AsyncMock()
        self.async_request_refresh = AsyncMock()
        self.operation_durations = {}

    def get_vehicle_by_vin(self, vin):
        for v in self.vehicles:
            if v.vin == vin:
                return v
        return None


class DummyConfig:
    def __init__(self):
        self.config_dir = "/tmp/dummy_config_dir"

    def path(self, *args):
        return "/tmp/dummy_path"


class DummyHass:
    def __init__(self):
        self.config = DummyConfig()
        self.data = {}
        self.created_tasks = []

    async def async_add_executor_job(self, func, *args, **kwargs):
        return func(*args, **kwargs)

    def async_create_task(self, coro, *args, **kwargs):
        task = asyncio.ensure_future(coro)
        self.created_tasks.append(task)
        return task


@pytest.mark.asyncio
async def test_charging_limit_number():
    vin = "VIN1"
    vehicle = MockVehicle(vin)
    coordinator = MockCoordinator([vehicle])

    number_entity = ZeekrChargingLimitNumber(coordinator, vin)
    number_entity.hass = DummyHass()
    number_entity.async_write_ha_state = MagicMock()

    # Test setting value 80%
    await number_entity.async_set_native_value(80.0)

    coordinator.async_inc_invoke.assert_called_once()
    vehicle.do_remote_control.assert_called_with(
        "start",
        "RCS",
        {
            "serviceParameters": [
                {
                    "key": "soc",
                    "value": "800"
                },
                {
                    "key": "rcs.setting",
                    "value": "1"
                },
                {
                    "key": "altCurrent",
                    "value": "1"
                }
            ]
        }
    )

    # Check optimistic update
    assert number_entity.native_value == 80.0
    number_entity.async_write_ha_state.assert_called()

    # Drain the scheduled reconcile task to avoid a dangling pending task.
    await asyncio.gather(*number_entity.hass.created_tasks)


@pytest.mark.asyncio
async def test_charging_limit_write_reconciles_after_delay():
    """A charging-limit write updates the value immediately and schedules a refresh."""
    vin = "VIN1"
    vehicle = MockVehicle(vin)
    coordinator = MockCoordinator([vehicle])

    number_entity = ZeekrChargingLimitNumber(coordinator, vin)
    number_entity.hass = DummyHass()
    number_entity.async_write_ha_state = MagicMock()

    await number_entity.async_set_native_value(80.0)

    # Optimistic coordinator update so native_value doesn't snap back.
    assert coordinator.data[vin]["chargingLimit"]["soc"] == 800
    assert number_entity.native_value == 80.0

    # A reconcile task was scheduled; run it and confirm it refreshes.
    assert number_entity.hass.created_tasks
    await asyncio.gather(*number_entity.hass.created_tasks)
    coordinator.async_request_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_charging_limit_read_from_coordinator():
    vin = "VIN1"
    vehicle = MockVehicle(vin)
    coordinator = MockCoordinator([vehicle])

    # Inject data into coordinator
    coordinator.data[vin] = {
        "chargingLimit": {
            "soc": "900"
        }
    }

    number_entity = ZeekrChargingLimitNumber(coordinator, vin)
    number_entity.hass = DummyHass()

    # Should read 90.0
    assert number_entity.native_value == 90.0

    # Update data
    coordinator.data[vin]["chargingLimit"]["soc"] = "550"
    assert number_entity.native_value == 55.0


@pytest.mark.asyncio
async def test_charging_limit_step():
    vin = "VIN1"
    vehicle = MockVehicle(vin)
    coordinator = MockCoordinator([vehicle])

    number_entity = ZeekrChargingLimitNumber(coordinator, vin)

    assert number_entity.native_step == 5


@pytest.mark.asyncio
async def test_config_number():
    vehicle = MockVehicle("VIN1")
    coordinator = MockCoordinator([vehicle])
    coordinator.operation_durations[vehicle.vin] = {"seat": 10}

    number_entity = ZeekrConfigNumber(
        coordinator,
        vehicle.vin,
        "seat_op",
        "Seat Operation",
        "seat",
        15,
    )
    number_entity.hass = DummyHass()
    number_entity.async_write_ha_state = MagicMock()

    # Check initial value
    assert number_entity.native_value == 10
    assert number_entity.native_min_value == 1
    assert number_entity.native_max_value == 60
    assert number_entity.device_info["identifiers"] == {("zeekr_ev", vehicle.vin)}

    # Set value
    await number_entity.async_set_native_value(5)
    assert number_entity.native_value == 5
    assert coordinator.operation_durations[vehicle.vin]["seat"] == 5
    number_entity.async_write_ha_state.assert_called()


@pytest.mark.asyncio
async def test_config_numbers_are_created_per_vehicle():
    coordinator = MockCoordinator([MockVehicle("VIN2"), MockVehicle("VIN1")])
    hass = DummyHass()
    entry = SimpleNamespace(entry_id="test_entry")
    hass.data = {DOMAIN: {entry.entry_id: coordinator}}
    async_add_entities = MagicMock()

    with patch(
        "custom_components.zeekr_ev.number._migrate_legacy_config_numbers",
        return_value={"seat": 12},
    ) as migrate:
        await async_setup_entry(hass, entry, async_add_entities)

    migrate.assert_called_once_with(hass, entry.entry_id, "VIN1")
    config_numbers = [
        entity
        for entity in async_add_entities.call_args.args[0]
        if isinstance(entity, ZeekrConfigNumber)
    ]
    assert {(entity.unique_id, entity.native_value) for entity in config_numbers} == {
        ("VIN1_seat_operation_duration", 12),
        ("VIN1_ac_operation_duration", 15),
        ("VIN1_steering_wheel_heat_duration", 8),
        ("VIN2_seat_operation_duration", 12),
        ("VIN2_ac_operation_duration", 15),
        ("VIN2_steering_wheel_heat_duration", 8),
    }


def test_migrate_legacy_config_numbers():
    registry = MagicMock()

    registry.async_get_entity_id.side_effect = lambda _domain, _platform, unique_id: (
        "number.seat_operation_duration"
        if unique_id == "test_entry_seat_operation_duration"
        else None
    )

    restored = SimpleNamespace(
        last_states={
            "number.seat_operation_duration": SimpleNamespace(
                state=SimpleNamespace(state="unavailable"),
                extra_data=SimpleNamespace(as_dict=lambda: {"native_value": 12}),
            )
        }
    )

    with patch(
        "custom_components.zeekr_ev.number.er.async_get", return_value=registry
    ), patch(
        "custom_components.zeekr_ev.number.restore_state.async_get",
        return_value=restored,
    ):
        values = _migrate_legacy_config_numbers(MagicMock(), "test_entry", "VIN1")

    assert values == {"seat": 12}
    registry.async_update_entity.assert_called_once_with(
        "number.seat_operation_duration",
        new_unique_id="VIN1_seat_operation_duration",
    )
