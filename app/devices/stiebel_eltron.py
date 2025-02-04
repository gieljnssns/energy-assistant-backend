"""Stiebel Eltron device implementation."""
from app import Optimizer

from . import (
    DeferrableLoadInfo,
    PowerModes,
    SessionStorage,
    State,
    StateId,
    StatesRepository,
)
from .analysis import DataBuffer
from .config import get_config_param
from .device import Device, DeviceWithState
from .homeassistant import HOMEASSISTANT_CHANNEL, assign_if_available

STIEBEL_ELTRON_POWER = 5000
POWER_HYSTERESIS = 0.1

def numeric_value(value: str | None) -> float | None:
    """Convert into a number."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None



class StiebelEltronDevice(Device, DeviceWithState):
    """Stiebel Eltron heatpump. This can be either a water heating part or a heating part."""

    def __init__(self, config: dict, session_storage: SessionStorage):
        """Create a Stiebel Eltron heatpump."""
        super().__init__(config, session_storage)
        self.grid_exported_power_data = DataBuffer()
        self._consumed_energy_today: State | None = None
        self._consumed_energy_today_entity_id: str = get_config_param(
            config, "energy_today")
        self._actual_temp_entity_id: str = get_config_param(
            config, "temperature")
        self._actual_temp: State | None = None
        self._state: State | None = None


        self._comfort_target_temperature_entity_id = config.get("comfort_target_temperature")
        self._target_temperature_normal: float | None = numeric_value(config.get("target_temperature_normal"))
        self._target_temperature_pv: float | None = numeric_value(config.get("target_temperatrure_pv"))
        if self._target_temperature_normal is not None and self._target_temperature_pv is not None and self._comfort_target_temperature_entity_id is not None:
            self._supported_power_modes.append(PowerModes.PV)
            self.supported_power_modes.append(PowerModes.OPTIMIZED)

        self._state_entity_id: str = get_config_param(config, "state")
        self._consumed_energy_entity_id: str = get_config_param(
            config, "energy_total")
        self._consumed_energy: State | None = None
        self._icon = "mdi-heat-pump"

    async def update_state(self, state_repository:StatesRepository, self_sufficiency: float) -> None:
        """Update the state of the Stiebel Eltron device."""
        old_state = self.state == 'on'
        self._state = assign_if_available(
            self._state, state_repository.get_state(self._state_entity_id))
        new_state = self.state == 'on'

        self._consumed_energy_today = assign_if_available(
            self._consumed_energy_today, state_repository.get_state(self._consumed_energy_today_entity_id))
        self._consumed_energy = assign_if_available(
            self._consumed_energy, state_repository.get_state(self._consumed_energy_entity_id))
        self._consumed_solar_energy.add_measurement(
            self.consumed_energy, self_sufficiency)
        self._actual_temp = assign_if_available(
            self._actual_temp, state_repository.get_state(self._actual_temp_entity_id))

        if self._energy_snapshot is None:
            self.set_snapshot(self.consumed_solar_energy, self.consumed_energy)

        await super().update_session(old_state, new_state, "Water heater")


    async def update_power_consumption(self, state_repository: StatesRepository, optimizer: Optimizer, grid_exported_power: float) -> None:
        """"Update the device based on the current pv availablity."""
        self.grid_exported_power_data.add_data_point(grid_exported_power)
        if self._target_temperature_normal is not None and self._target_temperature_pv is not None and self._comfort_target_temperature_entity_id is not None:
            current_target_temperature = state_repository.get_state(self._comfort_target_temperature_entity_id)
            if current_target_temperature is not None:
                target_temperature : float = current_target_temperature.numeric_value
                if self.power_mode == PowerModes.PV :
                    if self.state == 'off':
                        avg_300 = self.grid_exported_power_data.get_average_for(300)
                        if avg_300 > self.requested_additional_power * (1 + POWER_HYSTERESIS):
                            target_temperature = self._target_temperature_pv
                        elif avg_300 < self.requested_additional_power * (1 - POWER_HYSTERESIS):
                            target_temperature = self._target_temperature_normal
                elif self.power_mode == PowerModes.OPTIMIZED:
                    target_temperature = self._get_temperature_for_optimized(optimizer, target_temperature)
                if target_temperature != current_target_temperature.numeric_value:
                    state_repository.set_state(StateId(id=self._comfort_target_temperature_entity_id, channel=HOMEASSISTANT_CHANNEL), str(target_temperature))


    def _get_temperature_for_optimized(self, optimizer: Optimizer,  current_target_temperature:float) -> float:
        if self.state == 'off' and self._target_temperature_normal is not None and self._target_temperature_pv is not None:
            power = optimizer.get_optimized_power(self._id)
            if power > 0:
                return self._target_temperature_pv
            else:
                return self._target_temperature_normal
        else:
            return current_target_temperature

    def get_deferrable_load_info(self) -> DeferrableLoadInfo | None:
        """Get the current deferrable load info."""
        if self.power_mode == PowerModes.OPTIMIZED:
            return DeferrableLoadInfo(device_id=self.id, nominal_power=STIEBEL_ELTRON_POWER, deferrable_hours=1, is_continous=False)
        return None


    @property
    def requested_additional_power(self) -> float:
        """How much power the device could consume in pv mode."""
        # TODO: Consider the actual temperature
        return STIEBEL_ELTRON_POWER if self.state == 'off' else 0.0

    @property
    def consumed_energy(self) -> float:
        """Consumed energy in kWh."""
        energy = self._consumed_energy.numeric_value if self._consumed_energy else 0.0
        energy_today = self._consumed_energy_today.numeric_value if self._consumed_energy_today else 0.0
        return energy + energy_today

    @property
    def state(self) -> str:
        """The state of the device. The state is `on` in case the device is heating."""
        return self._state.value if self._state and self._state.value else "unknown"

    @property
    def power(self) -> float:
        """Current power consumption of the device."""
        if self._state is not None:
            return STIEBEL_ELTRON_POWER if self._state.value == 'on' else 0.0
        else:
            return 0.0

    @property
    def actual_temperature(self) -> float:
        """The actual temperature of the heating or water."""
        return self._actual_temp.numeric_value if self._actual_temp else 0.0

    @property
    def icon(self) -> str:
        """Icon of the device."""
        return self._icon

    @property
    def available(self) -> bool:
        """Is the device available?."""
        return self._consumed_energy is not None and self._consumed_energy.available and self._consumed_energy_today is not None and self._consumed_energy_today.available and self._actual_temp is not None and self._actual_temp.available and self._state is not None and self._state.available

    def restore_state(self, consumed_solar_energy: float, consumed_energy: float) -> None:
        """Restore the previously stored state."""
        super().restore_state(consumed_solar_energy, consumed_energy)
        self._consumed_energy = State(
            self._consumed_energy_entity_id, str(consumed_energy))

    @property
    def attributes(self) -> dict[str, str]:
        """Get the attributes of the device for the UI."""
        result : dict[str, str]= {
            "state": self.state,
            "actual_temperature": f"{self.actual_temperature} °C"
        }
        return result
