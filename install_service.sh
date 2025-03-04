#!/bin/bash
SERVICE_FILE=linakdesk.service
TARGET_PATH=/etc/systemd/system/$SERVICE_FILE

# Copy service file (adjust the source path if needed)
cp $SERVICE_FILE $TARGET_PATH

# Create a system user for the service
useradd --system --no-create-home --shell /usr/sbin/nologin linakdesk

# build and install the python package
pip install . --break-system-packages

# Reload systemd configuration, enable and start the service.
systemctl daemon-reload
systemctl enable linakdesk.service
systemctl start linakdesk.service

echo "Linak Desk Service installed and started."

# Append udev rule so non-root can access the Linak USB device.
UDEV_RULE='SUBSYSTEM=="usb", ATTRS{idVendor}=="12d3", ATTRS{idProduct}=="0002", MODE="0666"'
UDEV_FILE=/etc/udev/rules.d/linak.rules

echo "$UDEV_RULE" | sudo tee -a $UDEV_FILE > /dev/null
udevadm control --reload-rules
echo "Udev rule added to $UDEV_FILE."