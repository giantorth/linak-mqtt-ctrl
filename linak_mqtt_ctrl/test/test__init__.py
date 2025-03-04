import asyncio
import pytest
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from linak_mqtt_ctrl import async_main, AsyncLinakDevice, AsyncMQTTClient, StatusReport

# Language: python


# Relative import of the function and classes to test.

# -----------------------------------------------------------------------------
# Dummy Classes to simulate device and MQTT client behavior
# -----------------------------------------------------------------------------
class DummyStatusReport:
    def __init__(self):
        # Create a dummy raw response that gives a position of 100 and not moving.
        # raw[4] and raw[5] form the position and raw[6] indicates moving (>0 means moving)
        raw = bytearray(64)
        raw[4] = 100  # LSB
        raw[5] = 0    # MSB: position = 100
        raw[6] = 0    # not moving
        self.report = StatusReport(raw)

    @property
    def position(self):
        return self.report.position

    @property
    def position_in_cm(self):
        return self.report.position_in_cm

    @property
    def moving(self):
        return self.report.moving

class DummyDevice:
    def __init__(self):
        self.shutdown_called = False
        self.get_position_called = False

    async def get_position(self):
        self.get_position_called = True
        # Return a dummy status report.
        return DummyStatusReport().report

    async def _move(self, position):
        # Dummy move: do nothing.
        pass

    async def move(self, position):
        # Call _move and simulate a sleep.
        await self._move(position)
        await asyncio.sleep(0.01)
        return await self.get_position()

    async def run_loop(self, publish_callback, poll_interval=1):
        # Simulate a short run loop that runs one cycle then returns.
        await asyncio.sleep(0.2)
        return

    def shutdown(self):
        self.shutdown_called = True

    @classmethod
    async def create(cls, loop):
        # Return an instance immediately.
        return DummyDevice()

class DummyMQTTClient(AsyncMQTTClient):
    def __init__(self, broker, port, device_name, async_device, username=None, password=None):
        # Do not call parent's __init__ to avoid real MQTT client creation.
        self.device_name = device_name
        self.async_device = async_device
        self.disconnect_called = False
        self.cleanup_called = False

    async def connect(self):
        # Simulate successful connection.
        await asyncio.sleep(0.01)

    async def disconnect(self):
        self.disconnect_called = True
        await asyncio.sleep(0.01)

    async def cleanup(self):
        self.cleanup_called = True
        await asyncio.sleep(0.01)

    def publish_state(self, payload):
        pass

# -----------------------------------------------------------------------------
# Dummy Args classes for testing different subcommands.
# -----------------------------------------------------------------------------
class DummyArgs:
    def __init__(self, func, position=None, server=None, port=None, username=None, password=None):
        self.func = func
        # For compatibility with argparse, use attribute subcommand if needed.
        self.subcommand = func
        self.position = position
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        # For verbosity settings.
        self.verbose = 0
        self.quiet = 0

# -----------------------------------------------------------------------------
# Tests for signal_handler through async_main cleanup behavior.
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_signal_handler_status(monkeypatch):
    # Replace AsyncLinakDevice.create with the dummy device.
    dummy_device = DummyDevice()
    async def dummy_create(loop):
        return dummy_device
    monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)

    # Build dummy args for 'status' command.
    args = DummyArgs(func='status')
    # Run async_main. Since the 'status' branch finishes quickly, the finally block
    # (which calls the inner signal_handler) should call device.shutdown().
    await async_main(args)
    assert dummy_device.get_position_called
    assert dummy_device.shutdown_called

@pytest.mark.asyncio
async def test_signal_handler_mqtt(monkeypatch):
    # Prepare a dummy device instance.
    dummy_device = DummyDevice()
    async def dummy_create(loop):
        return dummy_device
    monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)

    # Replace AsyncMQTTClient methods with DummyMQTTClient ones.
    monkeypatch.setattr(AsyncMQTTClient, "__init__", DummyMQTTClient.__init__)
    monkeypatch.setattr(AsyncMQTTClient, "connect", DummyMQTTClient.connect)
    monkeypatch.setattr(AsyncMQTTClient, "disconnect", DummyMQTTClient.disconnect)
    monkeypatch.setattr(AsyncMQTTClient, "cleanup", DummyMQTTClient.cleanup)

    # Build dummy args for 'mqtt' command.
    args = DummyArgs(
        func='mqtt',
        server='dummy_broker',
        port=1883,
        username='test',
        password='test'
    )

    # Run async_main in a task and cancel it after a short delay to simulate shutdown.
    task = asyncio.create_task(async_main(args))
    await asyncio.sleep(0.3)  # Let the main loop start.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Verify that shutdown was called.
    assert dummy_device.shutdown_called

@pytest.mark.asyncio
async def test_move_command(monkeypatch):
    # Prepare a dummy device instance.
    dummy_device = DummyDevice()
    async def dummy_create(loop):
        return dummy_device
    monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)

    # Build dummy args for 'move' command.
    args = DummyArgs(func='move', position=100)

    # Run async_main.
    await async_main(args)
    assert dummy_device.get_position_called

@pytest.mark.asyncio
async def test_mqtt_connection_and_message_handling(monkeypatch):
    # Prepare a dummy device instance.
    dummy_device = DummyDevice()
    async def dummy_create(loop):
        return dummy_device
    monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)

    # Replace AsyncMQTTClient methods with DummyMQTTClient ones.
    monkeypatch.setattr(AsyncMQTTClient, "__init__", DummyMQTTClient.__init__)
    monkeypatch.setattr(AsyncMQTTClient, "connect", DummyMQTTClient.connect)
    monkeypatch.setattr(AsyncMQTTClient, "disconnect", DummyMQTTClient.disconnect)
    monkeypatch.setattr(AsyncMQTTClient, "cleanup", DummyMQTTClient.cleanup)

    # Build dummy args for 'mqtt' command.
    args = DummyArgs(
        func='mqtt',
        server='dummy_broker',
        port=1883,
        username='test',
        password='test'
    )

    # Run async_main in a task and cancel it after a short delay to simulate shutdown.
    task = asyncio.create_task(async_main(args))
    await asyncio.sleep(0.3)  # Let the main loop start.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Verify that shutdown was called.
    assert dummy_device.shutdown_called

@pytest.mark.asyncio
async def test_device_run_loop(monkeypatch):
    # Prepare a dummy device instance.
    # Dummy Classes to simulate device and MQTT client behavior
    class DummyStatusReport:
        def __init__(self):
            raw = bytearray(64)
            raw[4] = 100  # LSB
            raw[5] = 0    # MSB: position = 100
            raw[6] = 0    # not moving
            self.report = StatusReport(raw)

        @property
        def position(self):
            return self.report.position

        @property
        def position_in_cm(self):
            return self.report.position_in_cm

        @property
        def moving(self):
            return self.report.moving

    class DummyDevice:
        def __init__(self):
            self.shutdown_called = False
            self.get_position_called = False

        async def get_position(self):
            self.get_position_called = True
            return DummyStatusReport().report

        async def _move(self, position):
            pass

        async def move(self, position):
            await self._move(position)
            await asyncio.sleep(0.01)
            return await self.get_position()

        async def run_loop(self, publish_callback, poll_interval=1):
            await asyncio.sleep(0.2)
            return

        def shutdown(self):
            self.shutdown_called = True

        @classmethod
        async def create(cls, loop):
            return DummyDevice()

    class DummyMQTTClient(AsyncMQTTClient):
        def __init__(self, broker, port, device_name, async_device, username=None, password=None):
            self.device_name = device_name
            self.async_device = async_device
            self.disconnect_called = False
            self.cleanup_called = False

        async def connect(self):
            await asyncio.sleep(0.01)

        async def disconnect(self):
            self.disconnect_called = True
            await asyncio.sleep(0.01)

        async def cleanup(self):
            self.cleanup_called = True
            await asyncio.sleep(0.01)

        def publish_state(self, payload):
            pass

    class DummyArgs:
        def __init__(self, func, position=None, server=None, port=None, username=None, password=None):
            self.func = func
            self.subcommand = func
            self.position = position
            self.server = server
            self.port = port
            self.username = username
            self.password = password
            self.verbose = 0
            self.quiet = 0

    # Existing tests
    @pytest.mark.asyncio
    async def test_signal_handler_status(monkeypatch):
        dummy_device = DummyDevice()
        async def dummy_create(loop):
            return dummy_device
        monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)
        args = DummyArgs(func='status')
        await async_main(args)
        assert dummy_device.get_position_called
        assert dummy_device.shutdown_called

    @pytest.mark.asyncio
    async def test_signal_handler_mqtt(monkeypatch):
        dummy_device = DummyDevice()
        async def dummy_create(loop):
            return dummy_device
        monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)
        monkeypatch.setattr(AsyncMQTTClient, "__init__", DummyMQTTClient.__init__)
        monkeypatch.setattr(AsyncMQTTClient, "connect", DummyMQTTClient.connect)
        monkeypatch.setattr(AsyncMQTTClient, "disconnect", DummyMQTTClient.disconnect)
        monkeypatch.setattr(AsyncMQTTClient, "cleanup", DummyMQTTClient.cleanup)
        args = DummyArgs(
            func='mqtt',
            server='dummy_broker',
            port=1883,
            username='test',
            password='test'
        )
        task = asyncio.create_task(async_main(args))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert dummy_device.shutdown_called

    @pytest.mark.asyncio
    async def test_move_command(monkeypatch):
        dummy_device = DummyDevice()
        async def dummy_create(loop):
            return dummy_device
        monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)
        args = DummyArgs(func='move', position=100)
        await async_main(args)
        assert dummy_device.get_position_called

    @pytest.mark.asyncio
    async def test_mqtt_connection_and_message_handling(monkeypatch):
        dummy_device = DummyDevice()
        async def dummy_create(loop):
            return dummy_device
        monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)
        monkeypatch.setattr(AsyncMQTTClient, "__init__", DummyMQTTClient.__init__)
        monkeypatch.setattr(AsyncMQTTClient, "connect", DummyMQTTClient.connect)
        monkeypatch.setattr(AsyncMQTTClient, "disconnect", DummyMQTTClient.disconnect)
        monkeypatch.setattr(AsyncMQTTClient, "cleanup", DummyMQTTClient.cleanup)
        args = DummyArgs(
            func='mqtt',
            server='dummy_broker',
            port=1883,
            username='test',
            password='test'
        )
        task = asyncio.create_task(async_main(args))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert dummy_device.shutdown_called

    @pytest.mark.asyncio
    async def test_device_run_loop(monkeypatch):
        dummy_device = DummyDevice()
        async def dummy_create(loop):
            return dummy_device
        monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)
        monkeypatch.setattr(AsyncMQTTClient, "__init__", DummyMQTTClient.__init__)
        monkeypatch.setattr(AsyncMQTTClient, "connect", DummyMQTTClient.connect)
        monkeypatch.setattr(AsyncMQTTClient, "disconnect", DummyMQTTClient.disconnect)
        monkeypatch.setattr(AsyncMQTTClient, "cleanup", DummyMQTTClient.cleanup)
        args = DummyArgs(
            func='mqtt',
            server='dummy_broker',
            port=1883,
            username='test',
            password='test'
        )
        task = asyncio.create_task(async_main(args))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert dummy_device.shutdown_called

    # Additional tests
    @pytest.mark.asyncio
    async def test_mqtt_client_cleanup(monkeypatch):
        dummy_device = DummyDevice()
        async def dummy_create(loop):
            return dummy_device
        monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)

        # Capture instance of DummyMQTTClient for later assertions.
        mqtt_instances = []
        def dummy_mqtt_init(self, broker, port, device_name, async_device, username=None, password=None):
            DummyMQTTClient.__init__(self, broker, port, device_name, async_device, username, password)
            mqtt_instances.append(self)
        monkeypatch.setattr(AsyncMQTTClient, "__init__", dummy_mqtt_init)
        monkeypatch.setattr(AsyncMQTTClient, "connect", DummyMQTTClient.connect)
        monkeypatch.setattr(AsyncMQTTClient, "disconnect", DummyMQTTClient.disconnect)
        monkeypatch.setattr(AsyncMQTTClient, "cleanup", DummyMQTTClient.cleanup)

        args = DummyArgs(
            func='mqtt',
            server='dummy_broker',
            port=1883,
            username='test',
            password='test'
        )
        task = asyncio.create_task(async_main(args))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Ensure that at least one MQTT client instance was created and cleaned up.
        assert len(mqtt_instances) > 0
        client = mqtt_instances[0]
        assert client.disconnect_called
        assert client.cleanup_called

    @pytest.mark.asyncio
    async def test_invalid_command(monkeypatch):
        # Test behavior when an unsupported command is passed.
        dummy_device = DummyDevice()
        async def dummy_create(loop):
            return dummy_device
        monkeypatch.setattr(AsyncLinakDevice, "create", dummy_create)

        # Pass an invalid command.
        args = DummyArgs(func='invalid_command')
        # Run async_main. Depending on implementation, it might do nothing
        # or log an error without invoking device methods.
        await async_main(args)
        # Verify that the device has not been used.
        assert not dummy_device.get_position_called
        # Shutdown might be conditionally called; if so, adjust expectations accordingly.
        # Here we assume that an invalid command does not engage the device.
        assert not dummy_device.shutdown_called