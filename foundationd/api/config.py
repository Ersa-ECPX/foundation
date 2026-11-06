"""org.ersa.Foundation1.Config."""

from gi.repository import GLib

from ..errors import InvalidArgument, ReadOnly
from ..service import Method

CONFIG = "org.ersa.Foundation1.Config"
ACTION_CONFIG = "org.ersa.foundation.config"


def register(service):
    schema = service.config.schema

    def get(call):
        (name,) = call.args
        key = schema.get(name)
        return GLib.Variant("(v)", (GLib.Variant(key.signature, service.config.get(name)),))

    def list_all(call):
        effective = service.config.all()
        return GLib.Variant("(a{sv})", ({key.name: GLib.Variant(key.signature, effective[key.name]) for key in schema},))

    def describe(call):
        (name,) = call.args
        key = schema.get(name)
        info = {
            "type": GLib.Variant("s", key.signature),
            "default": GLib.Variant(key.signature, key.default),
            "description": GLib.Variant("s", key.description),
            "writable": GLib.Variant("b", key.writable),
        }
        if key.choices:
            info["choices"] = GLib.Variant("as", list(key.choices))
        return GLib.Variant("(a{sv})", (info,))

    def typed_value(call):
        """The variant argument of Set, checked against the schema's type signature."""
        key = schema.get(call.args[0])
        inner = call.parameters.get_child_value(1).get_variant()
        if inner.get_type_string() != key.signature:
            raise InvalidArgument(f"{key.name} takes type '{key.signature}', got '{inner.get_type_string()}'")
        return inner.unpack()

    def writable(name):
        key = schema.get(name)
        if not key.writable:
            raise ReadOnly(f"{name} cannot be changed through the API")
        return key

    def precheck_set(call):
        writable(call.args[0]).validate(typed_value(call))

    def precheck_reset(call):
        writable(call.args[0])

    def set_value(call):
        name = call.args[0]
        if service.config.set(name, typed_value(call)):
            service.emit(CONFIG, "Changed", "(s)", (name,))

    def reset(call):
        (name,) = call.args
        if service.config.reset(name):
            service.emit(CONFIG, "Changed", "(s)", (name,))

    return {
        (CONFIG, "Get"): Method(get),
        (CONFIG, "List"): Method(list_all),
        (CONFIG, "Describe"): Method(describe),
        (CONFIG, "Set"): Method(set_value, ACTION_CONFIG, precheck_set),
        (CONFIG, "Reset"): Method(reset, ACTION_CONFIG, precheck_reset),
    }
