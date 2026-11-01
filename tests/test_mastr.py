import unittest

from de_energy_live.sources import mastr

# Minimal fixture in the Gesamtdatenexport flat-XML shape (UTF-16 on the wire,
# but the parser works on decoded str so we feed str chunks directly).
CATALOG_XML = """<?xml version='1.0' encoding='UTF-16'?><Katalogwerte>
<Katalogwert><Id>35</Id><Wert>In Betrieb</Wert><KatalogKategorieId>4</KatalogKategorieId></Katalogwert>
<Katalogwert><Id>38</Id><Wert>Endgültig stillgelegt</Wert><KatalogKategorieId>4</KatalogKategorieId></Katalogwert>
<Katalogwert><Id>1409</Id><Wert>Nordrhein-Westfalen</Wert><KatalogKategorieId>101</KatalogKategorieId></Katalogwert>
<Katalogwert><Id>2408</Id><Wert>Braunkohle</Wert><KatalogKategorieId>133</KatalogKategorieId></Katalogwert>
<Katalogwert><Id>888</Id><Wert>Windenergie an Land</Wert><KatalogKategorieId>87</KatalogKategorieId></Katalogwert>
</Katalogwerte>"""

UNIT_XML = """<EinheitenVerbrennung><EinheitVerbrennung>
<EinheitMastrNummer>SEE1</EinheitMastrNummer>
<AnlagenbetreiberMastrNummer>ABR1</AnlagenbetreiberMastrNummer>
<Bundesland>1409</Bundesland><Landkreis>Rhein-Erft-Kreis</Landkreis>
<Postleitzahl>50129</Postleitzahl><Ort>Bergheim</Ort>
<Inbetriebnahmedatum>2012-05-23</Inbetriebnahmedatum>
<EinheitBetriebsstatus>35</EinheitBetriebsstatus>
<NameStromerzeugungseinheit>Niederaußem K</NameStromerzeugungseinheit>
<Energietraeger>2408</Energietraeger>
<Bruttoleistung>1012000.000</Bruttoleistung>
<Nettonennleistung>1000000.000</Nettonennleistung>
<NameKraftwerk>RWE &amp; Co</NameKraftwerk>
<NameKraftwerksblock>BoA 3</NameKraftwerksblock>
<Hauptbrennstoff>2463</Hauptbrennstoff><Technologie>838</Technologie>
</EinheitVerbrennung><EinheitVerbrennung>
<EinheitMastrNummer>SEE2</EinheitMastrNummer>
<AnlagenbetreiberMastrNummer>ABR2</AnlagenbetreiberMastrNummer>
<Postleitzahl>50129</Postleitzahl>
<EinheitBetriebsstatus>38</EinheitBetriebsstatus>
<Nettonennleistung>300000</Nettonennleistung>
</EinheitVerbrennung></EinheitenVerbrennung>"""

OPERATOR_XML = """<Marktakteure><Marktakteur>
<MastrNummer>ABR1</MastrNummer><Firmenname>RWE Power AG</Firmenname>
</Marktakteur><Marktakteur>
<MastrNummer>ABR2</MastrNummer><Vorname>Max</Vorname><Nachname>Muster</Nachname>
</Marktakteur><Marktakteur>
<MastrNummer>ABR3</MastrNummer><Firmenname>Nicht gebraucht GmbH</Firmenname>
</Marktakteur></Marktakteure>"""


def chunks(text: str, n: int = 512):
    for i in range(0, len(text), n):
        yield text[i : i + n]


class TestMastr(unittest.TestCase):
    def test_build_catalog(self):
        # Katalogwert XML also parses when fed as str chunks
        cat = {}
        for blk in mastr.iter_blocks(chunks(CATALOG_XML, 40), "Katalogwert"):
            f = mastr.block_fields(blk)
            cat[f["Id"]] = f["Wert"]
        self.assertEqual(cat["2408"], "Braunkohle")
        self.assertEqual(cat["1409"], "Nordrhein-Westfalen")

    def test_iter_blocks_across_chunks(self):
        # split inside tags/units — nothing may be lost
        blocks = list(mastr.iter_blocks(chunks(UNIT_XML, 37), "EinheitVerbrennung"))
        self.assertEqual(len(blocks), 2)
        self.assertIn("SEE1", blocks[0])
        self.assertIn("SEE2", blocks[1])

    def test_parse_unit_block(self):
        cat = {"35": "In Betrieb", "1409": "Nordrhein-Westfalen",
               "2408": "Braunkohle", "2463": "Rohbraunkohlen",
               "838": "Kondensationsmaschine mit Entnahme"}
        blk = next(mastr.iter_blocks(chunks(UNIT_XML), "EinheitVerbrennung"))
        row = mastr.parse_unit_block(blk, "verbrennung", mastr.UNIT_SPECS["verbrennung"], cat)
        self.assertEqual(row["mastr_nr"], "SEE1")
        self.assertEqual(row["subtype"], "Braunkohle")
        self.assertEqual(row["name"], "Niederaußem K")
        self.assertEqual(row["plant"], "RWE & Co")
        self.assertEqual(row["plz"], "50129")
        self.assertEqual(row["bundesland"], "Nordrhein-Westfalen")
        self.assertAlmostEqual(row["netto_mw"], 1000.0)  # kW -> MW
        self.assertEqual(row["status"], "In Betrieb")
        self.assertEqual(row["inbetrieb"], "2012-05-23")
        self.assertIn("Rohbraunkohlen", row["extra"])

    def test_parse_operator_block(self):
        rows = [
            mastr.parse_operator_block(b)
            for b in mastr.iter_blocks(chunks(OPERATOR_XML), "Marktakteur")
        ]
        self.assertEqual(
            rows, [("ABR1", "RWE Power AG"), ("ABR2", "Max Muster"),
                   ("ABR3", "Nicht gebraucht GmbH")]
        )

    def test_member_text_chunks_stored(self):
        # method 0 (stored) decodes utf-16 directly
        blob = UNIT_XML.encode("utf-16")
        text = "".join(mastr.member_text_chunks(blob, method=0, chunk=64))
        self.assertIn("Niederau", text)
        blocks = list(mastr.iter_blocks(iter([text]), "EinheitVerbrennung"))
        self.assertEqual(len(blocks), 2)

    def test_member_text_chunks_deflate(self):
        import zlib
        blob = UNIT_XML.encode("utf-16")
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        data = comp.compress(blob) + comp.flush()
        text = "".join(mastr.member_text_chunks(data, method=8, chunk=64))
        self.assertIn("SEE1", text)


if __name__ == "__main__":
    unittest.main()
