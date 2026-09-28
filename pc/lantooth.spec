# PyInstaller spec — builds dist\LANTooth\ with two executables sharing one runtime:
#   LANTooth.exe      GUI (windowed, tray icon)
#   lantooth-cli.exe  console client (main.py)
# Run via build_exe.ps1, or:  python -m PyInstaller --noconfirm --clean lantooth.spec
import os

from PIL import Image
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

HERE = os.path.abspath(SPECPATH)
VERSION_FILE = os.path.join(HERE, os.pardir, "VERSION")
with open(VERSION_FILE, encoding="utf-8") as f:
    VERSION = f.read().strip()
# Windows version resources need exactly four integers: 0.1.0 -> (0, 1, 0, 0)
APP_VERSION = tuple((list(map(int, VERSION.split("."))) + [0, 0, 0, 0])[:4])
BUILD = os.path.join(HERE, "build")
os.makedirs(BUILD, exist_ok=True)

# ── libopus: shipped next to the exe, found first by codec._load_libopus ────
opus_dll = next((n for n in ("opus.dll", "libopus-0.dll", "libopus.dll")
                 if os.path.isfile(os.path.join(HERE, n))), None)
if opus_dll is None:
    raise SystemExit("Put a 64-bit opus.dll / libopus-0.dll into pc\\ before building.")

# ── icon: assets/lantooth_icon.png -> .ico for the exes, plus a 256px copy the
# GUI loads at runtime for its window/tray icon (appdata.resource_path) ───────
art = Image.open(os.path.join(HERE, os.pardir, "assets", "lantooth_icon.png")).convert("RGBA")
art = art.crop(art.getbbox())  # trim the transparent margin, then re-square it
side = max(art.size)
square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
square.alpha_composite(art, ((side - art.width) // 2, (side - art.height) // 2))
art = square
icon_path = os.path.join(BUILD, "lantooth.ico")
art.save(icon_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
icon_png = os.path.join(BUILD, "lantooth_icon.png")
art.resize((256, 256), Image.LANCZOS).save(icon_png, optimize=True)


def _version(description: str, internal: str) -> VSVersionInfo:
    v = VERSION
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=APP_VERSION, prodvers=APP_VERSION),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("ProductName", "LANTooth"),
                StringStruct("FileDescription", description),
                StringStruct("InternalName", internal),
                StringStruct("OriginalFilename", f"{internal}.exe"),
                StringStruct("FileVersion", v),
                StringStruct("ProductVersion", v),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


common = dict(
    pathex=[HERE],
    binaries=[(os.path.join(HERE, opus_dll), ".")],
    datas=[(icon_png, "."), (VERSION_FILE, ".")],
    excludes=["pywin32", "win32crypt", "pythoncom", "pywintypes", "unittest", "pydoc", "test"],
)

gui = Analysis([os.path.join(HERE, "gui.py")], hiddenimports=["pystray._win32"], **common)
cli = Analysis([os.path.join(HERE, "main.py")], **common)

gui_exe = EXE(
    PYZ(gui.pure), gui.scripts, [],
    exclude_binaries=True, name="LANTooth", console=False,
    icon=icon_path, version=_version("LANTooth — LAN audio bridge", "LANTooth"),
)
cli_exe = EXE(
    PYZ(cli.pure), cli.scripts, [],
    exclude_binaries=True, name="lantooth-cli", console=True,
    icon=icon_path, version=_version("LANTooth command-line client", "lantooth-cli"),
)

COLLECT(
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    name="LANTooth",
)
