#!/usr/bin/env python
import argparse
import time
import sys

from logger import Logger
from mqtt import MqttService
from linak import LinakDevice, StatusReport

LOG = Logger.get_logger(__name__)

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

    Logger.set_verbose(args.verbose, args.quiet) # Correct call

    if args.subcommand == "mqtt":
        mqtt_service = MqttService(device, args.server, args.port, args.username, args.password)
        mqtt_service.connect()
        try:
            while True:
                mqtt_service.publish_state()
                time.sleep(2)
        except KeyboardInterrupt:
            mqtt_service.disconnect()
            return

    args.func(args)


if __name__ == '__main__':
    main()
