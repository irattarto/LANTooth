# Changelog

## [0.1.1] — 2026-09-28

PC-side improvement. The phone app is unchanged: 0.1.0 and 0.1.1 are fully
compatible (same protocol v2), and the 0.1.1 APK is included only to keep the
version numbers matching.

### Improved
- PC: on first run the phone-mic output defaults to an installed virtual cable
  (VB-CABLE, preferring its WASAPI entry) instead of System default.
- PC: the hint under the phone-mic output names the exact microphone to select
  in OBS/Discord/Zoom, and shows an orange warning when the mic goes to
  speakers/System default, where other apps can't use it as a microphone.

## [0.1.0] — 2026-09-28

First versioned release. Install **both** parts from this release: the PC and
phone apps must be the same version (protocol v2).

### Downloads
- `LANTooth-0.1.0-setup.exe`: Windows installer (per-user, no admin rights needed)
- `LANTooth-0.1.0-win64.zip`: portable Windows version (unzip and run `LANTooth.exe`)
- `LANTooth-0.1.0.apk`: Android app (Android 9+)

### Upgrading from a pre-0.1 build
- **Android:** uninstall the old app first. This release is signed with a new
  key, so Android refuses to install it over a build signed by the old one.
  Then accept the PC once on the phone.
- **PC:** settings and identity are stored in `%APPDATA%\LANTooth`.

### Features
- PC system audio (WASAPI loopback) to the phone, in stereo or mono.
- Phone mic to the PC with push-to-talk or locked always-on mode, e.g. into a
  virtual audio cable for Discord or Zoom.
- Connect by IP: the PC remembers the phones it has connected to, and the
  phone remembers PCs you accepted (Bluetooth-style, no PIN).
- Opus audio encrypted with ChaCha20-Poly1305. The session key is bound to the
  PC's identity key.
- Recovery of lost packets (each packet also carries the previous frame) and an
  adaptive 40–160 ms jitter buffer.
- Native libopus 1.5.2 on both sides.
- Phone media buttons and notification controls act as PC media keys.
