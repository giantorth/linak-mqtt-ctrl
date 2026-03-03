"""linak-mqtt-ctrl: Control Linak desks via USB2LIN06 over MQTT."""

from linak_mqtt_ctrl.constants import __version__
from linak_mqtt_ctrl.cli import main

__all__ = ["__version__", "main"]
