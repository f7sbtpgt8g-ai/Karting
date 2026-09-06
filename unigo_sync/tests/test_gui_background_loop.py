"""Regression tests for the GUI's background poll -- the only thing that
ever drains the pending-upload queue.

The bug these cover: the loop body read tk variables (`_auto_sync_var`,
and `_driver_var` via `_selected_driver`) from the background thread,
outside its `try`. Reading a tk variable off the main thread is not safe,
and anything raising there ended the `while` loop -- so the queue stopped
being flushed for the rest of the session, with nothing in the log and
"N session(s) waiting to upload" on screen forever.

Only the loop's control flow is exercised, with no Tk involved: the App is
built via `__new__` and given just the attributes the loop touches. That
runs on a headless machine because `gui_app` defers its tkinter imports
into the functions that need them (see its module docstring), so importing
it here needs no Tk install.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unigo_sync.platform_windows import gui_app  # noqa: E402


def _bare_app(tick):
    """An App with no Tk behind it, wired to `tick` as its per-poll work."""
    app = gui_app.App.__new__(gui_app.App)
    app._stop_event = threading.Event()
    app._background_tick = tick
    return app


def _run_loop_briefly(app, monkeypatch, ticks_expected: int) -> None:
    monkeypatch.setattr(gui_app, "_BACKGROUND_POLL_S", 0.01)
    thread = threading.Thread(target=app._background_loop, daemon=True)
    thread.start()
    deadline = threading.Event()
    deadline.wait(0.01 * ticks_expected * 20)
    app._stop_event.set()
    thread.join(timeout=2)
    assert not thread.is_alive(), "background loop did not stop on the stop event"


def test_loop_keeps_polling_after_a_tick_raises(monkeypatch):
    """The whole point: one bad poll must not end the thread."""
    calls = []

    def exploding_tick():
        calls.append(1)
        raise RuntimeError("main thread is not in main loop")

    app = _bare_app(exploding_tick)
    _run_loop_briefly(app, monkeypatch, ticks_expected=5)

    assert len(calls) > 1, f"loop stopped after {len(calls)} tick(s) instead of continuing"


def test_loop_stops_on_the_stop_event(monkeypatch):
    calls = []
    app = _bare_app(lambda: calls.append(1))
    _run_loop_briefly(app, monkeypatch, ticks_expected=5)

    before = len(calls)
    assert before >= 1
    # Already stopped by _run_loop_briefly; nothing further should arrive.
    threading.Event().wait(0.05)
    assert len(calls) == before


class _EmptyFlushOutcome:
    uploaded: list = []
    still_pending: list = []
    blocked_reason = None


def test_background_tick_reads_snapshots_not_tk_variables(monkeypatch):
    """The loop must reach only plain attributes. This App has no
    `_auto_sync_var` or `_driver_var` at all, so any attempt to read one
    raises AttributeError and fails the test -- which is the bug coming
    back."""
    app = gui_app.App.__new__(gui_app.App)
    app._flush_lock = threading.Lock()
    app.config = object()
    app._auto_sync_enabled = False
    app._selected_driver_snapshot = None
    app._ui = lambda fn, *args: None

    flushed = []
    monkeypatch.setattr(
        gui_app, "flush_pending_uploads",
        lambda config: flushed.append(config) or _EmptyFlushOutcome(),
    )

    app._background_tick()

    assert len(flushed) == 1


def test_auto_sync_does_not_check_wifi_when_no_driver_is_selected(monkeypatch):
    """`is_connected_to_unigo` shells out to netsh -- not worth doing four
    times a minute when there is nothing to sync for."""
    app = gui_app.App.__new__(gui_app.App)
    app._flush_lock = threading.Lock()
    app.config = object()
    app._auto_sync_enabled = True
    app._selected_driver_snapshot = None
    app._ui = lambda fn, *args: None

    monkeypatch.setattr(gui_app, "flush_pending_uploads", lambda config: _EmptyFlushOutcome())
    monkeypatch.setattr(
        gui_app, "is_connected_to_unigo",
        lambda prefix: pytest.fail("should not probe wifi without a selected driver"),
    )

    app._background_tick()
