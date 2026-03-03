"""USB device communication: StatusReport and AsyncLinakDevice."""

import asyncio
import threading
import usb1

from linak_mqtt_ctrl.logger import LOG
from linak_mqtt_ctrl.config import load_config
from linak_mqtt_ctrl.constants import (
    CONTROL_CBC, REQ_TYPE_GET_INTERFACE, REQ_TYPE_SET_INTERFACE,
    HID_GET_REPORT, HID_SET_REPORT, INIT, MOVE, GET_STATUS,
    BUF_LEN, MODE_OF_OPERATION, MODE_OF_OPERATION_DEFAULT,
    MOVE_UP, MOVE_DOWN, MOVE_STOP,
    DESK_MAX_HEIGHT, DESK_MIN_TRAVEL,
)


class StatusReport:
    """Parse a raw USB response into position and movement status.

    The raw response contains absolute position values that range from 0 to 6715.
    Height calculations are based on two calibration points provided during initialization.

    Example usage:
        # Using inches
        calibration = {
            'unit': 'in',
            'point1': {'raw': 0, 'height': 25.61},
            'point2': {'raw': DESK_MAX_HEIGHT, 'height': 51.61}
        }

        # Using centimeters
        calibration = {
            'unit': 'cm',
            'point1': {'raw': 0, 'height': 67},
            'point2': {'raw': DESK_MAX_HEIGHT, 'height': 132}
        }
    """

    _DEFAULT_CALIBRATION = {
        'unit': 'in',
        'point1': {'raw': 0, 'height': 25.61},
        'point2': {'raw': DESK_MAX_HEIGHT, 'height': 51.61}
    }

    def __init__(self, raw_response, calibration=None):
        self._calibration = calibration if calibration else self._DEFAULT_CALIBRATION

        # Determine if the device is moving (based on the 7th byte in the response)
        self.moving = raw_response[6] > 0

        # Combine two bytes to form the raw position value
        self.position = raw_response[4] + (raw_response[5] << 8)

        # Calculate heights based on calibration
        self._calculate_heights()

        LOG.info(f"Linak Position: {self.position}, Moving: {self.moving}")

    def _calculate_heights(self):
        """Calculate heights based on calibration data and current position."""
        height = self._interpolate_height(
            self.position,
            self._calibration['point1']['raw'],
            self._calibration['point1']['height'],
            self._calibration['point2']['raw'],
            self._calibration['point2']['height']
        )

        if self._calibration['unit'] == 'in':
            self.position_in_in = round(height, 2)
            self.position_in_cm = round(height * 2.54, 2)
        else:  # cm
            self.position_in_cm = round(height, 2)
            self.position_in_in = round(height / 2.54, 2)

    def _interpolate_height(self, raw_pos, raw1, height1, raw2, height2):
        """Linear interpolation between two calibration points."""
        if raw2 == raw1:
            return height1
        return height1 + (raw_pos - raw1) * (height2 - height1) / (raw2 - raw1)


class AsyncLinakDevice:
    """Asynchronous USB device controller using libusb1 async control transfers.

    Uses asyncio.wait_for to enforce timeouts on USB transfers, preventing
    hanging calls from blocking the event loop. A dedicated thread processes
    USB events. A stop flag allows halting any ongoing movement immediately.
    """

    VEND = 0x12d3
    PROD = 0x0002

    def __init__(self, context, handle, loop):
        self.context = context
        self.handle = handle
        self.loop = loop
        self._shutdown = False
        self._stop_requested = False
        self.min_travel = DESK_MIN_TRAVEL
        # Dedicated thread to process USB events.
        self.event_thread = threading.Thread(target=self._handle_events_loop, daemon=True)
        self.event_thread.start()

    @classmethod
    async def create(cls, loop):
        """Create and initialize the device."""
        context = usb1.USBContext()
        handle = context.openByVendorIDAndProductID(cls.VEND, cls.PROD, skip_on_error=True)
        if handle is None:
            raise ValueError(f"Device {cls.VEND}:{cls.PROD:04d} not found!")
        try:
            if handle.kernelDriverActive(0):
                handle.detachKernelDriver(0)
        except Exception as e:
            LOG.debug("Could not detach kernel driver: %s", e)

        handle.claimInterface(0)

        device = cls(context, handle, loop)

        config = load_config()
        device.min_travel = config.get("desk_min_travel", DESK_MIN_TRAVEL)
        LOG.info("Loaded desk_min_travel from config: %s", device.min_travel)

        await device._init_device()
        return device

    async def _init_device(self):
        """Send the initialization command to the device."""
        buf = bytearray(BUF_LEN)
        buf[0] = MODE_OF_OPERATION
        buf[1] = MODE_OF_OPERATION_DEFAULT
        buf[2] = 0x00
        buf[3] = 0xfb
        await self.async_ctrl_transfer(REQ_TYPE_SET_INTERFACE, HID_SET_REPORT, INIT, 0, buf)
        await asyncio.sleep(0.5)

    def _handle_events_loop(self):
        """Run in a dedicated thread to continuously process USB events."""
        while not self._shutdown:
            try:
                self.context.handleEventsTimeout(0.1)
            except Exception as e:
                LOG.error("USB event loop error: %s", e)
                break

    async def async_ctrl_transfer(self, request_type, request, value, index, data, timeout=1000):
        """Wrap an asynchronous USB control transfer with timeout enforcement.

        If the timeout is exceeded, the underlying USB transfer is explicitly
        cancelled to avoid resource leaks.
        """
        future = self.loop.create_future()
        transfer = self.handle.getTransfer()

        def callback(transfer):
            status = transfer.getStatus()
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

        try:
            return await asyncio.wait_for(future, timeout=timeout / 1000)
        except asyncio.TimeoutError:
            try:
                transfer.cancel()
                LOG.error("Cancelled USB transfer due to timeout.")
            except Exception as e:
                LOG.error("Error cancelling USB transfer: %s", e)
            raise

    async def get_position(self):
        """Retrieve the current device position asynchronously."""
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
        """Move the device to the desired position.

        Repeatedly sends move commands and polls until the target is reached,
        retries are exhausted, or a stop is requested.
        """
        self._stop_requested = False
        retry_count = 3
        previous_position = 0

        if position < self.min_travel:
            position = self.min_travel

        LOG.info("Moving to position: %s", position)
        while not self._stop_requested:
            await self._move(position)
            await asyncio.sleep(0.2)
            raw = await self.async_ctrl_transfer(
                REQ_TYPE_GET_INTERFACE, HID_GET_REPORT, GET_STATUS, 0, bytearray(BUF_LEN)
            )
            status_report = StatusReport(raw)
            LOG.info("Current position: %s", status_report.position)
            if self._stop_requested:
                LOG.info("Stop requested, halting move loop.")
                break
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
        await self._move(MOVE_STOP)

    async def _move(self, position):
        """Send the MOVE command over USB."""
        if position < self.min_travel:
            position = self.min_travel

        buf = bytearray(BUF_LEN)
        pos_l = position & 0xFF
        pos_h = (position >> 8) & 0xFF
        buf[0] = CONTROL_CBC
        buf[1] = buf[3] = buf[5] = buf[7] = pos_l
        buf[2] = buf[4] = buf[6] = buf[8] = pos_h
        await self.async_ctrl_transfer(REQ_TYPE_SET_INTERFACE, HID_SET_REPORT, MOVE, 0, buf)

    async def stop_movement(self):
        """Halt any ongoing movement by setting the stop flag and sending STOP."""
        LOG.info("Stop command received: setting stop flag and sending stop command.")
        self._stop_requested = True
        await self._move(MOVE_STOP)

    def shutdown(self):
        """Shut down the USB event loop thread and close the USB context."""
        LOG.debug("Shutting down Linak device...")
        self._shutdown = True
        self.event_thread.join()
        try:
            self.handle.close()
        except Exception as e:
            LOG.error("Error closing USB handle: %s", e)
        try:
            self.context.close()
        except Exception as e:
            LOG.error("Error closing USB context: %s", e)

    def convert_position_to_percent(self, position):
        """Convert a raw position value to a percentage (0-100)."""
        return int((position / DESK_MAX_HEIGHT) * 100)

    async def run_loop(self, publish_callback, poll_interval=1):
        """Continuously monitor the device state and publish on changes.

        Schedules get_position as a separate task with a 0.5s timeout so
        that a hanging USB call does not block the event loop.
        """
        last_state = None
        force_publish = True
        while not self._shutdown:
            await asyncio.sleep(poll_interval)
            try:
                get_position_task = asyncio.create_task(self.get_position())
                report = await asyncio.wait_for(get_position_task, timeout=0.5)
            except asyncio.TimeoutError:
                LOG.error("get_position task timed out; skipping this poll cycle.")
                get_position_task.cancel()
                continue
            except Exception as e:
                LOG.error("Error during get_position: %s", e)
                continue
            if report is not None:
                position_percent = self.convert_position_to_percent(report.position)
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
