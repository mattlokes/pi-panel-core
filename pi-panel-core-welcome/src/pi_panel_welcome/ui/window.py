"""The window: who this panel is, where to reach it, and why nothing else is
showing. It only displays what it is given (info.Info, status.Status)."""

from __future__ import annotations

import re
from importlib.resources import files

from gi.repository import Gdk, Gtk

from ..info import Info
from ..status import Status

DESIGN_WIDTH, DESIGN_HEIGHT = 1920, 1080
MAX_PROBLEMS = 4


def screen_size() -> tuple[int, int]:
    display = Gdk.Display.get_default()
    monitors = display.get_monitors() if display else None
    if monitors is not None and monitors.get_n_items():
        geometry = monitors.get_item(0).get_geometry()
        if geometry.width > 0 and geometry.height > 0:
            return geometry.width, geometry.height
    return DESIGN_WIDTH, DESIGN_HEIGHT


class WelcomeWindow(Gtk.Window):
    def __init__(self, size: tuple[int, int]) -> None:
        super().__init__(title="pi-panel")
        # One design unit: a pixel on a 1920x1080 screen.
        self.unit = min(size[0] / DESIGN_WIDTH, size[1] / DESIGN_HEIGHT)
        self.set_default_size(*size)
        self.set_decorated(False)
        self.add_css_class("welcome")
        self._load_css()

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.px(14),
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                         margin_start=self.px(80), margin_end=self.px(80))
        self._mark = self._label("mark", "PI-PANEL")
        self._hostname = self._label("hostname")
        self._facts = self._label("facts", wrap=True)
        self._headline = self._label("headline")
        self._hints = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.px(8))
        self._problems = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.px(10),
                                 visible=False)
        self._problems.add_css_class("problems")
        for w in (self._mark, self._hostname, self._facts, self._headline, self._hints,
                  self._problems):
            column.append(w)

        # Reserved for plan 5 (update available, OTA progress): a band along
        # the bottom, outside the centred column so the column never moves.
        self.updates = self._label("updates")
        self.updates.set_valign(Gtk.Align.END)
        self.updates.set_margin_bottom(self.px(40))
        self.updates.set_visible(False)

        overlay = Gtk.Overlay(child=column)
        overlay.add_overlay(self.updates)
        self.set_child(overlay)
        self.fullscreen()

    def px(self, design: float) -> int:
        return max(1, round(design * self.unit))

    def _load_css(self) -> None:
        css = files(__package__).joinpath("style.css").read_text(encoding="utf-8")
        css = re.sub(r"(-?\d+(?:\.\d+)?)u\b",
                     lambda m: f"{float(m.group(1)) * self.unit:.2f}px", css)
        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    @staticmethod
    def _label(css_class: str, text: str = "", wrap: bool = False,
               xalign: float = 0.5) -> Gtk.Label:
        justify = Gtk.Justification.CENTER if xalign == 0.5 else Gtk.Justification.LEFT
        label = Gtk.Label(label=text, xalign=xalign, wrap=wrap, justify=justify)
        label.add_css_class(css_class)
        return label

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        while (child := box.get_first_child()) is not None:
            box.remove(child)

    # --- updates -------------------------------------------------------------

    def set_info(self, info: Info) -> None:
        self._hostname.set_text(info.hostname)
        where = [f"{a.address}  ({a.interface})" for a in info.addresses] or ["no network address"]
        what = [info.release.label] + ([f"slot {info.slot}"] if info.slot else [])
        self._facts.set_text("    ·    ".join(where) + "\n" + "    ·    ".join(what))

    def set_status(self, status: Status) -> None:
        self._headline.set_text(status.headline)
        self._clear(self._hints)
        for line in status.lines:
            self._hints.append(self._label("hint", line, wrap=True))

        self._clear(self._problems)
        shown = status.problems[:MAX_PROBLEMS]
        for p in shown:
            name = self._label("problem-name", f"{p.name}: {p.severity}", xalign=0.0)
            if p.severity == "warning":
                name.add_css_class("warning")
            self._problems.append(name)
            if p.detail:
                self._problems.append(self._label("problem-detail", p.detail, wrap=True,
                                                  xalign=0.0))
        if len(status.problems) > len(shown):
            more = len(status.problems) - len(shown)
            self._problems.append(self._label(
                "problem-detail", f"…and {more} more: pi-panel-ctl config check", xalign=0.0))
        self._problems.set_visible(bool(shown))
