"""
LANTooth PC — client mode
Usage:
  python main.py                          # pick a remembered phone or type its IP
  python main.py --android-ip 192.168.x.x # connect directly by IP
  python main.py --list-devices           # list capture options and exit
  python main.py --capture N              # use capture device N (index from --list-devices)
  python main.py --loopback               # WASAPI loopback of default output (system audio)
  python main.py --playback M             # use playback device M for Android mic audio,
                                           #   skipping the interactive picker
  python main.py --mono                   # send mono instead of stereo to the phone
  python main.py --version                # protocol + libopus version (checks the DLL loads)
"""

import argparse
import ipaddress
import socket
import sys
import time

from appdata import setup_logging
from audio_engine import list_capture_options, list_playback_options, list_devices
from config import load_config, remember_ip
from identity import load_or_create_identity
from pairing import ConnectClient
from protocol import PAIRING_PORT, PROTOCOL_VERSION
from version import __version__
from session import END_BYE, END_ERROR, connect_once, stream_session, SessionStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _my_ips() -> list[str]:
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips or ["(unknown)"]


def _ask_ip() -> str:
    print("  (The phone's IP is shown on the LANTooth main screen.)")
    while True:
        ip = input("  Phone IP: ").strip()
        try:
            ipaddress.ip_address(ip)
            return ip
        except ValueError:
            print("  Invalid IP address (e.g. 192.168.1.100).")


def _select_android(args, cfg: dict) -> str:
    """Phone IP: --android-ip, else a remembered phone (shared with the GUI), else typed in."""
    if args.android_ip:
        return args.android_ip

    recent = cfg.get("recent_ips", [])
    if not recent:
        return _ask_ip()

    print("  Remembered phones:")
    for n, ip in enumerate(recent, 1):
        print(f"    [{n}] {ip}")
    print("    [0] Enter a new IP")
    while True:
        answer = input(f"  Select [0-{len(recent)}, Enter = 1]: ").strip() or "1"
        try:
            choice = int(answer)
        except ValueError:
            continue
        if choice == 0:
            return _ask_ip()
        if 1 <= choice <= len(recent):
            return recent[choice - 1]


def _select_capture(args) -> tuple[str, int | None]:
    """Return (backend, device_index).

    backend is "sd" (sounddevice, for mic/Stereo Mix) or
    "wasapi" (PyAudioWPatch WASAPI loopback, for system audio).
    """
    if args.loopback:
        return "wasapi", args.capture  # args.capture may be None → default output

    if args.capture is not None:
        return "sd", args.capture

    options = list_capture_options()
    if not options:
        print("  No capture devices found. Using system default.")
        return "sd", None

    print()
    print("  Audio capture sources:")
    for n, (backend, idx, name, _) in enumerate(options, 1):
        print(f"    [{n}] {name}")
    print()

    while True:
        try:
            choice = int(input(f"  Select source [1-{len(options)}]: "))
            if 1 <= choice <= len(options):
                backend, idx, name, _ = options[choice - 1]
                print(f"  Using: {name}")
                return backend, idx
        except ValueError:
            pass


def _select_playback(args) -> int | None:
    """Return the device index to play the Android mic audio on, or None for system default."""
    if args.playback is not None:
        return args.playback

    options = list_playback_options()
    if not options:
        print("  No playback devices found. Using system default.")
        return None

    has_cable = any(is_cable for _, _, is_cable in options)

    print()
    print("  Where should the phone's mic audio go?")
    print("    A real speaker/headphone device here only lets YOU hear the phone's")
    print("    mic — other apps (Discord/Zoom/games) can't pick it as their mic,")
    print("    Windows doesn't allow a plain app to register as a new microphone.")
    if has_cable:
        print("    To make it usable as a real mic elsewhere, pick the [virtual cable]")
        print("    device below, then select its matching *recording* device as your")
        print("    mic in the other app.")
    else:
        print("    To make it usable as a real mic elsewhere, install a virtual audio")
        print("    cable (e.g. VB-CABLE, free: https://vb-audio.com/Cable/), re-run,")
        print("    and pick it here — then select its matching recording device as")
        print("    your mic in the other app.")
    print()
    print("    [0] System default (monitor only)")
    for n, (_, name, is_cable) in enumerate(options, 1):
        tag = "  [virtual cable]" if is_cable else ""
        print(f"    [{n}] {name}{tag}")
    print()

    while True:
        try:
            choice = int(input(f"  Select device [0-{len(options)}]: "))
            if choice == 0:
                return None
            if 1 <= choice <= len(options):
                idx, name, _ = options[choice - 1]
                print(f"  Using: {name}")
                return idx
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def _print_status(text: str) -> None:
    print(f"  {text}")


def _print_stats(stats: SessionStats) -> None:
    print(f"  [stats] sent_to_phone={stats.sent}  "
          f"from_phone_mic: received={stats.received} recovered={stats.recovered} "
          f"concealed={stats.concealed} underruns={stats.underruns} late={stats.late} "
          f"trimmed={stats.trimmed} buffer={stats.target_depth * 20}ms "
          f"decode_errors={stats.decode_errors}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="LANTooth PC (client mode)")
    parser.add_argument("--android-ip", metavar="IP",
                        help="Phone IP (skips the remembered-phone picker)")
    parser.add_argument("--list-devices", action="store_true",
                        help="List capture options and exit")
    parser.add_argument("--capture", type=int, default=None, metavar="IDX",
                        help="Capture device index (from --list-devices)")
    parser.add_argument("--loopback", action="store_true",
                        help="WASAPI loopback of the device given by --capture (or default output)")
    parser.add_argument("--playback", type=int, default=None, metavar="IDX",
                        help="Playback device for Android mic audio (skips the interactive picker)")
    parser.add_argument("--mono", action="store_true",
                        help="Send mono instead of stereo to the phone")
    parser.add_argument("--version", action="store_true",
                        help="Print protocol and libopus versions and exit")
    args = parser.parse_args()

    if args.version:
        import codec
        try:
            opus = codec.libopus_version()
        except OSError as e:
            opus = f"NOT FOUND ({e})"
        print(f"LANTooth PC {__version__}  protocol v{PROTOCOL_VERSION}  {opus}")
        sys.exit(0)
    setup_logging(console=False)  # prints already go to the console; keep the log file for errors
    media_channels = 1 if args.mono else 2

    if args.android_ip:
        try:
            ipaddress.ip_address(args.android_ip)
        except ValueError:
            print(f"  Invalid --android-ip value: {args.android_ip!r}")
            sys.exit(1)

    if args.capture is not None and args.capture < 0:
        print("  --capture index must be >= 0")
        sys.exit(1)
    if args.playback is not None and args.playback < 0:
        print("  --playback index must be >= 0")
        sys.exit(1)

    if args.list_devices:
        print("Capture options:")
        for backend, idx, name, _ in list_capture_options():
            tag = " [WASAPI loopback]" if backend == "wasapi" else ""
            idx_str = f"{idx:3d}" if idx is not None else "  -"
            print(f"  {idx_str}  {name}{tag}")
        print("\nAll devices:")
        print(list_devices())
        sys.exit(0)

    print("=" * 50)
    print(f"  LANTooth PC {__version__}")
    print("=" * 50)
    print(f"  PC IP(s): {', '.join(_my_ips())}")
    print()

    identity_priv = load_or_create_identity()
    display_name = socket.gethostname()
    client = ConnectClient()

    try:
        # 1. Find Android, 2. audio source and destination — asked once; a
        # dropped link reconnects to the same phone/devices without re-asking.
        cfg = load_config()
        android_ip, android_port = _select_android(args, cfg), PAIRING_PORT
        print(f"  Target: {android_ip}:{android_port}")
        print()
        capture_backend, capture_device = _select_capture(args)
        playback_device = _select_playback(args)

        while True:
            # 3. Connect handshake
            print("  Waiting for you to accept the connection on your Android device…")
            try:
                session, our_stream_id, audio_sock = connect_once(
                    client, android_ip, android_port, identity_priv, display_name,
                    media_channels=media_channels,
                )
            except Exception as e:
                print(f"  Connection failed: {e}")
                time.sleep(2.0)
                continue

            remember_ip(cfg, android_ip)
            print(f"  Connected! Android audio port: {session.android_audio_port}")
            print()

            # 4. Stream — audio_sock ownership transfers to UDPStream; stream.stop() closes it
            reason = stream_session(
                android_ip=android_ip,
                session=session,
                our_stream_id=our_stream_id,
                capture_backend=capture_backend,
                capture_device=capture_device,
                playback_device=playback_device,
                audio_sock=audio_sock,
                should_stop=lambda: False,
                on_status=_print_status,
                on_stats=_print_stats,
                media_channels=media_channels,
            )
            print()
            if reason in (END_BYE, END_ERROR):
                break

    except KeyboardInterrupt:
        print("\n  Shutting down.")


if __name__ == "__main__":
    main()
