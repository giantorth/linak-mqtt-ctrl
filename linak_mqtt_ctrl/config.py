"""Configuration loading and saving for /etc/linakdesk/config.yaml."""

import os
import yaml

from linak_mqtt_ctrl.logger import LOG
from linak_mqtt_ctrl.constants import CONFIG_FILE, DESK_MIN_TRAVEL


def load_config():
    """Load configuration from CONFIG_FILE. Returns a dict."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                config = yaml.safe_load(f) or {}
        else:
            LOG.info("Configuration file %s does not exist. Using empty config.", CONFIG_FILE)
            config = {}
        config.setdefault("desk_min_travel", DESK_MIN_TRAVEL)
        LOG.debug("Configuration loaded from %s: %s", CONFIG_FILE, config)
        return config
    except Exception as e:
        LOG.error("Error reading configuration file %s: %s", CONFIG_FILE, e)
        return {"desk_min_travel": DESK_MIN_TRAVEL}


def save_config(config):
    """Save configuration dict to CONFIG_FILE."""
    try:
        if "desk_min_travel" not in config:
            config["desk_min_travel"] = DESK_MIN_TRAVEL
        with open(CONFIG_FILE, 'w') as f:
            yaml.safe_dump(config, f)
        LOG.debug("Configuration saved to %s: %s", CONFIG_FILE, config)
    except Exception as e:
        LOG.error("Error saving configuration file %s: %s", CONFIG_FILE, e)
