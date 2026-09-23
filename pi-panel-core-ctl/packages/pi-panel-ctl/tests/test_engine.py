"""The decision engine: priorities, holds, rotation timing, schedules."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pi_panel_ctl.engine import Engine
from pi_panel_ctl.model import Rotation, RotationEntry, Schedule, ValidationError, parse_days


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.mono = 1000.0
        self.wall = start

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


# A Wednesday, mid-afternoon.
WED_1500 = datetime(2026, 9, 23, 15, 0)


def rotation(name: str, *entries: tuple[str, float]) -> Rotation:
    return Rotation(name, tuple(RotationEntry(a, s) for a, s in entries))


def engine(clock: FakeClock, *schedules: Schedule, available=("photos", "clock", "cam")) -> Engine:
    e = Engine(
        rotations={
            "default": rotation("default", ("photos", 300), ("clock", 60)),
            "night": rotation("night", ("clock", 600)),
        },
        selected="default",
        schedules=list(schedules),
        clock=clock,
    )
    e.set_available(set(available))
    return e


def sched(**kw) -> Schedule:
    base = {"name": "s", "days": "daily", "start": "23:00", "end": "06:30", "action": "power_off"}
    return Schedule.from_dict(base | kw)


# --- rotation -------------------------------------------------------------

def test_rotation_cycles_on_time():
    clock = FakeClock(WED_1500)
    e = engine(clock)
    assert e.decide().showing == "photos"
    clock.advance(299)
    assert e.decide().showing == "photos"
    clock.advance(1)
    assert e.decide().showing == "clock"
    clock.advance(60)
    assert e.decide().showing == "photos"


def test_rotation_skips_apps_without_a_window():
    clock = FakeClock(WED_1500)
    e = engine(clock, available=("clock",))
    d = e.decide()
    assert d.showing == "clock" and d.reason == "rotation"


def test_rotation_moves_on_when_current_app_disappears():
    clock = FakeClock(WED_1500)
    e = engine(clock)
    assert e.decide().showing == "photos"
    e.set_available({"clock"})
    assert e.decide().showing == "clock"


def test_nothing_available_means_no_decision():
    clock = FakeClock(WED_1500)
    e = engine(clock, available=())
    d = e.decide()
    assert d.showing is None and d.reason == "none" and d.power is True


def test_pause_freezes_rotation_and_resume_gives_a_full_period():
    clock = FakeClock(WED_1500)
    e = engine(clock)
    e.decide()
    e.pause()
    clock.advance(1000)
    assert e.decide().showing == "photos"
    e.resume()
    clock.advance(299)
    assert e.decide().showing == "photos"
    clock.advance(1)
    assert e.decide().showing == "clock"


def test_next_and_previous():
    clock = FakeClock(WED_1500)
    e = engine(clock)
    e.decide()
    e.step(+1)
    assert e.decide().showing == "clock"
    e.step(-1)
    assert e.decide().showing == "photos"
    # and the new entry gets its full period
    clock.advance(299)
    assert e.decide().showing == "photos"


def test_next_wakeup_tracks_rotation():
    clock = FakeClock(WED_1500)
    e = engine(clock)
    e.decide()
    clock.advance(100)
    assert e.next_wakeup() == pytest.approx(60)   # capped: the wall clock can jump
    clock.advance(190)
    assert e.next_wakeup() == pytest.approx(10)


# --- holds ----------------------------------------------------------------

def test_timed_hold_expires_back_to_rotation():
    clock = FakeClock(WED_1500)
    e = engine(clock)
    e.decide()
    e.show("cam", seconds=30)
    d = e.decide()
    assert (d.showing, d.reason) == ("cam", "hold")
    assert e.next_wakeup() == pytest.approx(30)
    clock.advance(30)
    assert e.decide().showing == "photos"
    assert e.holds == {}


def test_most_recent_hold_wins_a_tie_and_higher_priority_wins():
    clock = FakeClock(WED_1500)
    e = engine(clock)
    e.show("clock", priority=5)
    e.show("photos", priority=5)
    assert e.decide().showing == "photos"
    low = e.show("cam", priority=1)
    assert e.decide().showing == "photos"
    e.release(low)
    high = e.show("cam", priority=9)
    assert e.decide().showing == "cam"
    assert e.release(high)
    assert not e.release(high)


def test_next_drops_untimed_holds_but_not_timed_ones():
    clock = FakeClock(WED_1500)
    e = engine(clock)
    e.show("cam")
    e.step(+1)
    assert e.decide().reason == "rotation"
    e.show("cam", seconds=10)
    e.step(+1)
    assert e.decide().showing == "cam"


def test_hold_on_app_without_window_still_decides_it():
    """The daemon switches once the window maps; the engine should not
    silently fall back to the rotation while the hold is live."""
    clock = FakeClock(WED_1500)
    e = engine(clock, available=("photos",))
    e.show("cam", seconds=30)
    assert e.decide().showing == "cam"


# --- schedules ------------------------------------------------------------

def test_power_off_schedule_across_midnight():
    clock = FakeClock(datetime(2026, 9, 23, 22, 59))
    e = engine(clock, sched(name="night"))
    assert e.decide().power is True
    clock.advance(60)
    d = e.decide()
    assert d.power is False and d.showing is None and d.active_schedules == ("night",)
    clock.advance(7 * 3600)  # 06:00 next day
    assert e.decide().power is False
    clock.advance(1800)       # 06:30
    assert e.decide().power is True


def test_weekday_window_belongs_to_its_start_day():
    fri_night = sched(days="fri")
    # Saturday 02:00 is inside Friday's 23:00-06:30 window
    assert fri_night.is_active(datetime(2026, 9, 26, 2, 0))
    # Friday 02:00 is inside *Thursday's* window, which is not scheduled
    assert not fri_night.is_active(datetime(2026, 9, 25, 2, 0))


def test_higher_priority_hold_beats_power_off():
    clock = FakeClock(datetime(2026, 9, 23, 23, 30))
    e = engine(clock, sched(priority=10))
    e.show("cam", seconds=30, priority=10)       # tie: hold wins
    d = e.decide()
    assert (d.showing, d.power, d.reason) == ("cam", True, "hold")
    clock.advance(30)
    assert e.decide().power is False


def test_lower_priority_hold_does_not_beat_schedule():
    clock = FakeClock(datetime(2026, 9, 23, 23, 30))
    e = engine(clock, sched(priority=10))
    e.show("cam", priority=1)
    assert e.decide().power is False


def test_show_schedule():
    clock = FakeClock(datetime(2026, 9, 23, 7, 30))
    e = engine(clock, sched(name="morning", start="07:00", end="09:00", action="show", app="clock"))
    d = e.decide()
    assert (d.showing, d.reason) == ("clock", "schedule")


def test_rotation_schedule_switches_playlist():
    clock = FakeClock(datetime(2026, 9, 23, 23, 30))
    e = engine(clock, sched(action="rotation", rotation="night"))
    d = e.decide()
    assert (d.showing, d.rotation, d.reason) == ("clock", "night", "schedule")


def test_next_wakeup_includes_schedule_boundary():
    clock = FakeClock(datetime(2026, 9, 23, 22, 59, 30))
    e = engine(clock, sched())
    e.pause()
    assert e.next_wakeup() == pytest.approx(30)


# --- validation -----------------------------------------------------------

@pytest.mark.parametrize(
    "spec,days",
    [("daily", set(range(7))), ("mon-fri", {0, 1, 2, 3, 4}), ("sat,sun", {5, 6}),
     ("fri-mon", {4, 5, 6, 0}), ("mon,wed-thu", {0, 2, 3})],
)
def test_parse_days(spec, days):
    assert parse_days(spec) == days


@pytest.mark.parametrize(
    "bad",
    [{"action": "explode"}, {"action": "show"}, {"action": "rotation"},
     {"start": "25:00"}, {"days": "someday"}, {"start": "07:00", "end": "07:00"},
     {"priority": "high"}],
)
def test_invalid_schedules(bad):
    with pytest.raises(ValidationError):
        sched(**bad)


def test_invalid_rotation():
    with pytest.raises(ValidationError):
        Rotation.from_dict({"name": "x", "entries": [{"app": "a", "seconds": 0}]})
