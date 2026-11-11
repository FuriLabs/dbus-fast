import asyncio

import pytest
import pytest_asyncio

from dbus_fast import Message, MessageFlag, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusStr
from dbus_fast.constants import ErrorType, PropertyAccess
from dbus_fast.errors import InvalidInterfaceNameError, InvalidObjectPathError
from dbus_fast.service import ServiceInterface, dbus_property

ROOT = "/org/example/Manager"
CHILD = ROOT + "/Device/0"
MANAGER = "org.freedesktop.DBus.ObjectManager"
DEVICE = "org.example.Device"
EXTRA = "org.example.Extra"


class LabelInterface(ServiceInterface):
    @dbus_property(access=PropertyAccess.READ)
    def Label(self) -> DBusStr:
        return self.name


class SlowInterface(LabelInterface):
    def __init__(self, name=DEVICE):
        super().__init__(name)
        self.gate = asyncio.Event()
        self.started = asyncio.Queue()

    @dbus_property(access=PropertyAccess.READ)
    async def Delayed(self) -> DBusStr:
        self.started.put_nowait(None)
        await self.gate.wait()
        return "ready"


class BrokenInterface(LabelInterface):
    @dbus_property(access=PropertyAccess.READ)
    def Broken(self) -> DBusStr:
        raise RuntimeError("getter failed")


@pytest_asyncio.fixture
async def buses():
    server = await MessageBus().connect()
    client = await MessageBus().connect()
    signals = []

    def receive(msg):
        if msg.message_type == MessageType.SIGNAL and msg.interface == MANAGER:
            signals.append(msg)

    client.add_message_handler(receive)
    try:
        reply = await client.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[f"type='signal',sender='{server.unique_name}'"],
            )
        )
        assert reply.message_type == MessageType.METHOD_RETURN
        yield server, client, signals
    finally:
        server.disconnect()
        client.disconnect()
        await asyncio.gather(server.wait_for_disconnect(), client.wait_for_disconnect())


async def managed(server, client, path=ROOT):
    return await client.call(
        Message(
            destination=server.unique_name,
            path=path,
            interface=MANAGER,
            member="GetManagedObjects",
        )
    )


async def flush(server, client):
    reply = await client.call(
        Message(
            destination=server.unique_name,
            path=ROOT,
            interface="org.freedesktop.DBus.Peer",
            member="Ping",
        )
    )
    assert reply.message_type == MessageType.METHOD_RETURN


@pytest.mark.asyncio
async def test_filtered_membership_and_signals(buses):
    server, client, signals = buses
    server.export_object_manager(ROOT, required_interfaces={DEVICE})
    server.export(ROOT, LabelInterface("org.example.Root"))
    extra = LabelInterface(EXTRA)
    server.export(CHILD, extra)
    assert (await managed(server, client)).body == [{}]
    assert not signals

    device = LabelInterface(DEVICE)
    server.export(CHILD, device)
    expected = {
        DEVICE: {"Label": Variant("s", DEVICE)},
        EXTRA: {"Label": Variant("s", EXTRA)},
    }
    reply = await managed(server, client)
    assert reply.message_type == MessageType.METHOD_RETURN
    assert reply.body == [{CHILD: expected}]
    received_signals = []
    for signal in signals:
        received_signals.append((signal.path, signal.member, signal.body))
    assert received_signals == [(ROOT, "InterfacesAdded", [CHILD, expected])]

    server.unexport(CHILD, device)
    assert (await managed(server, client)).body == [{}]
    assert signals[-1].path == ROOT
    assert signals[-1].member == "InterfacesRemoved"
    assert signals[-1].body[0] == CHILD
    assert set(signals[-1].body[1]) == {DEVICE, EXTRA}
    reply = await client.call(
        Message(
            destination=server.unique_name,
            path=CHILD,
            interface="org.freedesktop.DBus.Properties",
            member="GetAll",
            signature="s",
            body=[EXTRA],
        )
    )
    assert reply.body == [{"Label": Variant("s", EXTRA)}]

    server.export(CHILD, device)
    server.unexport(CHILD, extra)
    assert (await managed(server, client)).body == [
        {CHILD: {DEVICE: {"Label": Variant("s", DEVICE)}}}
    ]
    assert signals[-1].body == [CHILD, [EXTRA]]


@pytest.mark.asyncio
async def test_registration_introspection_and_legacy_boundaries(buses):
    server, client, signals = buses
    server.export_object_manager(ROOT)
    server.export_object_manager("/other", required_interfaces={DEVICE, EXTRA})
    root_node = await client.introspect(server.unique_name, ROOT)
    root_interface_names = set()
    for interface in root_node.interfaces:
        root_interface_names.add(interface.name)
    assert MANAGER in root_interface_names
    ancestor_node = await client.introspect(server.unique_name, "/org/example")
    child_names = set()
    for child in ancestor_node.nodes:
        child_names.add(child.name)
    assert child_names == {"Manager"}
    reply = await client.call(
        Message(
            destination=server.unique_name,
            path=ROOT,
            interface="org.freedesktop.DBus.Properties",
            member="GetAll",
            signature="s",
            body=[MANAGER],
        )
    )
    assert reply.message_type == MessageType.METHOD_RETURN
    assert reply.body == [{}]
    server.export(CHILD, LabelInterface(DEVICE))
    child_node = await client.introspect(server.unique_name, CHILD)
    child_interface_names = set()
    for interface in child_node.interfaces:
        child_interface_names.add(interface.name)
    assert MANAGER not in child_interface_names
    assert (await managed(server, client, CHILD)).error_name == ErrorType.UNKNOWN_METHOD

    outside = ROOT + "ial/Device/0"
    server.export(outside, LabelInterface(DEVICE))
    server.export("/other/device", LabelInterface(DEVICE))
    assert (await managed(server, client, "/other")).body == [{}]
    server.export("/other/device", LabelInterface(EXTRA))
    assert set((await managed(server, client, "/other")).body[0]["/other/device"]) == {
        DEVICE,
        EXTRA,
    }
    assert outside not in (await managed(server, client)).body[0]
    outside_node = await client.introspect(server.unique_name, outside)
    outside_interface_names = set()
    for interface in outside_node.interfaces:
        outside_interface_names.add(interface.name)
    assert MANAGER in outside_interface_names
    assert any(s.path == outside and s.body[0] == outside for s in signals)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [ROOT, ROOT + "/nested", "/org/example", "/"])
async def test_overlapping_registrations_rejected(buses, path):
    server, _, _ = buses
    server.export_object_manager(ROOT)
    with pytest.raises(ValueError, match="overlap"):
        server.export_object_manager(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [ROOT, CHILD])
async def test_late_registration_rejected(buses, path):
    server, _, _ = buses
    server.export(path, LabelInterface(DEVICE))
    with pytest.raises(ValueError, match="before exporting"):
        server.export_object_manager(ROOT)


@pytest.mark.asyncio
async def test_registration_validation(buses):
    server, _, _ = buses
    with pytest.raises(InvalidObjectPathError):
        server.export_object_manager("invalid")
    with pytest.raises(TypeError, match="collection"):
        server.export_object_manager(ROOT, required_interfaces=DEVICE)
    with pytest.raises(InvalidInterfaceNameError):
        server.export_object_manager(ROOT, required_interfaces={"invalid"})
    server.export_object_manager(ROOT)


@pytest.mark.asyncio
async def test_root_manager_excludes_itself(buses):
    server, client, signals = buses
    server.export_object_manager("/")
    server.export("/", LabelInterface(EXTRA))
    server.export("/device", LabelInterface(DEVICE))
    assert (await managed(server, client, "/")).body == [
        {"/device": {DEVICE: {"Label": Variant("s", DEVICE)}}}
    ]
    assert len(signals) == 1
    assert signals[0].path == "/"


@pytest.mark.asyncio
async def test_pending_additions_preserve_property_updates(buses):
    server, client, signals = buses
    server.export_object_manager(ROOT)
    slow = SlowInterface()
    server.export(CHILD, slow)
    await slow.started.get()
    server.export(CHILD, LabelInterface(EXTRA))
    pending = []
    for _ in range(2):
        pending.append(asyncio.create_task(managed(server, client)))
    try:
        await slow.started.get()
        await slow.started.get()
        slow.emit_properties_changed({"Label": "changed"}, ["Delayed"])
        await flush(server, client)
        assert not signals
    finally:
        slow.gate.set()
    replies = await asyncio.gather(*pending)
    for reply in replies:
        assert reply.message_type == MessageType.METHOD_RETURN
        assert reply.body == [
            {
                CHILD: {
                    DEVICE: {"Label": Variant("s", "changed")},
                    EXTRA: {"Label": Variant("s", EXTRA)},
                }
            }
        ]
    await flush(server, client)
    assert len(signals) == 1
    assert signals[0].body == [CHILD, replies[0].body[0][CHILD]]


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", [False, True])
async def test_removed_and_reexported_interfaces_ignore_stale_reads(buses, reuse):
    server, client, signals = buses
    server.export_object_manager(ROOT)
    slow = SlowInterface()
    server.export(CHILD, slow)
    await slow.started.get()
    pending = asyncio.create_task(managed(server, client))
    try:
        await slow.started.get()
        server.unexport(CHILD)
        assert (await pending).body == [{}]
        replacement = slow if reuse else LabelInterface(DEVICE)
        server.export(CHILD, replacement)
        if reuse:
            await slow.started.get()
    finally:
        slow.gate.set()
    await managed(server, client)
    await flush(server, client)
    signal_members = []
    for signal in signals:
        signal_members.append(signal.member)
    assert signal_members == ["InterfacesRemoved", "InterfacesAdded"]
    assert signals[-1].path == ROOT
    assert set(signals[-1].body[1]) == {DEVICE}


@pytest.mark.asyncio
async def test_getter_failure_keeps_partial_announcement(buses, caplog):
    server, client, signals = buses
    server.export_object_manager(ROOT)
    server.export(CHILD, BrokenInterface(DEVICE))
    reply = await managed(server, client)
    assert reply.message_type == MessageType.ERROR
    assert reply.error_name == ErrorType.SERVICE_ERROR
    assert signals[0].body == [CHILD, {DEVICE: {"Label": Variant("s", DEVICE)}}]
    assert "Some properties could not be read" in caplog.text


@pytest.mark.asyncio
async def test_no_reply_expected_skips_property_reads(buses):
    server, client, signals = buses
    server.export_object_manager(ROOT)
    slow = SlowInterface()
    server.export(CHILD, slow)
    await slow.started.get()
    try:
        await client.send(
            Message(
                destination=server.unique_name,
                path=ROOT,
                interface=MANAGER,
                member="GetManagedObjects",
                flags=MessageFlag.NO_REPLY_EXPECTED | MessageFlag.NO_AUTOSTART,
            )
        )
        await flush(server, client)
        assert slow.started.empty()
        assert not signals
    finally:
        slow.gate.set()
        await managed(server, client)


@pytest.mark.asyncio
async def test_disconnect_discards_pending_collections(buses):
    server, _, signals = buses
    server.export_object_manager(ROOT)
    slow = SlowInterface()
    server.export(CHILD, slow)
    await slow.started.get()
    manager = server._object_managers[ROOT]
    server.disconnect()
    await server.wait_for_disconnect()
    slow.gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not manager.pending
    assert not manager.announcements
    assert not manager.objects
    assert not server._object_managers
    assert not signals
