"""The screen. Everything here needs GTK 4; nothing outside ui/ does, so the
rest is tested without it."""

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
