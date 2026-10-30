import unittest
from datetime import datetime, timezone

from de_energy_live.model import TimeSeries
from de_energy_live.sources.energy_charts import parse_power_mix, total_generation
from de_energy_live.sources.smard import parse_series


class TestParsing(unittest.TestCase):
    def test_parse_power_mix(self):
        payload = {
            "unix_seconds": [1700000000, 1700000900],
            "production_types": [
                {"name": "Solar", "data": [12000.0, 13000.0]},
                {"name": "Wind onshore", "data": [8000.0, None]},
                {"name": "Load (incl. self-consumption)", "data": [55000.0, 54000.0]},
                {"name": "Renewable share of load", "data": [40.1, 41.0]},
            ],
        }
        mix = parse_power_mix(payload)
        self.assertEqual(
            set(mix),
            {"Solar", "Wind onshore", "Load (incl. self-consumption)", "Renewable share of load"},
        )
        self.assertEqual(
            mix["Solar"].timestamps[0],
            datetime.fromtimestamp(1700000000, tz=timezone.utc),
        )
        self.assertEqual(mix["Wind onshore"].values, [8000.0, None])
        self.assertEqual(mix["Renewable share of load"].unit, "%")

    def test_total_generation_skips_load(self):
        payload = {
            "unix_seconds": [1700000000],
            "production_types": [
                {"name": "Solar", "data": [12000.0]},
                {"name": "Wind onshore", "data": [8000.0]},
                {"name": "Load (incl. self-consumption)", "data": [55000.0]},
            ],
        }
        self.assertEqual(total_generation(parse_power_mix(payload)), 20000.0)

    def test_smard_parse_series(self):
        series = [[1700000000000, 42100.5], [1700000900000, None]]
        ts = parse_series(series, "Netzlast")
        self.assertEqual(ts.values, [42100.5, None])

    def test_smard_quarterhour_mwh_to_mw(self):
        # 16 000 MWh in a quarter-hour == 64 000 MW average power
        series = [[1700000000000, 16000.0]]
        ts = parse_series(series, "Netzlast", interval_hours=0.25)
        self.assertEqual(ts.values, [64000.0])
        self.assertEqual(
            ts.timestamps[0], datetime.fromtimestamp(1700000000, tz=timezone.utc)
        )

    def test_timeseries_latest_and_window(self):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts = TimeSeries(
            name="x",
            timestamps=[t0, t0.replace(hour=1), t0.replace(hour=2)],
            values=[1.0, None, 3.0],
        )
        self.assertEqual(ts.latest(), (t0.replace(hour=2), 3.0))
        self.assertEqual(ts.latest(before=t0.replace(hour=1)), (t0, 1.0))
        w = ts.window(t0.replace(hour=1), t0.replace(hour=2))
        self.assertEqual(w.values, [None, 3.0])


if __name__ == "__main__":
    unittest.main()
