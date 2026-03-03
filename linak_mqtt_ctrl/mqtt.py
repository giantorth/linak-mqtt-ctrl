"""MQTT client for Home Assistant integration with Linak desks."""

import json
import asyncio

from gmqtt import Client as GMQTTClient

from linak_mqtt_ctrl.logger import LOG
from linak_mqtt_ctrl.config import load_config, save_config
from linak_mqtt_ctrl.constants import (
    __version__,
    DESK_MAX_HEIGHT, DESK_MIN_TRAVEL,
    MOVE_UP, MOVE_DOWN, MOVE_STOP,
    ENTITY_ID, STATE_TOPIC, COMMAND_TOPIC, AVAILABILITY_TOPIC,
    LOCK_COMMAND_TOPIC, LOCK_STATE_TOPIC,
    PRESET_TOPIC_PREFIX, MIN_TRAVEL_STATE_TOPIC, MIN_TRAVEL_COMMAND_TOPIC,
    HA_STATUS_TOPIC, HA_DISCOVERY_TOPIC_PREFIX,
)


class AsyncMQTTClient:
    """An asyncio-native MQTT client built on top of gmqtt."""

    def __init__(self, broker, port, device_name, async_device, username=None, password=None):
        LOG.info("Initializing AsyncMQTTClient with broker: %s, port: %s", broker, port)
        self.device_name = device_name
        self.device_manufacturer = "Linak"
        self.device_model = "USB2LIN06"
        self.broker = broker
        self.port = port
        self.async_device = async_device
        self.payload_open = "OPEN"
        self.payload_stop = "STOP"
        self.payload_close = "CLOSE"

        self.last_state = None
        self.force_publish = True

        self.locked = False
        self.reconnecting = False

        self.client = GMQTTClient(ENTITY_ID)
        self.client.set_config({'reconnect_retries': -1, 'reconnect_delay': 1})

        if username and password:
            self.client.set_auth_credentials(username, password)
            LOG.info("Using MQTT credentials: username=%s", username)

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_subscribe = self.on_subscribe
        self.client.on_log = self.on_log

        # Load configuration once at startup.
        self.config = load_config()

        self._button_repeat_task = None

    def on_log(self, client, level, buf):
        """Log MQTT events, suppressing frequent ping messages to debug level."""
        if "PINGREQ" in buf or "PINGRESP" in buf:
            LOG.debug("MQTT PING event: %s", buf)
        else:
            LOG.info(buf)

    async def connect(self):
        """Connect to the MQTT broker."""
        try:
            await self.client.connect(self.broker, self.port, keepalive=60)
        except Exception as e:
            LOG.error("Failed to connect to MQTT broker: %s", e)
            asyncio.create_task(self.reconnect())

    def on_connect(self, client, flags, rc, properties):
        """Handle MQTT connection/reconnection.

        Publishes autodiscovery and state, subscribes to all command topics,
        and listens for the Home Assistant birth message to republish state.
        """
        LOG.info("Connected to MQTT broker with result code: %s, flags: %s, properties: %s", rc, flags, properties)
        asyncio.create_task(self.publish_discovery_and_state())
        client.subscribe(COMMAND_TOPIC)
        client.subscribe(LOCK_COMMAND_TOPIC)
        client.subscribe(f"{PRESET_TOPIC_PREFIX}/+/go")
        client.subscribe(f"{PRESET_TOPIC_PREFIX}/+/set")
        client.subscribe(MIN_TRAVEL_COMMAND_TOPIC)
        client.subscribe(HA_STATUS_TOPIC)
        asyncio.create_task(self.publish_availability("online"))
        self.force_publish = True

    async def on_message(self, client, topic, payload, qos, properties):
        """Process incoming MQTT messages for movement, lock, presets, and HA birth."""
        decoded_payload = payload.decode() if isinstance(payload, bytes) else payload
        LOG.info("Received message on topic %s: %s", topic, decoded_payload)

        if topic == HA_STATUS_TOPIC:
            if decoded_payload.lower() == "online":
                LOG.info("Home Assistant birth message received; publishing all state.")
                await self.publish_all_state()
            return

        if topic == COMMAND_TOPIC:
            if self.locked:
                LOG.warning("Movement command ignored because desk is locked.")
                return

            if isinstance(decoded_payload, (int, float)):
                command_value = decoded_payload
            else:
                try:
                    command_value = int(decoded_payload)
                except ValueError:
                    command_value = decoded_payload

            LOG.info("Processing movement command: %s", command_value)

            if command_value == self.payload_open:
                self.async_device._stop_requested = False
                if self._button_repeat_task:
                    self._button_repeat_task.cancel()
                self._button_repeat_task = asyncio.create_task(self._repeat_button(MOVE_UP))
            elif command_value == self.payload_close:
                self.async_device._stop_requested = False
                if self._button_repeat_task:
                    self._button_repeat_task.cancel()
                self._button_repeat_task = asyncio.create_task(self._repeat_button(MOVE_DOWN))
            elif command_value == self.payload_stop:
                if self._button_repeat_task:
                    self._button_repeat_task.cancel()
                    self._button_repeat_task = None
                asyncio.create_task(self.async_device.stop_movement())
            elif isinstance(command_value, int):
                position = self.percent_to_position(command_value)
                asyncio.create_task(self.async_device.move(position))
            else:
                LOG.error("Invalid position value received: %s", decoded_payload)
                return

            LOG.info("Processed movement command: %s", command_value)

        elif topic == LOCK_COMMAND_TOPIC:
            if decoded_payload == "LOCK":
                self.locked = True
                LOG.info("Desk locked: Movement commands will be disabled.")
            elif decoded_payload == "UNLOCK":
                self.locked = False
                LOG.info("Desk unlocked: Movement commands are now enabled.")
            else:
                LOG.error("Invalid lock command received: %s", decoded_payload)
                return

            lock_state = "LOCKED" if self.locked else "UNLOCKED"
            self.client.publish(LOCK_STATE_TOPIC, lock_state, qos=1)
            LOG.info("Published lock state: %s", lock_state)

        elif topic.startswith(f"{PRESET_TOPIC_PREFIX}/"):
            parts = topic.split('/')
            if len(parts) < 5:
                LOG.error("Invalid preset command topic format: %s", topic)
                return
            preset_number = parts[3]
            command = parts[4]
            if preset_number not in ["1", "2", "3", "4"]:
                LOG.error("Invalid preset number: %s", preset_number)
                return
            if command == "set":
                await self.set_preset(preset_number)
            elif command == "go":
                await self.go_to_preset(preset_number)
            else:
                LOG.error("Unknown preset command: %s", command)

        elif topic == MIN_TRAVEL_COMMAND_TOPIC:
            try:
                new_min = int(decoded_payload)
                self.async_device.min_travel = new_min
                LOG.info("Updated desk_min_travel to: %s", new_min)
                self.config["desk_min_travel"] = new_min
                save_config(self.config)
                self.client.publish(MIN_TRAVEL_STATE_TOPIC, str(new_min), qos=1)
            except ValueError:
                LOG.error("Invalid desk_min_travel value received: %s", decoded_payload)
                return

        else:
            LOG.warning("Received message on an unrecognized topic: %s", topic)

    async def _repeat_button(self, move_command):
        """Repeatedly send a move command every 0.3s until cancelled or limits reached."""
        report = await self.async_device.get_position()
        min_travel = self.config.get("desk_min_travel", DESK_MIN_TRAVEL)
        if report.position <= min_travel and move_command == MOVE_DOWN:
            LOG.info("Desk is already at the minimum travel limit (%s). Cancelling repeating move command.", report.position)
            return
        try:
            while not self.async_device._stop_requested:
                await self.async_device._move(move_command)
                report = await self.async_device.get_position()
                if (report.position <= min_travel and move_command == MOVE_DOWN) or \
                   (report.position >= DESK_MAX_HEIGHT and move_command == MOVE_UP):
                    LOG.info("Desk reached limit (%s). Cancelling repeating move command.", report.position)
                    break
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            LOG.info("Repeating move command cancelled.")
            raise

    def on_subscribe(self, client, mid, qos, properties):
        """Log successful subscription."""
        LOG.info("Subscribed to topic with mid: %s", mid)

    async def publish_discovery(self):
        """Publish a combined MQTT discovery payload for Home Assistant.

        Contains definitions for the cover (desk), lock, preset buttons,
        height sensor, and min-travel number entity.
        """
        device_id = self.device_name.replace(" ", "_").lower()

        components = {
            "standing_desk": {
                "platform": "cover",
                "device_class": "damper",
                "name": self.device_name,
                "unique_id": ENTITY_ID,
                "state_topic": STATE_TOPIC,
                "command_topic": COMMAND_TOPIC,
                "state_open": "Open",
                "state_closed": "Closed",
                "value_template": "{% if value_json.position > 0 %}Open{% else %}Closed{% endif %}",
                "position_open": 100,
                "position_closed": 0,
                "position_topic": STATE_TOPIC,
                "position_template": "{{ value_json.position }}",
                "payload_open": self.payload_open,
                "payload_stop": self.payload_stop,
                "payload_close": self.payload_close,
                "set_position_topic": COMMAND_TOPIC,
                "set_position_template": "{{ value }}",
                "json_attributes_topic": STATE_TOPIC,
                "icon": "mdi:table-furniture"
            },
            "desk_lock": {
                "platform": "lock",
                "name": "Desk Lock",
                "unique_id": f"{ENTITY_ID}_lock",
                "command_topic": LOCK_COMMAND_TOPIC,
                "state_topic": LOCK_STATE_TOPIC,
                "payload_lock": "LOCK",
                "payload_unlock": "UNLOCK",
                "state_locked": "LOCKED",
                "state_unlocked": "UNLOCKED"
            },
            "desk_height": {
                "platform": "sensor",
                "name": "Desk Height",
                "unique_id": f"{ENTITY_ID}_height",
                "state_topic": STATE_TOPIC,
                "value_template": "{{ value_json.height }}",
                "unit_of_measurement": "in",
                "icon": "mdi:arrow-expand-vertical"
            },
            "desk_min_travel": {
                "platform": "number",
                "name": "Desk Min Travel",
                "unique_id": f"{ENTITY_ID}_min_travel",
                "state_topic": MIN_TRAVEL_STATE_TOPIC,
                "command_topic": MIN_TRAVEL_COMMAND_TOPIC,
                "min": 0,
                "max": DESK_MAX_HEIGHT,
                "step": 1,
                "unit_of_measurement": "raw",
                "icon": "mdi:arrow-collapse-down",
                "entity_category": "config"
            }
        }

        # Generate preset buttons in a loop instead of repeating 8 times.
        for i in range(1, 5):
            components[f"preset_button_{i}"] = {
                "platform": "button",
                "name": f"Preset {i}",
                "unique_id": f"{ENTITY_ID}_preset{i}",
                "command_topic": f"{PRESET_TOPIC_PREFIX}/{i}/go"
            }
            components[f"set_preset_button_{i}"] = {
                "platform": "button",
                "name": f"Set Preset {i}",
                "unique_id": f"{ENTITY_ID}_set_preset{i}",
                "command_topic": f"{PRESET_TOPIC_PREFIX}/{i}/set",
                "entity_category": "config"
            }

        discovery_payload = {
            "device": {
                "identifiers": [device_id],
                "name": self.device_manufacturer + " Desk",
                "manufacturer": self.device_manufacturer,
                "model": self.device_model,
                "sw_version": __version__
            },
            "origin": {
                "name": self.device_manufacturer,
                "sw_version": __version__,
                "support_url": "https://github.com/giantorth/linak-mqtt-ctrl"
            },
            "availability": {
                "topic": AVAILABILITY_TOPIC,
            },
            "components": components,
            "qos": 1
        }

        topic = f"{HA_DISCOVERY_TOPIC_PREFIX}/{device_id}/config"
        payload_json = json.dumps(discovery_payload)
        LOG.info("MQTT Publishing discovery payload: %s", discovery_payload)
        self.client.publish(topic, payload_json, qos=1, retain=True)

    async def publish_all_state(self):
        """Publish all device states (desk position, lock, min travel).

        Called after autodiscovery to ensure Home Assistant has current values.
        """
        await asyncio.sleep(0.5)

        try:
            report = await self.async_device.get_position()
            position_percent = self.position_to_percent(report.position)
            state_payload = {
                "position": position_percent,
                "raw_position": report.position,
                "moving": report.moving,
                "height": report.position_in_in,
                "height_cm": report.position_in_cm
            }
            LOG.info("Publishing desk state: %s", state_payload)
            await self.publish_state(state_payload)
        except Exception as e:
            LOG.error("Error retrieving desk state: %s", e)

        lock_state = "LOCKED" if self.locked else "UNLOCKED"
        LOG.info("Publishing lock state: %s", lock_state)
        self.client.publish(LOCK_STATE_TOPIC, lock_state, qos=1)

        min_travel = str(self.async_device.min_travel)
        LOG.info("Publishing desk min travel state: %s", min_travel)
        self.client.publish(MIN_TRAVEL_STATE_TOPIC, min_travel, qos=1)

    async def publish_discovery_and_state(self):
        """Publish autodiscovery payload followed by all device state."""
        await self.publish_discovery()
        await self.publish_all_state()

    async def publish_availability(self, payload):
        """Publish an availability message ('online' or 'offline')."""
        LOG.info("MQTT Publishing availability payload: %s", payload)
        self.client.publish(AVAILABILITY_TOPIC, payload, qos=1, retain=True)

    async def publish_state(self, state_payload):
        """Publish a state payload to the MQTT state topic."""
        payload_json = json.dumps(state_payload)
        LOG.info("MQTT Publishing state: %s", state_payload)
        self.client.publish(STATE_TOPIC, payload_json, qos=1)

    def percent_to_position(self, percent):
        """Convert a percentage (0-100) to a raw position value."""
        return int((percent / 100) * DESK_MAX_HEIGHT)

    def position_to_percent(self, position):
        """Convert a raw position value to a percentage (0-100)."""
        return int((position / DESK_MAX_HEIGHT) * 100)

    async def disconnect(self):
        """Publish offline availability and disconnect."""
        await self.publish_availability("offline")
        await self.client.disconnect()

    async def cleanup(self):
        """Cancel any active repeating move task for graceful shutdown."""
        if self._button_repeat_task:
            self._button_repeat_task.cancel()
            try:
                await self._button_repeat_task
            except asyncio.CancelledError:
                LOG.info("Button repeat task cancelled during cleanup.")

    async def set_preset(self, preset_number):
        """Record the current desk position as a preset in config."""
        try:
            report = await self.async_device.get_position()
            current_position = report.position
            LOG.info("Current position retrieved for preset %s: %s", preset_number, current_position)
            self.config[f"preset{preset_number}"] = current_position
            await asyncio.to_thread(save_config, self.config)
            LOG.info("Preset %s set to position %s and saved to config", preset_number, current_position)
        except Exception as e:
            LOG.error("Error setting preset %s: %s", preset_number, e)

    async def go_to_preset(self, preset_number):
        """Move the desk to a saved preset position."""
        if self.locked:
            LOG.warning("Preset movement command ignored because desk is locked.")
            return
        try:
            preset_key = f"preset{preset_number}"
            if preset_key not in self.config:
                LOG.error("Preset %s not found in configuration.", preset_number)
                return
            target_position = self.config[preset_key]
            LOG.info("Moving desk to preset %s position: %s", preset_number, target_position)
            asyncio.create_task(self.async_device.move(target_position))
        except Exception as e:
            LOG.error("Error executing preset %s command: %s", preset_number, e)
