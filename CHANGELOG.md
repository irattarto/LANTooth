# Changelog

## [0.2.0] — 2026-10-04

Security hardening — **protocol v3**. PC and phone must be updated together; a
v2 peer is refused with a version-mismatch message.

### Changed
- Both devices now have an identity key. The first time a PC and phone connect,
  each shows the same 8-digit code; confirm they match on both (like Bluetooth
  numeric comparison). Every later connection is automatic.
- The phone is now authenticated to the PC (previously only the PC was), so a
  device on the LAN can no longer pose as your phone.
- A session only starts after the PC proves it holds its private key, so a
  replayed connection request can no longer occupy or block the phone.
- Separate encryption keys for each direction.
- Phone: at most 3 pending pairing prompts, sanitized PC names, throttled
  version-mismatch replies; new "Forget paired PCs" button.
- Release APK no longer logs connection details; release workflow build jobs
  are read-only.

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
