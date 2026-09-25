"""Minimal Standard MIDI File model, reader and writer (stdlib only).

Messages mirror the attribute names used by ``mido`` (``type``, ``time``,
``note``, ``velocity``, ``tempo`` ...) so the PianoVision JSON builder can
consume either a parsed ``.mid`` file or a score rendered in memory.
``time`` is a delta in ticks, exactly like ``mido``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional

# key signature names as produced by mido for (sharps/flats, mode)
_MAJOR_KEYS = {-7: "Cb", -6: "Gb", -5: "Db", -4: "Ab", -3: "Eb", -2: "Bb", -1: "F",
               0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F#", 7: "C#"}
_MINOR_KEYS = {-7: "Abm", -6: "Ebm", -5: "Bbm", -4: "Fm", -3: "Cm", -2: "Gm", -1: "Dm",
               0: "Am", 1: "Em", 2: "Bm", 3: "F#m", 4: "C#m", 5: "G#m", 6: "D#m", 7: "A#m"}


@dataclass
class Msg:
    type: str
    time: int = 0
    channel: Optional[int] = None
    note: int = 0
    velocity: int = 0
    control: int = 0
    value: int = 0
    program: int = 0
    pitch: int = 0
    tempo: int = 500000
    numerator: int = 4
    denominator: int = 4
    key: str = "C"
    name: str = ""
    text: str = ""
    port: int = 0
    data: bytes = b""

    @property
    def is_meta(self) -> bool:
        return self.channel is None and self.type not in ("sysex",)


@dataclass
class MidiFile:
    ticks_per_beat: int = 480
    tracks: List[List[Msg]] = field(default_factory=list)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _read_varlen(data: bytes, pos: int):
    value = 0
    while True:
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, pos


def read_midi(path: str) -> MidiFile:
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"MThd":
        raise ValueError(f"{path}: not a MIDI file")
    hlen = struct.unpack(">I", data[4:8])[0]
    _fmt, ntracks, division = struct.unpack(">HHH", data[8:14])
    pos = 8 + hlen
    mf = MidiFile(ticks_per_beat=division)
    for _ in range(ntracks):
        while data[pos:pos + 4] != b"MTrk":
            clen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
            pos += 8 + clen
        tlen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        pos += 8
        mf.tracks.append(_read_track(data[pos:pos + tlen]))
        pos += tlen
    return mf


def _read_track(data: bytes) -> List[Msg]:
    msgs: List[Msg] = []
    pos = 0
    status = None
    while pos < len(data):
        delta, pos = _read_varlen(data, pos)
        b = data[pos]
        if b == 0xFF:
            mtype = data[pos + 1]
            length, pos = _read_varlen(data, pos + 2)
            payload = data[pos:pos + length]
            pos += length
            msgs.append(_meta(mtype, payload, delta))
            continue
        if b in (0xF0, 0xF7):
            length, pos = _read_varlen(data, pos + 1)
            msgs.append(Msg("sysex", delta, data=data[pos:pos + length]))
            pos += length
            continue
        if b & 0x80:
            status = b
            pos += 1
        kind, ch = status & 0xF0, status & 0x0F
        if kind in (0xC0, 0xD0):
            d1 = data[pos]
            pos += 1
            if kind == 0xC0:
                msgs.append(Msg("program_change", delta, channel=ch, program=d1))
            else:
                msgs.append(Msg("aftertouch", delta, channel=ch, value=d1))
            continue
        d1, d2 = data[pos], data[pos + 1]
        pos += 2
        if kind == 0x90:
            msgs.append(Msg("note_on", delta, channel=ch, note=d1, velocity=d2))
        elif kind == 0x80:
            msgs.append(Msg("note_off", delta, channel=ch, note=d1, velocity=d2))
        elif kind == 0xB0:
            msgs.append(Msg("control_change", delta, channel=ch, control=d1, value=d2))
        elif kind == 0xE0:
            msgs.append(Msg("pitchwheel", delta, channel=ch, pitch=(d1 | (d2 << 7)) - 8192))
        elif kind == 0xA0:
            msgs.append(Msg("polytouch", delta, channel=ch, note=d1, value=d2))
    return msgs


def _meta(mtype: int, payload: bytes, delta: int) -> Msg:
    if mtype == 0x51:
        return Msg("set_tempo", delta, tempo=(payload[0] << 16) | (payload[1] << 8) | payload[2])
    if mtype == 0x58:
        return Msg("time_signature", delta, numerator=payload[0], denominator=2 ** payload[1])
    if mtype == 0x59:
        sf = struct.unpack("b", payload[:1])[0]
        mode = payload[1]
        return Msg("key_signature", delta, key=(_MINOR_KEYS if mode else _MAJOR_KEYS)[sf])
    if mtype == 0x2F:
        return Msg("end_of_track", delta)
    if mtype == 0x03:
        return Msg("track_name", delta, name=payload.decode("latin1"))
    if mtype == 0x21:
        return Msg("midi_port", delta, port=payload[0])
    if mtype == 0x05:
        return Msg("lyrics", delta, text=payload.decode("latin1"))
    if mtype == 0x06:
        return Msg("marker", delta, text=payload.decode("latin1"))
    return Msg(f"meta_{mtype:02x}", delta, data=payload)


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def _varlen(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


_MAJOR_SF = {v: k for k, v in _MAJOR_KEYS.items()}
_MINOR_SF = {v: k for k, v in _MINOR_KEYS.items()}


def _encode(msg: Msg) -> bytes:
    t = msg.type
    if t == "note_on":
        return bytes((0x90 | msg.channel, msg.note, msg.velocity))
    if t == "note_off":
        return bytes((0x80 | msg.channel, msg.note, msg.velocity))
    if t == "control_change":
        return bytes((0xB0 | msg.channel, msg.control, msg.value))
    if t == "program_change":
        return bytes((0xC0 | msg.channel, msg.program))
    if t == "aftertouch":
        return bytes((0xD0 | msg.channel, msg.value))
    if t == "polytouch":
        return bytes((0xA0 | msg.channel, msg.note, msg.value))
    if t == "pitchwheel":
        v = msg.pitch + 8192
        return bytes((0xE0 | msg.channel, v & 0x7F, v >> 7))
    if t == "set_tempo":
        payload = bytes(((msg.tempo >> 16) & 0xFF, (msg.tempo >> 8) & 0xFF, msg.tempo & 0xFF))
        return b"\xff\x51" + _varlen(3) + payload
    if t == "time_signature":
        dd = msg.denominator.bit_length() - 1
        return b"\xff\x58\x04" + bytes((msg.numerator, dd, 24, 8))
    if t == "key_signature":
        if msg.key in _MAJOR_SF:
            sf, mode = _MAJOR_SF[msg.key], 0
        else:
            sf, mode = _MINOR_SF[msg.key], 1
        return b"\xff\x59\x02" + struct.pack("b", sf) + bytes((mode,))
    if t == "end_of_track":
        return b"\xff\x2f\x00"
    if t in ("track_name", "lyrics", "marker"):
        code = {"track_name": 0x03, "lyrics": 0x05, "marker": 0x06}[t]
        payload = (msg.name if t == "track_name" else msg.text).encode("latin1", "replace")
        return bytes((0xFF, code)) + _varlen(len(payload)) + payload
    if t == "midi_port":
        return b"\xff\x21\x01" + bytes((msg.port,))
    if t == "sysex":
        return b"\xf0" + _varlen(len(msg.data)) + msg.data
    if t.startswith("meta_"):
        return bytes((0xFF, int(t[5:], 16))) + _varlen(len(msg.data)) + msg.data
    raise ValueError(f"cannot encode {t}")


def write_midi(mf: MidiFile, path: str) -> None:
    out = bytearray(b"MThd" + struct.pack(">IHHH", 6, 1, len(mf.tracks), mf.ticks_per_beat))
    for track in mf.tracks:
        body = bytearray()
        for msg in track:
            body += _varlen(msg.time) + _encode(msg)
        out += b"MTrk" + struct.pack(">I", len(body)) + body
    with open(path, "wb") as f:
        f.write(out)


# --------------------------------------------------------------------------
# helpers shared by the JSON builder (reproducing mido semantics)
# --------------------------------------------------------------------------

def merged_messages_seconds(mf: MidiFile):
    """Yield (msg, delta_seconds) like iterating a ``mido.MidiFile``.

    Tracks are merged with a stable sort on absolute tick, end_of_track
    messages are folded into the following message, and deltas are converted
    to seconds with the tempo in effect (updated *after* a set_tempo message).
    """
    merged = []
    for track in mf.tracks:
        now = 0
        for msg in track:
            now += msg.time
            merged.append((now, msg))
    merged.sort(key=lambda item: item[0])

    rel = []
    now = 0
    for abs_tick, msg in merged:
        rel.append((abs_tick - now, msg))
        now = abs_tick
    fixed = []
    accum = 0
    for delta, msg in rel:
        if msg.type == "end_of_track":
            accum += delta
        elif accum:
            fixed.append((accum + delta, msg))
            accum = 0
        else:
            fixed.append((delta, msg))
    fixed.append((accum, Msg("end_of_track")))

    tempo = 500000
    tpb = mf.ticks_per_beat
    for delta, msg in fixed:
        if delta > 0:
            scale = tempo * 1e-6 / tpb
            secs = delta * scale
        else:
            secs = 0
        yield msg, secs
        if msg.type == "set_tempo":
            tempo = msg.tempo
