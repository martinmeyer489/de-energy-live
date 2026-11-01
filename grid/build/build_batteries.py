#!/usr/bin/env python3
"""Build data/batteries.json for the Power Grid Map battery layer.

Reads the MaStR register snapshot SQLite (all German storage units) and keeps
in-service battery units that have geocoded coordinates inside Germany.

Source DB:
  /paperclip/workspaces/chief/de-energy-live/data/mastr/mastr.db  (table: units)

The register holds ~2.9M battery rows (mostly home storage); only a small
fraction carry lon/lat — those are the ones we can map. Batteries change
slowly: run this script on demand to regenerate.

Output format (compact, columnar):
  { generatedAt, source, count, totalMW,
    cols: ["lat","lon","mw","name","op","ort","plz","bl","since"],
    batteries: [[...], ...] }
"""
import json, os, sqlite3, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data", "batteries.json")
DB = "/paperclip/workspaces/chief/de-energy-live/data/mastr/mastr.db"

# Germany-ish bounding box — drops the rare mis-geocoded row
LAT_MIN, LAT_MAX = 45.0, 56.5
LON_MIN, LON_MAX = 4.0, 16.5

COLS = ["lat", "lon", "mw", "name", "op", "ort", "plz", "bl", "since"]


def main():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    cur = con.cursor()
    cur.execute(
        """SELECT u.lat, u.lon, u.netto_mw, u.name,
                  COALESCE(NULLIF(o.name, ''), ''), u.ort, u.plz, u.bundesland, u.inbetrieb
           FROM units u LEFT JOIN operators o ON o.abr = u.operator_abr
           WHERE u.category='speicher' AND u.subtype='Batterie' AND u.status='In Betrieb'
             AND u.lat BETWEEN ? AND ? AND u.lon BETWEEN ? AND ?""",
        (LAT_MIN, LAT_MAX, LON_MIN, LON_MAX),
    )
    batteries = [
        [round(lat, 5), round(lon, 5), round(mw or 0, 3), name or "", op or "",
         ort or "", plz or "", bl or "", (since or "")[:10]]
        for lat, lon, mw, name, op, ort, plz, bl, since in cur
    ]
    con.close()

    total = round(sum(b[2] for b in batteries), 1)
    doc = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "Marktstammdatenregister (MaStR) — battery storage units in operation",
        "count": len(batteries),
        "totalMW": total,
        "cols": COLS,
        "batteries": batteries,
    }
    with open(OUT, "w") as f:
        json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
    print("wrote %s: %d batteries, %.1f MW total, %.1f KB"
          % (OUT, len(batteries), total, os.path.getsize(OUT) / 1024))


if __name__ == "__main__":
    main()
