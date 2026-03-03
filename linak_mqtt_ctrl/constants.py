"""Constants for USB communication, MQTT topics, and device configuration."""

__version__ = "1.0.0"

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

# Device constants
DEVICE_NAME = "Standing Desk"
DESK_MAX_HEIGHT = 6715
DESK_MIN_TRAVEL = 0

# Configuration file path
CONFIG_FILE = "/etc/linakdesk/config.yaml"

# MQTT topic layout
TOPIC_PREFIX = "linak/desk"
ENTITY_ID = "linak_desk"
STATE_TOPIC = f"{TOPIC_PREFIX}/{ENTITY_ID}/state"
COMMAND_TOPIC = f"{TOPIC_PREFIX}/{ENTITY_ID}/set"
AVAILABILITY_TOPIC = f"{TOPIC_PREFIX}/{ENTITY_ID}/availability"
LOCK_COMMAND_TOPIC = f"{TOPIC_PREFIX}/lock/set"
LOCK_STATE_TOPIC = f"{TOPIC_PREFIX}/lock/state"
PRESET_TOPIC_PREFIX = f"{TOPIC_PREFIX}/preset"
MIN_TRAVEL_STATE_TOPIC = f"{TOPIC_PREFIX}/desk_min_travel/state"
MIN_TRAVEL_COMMAND_TOPIC = f"{TOPIC_PREFIX}/desk_min_travel/set"
HA_STATUS_TOPIC = "homeassistant/status"
HA_DISCOVERY_TOPIC_PREFIX = "homeassistant/device"
