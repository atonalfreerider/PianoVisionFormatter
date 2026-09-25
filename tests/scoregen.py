"""Tiny MuseScore 4.6 scores for tests."""

import zipfile

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<museScore version="4.60"><programVersion>4.6.0</programVersion><Score><Division>480</Division>
<Part id="1"><Staff id="1"><StaffType group="pitched"><name>stdNormal</name></StaffType></Staff>
<trackName>Piano</trackName><Instrument id="piano"><longName>Piano</longName><trackName>Piano</trackName>
<instrumentId>keyboard.piano</instrumentId><Channel><program value="0"/></Channel></Instrument></Part>
<Staff id="1"><VBox><height>10</height><Text><style>title</style><text>{title}</text></Text>
<Text><style>composer</style><text>{composer}</text></Text></VBox>
{measures}</Staff></Score></museScore>
"""


def make_score(path: str, title: str = "Test Song", composer: str = "Jane Tester",
               pitches=(60, 62, 64, 65, 67, 69, 71, 72), tempo_bpm: int = 0) -> str:
    measures = []
    for i in range(0, len(pitches), 4):
        head = "<KeySig><concertKey>0</concertKey></KeySig><TimeSig><sigN>4</sigN><sigD>4</sigD></TimeSig>" if i == 0 else ""
        if i == 0 and tempo_bpm:
            head += (f"<Tempo><tempo>{tempo_bpm / 60:.6f}</tempo><followText>1</followText>"
                     f"<text>= {tempo_bpm}</text></Tempo>")
        chords = "".join(f"<Chord><durationType>quarter</durationType><Note><pitch>{p}</pitch></Note></Chord>"
                         for p in pitches[i:i + 4])
        measures.append(f"<Measure><voice>{head}{chords}</voice></Measure>")
    xml = TEMPLATE.format(title=title, composer=composer, measures="".join(measures))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><container><rootfiles>'
                   '<rootfile full-path="score.mscx"/></rootfiles></container>')
        z.writestr("score.mscx", xml)
        z.writestr("Thumbnails/thumbnail.png", b"not really a png")
    return path
