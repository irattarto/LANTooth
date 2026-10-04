# Changelog

## [0.2.1] — 2026-10-04 (test release)

Fixes for pairing in 0.2.0, where one device could show a code the other didn't.
- Phone: PCs trusted by older versions (granted without a code) are dropped, so
  they pair again with the code on both devices.
- PC: always compares the code when the phone shows one, even if it still has
  the phone pinned (e.g. after "Forget paired PCs" on the phone).
- PC that no longer knows a phone now tells it (authenticated) to forget the PC,
  so the code appears on both sides.

## [0.2.0] — 2026-10-04 (test release)

Security hardening — **protocol v4**. PC and phone must be updated together; an
older peer is refused with a version-mismatch message.

### Changed in v4
- The pairing code can no longer be forced by a man-in-the-middle: the PC commits
  to its ephemeral key before seeing the phone's and reveals it afterwards, so
  matching codes can't be ground out by trial.
- The phone now proves it derived the session keys (CONNECT_READY) before the PC
  starts a session, so a spoofed "accept" can't make the PC stream to a stranger.
- Phone: a pairing request can only be accepted inside the app, where the code is
  shown — the notification no longer has an Accept button. Only one pairing
  prompt at a time; forged connection requests can no longer evict a real
  handshake.
- Phone: the identity private key is encrypted with an Android Keystore key
  (an existing key is migrated automatically).
- PC: the trusted-phones list is DPAPI-protected (the old trusted_phones.json
  is migrated); the exe never loads libopus from VLC or PATH.

### Changed in v3
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
