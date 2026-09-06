"""The primary end-user front end for unigo_sync: a login screen, then a
settings screen (driver + sync period) with a "Connect & Sync" button.

Supersedes bare `tray_app.py` as the Start Menu entry point -- that one
never signed in or uploaded anywhere (see its docstring), which is fine
for a purely local staging-folder tool but not for "put this session in
the database under the right driver's name", which is the actual job
here. `tray_app.py` is left in place for anyone who wants the older,
account-free "just decode to a folder" behaviour.

Built on `tkinter` (stdlib -- no new dependency, and PyInstaller bundles
it automatically on Windows) rather than pulling in a heavier GUI
toolkit. All the actual login/sync/upload logic lives in `core/` (see
`core.auth_session`, `core.sync_orchestrator`) and is plain, toolkit-free
Python; this module is just the widgets and thread plumbing around it.

Threading note: every long-running call (login, sync, upload, the
connectivity poll) runs on a background thread so the window never
freezes, and every UI update coming off one of those threads is
marshalled back onto the Tk main thread via `root.after(...)` -- Tk
widgets are not thread-safe to touch directly from another thread.

`tkinter` itself is imported lazily in `main()` (module-level `tk`/`ttk`/
`simpledialog` names, set with `global`) rather than at module import
time, the same reason `tray_app.py` defers its `pystray`/`PIL` imports
into `run()`/`_make_icon_image`: it keeps this module importable (for
tooling, or a future headless smoke test) in an environment without a Tk
install, even though a real run always needs one.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from ..core import auth_cache, auth_session
from ..core.config import SyncConfig, load_config
from ..core.period import DEFAULT_SYNC_PERIOD, SYNC_PERIOD_ALL, SYNC_PERIOD_LABELS, SYNC_PERIODS, cutoff_for
from ..core.sync_engine import configure_logging
from ..core.sync_orchestrator import flush_pending_uploads, sync_and_upload
from .wifi import is_connected_to_unigo

logger = logging.getLogger("unigo_sync.platform_windows.gui_app")

_ADD_NEW_DRIVER = "+ Add new driver..."

# How often the background thread checks "is the database reachable yet"
# to flush anything queued while offline, and (if enabled) whether the
# laptop has joined the device's WiFi to trigger an auto-sync. Independent
# of `config.poll_interval_s`, which is about how *often the device is
# asked what's new*, not how often connectivity is rechecked.
_BACKGROUND_POLL_S = 15.0


class App:
    def __init__(self, config: SyncConfig | None = None):
        self.config = config or load_config()
        configure_logging(self.config)

        self.root = tk.Tk()
        self.root.title("UniGo Sync")
        self.root.geometry("480x520")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.user_id: int | None = None
        self.email: str | None = None
        self.session_token: str | None = None
        self.driver_choices: list[auth_session.DriverChoice] = []

        self._stop_event = threading.Event()
        self._background_thread: threading.Thread | None = None

        self._container = ttk.Frame(self.root, padding=16)
        self._container.pack(fill="both", expand=True)

        self._login_frame: ttk.Frame | None = None
        self._settings_frame: ttk.Frame | None = None
        self._upload_now_button: ttk.Button | None = None
        # Last reason the pending queue could not drain, so the background
        # poll can report a change without repeating itself every 15s.
        self._last_flush_block_reason: str | None = None
        # One flush at a time. The background poll and the "Upload now"
        # button would otherwise be able to run concurrently and both
        # ingest the same queued session before either removes it from the
        # queue, saving it into the database twice.
        self._flush_lock = threading.Lock()

        self._try_restore_cached_session()

    # -- thread-safe UI helper ------------------------------------------

    def _ui(self, fn, *args) -> None:
        self.root.after(0, lambda: fn(*args))

    # -- startup: restore a cached login if there is one -----------------

    def _try_restore_cached_session(self) -> None:
        cached = auth_cache.load(self.config.auth_cache_path)
        if cached is None or cached.is_stale():
            if cached is not None:
                auth_cache.clear(self.config.auth_cache_path)
            self._show_login()
            return

        # Trust the cache outright while offline (that's the whole point --
        # a laptop joined to the UniGo device's AP has no route to the
        # database to check against) and only bother re-validating when a
        # connection is actually available.
        from ..core.connectivity import is_online

        if is_online():
            valid_user_id = auth_session.validate_session(cached.session_token, self.config.sessions_db)
            if valid_user_id is None:
                auth_cache.clear(self.config.auth_cache_path)
                self._show_login()
                return

        self.user_id = cached.user_id
        self.email = cached.email
        self.session_token = cached.session_token
        self._show_settings(
            preselect_profile_id=cached.driver_profile_id,
            preselect_period=cached.sync_period,
        )

    # -- login screen -----------------------------------------------------

    def _show_login(self) -> None:
        if self._settings_frame is not None:
            self._settings_frame.destroy()
            self._settings_frame = None
        self._login_frame = ttk.Frame(self._container)
        self._login_frame.pack(fill="both", expand=True)

        ttk.Label(self._login_frame, text="Sign in to UniGo Sync", font=("", 14, "bold")).pack(pady=(0, 16))

        form = ttk.Frame(self._login_frame)
        form.pack(fill="x")
        ttk.Label(form, text="Email").grid(row=0, column=0, sticky="w", pady=4)
        email_entry = ttk.Entry(form, width=32)
        email_entry.grid(row=0, column=1, pady=4)
        ttk.Label(form, text="Password").grid(row=1, column=0, sticky="w", pady=4)
        password_entry = ttk.Entry(form, width=32, show="*")
        password_entry.grid(row=1, column=1, pady=4)

        error_label = ttk.Label(
            self._login_frame, text="", foreground="red", wraplength=380, justify="left",
        )
        error_label.pack(pady=8)

        sign_in_button = ttk.Button(self._login_frame, text="Sign in")
        sign_in_button.pack(pady=8)

        # Which database credentials go to is a config.yaml decision with
        # no other visible trace in the app -- show it, so "signed in fine
        # on the web app, rejected here" is diagnosable without reading the
        # config file.
        ttk.Label(
            self._login_frame,
            text=auth_session.describe_backend(self.config.sessions_db),
            wraplength=380, foreground="gray", justify="left",
        ).pack(pady=(4, 0))

        offline_note = ttk.Label(
            self._login_frame,
            text=(
                "Sign in once while you have normal internet access, before connecting to the "
                "UniGo device's WiFi -- your login is remembered for a week, so you can still sync "
                "and stage sessions while offline at the track."
            ),
            wraplength=380, foreground="gray",
        )
        offline_note.pack(pady=(16, 0))

        def do_login() -> None:
            email, password = email_entry.get().strip(), password_entry.get()
            if not email or not password:
                error_label.config(text="Enter both an email and a password.")
                return
            sign_in_button.config(state="disabled")
            error_label.config(text="Signing in...")
            threading.Thread(target=login_worker, args=(email, password), daemon=True).start()

        def login_worker(email: str, password: str) -> None:
            # Anything escaping here would kill this thread with the button
            # still disabled and "Signing in..." still on screen -- a hung
            # app, rather than the unreachable-database problem it usually
            # is. The sync workers below already catch for the same reason.
            try:
                result = auth_session.login(email, password, self.config.sessions_db)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Sign-in failed")
                result = auth_session.LoginResult(False, error=f"Could not sign in: {exc}")
            self._ui(self._on_login_result, result)

        sign_in_button.config(command=do_login)
        password_entry.bind("<Return>", lambda _e: do_login())
        self._login_error_label = error_label
        self._login_sign_in_button = sign_in_button

    def _on_login_result(self, result: auth_session.LoginResult) -> None:
        if not result.ok:
            self._login_error_label.config(text=result.error or "Sign in failed.")
            self._login_sign_in_button.config(state="normal")
            return

        self.user_id, self.email, self.session_token = result.user_id, result.email, result.session_token
        auth_cache.save(
            self.config.auth_cache_path,
            auth_cache.CachedSession(
                user_id=result.user_id, email=result.email, session_token=result.session_token,
                cached_at=datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._login_frame.destroy()
        self._login_frame = None
        self._show_settings()

    # -- settings / sync screen -------------------------------------------

    def _show_settings(self, preselect_profile_id: int | None = None, preselect_period: str | None = None) -> None:
        self._settings_frame = ttk.Frame(self._container)
        self._settings_frame.pack(fill="both", expand=True)

        top = ttk.Frame(self._settings_frame)
        top.pack(fill="x")
        ttk.Label(top, text=f"Signed in as {self.email}").pack(side="left")
        ttk.Button(top, text="Sign out", command=self._sign_out).pack(side="right")

        ttk.Separator(self._settings_frame).pack(fill="x", pady=8)

        ttk.Label(self._settings_frame, text="Driver").pack(anchor="w")
        self.driver_choices = auth_session.list_driver_choices(self.user_id, self.config.sessions_db)
        self._driver_var = tk.StringVar()
        self._driver_combo = ttk.Combobox(self._settings_frame, textvariable=self._driver_var, state="readonly", width=40)
        self._refresh_driver_combo(preselect_profile_id)
        self._driver_combo.pack(fill="x", pady=(2, 12))
        self._driver_combo.bind("<<ComboboxSelected>>", self._on_driver_selected)

        ttk.Label(self._settings_frame, text="Sync period").pack(anchor="w")
        self._period_var = tk.StringVar(value=preselect_period or DEFAULT_SYNC_PERIOD)
        period_frame = ttk.Frame(self._settings_frame)
        period_frame.pack(fill="x", pady=(2, 4))
        for period in SYNC_PERIODS:
            ttk.Radiobutton(
                period_frame, text=SYNC_PERIOD_LABELS[period], variable=self._period_var, value=period,
                command=self._save_settings,
            ).pack(anchor="w")
        ttk.Label(
            self._settings_frame,
            text="\"Today only\" is fastest -- the device is asked for everything it has, but only "
            "today's sessions are actually downloaded.",
            wraplength=420, foreground="gray",
        ).pack(anchor="w", pady=(0, 12))

        self._auto_sync_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self._settings_frame, text="Auto-sync whenever this laptop joins the UniGo device's WiFi",
            variable=self._auto_sync_var, command=self._on_auto_sync_toggled,
        ).pack(anchor="w", pady=(0, 12))

        self._sync_button = ttk.Button(self._settings_frame, text="Connect & Sync", command=self._start_sync)
        self._sync_button.pack(fill="x", pady=(0, 8))

        self._sync_status_label = ttk.Label(self._settings_frame, text="Idle", foreground="gray")
        self._sync_status_label.pack(anchor="w")
        self._progress = ttk.Progressbar(self._settings_frame, mode="determinate", maximum=1, value=0)
        self._progress.pack(fill="x", pady=(2, 8))

        pending_row = ttk.Frame(self._settings_frame)
        pending_row.pack(fill="x")
        self._pending_label = ttk.Label(pending_row, text="", wraplength=340, justify="left")
        self._pending_label.pack(side="left", anchor="w")
        # The queue drains on a 15-second background poll, which is
        # invisible from here -- without a button, a user watching "waiting
        # to upload" has no way to tell a stuck queue from a slow one.
        self._upload_now_button = ttk.Button(
            pending_row, text="Upload now", command=self._start_manual_flush,
        )
        self._upload_now_button.pack(side="right")

        ttk.Label(self._settings_frame, text="Status").pack(anchor="w", pady=(12, 0))
        log_frame = ttk.Frame(self._settings_frame)
        log_frame.pack(fill="both", expand=True)
        self._log = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        self._log.pack(fill="both", expand=True, side="left")
        scrollbar = ttk.Scrollbar(log_frame, command=self._log.yview)
        scrollbar.pack(side="right", fill="y")
        self._log.config(yscrollcommand=scrollbar.set)

        self._save_settings()
        self._refresh_pending_label()
        self._start_background_thread()

    def _refresh_driver_combo(self, preselect_profile_id: int | None) -> None:
        labels = [self._driver_label(c) for c in self.driver_choices] + [_ADD_NEW_DRIVER]
        self._driver_combo["values"] = labels
        index = 0
        if preselect_profile_id is not None:
            for i, choice in enumerate(self.driver_choices):
                if choice.profile_id == preselect_profile_id:
                    index = i
                    break
        if labels:
            self._driver_combo.current(index)

    @staticmethod
    def _driver_label(choice: auth_session.DriverChoice) -> str:
        return f"{choice.display_name} (me)" if choice.is_own else choice.display_name

    def _selected_driver(self) -> auth_session.DriverChoice | None:
        selection = self._driver_var.get()
        for choice in self.driver_choices:
            if self._driver_label(choice) == selection:
                return choice
        return None

    def _on_driver_selected(self, _event=None) -> None:
        if self._driver_var.get() == _ADD_NEW_DRIVER:
            name = simpledialog.askstring("Add driver", "Driver name:", parent=self.root)
            if name and name.strip():
                new_choice = auth_session.create_driver(name, self.user_id, self.config.sessions_db)
                self.driver_choices.append(new_choice)
                self._refresh_driver_combo(new_choice.profile_id)
            else:
                self._refresh_driver_combo(None)
        self._save_settings()

    def _save_settings(self) -> None:
        selected = self._selected_driver()
        auth_cache.update_settings(
            self.config.auth_cache_path,
            driver_profile_id=selected.profile_id if selected else None,
            driver_display_name=selected.display_name if selected else None,
            sync_period=self._period_var.get(),
        )

    def _on_auto_sync_toggled(self) -> None:
        self._log_line(
            "Auto-sync enabled -- will sync automatically when this laptop joins the device's WiFi."
            if self._auto_sync_var.get() else "Auto-sync disabled."
        )

    # -- sync ---------------------------------------------------------------

    def _start_sync(self) -> None:
        selected = self._selected_driver()
        if selected is None:
            self._log_line("Choose a driver before syncing.")
            return
        self._sync_button.config(state="disabled")

        if self._period_var.get() == SYNC_PERIOD_ALL:
            # "Everything on the device" can mean a long download on a
            # device that's never been cleared out -- check first and let
            # the user back out, rather than discover the size of it
            # partway through (see core.sync_engine.preview_sync).
            self._set_progress_busy("Checking the device for how much there is to sync...")
            threading.Thread(target=self._preview_worker, args=(selected,), daemon=True).start()
        else:
            self._begin_sync(selected)

    def _preview_worker(self, selected: auth_session.DriverChoice) -> None:
        from ..core.device_client import DeviceError
        from ..core.sync_engine import preview_sync

        try:
            preview = preview_sync(self.config)
        except DeviceError as exc:
            self._ui(self._on_sync_error, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - report, don't crash the app
            logger.exception("preview failed")
            self._ui(self._on_sync_error, str(exc))
            return
        self._ui(self._on_preview_ready, selected, preview)

    def _on_preview_ready(self, selected: auth_session.DriverChoice, preview) -> None:
        if preview.new_count == 0:
            self._reset_progress()
            self._sync_button.config(state="normal")
            self._log_line(f"Nothing new to sync -- {preview.total_on_device} session(s) on the device, all already synced.")
            return

        size_mb = preview.new_bytes / 1_000_000
        proceed = messagebox.askyesno(
            "Sync everything on the device?",
            f"The device has {preview.total_on_device} session(s) in total.\n\n"
            f"{preview.new_count} of them are new (~{size_mb:.1f} MB) and not yet synced. "
            "Downloading everything can take a while on a device with a long history.\n\n"
            "Continue?",
        )
        if not proceed:
            self._reset_progress()
            self._sync_button.config(state="normal")
            self._log_line("Sync cancelled.")
            return
        self._begin_sync(selected)

    def _begin_sync(self, selected: auth_session.DriverChoice) -> None:
        self._sync_button.config(state="disabled")
        self._set_progress_busy(f"Connecting to the UniGo device and syncing for {selected.display_name}...")
        self._log_line(f"Connecting to the UniGo device and syncing for {selected.display_name}...")
        threading.Thread(target=self._sync_worker, args=(selected,), daemon=True).start()

    def _sync_worker(self, selected: auth_session.DriverChoice) -> None:
        cutoff = cutoff_for(self._period_var.get())
        try:
            outcome = sync_and_upload(
                self.config, cutoff, selected.profile_id, selected.display_name,
                uploaded_by_user_id=self.user_id,
                on_progress=lambda i, t, n: self._ui(self._on_sync_progress, i, t, n),
            )
        except Exception as exc:  # noqa: BLE001 - report, don't crash the app
            logger.exception("sync failed")
            self._ui(self._on_sync_error, str(exc))
            return
        self._ui(self._on_sync_done, outcome)

    def _on_sync_progress(self, index: int, total: int, name: str) -> None:
        if str(self._progress["mode"]) != "determinate":
            self._progress.stop()
            self._progress.config(mode="determinate")
        self._progress.config(maximum=max(total, 1))
        self._progress["value"] = index
        self._sync_status_label.config(text=f"Syncing {index} of {total}: {name}")

    def _set_progress_busy(self, status: str) -> None:
        self._sync_status_label.config(text=status)
        self._progress.config(mode="indeterminate")
        self._progress.start(10)

    def _reset_progress(self, status: str = "Idle") -> None:
        self._progress.stop()
        self._progress.config(mode="determinate", maximum=1, value=0)
        self._sync_status_label.config(text=status)

    def _on_sync_error(self, message: str) -> None:
        self._sync_button.config(state="normal")
        self._reset_progress("Failed")
        self._log_line(f"Sync failed: {message}")

    def _on_sync_done(self, outcome) -> None:
        self._sync_button.config(state="normal")
        r = outcome.sync_result
        self._reset_progress(f"Done -- {len(r.new_synced)} new, {len(r.failed)} failed")
        self._log_line(
            f"Sync complete: {len(r.new_synced)} new, {len(r.already_synced)} already synced, "
            f"{len(r.failed)} failed, {len(r.skipped_out_of_period)} outside the selected period."
        )
        if outcome.uploaded:
            self._log_line(f"Uploaded {len(outcome.uploaded)} session(s) to the database.")
        blocked_reason = None
        if outcome.queued:
            if outcome.offline_reason:
                blocked_reason = f"database not reachable: {outcome.offline_reason}"
            elif outcome.upload_errors:
                blocked_reason = f"upload failed: {outcome.upload_errors[0][1]}"
            self._log_line(
                f"{len(outcome.queued)} session(s) staged locally, pending upload -- "
                "retried automatically every 15 seconds, or press \"Upload now\"."
            )
            if blocked_reason:
                self._log_line(f"  Reason: {blocked_reason}")
        for name, error in r.failed:
            self._log_line(f"  FAILED: {name}: {error}")
        self._last_flush_block_reason = blocked_reason
        self._refresh_pending_label(blocked_reason)

    # -- background: connectivity flush + optional wifi auto-sync ----------

    def _start_background_thread(self) -> None:
        if self._background_thread is not None:
            return
        self._background_thread = threading.Thread(target=self._background_loop, daemon=True)
        self._background_thread.start()

    def _background_loop(self) -> None:
        while not self._stop_event.wait(_BACKGROUND_POLL_S):
            # Skipped rather than queued behind a manual flush: another one
            # is already doing exactly this work, and it will report.
            if self._flush_lock.acquire(blocking=False):
                try:
                    outcome = flush_pending_uploads(self.config)
                    self._ui(self._on_flush_done, outcome, False)
                except Exception:  # noqa: BLE001 - keep polling even if one attempt errors
                    logger.exception("background upload flush failed")
                finally:
                    self._flush_lock.release()

            if self._auto_sync_var.get() and is_connected_to_unigo(self.config.wifi_ssid_prefix):
                selected = self._selected_driver()
                if selected is not None:
                    self._ui(lambda: self._sync_button.config(state="disabled"))
                    self._ui(self._set_progress_busy, f"UniGo WiFi detected -- auto-syncing for {selected.display_name}...")
                    self._ui(self._log_line, f"UniGo WiFi detected -- auto-syncing for {selected.display_name}...")
                    self._sync_worker(selected)

    def _start_manual_flush(self) -> None:
        self._upload_now_button.config(state="disabled")
        self._log_line("Trying to upload queued sessions now...")
        threading.Thread(target=self._manual_flush_worker, daemon=True).start()

    def _manual_flush_worker(self) -> None:
        # Blocking, unlike the background poll's try-acquire: the user
        # asked for this, so wait out an in-flight flush rather than
        # answering "nothing happened".
        with self._flush_lock:
            try:
                outcome = flush_pending_uploads(self.config)
            except Exception as exc:  # noqa: BLE001 - report, don't crash the app
                logger.exception("manual upload flush failed")
                self._ui(self._log_line, f"Upload failed: {exc}")
                self._ui(self._enable_upload_button)
                return
        self._ui(self._on_flush_done, outcome, True)

    def _enable_upload_button(self) -> None:
        if self._upload_now_button is not None:
            self._upload_now_button.config(state="normal")

    def _on_flush_done(self, outcome, manual: bool) -> None:
        """`manual` distinguishes a user pressing "Upload now" -- which
        deserves an answer either way -- from the 15-second background
        poll, which must only speak up when something actually changed or
        it would fill the status box with four identical lines a minute."""
        if manual:
            self._enable_upload_button()

        if outcome.uploaded:
            self._log_line(f"Uploaded {len(outcome.uploaded)} queued session(s).")

        reason = outcome.blocked_reason
        if reason and (manual or reason != self._last_flush_block_reason):
            self._log_line(
                f"{len(outcome.still_pending)} session(s) still waiting -- {reason}"
            )
        elif manual and not outcome.uploaded and not reason:
            self._log_line("Nothing was waiting to upload.")
        self._last_flush_block_reason = reason

        self._refresh_pending_label(reason)

    def _refresh_pending_label(self, blocked_reason: str | None = None) -> None:
        from ..core.pending_uploads import PendingUploadQueue

        try:
            with PendingUploadQueue(self.config.pending_uploads_db) as queue:
                count = queue.count()
        except Exception:  # noqa: BLE001 - a locked queue is not worth a crash here
            logger.exception("could not read the pending-upload queue")
            return

        if not count:
            self._pending_label.config(text="Everything is uploaded.")
            return
        text = f"{count} session(s) staged, waiting to upload."
        if blocked_reason:
            text += f"\nNot uploading: {blocked_reason}"
        self._pending_label.config(text=text)

    # -- misc ---------------------------------------------------------------

    def _log_line(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._log.config(state="normal")
        self._log.insert("end", f"[{timestamp}] {message}\n")
        self._log.see("end")
        self._log.config(state="disabled")

    def _sign_out(self) -> None:
        from ..core.connectivity import is_online

        if self.session_token and is_online():
            try:
                auth_session.sign_out(self.session_token, self.config.sessions_db)
            except Exception:  # noqa: BLE001 - local sign-out still proceeds
                logger.exception("failed to revoke session server-side")
        auth_cache.clear(self.config.auth_cache_path)
        self.user_id = self.email = self.session_token = None
        self._settings_frame.destroy()
        self._settings_frame = None
        self._show_login()

    def _on_close(self) -> None:
        self._stop_event.set()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    global tk, ttk, simpledialog, messagebox
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk

    App().run()


if __name__ == "__main__":
    main()
