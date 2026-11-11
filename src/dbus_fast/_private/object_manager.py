"""Explicit ObjectManager registration and property collection."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..constants import MessageFlag
from ..message import Message
from ..service import ServiceInterface
from ..signature import Variant

if TYPE_CHECKING:
    from ..message_bus import BaseMessageBus
    from ..send_reply import SendReply

_LOGGER = logging.getLogger(__name__)
_OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"


@dataclass(eq=False)
class _ManagedInterface:
    interface: ServiceInterface


class _PropertyCollection:
    def __init__(
        self,
        manager: _ObjectManager,
        complete: Callable[[_PropertyCollection, Exception | None], None],
        allow_partial: bool = False,
    ) -> None:
        self.manager = manager
        self.complete = complete
        self.allow_partial = allow_partial
        self.objects: dict[str, dict[str, _ManagedInterface]] = {}
        self.values: dict[_ManagedInterface, dict[str, Variant] | None] = {}
        self.updates: dict[_ManagedInterface, dict[str, Variant | None]] = {}
        self.finished = False
        manager.pending.add(self)

    def add(self, objects: dict[str, dict[str, _ManagedInterface]]) -> None:
        added = []
        for path, interfaces in objects.items():
            self.objects.setdefault(path, {}).update(interfaces)
            for exported in interfaces.values():
                self.values[exported] = None
                self.updates[exported] = {}
                added.append(exported)
        for exported in added:
            if self.finished:
                return
            ServiceInterface._get_all_property_values(
                exported.interface, self._properties_ready, exported
            )
        self._finish_if_ready()

    def discard(self, exported: _ManagedInterface) -> None:
        self.values.pop(exported, None)
        self.updates.pop(exported, None)
        self._finish_if_ready()

    def cancel(self) -> None:
        self.finished = True
        self.manager.pending.discard(self)

    def result(self) -> dict[str, dict[str, dict[str, Variant]]]:
        result = {}
        for path, interfaces in self.objects.items():
            current = self.manager.objects.get(path, {})
            properties = {}
            for name, exported in interfaces.items():
                values = self.values.get(exported)
                if current.get(name) is not exported or values is None:
                    continue
                interface_properties: dict[str, Variant] = {}
                merged_values = {**values, **self.updates[exported]}
                for key, value in merged_values.items():
                    if value is None:
                        continue
                    interface_properties[key] = value
                properties[name] = interface_properties
            if properties:
                result[path] = properties
        return result

    def _properties_ready(
        self,
        interface: ServiceInterface,
        values: dict[str, Variant],
        exported: _ManagedInterface,
        error: Exception | None,
    ) -> None:
        if self.finished or exported not in self.values:
            return
        if error is not None:
            if not self.allow_partial:
                self._finish(error)
                return
            _LOGGER.warning(
                "Some properties could not be read for %s",
                interface.name,
                exc_info=(type(error), error, error.__traceback__),
            )
        self.values[exported] = values
        self._finish_if_ready()

    def _finish_if_ready(self) -> None:
        if not self.finished and all(
            value is not None for value in self.values.values()
        ):
            self._finish(None)

    def _finish(self, error: Exception | None) -> None:
        self.cancel()
        if not self.manager.bus._disconnected:
            self.complete(self, error)


class _ObjectManager:
    def __init__(
        self, bus: BaseMessageBus, path: str, required_interfaces: frozenset[str]
    ) -> None:
        self.bus = bus
        self.path = path
        self.required_interfaces = required_interfaces
        self.objects: dict[str, dict[str, _ManagedInterface]] = {}
        self.pending: set[_PropertyCollection] = set()
        self.announcements: dict[str, _PropertyCollection] = {}

    def contains(self, path: str) -> bool:
        return path == self.path or path.startswith(self.path.rstrip("/") + "/")

    def interfaces_changed(self, path: str) -> None:
        if path == self.path:
            return
        interfaces = self.bus._path_exports.get(path, {})
        if not self.required_interfaces.issubset(interfaces):
            interfaces = {}
        previous = self.objects.get(path, {})
        current: dict[str, _ManagedInterface] = {}
        for name, interface in interfaces.items():
            if name in previous and previous[name].interface is interface:
                current[name] = previous[name]
            else:
                current[name] = _ManagedInterface(interface)
        if current:
            self.objects[path] = current
        else:
            self.objects.pop(path, None)

        removed: dict[str, _ManagedInterface] = {}
        for name, exported in previous.items():
            if current.get(name) is exported:
                continue
            removed[name] = exported
        if removed:
            self.bus.send(
                Message.new_signal(
                    self.path,
                    _OBJECT_MANAGER,
                    "InterfacesRemoved",
                    "oas",
                    [path, list(removed)],
                )
            )
            for collection in tuple(self.pending):
                for exported in removed.values():
                    collection.discard(exported)

        added: dict[str, _ManagedInterface] = {}
        for name, exported in current.items():
            if previous.get(name) is exported:
                continue
            added[name] = exported
        if not added:
            return
        collection = self.announcements.get(path)
        if collection is None:
            collection = _PropertyCollection(self, self._announce, allow_partial=True)
            self.announcements[path] = collection
        collection.add({path: added})

    def properties_changed(
        self,
        path: str,
        interface: ServiceInterface,
        changed: dict[str, Variant],
        invalidated: list[str],
    ) -> None:
        exported = self.objects.get(path, {}).get(interface.name)
        if exported is None or exported.interface is not interface:
            return
        for collection in self.pending:
            updates = collection.updates.get(exported)
            if updates is not None:
                updates.update(changed)
                updates.update(dict.fromkeys(invalidated))

    def get_managed_objects(self, msg: Message, send_reply: SendReply) -> None:
        if msg.flags & MessageFlag.NO_REPLY_EXPECTED:
            return

        def complete(collection: _PropertyCollection, error: Exception | None) -> None:
            with send_reply:
                if error is not None:
                    send_reply.send_error(error)
                else:
                    send_reply(
                        Message.new_method_return(
                            msg, "a{oa{sa{sv}}}", [collection.result()]
                        )
                    )

        collection = _PropertyCollection(self, complete)
        objects: dict[str, dict[str, _ManagedInterface]] = {}
        for path, interfaces in self.objects.items():
            objects[path] = dict(interfaces)
        collection.add(objects)

    def close(self) -> None:
        for collection in tuple(self.pending):
            collection.cancel()
        self.announcements.clear()
        self.objects.clear()

    def _announce(
        self, collection: _PropertyCollection, error: Exception | None
    ) -> None:
        for path in collection.objects:
            if self.announcements.get(path) is collection:
                del self.announcements[path]
        for path, interfaces in collection.result().items():
            self.bus.send(
                Message.new_signal(
                    self.path,
                    _OBJECT_MANAGER,
                    "InterfacesAdded",
                    "oa{sa{sv}}",
                    [path, interfaces],
                )
            )
