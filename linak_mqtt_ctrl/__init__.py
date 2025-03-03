#!/usr/bin/env python
import argparse
import logging
import sys
import threading
import json
import asyncio
import usb1  # Using libusb1 for asynchronous USB transfers
import signal
import time

# Import gmqtt for asyncio-native MQTT communication.
from gmqtt import Client as GMQTTClient

# USB communication and control constants
CONTROL_CBC = 5
REQ_TYPE_GET_INTERFACE = 0xa1
REQ_TYPE_SET_INTERFACE = 0x21
HID_GET_REPORT = 0x01
HID_SET_REPORT = 0x09
INIT = 0x0303
MOVE = 0x0305
GET_STATUS = 0x0304
BUF_LEN = 64
MODE_OF_OPERATION = 0x03
MODE_OF_OPERATION_DEFAULT = 0x04

# USB Button commands
MOVE_DOWN = 32767
MOVE_UP = 32768
MOVE_STOP = 32769

# MQTT and device constants
MQTT_TOPIC = "linak/desk"
DEVICE_NAME = "Linak Desk"

###############################################################################
# Logger Class
###############################################################################
class Logger:
    """
    A simple logger that outputs to the console.
    """
    def __init__(self, logger_name):
        self._log = logging.getLogger(logger_name)
        self.setup_logger()
        self._log.set_verbose = self.set_verbose

    def __call__(self):
        return self._log

    def set_verbose(self, verbose_level, quiet_level):
        self._log.setLevel(logging.WARNING)
        if quiet_level:
            self._log.setLevel(logging.ERROR)
            if quiet_level > 1:
                self._log.setLevel(logging.CRITICAL)
        if verbose_level:
            self._log.setLevel(logging.INFO)
            if verbose_level > 1:
                self._log.setLevel(logging.DEBUG)

    def setup_logger(self):
        if self._log.handlers:
            return
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.set_name("console")
        console_formatter = logging.Formatter("%(message)s")
        console_handler.setFormatter(console_formatter)
        self._log.addHandler(console_handler)
        self._log.setLevel(logging.WARNING)

LOG = Logger(__name__)()

###############################################################################
# StatusReport Class
###############################################################################
class StatusReport:
    """
    Get the status: position and movement.

    The raw response contains absolute position values that range from 0 to 6715.
    The conversion to a physical measurement (in cm or inches) is based on calibration:
      - Minimum desk height (cm): 67
      - Minimum desk height (in): 32

      - Maximum desk height (cm): 132
      - Maximum desk height (in): 51.61

    The conversion equations are:
      - Centimeters:  position_in_cm = actual_read_position / (max_raw_range) + 67
      - Inches:       position_in_in = (actual_read_position * 0.003856) + 25.61

    Note: The raw values and conversion factors have been determined through calibration.
    """

    def __init__(self, raw_response):
        # Determine if the device is moving (based on the 7th byte in the response)
        self.moving = raw_response[6] > 0
        # Combine two bytes to form the raw position value
        self.position = raw_response[4] + (raw_response[5] << 8)
        # Convert raw position to centimeters and inches based on calibration data
        self.position_in_cm = self.position / 65 + 67
        self.position_in_in = (self.position * 0.003856) + 25.61
        LOG.info(f"Linak Position: {self.position}, Moving: {self.moving}")

###############################################################################
# AsyncLinakDevice Class (The 'Linak' Class)
###############################################################################
class AsyncLinakDevice:
    """
    Asynchronous USB device class using libusb1’s asynchronous control transfers.
    This class now also contains a continuous run loop (via the run_loop() method) that
    monitors the device state and calls a publish callback whenever the state changes.
    """
    VEND = 0x12d3
    PROD = 0x0002

    def __init__(self, context, handle, loop):
        self.context = context
        self.handle = handle
        self.loop = loop
        self._shutdown = False
        # Start a dedicated thread to process USB events.
        self.event_thread = threading.Thread(target=self._handle_events_loop, daemon=True)
        self.event_thread.start()

    @classmethod
    async def create(cls, loop):
        """
        Asynchronously create and initialize the device.
        """
        context = usb1.USBContext()
        handle = context.openByVendorIDAndProductID(cls.VEND, cls.PROD, skip_on_error=True)
        if handle is None:
            raise ValueError(f"Device {cls.VEND}:{cls.PROD:04d} not found!")
        try:
            if handle.kernelDriverActive(0):
                handle.detachKernelDriver(0)
        except Exception as e:
            LOG.debug("Could not detach kernel driver: %s", e)
        device = cls(context, handle, loop)
        await device._init_device()
        return device

    async def _init_device(self):
        """
        Sends the initialization command to the device asynchronously.
        """
        buf = bytearray(BUF_LEN)
        buf[0] = MODE_OF_OPERATION
        buf[1] = MODE_OF_OPERATION_DEFAULT
        buf[2] = 0x00
        buf[3] = 0xfb
        await self.async_ctrl_transfer(REQ_TYPE_SET_INTERFACE, HID_SET_REPORT, INIT, 0, buf)
        await asyncio.sleep(0.5)  # Allow time for device initialization

    def _handle_events_loop(self):
        """
        Runs in a dedicated thread to continuously process USB events.
        """
        while not self._shutdown:
            try:
                self.context.handleEventsTimeout(0.1)
            except Exception as e:
                LOG.error("USB event loop error: %s", e)

    async def async_ctrl_transfer(self, request_type, request, value, index, data, timeout=1000):
        """
        Wraps an asynchronous control transfer.
        """
        start_time = time.time()  # Capture the start time
        future = self.loop.create_future()
        transfer = self.handle.getTransfer()

        def callback(transfer):
            status = transfer.getStatus()
            end_time = time.time()  # Capture the end time
            duration = end_time - start_time  # Calculate the duration
            LOG.info("USB control transfer duration: %.4f seconds", duration)  # Log the duration
            if status == usb1.TRANSFER_COMPLETED:
                result_data = bytes(transfer.getBuffer()[:transfer.getActualLength()])
                self.loop.call_soon_threadsafe(future.set_result, result_data)
            else:
                self.loop.call_soon_threadsafe(
                    future.set_exception, Exception("Transfer error: " + str(status))
                )
        try:
            transfer.setControl(request_type, request, value, index, data)
            transfer.setCallback(callback)
            transfer.submit()
        except Exception as e:
            future.set_exception(e)
        return await future

    async def get_position(self):
        """
        Asynchronously retrieves the current device position.
        """
        raw = await self.async_ctrl_transfer(
            REQ_TYPE_GET_INTERFACE, HID_GET_REPORT, GET_STATUS, 0, bytearray(BUF_LEN)
        )
        report = StatusReport(raw)
        LOG.debug('Position: %s, height: %.2fcm, moving: %s',
                  report.position, report.position_in_cm, report.moving)
        return report

    async def move(self, position):
        """
        Asynchronously moves the device to the desired position.
        """
        retry_count = 3
        previous_position = 0
        LOG.info("Moving to position: %s", position)
        while True:
            await self._move(position)
            await asyncio.sleep(0.2)

            raw = await self.async_ctrl_transfer(
                REQ_TYPE_GET_INTERFACE, HID_GET_REPORT, GET_STATUS, 0, bytearray(BUF_LEN)
            )
            status_report = StatusReport(raw)
            LOG.info("Current position: %s", status_report.position)

            if position == MOVE_UP or position == MOVE_DOWN or position == MOVE_STOP:
                break
            if status_report.position == position:
                break
            if previous_position == status_report.position:
                LOG.debug("Position unchanged: %s", previous_position)
                retry_count -= 1
            previous_position = status_report.position
            if retry_count == 0:
                LOG.debug("Retry threshold reached. Stopping move.")
                break

    async def _move(self, position):
        """
        Helper method to send the MOVE command.
        """
        buf = bytearray(BUF_LEN)
        pos = f"{position:04x}"  # Convert position to a 4-digit hex string
        pos_l = int(pos[2:], 16)
        pos_h = int(pos[:2], 16)
        buf[0] = CONTROL_CBC
        buf[1] = buf[3] = buf[5] = buf[7] = pos_l
        buf[2] = buf[4] = buf[6] = buf[8] = pos_h
        await self.async_ctrl_transfer(REQ_TYPE_SET_INTERFACE, HID_SET_REPORT, MOVE, 0, buf)

    def shutdown(self):
        """
        Cleanly shuts down the USB event loop thread and closes the USB context.
        """
        self._shutdown = True
        self.event_thread.join()
        self.handle.close()
        self.context.close()

    def _convert_position_to_percent(self, position):
        """
        Helper method to convert a raw position value to a percentage (0-100).
        This conversion uses a simple linear scale based on the device's raw range.
        """
        return int((position / 6715) * 100)

    async def run_loop(self, publish_callback, poll_interval=5):
        """
        Continuously monitors the device state.
        This simplified loop now simply awaits the poll_interval between polls,
        ensuring the event loop remains responsive.
        """
        last_state = None
        force_publish = True

        while not self._shutdown:
            await asyncio.sleep(poll_interval)
            try:
                report = await self.get_position()
                if report is not None:
                    # Convert the raw position to a percentage value
                    position_percent = self._convert_position_to_percent(report.position)
                    # Check if the position has changed or if a forced publish is required
                    if force_publish or last_state is None or position_percent != last_state:
                        state_payload = {
                            "position": position_percent,
                            "raw_position": report.position,
                            "moving": report.moving,
                            "height": report.position_in_in,
                            "height_cm": report.position_in_cm
                        }
                        LOG.info("Linak Device: State change detected, publishing new state: %s", state_payload)
                        await publish_callback(state_payload)
                        last_state = position_percent
                        force_publish = False
                    else:
                        LOG.debug("Linak Device: State unchanged (position: %s%%). Skipping publish.", position_percent)
                else:
                    LOG.info("Linak Device: Failed to get position data.")
            except Exception as e:
                LOG.error("Linak Device: Error in run_loop: %s", e)

###############################################################################
# AsyncMQTTClient Class Using gmqtt (Asyncio-Native)
###############################################################################
class AsyncMQTTClient:
    """
    An asyncio-native MQTT client built on top of gmqtt.
    """
    def __init__(self, broker, port, device_name, async_device, username=None, password=None):
        LOG.info("Initializing AsyncMQTTClient with broker: %s, port: %s", broker, port)
        self.device_name = device_name
        self.entity_id = "linak_desk"
        self.state_topic = f"linak/desk/{self.entity_id}/state"
        self.command_topic = f"linak/desk/{self.entity_id}/set"
        self.availability_topic = f"linak/desk/{self.entity_id}/availability"
        self.device_manufacturer = "Linak"
        self.device_model = "USB2LIN06"
        self.broker = broker
        self.port = port
        self.async_device = async_device
        self.payload_open = "OPEN"
        self.payload_stop = "STOP"
        self.payload_close = "CLOSE"

        self.last_state = None
        self.force_publish = True  # Force a state update immediately after connection.

        # This flag prevents scheduling multiple reconnect tasks concurrently.
        self.reconnecting = False

        # Create the gmqtt client instance with the specified client ID.
        self.client = GMQTTClient(self.entity_id)
        self.client.set_config({'reconnect_retries': -1, 'reconnect_delay': 1})

        # Set authentication if provided.
        if username and password:
            self.client.set_auth_credentials(username, password)
            LOG.info("Using MQTT credentials: username=%s", username)

        # Set gmqtt event callbacks.
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_subscribe = self.on_subscribe
        self.client.on_log = self.on_log

        # Initialize discovery publish timestamp (for logging purposes only).
        self._last_discovery_publish = None

    def on_log(self, client, level, buf):
        """
        Callback for logging MQTT events.
        """
        if "PINGREQ" in buf or "PINGRESP" in buf:
            LOG.debug("MQTT PING event: %s", buf)
        else:
            LOG.info(buf)

    async def connect(self):
        """
        Connect to the MQTT broker using gmqtt with an increased keepalive of 60 seconds.
        """
        try:
            # Changed keepalive from 10 to 60 seconds to allow more time for PING exchanges.
            await self.client.connect(self.broker, self.port, keepalive=60)
        except Exception as e:
            LOG.error("Failed to connect to MQTT broker: %s", e)
            asyncio.create_task(self.reconnect())

    def on_connect(self, client, flags, rc, properties):
        """
        Callback when the MQTT client connects or reconnects.
        Always publishes the discovery payload, subscribes to the command topic,
        and publishes an 'online' availability payload.
        """
        LOG.info("Connected to MQTT broker with result code: %s, flags: %s, properties: %s", rc, flags, properties)
        # Always publish discovery payload on connection.
        asyncio.create_task(self.publish_discovery())
        client.subscribe(self.command_topic)
        asyncio.create_task(self.publish_availability("online"))
        self.force_publish = True

    async def on_message(self, client, topic, payload, qos, properties):
        """
        Callback when a message is received.
        Processes movement commands received on the command topic.
        """
        decoded_payload = payload.decode() if isinstance(payload, bytes) else payload
        if topic == self.command_topic:
            if isinstance(decoded_payload, (int, float)):
                command_value = decoded_payload
            else:
                try:
                    command_value = int(decoded_payload)
                except ValueError:
                    command_value = decoded_payload
        
            LOG.info("Received message on topic %s: %s", topic, command_value)

            if command_value == self.payload_open:
                asyncio.create_task(self.async_device._move(MOVE_UP))
                position = MOVE_UP
            elif command_value == self.payload_close:
                asyncio.create_task(self.async_device._move(MOVE_DOWN))
                position = MOVE_DOWN
            elif command_value == self.payload_stop:
                asyncio.create_task(self.async_device._move(MOVE_STOP))
                position = MOVE_STOP
            elif isinstance(command_value, int):
                position = self.percent_to_position(command_value)
                asyncio.create_task(self.async_device.move(position))
            else:
                LOG.error("Invalid position value received: %s", decoded_payload)
                return 131
            LOG.info("Processed movement command: %s", command_value)
        return 0

    def on_subscribe(self, client, mid, qos, properties):
        """
        Callback when subscription is successful.
        """
        LOG.info("Subscribed to topic with mid: %s", mid)

    async def publish_discovery(self):
        """
        Publishes the MQTT discovery payload for Home Assistant integration.
        This payload is now always published after connecting to the server.
        """
        now = time.time()
        if self._last_discovery_publish is not None:
            interval = now - self._last_discovery_publish
            LOG.info("Discovery payload publishing interval: %.2f seconds", interval)
        else:
            LOG.info("Publishing discovery payload for the first time.")
        self._last_discovery_publish = now

        discovery_payload = {
            "unique_id": self.entity_id,
            "state_topic": self.state_topic,
            "state_open": 100,
            "state_closed": 0,
            "value_template": "{{ value_json.position }}",
            "command_topic": self.command_topic,
            "availability_topic": self.availability_topic,
            "position_open": 100,
            "position_closed": 0,
            "position_topic": self.state_topic,
            "position_template": "{{ value_json.position }}",
            "payload_open": self.payload_open,
            "payload_stop": self.payload_stop,
            "payload_close": self.payload_close,
            "set_position_topic": self.command_topic,
            "set_position_template": "{{ value }}",
            "json_attributes_topic": self.state_topic,
            "device": {
                "identifiers": [self.device_name.replace(" ", "_").lower()],
                "name": self.device_name,
                "model": self.device_model,
                "manufacturer": self.device_manufacturer
            }
        }
        topic = f"homeassistant/cover/{self.device_name.replace(' ', '_').lower()}/config"
        payload_json = json.dumps(discovery_payload)
        LOG.info("MQTT Publishing discovery payload: %s", discovery_payload)
        self.client.publish(topic, payload_json, qos=1)

    async def publish_availability(self, payload):
        """
        Publishes an availability message (e.g., 'online' or 'offline').
        """
        LOG.info("MQTT Publishing availability payload: %s", payload)
        self.client.publish(self.availability_topic, payload, qos=1, retain=True)

    async def publish_state(self, state_payload):
        """
        Publishes a state payload to the configured MQTT state topic.
        """
        payload_json = json.dumps(state_payload)
        LOG.info("MQTT Publishing state: %s", state_payload)
        self.client.publish(self.state_topic, payload_json, qos=1)

    def percent_to_position(self, percent):
        """
        Converts a percentage (0-100) to a raw position value.
        """
        return int((percent / 100) * 6715)

    async def disconnect(self):
        """
        Publishes an offline availability message and disconnects the MQTT client.
        """
        await self.publish_availability("offline")
        await self.client.disconnect()

###############################################################################
# Main Async Entry Point and Signal Handling
###############################################################################
async def async_main(args):
    """
    Main asynchronous entry point that creates the device and dispatches to the correct subcommand.
    """
    device = await AsyncLinakDevice.create(asyncio.get_running_loop())
    mqtt_client = None
    shutdown_triggered = False
    loop = asyncio.get_running_loop()

    async def signal_handler():
        nonlocal shutdown_triggered
        if shutdown_triggered:
            return
        shutdown_triggered = True
        LOG.warning("Received exit signal, shutting down...")
        device.shutdown()
        if mqtt_client:
            try:
                await mqtt_client.disconnect()
            except Exception as e:
                LOG.error("Error during MQTT disconnect: %s", e)

    # Use loop.add_signal_handler for clean integration with asyncio.
    try:
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(signal_handler()))
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(signal_handler()))
    except NotImplementedError:
        # Signal handling might not be implemented on some platforms (e.g., Windows)
        pass

    try:
        if args.func == 'status':
            report = await device.get_position()
            LOG.error('Position: %s, height: %.2fcm, moving: %s',
                      report.position, report.position_in_cm, report.moving)
        elif args.func == 'move':
            await device.move(args.position)
        elif args.func == 'mqtt':
            mqtt_client = AsyncMQTTClient(args.server, args.port, DEVICE_NAME, device, args.username, args.password)
            await mqtt_client.connect()
            # Use the device's run_loop to publish state changes via MQTT.
            await device.run_loop(mqtt_client.publish_state)
    finally:
        await signal_handler()

###############################################################################
# Main Function and Argument Parsing
###############################################################################
def main():
    parser = argparse.ArgumentParser('A utility to interact with USB2LIN06 device asynchronously using libusb1 and gmqtt.')
    subparsers = parser.add_subparsers(help='supported commands', dest='subcommand')
    subparsers.required = True

    parser_status = subparsers.add_parser('status', help='Get status of the device.')
    parser_status.set_defaults(func='status')

    parser_move = subparsers.add_parser('move', help='Move to a specified position.')
    parser_move.add_argument('position', type=int)
    parser_move.set_defaults(func='move')

    parser_mqtt = subparsers.add_parser('mqtt', help='Run in MQTT mode.')
    parser_mqtt.add_argument('--server', required=True, help='MQTT server address')
    parser_mqtt.add_argument('--port', type=int, default=1883, help='MQTT server port')
    parser_mqtt.add_argument('--username', help='MQTT username')
    parser_mqtt.add_argument('--password', help='MQTT password')
    parser_mqtt.set_defaults(func='mqtt')

    group = parser.add_mutually_exclusive_group()
    group.add_argument("-q", "--quiet", help='Decrease verbosity', action="count", default=0)
    group.add_argument("-v", "--verbose", help='Increase verbosity', action="count", default=0)

    args = parser.parse_args()
    LOG.set_verbose(args.verbose, args.quiet)
    asyncio.run(async_main(args))

if __name__ == '__main__':
    main()
