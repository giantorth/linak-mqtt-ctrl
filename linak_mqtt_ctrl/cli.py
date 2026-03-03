"""CLI entry point: argument parsing, async_main, and daemonize."""

import argparse
import asyncio
import os
import sys
import signal

from linak_mqtt_ctrl.logger import LOG, set_log_verbose
from linak_mqtt_ctrl.config import load_config
from linak_mqtt_ctrl.constants import DEVICE_NAME, CONFIG_FILE
from linak_mqtt_ctrl.device import AsyncLinakDevice
from linak_mqtt_ctrl.mqtt import AsyncMQTTClient


async def async_main(args):
    """Main asynchronous entry point that creates the device and dispatches subcommands."""
    try:
        device = await AsyncLinakDevice.create(asyncio.get_running_loop())
    except ValueError as e:
        LOG.error("%s", e)
        LOG.error("Make sure the USB2LIN06 cable is connected and you have permission to access it.")
        return

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

        if mqtt_client:
            try:
                await mqtt_client.disconnect()
                await mqtt_client.cleanup()
            except Exception as e:
                LOG.error("Error during MQTT disconnect/cleanup: %s", e)

        if run_loop_task:
            run_loop_task.cancel()
            try:
                await run_loop_task
            except asyncio.CancelledError:
                LOG.info("Device run loop task cancelled.")

        await asyncio.sleep(0.5)

        try:
            device.shutdown()
        except Exception as e:
            LOG.error("Error during device shutdown: %s", e)

        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(signal_handler()))

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
            mqtt_client = AsyncMQTTClient(
                args.server, args.port, DEVICE_NAME, device,
                args.username, args.password
            )
            await mqtt_client.connect()
            run_loop_task = asyncio.create_task(device.run_loop(mqtt_client.publish_state))
            try:
                while not shutdown_triggered:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                LOG.info("Main MQTT loop sleep cancelled. Shutting down gracefully.")
    finally:
        if args.func == 'mqtt':
            await signal_handler()
        else:
            device.shutdown()


def daemonize():
    """Perform the UNIX double-fork to daemonize the process."""
    try:
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
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"Fork #2 failed: {e.errno} ({e.strerror})\n")
        sys.exit(1)

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

    if args.func == 'mqtt':
        config = load_config()
        args.server = args.server or config.get("server")
        args.port = args.port or config.get("port", args.port)
        args.username = args.username or config.get("username")
        args.password = args.password or config.get("password")

        if not args.server:
            parser.error(f"MQTT server address is required. Provide via --server or in {CONFIG_FILE}")

        if args.daemon:
            if sys.platform.startswith('linux'):
                daemonize()
            else:
                print("--daemon option is only supported on Linux; continuing in normal mode.")

    set_log_verbose(LOG, args.verbose, args.quiet)
    asyncio.run(async_main(args))
