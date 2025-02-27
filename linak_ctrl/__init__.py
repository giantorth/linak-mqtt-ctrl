#!/usr/bin/env python
import argparse
import array
import logging
import time
import sys
import json
import threading
import queue

import usb.core
import usb.util
import paho.mqtt.client as mqtt

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

# Linak min/max values
LINAK_MIN_POS = 0
LINAK_MAX_POS = 6715
# Home assistant min/max values
HA_MIN_POS = 0
HA_MAX_POS = 100


class Logger:
    """
    Simple logger class with output on console only
    """

    def __init__(self, logger_name):
        """
        Initialize named logger
        """
        self._log = logging.getLogger(logger_name)
        self.setup_logger()
        self._log.set_verbose = self.set_verbose

    def get_logger(self):
        """
        Return the configured logging.Logger object
        """
        return self._log

    def set_verbose(self, verbose_level, quiet_level):
        """
        Change verbosity level. Default level is warning.
        """
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
        Create setup instance and make output meaningful :)
        """
        if self._log.handlers:
            # need only one handler
            return

        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.set_name("console")
        console_formatter = logging.Formatter("%(levelname)s: %(message)s")
        console_handler.setFormatter(console_formatter)
        self._log.addHandler(console_handler)
        self._log.setLevel(logging.WARNING)


LOG = Logger(__name__).get_logger()


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

    1658→32 inches
    6715→51.5 inches

        postition_in_in = 0.003856 * actual_read_position + 25.61

    """

    def __init__(self, raw_response):
        self.moving = raw_response[6] > 0
        self.position = raw_response[4] + (raw_response[5] << 8)
        self.position_in_cm = self.position / 65 + 67
        self.position_in_in = (self.position * 0.003856) + 25.61
        self.ha_position = int(
            ((self.position - LINAK_MIN_POS) / (LINAK_MAX_POS - LINAK_MIN_POS))
            * (HA_MAX_POS - HA_MIN_POS)
            + HA_MIN_POS
        )
        LOG.info(f"Linak Position: {self.position}, HA Position: {self.ha_position}, Moving: {self.moving}")


class LinakDevice:
    """
    Class representing USB interface for Linak controller USB2LIN06
    """

    VEND = 0x12d3
    PROD = 0x0002

    def __init__(self):
        self._dev = usb.core.find(idVendor=LinakDevice.VEND,
                                  idProduct=LinakDevice.PROD)
        if not self._dev:
            raise ValueError(f'Device {LinakDevice.VEND}:'
                             f'{LinakDevice.PROD:04d} '
                             f'not found!')

        # detach kernel driver, if attached
        if self._dev.is_kernel_driver_active(0):
            self._dev.detach_kernel_driver(0)

        # init device
        buf = [0 for _ in range(BUF_LEN)]
        buf[0] = MODE_OF_OPERATION          # 0x03 Feature report ID = 3
        buf[1] = MODE_OF_OPERATION_DEFAULT  # 0x04 mode of operation
        buf[2] = 0x00                       # ?
        buf[3] = 0xfb                       # ?
        self._dev.ctrl_transfer(REQ_TYPE_SET_INTERFACE, HID_SET_REPORT, INIT,
                                0, array.array('B', buf))
        self._is_moving = False
        # hold a little bit, to make it effect.
        time.sleep(0.5)

    def is_moving(self):
        return self._is_moving
    
    def get_position(self, args):
        try:
            while True:
                report = self._get_report()
                LOG.warning('Position: %s, height: %.2fin (%.2f), moving: %s',
                            report.position, report.position_in_in, report.position_in_cm,
                            report.moving)
                if not args.loop:
                    break
                time.sleep(0.2)
        except KeyboardInterrupt:
            return

    def move(self, target_position):
        self._is_moving = True  # Set moving flag
        retry_count = 3  # Increased retry count
        previous_position = -1

        while True:
            self._move(target_position)
            time.sleep(0.2) 

            status_report = self._get_report()
            LOG.info(f"Current position: {status_report.position}, Target: {target_position}")

            if status_report.position == target_position:
                LOG.info("Reached target position.")
                self._is_moving = False  # Clear moving flag
                break

            if previous_position == status_report.position:
                LOG.debug(f"Position is same as previous one: {previous_position}, retry count: {retry_count}")
                retry_count -= 1

            previous_position = status_report.position

            if retry_count == 0:
                LOG.debug("Retry has reached its threshold. Stop moving.")
                self._is_moving = False  # Clear moving flag
                break

    def _get_report(self):
        raw = self._dev.ctrl_transfer(REQ_TYPE_GET_INTERFACE, HID_GET_REPORT,
                                      GET_STATUS, 0, BUF_LEN)
        LOG.debug(f"Raw report: {raw}")
        return StatusReport(raw)

    def _move(self, position):
        buf = [0 for _ in range(BUF_LEN)]
        pos = "%04x" % position  # for example: 0x02ff
        pos_l = int(pos[2:], 16)  # 0xff
        pos_h = int(pos[:2], 16)  # 0x02

        buf[0] = CONTROL_CBC
        # For my desk controller, seting position bytes on indexes 1 and 2 are
        # effective, the other does nothing in my case, although there might
        # be some differences on other hw.
        buf[1] = buf[3] = buf[5] = buf[7] = pos_l
        buf[2] = buf[4] = buf[6] = buf[8] = pos_h
        LOG.debug(f"Moving to: {position}. Buffer: {buf}")
        self._dev.ctrl_transfer(REQ_TYPE_SET_INTERFACE, HID_SET_REPORT, MOVE,
                                0, array.array('B', buf))

    def set_ha_position(self, ha_position):
        linak_position = int(
            ((ha_position - HA_MIN_POS) / (HA_MAX_POS - HA_MIN_POS))
            * (LINAK_MAX_POS - LINAK_MIN_POS)
            + LINAK_MIN_POS
        )
        LOG.info(f"Set HA position: {ha_position}, Linak position: {linak_position}")
        self.move(linak_position)


class MqttService:
    def __init__(self, linak_device, server, port, username, password, client_id="linak_desk"):
        self.linak_device = linak_device
        self.client = mqtt.Client(client_id=client_id)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
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
        else:
            LOG.error("Failed to connect, return code %d\n", rc)


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
            command = self.command_queue.get()
            if command[0] == "position":
                self.linak_device.set_ha_position(command[1])
                self.publish_state()
            elif command[0] == "stop":
                # TODO: Implement stop functionality
                pass
            self.command_queue.task_done()

    def send_discovery(self):
        config_topic = f"homeassistant/cover/{self.entity_id}/config"
        config_payload = {
            "name": self.desk_name,
            "unique_id": self.entity_id,
            "position_topic": self.state_topic,
            "set_position_topic": self.command_topic,
            "availability": {
                "topic": self.availability_topic,
                "value_template": '{{ value_json.position }}'
            },
            "payload_open": "OPEN",
            "payload_close": "CLOSE",
            "payload_stop": "STOP",
            "position_open": 100,
            "position_closed": 0,
            "set_position_template": '{"position": {{value}}}',
            "device_class": "none",
            "command_topic": self.command_topic,
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
        if not self.linak_device.is_moving():  # Only publish state if not moving
            report = self.linak_device._get_report()
            state_payload = {"position": report.ha_position, "moving": report.moving}
            LOG.info(f"Sending state message to: {self.state_topic} : {state_payload}")
            self.client.publish(self.state_topic, json.dumps(state_payload), retain=True)

    def connect(self):
        try:
            LOG.info(f"Connecting to MQTT server: {self.server}:{self.port}")
            self.client.connect(self.server, self.port, 60)
            self.client.loop_start()
            self.send_availability("online")
            while self.connected == False:
                LOG.info("Waiting for MQTT connection")
                time.sleep(1)
            time.sleep(1)  # small delay for connection to complete
        except Exception as ex:
            LOG.error(f"Error connecting to MQTT server: {ex}")

    def disconnect(self):
        self.send_availability("offline")
        self.client.disconnect()
        self.client.loop_stop()

def main():
    try:
        device = LinakDevice()
    except ValueError as ex:
        sys.stderr.write(ex.args[0] + '\n')
        sys.exit(1)

    parser = argparse.ArgumentParser('An utility to interact with USB2LIN06 '
                                     'device.')
    subparsers = parser.add_subparsers(help='supported commands',
                                       dest='subcommand')
    subparsers.required = True
    parser_status = subparsers.add_parser('status', help='get status of the '
                                          'device.')
    parser_status.add_argument('-l', '--loop', help='run indefinitely, use '
                               'ctrl-c to stop.', action="store_true")
    parser_status.set_defaults(func=device.get_position)
    parser_move = subparsers.add_parser('move', help='move to the desired '
                                        'height. Note, that height need to be '
                                        'provided as reported by status.')
    parser_move.add_argument('position', type=int)
    parser_move.set_defaults(func=lambda args: device.move(args.position))

    parser_mqtt = subparsers.add_parser('mqtt', help='run as a mqtt service')
    parser_mqtt.add_argument('--server', required=True, help='MQTT server address')
    parser_mqtt.add_argument('--port', type=int, default=1883, help='MQTT server port')
    parser_mqtt.add_argument('--username', required=True, help='MQTT username')
    parser_mqtt.add_argument('--password', required=True, help='MQTT password')
    parser_mqtt.set_defaults(func=None)  # will handle it differently

    group = parser.add_mutually_exclusive_group()
    group.add_argument("-q", "--quiet", help='please, be quiet. Adding more '
                       '"q" will decrease verbosity', action="count",
                       default=0)
    group.add_argument("-v", "--verbose", help='be verbose. Adding more "v" '
                       'will increase verbosity', action="count", default=0)
    args = parser.parse_args()

    LOG.set_verbose(args.verbose, args.quiet)

    if args.subcommand == "mqtt":
        mqtt_service = MqttService(device, args.server, args.port, args.username, args.password)
        mqtt_service.connect()
        try:
            while True:
                mqtt_service.publish_state()
                time.sleep(10)
        except KeyboardInterrupt:
            mqtt_service.disconnect()
            return

    args.func(args)


if __name__ == '__main__':
    main()
