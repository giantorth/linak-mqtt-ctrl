#!/bin/bash
# filepath: /c:/Users/xboxe/Repos/linak-mqtt-ctrl/install_service.sh

SERVICE_FILE=linakdesk.service
TARGET_PATH=/etc/systemd/system/$SERVICE_FILE

echo "Starting installation of Linak Desk Service..."

# Copy service file (adjust the source path if needed)
echo "Copying $SERVICE_FILE to $TARGET_PATH..."
cp $SERVICE_FILE $TARGET_PATH

# Checking if the system user 'linakdesk' exists before creating it
if id "linakdesk" &>/dev/null; then
    echo "User 'linakdesk' already exists."
else
    echo "Creating system user 'linakdesk'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin linakdesk
fi

# Checking if linak-mqtt-service is running
echo "Checking status of linak-mqtt-service..."
if systemctl is-active --quiet linak-mqtt-service; then
    echo "linak-mqtt-service is running. Stopping it..."
    systemctl stop linak-mqtt-service
    echo "linak-mqtt-service has been stopped."
else
    echo "linak-mqtt-service is not running."
fi

# Build and install the Python package
echo "Installing the Python package..."
pip install . --break-system-packages

# Create /etc/linakdesk directory if it doesn't already exist and change its ownership
CONFIG_DIR="/etc/linakdesk"
CONFIG_FILE="config.yaml"
DEFAULT_CONFIG="./config.yaml"

echo "Creating configuration directory at $CONFIG_DIR (if it doesn't exist)..."
mkdir -p "$CONFIG_DIR"
chown linakdesk:linakdesk "$CONFIG_DIR"

# Copy default config if not present
if [ ! -f "$CONFIG_DIR/$CONFIG_FILE" ]; then
    echo "Copying default config.yaml to $CONFIG_DIR..."
    cp "$DEFAULT_CONFIG" "$CONFIG_DIR/$CONFIG_FILE"
    chown linakdesk:linakdesk "$CONFIG_DIR/$CONFIG_FILE"
    echo "Default config.yaml copied and ownership set to linakdesk."
else
    echo "Configuration file already exists at $CONFIG_DIR/$CONFIG_FILE."
fi

# Append udev rule so non-root can access the Linak USB device
echo "Adding udev rule so non-root users can access the Linak USB device..."
UDEV_RULE='SUBSYSTEM=="usb", ATTRS{idVendor}=="12d3", ATTRS{idProduct}=="0002", MODE="0666"'
UDEV_FILE=/etc/udev/rules.d/linak.rules

echo "$UDEV_RULE" | sudo tee -a $UDEV_FILE > /dev/null
udevadm control --reload-rules
echo "Udev rule added to $UDEV_FILE and rules reloaded."

# Reload systemd configuration, enable and start the service.
echo "Reloading systemd daemon and enabling the Linak Desk Service..."
systemctl daemon-reload
systemctl enable linakdesk.service
systemctl start linakdesk.service

echo "Linak Desk Service installed and started successfully."