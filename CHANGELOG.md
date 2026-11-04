# Changelog

## [0.4.1] — 2026-11-04 (test release)

Fixes the phone mic not reaching the PC. Only the phone app changed; the protocol
is still v6, so a 0.4.0 PC app works with the 0.4.1 phone app.
- Phone: the mic picker listed every input the phone reports, including Telephony
  Rx, FM Tuner and Remote Submix, which normal apps aren't allowed to record.
  Selecting one left the session connected but with no mic audio and no error. The
  picker now lists only real microphones, and a saved selection that isn't one
  resets to System default.
- Phone: the speaker picker no longer lists Telephony Tx, FM or Remote Submix.
- Phone: capture no longer restarts when the mic picker re-reports the current
  selection, which opened the mic twice when connecting.

## [0.4.0] — 2026-11-04 (test release)

Steadier sound on Wi-Fi without extra latency. **Protocol v6: update the PC app
and the phone app together.**
- Both: each side reports packet loss, loss bursts and stalls to the other twice
  a second. The sender then repeats up to three earlier frames in each packet
  when losses come in bursts (previously one), sets the Opus loss hint from the
  measured loss, and lowers the bitrate only while the link is bad. Full
  quality returns after 15 s without loss.
- Both: the jitter buffer reacts to short Wi-Fi stalls straight away and keeps
  enough buffer for them for 20 s, instead of dropping out at every stall. On a
  clean link its minimum is now 20 ms (was 30 ms).
- Fixed: a short pause in the audio (push-to-talk released for under a second,
  PC audio going briefly silent) looked like network delay and pushed latency
  up to 160 ms for several seconds.
- Fixed (PC): a Windows network error could stop the PC receiving audio and
  end the session after 6 s instead of continuing.
- Security: a bystander on the network can no longer fake a "declined" or
  "rejected" pairing message to abort a connection.
- PC: new **Wi-Fi priority mark** setting with a **Set up…** button. It creates
  the Windows QoS policy (one administrator prompt) so the access point
  prioritizes LANTooth's audio. The phone uses the same mark.
- PC: the stats line shows loss, bitrate and redundancy for each direction.
- Phone: fewer memory allocations per audio packet.

## [0.3.0] — 2026-10-23 (test release)

Lower latency and better sound. **Protocol v5: update the PC app and the phone
app together.**
- Audio is sent in 10 ms frames instead of 20 ms, in both directions.
- PC: plays the phone mic through WASAPI instead of the old default (MME). On
  the test machine output buffering dropped from 100 ms to 23 ms. A new
  "Exclusive mode" option lowers it to about 13 ms but locks the device for
  other apps. The device lists put WASAPI entries first, and the default PC
  audio source is now the WASAPI loopback of the default output.
- Phone: plays and records through AAudio, Android's low-latency audio path.
  The playback buffer starts as small as possible and grows only when the phone
  underruns. Falls back to AudioTrack/AudioRecord when AAudio isn't available.
- PC: new "Auto-level the phone mic" option (AGC + limiter), on by default.
- PC: new "Latency test" button. Sends five clicks to the phone and measures
  the round trip through the phone's speaker and mic.
- Both: the jitter buffer cuts latency on quiet frames, crossfading each cut.
  Fades out instead of cutting to silence after a dropout, and fades back in.
- Opus complexity raised to 10; PC → phone stereo bitrate raised from 192 to
  256 kbit/s.

## [0.2.4] — 2026-10-09

- PC: when the connection to the phone is lost while PC audio is playing, sends
  the system Play/Pause media key (same as the phone's button). If the
  connection is restored within 30 s, sends it again to resume.

## [0.2.3] — 2026-10-04 (test release)

- Phone: Play/Pause, Hold to Talk and Lock are now three round buttons of the
  same size.

## [0.2.2] — 2026-10-04 (test release)

- Phone: new Play/Pause button on the control screen; it presses the PC's media
  play/pause key (previously only the notification buttons could do this).

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
