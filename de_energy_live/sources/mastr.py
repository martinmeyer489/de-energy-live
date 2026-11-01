"""MaStR (marktstammdatenregister.de) Gesamtdatenexport client.

The Bundesnetzagentur publishes a daily full dump of the register as a
single large zip ("Gesamtdatenexport", ~3 GB compressed, no API key).
To avoid downloading all of it we read the zip central directory over
HTTP range requests and then fetch only the member files we need.

Members are cached *still deflate-compressed* under data/mastr/zip/ and
parsed via a lightweight streaming scanner (the XML is flat UTF-16, so
units are extracted with str.find rather than a full DOM parse). Parsed
rows land in a sqlite database (data/mastr/mastr.db) which all `mastr`
CLI commands query.
"""
from __future__ import annotations

import codecs
import csv
import json
import os
import re
import sqlite3
import struct
import time
import urllib.request
import zlib
from xml.sax.saxutils import unescape

EXPORT_PAGE = "https://www.marktstammdatenregister.de/MaStR/Datendownload"
UA = {"User-Agent": "de-energy-live/0.1 (+https://github.com)"}
CHUNK = 4 * 1024 * 1024

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "mastr",
)

# ---------------------------------------------------------------------------
# Remote zip (range-request) reader


def _get(url: str, headers: dict | None = None, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class RemoteZip:
    """Seekable zip directory over HTTP range requests."""

    def __init__(self, url: str):
        self.url = url
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as resp:
            self.size = int(resp.headers["Content-Length"])
        self._entries: dict[str, tuple[int, int, int, int]] | None = None

    def _range(self, start: int, end: int) -> bytes:
        return _get(self.url, {"Range": f"bytes={start}-{end}"}, timeout=300)

    def entries(self) -> dict[str, tuple[int, int, int, int]]:
        """name -> (method, compressed_size, uncompressed_size, local_hdr_off)."""
        if self._entries is not None:
            return self._entries
        tail = self._range(max(0, self.size - 200_000), self.size - 1)
        idx = tail.rfind(b"PK\x05\x06")
        if idx < 0:
            raise RuntimeError("no end-of-central-directory found")
        cd_size, cd_off = struct.unpack("<II", tail[idx + 12 : idx + 20])
        cd = self._range(cd_off, cd_off + cd_size - 1)
        entries: dict[str, tuple[int, int, int, int]] = {}
        pos = 0
        while pos + 46 <= len(cd):
            if cd[pos : pos + 4] != b"PK\x01\x02":
                break
            v = struct.unpack("<4x6H3I5H2I", cd[pos : pos + 46])
            method, csize, usize, nlen, elen, clen, lho = (
                v[3], v[7], v[8], v[9], v[10], v[11], v[15],
            )
            extra = cd[pos + 46 + nlen : pos + 46 + nlen + elen]
            # zip64 fallback: real values live in extra field 0x0001
            if usize == 0xFFFFFFFF or csize == 0xFFFFFFFF or lho == 0xFFFFFFFF:
                ep = 0
                while ep + 4 <= len(extra):
                    tag, ln = struct.unpack("<HH", extra[ep : ep + 4])
                    if tag == 1:
                        vals = struct.unpack(f"<{ln // 8}Q", extra[ep + 4 : ep + 4 + ln])
                        it = iter(vals)
                        if usize == 0xFFFFFFFF:
                            usize = next(it)
                        if csize == 0xFFFFFFFF:
                            csize = next(it)
                        if lho == 0xFFFFFFFF:
                            lho = next(it)
                        break
                    ep += 4 + ln
            name = cd[pos + 46 : pos + 46 + nlen].decode("utf-8", "replace")
            entries[name] = (method, csize, usize, lho)
            pos += 46 + nlen + elen + clen
        self._entries = entries
        return entries

    def member_compressed(self, name: str) -> tuple[int, bytes]:
        """Return (method, compressed-bytes) for a member."""
        method, csize, _usize, lho = self.entries()[name]
        head = self._range(lho, lho + 29)
        nlen, elen = struct.unpack("<HH", head[26:30])
        data = self._range(lho + 30 + nlen + elen, lho + 30 + nlen + elen + csize - 1)
        return method, data


def resolve_export_url() -> str:
    """Scrape the Datendownload page for the latest Gesamtdatenexport zip."""
    html = _get(EXPORT_PAGE, timeout=60).decode("utf-8", "replace")
    urls = re.findall(
        r'https://download\.marktstammdatenregister\.de/Gesamtdatenexport[^"\']+?\.zip',
        html,
    )
    if not urls:
        raise RuntimeError("no Gesamtdatenexport link found on Datendownload page")
    return urls[0]


# ---------------------------------------------------------------------------
# Member selection

CATALOG_MEMBERS = ["Katalogwerte.xml", "Katalogkategorien.xml"]

# key -> (member regex, unit xml tag, kind)
UNIT_SPECS: dict[str, dict] = {
    "verbrennung": {
        "re": r"EinheitenVerbrennung(_\d+)?\.xml",
        "tag": "EinheitVerbrennung",
        "subtype_tag": "Energietraeger",
        "extra_tags": ["Hauptbrennstoff", "Technologie"],
        "plant_tags": ["NameKraftwerk", "NameKraftwerksblock"],
    },
    "wind": {
        "re": r"EinheitenWind(_\d+)?\.xml",
        "tag": "EinheitWind",
        "subtype_tag": "WindAnLandOderAufSee",
        "extra_tags": ["Hersteller", "Typenbezeichnung"],
        "plant_tags": ["NameWindpark"],
    },
    "wasser": {
        "re": r"EinheitenWasser(_\d+)?\.xml",
        "tag": "EinheitWasser",
        "subtype_tag": "ArtDerWasserkraftanlage",
        "extra_tags": ["ArtDesZuflusses"],
        "plant_tags": ["NameKraftwerk"],
    },
    "biomasse": {
        "re": r"EinheitenBiomasse(_\d+)?\.xml",
        "tag": "EinheitBiomasse",
        "subtype_tag": "Biomasseart",
        "extra_tags": ["Hauptbrennstoff", "Technologie"],
        "plant_tags": ["NameKraftwerk"],
    },
    "solar": {
        "re": r"EinheitenSolar(_\d+)?\.xml",
        "tag": "EinheitSolar",
        "subtype_tag": "ArtDerSolaranlage",
        "extra_tags": ["Nutzungsbereich"],
        "plant_tags": [],
    },
    "speicher": {
        "re": r"EinheitenStromSpeicher(_\d+)?\.xml",
        "tag": "EinheitStromSpeicher",
        "subtype_tag": "Technologie",
        "extra_tags": ["Batterietechnologie"],
        "plant_tags": ["NameKraftwerk"],
    },
    "kernkraft": {
        "re": r"EinheitenKernkraft(_\d+)?\.xml",
        "tag": "EinheitKernkraft",
        "subtype_tag": "Technologie",
        "extra_tags": [],
        "plant_tags": ["NameKraftwerk", "NameKraftwerksblock"],
    },
    "geo": {
        "re": r"EinheitenGeothermieGrubengasDruckentspannung(_\d+)?\.xml",
        "tag": "EinheitGeothermieGrubengasDruckentspannung",
        "subtype_tag": "Energietraeger",
        "extra_tags": ["Technologie"],
        "plant_tags": ["NameKraftwerk"],
    },
    "gaserzeuger": {
        "re": r"EinheitenGasErzeuger(_\d+)?\.xml",
        "tag": "EinheitGasErzeuger",
        "subtype_tag": "",
        "extra_tags": [],
        "plant_tags": [],
    },
}

CONSUMER_SPECS: dict[str, dict] = {
    "stromverbraucher": {
        "re": r"EinheitenStromVerbraucher(_\d+)?\.xml",
        "tag": "EinheitStromVerbraucher",
        "name_tags": ["NameStromverbrauchseinheit"],
        "extra_tags": ["AnzahlStromverbrauchseinheitenGroesser50Mw"],
    },
    "gasverbraucher": {
        "re": r"EinheitenGasverbraucher(_\d+)?\.xml",
        "tag": "EinheitGasverbraucher",
        "name_tags": ["NameGasverbrauchsseinheit"],
        "extra_tags": ["EinheitDientDerStromerzeugung"],
    },
}

OPERATOR_RE = r"Marktakteure_\d+\.xml"
OPERATOR_TAG = "Marktakteur"

COMMON_TAGS = [
    "EinheitMastrNummer", "AnlagenbetreiberMastrNummer",
    "Postleitzahl", "Ort", "Landkreis", "Bundesland",
    "Nettonennleistung", "Bruttoleistung",
    "EinheitBetriebsstatus", "Inbetriebnahmedatum",
    "Laengengrad", "Breitengrad",
]
NAME_TAGS = ["NameStromerzeugungseinheit", "NameKraftwerk", "NameWindpark",
             "NameGaserzeugungseinheit"]

UNIT_COLS = [
    "mastr_nr", "category", "subtype", "name", "plant", "operator_abr",
    "plz", "ort", "landkreis", "bundesland",
    "netto_mw", "brutto_mw", "status", "inbetrieb", "lon", "lat", "extra",
]
CONSUMER_COLS = [
    "mastr_nr", "kind", "name", "operator_abr", "plz", "ort",
    "bundesland", "status", "inbetrieb", "lon", "lat", "extra",
]
OPERATOR_COLS = ["abr", "name"]


# ---------------------------------------------------------------------------
# Streaming parser
#
# The export XML is flat UTF-16: <Units><Unit><Field>v</Field>...</Unit>...
# We scan for <Tag>...</Tag> blocks with str.find — much faster than a full
# XML tree parse and constant memory when fed in chunks.


def iter_blocks(text_iter, tag: str):
    """Yield the inner text of each <tag>...</tag> element.

    text_iter: iterator of str chunks (partial reads of decoded XML).
    Uses a cursor into the buffer so we don't re-slice on every unit.
    """
    open_t = f"<{tag}>"
    close_t = f"</{tag}>"
    buf = ""
    pos = 0
    for chunk in text_iter:
        buf += chunk
        while True:
            i = buf.find(open_t, pos)
            if i < 0:
                # keep a tail so a tag split across chunks is still found;
                # rescan it next round (pos=0) since it may complete a tag
                keep = len(open_t)
                buf = buf[-keep:] if len(buf) > keep else buf
                pos = 0
                break
            j = buf.find(close_t, i)
            if j < 0:
                buf = buf[i:]
                pos = 0
                break
            yield buf[i + len(open_t): j]
            pos = j + len(close_t)


def member_text_chunks(blob: bytes, method: int | None = None,
                       chunk: int = CHUNK):
    """Decompress a deflate/stored member to decoded str chunks.

    method: 8 = deflate, 0 = stored, None = auto-detect.
    """
    if method is None:
        method = 8
        try:
            zlib.decompressobj(-15).decompress(blob[:4096])
        except zlib.error:
            method = 0
    dec = zlib.decompressobj(-15) if method == 8 else None
    u16 = codecs.getincrementaldecoder("utf-16")()
    for off in range(0, len(blob), chunk):
        piece = blob[off : off + chunk]
        piece = dec.decompress(piece) if dec else piece
        text = u16.decode(piece)
        if text:
            yield text
    tail = u16.decode(b"", final=True)
    if tail:
        yield tail


def block_fields(block: str) -> dict[str, str]:
    """Split a flat XML element into {tag: text} in one pass.

    Values in the export never contain raw '<' (would be escaped), and
    tags carry no attributes, so split("<") is safe and ~5x faster than
    repeated str.find per field.
    """
    d: dict[str, str] = {}
    for piece in block.split("<"):
        gt = piece.find(">")
        if gt <= 0:
            continue
        tag = piece[:gt]
        if tag.startswith("/"):
            continue
        v = piece[gt + 1:]
        d[tag] = unescape(v) if "&" in v else v
    return d


def field(block: str, tag: str) -> str:
    i = block.find(f"<{tag}>")
    if i < 0:
        return ""
    j = block.find(f"</{tag}>", i)
    if j < 0:
        return ""
    return unescape(block[i + len(tag) + 2 : j])


def _first(f: dict, tags: list[str]) -> str:
    for t in tags:
        v = f.get(t, "")
        if v:
            return v
    return ""


def _float(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _mw(v: str) -> float | None:
    """MaStR capacities are in kW — convert to MW."""
    f = _float(v)
    return f / 1000.0 if f is not None else None


def parse_unit_block(block: str, category: str, spec: dict, catalog: dict) -> dict:
    f = block_fields(block)
    decode = catalog.get
    subtype = f.get(spec["subtype_tag"], "") if spec["subtype_tag"] else ""
    return {
        "mastr_nr": f.get("EinheitMastrNummer", ""),
        "category": category,
        "subtype": decode(subtype, subtype),
        "name": _first(f, NAME_TAGS),
        "plant": _first(f, spec["plant_tags"]),
        "operator_abr": f.get("AnlagenbetreiberMastrNummer", ""),
        "plz": f.get("Postleitzahl", ""),
        "ort": f.get("Ort", ""),
        "landkreis": f.get("Landkreis", ""),
        "bundesland": decode(f.get("Bundesland", ""), f.get("Bundesland", "")),
        "netto_mw": _mw(f.get("Nettonennleistung", "")),
        "brutto_mw": _mw(f.get("Bruttoleistung", "")),
        "status": decode(f.get("EinheitBetriebsstatus", ""),
                         f.get("EinheitBetriebsstatus", "")),
        "inbetrieb": f.get("Inbetriebnahmedatum", "")[:10],
        "lon": _float(f.get("Laengengrad", "")),
        "lat": _float(f.get("Breitengrad", "")),
        "extra": ", ".join(
            decode(e, e) for e in (f.get(t, "") for t in spec["extra_tags"]) if e
        ),
    }


def parse_consumer_block(block: str, kind: str, spec: dict, catalog: dict) -> dict:
    f = block_fields(block)
    decode = catalog.get
    extras = [f.get(t, "") for t in spec["extra_tags"]]
    return {
        "mastr_nr": f.get("EinheitMastrNummer", ""),
        "kind": kind,
        "name": _first(f, spec["name_tags"]),
        "operator_abr": f.get("AnlagenbetreiberMastrNummer", ""),
        "plz": f.get("Postleitzahl", ""),
        "ort": f.get("Ort", ""),
        "bundesland": decode(f.get("Bundesland", ""), f.get("Bundesland", "")),
        "status": decode(f.get("EinheitBetriebsstatus", ""),
                         f.get("EinheitBetriebsstatus", "")),
        "inbetrieb": f.get("Inbetriebnahmedatum", "")[:10],
        "lon": _float(f.get("Laengengrad", "")),
        "lat": _float(f.get("Breitengrad", "")),
        "extra": ", ".join(e for e in extras if e and e != "0"),
    }


def parse_operator_block(block: str) -> tuple[str, str]:
    f = block_fields(block)
    name = f.get("Firmenname", "") or " ".join(
        p for p in (f.get("Vorname", ""), f.get("Nachname", "")) if p
    )
    return f.get("MastrNummer", ""), name


def build_catalog(blobs: list[bytes]) -> dict[str, str]:
    """Katalogwert.Id -> Wert (ids are global across categories)."""
    cat: dict[str, str] = {}
    for blob in blobs:
        for block in iter_blocks(member_text_chunks(blob), "Katalogwert"):
            kid = field(block, "Id")
            if kid:
                cat[kid] = field(block, "Wert")
    return cat


# ---------------------------------------------------------------------------
# Cache layout

def _paths(data_dir: str) -> dict:
    return {
        "zip": os.path.join(data_dir, "zip"),
        "partials": os.path.join(data_dir, "partials"),
        "db": os.path.join(data_dir, "mastr.db"),
        "meta": os.path.join(data_dir, "meta.json"),
        "catalog": os.path.join(data_dir, "catalog.json"),
        "abrs": os.path.join(data_dir, "needed_abrs.txt"),
    }


def _load_meta(data_dir: str) -> dict:
    try:
        return json.load(open(_paths(data_dir)["meta"]))
    except OSError:
        return {}


def _save_meta(data_dir: str, meta: dict) -> None:
    json.dump(meta, open(_paths(data_dir)["meta"], "w"), indent=2)


def select_members(entries: dict, pattern: str) -> list[str]:
    rx = re.compile(f"^{pattern}$")
    return sorted(n for n in entries if rx.match(n))


def download_members(
    url: str, names: list[str], zip_dir: str, entries: dict | None = None
) -> None:
    """Fetch missing members (compressed bytes) into zip_dir/<name>.deflate."""
    z = RemoteZip(url)
    ent = entries or z.entries()
    os.makedirs(zip_dir, exist_ok=True)
    for n in names:
        dest = os.path.join(zip_dir, n + ".deflate")
        want = ent[n][1]
        if os.path.exists(dest) and os.path.getsize(dest) == want:
            continue
        t0 = time.time()
        _method, data = z.member_compressed(n)
        if len(data) != want:
            raise RuntimeError(f"{n}: got {len(data)} bytes, expected {want}")
        tmp = dest + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
        print(f"  {n:<55} {want/1e6:8.1f} MB  ({time.time()-t0:.0f}s)", flush=True)


def _member_blob(zip_dir: str, name: str) -> bytes:
    return open(os.path.join(zip_dir, name + ".deflate"), "rb").read()


# ---------------------------------------------------------------------------
# Update pipeline

def _write_csv(path: str, cols: list[str], rows) -> int:
    n = 0
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        for r in rows:
            w.writerow(r)
            n += 1
    return n


def _parse_member_worker(args):
    """multiprocessing entry: parse one cached member -> partial CSV."""
    name, zip_dir, part_dir, category, kind, spec, min_abr_mw = args
    catalog = _parse_member_worker.catalog  # set by pool initializer
    blob = _member_blob(zip_dir, name)
    cols = UNIT_COLS if kind == "unit" else CONSUMER_COLS
    prefix = "u_" if kind == "unit" else "c_"
    abrs = set()
    count = 0
    with open(os.path.join(part_dir, prefix + name + ".csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        for block in iter_blocks(member_text_chunks(blob), spec["tag"]):
            if kind == "unit":
                row = parse_unit_block(block, category, spec, catalog)
                if (row["netto_mw"] or 0) >= min_abr_mw or category not in (
                    "solar", "speicher"
                ):
                    abrs.add(row["operator_abr"])
            else:
                row = parse_consumer_block(block, category, spec, catalog)
                abrs.add(row["operator_abr"])
            w.writerow(row)
            count += 1
    return name, count, abrs


def _init_worker(catalog_path: str):
    _parse_member_worker.catalog = json.load(open(catalog_path))


def _parse_operator_worker(args):
    name, zip_dir, part_dir, _abr_file = args
    needed = _parse_operator_worker.needed  # set by pool initializer
    blob = _member_blob(zip_dir, name)
    n = 0
    with open(os.path.join(part_dir, "op_" + name + ".csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OPERATOR_COLS)
        for block in iter_blocks(member_text_chunks(blob), OPERATOR_TAG):
            abr, aname = parse_operator_block(block)
            if abr in needed:
                w.writerow({"abr": abr, "name": aname})
                n += 1
    return name, n


def _init_operator_worker(abr_file: str):
    _parse_operator_worker.needed = set(
        l.strip() for l in open(abr_file) if l.strip()
    )


def update(
    data_dir: str = DATA_DIR,
    groups: list[str] | None = None,
    skip_operators: bool = False,
    force: bool = False,
    jobs: int = 2,
) -> None:
    """Download + parse the Gesamtdatenexport into the local sqlite cache."""
    import multiprocessing as mp

    p = _paths(data_dir)
    os.makedirs(p["zip"], exist_ok=True)
    os.makedirs(p["partials"], exist_ok=True)

    url = resolve_export_url()
    print(f"MaStR export: {url}")
    z = RemoteZip(url)
    entries = z.entries()
    print(f"{len(entries)} members, {z.size/1e9:.2f} GB total")

    meta = _load_meta(data_dir)
    same_export = meta.get("export_url") == url and not force
    if not same_export:
        # new export (or --force): drop stale partials so nothing old
        # leaks into the rebuilt database
        for fn in os.listdir(p["partials"]):
            if fn.endswith(".csv"):
                os.remove(os.path.join(p["partials"], fn))
        if os.path.exists(p["abrs"]):
            os.remove(p["abrs"])

    wanted = dict(UNIT_SPECS)
    if groups:
        wanted = {k: v for k, v in wanted.items() if k in groups}

    member_plan: dict[str, str] = {}  # member -> category
    for cat, spec in wanted.items():
        for m in select_members(entries, spec["re"]):
            member_plan[m] = cat
    consumer_plan = {
        m: cat for cat, spec in CONSUMER_SPECS.items()
        for m in select_members(entries, spec["re"])
    }
    catalog_members = [m for m in CATALOG_MEMBERS if m in entries]
    operator_members = (
        [] if skip_operators else select_members(entries, OPERATOR_RE)
    )

    download = sorted(set(member_plan) | set(consumer_plan) |
                      set(catalog_members) | set(operator_members))
    print(f"Downloading {len(download)} members -> {p['zip']}")
    download_members(url, download, p["zip"], entries)

    # --- catalog -----------------------------------------------------------
    catalog = build_catalog([_member_blob(p["zip"], m) for m in catalog_members])
    json.dump(catalog, open(p["catalog"], "w"))
    print(f"catalog: {len(catalog)} values")

    # --- units + consumers ---------------------------------------------------
    jobs_list = []
    for m, cat in member_plan.items():
        spec = wanted[cat]
        part = os.path.join(p["partials"], "u_" + m + ".csv")
        if same_export and os.path.exists(part):
            continue
        jobs_list.append((m, p["zip"], p["partials"], cat, "unit", spec, 1.0))
    for m, cat in consumer_plan.items():
        spec = CONSUMER_SPECS[cat]
        part = os.path.join(p["partials"], "c_" + m + ".csv")
        if same_export and os.path.exists(part):
            continue
        jobs_list.append((m, p["zip"], p["partials"], cat, "consumer", spec, 0.0))

    needed_abrs: set[str] = set()
    if jobs_list:
        print(f"Parsing {len(jobs_list)} unit/consumer members ({jobs} workers)")
        t0 = time.time()
        with mp.Pool(jobs, initializer=_init_worker, initargs=(p["catalog"],)) as pool:
            for i, (m, n, abrs) in enumerate(
                pool.imap_unordered(_parse_member_worker, jobs_list), 1
            ):
                needed_abrs |= abrs
                print(f"  [{i}/{len(jobs_list)}] {m:<50} {n:>8,} rows", flush=True)
        print(f"  parsed in {time.time()-t0:.0f}s")

    if os.path.exists(p["abrs"]):
        needed_abrs |= set(l.strip() for l in open(p["abrs"]) if l.strip())
    with open(p["abrs"], "w") as fh:
        fh.write("\n".join(sorted(needed_abrs)))

    # --- operators -----------------------------------------------------------
    if operator_members:
        todo = [
            (m, p["zip"], p["partials"], p["abrs"])
            for m in operator_members
            if not (same_export and os.path.exists(
                os.path.join(p["partials"], "op_" + m + ".csv")))
        ]
        if todo:
            print(f"Parsing {len(todo)} Marktakteure members "
                  f"({len(needed_abrs):,} needed operators)")
            with mp.Pool(
                jobs, initializer=_init_operator_worker, initargs=(p["abrs"],)
            ) as pool:
                for i, (m, n) in enumerate(
                    pool.imap_unordered(_parse_operator_worker, todo), 1
                ):
                    print(f"  [{i}/{len(todo)}] {m:<50} {n:>6,} operators", flush=True)

    # --- sqlite ----------------------------------------------------------------
    build_db(data_dir)

    meta = {
        "export_url": url,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "categories": sorted(set(member_plan.values())),
        "operators": bool(operator_members),
    }
    _save_meta(data_dir, meta)
    print("done")


def _iter_part_csv(path: str):
    with open(path, newline="") as fh:
        yield from csv.reader(fh)


def build_db(data_dir: str = DATA_DIR) -> None:
    """(Re)build mastr.db from partial CSVs."""
    p = _paths(data_dir)
    tmp = p["db"] + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE units(
        mastr_nr TEXT, category TEXT, subtype TEXT, name TEXT, plant TEXT,
        operator_abr TEXT, plz TEXT, ort TEXT, landkreis TEXT, bundesland TEXT,
        netto_mw REAL, brutto_mw REAL, status TEXT, inbetrieb TEXT,
        lon REAL, lat REAL, extra TEXT)""")
    cur.execute("""CREATE TABLE consumers(
        mastr_nr TEXT, kind TEXT, name TEXT, operator_abr TEXT, plz TEXT,
        ort TEXT, bundesland TEXT, status TEXT, inbetrieb TEXT,
        lon REAL, lat REAL, extra TEXT)""")
    cur.execute("CREATE TABLE operators(abr TEXT PRIMARY KEY, name TEXT)")

    def _bulk_insert(table: str, ncols: int, fp: str, numeric_idx=()) -> int:
        q = f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * ncols)})"
        n = 0
        batch = []
        cur2 = conn.cursor()
        for r in _iter_part_csv(fp):
            for i in numeric_idx:
                r[i] = float(r[i]) if r[i] else None
            batch.append(r)
            if len(batch) >= 20000:
                cur2.executemany(q, batch)
                n += len(batch)
                batch = []
        if batch:
            cur2.executemany(q, batch)
            n += len(batch)
        return n

    n_units = n_cons = n_ops = 0
    for fn in sorted(os.listdir(p["partials"])):
        fp = os.path.join(p["partials"], fn)
        if fn.startswith("op_") and fn.endswith(".csv"):
            n_ops += _bulk_insert("operators", len(OPERATOR_COLS), fp)
        elif fn.startswith("u_") and fn.endswith(".csv"):
            n_units += _bulk_insert("units", len(UNIT_COLS), fp,
                                    numeric_idx=(10, 11, 14, 15))
        elif fn.startswith("c_") and fn.endswith(".csv"):
            n_cons += _bulk_insert("consumers", len(CONSUMER_COLS), fp,
                                   numeric_idx=(9, 10))
        conn.commit()
    cur.execute("CREATE INDEX idx_units_cat ON units(category, netto_mw)")
    cur.execute("CREATE INDEX idx_units_plz ON units(plz)")
    cur.execute("CREATE INDEX idx_units_plz_mw ON units(plz, netto_mw)")
    cur.execute("CREATE INDEX idx_units_subtype ON units(subtype)")
    # covering indexes for the dashboard's capacity/battery queries:
    # (cat, subtype, status, mw) serves "top N by MW" + filtered COUNT/SUM,
    # the bundesland variant serves per-state aggregates — both fully in-index
    cur.execute("CREATE INDEX idx_units_csm "
                "ON units(category, subtype, status, netto_mw)")
    cur.execute("CREATE INDEX idx_units_csbl "
                "ON units(category, subtype, status, bundesland, netto_mw)")
    # covering index for the unit map: per-state aggregation incl.
    # coordinate-validity counts without touching the main table
    cur.execute("CREATE INDEX idx_units_map "
                "ON units(category, subtype, status, bundesland,"
                "         lon, lat, netto_mw)")
    cur.execute("CREATE INDEX idx_cons_plz ON consumers(plz)")
    conn.commit()
    conn.close()
    os.replace(tmp, p["db"])
    print(f"mastr.db: {n_units:,} units, {n_cons:,} consumers, "
          f"{n_ops:,} operators")


def db_path(data_dir: str = DATA_DIR) -> str | None:
    p = _paths(data_dir)["db"]
    return p if os.path.exists(p) else None


def open_db(data_dir: str = DATA_DIR) -> sqlite3.Connection:
    p = db_path(data_dir)
    if not p:
        raise RuntimeError(
            f"no MaStR cache at {data_dir} — run `python main.py mastr update` first"
        )
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


def operator_names(conn: sqlite3.Connection, abrs) -> dict[str, str]:
    abrs = [a for a in set(abrs) if a]
    if not abrs:
        return {}
    q = ",".join("?" * len(abrs))
    return {
        r["abr"]: r["name"]
        for r in conn.execute(f"SELECT abr,name FROM operators WHERE abr IN ({q})", abrs)
    }
