from datetime import datetime

import requests

from . import Device, EnergyIntegrator, EnergySnapshot
from .mqtt import MqttDevice


class State:
    def __init__(self, entity_id:str, state:str, attributes:dict):
        try:
            self._state = float(state)
        except ValueError:
            self._state = state
        self._attributes = attributes
        self._entity_id = entity_id

    @property
    def state(self):
        return self._state

    @property
    def unit(self) -> str:
        if self._attributes is not None:
            return self._attributes.get("unit_of_measurement")
        return None

    @property
    def name(self) -> str:
        if self._attributes is not None:
            return self._attributes.get("friendly_name")
        return self._entity_id

    @property
    def entity_id(self) -> str:
        return self._entity_id


class Homeassistant:
    def __init__(self, url, token):
        self._url = url
        self._states = {}
        self._token = token


    def update_states(self):
        headers = {
            "Authorization": f"Bearer {self._token}",
            "content-type": "application/json",
        }
        time_stamp = datetime.now().timestamp()
        response = requests.get(
            f"{self._url}/api/states", headers=headers)
        datetime.now().timestamp() - time_stamp
        if response.ok:
            states = response.json()
            self._states = {}
            for state in states:
                entity_id = state.get("entity_id")
                self._states[entity_id] = State(entity_id, state.get("state"), state.get("attributes"))

    def get_state(self, entity_id) -> State:
        return self._states.get(entity_id)


class HomeassistantDevice(Device):
    def __init__(self, name, power_entity_id, consumed_energy_entity_id):
        super().__init__(name)
        self._power_entity_id = power_entity_id
        self._consumed_energy_entity_id = consumed_energy_entity_id
        self._power = None
        self._consumed_energy = None


    def update_state(self, hass:Homeassistant, self_sufficiency: float):
        self._power = hass.get_state(self._power_entity_id)
        self._consumed_energy = hass.get_state(self._consumed_energy_entity_id)
        self._consumed_solar_energy.add_measurement(self.consumed_energy, self_sufficiency)


    @property
    def icon(self):
        return "mdi:mdi-car-electric"

    @property
    def power(self):
        """The current power used by the device."""
        return self._power.state if self._power is not None else 0.0



STIEBEL_ELTRON_POWER = 5000
class StiebelEltronDevice(HomeassistantDevice):
    def __init__(self, name, state_entity_id, consumed_energy_entity_id, consumed_energy_today_entity_id, actual_temp_entity_id):
        super().__init__(name, state_entity_id, consumed_energy_entity_id)
        self._consumed_energy_today = None
        self._consumed_energy_today_entity_id = consumed_energy_today_entity_id
        self._actual_temp_entity_id = actual_temp_entity_id
        self._actual_temp = None
        self._state = None

    def update_state(self, hass:Homeassistant, self_sufficiency: float):
        self._state = hass.get_state(self._power_entity_id)
        self._consumed_energy_today = hass.get_state(self._consumed_energy_today_entity_id)
        self._consumed_energy = hass.get_state(self._consumed_energy_entity_id)
        self._consumed_solar_energy.add_measurement(self.consumed_energy, self_sufficiency)
        self._actual_temp = hass.get_state(self._actual_temp_entity_id)

    @property
    def consumed_energy(self):
        """Consumed energy in kWh."""
        energy =  self._consumed_energy.state if self._consumed_energy is not None else 0.0
        energy_today =  self._consumed_energy_today.state if self._consumed_energy_today is not None else 0.0
        return energy + energy_today


    @property
    def power(self):
        if self._state is not None:
            return STIEBEL_ELTRON_POWER if self._state.state == 'on' else 0.0
        else:
            return 0.0

    @property
    def actual_temperature(self):
        return self._actual_temp.state if self._actual_temp is not None else 0.0

    @property
    def icon(self):
        return "mdi:mdi-heat-pump"


class Home:
    def __init__(self, name, solar_power_entity_id, grid_supply_power_entity_id, solar_energy_entity_id, grid_import_energy_entity_id, grid_export_energy_entity_id):
        self._name = name
        self._solar_power_entity_id = solar_power_entity_id
        self._grid_supply_power_entity_id = grid_supply_power_entity_id
        self._solar_energy_entity_id = solar_energy_entity_id
        self._grid_import_energy_entity_id = grid_import_energy_entity_id
        self._grid_export_energy_entity_id = grid_export_energy_entity_id

        self._solar_production_power = None
        self._grid_supply_power = None
        self._consumed_energy = 0
        self._last_consumed_solar_energy = None
        self._consumed_solar_energy = EnergyIntegrator()
        self._energy_snapshop = None
        self.devices = []


    def add_device(self, device):
        self.devices.append(device)

    @property
    def name(self) -> str:
        return self._name

    @property
    def solar_production_energy(self):
        """Solar energy in kWh."""
        return self._solar_production_energy.state if self._solar_production_energy is not None else 0.0

    @property
    def consumed_energy(self):
        """Consumed energy in kWh."""
        return self._consumed_energy

    @property
    def consumed_solar_energy(self):
        """Consumed solar energy in kWh."""
        return self._consumed_solar_energy.consumed_solar_energy

    @property
    def home_consumption_power(self):
        result = self.solar_production_power - self.grid_supply_power
        if result > 0:
            return result
        else:
            return 0

    @property
    def solar_self_consumption_power(self):
        if self.grid_supply_power < 0:
            return self.solar_production_power
        else:
            return self.solar_production_power - self.grid_supply_power


    @property
    def self_sufficiency(self):
        hc = self.home_consumption_power
        if hc > 0:
            return self.solar_self_consumption_power / hc
        else:
            return 0


    def update_state_from_hass(self, hass:Homeassistant):
        self._solar_production_power = hass.get_state(self._solar_power_entity_id)
        self._grid_supply_power = hass.get_state(self._grid_supply_power_entity_id)


        self._solar_production_energy = hass.get_state(self._solar_energy_entity_id)
        self._grid_import_energy = hass.get_state(self._grid_import_energy_entity_id)
        self._grid_export_energy = hass.get_state(self._grid_export_energy_entity_id)
        self._consumed_energy = self._grid_import_energy.state - self._grid_export_energy.state +  self._solar_production_energy.state
        self._consumed_solar_energy.add_measurement(self._consumed_energy, self.self_sufficiency)

        for device in self.devices:
            if isinstance(device, HomeassistantDevice):
                device.update_state(hass, self.self_sufficiency)


    def update_state_from_mqtt(self, topic: str, message: str):
        for device in self.devices:
            if isinstance(device, MqttDevice):
                device.update_state(topic, message, self.self_sufficiency)


    def mqtt_topics(self):
        result = [self._solar_power_entity_id, self._grid_supply_power_entity_id]
        for device in self.devices:
            if isinstance(device, MqttDevice):
                result.append(device.mqtt_topic)
        return result

    @property
    def icon(self):
        return "mdi:mdi-home"

    @property
    def solar_production_power(self)-> float:
        return self._solar_production_power.state if self._solar_production_power is not None else 0.0

    @property
    def grid_supply_power(self)-> float:
        return self._grid_supply_power.state if self._grid_supply_power is not None else 0.0

    def restore_state(self, consumed_solar_energy, consumed_energy):
        self._consumed_solar_energy.restore_state(consumed_solar_energy)
        self.set_snapshot(consumed_solar_energy, consumed_energy)

    def set_snapshot(self, consumed_solar_energy, consumed_energy):
        self._energy_snapshop = EnergySnapshot(consumed_solar_energy, consumed_energy)

    def store_energy_snapshot(self):
        """Stores the current values in the snapshot."""
        self.set_snapshot(self.consumed_solar_energy, self.consumed_energy)
        for device in self.devices:
            device.store_energy_snapshot()

    @property
    def energy_snapshop(self):
        """The last energy snapshot of the device."""
        return self._energy_snapshop
