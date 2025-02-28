import paho.mqtt.client as mqtt
import threading
import queue
import time
import json

from logger import Logger
from constants import HA_MIN_POS, HA_MAX_POS

LOG = Logger.get_logger(__name__)

class MqttService:
    def __init__(self, linak_device, server, port, username, password, client_id="linak_desk"):
        self.linak_device = linak_device
        self.client = mqtt.Client(client_id=client_id)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self.client.username_pw_set(username, password)
        self.server = server
        self.port = port
        self.connected = False
        self.desk_name = "Linak Desk"
        self.entity_id = "linak_desk"
        self.state_topic = f"linak/desk/{self.entity_id}/state"
        self.command_topic = f"linak/desk/{self.entity_id}/set"
        self.availability_topic = f"linak/desk/{self.entity_id}/availability"
        self.device_identifiers = ["linak_desk_controller"]
        self.device_manufacturer = "Linak"
        self.device_model = "USB2LIN06"
        self.command_queue = queue.Queue()
        self.thread = threading.Thread(target=self.process_commands)
        self.thread.daemon = True
        self.thread.start()
        self.last_reported_position = -1
        self.max_reconnect_attempts = 5  # Maximum reconnection attempts
        self.reconnect_delay = 1  # Initial reconnect delay (seconds)
        self.keepalive = 60 # Keep alive of 60 seconds
        self.last_ping = time.time()

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            LOG.info("Connected to MQTT Broker!")
            self.client.subscribe(self.command_topic)
            # send discovery and availability only on initial connection
            if not hasattr(self, 'initial_connection'):
                self.send_discovery()
                self.send_availability("online")
                self.initial_connection = True  # Mark initial connection as done
                self.publish_state()
            self.reconnect_delay = 1  # Reset delay on successful connection
            self.last_ping = time.time()
        else:
            LOG.error(f"Failed to connect, return code {rc}")

    def on_disconnect(self, client, userdata, rc):
        self.connected = False
        LOG.warning(f"Disconnected from MQTT Broker, return code: {rc}")
        if rc != 0:
            self.reconnect()

    def reconnect(self):
        reconnect_attempts = 0
        while reconnect_attempts < self.max_reconnect_attempts:
            try:
                LOG.info(f"Attempting to reconnect... (attempt {reconnect_attempts + 1})")
                self.client.reconnect()
                LOG.info("Reconnected to MQTT Broker.")
                self.send_availability("online")
                self.publish_state()
                self.reconnect_delay = 1  # Reset delay on successful connection
                self.last_ping = time.time()
                return  # Exit loop if reconnected
            except Exception as ex:
                LOG.error(f"Failed to reconnect: {ex}")
                reconnect_attempts += 1
                time.sleep(self.reconnect_delay)
                self.reconnect_delay *= 2  # Exponential backoff
        LOG.error("Max reconnection attempts reached.")

    def on_message(self, client, userdata, msg):
        LOG.info(f"Received `{msg.payload.decode()}` from `{msg.topic}` topic")
        if msg.topic == self.command_topic:
            try:
                payload = json.loads(msg.payload.decode())
                if "position" in payload:
                    position = int(payload["position"])
                    if position >= HA_MIN_POS and position <= HA_MAX_POS:
                        self.command_queue.put(("position", position))
                    else:
                        LOG.error(f"Invalid position: {position}. Must be between {HA_MIN_POS} and {HA_MAX_POS}")
                elif "state" in payload:
                    if payload["state"] == "STOP":
                        self.command_queue.put(("stop",))
                    else:
                        LOG.error(f"Unknown command received: {payload}")
                else:
                    LOG.error(f"Unknown command received: {payload}")
            except json.JSONDecodeError:
                LOG.error("Invalid JSON payload received.")

    def process_commands(self):
        while True:
            try:
                command = self.command_queue.get()
                if command[0] == "position":
                    self.linak_device.set_ha_position(command[1])
                    self.publish_state()
                elif command[0] == "stop":
                    # TODO: Implement stop functionality
                    pass
            except Exception as e:
                LOG.error(f"An error ocurred on process commands {e}")
            finally:
                self.command_queue.task_done()

    def send_discovery(self):
        config_topic = f"homeassistant/cover/{self.entity_id}/config"
        config_payload = {
            "unique_id": self.entity_id,
            "position_topic": self.state_topic,
            "set_position_topic": self.command_topic,
            "availability_topic": self.availability_topic,
            "payload_open": "OPEN",
            "payload_close": "CLOSE",
            "payload_stop": "STOP",
            "position_open": 100,
            "position_closed": 0,
            "set_position_template": '{"position": {{value}}}',
            "command_topic": self.command_topic,
            "state_topic": self.state_topic,
            "json_attributes_topic": self.state_topic,
            "device": {
                "identifiers": self.device_identifiers,
                "name": self.desk_name,
                "manufacturer": self.device_manufacturer,
                "model": self.device_model
            },
        }
        LOG.info(f"Sending discovery message to: {config_topic} : {json.dumps(config_payload)}")
        self.client.publish(config_topic, json.dumps(config_payload), retain=True)

    def send_availability(self, payload):
        LOG.info(f"Sending availability message to: {self.availability_topic} : {payload}")
        self.client.publish(self.availability_topic, payload, retain=True)

    def publish_state(self):
        if not self.connected:
            LOG.warning("Not publishing state because not connected.")
            self.last_reported_position = None
            return

        if time.time() - self.last_ping > self.keepalive*2:
                LOG.warning("Haven't pinged the server in a while, reconnecting...")
                self.reconnect()
                return

        try:
            report = self.linak_device._get_report()
            if self.last_reported_position != report.ha_position:
                state_payload = {
                    "position": report.ha_position,
                    "raw_position": report.position,
                    "moving": report.moving,
                    "height": report.position_in_in
                }
                LOG.info(f"Sending state message to: {self.state_topic} : {state_payload}")
                result, mid = self.client.publish(self.state_topic, json.dumps(state_payload), retain=False)

                if result != mqtt.MQTT_ERR_SUCCESS:
                    LOG.error(f"Error publishing state: {result}")
                    self.reconnect()
                    return

                self.last_reported_position = report.ha_position
                self.last_ping = time.time()
            else:
                try:
                    result, mid = self.client.publish(f"{self.state_topic}/ping", "ping", retain=False)
                    if result != mqtt.MQTT_ERR_SUCCESS:
                        LOG.error(f"Error publishing ping: {result}")
                        self.reconnect()
                except Exception as e:
                        LOG.error(f"Error sending ping: {e}")
                        self.reconnect()
        except Exception as e:
            LOG.error(f"Error publishing state: {e}")
            # Try to reconnect in case of error
            self.reconnect()

    def connect(self):
        try:
            LOG.info(f"Connecting to MQTT server: {self.server}:{self.port}")
            self.client.connect(self.server, self.port, self.keepalive)
            self.client.loop_start()
            self.send_availability("online")
        except Exception as ex:
            LOG.error(f"Error connecting to MQTT server: {ex}")

    def disconnect(self):
        self.send_availability("offline")
        self.client.disconnect()
        self.client.loop_stop()
