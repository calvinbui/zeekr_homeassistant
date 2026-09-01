from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.zeekr_ev import select as zeekr_select


def create_seat_select(remote_result=True):
    vehicle = MagicMock(vin="VIN1")
    vehicle.do_remote_control.return_value = remote_result
    coordinator = SimpleNamespace(
        data={
            vehicle.vin: {"additionalVehicleStatus": {"climateStatus": {"drvHeatSts": 0}}}
        },
        operation_durations={vehicle.vin: {"seat": 20}},
        async_inc_invoke=AsyncMock(),
        async_request_refresh=AsyncMock(),
        get_vehicle_by_vin=lambda vin: vehicle if vin == vehicle.vin else None,
    )
    entity = zeekr_select.ZeekrSeatSelect(
        coordinator,
        vehicle.vin,
        "seat_heat_driver",
        "Driver Seat Heat",
        "SH.11",
        "heat",
        ["drvHeatSts"],
    )
    entity.hass = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=lambda func, *args: func(*args)),
    )
    entity.async_write_ha_state = MagicMock()
    return entity, coordinator, vehicle


@pytest.mark.asyncio
async def test_seat_select_command_lifecycle():
    entity, coordinator, vehicle = create_seat_select()

    await entity.async_select_option(zeekr_select.OPTION_LEVEL_2)

    vehicle.do_remote_control.assert_called_once_with(
        "start",
        "ZAF",
        {
            "serviceParameters": [
                {"key": "SH.11", "value": "true"},
                {"key": "SH.11.level", "value": "2"},
                {"key": "SH.11.duration", "value": "20"},
            ]
        },
    )
    coordinator.async_request_refresh.assert_awaited_once()

    rejected, rejected_coordinator, _ = create_seat_select(remote_result=False)

    with pytest.raises(HomeAssistantError, match="Failed to set Driver Seat Heat"):
        await rejected.async_select_option(zeekr_select.OPTION_LEVEL_2)

    assert rejected.current_option == zeekr_select.OPTION_OFF
    rejected_coordinator.async_request_refresh.assert_not_awaited()
