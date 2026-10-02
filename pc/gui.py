"""
LANTooth PC — GUI client.

Small window with a phone-IP field and capture/playback device pickers.
Minimizes to a system tray icon instead of closing. Connection is by IP only:
type the phone's IP once (shown on the phone's main screen); every phone
connected to successfully is remembered in the IP dropdown, and the last one is
reconnected automatically on launch. The phone in turn remembers this PC once
you've accepted it, so later connects need no confirmation.

Usage:
  python gui.py
"""

import ctypes
import ipaddress
import logging
import queue
import socket
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import pystray
from PIL import Image, ImageDraw, ImageTk

from appdata import resource_path, setup_logging
from audio_engine import hostapi_name, is_wdm_ks, list_capture_options, list_playback_options
from config import forget_ip, load_config, remember_ip, save_config
from identity import load_or_create_identity
from pairing import ConnectCancelled, ConnectClient
from protocol import PAIRING_PORT
from version import __version__
from session import (
    END_BYE, END_ERROR, SessionStats, connect_once, stream_session,
)

_log = logging.getLogger("lantooth.gui")

RECONNECT_DELAY_S = 5.0
CONNECT_ATTEMPT_TIMEOUT_S = 45.0   # first pairing needs time to compare the code on both devices
SYSTEM_DEFAULT_PLAYBACK_LABEL = "System default (monitor only)"
_ICON_FILE = "lantooth_icon.png"


def _load_icon_art() -> Image.Image:
    """The app artwork (assets/lantooth_icon.png), or a plain fallback glyph if
    it's missing so the tray icon never fails to appear."""
    try:
        return Image.open(resource_path(_ICON_FILE)).convert("RGBA")
    except OSError:
        _log.warning("icon %s not found, using fallback", _ICON_FILE)
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, size - 4, size - 4), fill=(37, 99, 235, 255))
        d.ellipse((size // 2 - 10, size // 2 - 10, size // 2 + 10, size // 2 + 10), fill=(255, 255, 255, 255))
        return img
_SINGLE_INSTANCE_MUTEX = "Local\\LANTooth.GUI"


class StreamWorker:
    """Runs the connect handshake + audio streaming loop on a background thread.

    Auto-retries the connect handshake (Android not reachable/ready yet) until it
    succeeds or stop() is called. Once a session is established, a dropped link is
    detected via the keepalive/liveness check in session.stream_session, which then
    returns so this loop can reconnect — unless the phone hung up deliberately
    (BYE), in which case reconnecting would just drag it straight back.

    stop() never blocks: the handshake and session loop both poll the stop event
    every few hundred ms, and the thread reports its end with a "stopped" message.
    Messages carry the worker generation so the GUI can ignore stragglers from a
    previous run.
    """

    def __init__(self, msg_queue: "queue.Queue[tuple[str, str, int]]"):
        self._q = msg_queue
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._identity_priv = load_or_create_identity()
        self._display_name = socket.gethostname()
        self._client = ConnectClient(confirm=self._ask_pairing_code)
        self.generation = 0
        self._confirm_answer: bool | None = None
        self._confirm_ready = threading.Event()
        self._gen_now = 0

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_stopping(self) -> bool:
        return self.is_running() and self._stop.is_set()

    def start(self, android_ip, capture_backend, capture_device, playback_device, media_channels) -> bool:
        if self.is_running():
            return False
        self._stop.clear()
        self.generation += 1
        self._gen_now = self.generation
        self._thread = threading.Thread(
            target=self._run,
            args=(self.generation, android_ip, capture_backend, capture_device,
                  playback_device, media_channels),
            daemon=True,
            name="lantooth-worker",
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _ask_pairing_code(self, code: str) -> bool:
        """Called on the worker thread at first pairing: has the GUI thread ask the
        user whether `code` matches the one on the phone, and waits for the answer."""
        self._confirm_answer = None
        self._confirm_ready.clear()
        self._q.put(("confirm", code, self._gen_now))
        while not self._confirm_ready.wait(0.25):
            if self._stop.is_set():
                raise ConnectCancelled()
        return bool(self._confirm_answer)

    def answer_pairing(self, ok: bool) -> None:
        self._confirm_answer = ok
        self._confirm_ready.set()

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, gen, android_ip, capture_backend, capture_device, playback_device, media_channels) -> None:
        def status(text: str) -> None:
            self._q.put(("status", text, gen))

        def stats(s: SessionStats) -> None:
            self._q.put(("stats", (
                f"to phone: {s.sent}   from phone: {s.received} recv · {s.recovered} recovered · "
                f"{s.concealed} lost · {s.underruns} underruns   buffer {s.target_depth * 20} ms"
            ), gen))

        final = ""
        try:
            while not self._stop.is_set():
                status(f"Connecting to {android_ip}…")
                try:
                    session, our_stream_id, audio_sock = connect_once(
                        self._client, android_ip, PAIRING_PORT, self._identity_priv,
                        self._display_name, media_channels=media_channels,
                        timeout=CONNECT_ATTEMPT_TIMEOUT_S, stop_event=self._stop,
                    )
                except ConnectCancelled:
                    break
                except Exception as e:
                    status(f"Connect failed: {e}")
                    if self._stop.wait(RECONNECT_DELAY_S):
                        break
                    continue

                self._q.put(("connected", android_ip, gen))
                status(f"Connected: {android_ip}")
                reason = stream_session(
                    android_ip=android_ip,
                    session=session,
                    our_stream_id=our_stream_id,
                    capture_backend=capture_backend,
                    capture_device=capture_device,
                    playback_device=playback_device,
                    audio_sock=audio_sock,
                    should_stop=self._stop.is_set,
                    on_status=status,
                    on_stats=stats,
                    media_channels=media_channels,
                )
                if reason == END_BYE:
                    final = "Phone disconnected — press Connect to reconnect"
                    break
                if reason == END_ERROR:
                    final = "Stopped: audio device error (see message above / log)"
                    break
                if self._stop.is_set():
                    break
                status("Disconnected — reconnecting…")
        except Exception:
            _log.exception("worker crashed")
            final = "Stopped: unexpected error (see lantooth.log)"
        finally:
            self._q.put(("stopped", final, gen))


class LanToothGUI:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.msg_queue: "queue.Queue[tuple[str, str, int]]" = queue.Queue()
        self.worker = StreamWorker(self.msg_queue)
        self.tray_icon: pystray.Icon | None = None

        self.capture_options: list[tuple[str, int | None, str, bool]] = []
        self.playback_options: list[tuple[int | None, str, bool]] = []

        self.root = tk.Tk()
        self.root.title(f"LANTooth {__version__}")
        self.root.geometry("640x360")
        self.root.minsize(480, 340)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.root.bind("<Unmap>", self._on_unmap)
        self.root.bind("<Configure>", self._on_resize)
        self.root.report_callback_exception = self._on_tk_error
        self.icon_art = _load_icon_art()
        # Several sizes so Windows picks a sharp one for title bar vs. taskbar/Alt-Tab.
        self._window_icons = [
            ImageTk.PhotoImage(self.icon_art.resize((n, n), Image.LANCZOS)) for n in (16, 32, 48, 256)
        ]
        self.root.iconphoto(True, *self._window_icons)

        self._build_widgets()
        self._populate_devices()

        if self.cfg.get("android_ip"):
            self.root.after(300, self._start_worker)

        self._poll_queue()

    def run(self) -> None:
        self.root.mainloop()

    @staticmethod
    def _on_tk_error(exc, val, tb) -> None:
        _log.error("Tk callback error", exc_info=(exc, val, tb))

    # -----------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------

    def _build_widgets(self) -> None:
        pad = {"padx": 10, "pady": 4}

        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Phone IP:").grid(row=0, column=0, sticky="w", **pad)
        ip_row = ttk.Frame(frm)
        ip_row.grid(row=0, column=1, sticky="we", **pad)
        self.ip_var = tk.StringVar(value=self.cfg.get("android_ip", ""))
        # Editable: pick a remembered phone or type a new IP.
        self.ip_combo = ttk.Combobox(ip_row, textvariable=self.ip_var, width=28,
                                     values=self.cfg.get("recent_ips", []))
        self.ip_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(ip_row, text="Forget", width=8, command=self._forget_ip).pack(side="left", padx=(6, 0))

        ttk.Label(frm, text="PC → Phone  (your audio to send):").grid(row=1, column=0, sticky="w", **pad)
        self.capture_combo = ttk.Combobox(frm, state="readonly", width=46)
        self.capture_combo.grid(row=1, column=1, sticky="we", **pad)

        self.stereo_var = tk.BooleanVar(value=bool(self.cfg.get("stereo", True)))
        ttk.Checkbutton(
            frm, text="Stereo to phone (music) — untick for mono (speech, lower bandwidth)",
            variable=self.stereo_var,
        ).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Phone → PC  (play phone's mic on):").grid(row=3, column=0, sticky="w", **pad)
        self.playback_combo = ttk.Combobox(frm, state="readonly", width=46)
        self.playback_combo.grid(row=3, column=1, sticky="we", **pad)
        self.playback_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_playback_hint())

        self.connect_btn = ttk.Button(frm, text="Connect", command=self._toggle_connect)
        self.connect_btn.grid(row=4, column=0, columnspan=2, sticky="we", **pad)

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(frm, textvariable=self.status_var, wraplength=560)
        self.status_label.grid(row=5, column=0, columnspan=2, sticky="w", **pad)

        self.stats_var = tk.StringVar(value="")
        self.stats_label = ttk.Label(frm, textvariable=self.stats_var, wraplength=560, foreground="#666")
        self.stats_label.grid(row=6, column=0, columnspan=2, sticky="w", **pad)

        self.playback_hint_var = tk.StringVar(value="")
        self.playback_hint_label = ttk.Label(
            frm, textvariable=self.playback_hint_var, wraplength=560, foreground="#888"
        )
        self.playback_hint_label.grid(row=7, column=0, columnspan=2, sticky="w", **pad)

        self.footer_label = ttk.Label(
            frm, text="Closing this window minimizes to the tray — use Quit there to exit.",
            wraplength=560, foreground="#888",
        )
        self.footer_label.grid(row=8, column=0, columnspan=2, sticky="w", **pad)

        frm.columnconfigure(1, weight=1)

    def _on_resize(self, event: "tk.Event") -> None:
        if event.widget is not self.root:
            return
        wrap = max(300, event.width - 60)
        for label in (self.status_label, self.stats_label, self.playback_hint_label, self.footer_label):
            label.config(wraplength=wrap)

    @staticmethod
    def _hostapi_suffix(device_index: int | None) -> str:
        name = hostapi_name(device_index)
        return f"  ({name})" if name else ""

    def _populate_devices(self) -> None:
        self.capture_options = [
            opt for opt in list_capture_options()
            if opt[3] and not (opt[0] == "sd" and is_wdm_ks(opt[1]))
        ]
        labels = [
            label + (self._hostapi_suffix(idx) if backend == "sd" else "")
            for backend, idx, label, _ in self.capture_options
        ]
        self.capture_combo["values"] = labels
        saved = self.cfg.get("capture_device_name", "")
        if saved and saved in labels:
            self.capture_combo.current(labels.index(saved))
        elif labels:
            default_i = next(
                (i for i, (_, _, _, is_sys) in enumerate(self.capture_options) if is_sys), 0
            )
            self.capture_combo.current(default_i)

        self.playback_options = [(None, SYSTEM_DEFAULT_PLAYBACK_LABEL, False)] + [
            opt for opt in list_playback_options() if not is_wdm_ks(opt[0])
        ]
        play_labels = [
            name + (self._hostapi_suffix(idx) if idx is not None else "")
            for idx, name, _ in self.playback_options
        ]
        self.playback_combo["values"] = play_labels
        saved_play = self.cfg.get("playback_device_name", "")
        if saved_play and saved_play in play_labels:
            self.playback_combo.current(play_labels.index(saved_play))
        else:
            self.playback_combo.current(self._default_playback_index())
        self._update_playback_hint()

    def _cable_indices(self) -> list[int]:
        return [i for i, (_, _, is_cable) in enumerate(self.playback_options) if is_cable]

    def _default_playback_index(self) -> int:
        """First run (nothing saved): route the phone mic into a virtual cable if
        one is installed — that's what makes it usable as a mic in OBS/Discord —
        preferring its WASAPI entry. Otherwise System default."""
        cables = self._cable_indices()
        if not cables:
            return 0
        rank = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2}
        return min(cables, key=lambda i: rank.get(hostapi_name(self.playback_options[i][0]), 9))

    @staticmethod
    def _recording_side(playback_name: str) -> str:
        """The recording device a virtual cable's playback end shows up as —
        "CABLE Input (VB-Audio ...)" -> "CABLE Output (VB-Audio ...)"."""
        return playback_name.replace("Input", "Output", 1) if "Input" in playback_name else playback_name

    def _update_playback_hint(self) -> None:
        i = self.playback_combo.current()
        if i < 0 or not self.playback_options:
            self.playback_hint_var.set("")
            return
        _, name, is_cable = self.playback_options[i]
        later = "  (Applies on the next Connect.)" if self.worker.is_running() else ""
        if is_cable:
            self.playback_hint_label.config(foreground="#888")
            self.playback_hint_var.set(
                f"✓ Phone mic is available to other apps as the microphone "
                f"“{self._recording_side(name)}” — select that in OBS/Discord/Zoom.{later}"
            )
            return

        self.playback_hint_label.config(foreground="#B45309")
        cables = self._cable_indices()
        if cables:
            cable = self.playback_options[self._default_playback_index()][1]
            self.playback_hint_var.set(
                f"⚠ The phone mic only plays on this device, so OBS/Discord/Zoom can't use it as a "
                f"microphone. To use it as a mic, choose “{cable}” above.{later}"
            )
        else:
            self.playback_hint_var.set(
                "⚠ The phone mic only plays on this device, so other apps can't use it as a microphone. "
                "For that, install a virtual audio cable (e.g. VB-CABLE, vb-audio.com/Cable) and pick "
                f"its “CABLE Input” here.{later}"
            )

    # -----------------------------------------------------------------
    # Connect / disconnect
    # -----------------------------------------------------------------

    def _selected_capture(self):
        i = self.capture_combo.current()
        if i < 0 or not self.capture_options:
            return "wasapi", None
        backend, idx, _, _ = self.capture_options[i]
        return backend, idx

    def _selected_playback(self):
        i = self.playback_combo.current()
        if i < 0 or not self.playback_options:
            return None
        idx, _, _ = self.playback_options[i]
        return idx

    def _toggle_connect(self) -> None:
        if self.worker.is_running() and not self.worker.is_stopping():
            self._stop_worker()
        else:
            self._start_worker()

    def _start_worker(self) -> None:
        if self.worker.is_stopping():
            # Previous run is still winding down (<~1s) — try again shortly.
            self.root.after(200, self._start_worker)
            return
        if self.worker.is_running():
            return

        ip = self.ip_var.get().strip()
        if not ip:
            self.status_var.set("Enter an Android IP first")
            return
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            self.status_var.set(f"Invalid IP address: {ip!r}")
            return

        backend, cap_idx = self._selected_capture()
        play_idx = self._selected_playback()
        stereo = self.stereo_var.get()

        # The IP itself is remembered only once a connection succeeds ("connected").
        self.cfg.update({
            "capture_device_name": self.capture_combo.get(),
            "playback_device_name": self.playback_combo.get(),
            "stereo": stereo,
        })
        save_config(self.cfg)

        if self.worker.start(ip, backend, cap_idx, play_idx, 2 if stereo else 1):
            self.connect_btn.config(text="Disconnect")

    def _forget_ip(self) -> None:
        ip = self.ip_var.get().strip()
        if ip in self.cfg.get("recent_ips", []):
            forget_ip(self.cfg, ip)
            self.ip_combo["values"] = self.cfg["recent_ips"]
            self.ip_var.set("")
            self.status_var.set(f"Forgot {ip}")

    def _stop_worker(self) -> None:
        if not self.worker.is_running():
            return
        self.worker.stop()
        self.connect_btn.config(text="Connect")
        self.status_var.set("Disconnecting…")
        self.stats_var.set("")

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, text, gen = self.msg_queue.get_nowait()
                if gen != self.worker.generation:
                    continue  # from an earlier run
                if kind == "connected":
                    remember_ip(self.cfg, text)
                    self.ip_combo["values"] = self.cfg["recent_ips"]
                elif kind == "confirm":
                    self._show_window()
                    ok = messagebox.askyesno(
                        "Pair with phone",
                        f"Your phone should now show this code:\n\n        {text}\n\n"
                        "Does it match exactly?\n(Only say Yes if you started this connection and the codes are identical.)",
                        parent=self.root,
                    )
                    self.worker.answer_pairing(ok)
                elif kind == "status":
                    self.status_var.set(text)
                elif kind == "stats":
                    self.stats_var.set(text)
                elif kind == "stopped":
                    self.connect_btn.config(text="Connect")
                    self.status_var.set(text or "Idle")
                    if text:
                        self.stats_var.set("")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    # -----------------------------------------------------------------
    # Tray
    # -----------------------------------------------------------------

    def _on_unmap(self, _event) -> None:
        if self.root.state() == "iconic":
            self.root.after(10, self._hide_to_tray)

    def _hide_to_tray(self) -> None:
        self.root.withdraw()
        if self.tray_icon is None:
            self._create_tray_icon()

    def _create_tray_icon(self) -> None:
        image = self.icon_art.resize((64, 64), Image.LANCZOS)
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda: self.root.after(0, self._show_window), default=True),
            pystray.MenuItem("Connect", lambda: self.root.after(0, self._start_worker)),
            pystray.MenuItem("Disconnect", lambda: self.root.after(0, self._stop_worker)),
            pystray.MenuItem("Quit", lambda: self.root.after(0, self._quit)),
        )
        self.tray_icon = pystray.Icon("lantooth", image, "LANTooth", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True, name="lantooth-tray").start()

    def _show_window(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.deiconify()
        self.root.state("normal")

    def _quit(self) -> None:
        self.worker.stop()
        self.worker.join(2.0)  # let the session send its BYE and release audio devices
        if self.tray_icon is not None:
            self.tray_icon.stop()
        self.root.after(0, self.root.destroy)


def _acquire_single_instance() -> object | None:
    """Named mutex so a second launch (e.g. double-clicked exe) doesn't fight the
    first over the audio devices. Returns the handle (keep it alive) or None."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def main() -> None:
    log_path = setup_logging(console=sys.stderr is not None)
    _log.info("LANTooth GUI %s starting (log: %s)", __version__, log_path)
    mutex = _acquire_single_instance()
    if mutex is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("LANTooth", "LANTooth is already running — look for its tray icon.")
        return
    try:
        LanToothGUI().run()
    except Exception:
        _log.exception("fatal error")
        raise


if __name__ == "__main__":
    main()
