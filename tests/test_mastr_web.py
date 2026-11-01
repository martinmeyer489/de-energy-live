import sqlite3
import unittest

from de_energy_live import web


def fixture_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE units("
        "mastr_nr TEXT, category TEXT, subtype TEXT, name TEXT, plant TEXT,"
        "operator_abr TEXT, plz TEXT, ort TEXT, landkreis TEXT, bundesland TEXT,"
        "netto_mw REAL, brutto_mw REAL, status TEXT, inbetrieb TEXT,"
        "lon REAL, lat REAL, extra TEXT)")
    conn.execute("CREATE TABLE operators(abr TEXT PRIMARY KEY, name TEXT)")
    conn.executemany(
        "INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("S1", "speicher", "Batterie", "Big BESS", "", "OP1",
             "59071", "Hamm", "", "Nordrhein-Westfalen",
             140.0, 174.0, "In Betrieb", "2024-12-28", 7.97, 51.68, ""),
            ("S2", "speicher", "Batterie", "Small BESS", "", "OP1",
             "10115", "Berlin", "", "Berlin",
             0.01, 0.01, "In Betrieb", "2020-01-01", 13.4, 52.5, ""),
            ("S3", "speicher", "Batterie", "Planned BESS", "", "OP2",
             "59071", "Hamm", "", "Nordrhein-Westfalen",
             50.0, 50.0, "In Planung", "", None, None, ""),
            ("S4", "speicher", "Pumpspeicher", "Pumpwerk", "", "OP2",
             "83471", "Berchtesgaden", "", "Bayern",
             300.0, 310.0, "In Betrieb", "1975-06-01", 13.0, 47.6, ""),
            ("W1", "wind", "Windenergie an Land", "WA1", "Windpark X", "OP2",
             "59071", "Hamm", "", "Nordrhein-Westfalen",
             4.5, 4.5, "In Betrieb", "2010-03-01", 8.0, 51.7, ""),
            ("W2", "wind", "Windenergie an Land", "WA2", "Windpark X", "OP2",
             "20354", "Hamburg", "", "Hamburg",
             3.0, 3.0, "In Betrieb", "2012-05-01", None, None, ""),
            ("W3", "wind", "Windenergie auf See", "WA3", "", "OP1",
             "", "", "", "Ausschließliche Wirtschaftszone",
             8.0, 8.0, "In Betrieb", "2018-01-01", 99.0, 20.0, ""),
            ("K1", "kernkraft", "", "KKW Alt", "", "OP1",
             "94339", "Leibstadt", "", "Bayern",
             1000.0, 1100.0, "Stillgelegt", "1980-01-01", 9.8, 49.9, ""),
        ])
    conn.executemany(
        "INSERT INTO operators VALUES (?,?)",
        [("OP1", "RWE Generation SE"), ("OP2", "Eon Kraftwerke")])
    conn.commit()
    return conn


class TestMastrWeb(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_db()
        web._mastr_qcache.clear()

    def test_batteries_defaults(self):
        d = web.mastr_batteries({"limit": ["10"]}, conn=self.conn)
        self.assertTrue(d["available"])
        # only subtype Batterie + status In Betrieb
        self.assertEqual(d["total_units"], 2)
        self.assertAlmostEqual(d["total_mw_netto"], 140.0)
        self.assertEqual(d["units"][0]["mastr_nr"], "S1")
        self.assertEqual(d["units"][0]["operator"], "RWE Generation SE")
        self.assertIn("Berlin", d["bundeslaender"])

    def test_batteries_filters(self):
        d = web.mastr_batteries(
            {"plz": ["10"], "bundesland": ["BE"]}, conn=self.conn)
        self.assertEqual(d["total_units"], 1)
        self.assertEqual(d["units"][0]["mastr_nr"], "S2")
        # all_types includes pumped storage
        d = web.mastr_batteries({"all_types": ["1"]}, conn=self.conn)
        self.assertEqual(d["total_units"], 3)
        # state filter accepts full names too
        d = web.mastr_batteries(
            {"bundesland": ["Bayern"], "all_types": ["1"]}, conn=self.conn)
        self.assertEqual(d["total_units"], 1)
        self.assertEqual(d["units"][0]["subtype"], "Pumpspeicher")

    def test_batteries_pagination(self):
        d = web.mastr_batteries(
            {"limit": ["1"], "offset": ["1"]}, conn=self.conn)
        self.assertEqual(d["units"][0]["mastr_nr"], "S2")

    def test_capacity(self):
        d = web.mastr_capacity({}, conn=self.conn)
        self.assertTrue(d["available"])
        by_slug = {g["slug"]: g for g in d["groups"]}
        # battery group = speicher minus Pumpspeicher
        self.assertEqual(by_slug["battery"]["units"], 2)
        self.assertAlmostEqual(by_slug["battery"]["mw"], 140.0)
        self.assertEqual(by_slug["pumped"]["units"], 1)
        self.assertAlmostEqual(by_slug["onshore"]["mw"], 7.5)
        self.assertEqual(by_slug["nuclear"]["units"], 0)

    def test_capacity_state_filter(self):
        d = web.mastr_capacity({"bundesland": ["Berlin"]}, conn=self.conn)
        self.assertEqual(d["bundesland"], "Berlin")
        self.assertEqual(d["total_units"], 1)
        self.assertAlmostEqual(d["total_mw"], 0.0)

    def test_map_battery(self):
        d = web.mastr_map({"type": ["battery"]}, conn=self.conn)
        self.assertTrue(d["available"])
        self.assertEqual(d["total_units"], 2)  # S3 is In Planung
        self.assertEqual(d["with_coords"], 2)
        self.assertEqual(len(d["markers"]), 2)
        self.assertEqual(d["markers"][0]["mastr_nr"], "S1")  # by MW
        by_name = {s["name"]: s for s in d["states"]}
        self.assertEqual(by_name["Nordrhein-Westfalen"]["units"], 1)
        self.assertAlmostEqual(by_name["Nordrhein-Westfalen"]["mw"], 140.0)
        self.assertIn({"slug": "nuclear", "label": "Nuclear"},
                      d["layers"])

    def test_map_bad_coords_fall_back_to_states(self):
        # W2 has no coordinates, W3's are outside Germany: both must
        # still appear in the per-state aggregation, not as markers.
        d = web.mastr_map({"type": ["onshore"]}, conn=self.conn)
        self.assertEqual(d["total_units"], 2)
        self.assertEqual(d["with_coords"], 1)
        self.assertEqual([m["mastr_nr"] for m in d["markers"]], ["W1"])
        by_name = {s["name"]: s for s in d["states"]}
        self.assertEqual(by_name["Hamburg"]["units"], 1)
        self.assertEqual(by_name["Hamburg"]["with_coords"], 0)

    def test_map_offshore_eez_is_other(self):
        d = web.mastr_map({"type": ["offshore"]}, conn=self.conn)
        self.assertEqual(d["total_units"], 1)
        self.assertEqual(d["states"], [])  # EEZ is not a Bundesland
        self.assertEqual(d["other"][0]["name"],
                         "Ausschließliche Wirtschaftszone")
        self.assertEqual(d["markers"], [])  # coords outside DE bbox

    def test_map_nuclear_any_status(self):
        d = web.mastr_map({"type": ["nuclear"]}, conn=self.conn)
        self.assertTrue(d["any_status"])
        self.assertEqual(d["total_units"], 1)  # Stillgelegt included
        self.assertEqual(d["markers"][0]["mastr_nr"], "K1")

    def test_map_unknown_type(self):
        d = web.mastr_map({"type": ["bogus"]}, conn=self.conn)
        self.assertIn("error", d)
        self.assertTrue(d["layers"])


if __name__ == "__main__":
    unittest.main()
