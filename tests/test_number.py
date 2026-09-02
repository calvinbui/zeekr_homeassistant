import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
from homeassistant.helpers.update_coordinator import BaseCoordinatorEntity

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
    assert number_entity.device_info["identifiers"] == {(DOMAIN, vehicle.vin)}

    # Set value
    await number_entity.async_set_native_value(5)
    assert number_entity.native_value == 5
    assert coordinator.operation_durations[vehicle.vin]["seat"] == 5
    number_entity.async_write_ha_state.assert_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restored", "expected"),
    [(0, 1), (999, 60), (7.0, 7)],
    ids=["clamped-to-min", "clamped-to-max", "in-range"],
)
async def test_config_number_clamps_restored_value(restored, expected):
    vehicle = MockVehicle("VIN1")
    coordinator = MockCoordinator([vehicle])

    number_entity = ZeekrConfigNumber(
        coordinator,
        vehicle.vin,
        "seat_op",
        "Seat Operation",
        "seat",
        15,
    )
    number_entity.hass = DummyHass()

    # A value stored under the old 0-15 range is clamped into the new 1-60 range
    with patch.object(
        BaseCoordinatorEntity, "async_added_to_hass", AsyncMock()
    ), patch.object(
        ZeekrConfigNumber,
        "async_get_last_number_data",
        AsyncMock(return_value=SimpleNamespace(native_value=restored)),
    ):
        await number_entity.async_added_to_hass()

    assert number_entity.native_value == expected
    assert coordinator.operation_durations[vehicle.vin]["seat"] == expected


@pytest.mark.asyncio
async def test_config_numbers_are_created_per_vehicle(hass, mock_config_entry):
    coordinator = MockCoordinator([MockVehicle("VIN2"), MockVehicle("VIN1")])
    hass.data[DOMAIN] = {mock_config_entry.entry_id: coordinator}
    async_add_entities = MagicMock()

    with patch(
        "custom_components.zeekr_ev.number._migrate_legacy_config_numbers",
        return_value={"seat": 12},
    ) as migrate:
        await async_setup_entry(hass, mock_config_entry, async_add_entities)

    # The lowest VIN inherits the legacy entities; every vehicle is seeded with their values
    migrate.assert_called_once_with(hass, mock_config_entry.entry_id, "VIN1")
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


@pytest.mark.asyncio
async def test_config_numbers_setup_without_legacy_entities(hass, mock_config_entry):
    coordinator = MockCoordinator([MockVehicle("VIN1")])
    hass.data[DOMAIN] = {mock_config_entry.entry_id: coordinator}
    async_add_entities = MagicMock()

    # Unpatched: the registry seeded by conftest reports no legacy entities
    await async_setup_entry(hass, mock_config_entry, async_add_entities)

    registry = hass.data[er.DATA_REGISTRY]
    registry.async_get_entity_id.assert_any_call(
        Platform.NUMBER, DOMAIN, f"{mock_config_entry.entry_id}_seat_operation_duration"
    )
    registry.async_update_entity.assert_not_called()
    config_numbers = [
        entity
        for entity in async_add_entities.call_args.args[0]
        if isinstance(entity, ZeekrConfigNumber)
    ]
    assert {(entity.unique_id, entity.native_value) for entity in config_numbers} == {
        ("VIN1_seat_operation_duration", 15),
        ("VIN1_ac_operation_duration", 15),
        ("VIN1_steering_wheel_heat_duration", 8),
    }


@pytest.mark.asyncio
async def test_config_numbers_setup_without_vehicles(hass, mock_config_entry):
    coordinator = MockCoordinator([])
    hass.data[DOMAIN] = {mock_config_entry.entry_id: coordinator}
    async_add_entities = MagicMock()

    with patch(
        "custom_components.zeekr_ev.number._migrate_legacy_config_numbers"
    ) as migrate:
        await async_setup_entry(hass, mock_config_entry, async_add_entities)

    # Nothing to migrate or create for an account without cars
    migrate.assert_not_called()
    assert not any(
        isinstance(entity, ZeekrConfigNumber)
        for entity in async_add_entities.call_args.args[0]
    )


def _stored_state(native_value):
    """Mimic the RestoreNumber StoredState of a legacy duration entity.

    Only ``extra_data`` is modelled: the migration never reads ``.state``, because
    RestoreNumber has always stored ``native_value`` in extra_data.
    """
    return SimpleNamespace(
        extra_data=SimpleNamespace(as_dict=lambda: {"native_value": native_value}),
    )


def _legacy_seat_registry(hass):
    """Make the seeded registry report one legacy seat duration entity."""
    registry = hass.data[er.DATA_REGISTRY]
    registry.async_get_entity_id.side_effect = lambda _domain, _platform, unique_id: (
        "number.seat_operation_duration"
        if unique_id == "test_entry_seat_operation_duration"
        else None
    )
    return registry


@pytest.mark.parametrize(
    ("stored_state", "expected"),
    [
        (_stored_state(12), {"seat": 12}),
        (_stored_state(0), {"seat": 1}),
        (_stored_state(999), {"seat": 60}),
        (_stored_state("abc"), {}),
        # A live state is never read on its own (see _stored_state)
        (SimpleNamespace(state=SimpleNamespace(state="7"), extra_data=None), {}),
        (None, {}),
    ],
    ids=[
        "value",
        "clamped-to-min",
        "clamped-to-max",
        "non-numeric",
        "no-extra-data",
        "no-restore-data",
    ],
)
def test_migrate_legacy_config_numbers(hass, stored_state, expected):
    registry = _legacy_seat_registry(hass)
    if stored_state is not None:
        last_states = hass.data[restore_state.DATA_RESTORE_STATE].last_states
        last_states["number.seat_operation_duration"] = stored_state

    values = _migrate_legacy_config_numbers(hass, "test_entry", "VIN1")

    assert values == expected
    # The legacy entity keeps its entity_id and history under the new unique_id
    registry.async_update_entity.assert_called_once_with(
        "number.seat_operation_duration",
        new_unique_id="VIN1_seat_operation_duration",
    )


def test_migrate_legacy_config_numbers_already_migrated(hass):
    registry = hass.data[er.DATA_REGISTRY]
    registry.async_get_entity_id.return_value = "number.seat_operation_duration"

    values = _migrate_legacy_config_numbers(hass, "test_entry", "VIN1")

    # The per-vehicle unique_ids already exist, so nothing is renamed or carried over
    assert values == {}
    registry.async_update_entity.assert_not_called()


def test_migrate_legacy_config_numbers_without_legacy_entities(hass):
    registry = hass.data[er.DATA_REGISTRY]

    values = _migrate_legacy_config_numbers(hass, "test_entry", "VIN1")

    assert values == {}
    registry.async_update_entity.assert_not_called()
