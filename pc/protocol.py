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
PROTOCOL_VERSION = 4

# Connect handshake (Bluetooth-style numeric comparison on first pairing, then automatic)
CONNECT_REQ     = b"CONNECT_REQ"      # PC -> phone: identity key + COMMITMENT to the ephemeral key
CONNECT_PENDING = b"CONNECT_PENDING"   # phone -> PC: waiting for the user to accept; carries keys so both can show the code
CONNECT_REVEAL  = b"CONNECT_REVEAL"    # PC -> phone: the committed ephemeral key (after PENDING)
CONNECT_ACCEPT  = b"CONNECT_ACCEPT"
CONNECT_REJECT  = b"CONNECT_REJECT"
CONNECT_CONFIRM = b"CONNECT_CONFIRM"   # PC -> phone: ephemeral key + proof of the session key
CONNECT_READY   = b"CONNECT_READY"     # phone -> PC: proof the phone derived the same keys, starts the session
CONNECT_CANCEL  = b"CONNECT_CANCEL"    # PC -> phone: the user declined the pairing code

# CONNECT_REJECT body reason codes: 1B reason + 1B responder's PROTOCOL_VERSION
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

# Audio payload (inside the encrypted packet):
#   [ 4B seq | 2B cur_len | cur_len bytes current Opus frame | previous Opus frame (optional) ]
# The previous frame is a redundant copy so any single lost packet is recovered
# exactly by the next one. It is left out when both wouldn't fit.
AUDIO_HDR_FMT = "!IH"
AUDIO_HDR_SIZE = struct.calcsize(AUDIO_HDR_FMT)  # 6 bytes
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


def pack_audio(seq: int, cur: bytes, prev: bytes | None) -> bytes:
    out = struct.pack(AUDIO_HDR_FMT, seq & 0xFFFFFFFF, len(cur)) + cur
    if prev and len(out) + len(prev) <= MAX_AUDIO_PAYLOAD:
        out += prev
    return out


def unpack_audio(data: bytes) -> tuple[int, bytes, bytes | None] | None:
    if len(data) < AUDIO_HDR_SIZE:
        return None
    seq, cur_len = struct.unpack_from(AUDIO_HDR_FMT, data)
    end = AUDIO_HDR_SIZE + cur_len
    if cur_len == 0 or end > len(data):
        return None
    prev = data[end:] or None
    return seq, data[AUDIO_HDR_SIZE:end], prev


def pack_control(command: int, value: int = 0) -> bytes:
    return struct.pack("!BH", command, value)


def unpack_control(data: bytes) -> tuple[int, int]:
    if len(data) < 3:
        return 0, 0
    cmd, val = struct.unpack_from("!BH", data)
    return cmd, val
