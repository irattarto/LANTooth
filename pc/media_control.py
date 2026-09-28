import ctypes
import ctypes.wintypes as _wt
from protocol import (
    CMD_PLAY_PAUSE, CMD_NEXT_TRACK, CMD_PREV_TRACK, CMD_STOP,
)

# Windows virtual key codes for media keys
_VK_MEDIA_PLAY_PAUSE = 0xB3
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1
_VK_MEDIA_STOP       = 0xB2

_CMD_MAP = {
    CMD_PLAY_PAUSE: _VK_MEDIA_PLAY_PAUSE,
    CMD_NEXT_TRACK: _VK_MEDIA_NEXT_TRACK,
    CMD_PREV_TRACK: _VK_MEDIA_PREV_TRACK,
    CMD_STOP:       _VK_MEDIA_STOP,
}

_KEYEVENTF_KEYUP = 0x0002


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         _wt.WORD),
        ("wScan",       _wt.WORD),
        ("dwFlags",     _wt.DWORD),
        ("time",        _wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          _wt.LONG),
        ("dy",          _wt.LONG),
        ("mouseData",   _wt.DWORD),
        ("dwFlags",     _wt.DWORD),
        ("time",        _wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", _wt.DWORD), ("u", _INPUTunion)]


_SendInput = ctypes.windll.user32.SendInput
_SendInput.restype  = _wt.UINT
_SendInput.argtypes = [_wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]


def _press(vk: int) -> None:
    ev_dn = _INPUT(1, _INPUTunion(ki=_KEYBDINPUT(vk, 0, 0, 0, 0)))
    ev_up = _INPUT(1, _INPUTunion(ki=_KEYBDINPUT(vk, 0, _KEYEVENTF_KEYUP, 0, 0)))
    _SendInput(2, (_INPUT * 2)(ev_dn, ev_up), ctypes.sizeof(_INPUT))


def handle_control(command: int, value: int = 0) -> None:
    """Translate a LANTooth control command into a system-wide media key press."""
    vk = _CMD_MAP.get(command)
    if vk:
        _press(vk)
