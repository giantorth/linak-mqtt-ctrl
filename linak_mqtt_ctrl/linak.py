import usb.core
import usb.util
import array
import time
from logger import Logger

LOG = Logger.get_logger(__name__)

from constants import HA_MIN_POS, HA_MAX_POS, LINAK_MIN_POS, LINAK_MAX_POS, MODE_OF_OPERATION, MODE_OF_OPERATION_DEFAULT, MOVE, REQ_TYPE_GET_INTERFACE,REQ_TYPE_SET_INTERFACE, HID_GET_REPORT, HID_SET_REPORT, INIT, GET_STATUS, BUF_LEN, CONTROL_CBC

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