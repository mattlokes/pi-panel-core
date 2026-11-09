"""`--screenshot`: the window as a PNG, rendered by GTK itself. It needs no
screenshot tool, and works for a window the compositor is not showing."""

from __future__ import annotations

import logging
from pathlib import Path

from gi.repository import Graphene, Gtk

log = logging.getLogger("pi-panel-welcome")


def save(window: Gtk.Window, path: Path) -> None:
    width, height = window.get_width(), window.get_height()
    renderer = window.get_renderer()
    if renderer is None or width <= 0 or height <= 0:
        log.error("no screenshot: the window has not been drawn")
        return
    snapshot = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(window).snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        log.error("no screenshot: nothing to draw")
        return
    renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height)).save_to_png(str(path))
    log.info("saved %s (%dx%d)", path, width, height)
