==========
linak-mqtt-ctrl
==========

A simple Python script to control Linak powered desks and the USB2LIN06 cable over MQTT, with Home Assistant autodiscovery.

Requirements
============
* Linak desk
* USB2LIN06 device
* Python (>=3.7)
* MQTT server
* Home Assistant (optional)
* `libusb1`_ library
* `gmqtt`_ library

Installation
============
There are several ways to install ``linak-mqtt-ctrl``.

Using virtualenv and pip:
-------------------------
.. code:: shell-session

   $ git clone https://github.com/giantorth/linak-mqtt-ctrl
   $ cd linak-mqtt-ctrl
   $ python -m venv linak
   $ source linak/bin/activate
   (linak) $ pip install .
   (linak) $ linak-mqtt-ctrl status
   Position: 767, height: 78.80cm, moving: False

Install system-wide:
--------------------
.. code:: shell-session

   # sudo pip install linak-mqtt-ctrl

Or install dependencies from your system repositories and place the script in your ``$PATH``.

Building the Package
====================
The project is built using setuptools. To build the package, simply run:

.. code:: shell-session

   $ python setup.py sdist bdist_wheel

The build configuration is defined in [setup.py](setup.py) and [setup.cfg](setup.cfg).

Usage
=====
The script supports three commands: ``status``, ``move`` and ``mqtt``.

Status:
-------
.. code:: shell-session

   $ linak-mqtt-ctrl status
   Position: 767, height: 78.80cm, moving: False

Move:
-----
Adjust the desk height by specifying an absolute position (range: 0-6715):

.. code:: shell-session

   $ linak-mqtt-ctrl move 1000

Increase verbosity for debugging:
.. code:: shell-session

   $ linak-mqtt-ctrl -v move 1000
   $ linak-mqtt-ctrl -vv move 1000

MQTT Mode (Service Mode):
-------------------------
The ``mqtt`` command allows the script to run continuously in service mode,
publishing Home Assistant autodiscovery messages.

Basic usage:
.. code:: shell-session

   $ linak-mqtt-ctrl mqtt --server <MQTT_SERVER> --port <MQTT_PORT> --username <MQTT_USERNAME> --password <MQTT_PASSWORD>

Configuration via File:
-----------------------
When running in MQTT mode, the script will look for a configuration file at
``/etc/linakdesk/config.yaml``. This file can contain MQTT connection options. Example:

.. code:: yaml

   server: "mqtt.example.com"
   port: 1883
   username: "your_username"
   password: "your_password"

Running as a Service
====================
You can run ``linak-mqtt-ctrl`` as a system service by using the provided
service file and installation script.

1.   Review and adjust [linakdesk.service](linakdesk.service) if necessary. Note
     the ``WorkingDirectory`` and ``ExecStart`` paths must point to your application.

2.   Install the service using the provided script:

.. code:: shell-session

   $ chmod +x install_service.sh
   $ sudo ./install_service.sh

This script copies the service file to ``/etc/systemd/system/``, creates a system user,
reloads the systemd configuration, and starts the service. It also adds a udev rule to allow
non-root access to the USB device.

Uninstall or stop the service using standard systemd commands:
.. code:: shell-session

   $ sudo systemctl stop linakdesk.service
   $ sudo systemctl disable linakdesk.service

Alternatives
============

There are three projects, which more or less are doing the same.  This script was heavily inspired by linak-ctrl.

* `usb2lin06-HID-in-linux-for-LINAK-Desk-Control-Cable`_
* `python-linak-desk-control`_
* `linak-ctrl`_

License
=======
This software is licensed under the 3-clause BSD license. See the [LICENSE](LICENSE) file for details.

.. _libusb1: https://github.com/vpelletier/python-libusb1
.. _gmqtt: https://github.com/wialon/gmqtt
.. _usb2lin06-HID-in-linux-for-LINAK-Desk-Control-Cable: https://github.com/UrbanskiDawid/usb2lin06-HID-in-linux-for-LINAK-Desk-Control-Cable
.. _python-linak-desk-control: https://github.com/monofox/python-linak-desk-control
.. _linak-ctrl: https://github.com/gryf/linak-ctrl