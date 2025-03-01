==========
linak-mqtt-ctrl
==========

Simple python script to control Linak powered desks and USB2LIN06 cable over MQTT, with Home Assistant autodiscovery.


Requirements
============

* Linak desk
* USB2LIN06 device
* Python
* MQTT server
* Home Assistant (optional)
* `libusb1`_
* `gmqtt`_


Installation
============

There are a couple of different ways for installing ``linak-mqtt-ctrl``. One of the
preferred ways is to use virtualenv and pip:

.. code:: shell-session

   $ git clone https://github.com/giantorth/linak-mqtt-ctrl
   $ cd linak-mqtt-ctrl
   linak-mqtt-ctrl $ python -m venv linak
   (linak) linak-mqtt-ctrl $ pip install .
   (linak) linak-mqtt-ctrl $ linak-mqtt-ctrl status
   Position: 767, height: 78.80cm, moving: False

Or, you can install it system-wide:

.. code:: shell-session

   # sudo pip install linak-mqtt-ctrl

And finally, you could also install dependencies from your system repositories,
and use the script directly, by placing it somewhere in your ``$PATH``.


Usage
=====

Currently, the script has three available commands: ``status``,  ``move`` and ``mqtt``.

Invoking ``status`` will give information about desk height - both in absolute
number, and in inches/centimeters, and information if the desk is moving.

.. code:: shell-session

   $ linak-mqtt-ctrl status
   Position: 767, height: 78.80cm, moving: False

Note, that height was measured manually and may differ depending if the desk has
casters or regular feet.

Command ``move`` is used for adjusting desk height. It needs the parameter
``position``, which is an absolute number, and its range is between 0 and 6480 (in
my case). For example:

.. code:: shell-session

   $ linak-mqtt-ctrl move 1000

For displaying debug information, verbosity can be increased using the ``--verbose``
parameter:

.. code:: shell-session

   $ linak-mqtt-ctrl -v move 1000
   Current position: 771
   Current position: 792
   Current position: 825
   Current position: 873
   Current position: 939
   Current position: 988
   Current position: 1000

Adding more `-v` will increase the amount of information:

.. code:: shell-session

   $ linak-mqtt-ctrl -vv move 1000
   array('B', [4, 56, 17, 8, 3, 3, 0, 57, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 232, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 0, 0])
   Current position: 771
   array('B', [4, 56, 17, 0, 21, 3, 0, 129, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 232, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 0, 0])
   Current position: 789
   array('B', [4, 56, 17, 0, 55, 3, 0, 205, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 232, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 0, 0])
   Current position: 823
   array('B', [4, 56, 17, 0, 101, 3, 16, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 232, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 0, 0])
   Current position: 869
   array('B', [4, 56, 17, 0, 162, 3, 16, 92, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 232, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 0, 0])
   Current position: 930
   array('B', [4, 56, 17, 0, 217, 3, 0, 170, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 232, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 0, 0])
   Current position: 985
   array('B', [4, 56, 17, 0, 232, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 232, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 0, 0])
   Current position: 1000

Command ``mqtt`` is for running in service mode.

.. code:: shell-session

   $ linak-mqtt-ctrl mqtt --server <MQTT_SERVER> --port <MQTT_PORT> --username <MQTT_USERNAME> --password <MQTT_PASSWORD>
   usage: An utility to interact with USB2LIN06 device. mqtt [-h] --server SERVER [--port PORT] --username USERNAME --password PASSWORD

   Will publish Home Assistant autodiscovery messages to MQTT server as a cover.

Alternatives
============

There are three projects, which more or less are doing the same.  This script was heavily inspired by linak-ctrl.

* `usb2lin06-HID-in-linux-for-LINAK-Desk-Control-Cable`_
* `python-linak-desk-control`_
* `linak-ctrl`_


License
=======

This software is licensed under the 3-clause BSD license. See LICENSE file for
details.


.. _libusb1: https://github.com/vpelletier/python-libusb1
.. _gmqtt: https://github.com/wialon/gmqtt
.. _usb2lin06-HID-in-linux-for-LINAK-Desk-Control-Cable: https://github.com/UrbanskiDawid/usb2lin06-HID-in-linux-for-LINAK-Desk-Control-Cable
.. _python-linak-desk-control: https://github.com/monofox/python-linak-desk-control
.. _linak-ctrl: https://github.com/gryf/linak-ctrl