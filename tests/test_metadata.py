"""Title/composer extraction and output naming."""

import unittest
import xml.etree.ElementTree as ET

from pianovision.metadata import extract_title_artist, format_output_filename

MS3 = """<museScore version="3.02"><Score>
<metaTag name="composer">Alicia Keys</metaTag><metaTag name="workTitle">Alicia Keys - Song</metaTag>
<Staff id="1"><VBox>
<Text><style>Title</style><text>Empire State of Mind (Part II) Broken Down</text></Text>
<Text><style>Subtitle</style><text>from The Element of Freedom (2010)</text></Text>
<Text><style>Composer</style><text>Alicia Keys</text></Text>
<Text><style>Subtitle</style><text>Transcription &amp; arrangement by Someone</text></Text>
</VBox></Staff></Score></museScore>"""

PATH = "/scores/Pop/empire-state-of-mind.mscz"


class Metadata(unittest.TestCase):
    def test_musescore3_capitalised_styles(self):
        title, artist = extract_title_artist(ET.fromstring(MS3), PATH)
        self.assertEqual(title, "Empire State of Mind (Part II) Broken Down - from The Element of Freedom (2010)")
        self.assertEqual(format_output_filename(title, artist, PATH),
                         "keys_Empire_State_of_Mind_Part_II_Broken_Down__from_The_Element_of_Freedom_2010.json")

    def test_legacy_rules_unchanged(self):
        # the old scripts only knew MuseScore 4's lower-case styles: file name + folder
        title, artist = extract_title_artist(ET.fromstring(MS3), PATH, legacy=True)
        self.assertEqual(format_output_filename(title, artist, PATH), "pop_empirestateofmind.json")

    def test_score_properties_before_file_name(self):
        root = ET.fromstring('<museScore><Score><metaTag name="workTitle">Real Title</metaTag>'
                             '<metaTag name="composer">Ann Writer</metaTag></Score></museScore>')
        title, artist = extract_title_artist(root, PATH)
        self.assertEqual(format_output_filename(title, artist, PATH), "writ_Real_Title.json")


if __name__ == "__main__":
    unittest.main()
