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
    Get the status: position and movement

    Measurement height in cm has been taken manually. In minimal height,
    height from floor to the underside of the desktop and is 67cm. Note, this
    value may differ, since mine desk have wheels. In maximal elevation, it is
    132cm.
    For readings from the USB device, numbers are absolute, and have values of
    0 and 6480 for min and max positions.

    Exposed position information in cm is than taken as a result of the
    equation:

        position_in_cm = actual_read_position / (132 - 67) + 67

    For inches, the equation is:
    1658→32 inches
    6715→51.5 inches

        postition_in_in = 0.003856 * actual_read_position + 25.61

    """

    def __init__(self, raw_response):
        self.moving = raw_response[6] > 0
        self.position = raw_response[4] + (raw_response[5] << 8)
        self.position_in_cm = self.position / 65 + 67
        self.position_in_in = (self.position * 0.003856) + 25.61
        LOG.info(f"Linak Position: {self.position}, Moving: {self.moving}")

###############################################################################
# AsyncLinakDevice Class
###############################################################################
class AsyncLinakDevice:
    """
    Asynchronous USB device class using libusb1’s asynchronous control transfers.
    Creates a USB context, opens the device, detaches the kernel driver if necessary,
    and initializes the device asynchronously. A dedicated thread continuously handles
    USB events.
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
        time.sleep(1)  # Allow time for the event thread to exit
        self.handle.close()
        self.context.close()

###############################################################################
# AsyncMQTTClient Class Using gmqtt (Asyncio-Native)
###############################################################################
class AsyncMQTTClient:
    """
    An asyncio-native MQTT client built on top of gmqtt.
    Publishes device state periodically and handles incoming commands to move the device.
    It also publishes a discovery payload for Home Assistant integration.
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

        # Track last published state to avoid unnecessary updates.
        self.last_state = None
        self.force_publish = True  # Force a state update on (re)connection.

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
        # self.client.on_disconnect = self.on_disconnect
        self.client.on_subscribe = self.on_subscribe
        self.client.on_log = self.on_log

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
        Connect to the MQTT broker using gmqtt with a keepalive of 60 seconds.
        """
        try:
            await self.client.connect(self.broker, self.port, keepalive=10)
        except Exception as e:
            LOG.error("Failed to connect to MQTT broker: %s", e)
            asyncio.create_task(self.reconnect())

    def on_connect(self, client, flags, rc, properties):
        """
        Callback when the MQTT client connects or reconnects.
        Publishes discovery and availability payloads and subscribes to the command topic.
        """
        LOG.info("Connected to MQTT broker with result code: %s, flags: %s, properties: %s", rc, flags, properties)
        # Publish discovery payload for Home Assistant integration.
        asyncio.create_task(self.publish_discovery())
        # Subscribe to the command topic.
        client.subscribe(self.command_topic)
        # Publish an 'online' availability message.
        asyncio.create_task(self.publish_availability("online"))
        # Force a state update immediately after connection.
        self.force_publish = True

    async def on_message(self, client, topic, payload, qos, properties):
        """
        Callback when a message is received.
        If the message is on the command topic, process the movement command.
        """
        decoded_payload = payload.decode() if isinstance(payload, bytes) else payload
        LOG.info("Received message on topic %s: %s", topic, decoded_payload)
        if topic == self.command_topic:
            try:
                position_percent = int(decoded_payload)
                position = self.percent_to_position(position_percent)
                LOG.info("Moving device to position: %s", position)
                asyncio.create_task(self.async_device.move(position))
            except ValueError:
                LOG.error("Invalid position value received: %s", decoded_payload)
                return 131
        return 0

    def on_disconnect(self, client, packet, exc=None):
        """
        Callback when the client disconnects from the MQTT broker.
        Prevents multiple reconnect attempts by checking the reconnecting flag.
        """
        LOG.warning("Disconnected from MQTT broker. Client: %s, Packet: %s, Exception: %s", client, packet, exc)
        if not self.reconnecting:
            self.reconnecting = True
            asyncio.create_task(self.reconnect())
        else:
            LOG.debug("Reconnect already in progress; ignoring duplicate disconnect event.")

    def on_subscribe(self, client, mid, qos, properties):
        """
        Callback when subscription is successful.
        """
        LOG.info("Subscribed to topic with mid: %s", mid)

    async def reconnect(self):
        """
        Attempts to reconnect to the MQTT broker after a 5-second delay.
        Only one reconnect attempt will run at a time.
        """
        await asyncio.sleep(5)
        try:
            await self.client.reconnect()
            LOG.info("Reconnected to MQTT broker")
        except Exception as e:
            LOG.error("Reconnection failed: %s", e)
            # If reconnection fails, schedule another attempt.
            asyncio.create_task(self.reconnect())
        finally:
            # Reset the flag to allow future reconnect attempts if necessary.
            self.reconnecting = False

    async def publish_discovery(self):
        """
        Publishes the MQTT discovery payload for Home Assistant integration.
        """
        discovery_payload = {
            "unique_id": self.entity_id,
            "state_topic": self.state_topic,
            "state_open": 100,
            "state_closed": 0,
            "command_topic": self.command_topic,
            "availability_topic": self.availability_topic,
            "position_open": 100,
            "position_closed": 0,
            "position_topic": self.state_topic,
            "position_template": "{{ value_json.position }}",
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

    async def run(self):
        """
        Main loop that periodically retrieves the device's position and publishes it
        to the MQTT state topic if the state has changed or a forced update is required.
        """
        while not self.async_device._shutdown:
            try:
                report = await self.async_device.get_position()
                if report is not None:
                    position_percent = self.position_to_percent(report.position)
                    if self.force_publish or self.last_state is None or position_percent != self.last_state:
                        state_payload = {
                            "position": position_percent,
                            "raw_position": report.position,
                            "moving": report.moving,
                            "height": report.position_in_in,
                            "height_cm": report.position_in_cm
                        }
                        LOG.info("MQTT Publishing state: %s", state_payload)
                        payload_json = json.dumps(state_payload)
                        self.client.publish(self.state_topic, payload_json, qos=1)
                        self.last_state = position_percent
                        self.force_publish = False
                    else:
                        LOG.debug("State unchanged (position: %s). Not publishing.", position_percent)
                else:
                    LOG.info("Failed to get position")
            except Exception as e:
                LOG.error("Error getting position: %s", e)
            await asyncio.sleep(2)

    def percent_to_position(self, percent):
        """
        Converts a percentage (0-100) to a raw position value.
        """
        return int((percent / 100) * 6715)

    def position_to_percent(self, position):
        """
        Converts a raw position value to a percentage (0-100).
        """
        return int((position / 6715) * 100)

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

    async def signal_handler():
        LOG.info("Received exit signal, shutting down...")
        if mqtt_client:
            await mqtt_client.disconnect()
        device.shutdown()

    # Register signal handlers for graceful shutdown.
    signal.signal(signal.SIGINT, lambda s, f: asyncio.create_task(signal_handler()))
    signal.signal(signal.SIGTERM, lambda s, f: asyncio.create_task(signal_handler()))

    try:
        if args.func == 'status':
            await device.get_position()
        elif args.func == 'move':
            await device.move(args.position)
        elif args.func == 'mqtt':
            mqtt_client = AsyncMQTTClient(args.server, args.port, DEVICE_NAME, device, args.username, args.password)
            await mqtt_client.connect()
            # Note: gmqtt handles incoming messages automatically once connected,
            # so there's no need to start an explicit listener task.
            await mqtt_client.run()
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
