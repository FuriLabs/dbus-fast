ObjectManager
=============

Call :func:`MessageBus.export_object_manager()
<dbus_fast.message_bus.BaseMessageBus.export_object_manager>` to register a
root for ``org.freedesktop.DBus.ObjectManager`` before exporting objects on
or below that path. This API is shared by the asyncio and GLib backends.

.. code-block:: python3

    bus = await MessageBus().connect()
    bus.export_object_manager(
        "/org/example/Service",
        required_interfaces={"org.example.Service.Device"},
    )
    bus.export("/org/example/Service/Device/0", device_interface)
    bus.export("/org/example/Service/Device/0", settings_interface)
    bus.export("/org/example/Service/Helper/0", helper_interface)

``GetManagedObjects`` at ``/org/example/Service`` returns the device and
both of its interfaces. The helper is accessible through direct method and
property calls, but is excluded from this manager because it does not
implement ``org.example.Service.Device``. Omit ``required_interfaces`` to
manage all descendant objects. The object at the manager path itself is
excluded.

The manager emits ``InterfacesAdded`` and ``InterfacesRemoved`` at its
registered root, with the affected child path in the signal body. Adding
the last required interface admits an object with all of its interfaces;
removing any required interface removes the whole object from the manager.
Other interfaces can be added and removed independently.

Asynchronous property reads for the same object's pending additions are
combined into one announcement, so a fast additional interface cannot be
announced before an earlier interface with a slow getter. Property changes
and invalidations emitted while discovery is pending are reflected in the
announcement or reply. Removed or replaced interfaces are excluded from
pending results, including an interface instance exported again at the
same path after removal.

If a getter fails, announcements retain the other readable properties and
log the error. ``GetManagedObjects`` returns an error when any getter fails.
Both operations ignore results arriving after the connection closes.

Registrations last until disconnect. Multiple roots must be disjoint; a
root at ``/`` covers the whole connection. Descendants of a registered
root do not advertise their own ObjectManager interface. Subtrees outside
registered roots keep the default behavior, including the implicit
ObjectManager interface and signals at each exported object's path.
