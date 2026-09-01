"""Number platform for Zeekr EV API Integration."""

from __future__ import annotations

import asyncio

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ZeekrCoordinator
from .entity import ZeekrEntity

CONFIG_NUMBERS = {
    "seat_operation_duration": ("Seat Operation Duration", "seat", 15),
    "ac_operation_duration": ("AC Operation Duration", "ac", 15),
    "steering_wheel_heat_duration": ("Steering Wheel Heat Duration", "wheel", 8),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the number platform."""
    coordinator: ZeekrCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[NumberEntity] = []

    legacy_values: dict[str, int] = {}
    if coordinator.vehicles:
        legacy_values = _migrate_legacy_config_numbers(
            hass,
            entry.entry_id,
            min(vehicle.vin for vehicle in coordinator.vehicles),
        )

    for vehicle in coordinator.vehicles:
        for key, (name, duration_key, default_value) in CONFIG_NUMBERS.items():
            entities.append(
                ZeekrConfigNumber(
                    coordinator,
                    vehicle.vin,
                    key,
                    name,
                    duration_key,
                    legacy_values.get(duration_key, default_value),
                )
            )
        entities.append(ZeekrChargingLimitNumber(coordinator, vehicle.vin))

    async_add_entities(entities)


def _migrate_legacy_config_numbers(
    hass: HomeAssistant, entry_id: str, vin: str
) -> dict[str, int]:
    """Migrate legacy IDs and return their values to seed every vehicle."""
    registry = er.async_get(hass)
    restored_states = restore_state.async_get(hass).last_states
    values: dict[str, int] = {}

    for key, (_name, duration_key, _default_value) in CONFIG_NUMBERS.items():
        new_unique_id = f"{vin}_{key}"
        if registry.async_get_entity_id(Platform.NUMBER, DOMAIN, new_unique_id):
            continue

        old_unique_id = f"{entry_id}_{key}"
        if entity_id := registry.async_get_entity_id(
            Platform.NUMBER, DOMAIN, old_unique_id
        ):
            stored_state = restored_states.get(entity_id)
            native_value = None
            if stored_state and (extra_data := stored_state.extra_data):
                native_value = extra_data.as_dict().get("native_value")

            state = stored_state.state if stored_state else hass.states.get(entity_id)
            if (
                native_value is None
                and state
                and state.state
                not in (
                    STATE_UNKNOWN,
                    STATE_UNAVAILABLE,
                )
            ):
                native_value = state.state

            if native_value is not None:
                try:
                    values[duration_key] = max(1, min(int(float(native_value)), 60))
                except (TypeError, ValueError):
                    pass
            registry.async_update_entity(entity_id, new_unique_id=new_unique_id)

    return values


class ZeekrConfigNumber(ZeekrEntity, RestoreNumber):
    """Zeekr Configuration Number class."""

    _attr_has_entity_name = True
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 1
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-outline"

    def __init__(
        self,
        coordinator: ZeekrCoordinator,
        vin: str,
        key: str,
        name: str,
        duration_key: str,
        default_value: int,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator, vin)
        self._duration_key = duration_key
        self._attr_name = name
        self._attr_unique_id = f"{vin}_{key}"
        durations = coordinator.operation_durations.setdefault(vin, {})
        self._attr_native_value = durations.setdefault(duration_key, default_value)

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_number_data()
        if last_state and last_state.native_value is not None:
            value = max(
                int(self.native_min_value),
                min(int(last_state.native_value), int(self.native_max_value)),
            )
            self._attr_native_value = value
            self.coordinator.operation_durations[self.vin][self._duration_key] = value

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        self._attr_native_value = value
        self.coordinator.operation_durations[self.vin][self._duration_key] = int(value)
        self.async_write_ha_state()


class ZeekrChargingLimitNumber(ZeekrEntity, RestoreNumber):
    """Zeekr Charging Limit Number class."""

    _attr_has_entity_name = True
    _attr_native_min_value = 50
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery-charging-high"

    def __init__(self, coordinator: ZeekrCoordinator, vin: str) -> None:
        """Initialize the charging limit number."""
        super().__init__(coordinator, vin)
        self._attr_name = "Charging Limit"
        self._attr_unique_id = f"{vin}_charging_limit"
        self._attr_native_value: float | None = None

    @property
    def native_value(self) -> float | None:
        """Return the value reported by the coordinator."""
        try:
            val = (
                self.coordinator.data.get(self.vin, {})
                .get("chargingLimit", {})
                .get("soc")
            )
            if val is not None:
                # API returns value * 10 (e.g. 800 -> 80.0)
                return float(val) / 10.0
        except (ValueError, TypeError, AttributeError):
            pass
        return self._attr_native_value

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_number_data()
        if last_state and last_state.native_value is not None:
            self._attr_native_value = last_state.native_value

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        vehicle = self.coordinator.get_vehicle_by_vin(self.vin)
        if not vehicle:
            return

        command = "start"
        service_id = "RCS"
        # API expects value * 10 (e.g. 80.2% -> 802)
        # We handle full integers, so 80% -> 800
        soc_value = int(value * 10)

        setting = {
            "serviceParameters": [
                {
                    "key": "soc",
                    "value": str(soc_value)
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

        await self.coordinator.async_inc_invoke()
        await self.hass.async_add_executor_job(
            vehicle.do_remote_control, command, service_id, setting
        )

        # Reflect the new target immediately (native_value prefers the
        # coordinator value, which would otherwise snap back to the old SoC),
        # then reconcile from the backend after a short delay — an immediate
        # poll can still return the previous SoC before the car applies it.
        self.coordinator.data.setdefault(self.vin, {}).setdefault("chargingLimit", {})[
            "soc"
        ] = soc_value
        self._attr_native_value = value
        self.async_write_ha_state()

        async def _reconcile() -> None:
            await asyncio.sleep(10)
            await self.coordinator.async_request_refresh()

        self.hass.async_create_task(_reconcile())
