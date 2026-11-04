# LANTooth

Use your Android phone as a wireless headset for your Windows PC over Wi-Fi.
PC audio plays on the phone, and the phone's microphone works as a mic on the PC.

## Install

Get the latest [release](../../releases):

1. **PC:** run `LANTooth-<version>-setup.exe`. It installs per-user, so no
   admin rights are needed. Or unzip `LANTooth-<version>-win64.zip` and run
   `LANTooth.exe`.
2. **Phone:** install `LANTooth-<version>.apk`. You may need to allow installs
   from unknown sources. Open it and grant the microphone and notification
   permissions.

The PC and phone apps must be the same version.

## Use

1. Open LANTooth on the phone. Its IP address is shown on the main screen.
2. On the PC, type that IP into LANTooth and press **Connect**.
3. First time only: both screens show an 8-digit code. If it is identical on the
   PC and the phone, confirm on both. Later connections are automatic.

The PC remembers the phones it has connected to and reconnects to the last one
at startup. Closing the PC window minimizes LANTooth to the tray; use **Quit**
there to exit.

**Using the phone as a mic in other apps:** install a virtual audio cable such
as [VB-CABLE](https://vb-audio.com/Cable/). In LANTooth, choose *CABLE Input*
as the phone-mic output. In Discord, Zoom or similar, pick *CABLE Output* as
the microphone.

## Better sound on Wi-Fi

LANTooth measures the connection while it streams. When packets get lost it
repeats recent audio in later packets, and on a bad link it lowers the bitrate
until the link is clean again. Your network setup still matters most:

- **Use 5 GHz Wi-Fi** for the phone, and keep it within good range of the
  router or access point.
- **Keep WMM enabled** on the router (usually on by default). It provides the
  priority queues used below.
- **Wi-Fi priority:** in the PC app, press **Set up…** next to *Wi-Fi priority
  mark* once (needs administrator rights). The access point then sends the PC's
  audio to the phone ahead of other traffic, and the phone marks its mic audio
  the same way. Keep the recommended value 48 unless your router documents a
  different voice mapping; then try 46.
- **On the phone, turn off Wi-Fi scanning** (Settings → Location → Location
  services → Wi-Fi scanning). Background scans pause the Wi-Fi radio for 100–300 ms.
- The PC stats line shows packet loss, bitrate and stalls for each direction.
  Steady loss above a few percent means the Wi-Fi link is weak.

## Build from source

| Part | Requirements | Command |
|---|---|---|
| PC exe + zip | Python 3.10+ x64 and a 64-bit `opus.dll`/`libopus-0.dll` in `pc\` | `powershell -ExecutionPolicy Bypass -File pc\build_exe.ps1` |
| PC installer | the above plus [Inno Setup 6](https://jrsoftware.org/isinfo.php) | `... pc\build_exe.ps1 -Installer` |
| Android APK | Android SDK. Gradle downloads the NDK and CMake itself. | `cd android && gradlew assembleDebug` |
| PC self-test | — | `pc\.venv\Scripts\python pc\selftest.py` |
| Android unit tests | Android SDK | `cd android && gradlew testDebugUnitTest` |

To run the PC app from source: `pip install -r pc/requirements.txt`, then
`python pc/gui.py` (GUI) or `python pc/main.py --help` (CLI).

A signed Android release build needs `android/keystore.properties` (not in the
repo; see `android/app/build.gradle.kts`).

## Releasing

The version lives in the `VERSION` file.

1. Update `VERSION` and add a section for the new version to `CHANGELOG.md`.
2. Commit, then push a tag: `git tag v<version> && git push origin v<version>`.

The [Release workflow](.github/workflows/release.yml) builds the installer,
the zip and the signed APK, then publishes the GitHub release.
