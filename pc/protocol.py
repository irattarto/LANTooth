import struct
from dataclasses import dataclass

# Packet wire format: [ 1B type | 8B counter | 4B stream_id | 2B payload_len | N bytes ciphertext ]
HEADER_FMT = "!BQIH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 15 bytes

TYPE_AUDIO_PC_TO_ANDROID = 0x01
TYPE_AUDIO_ANDROID_TO_PC = 0x02
TYPE_CONTROL             = 0x08

CMD_PLAY_PAUSE    = 0x01
CMD_NEXT_TRACK    = 0x02
CMD_PREV_TRACK    = 0x03
CMD_STOP          = 0x04
# 0x05-0x09 retired (seek / volume / mic-toggle were never sent by either side)
CMD_KEEPALIVE     = 0x0A
# Deliberate end of session (user pressed Disconnect on either side). Lets the
# peer drop the session at once instead of waiting out LIVENESS_TIMEOUT, and tells
# the PC not to auto-reconnect straight back into a phone that just hung up.
CMD_BYE           = 0x0B
# Link-quality report for the stream the sender RECEIVES (see pack_report), sent
# every REPORT_INTERVAL_S so the other side can adapt its redundancy/bitrate.
CMD_REPORT        = 0x0C
REPORT_INTERVAL_S = 0.5

PAIRING_PORT = 7890  # fixed UDP port the PC connects to (phone IP + this port)
MAX_UDP_PAYLOAD = 1400

# Bump whenever a wire-incompatible change is made (audio framing, packet layout,
# crypto scheme, ...) — the connect handshake rejects any peer whose declared
# PROTOCOL_VERSION doesn't match ours instead of silently misbehaving: PC and
# phone must always be rebuilt/reinstalled together.
#   v1: 20ms Opus frames, payload = seq + opus
#   v2: identity-authenticated session key, payload carries a redundant copy of
#       the previous frame, stereo-capable PC -> phone stream
#   v3: mutual identity authentication (the phone has an identity key too), a
#       first-pairing numeric-comparison code, separate PC->phone / phone->PC
#       keys, and a CONNECT_CONFIRM proof so a session only starts for a peer
#       that really holds the trusted identity's private key
#   v4: commit-then-reveal pairing code (the PC commits to its ephemeral key in
#       CONNECT_REQ and reveals it only after the phone's keys are known, so a
#       man-in-the-middle cannot grind keys to force matching codes), and a
#       CONNECT_READY key-confirmation proof from the phone, so the PC only
#       starts a session with a peer that really derived the session keys
#   v5: 10ms Opus frames (was 20ms) in both directions
#   v6: audio seq advances with real time (also while muted/silent); payload
#       carries up to MAX_REDUNDANCY earlier frames; CMD_REPORT link reports;
#       authenticated CONNECT_CANCEL and user CONNECT_REJECT; DSCP value in
#       CONNECT_REQ
PROTOCOL_VERSION = 6

# Connect handshake (Bluetooth-style numeric comparison on first pairing, then automatic)
CONNECT_REQ     = b"CONNECT_REQ"      # PC -> phone: identity key + COMMITMENT to the ephemeral key
CONNECT_PENDING = b"CONNECT_PENDING"   # phone -> PC: waiting for the user to accept; carries keys so both can show the code
CONNECT_REVEAL  = b"CONNECT_REVEAL"    # PC -> phone: the committed ephemeral key (after PENDING)
CONNECT_ACCEPT  = b"CONNECT_ACCEPT"
CONNECT_REJECT  = b"CONNECT_REJECT"
CONNECT_CONFIRM = b"CONNECT_CONFIRM"   # PC -> phone: ephemeral key + proof of the session key
CONNECT_READY   = b"CONNECT_READY"     # phone -> PC: proof the phone derived the same keys, starts the session
CONNECT_UNKNOWN = b"CONNECT_UNKNOWN"   # PC -> phone: "I no longer know you" (authenticated); phone must re-pair with a code
CONNECT_CANCEL  = b"CONNECT_CANCEL"    # PC -> phone: the user declined the pairing code (authenticated)

# CONNECT_REJECT body: 1B reason + 1B responder's PROTOCOL_VERSION, and for
# REJECT_REASON_USER also phone_ephemeral_pub(32) + HMAC(confirm_key, transcript),
# so nobody else on the LAN can abort a connect by faking one. A version-mismatch
# reject can't be authenticated (the two versions don't share a key schedule), so
# the PC only believes it if the phone gives no other answer for a few seconds.
REJECT_REASON_USER             = 0  # user tapped Reject on Android
REJECT_REASON_VERSION_MISMATCH = 1  # auto-rejected before any user prompt

# HKDF parameters. v3 input keying material is four DH results,
#   X25519(pc_eph, ph_eph) || X25519(pc_id, ph_eph) || X25519(pc_eph, ph_id) || X25519(pc_id, ph_id)
# so only a peer holding BOTH private identity keys can derive the session keys.
# The transcript (all four public keys) is bound into the HKDF info, and 96 bytes
# are produced: PC->phone key, phone->PC key, confirmation key.
SESSION_KDF_SALT = b"lantooth-connect-v4"
SESSION_KDF_INFO = b"lantooth-session-v4"
PAIRING_CODE_TAG = b"lantooth-pair-v4"
CONFIRM_TAG      = b"lantooth-confirm-v4"
READY_TAG        = b"lantooth-ready-v4"
COMMIT_TAG       = b"lantooth-commit-v4"
UNKNOWN_TAG      = b"lantooth-unknown-v4"
CANCEL_TAG       = b"lantooth-cancel-v6"
REJECT_TAG       = b"lantooth-reject-v6"

# Wi-Fi priority (DSCP) of the audio packets; the PC tells the phone which one to
# use in CONNECT_REQ. 48 (CS6) lands in the WMM voice queue under both the common
# "DSCP >> 3" mapping and RFC 8325; 46 (EF) is voice only under RFC 8325 and
# video under the simple mapping many home access points use.
DSCP_VALUES = (48, 46)
DEFAULT_DSCP = 48

# Audio payload (inside the encrypted packet):
#   [ 4B seq | 1B count | count x 2B frame length | frames ]
# frames[0] is frame `seq`, frames[i] is frame seq - i: redundant copies of the
# frames before it, so a burst of up to count - 1 lost packets is recovered
# exactly by the next packet that arrives. Copies that wouldn't fit are left out.
# seq counts 10ms frame slots of real time, so it also advances while the sender
# is muted or silent (no packets sent) — the receiver tells a pause from network
# delay by it.
AUDIO_HDR_FMT = "!IB"
AUDIO_HDR_SIZE = struct.calcsize(AUDIO_HDR_FMT)  # 5 bytes
MAX_REDUNDANCY = 3                                # earlier frames carried at most
MAX_AUDIO_PAYLOAD = MAX_UDP_PAYLOAD - 16 - 50     # AEAD tag + headroom


@dataclass
class Packet:
    type: int
    counter: int
    stream_id: int
    payload: bytes


def pack_packet(pkt: Packet) -> bytes:
    hdr = struct.pack(HEADER_FMT, pkt.type, pkt.counter, pkt.stream_id, len(pkt.payload))
    return hdr + pkt.payload


def unpack_packet(data: bytes) -> Packet | None:
    if len(data) < HEADER_SIZE:
        return None
    ptype, counter, stream_id, plen = struct.unpack_from(HEADER_FMT, data)
    if plen > MAX_UDP_PAYLOAD:
        return None
    payload = data[HEADER_SIZE: HEADER_SIZE + plen]
    if len(payload) != plen:
        return None
    return Packet(type=ptype, counter=counter, stream_id=stream_id, payload=payload)


def pack_audio(seq: int, frames: list[bytes]) -> bytes:
    """frames[0] = frame `seq`, frames[i] = frame seq - i (newest first)."""
    size = AUDIO_HDR_SIZE
    used: list[bytes] = []
    for f in frames[:1 + MAX_REDUNDANCY]:
        if used and size + 2 + len(f) > MAX_AUDIO_PAYLOAD:
            break
        used.append(f)
        size += 2 + len(f)
    return (struct.pack(AUDIO_HDR_FMT, seq & 0xFFFFFFFF, len(used))
            + b"".join(struct.pack("!H", len(f)) for f in used) + b"".join(used))


def unpack_audio(data: bytes) -> tuple[int, list[bytes]] | None:
    """(seq, frames newest first) or None if malformed."""
    if len(data) < AUDIO_HDR_SIZE:
        return None
    seq, count = struct.unpack_from(AUDIO_HDR_FMT, data)
    if count == 0 or count > 1 + MAX_REDUNDANCY:
        return None
    pos = AUDIO_HDR_SIZE + 2 * count
    if pos > len(data):
        return None
    frames = []
    for (n,) in struct.iter_unpack("!H", data[AUDIO_HDR_SIZE:pos]):
        if n == 0 or pos + n > len(data):
            return None
        frames.append(data[pos:pos + n])
        pos += n
    return seq, frames


def pack_control(command: int, value: int = 0) -> bytes:
    return struct.pack("!BH", command, value)


def unpack_control(data: bytes) -> tuple[int, int]:
    if len(data) < 3:
        return 0, 0
    cmd, val = struct.unpack_from("!BH", data)
    return cmd, val


@dataclass
class LinkReport:
    """Receiver's view of one stream over the last REPORT_INTERVAL_S."""
    loss_permille: int   # packets lost (transport-counter gaps)
    max_burst: int       # longest run of consecutive lost packets
    stalls: int          # buffer ran dry because packets were late (not a sender pause)
    late: int            # frames that arrived after their play slot
    concealed: int       # frames lost for good (not recovered from redundancy)
    target_ms: int       # receiver's jitter buffer target


_REPORT_FMT = "!HBBHHH"


def pack_report(r: LinkReport) -> bytes:
    def u8(v): return max(0, min(255, v))
    def u16(v): return max(0, min(0xFFFF, v))
    return pack_control(CMD_REPORT) + struct.pack(
        _REPORT_FMT, u16(r.loss_permille), u8(r.max_burst), u8(r.stalls),
        u16(r.late), u16(r.concealed), u16(r.target_ms))


def unpack_report(data: bytes) -> LinkReport | None:
    """Parses a CMD_REPORT control payload (including its 3-byte command header)."""
    if len(data) < 3 + struct.calcsize(_REPORT_FMT):
        return None
    return LinkReport(*struct.unpack_from(_REPORT_FMT, data, 3))
