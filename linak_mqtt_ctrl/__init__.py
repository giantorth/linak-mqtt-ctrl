#!/usr/bin/env python

__version__ = "1.0.0"

import argparse
import logging
import sys
import threading
import json
import asyncio
import usb1
import time
import queue
import os
import yaml

from logging.handlers import QueueHandler, QueueListener

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
DEVICE_NAME = "Standing Desk"
DESK_MAX_HEIGHT = 6715
DESK_MIN_TRAVEL = 1550

# Define the path to the configuration file which holds MQTT config and presets.
CONFIG_FILE = "/etc/linakdesk/config.yaml"

###############################################################################
# Asynchronous Logger Setup Using QueueHandler/QueueListener
###############################################################################
class Logger:
    """
    A logger that uses asynchronous logging to avoid blocking the asyncio event loop.
    Instead of writing log messages directly to sys.stderr (which may be slow),
    this logger enqueues log records and processes them on a separate thread.
    """
    def __init__(self, logger_name):
        # Create a logger instance.
        self._log = logging.getLogger(logger_name)
        self.setup_logger()
        # Expose set_verbose for convenience.
        self._log.set_verbose = self.set_verbose

    def __call__(self):
        return self._log

    def set_verbose(self, verbose_level, quiet_level):
        # Adjust log level based on verbose/quiet settings.
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
        """
        Configure the logger to use a QueueHandler so that log messages are processed
        asynchronously. This helps prevent logging from blocking the main asyncio loop.
        """
        # If already configured, do nothing.
        if self._log.handlers:
            return

        # Create a queue for log records.
        log_queue = queue.Queue(-1)
        # Create a QueueHandler that will send log records to the queue.
        queue_handler = QueueHandler(log_queue)
        self._log.addHandler(queue_handler)
        # Disable propagation to avoid duplicate log messages.
        self._log.propagate = False   
        # Create a StreamHandler that writes to sys.stderr.
        stream_handler = logging.StreamHandler(sys.stderr)
        # Set the formatter for the stream handler.
        stream_handler.setFormatter(logging.Formatter("%(message)s"))
        # Create a QueueListener that will listen to the queue and dispatch records to stream_handler.
        listener = QueueListener(log_queue, stream_handler)
        listener.start()
        # Save a reference to the listener so it isn’t garbage-collected.
        self._listener = listener
        # Set a default level.
        self._log.setLevel(logging.WARNING)

# Initialize the logger instance.
LOG = Logger(__name__)()

###############################################################################
# StatusReport Class
###############################################################################
class StatusReport:
    """
    Get the status: position and movement.

    The raw response contains absolute position values that range from 0 to 6715.
    Height calculations are based on two calibration points provided during initialization.
    
    Example usage:
        # Using inches
        calibration = {
            'unit': 'in',
            'point1': {'raw': 0, 'height': 25.61},    # lowest position
            'point2': {'raw': DESK_MAX_HEIGHT, 'height': 51.61}  # highest position
        }
        
        # Using centimeters
        calibration = {
            'unit': 'cm',
            'point1': {'raw': 0, 'height': 67},      # lowest position
            'point2': {'raw': DESK_MAX_HEIGHT, 'height': 132}   # highest position
        }
    """
    def __init__(self, raw_response, calibration=None):
        # Default calibration if none provided (backward compatibility)
        self._default_calibration = {
            'unit': 'in',
            'point1': {'raw': 0, 'height': 25.61},
            'point2': {'raw': DESK_MAX_HEIGHT, 'height': 51.61}
        }
        
        # Set calibration data
        self._calibration = calibration if calibration else self._default_calibration
        
        # Determine if the device is moving (based on the 7th byte in the response)
        self.moving = raw_response[6] > 0
        
        # Combine two bytes to form the raw position value
        self.position = raw_response[4] + (raw_response[5] << 8)
        
        # Calculate heights based on calibration
        self._calculate_heights()
        
        LOG.info(f"Linak Position: {self.position}, Moving: {self.moving}")

    def _calculate_heights(self):
        """Calculate heights based on calibration data and current position."""
        # Using custom calibration
        height = self._interpolate_height(
            self.position,
            self._calibration['point1']['raw'],
            self._calibration['point1']['height'],
            self._calibration['point2']['raw'],
            self._calibration['point2']['height']
        )
        
        if self._calibration['unit'] == 'in':
            self.position_in_in = round(height, 2)
            self.position_in_cm = round(self._inches_to_cm(height), 2)
        else:  # cm
            self.position_in_cm = round(height, 2)
            self.position_in_in = round(self._cm_to_inches(height), 2)

    def _interpolate_height(self, raw_pos, raw1, height1, raw2, height2):
        """
        Linear interpolation between two calibration points.
        """
        if raw2 == raw1:
            return height1
        return height1 + (raw_pos - raw1) * (height2 - height1) / (raw2 - raw1)

    def _inches_to_cm(self, inches):
        """Convert inches to centimeters."""
        return inches * 2.54

    def _cm_to_inches(self, cm):
        """Convert centimeters to inches."""
        return cm / 2.54

###############################################################################
# AsyncLinakDevice Class (The 'Linak' Class)
###############################################################################
class AsyncLinakDevice:
    """
    Asynchronous USB device class using libusb1 asynchronous control transfers.
    This class also contains a continuous run loop (via the run_loop() method) that
    monitors the device state and calls a publish callback whenever the state changes.

    KEY CHANGES:
    1. The async_ctrl_transfer method uses asyncio.wait_for to enforce a timeout.
    2. If a USB control transfer times out, the underlying USB transfer is explicitly canceled.
    3. The run_loop method schedules get_position as a separate task with a short timeout,
       ensuring that a hanging USB call does not block the event loop and delay MQTT ping responses.
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
        
        # Claim interface 0
        handle.claimInterface(0)
        
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

        This method includes a timeout to prevent long-running USB operations
        from blocking the main asyncio event loop and delaying MQTT ping responses.
        If the timeout is exceeded, the underlying USB transfer is explicitly canceled.

        Parameters:
            request_type: The USB request type.
            request: The USB request.
            value: The value for the request.
            index: The index for the request.
            data: The data buffer for the request.
            timeout: Timeout in milliseconds (default 1000 ms).

        Returns:
            The result data from the USB control transfer.

        Raises:
            TimeoutError if the transfer does not complete within the specified timeout.
            Exception if the transfer fails.
        """
        start_time = time.time()
        future = self.loop.create_future()
        transfer = self.handle.getTransfer()

        def callback(transfer):
            status = transfer.getStatus()
            end_time = time.time()
            duration = end_time - start_time
            LOG.debug("USB control transfer duration: %.4f seconds", duration)
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

        # Enforce a timeout on the USB transfer using asyncio.wait_for.
        try:
            result = await asyncio.wait_for(future, timeout=timeout/1000)
            return result
        except asyncio.TimeoutError:
            # If the wait_for times out, explicitly cancel the underlying USB transfer.
            try:
                transfer.cancel()
                LOG.error("Cancelled USB transfer due to timeout.")
            except Exception as e:
                LOG.error("Error cancelling USB transfer: %s", e)
            raise

    async def get_position(self):
        """
        Asynchronously retrieves the current device position.
        If the USB control transfer takes too long, a TimeoutError will be raised.
        """
        try:
            raw = await self.async_ctrl_transfer(
                REQ_TYPE_GET_INTERFACE, HID_GET_REPORT, GET_STATUS, 0, bytearray(BUF_LEN)
            )
        except asyncio.TimeoutError:
            LOG.error("USB control transfer timed out during get_position.")
            raise
        report = StatusReport(raw)
        LOG.debug('Position: %s, height: %.2fcm, moving: %s',
                  report.position, report.position_in_cm, report.moving)
        return report

    async def move(self, position):
        """
        Asynchronously moves the device to the desired position.
        This method repeatedly sends move commands and polls for the current position.
        It stops once the desired position is reached or a retry threshold is exceeded.
        """
        retry_count = 3
        previous_position = 0

        if position < DESK_MIN_TRAVEL: 
            position = DESK_MIN_TRAVEL

        LOG.info("Moving to position: %s", position)
        while True:
            await self._move(position)
            await asyncio.sleep(0.2)
            raw = await self.async_ctrl_transfer(
                REQ_TYPE_GET_INTERFACE, HID_GET_REPORT, GET_STATUS, 0, bytearray(BUF_LEN)
            )
            status_report = StatusReport(raw)
            LOG.info("Current position: %s", status_report.position)
            if position in (MOVE_UP, MOVE_DOWN, MOVE_STOP):
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
        Helper method to send the MOVE command to USB.
        """
        if position < DESK_MIN_TRAVEL:
            position = DESK_MIN_TRAVEL

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
        LOG.debug("Shutting down Linak device...")
        self._shutdown = True
        self.event_thread.join()
        self.handle.close()
        self.context.close()

    def _convert_position_to_percent(self, position):
        """
        Helper method to convert a raw position value to a percentage (0-100).
        """
        return int((position / DESK_MAX_HEIGHT) * 100)

    async def run_loop(self, publish_callback, poll_interval=1):
        """
        Continuously monitors the device state.
        Instead of directly awaiting get_position, we schedule it as a separate task
        with a short timeout (0.5 seconds). If get_position does not complete within
        this timeout, the poll cycle is skipped.
        """
        last_state = None
        force_publish = True
        while not self._shutdown:
            # Sleep for the poll interval, yielding control to the event loop.
            await asyncio.sleep(poll_interval)
            try:
                # Schedule get_position in its own task.
                get_position_task = asyncio.create_task(self.get_position())
                # Wait for get_position to finish but only for 0.5 seconds.
                report = await asyncio.wait_for(get_position_task, timeout=0.5)
            except asyncio.TimeoutError:
                LOG.error("get_position task timed out; skipping this poll cycle.")
                # Cancel the task if still running.
                get_position_task.cancel()
                continue
            except Exception as e:
                LOG.error("Error during get_position: %s", e)
                continue
            if report is not None:
                # Convert the raw position to a percentage value.
                position_percent = self._convert_position_to_percent(report.position)
                # Check if the position has changed or if a forced publish is required.
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

        # NEW: Add a lock state attribute (False = unlocked, True = locked)
        self.locked = False

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

        # NEW: Initialize movement repeater task holder.
        self._button_repeat_task = None

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
        Connect to the MQTT broker using gmqtt.
        """
        try:
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
        # Subscribe to both the desk command topic and the lock command topic.
        client.subscribe(self.command_topic)
        client.subscribe("linak/desk/lock/set")
        # NEW: Subscribe to preset commands for both 'go' and 'set'
        client.subscribe("linak/desk/preset/+/go")
        client.subscribe("linak/desk/preset/+/set")
        asyncio.create_task(self.publish_availability("online"))
        self.force_publish = True

    async def on_message(self, client, topic, payload, qos, properties):
        """
        Callback when a message is received.
        Processes movement commands received on the command topic,
        lock commands, and the newly added preset commands.
        """
        decoded_payload = payload.decode() if isinstance(payload, bytes) else payload
        LOG.info("Received message on topic %s: %s", topic, decoded_payload)

        # Handle messages on the cover (desk) command topic.
        if topic == self.command_topic:
            # Check if the desk is locked. If so, ignore any movement commands.
            if self.locked:
                LOG.warning("Movement command ignored because desk is locked.")
                return 0  # Exit early since movement should be disabled.

            if isinstance(decoded_payload, (int, float)):
                command_value = decoded_payload
            else:
                try:
                    command_value = int(decoded_payload)
                except ValueError:
                    command_value = decoded_payload

            LOG.info("Processing movement command: %s", command_value)

            if command_value == self.payload_open:
                # Cancel any existing repeating task before starting a new one.
                if self._button_repeat_task:
                    self._button_repeat_task.cancel()
                self._button_repeat_task = asyncio.create_task(self._repeat_button(MOVE_UP))
                position = MOVE_UP
            elif command_value == self.payload_close:
                if self._button_repeat_task:
                    self._button_repeat_task.cancel()
                self._button_repeat_task = asyncio.create_task(self._repeat_button(MOVE_DOWN))
                position = MOVE_DOWN
            elif command_value == self.payload_stop:
                # Cancel any active repeating move.
                if self._button_repeat_task:
                    self._button_repeat_task.cancel()
                    self._button_repeat_task = None
                asyncio.create_task(self.async_device._move(MOVE_STOP))
                position = MOVE_STOP
            elif isinstance(command_value, int):
                position = self.percent_to_position(command_value)
                asyncio.create_task(self.async_device.move(position))
            else:
                LOG.error("Invalid position value received: %s", decoded_payload)
                return 131
            LOG.info("Processed movement command: %s", command_value)

        # Handle messages on the lock command topic.
        elif topic == "linak/desk/lock/set":
            if decoded_payload == "LOCK":
                self.locked = True
                LOG.info("Desk locked: Movement commands will be disabled.")
            elif decoded_payload == "UNLOCK":
                self.locked = False
                LOG.info("Desk unlocked: Movement commands are now enabled.")
            else:
                LOG.error("Invalid lock command received: %s", decoded_payload)
                return 131

            # Publish updated lock state on the lock state topic.
            lock_state = "LOCKED" if self.locked else "UNLOCKED"
            self.client.publish("linak/desk/lock/state", lock_state, qos=1)
            LOG.info("Published lock state: %s", lock_state)

        # NEW: Handle preset commands (both "set" and "go").
        # The expected topic format is: "linak/desk/preset/<preset_number>/<command>"
        elif topic.startswith("linak/desk/preset/"):
            parts = topic.split('/')
            # Ensure the topic has the proper format (should have at least 5 parts)
            if len(parts) < 5:
                LOG.error("Invalid preset command topic format: %s", topic)
                return
            preset_number = parts[3]
            command = parts[4]
            # Validate that the preset number is one of the allowed values (1-4)
            if preset_number not in ["1", "2", "3", "4"]:
                LOG.error("Invalid preset number: %s", preset_number)
                return
            if command == "set":
                # Call the method to record the current position to the config file.
                await self.set_preset(preset_number)
            elif command == "go":
                # Call the method to move the desk to the preset position.
                await self.go_to_preset(preset_number)
            else:
                LOG.error("Unknown preset command: %s", command)
        else:
            LOG.warning("Received message on an unrecognized topic: %s", topic)
        return 0

    async def _repeat_button(self, move_command):
        """
        Repeatedly sends the given move_command every 0.3 seconds.
        Runs until cancelled (e.g. when a MOVE_STOP arrives) or when the desk reaches its limits.
        """
        try:
            while True:
                await self.async_device._move(move_command)
                report = await self.async_device.get_position()
                # Check if the desk has reached its lower or upper limit.
                if report.position <= 0 or report.position >= DESK_MAX_HEIGHT:
                    LOG.info("Desk reached limit (%s). Cancelling repeating move command.", report.position)
                    break
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            LOG.info("Repeating move command cancelled.")
            raise

    def on_subscribe(self, client, mid, qos, properties):
        """
        Callback when subscription is successful.
        """
        LOG.info("Subscribed to topic with mid: %s", mid)

    async def publish_discovery(self):
        """
        Publishes a combined MQTT discovery payload for Home Assistant integration,
        containing multiple components (cover, lock, preset buttons, and set preset buttons)
        for a single device.
        """
    
        discovery_payload = {
            "device": {
                "identifiers": [self.device_name.replace(" ", "_").lower()],
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
                "topic": self.availability_topic,
            },
            "components": {
                "standing_desk": {
                    "platform": "cover",
                    "name": self.device_name,
                    "unique_id": self.entity_id,
                    "state_topic": self.state_topic,
                    "command_topic": self.command_topic,
                    "state_open": 100,
                    "state_closed": 0,
                    "value_template": "{{ value_json.position }}",
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
                    "icon": "mdi:table-furniture"
                },
                "desk_lock": {
                    "platform": "lock",
                    "name": "Desk Lock",
                    "unique_id": f"{self.entity_id}_lock",
                    "command_topic": "linak/desk/lock/set",
                    "state_topic": "linak/desk/lock/state",
                    "payload_lock": "LOCK",
                    "payload_unlock": "UNLOCK",
                    "state_locked": "LOCKED",
                    "state_unlocked": "UNLOCKED"
                },
                "preset_button_1": {
                    "platform": "button",
                    "name": "Preset 1",
                    "unique_id": f"{self.entity_id}_preset1",
                    "command_topic": "linak/desk/preset/1/go"
                },
                "preset_button_2": {
                    "platform": "button",
                    "name": "Preset 2",
                    "unique_id": f"{self.entity_id}_preset2",
                    "command_topic": "linak/desk/preset/2/go"
                },
                "preset_button_3": {
                    "platform": "button",
                    "name": "Preset 3",
                    "unique_id": f"{self.entity_id}_preset3",
                    "command_topic": "linak/desk/preset/3/go"
                },
                "preset_button_4": {
                    "platform": "button",
                    "name": "Preset 4",
                    "unique_id": f"{self.entity_id}_preset4",
                    "command_topic": "linak/desk/preset/4/go"
                },
                "set_preset_button_1": {
                    "platform": "button",
                    "name": "Set Preset 1",
                    "unique_id": f"{self.entity_id}_set_preset1",
                    "command_topic": "linak/desk/preset/1/set",
                    "entity_category": "config"
                },
                "set_preset_button_2": {
                    "platform": "button",
                    "name": "Set Preset 2",
                    "unique_id": f"{self.entity_id}_set_preset2",
                    "command_topic": "linak/desk/preset/2/set",
                    "entity_category": "config"
                },
                "set_preset_button_3": {
                    "platform": "button",
                    "name": "Set Preset 3",
                    "unique_id": f"{self.entity_id}_set_preset3",
                    "command_topic": "linak/desk/preset/3/set",
                    "entity_category": "config"
                },
                "set_preset_button_4": {
                    "platform": "button",
                    "name": "Set Preset 4",
                    "unique_id": f"{self.entity_id}_set_preset4",
                    "command_topic": "linak/desk/preset/4/set",
                    "entity_category": "config"
                },
                "desk_height": {   # New sensor component for desk height (in inches)
                    "platform": "sensor",
                    "name": "Desk Height",
                    "unique_id": f"{self.entity_id}_height",
                    "state_topic": self.state_topic,
                    "value_template": "{{ value_json.height }}",
                    "unit_of_measurement": "in",
                    "icon": "mdi:arrow-expand-vertical"
                }
            },
            "state_topic": self.state_topic,
            "qos": 1
        }

        topic = f"homeassistant/device/{self.device_name.replace(' ', '_').lower()}/config"
        payload_json = json.dumps(discovery_payload)
        LOG.info("MQTT Publishing discovery payload: %s", discovery_payload)
        self.client.publish(topic, payload_json, qos=1, retain=True)
        # Publish the lock state immediately after discovery.
        self.client.publish("linak/desk/lock/state", "UNLOCKED", qos=1)
        # Home assistant seems to read the set value immediately after discovery.
        self.client.publish("linak/desk/lock/set", "UNLOCK", qos=1)

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
        return int((percent / 100) * DESK_MAX_HEIGHT)

    async def disconnect(self):
        """
        Publishes an offline availability message and disconnects the MQTT client.
        """
        await self.publish_availability("offline")
        await self.client.disconnect()

    async def cleanup(self):
        """
        Cancels any active repeating move task to ensure a graceful shutdown.
        """
        if self._button_repeat_task:
            self._button_repeat_task.cancel()
            try:
                await self._button_repeat_task
            except asyncio.CancelledError:
                LOG.info("Button repeat task cancelled during cleanup.")

    def load_config(self):
        """
        Synchronously loads the configuration from CONFIG_FILE.
        Returns:
            A dictionary with the configuration data. If the file does not exist,
            returns an empty dictionary.
        """
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    # Load YAML data; if the file is empty, use an empty dict.
                    config = yaml.safe_load(f) or {}
                LOG.debug("Configuration loaded from %s: %s", CONFIG_FILE, config)
                return config
            else:
                LOG.info("Configuration file %s does not exist. Using empty config.", CONFIG_FILE)
                return {}
        except Exception as e:
            LOG.error("Error reading configuration file %s: %s", CONFIG_FILE, e)
            return {}

    def save_config(self, config):
        """
        Synchronously saves the given configuration dictionary to CONFIG_FILE.
        Parameters:
            config (dict): The configuration data to save.
        """
        try:
            with open(CONFIG_FILE, 'w') as f:
                yaml.safe_dump(config, f)
            LOG.debug("Configuration saved to %s: %s", CONFIG_FILE, config)
        except Exception as e:
            LOG.error("Error saving configuration file %s: %s", CONFIG_FILE, e)

    async def set_preset(self, preset_number):
        """
        Records the current desk position as a preset in the configuration file.
        This method retrieves the current raw position from the device and saves it
        under a key (e.g., 'preset1') in CONFIG_FILE.
        Parameters:
            preset_number (str): The preset number as a string (e.g., "1").
        """
        try:
            # Retrieve the current desk position asynchronously.
            report = await self.async_device.get_position()
            current_position = report.position
            LOG.info("Current position retrieved for preset %s: %s", preset_number, current_position)
            # Load the current configuration in a separate thread to avoid blocking.
            config = await asyncio.to_thread(self.load_config)
            # Update the configuration with the new preset value.
            config[f"preset{preset_number}"] = current_position
            # Save the updated configuration back to the file.
            await asyncio.to_thread(self.save_config, config)
            LOG.info("Preset %s set to position %s and saved to %s", preset_number, current_position, CONFIG_FILE)
        except Exception as e:
            LOG.error("Error setting preset %s: %s", preset_number, e)

    async def go_to_preset(self, preset_number):
        """
        Commands the desk to move to a preset position as defined in the configuration file.
        This method loads the configuration, reads the preset value for the given preset number,
        and then instructs the device to move to that position.
        Parameters:
            preset_number (str): The preset number as a string (e.g., "1").
        """
        # Check if the desk is locked; if so, ignore preset movement commands.
        if self.locked:
            LOG.warning("Preset movement command ignored because desk is locked.")
            return
        try:
            # Load the configuration from the file.
            config = await asyncio.to_thread(self.load_config)
            preset_key = f"preset{preset_number}"
            if preset_key not in config:
                LOG.error("Preset %s not found in configuration file %s.", preset_number, CONFIG_FILE)
                return
            target_position = config[preset_key]
            LOG.info("Moving desk to preset %s position: %s", preset_number, target_position)
            # Command the device to move to the target position.
            asyncio.create_task(self.async_device.move(target_position))
        except Exception as e:
            LOG.error("Error executing preset %s command: %s", preset_number, e)

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
    run_loop_task = None
    loop = asyncio.get_running_loop()

    async def signal_handler():
        nonlocal shutdown_triggered, run_loop_task
        if shutdown_triggered:
            return
        shutdown_triggered = True
        LOG.warning("Received exit signal, shutting down...")

        # Disconnect MQTT client and run cleanup
        if mqtt_client:
            try:
                await mqtt_client.disconnect()
                await mqtt_client.cleanup()
            except Exception as e:
                LOG.error("Error during MQTT disconnect/cleanup: %s", e)
        
        # Cancel the device run loop task if it exists
        if run_loop_task:
            run_loop_task.cancel()
            try:
                await run_loop_task
            except asyncio.CancelledError:
                LOG.info("Device run loop task cancelled.")

        # Allow time for MQTT operations to complete
        await asyncio.sleep(0.5)
        
        # Shutdown the USB device
        try:
            device.shutdown()
        except Exception as e:
            LOG.error("Error during device shutdown: %s", e)

        # Cancel all remaining tasks to ensure a clean exit
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

    try:
        if args.func == 'status':
            report = await device.get_position()
            LOG.warning('Position: %s, height: %.2fcm, moving: %s',
                     report.position, report.position_in_cm, report.moving)
        elif args.func == 'move':
            await device.move(args.position)
            report = await device.get_position()
            LOG.warning("Current position: %s", report.position)
        elif args.func == 'mqtt':
            mqtt_client = AsyncMQTTClient(args.server, args.port, DEVICE_NAME, device, args.username, args.password)
            await mqtt_client.connect()
            # Schedule the device run loop as a background task.
            run_loop_task = asyncio.create_task(device.run_loop(mqtt_client.publish_state))
            # Instead of waiting indefinitely on an Event, use a periodic sleep loop that checks for shutdown.
            try:
                while not shutdown_triggered:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                LOG.info("Main MQTT loop sleep cancelled. Shutting down gracefully.")
    finally:
        await signal_handler()

###############################################################################
# Main Function and Argument Parsing
###############################################################################
def daemonize():
    """Perform the UNIX double-fork magic to daemonize the process."""
    try:
        # First fork
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"Fork #1 failed: {e.errno} ({e.strerror})\n")
        sys.exit(1)

    os.chdir("/")
    os.setsid()
    os.umask(0)

    try:
        # Second fork
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"Fork #2 failed: {e.errno} ({e.strerror})\n")
        sys.exit(1)

    # Redirect standard file descriptors.
    sys.stdout.flush()
    sys.stderr.flush()
    with open('/dev/null', 'r') as si:
        os.dup2(si.fileno(), sys.stdin.fileno())
    with open('/dev/null', 'a+') as so:
        os.dup2(so.fileno(), sys.stdout.fileno())
    with open('/dev/null', 'a+') as se:
        os.dup2(se.fileno(), sys.stderr.fileno())

def main():
    parser = argparse.ArgumentParser(
        'A utility to interact with USB2LIN06 device asynchronously using libusb1 and gmqtt.'
    )
    subparsers = parser.add_subparsers(help='supported commands', dest='subcommand')
    subparsers.required = True

    parser_status = subparsers.add_parser('status', help='Get status of the device.')
    parser_status.set_defaults(func='status')

    parser_move = subparsers.add_parser('move', help='Move to a specified position.')
    parser_move.add_argument('position', type=int)
    parser_move.set_defaults(func='move')

    parser_mqtt = subparsers.add_parser('mqtt', help='Run in MQTT mode.')
    parser_mqtt.add_argument('--server', help='MQTT server address')
    parser_mqtt.add_argument('--port', type=int, default=1883, help='MQTT server port')
    parser_mqtt.add_argument('--username', help='MQTT username')
    parser_mqtt.add_argument('--password', help='MQTT password')
    parser_mqtt.add_argument('--daemon', action='store_true', help='Run in daemon mode (Linux only)')
    parser_mqtt.set_defaults(func='mqtt')

    group = parser.add_mutually_exclusive_group()
    group.add_argument("-q", "--quiet", help='Decrease verbosity', action="count", default=0)
    group.add_argument("-v", "--verbose", help='Increase verbosity', action="count", default=0)

    args = parser.parse_args()

    # NEW: Load MQTT config options from /etc/linakdesk/config.yaml if present
    if args.func == 'mqtt':
        config_file = CONFIG_FILE
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    config = yaml.safe_load(f)
                args.server = args.server or config.get("server")
                args.port = args.port or config.get("port", args.port)
                args.username = args.username or config.get("username")
                args.password = args.password or config.get("password")
                print(f"Loaded MQTT config from {config_file}")
            except Exception as e:
                LOG.error("Error loading config file: %s", e)
        else:
            LOG.info("Config file %s not found, using command line arguments", config_file)
        
        # Process daemon option only if running in MQTT mode
        if args.daemon:
            if sys.platform.startswith('linux'):
                daemonize()  # Detach process on Linux only.
            else:
                print("--daemon option is only supported on Linux; continuing in normal mode.")

    LOG.set_verbose(args.verbose, args.quiet)
    asyncio.run(async_main(args))

if __name__ == '__main__':
    main()
