"""Tiny MuseScore 4.6 scores for tests."""

import zipfile

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<museScore version="4.60"><programVersion>4.6.0</programVersion><Score><Division>480</Division>
<Part id="1"><Staff id="1"><StaffType group="pitched"><name>stdNormal</name></StaffType></Staff>
<trackName>Piano</trackName><Instrument id="piano"><longName>Piano</longName><trackName>Piano</trackName>
<instrumentId>keyboard.piano</instrumentId><Channel><program value="0"/></Channel></Instrument></Part>
<Staff id="1"><VBox><height>10</height><Text><style>{t}itle</style><text>{title}</text></Text>
<Text><style>{c}omposer</style><text>{composer}</text></Text></VBox>
{measures}</Staff></Score></museScore>
"""


def make_score(path: str, title: str = "Test Song", composer: str = "Jane Tester",
               pitches=(60, 62, 64, 65, 67, 69, 71, 72), tempo_bpm: int = 0, ms3_styles: bool = False) -> str:
    """``ms3_styles`` writes MuseScore 3's capitalised text style names ("Title")."""
    measures = []
    for i in range(0, len(pitches), 4):
        head = "<KeySig><concertKey>0</concertKey></KeySig><TimeSig><sigN>4</sigN><sigD>4</sigD></TimeSig>" if i == 0 else ""
        if i == 0 and tempo_bpm:
            head += (f"<Tempo><tempo>{tempo_bpm / 60:.6f}</tempo><followText>1</followText>"
                     f"<text>= {tempo_bpm}</text></Tempo>")
        chords = "".join(f"<Chord><durationType>quarter</durationType><Note><pitch>{p}</pitch></Note></Chord>"
                         for p in pitches[i:i + 4])
        measures.append(f"<Measure><voice>{head}{chords}</voice></Measure>")
    t, c = ("T", "C") if ms3_styles else ("t", "c")
    xml = TEMPLATE.format(title=title, composer=composer, measures="".join(measures), t=t, c=c)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><container><rootfiles>'
                   '<rootfile full-path="score.mscx"/></rootfiles></container>')
        z.writestr("score.mscx", xml)
        z.writestr("Thumbnails/thumbnail.png", b"not really a png")
    return path


# ------------------------------------------------------------------------------
# two-staff piano scores laid out like MuseScore 4 writes them (for hand edits)
# ------------------------------------------------------------------------------

class E:
    """An element: ``E("Chord", E("durationType", text="quarter"), ...)``."""

    def __init__(self, tag, *children, text=None, **attrs):
        self.tag, self.children, self.text, self.attrs = tag, [c for c in children if c is not None], text, attrs

    def lines(self, indent=""):
        attrs = "".join(f' {k}="{v}"' for k, v in self.attrs.items())
        if self.text is not None:
            return [f"{indent}<{self.tag}{attrs}>{self.text}</{self.tag}>"]
        if not self.children:
            return [f"{indent}<{self.tag}{attrs}/>"]
        out = [f"{indent}<{self.tag}{attrs}>"]
        for c in self.children:
            out += c.lines(indent + "  ") if isinstance(c, E) else [indent + "  " + ln for ln in c]
        out.append(f"{indent}  </{self.tag}>")
        return out


def note(pitch, tie_next=None, tie_prev=None, velocity=None):
    """A note; ``tie_next``/``tie_prev`` are (measures, "fraction") relative locations of the other end."""
    kids = []
    for tag, rel in (("next", tie_next), ("prev", tie_prev)):
        if rel is None:
            continue
        loc = []
        if rel[0]:
            loc.append(E("measures", text=str(rel[0])))
        if rel[1] not in ("0", "0/1"):
            loc.append(E("fractions", text=rel[1]))
        sp = [E("Tie", E("eid", text="T")) if tag == "next" else None, E(tag, E("location", *loc) if loc else E("location"))]
        kids.append(E("Spanner", *sp, type="Tie"))
    kids += [E("pitch", text=str(pitch)), E("tpc", text=str(_TPC[pitch % 12]))]
    if velocity is not None:
        kids.append(E("velocity", text=str(velocity)))
    return E("Note", *kids)


_TPC = [14, 21, 16, 11, 18, 13, 20, 15, 10, 17, 12, 19]


def chord(dur, *notes_, dots=0, staff_move=0, extra=(), grace=None):
    kids = []
    if dots:
        kids.append(E("dots", text=str(dots)))
    if staff_move:
        kids.append(E("staffMove", text=str(staff_move)))
    kids.append(E("durationType", text=dur))
    if grace:
        kids.append(E(grace))
    kids += list(extra)
    kids += [n if isinstance(n, E) else note(n) for n in notes_]
    return E("Chord", *kids)


def rest(dur, dots=0, visible=True):
    kids = [] if visible else [E("visible", text="0")]
    if dots:
        kids.append(E("dots", text=str(dots)))
    kids.append(E("durationType", text=dur))
    return E("Rest", *kids)


def loc(frac):
    return E("location", E("fractions", text=frac))


def tuplet(actual=3, normal=2, base="eighth"):
    return E("Tuplet", E("normalNotes", text=str(normal)), E("actualNotes", text=str(actual)),
             E("baseNote", text=base), E("Number", E("text", text=str(actual))))


def end_tuplet():
    return E("endTuplet")


def accent():
    return E("Articulation", E("subtype", text="articAccentAbove"))


def dynamic(subtype, velocity, staff_only=True):
    kids = [E("subtype", text=subtype), E("velocity", text=str(velocity))]
    if staff_only:
        kids.append(E("voiceAssignment", text="allInStaff"))
    return E("Dynamic", *kids)


def make_piano_score(path, rh, lh, title="Hands", composer="Jane Tester", repeat=None, parts=()):
    """``rh``/``lh``: one entry per measure, each a list of voices, each a list of elements (4/4, C major).
    ``repeat``: (first measure, last measure) of a repeated section, 0-based.  ``parts``: more parts after
    the piano, each ``(track name, program, [staff, ...])`` with a staff as ``rh``/``lh`` (for example
    ``("Piano-simplified", 0, [rh2, lh2])`` or ``("Violin", 40, [vln])``)."""
    def staff(sid, measures, top):
        kids = []
        if top:
            kids.append(E("VBox", E("height", text="10"),
                          E("Text", E("style", text="title"), E("text", text=title)),
                          E("Text", E("style", text="composer"), E("text", text=composer))))
        for mi, voices in enumerate(measures):
            head = []
            if mi == 0:
                head = [E("KeySig", E("concertKey", text="0")), E("TimeSig", E("sigN", text="4"), E("sigD", text="4"))]
            mkids = []
            if repeat and mi == repeat[0]:
                mkids.append(E("startRepeat"))
            if repeat and mi == repeat[1]:
                mkids.append(E("endRepeat", text="2"))
            for vi, v in enumerate(voices):
                mkids.append(E("voice", *((head if vi == 0 else []) + list(v))))
            kids.append(E("Measure", *mkids))
        return E("Staff", *kids, id=str(sid))

    part = E("Part",
             E("Staff", E("StaffType", E("name", text="stdNormal"), group="pitched"), id="1"),
             E("Staff", E("StaffType", E("name", text="stdNormal"), group="pitched"),
               E("defaultClef", text="F"), id="2"),
             E("trackName", text="Piano"),
             E("Instrument", E("longName", text="Piano"), E("trackName", text="Piano"),
               E("instrumentId", text="keyboard.piano"), E("Channel", E("program", value="0"))),
             id="1")
    extra_parts, extra_staves = [], []
    sid = 3
    for pi, (name, program, staves) in enumerate(parts):
        defs = [E("Staff", E("StaffType", E("name", text="stdNormal"), group="pitched"), id=str(sid + k))
                for k in range(len(staves))]
        extra_parts.append(E("Part", *defs, E("trackName", text=name),
                             E("Instrument", E("longName", text=name), E("trackName", text=name),
                               E("instrumentId", text="keyboard.piano" if program < 8 else "strings.violin"),
                               E("Channel", E("program", value=str(program)))), id=str(pi + 2)))
        for k, measures in enumerate(staves):
            extra_staves.append(staff(sid + k, measures, False))
        sid += len(staves)
    score = E("Score", E("Division", text="480"), part, *extra_parts, staff(1, rh, True), staff(2, lh, False),
              *extra_staves)
    root = E("museScore", E("programVersion", text="4.6.0"), score, version="4.60")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(root.lines()) + "\n"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><container><rootfiles>'
                   '<rootfile full-path="score.mscx"/></rootfiles></container>')
        z.writestr("score.mscx", xml)
        z.writestr("Thumbnails/thumbnail.png", b"not really a png")
    return path
