#!/usr/bin/env python3
"""build.py — AP-12 Part BH. The store table, US + Canada, from the Geofabrik extracts.

Not wired into the app. See README.md for the pipeline, the row schema and the size study.

    python3 Docs/store-table/build.py              # every pass; a finished pass (`.done` marker) is skipped
    python3 Docs/store-table/build.py --force      # redo every pass

Needs `osmium` (Homebrew `osmium-tool`) on PATH and the two extracts in cache/.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import io
import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import storetable as st  # noqa: E402

C = st.CACHE
EXTRACTS = [("US", "us-latest.osm.pbf"), ("CA", "canada-latest.osm.pbf")]
SUMMARY = os.path.join(st.HERE, "summary-2026-09-25.md")
TIMINGS = os.path.join(C, "timings.json")
FULL = os.path.join(C, "store-table-full.jsonl.gz")
CAP_BYTES = 10 * 1000 * 1000          # Alex, 09-25: at most 10 MB added to the download
CELL = 0.005                          # join grid, degrees (~550 m lat)
LOT_M = 25.0

timings: dict = json.load(open(TIMINGS)) if os.path.exists(TIMINGS) else {}


def run_pass(name: str, out: str, cmd: list[str], force: bool):
    done = out + ".done"                # a pass killed mid-write leaves `out` but no marker
    if os.path.exists(out) and os.path.exists(done) and not force:
        print(f"[{name}] cached: {out}", file=sys.stderr)
        return
    t = time.time()
    print(f"[{name}] {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, check=True)
    open(done, "w").close()
    timings[name] = round(time.time() - t, 1)
    json.dump(timings, open(TIMINGS, "w"), indent=1)
    print(f"[{name}] {timings[name]} s", file=sys.stderr)


def osmium_passes(force: bool, only: str | None = None):
    cfg = os.path.join(C, "export-config.json")
    json.dump({"include_tags": ["building", "amenity"]}, open(cfg, "w"))
    for cc, pbf in EXTRACTS:
        if only and cc != only:
            continue
        src = os.path.join(C, pbf)
        shops = os.path.join(C, f"{cc}-shops.osm.pbf")
        run_pass(f"{cc} filter shops", shops,
                 ["osmium", "tags-filter", src, "nwr/shop", "nwr/amenity=pharmacy,fuel,marketplace",
                  "-o", shops, "--overwrite"], force)
        run_pass(f"{cc} export shops", shops.replace(".osm.pbf", ".geojsonseq"),
                 ["osmium", "export", shops, "-f", "geojsonseq", "-a", "type,id",
                  "--format-option", "print_record_separator=false",
                  "-o", shops.replace(".osm.pbf", ".geojsonseq"), "--overwrite"], force)
        bp = os.path.join(C, f"{cc}-bldg-parking.osm.pbf")
        run_pass(f"{cc} filter buildings+parking", bp,
                 ["osmium", "tags-filter", src, "wr/building", "wr/amenity=parking", "-o", bp, "--overwrite"], force)
        # ~1B building nodes in the US: an on-disk node index, not osmium's in-memory default (24 GB Mac)
        idx = os.path.join(C, f"{cc}-nodes.idx")
        run_pass(f"{cc} export buildings+parking", bp.replace(".osm.pbf", ".geojsonseq"),
                 ["osmium", "export", bp, "-f", "geojsonseq", "-c", cfg, "--geometry-types=polygon",
                  "-i", f"sparse_file_array,{idx}",
                  "--format-option", "print_record_separator=false",
                  "-o", bp.replace(".osm.pbf", ".geojsonseq"), "--overwrite"], force)
        if os.path.exists(idx):
            os.remove(idx)


# --- rows -----------------------------------------------------------------------------------------

def outer_rings(geom: dict) -> list:
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    if geom["type"] == "MultiPolygon":
        return [p[0] for p in geom["coordinates"]]
    return []


def load_rows() -> tuple[list, dict]:
    rows, seen, stats = [], set(), collections.Counter()
    lines: dict = {}
    for cc, _ in EXTRACTS:
        path = os.path.join(C, f"{cc}-shops.geojsonseq")
        with open(path) as fh:
            for line in fh:
                ft = json.loads(line)
                p = ft["properties"]
                kind = st.osm_kind(p)
                if not kind:
                    continue
                otype, oid = p["@type"], p["@id"]
                if (otype, oid) in seen:
                    stats[f"dup {cc}"] += 1          # the extracts overlap at the border: first wins (US)
                    continue
                g = ft["geometry"]
                ring = None
                if g["type"] == "Point":
                    lon, lat = g["coordinates"]
                elif g["type"] in ("Polygon", "MultiPolygon"):
                    ring = max(outer_rings(g), key=st.ring_area)
                    lat, lon = st.ring_centroid(ring)
                elif g["type"] == "LineString":
                    # osmium writes a closed area-tagged way twice (line + polygon): keep the line only
                    # as a fallback for a way that never gets a polygon (an unclosed shop way)
                    if (otype, oid) not in lines:
                        lon, lat = g["coordinates"][len(g["coordinates"]) // 2]
                        lines[(otype, oid)] = (cc, lat, lon, kind, p)
                    continue
                else:
                    stats[f"skip {g['type']}"] += 1
                    continue
                seen.add((otype, oid))
                rows.append({
                    "cc": cc, "lat": round(lat, 6), "lon": round(lon, 6), "t": otype[0], "id": oid,
                    "k": kind[0], "v": kind[1], "brand": p.get("brand", ""), "wd": p.get("brand:wikidata", ""),
                    "name": p.get("name", ""), "ring": ring, "bldg": bool(ring and p.get("building")),
                    "outline": "own" if ring else None,
                })
    for (otype, oid), (cc, lat, lon, kind, p) in lines.items():   # an unclosed shop way: midpoint vertex, no outline
        if (otype, oid) in seen:
            continue
        seen.add((otype, oid))
        stats["linestring only"] += 1
        rows.append({"cc": cc, "lat": round(lat, 6), "lon": round(lon, 6), "t": otype[0], "id": oid,
                     "k": kind[0], "v": kind[1], "brand": p.get("brand", ""), "wd": p.get("brand:wikidata", ""),
                     "name": p.get("name", ""), "ring": None, "bldg": False, "outline": None})
    return rows, stats


def first_coord(line: str):
    i = line.find('"coordinates":')
    m = re.match(r"\[+(-?[\d.eE+-]+),(-?[\d.eE+-]+)", line[i + 14:i + 80])
    return (float(m.group(1)), float(m.group(2))) if m else None


def cell(lat, lon):
    return (math.floor(lat / CELL), math.floor(lon / CELL))


def neighbours(k):
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            yield (k[0] + i, k[1] + j)


def join_buildings(rows):
    """A node inside a `building` area takes the smallest such area as its outline."""
    grid = collections.defaultdict(list)
    for r in rows:
        if r["ring"] is None and r["t"] == "n":
            grid[cell(r["lat"], r["lon"])].append(r)
    best_area = {}
    n = cand = 0
    for cc, _ in EXTRACTS:
        with open(os.path.join(C, f"{cc}-bldg-parking.geojsonseq")) as fh:
            for line in fh:
                n += 1
                if '"building"' not in line:
                    continue
                fc = first_coord(line)
                if not fc:
                    continue
                k = cell(fc[1], fc[0])
                near = [r for nb in neighbours(k) for r in grid.get(nb, ())]
                if not near:
                    continue
                cand += 1
                ft = json.loads(line)
                for ring in outer_rings(ft["geometry"]):
                    xs = [p[0] for p in ring]
                    ys = [p[1] for p in ring]
                    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                    area = None
                    for r in near:
                        if x0 <= r["lon"] <= x1 and y0 <= r["lat"] <= y1 and st.contains(r["lon"], r["lat"], ring):
                            area = area if area is not None else st.ring_area(ring)
                            if id(r) not in best_area or area < best_area[id(r)]:
                                best_area[id(r)] = area
                                r["ring"], r["outline"], r["bldg"] = ring, "building", True
    return {"lines": n, "candidates": cand, "nodesInBuilding": len(best_area)}


def join_parking(rows):
    """The AZ rule: `lot` when a parking area has a vertex within 25 m of an outline vertex."""
    grid = collections.defaultdict(list)
    for r in rows:
        if r["ring"]:
            r["bbox"] = (min(p[0] for p in r["ring"]), min(p[1] for p in r["ring"]),
                         max(p[0] for p in r["ring"]), max(p[1] for p in r["ring"]))
            for k in {cell(p[1], p[0]) for p in r["ring"]}:
                grid[k].append(r)
    pad = LOT_M / 111_320
    n = 0
    for cc, _ in EXTRACTS:
        with open(os.path.join(C, f"{cc}-bldg-parking.geojsonseq")) as fh:
            for line in fh:
                if '"parking"' not in line:
                    continue
                fc = first_coord(line)
                if not fc:
                    continue
                near = {id(r): r for nb in neighbours(cell(fc[1], fc[0])) for r in grid.get(nb, ()) if not r.get("lot")}
                if not near:
                    continue
                n += 1
                for q in outer_rings(json.loads(line)["geometry"]):
                    qb = (min(p[0] for p in q), min(p[1] for p in q), max(p[0] for p in q), max(p[1] for p in q))
                    for r in near.values():
                        if r.get("lot"):
                            continue
                        b = r["bbox"]
                        padlon = pad / max(math.cos(math.radians(r["lat"])), 0.2)
                        if qb[0] > b[2] + padlon or qb[2] < b[0] - padlon or qb[1] > b[3] + pad or qb[3] < b[1] - pad:
                            continue
                        if st.lot_adjoins(r["ring"], [q], LOT_M):
                            r["lot"] = True
    return {"parkingCandidates": n}


def finish(rows):
    for r in rows:
        r["far"] = round(st.far_m(r["lat"], r["lon"], r["ring"])) if r["ring"] else None
        r["lot"] = bool(r.get("lot"))
        e = st.map_kind((r["k"], r["v"]))
        r["cats"] = e["categories"] if e else None
        r["conv"] = bool(e and e["convenience"])
        r["big"] = st.is_big_format(r["brand"], r["name"]) if (r["brand"] or r["name"]) else False
        r["nn"] = st.norm(r["name"])


def public(r) -> dict:
    """One table row as it is stored (the fields BH item 2 names, plus category / flags)."""
    return {"lat": r["lat"], "lon": r["lon"], "osm": f"{r['t']}{r['id']}", r["k"]: r["v"],
            "brand": r["brand"] or None, "wd": r["wd"] or None, "name": r["nn"] or None,
            "far": r["far"], "lot": r["lot"], "building": r["bldg"], "cats": r["cats"],
            "conv": r["conv"], "big": r["big"], "cc": r["cc"]}


# --- encodings ------------------------------------------------------------------------------------

def gz(b: bytes) -> int:
    return len(gzip.compress(b, 9, mtime=0))


def enc_jsonl(rs, names=True, coord=6) -> bytes:
    out = io.StringIO()
    for r in rs:
        d = public(r)
        if not names:
            d.pop("name")
        if coord != 6:
            d["lat"], d["lon"] = round(r["lat"], coord), round(r["lon"], coord)
        out.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False) + "\n")
    return out.getvalue().encode()


def enc_sqlite(rs, path, names=True) -> bytes:
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE store(id INTEGER PRIMARY KEY, osm TEXT, k TEXT, v TEXT, brand TEXT, wd TEXT, "
               "name TEXT, far INTEGER, lot INTEGER, building INTEGER, lat INTEGER, lon INTEGER)")
    db.execute("CREATE VIRTUAL TABLE store_rtree USING rtree(id, minLat, maxLat, minLon, maxLon)")
    for i, r in enumerate(rs, 1):
        la, lo = round(r["lat"] * 1e6), round(r["lon"] * 1e6)
        db.execute("INSERT INTO store VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (i, f"{r['t']}{r['id']}", r["k"], r["v"], r["brand"] or None, r["wd"] or None,
                    (r["nn"] or None) if names else None, r["far"], int(r["lot"]), int(r["bldg"]), la, lo))
        db.execute("INSERT INTO store_rtree VALUES(?,?,?,?,?)", (i, r["lat"], r["lat"], r["lon"], r["lon"]))
    db.commit()
    db.execute("VACUUM")
    db.close()
    return open(path, "rb").read()


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zz(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def enc_binary(rs, names=True, coord=6, osm_ids=True) -> bytes:
    """Tile-sorted binary: rows sorted by 0.01° tile then lat; per row zig-zag varint deltas of lat/lon
    (at `coord` decimals), varint indexes into de-duplicated string tables (value, brand, wikidata,
    name), far as varint, one flag byte (osm type, lot, building, has-far); the OSM id as a varint.
    A tile index (tile key → first row) is the header. This is the size of the idea, not a final format."""
    scale = 10 ** coord
    rs = sorted(rs, key=lambda r: (math.floor(r["lat"] * 100), math.floor(r["lon"] * 100), r["lat"], r["lon"]))
    tabs = {k: {} for k in ("v", "brand", "wd", "name")}

    def idx(t, s):
        d = tabs[t]
        if s not in d:
            d[s] = len(d)
        return d[s]

    body = bytearray()
    tiles = []
    last_tile = None
    pla = plo = 0
    for i, r in enumerate(rs):
        tk = (math.floor(r["lat"] * 100), math.floor(r["lon"] * 100))
        if tk != last_tile:
            tiles.append((tk, i))
            last_tile = tk
        la, lo = round(r["lat"] * scale), round(r["lon"] * scale)
        body += varint(zz(la - pla)) + varint(zz(lo - plo))
        pla, plo = la, lo
        body += varint(idx("v", f"{r['k']}={r['v']}")) + varint(idx("brand", r["brand"]))
        body += varint(idx("wd", r["wd"]))
        if names:
            body += varint(idx("name", r["nn"]))
        flags = ("nwr".index(r["t"])) | (r["lot"] << 2) | (r["bldg"] << 3) | ((r["far"] is not None) << 4)
        body.append(flags)
        if r["far"] is not None:
            body += varint(min(r["far"], 100000))
        if osm_ids:
            body += varint(r["id"])
    head = bytearray(struct.pack("<I", len(tiles)))
    for (a, b), i in tiles:
        head += struct.pack("<hhI", a, b, i)
    strs = bytearray()
    for t in ("v", "brand", "wd", "name"):
        items = sorted(tabs[t], key=tabs[t].get)
        blob = "\x00".join(items).encode()
        strs += struct.pack("<I", len(blob)) + blob
    return bytes(head + strs + body)


# --- summary --------------------------------------------------------------------------------------

def mb(n):
    return f"{n / 1e6:.2f} MB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--osmium-only", metavar="CC", help="run only the osmium passes for US or CA, then stop")
    ap.add_argument("--app", action="store_true", help="AP-13 BI: write HereMinder/StoreTable.bin + the fixture")
    ap.add_argument("--cc", metavar="CC", help="--app on one extract only (a development run; writes to --out)")
    ap.add_argument("--out", metavar="DIR", help="--app: write the .bin and fixture here instead of HereMinder/")
    args = ap.parse_args()
    if args.app:
        global EXTRACTS, APP_SLICE
        if args.cc:
            EXTRACTS = [e for e in EXTRACTS if e[0] == args.cc]
            APP_SLICE = os.path.join(C, f"app-slice-{args.cc}.jsonl.gz")
        d = args.out
        app_main(args.force, os.path.join(d, "StoreTable.bin") if d else APP_BIN,
                 os.path.join(d, "StoreTable-fixture.json") if d else APP_FIXTURE)
        return
    t_all = time.time()
    if args.osmium_only:
        osmium_passes(args.force, args.osmium_only)
        return
    osmium_passes(args.force)

    t = time.time()
    rows, stats = load_rows()
    timings["python load shops"] = round(time.time() - t, 1)
    t = time.time()
    bstats = join_buildings(rows)
    timings["python building join"] = round(time.time() - t, 1)
    t = time.time()
    pstats = join_parking(rows)
    timings["python parking join"] = round(time.time() - t, 1)
    finish(rows)
    with gzip.open(FULL, "wt") as fh:
        for r in rows:
            fh.write(json.dumps(public(r), separators=(",", ":"), ensure_ascii=False) + "\n")

    # unmapped values → the category map
    unm = collections.Counter(f"{r['k']}={r['v']}" for r in rows if r["cats"] is None)
    cm = json.load(open(st.CATEGORY_MAP))
    cm["unmapped"] = [{"value": v, "rows": n} for v, n in unm.most_common()]
    json.dump(cm, open(st.CATEGORY_MAP, "w"), indent=1, ensure_ascii=False)

    six = set(st.OUR_CATEGORIES) - {"other"}
    slices = {
        "(a) every row": rows,
        "(b) six categories + fuel": [r for r in rows if (r["cats"] and set(r["cats"]) & six) or r["v"] == "fuel"],
        "(c) branded rows": [r for r in rows if r["brand"] or r["wd"]],
    }
    t = time.time()
    sizes, raw_sizes = {}, {}
    for sname, rs in slices.items():
        for ename, blob in (("SQLite + R-tree", enc_sqlite(rs, os.path.join(C, "tmp.sqlite"))),
                            ("tile-sorted binary", enc_binary(rs)), ("JSON lines", enc_jsonl(rs))):
            sizes[(sname, ename)] = gz(blob)
            raw_sizes[(sname, ename)] = len(blob)
    # cuts, on the smallest encoding, for any slice over the cap
    cuts = []
    for sname, rs in slices.items():
        if sizes[(sname, "tile-sorted binary")] <= CAP_BYTES:
            continue
        for label, sub, kw in [
            ("drop names", rs, dict(names=False)),
            ("drop names + OSM ids", rs, dict(names=False, osm_ids=False)),
            ("drop names + OSM ids, 1e-5° coords (~1 m)", rs, dict(names=False, osm_ids=False, coord=5)),
            ("drop `other`-only and unmapped shops", [r for r in rs if (r["cats"] and set(r["cats"]) & six) or r["v"] == "fuel"], {}),
            ("drop `other`-only/unmapped + names + OSM ids", [r for r in rs if (r["cats"] and set(r["cats"]) & six) or r["v"] == "fuel"], dict(names=False, osm_ids=False)),
            ("drop `other`-only/unmapped + fuel + names + OSM ids", [r for r in rs if r["cats"] and set(r["cats"]) & six], dict(names=False, osm_ids=False)),
        ]:
            cuts.append((sname, label, len(sub), gz(enc_binary(sub, **kw))))
    timings["python encodings"] = round(time.time() - t, 1)
    timings["total this run"] = round(time.time() - t_all, 1)
    json.dump(timings, open(TIMINGS, "w"), indent=1)
    write_summary(rows, stats, bstats, pstats, slices, sizes, raw_sizes, cuts, unm)


def write_summary(rows, stats, bstats, pstats, slices, sizes, raw_sizes, cuts, unm):
    L = []
    w = L.append
    fsz = {pbf: os.path.getsize(os.path.join(C, pbf)) for _, pbf in EXTRACTS}
    w("# AP-12 Part BH — the store table, US + Canada (2026-09-25)\n")
    w("*Generated by `Docs/store-table/build.py`. Source: Geofabrik `us-latest.osm.pbf` and "
      "`canada-latest.osm.pbf`, both the 2026-09-24 extract (`us-260924` / `canada-260924`); "
      + ", ".join(f"{p} {fsz[p] / 1e9:.2f} GB" for p in fsz) +
      f". Full table: `cache/store-table-full.jsonl.gz` ({mb(os.path.getsize(FULL))}, not committed).*\n")
    w("## Row counts\n")
    by_cc = collections.Counter(r["cc"] for r in rows)
    w("| | US | CA | Total |\n| :--- | ---: | ---: | ---: |")
    def line(label, pred):
        a = sum(1 for r in rows if r["cc"] == "US" and pred(r))
        b = sum(1 for r in rows if r["cc"] == "CA" and pred(r))
        w(f"| {label} | {a:,} | {b:,} | {a + b:,} |")
    line("rows", lambda r: True)
    line("nodes", lambda r: r["t"] == "n")
    line("areas (way / relation)", lambda r: r["t"] != "n")
    line("has brand (`brand` or `brand:wikidata`)", lambda r: r["brand"] or r["wd"])
    line("has outline (own area, or node in a building)", lambda r: r["ring"] is not None)
    line("… building outline", lambda r: r["bldg"])
    line("lot adjoins (outline rows)", lambda r: r["lot"])
    line("big-format (brand/name on `bigFormatBrandFragments`)", lambda r: r["big"])
    line("convenience flag", lambda r: r["conv"])
    for c in st.OUR_CATEGORIES:
        line(f"category: {c}", lambda r, c=c: r["cats"] and c in r["cats"])
    line("amenity=fuel", lambda r: r["v"] == "fuel" and r["k"] == "amenity")
    line("unmapped value", lambda r: r["cats"] is None)
    w(f"\nA row counts once in every category it maps to. Build notes: {dict(stats)}; building join "
      f"{bstats}; parking join {pstats}.\n")

    w("## The 50 most common brands\n")
    bc = collections.Counter(r["brand"] for r in rows if r["brand"])
    w("| # | brand | rows | | # | brand | rows |\n| ---: | :--- | ---: | --- | ---: | :--- | ---: |")
    top = bc.most_common(50)
    for i in range(min(25, len(top))):
        a, b = top[i], (top[i + 25] if i + 25 < len(top) else ("", ""))
        w(f"| {i + 1} | {a[0]} | {a[1]:,} | | {i + 26} | {b[0]} | {b[1]:,} |" if b[0] else f"| {i + 1} | {a[0]} | {a[1]:,} | | | | |")
    w("")

    w("## Nine sizes, against the 10 MB cap (Alex, 09-25)\n")
    w("Every size is the gzip -9 of the encoding (what the table adds to a compressed app download; "
      "the App Store's own compression of an .ipa is comparable, not identical). ✅ = fits under 10 MB.\n")
    encs = ["SQLite + R-tree", "tile-sorted binary", "JSON lines"]
    w("| Slice | Rows | " + " | ".join(encs) + " |\n| :--- | ---: | " + " | ".join("---:" for _ in encs) + " |")
    for sname, rs in slices.items():
        w(f"| {sname} | {len(rs):,} | " + " | ".join(
            f"{mb(sizes[(sname, e)])} {'✅' if sizes[(sname, e)] <= CAP_BYTES else '❌'}" for e in encs) + " |")
    w("\n**Uncompressed** (what the table occupies on the phone if it ships or is stored unzipped; the "
      "download adds only the gzip size above when the file is bundled compressed and inflated on first launch):\n")
    w("| Slice | " + " | ".join(encs) + " |\n| :--- | " + " | ".join("---:" for _ in encs) + " |")
    for sname in slices:
        w(f"| {sname} | " + " | ".join(mb(raw_sizes[(sname, e)]) for e in encs) + " |")
    w("")
    if cuts:
        w("**Cuts for the slices that do not fit** (on the tile-sorted binary, the smallest encoding):\n")
        w("| Slice | Cut | Rows | Size | Fits |\n| :--- | :--- | ---: | ---: | :--- |")
        for s, label, n, size in cuts:
            w(f"| {s} | {label} | {n:,} | {mb(size)} | {'✅' if size <= CAP_BYTES else '❌'} |")
        w("")
    w("## Unmapped values\n")
    w(f"{len(unm):,} distinct `shop` / `amenity` values ({sum(unm.values()):,} rows) are not in "
      "`category-map.json`; the full list with counts is its `unmapped` array. The 60 most common:\n")
    w(", ".join(f"`{v}` {n:,}" for v, n in unm.most_common(60)) + "\n")
    w("## Pass timings (fanless Air, one pass at a time)\n")
    w("| Pass | Seconds |\n| :--- | ---: |")
    for k, v in timings.items():
        w(f"| {k} | {v:,} |")
    w("\n## Verified vs inferred\n")
    w("- **Verified:** every count and size above is computed by this run from the extracts.\n"
      "- **Inferred / limits:** an area's point is its planar centroid, which can fall outside an L-shaped "
      "building; a relation's outline is its largest outer ring; a node's containing building is searched only "
      "among buildings whose first vertex lies within one 0.005° grid cell of the node (a building wider than "
      "~500 m can be missed); `lot` is tested against parking areas whose first vertex lies within one cell of "
      "an outline vertex. Where the extracts overlap at the border a feature is kept once (US first). The "
      "tile-sorted binary is a size estimate of the approach, not a finished format; the SQLite size includes "
      "the R-tree index.")
    open(SUMMARY, "w").write("\n".join(L) + "\n")
    print(f"wrote {SUMMARY}", file=sys.stderr)


# --- the app table (AP-13 Part BI) ----------------------------------------------------------------
#
#   python3 Docs/store-table/build.py --app     # the shipped table: HereMinder/StoreTable.bin + fixture
#
# Reuses the cached osmium passes. The joined slice is cached (cache/app-slice.jsonl.gz, keyed on the
# slice's OSM ids) so a second --app run only re-encodes. The byte layout is StoreTable-format.md.

APP_BIN = os.path.join(st.ROOT, "HereMinder", "StoreTable.bin")
APP_FIXTURE = os.path.join(st.ROOT, "HereMinder", "StoreTable-fixture.json")
APP_SLICE = os.path.join(C, "app-slice.jsonl.gz")
EXTRACT_DATE = "20260924"             # Geofabrik us-260924 / canada-260924
FORMAT_VERSION = 1
TILES_PER_DEGREE = 100                # 0.01° tiles
SIX = ("grocery", "hardware", "pharmacy", "electronics", "general", "garden")
# `big` = a big-format brand AND one of these kinds (review M1): never fuel, pharmacy, convenience,
# alcohol or car repair, so Costco Gas / Walmart Pharmacy / Publix Liquors stay small.
BIG_KINDS = {"supermarket", "department_store", "doityourself", "hardware", "wholesale", "electronics",
             "computer", "garden_centre", "variety_store", "country_store", "agrarian", "general"}
NEVER_BIG = {"fuel", "pharmacy", "convenience", "alcohol", "car_repair", "chemist"}
RING_TOL_M = 2.0                      # Douglas–Peucker tolerance
RING_MAX = 24                         # vertices kept per ring, at most


def big_kind(k: str, v: str) -> bool:
    if k != "shop":
        return False
    parts = {p.strip() for p in v.split(";")}
    return bool(parts & BIG_KINDS) and not (parts & NEVER_BIG)


def app_classify(r) -> str | None:
    """Why a row is left out of the shipped table, or None when it ships."""
    e = st.map_kind((r["k"], r["v"]))
    if not e or not set(e["categories"]) & set(SIX):
        return "not one of our six"
    if r["k"] == "amenity" and r["v"] == "fuel":          # N7: the key, not the value
        return "fuel"
    if e["convenience"]:
        ck = st.chain_key(r["brand"])
        if not (ck and st.chain_carries()[ck]["convenienceRowOK"]):
            return "convenience"
    return None


def slice_key(rows) -> str:
    import hashlib
    h = hashlib.sha256()
    for r in sorted(f"{r['t']}{r['id']}" for r in rows):
        h.update(r.encode())
    return h.hexdigest()


def app_slice(force: bool):
    t = time.time()
    rows, stats = load_rows()
    timings["app load shops"] = round(time.time() - t, 1)
    reasons = collections.Counter()
    sl = []
    for r in rows:
        why = app_classify(r)
        reasons[why or "ships"] += 1
        if why is None:
            sl.append(r)
    key = slice_key(sl)
    if not force and os.path.exists(APP_SLICE):
        with gzip.open(APP_SLICE, "rt") as fh:
            head = json.loads(fh.readline())
            if head.get("sliceKey") == key:
                cached = {(c["t"], c["id"]): c for c in map(json.loads, fh)}
                for r in sl:
                    c = cached[(r["t"], r["id"])]
                    r["ring"], r["outline"], r["bldg"], r["lot"] = c["ring"], c["outline"], c["bldg"], c["lot"]
                print(f"[app] joined slice reused ({len(sl):,} rows)", file=sys.stderr)
                return sl, reasons, stats, {"reused": True}
    t = time.time()
    bstats = join_buildings(sl)
    timings["app building join"] = round(time.time() - t, 1)
    t = time.time()
    pstats = join_parking(sl)
    timings["app parking join"] = round(time.time() - t, 1)
    with gzip.open(APP_SLICE, "wt") as fh:
        fh.write(json.dumps({"sliceKey": key}) + "\n")
        for r in sl:
            fh.write(json.dumps({"t": r["t"], "id": r["id"], "ring": r["ring"], "outline": r["outline"],
                                 "bldg": r["bldg"], "lot": bool(r.get("lot"))}) + "\n")
    return sl, reasons, stats, {**bstats, **pstats}


# ring simplification: local metres around the row's point

def _xy(lat0, lon0):
    kx = 111_320.0 * math.cos(math.radians(lat0))
    return lambda p: ((p[0] - lon0) * kx, (p[1] - lat0) * 111_320.0)


def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    u = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + u * dx), py - (ay + u * dy))


def _dp(pts, tol):
    """Douglas–Peucker on an open chain; returns the kept indexes."""
    keep = {0, len(pts) - 1}
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            d = _seg_dist(pts[k], pts[i], pts[j])
            if d > best:
                best, bi = d, k
        if best > tol:
            keep.add(bi)
            stack += [(i, bi), (bi, j)]
    return sorted(keep)


def simplify_ring(ring, lat0, lon0):
    """A closed [lon, lat] ring → at most RING_MAX [lon, lat] vertices (not closed), and the largest
    distance (m) of a dropped vertex from the simplified outline."""
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)
    if len(pts) <= 3:
        return pts, 0.0
    to = _xy(lat0, lon0)
    xy = [to(p) for p in pts]
    far_i = max(range(len(xy)), key=lambda k: math.hypot(xy[k][0] - xy[0][0], xy[k][1] - xy[0][1]))
    far_i = far_i or 1
    tol = RING_TOL_M
    while True:
        a = _dp(xy[:far_i + 1], tol)
        b = _dp(xy[far_i:] + [xy[0]], tol)
        idx = a + [far_i + k for k in b[1:-1]]
        if len(idx) < 3:
            idx = sorted(set(idx) | {0, far_i, (far_i // 2) or 1})[:3]
        if len(idx) <= RING_MAX:
            break
        tol *= 1.25
    kept = [xy[k] for k in idx]
    err = 0.0
    ks = set(idx)
    for k, p in enumerate(xy):
        if k in ks:
            continue
        err = max(err, min(_seg_dist(p, kept[m], kept[(m + 1) % len(kept)]) for m in range(len(kept))))
    return [pts[k] for k in idx], err


def e6(x: float) -> int:
    return int(round(x * 1_000_000))


def tile_key(lat6: int, lon6: int) -> tuple[int, int]:
    step = 1_000_000 // TILES_PER_DEGREE
    return (lat6 // step, lon6 // step)       # floor division, as the Swift reader does


def app_rows(sl):
    """The shipped fields per row; ring errors collected for the report."""
    out, errs = [], []
    for r in sl:
        e = st.map_kind((r["k"], r["v"]))
        big = (big_kind(r["k"], r["v"]) and st.is_big_format(r["brand"], r["name"])) if (r["brand"] or r["name"]) else False
        ring = None
        if big and r["ring"]:
            ring, err = simplify_ring(r["ring"], r["lat"], r["lon"])
            errs.append((err, f"{r['t']}{r['id']}", r["brand"] or r["name"]))
            ring = [(e6(p[1]), e6(p[0])) for p in ring]
        name = r["name"] or ""
        if r["brand"] and name and st.names_agree(name, r["brand"]):
            name = ""                                   # the name ships only when it tells us something
        out.append({"lat6": e6(r["lat"]), "lon6": e6(r["lon"]), "t": r["t"], "id": r["id"],
                    "kind": f"{r['k']}={r['v']}", "cats": e["categories"], "conv": e["convenience"],
                    "brand": r["brand"] or "", "name": name, "big": big, "lot": bool(r.get("lot")),
                    "ring": ring, "cc": r["cc"]})
    return out, errs


CAT_BITS = {c: 1 << i for i, c in enumerate(st.OUR_CATEGORIES)}


def _strtab(items: list[str]) -> bytes:
    blobs = [s.encode("utf-8") for s in items]
    offs, o = [], 0
    for b in blobs:
        offs.append(o)
        o += len(b)
    offs.append(o)
    return struct.pack("<I", len(items)) + struct.pack(f"<{len(offs)}I", *offs) + b"".join(blobs)


def enc_app(rows) -> bytes:
    rows = sorted(rows, key=lambda r: (tile_key(r["lat6"], r["lon6"]), r["lat6"], r["lon6"], r["t"], r["id"]))
    kinds, brands, names = {}, {}, {}

    def ix(d, s):
        if s not in d:
            d[s] = len(d)
        return d[s]

    kind_meta = {}
    body = bytearray()
    tiles = []
    step = 1_000_000 // TILES_PER_DEGREE
    cur, pla, plo = None, 0, 0
    for r in rows:
        tk = tile_key(r["lat6"], r["lon6"])
        if tk != cur:
            tiles.append([tk, len(body), 0])
            cur, pla, plo = tk, tk[0] * step, tk[1] * step      # delta base resets per tile (S4)
        tiles[-1][2] += 1
        body += varint(zz(r["lat6"] - pla)) + varint(zz(r["lon6"] - plo))
        pla, plo = r["lat6"], r["lon6"]
        k = ix(kinds, r["kind"])
        kind_meta[k] = (sum(CAT_BITS[c] for c in r["cats"]), 1 if r["conv"] else 0)
        body += varint(k)
        body += varint(ix(brands, r["brand"]) + 1 if r["brand"] else 0)
        body += varint(ix(names, r["name"]) + 1 if r["name"] else 0)
        flags = "nwr".index(r["t"]) | (r["lot"] << 2) | (r["big"] << 3) | ((r["ring"] is not None) << 4)
        body.append(flags)
        body += varint(r["id"])
        if r["ring"] is not None:
            body += varint(len(r["ring"]))
            qa, qo = r["lat6"], r["lon6"]
            for a, o in r["ring"]:
                body += varint(zz(a - qa)) + varint(zz(o - qo))
                qa, qo = a, o
    kind_list = sorted(kinds, key=kinds.get)
    ktab = struct.pack("<I", len(kind_list)) + b"".join(struct.pack("<BB", *kind_meta[i]) for i in range(len(kind_list)))
    ktab += _strtab(kind_list)
    btab = _strtab(sorted(brands, key=brands.get))
    ntab = _strtab(sorted(names, key=names.get))
    tidx = b"".join(struct.pack("<hhII", a, b, off, n) for (a, b), off, n in tiles)
    HEADER = 48
    o_k = HEADER
    o_b = o_k + len(ktab)
    o_n = o_b + len(btab)
    o_t = o_n + len(ntab)
    o_body = o_t + len(tidx)
    head = b"HMST" + struct.pack("<HH", FORMAT_VERSION, TILES_PER_DEGREE) + EXTRACT_DATE.encode()
    head += struct.pack("<IIIIIIII", len(rows), len(tiles), o_k, o_b, o_n, o_t, o_body, len(body))
    assert len(head) == HEADER
    return head + ktab + btab + ntab + tidx + bytes(body)


def dec_app(buf: bytes) -> list[dict]:
    """The reference decoder (StoreTable-format.md); the Swift reader must agree with it."""
    assert buf[:4] == b"HMST"
    ver, tpd = struct.unpack_from("<HH", buf, 4)
    assert ver == FORMAT_VERSION
    nrows, ntiles, o_k, o_b, o_n, o_t, o_body, blen = struct.unpack_from("<IIIIIIII", buf, 16)

    def strtab(o):
        n = struct.unpack_from("<I", buf, o)[0]
        offs = struct.unpack_from(f"<{n + 1}I", buf, o + 4)
        base = o + 4 + 4 * (n + 1)
        return [buf[base + offs[i]:base + offs[i + 1]].decode("utf-8") for i in range(n)]

    nk = struct.unpack_from("<I", buf, o_k)[0]
    meta = [struct.unpack_from("<BB", buf, o_k + 4 + 2 * i) for i in range(nk)]
    kinds = strtab(o_k + 4 + 2 * nk)
    brands, names = strtab(o_b), strtab(o_n)

    def rv(p):
        n = sh = 0
        while True:
            b = buf[p]
            p += 1
            n |= (b & 0x7F) << sh
            sh += 7
            if not b & 0x80:
                return n, p

    def rz(p):
        n, p = rv(p)
        return (n >> 1) ^ -(n & 1), p

    step = 1_000_000 // tpd
    out = []
    for ti in range(ntiles):
        a, b, off, n = struct.unpack_from("<hhII", buf, o_t + 12 * ti)
        p = o_body + off
        la, lo = a * step, b * step
        for _ in range(n):
            d, p = rz(p); la += d
            d, p = rz(p); lo += d
            k, p = rv(p)
            bi, p = rv(p)
            ni, p = rv(p)
            fl = buf[p]; p += 1
            oid, p = rv(p)
            ring = None
            if fl & 16:
                nv, p = rv(p)
                ring, qa, qo = [], la, lo
                for _ in range(nv):
                    d, p = rz(p); qa += d
                    d, p = rz(p); qo += d
                    ring.append((qa, qo))
            out.append({"lat6": la, "lon6": lo, "t": "nwr"[fl & 3], "id": oid, "kind": kinds[k],
                        "cats": [c for c in st.OUR_CATEGORIES if meta[k][0] & CAT_BITS[c]], "conv": bool(meta[k][1]),
                        "brand": brands[bi - 1] if bi else "", "name": names[ni - 1] if ni else "",
                        "big": bool(fl & 8), "lot": bool(fl & 4), "ring": ring, "tile": (a, b)})
    assert len(out) == nrows
    return out


FIXTURE_PINNED = ["w47947892", "w47948179", "n5195826569", "w663864462", "n11200673834"]


def fixture_rows(dec) -> list[dict]:
    """50 decoded rows: the pinned spot rows, then a deterministic spread (every k-th row, rings
    over-sampled so the ring decode is exercised)."""
    by = {f"{r['t']}{r['id']}": r for r in dec}
    pick = [by[k] for k in FIXTURE_PINNED if k in by]
    ringed = [r for r in dec if r["ring"]]
    plain = [r for r in dec if not r["ring"]]
    for src, n in ((ringed, 15), (plain, 50)):
        step = max(1, len(src) // n)
        for r in src[::step]:
            if len(pick) >= (20 if src is ringed else 50):
                break
            if r not in pick:
                pick.append(r)
    return [{"osm": f"{r['t']}{r['id']}", "lat": r["lat6"] / 1e6, "lon": r["lon6"] / 1e6, "kind": r["kind"],
             "categories": r["cats"], "brand": r["brand"], "name": r["name"], "big": r["big"], "lot": r["lot"],
             "ring": [[a / 1e6, o / 1e6] for a, o in r["ring"]] if r["ring"] else None} for r in pick[:50]]


def app_main(force: bool, out_bin: str, out_fix: str):
    t_all = time.time()
    osmium_passes(False)
    sl, reasons, stats, jstats = app_slice(force)
    t = time.time()
    rows, errs = app_rows(sl)
    blob = enc_app(rows)
    timings["app encode"] = round(time.time() - t, 1)
    dec = dec_app(blob)
    # the round trip, every row
    want = {(r["t"], r["id"]): r for r in rows}
    bad = 0
    for d in dec:
        w = want[(d["t"], d["id"])]
        if any(d[f] != w[f] for f in ("lat6", "lon6", "kind", "conv", "brand", "name", "big", "lot")) \
                or set(d["cats"]) != set(w["cats"]) \
                or (d["ring"] or None) != ([tuple(v) for v in w["ring"]] if w["ring"] else None):
            bad += 1
    assert bad == 0, f"{bad} rows did not round-trip"
    open(out_bin, "wb").write(blob)
    json.dump({"_about": "AP-13 BI: 50 rows decoded from StoreTable.bin by build.py's reference decoder "
                         "(dec_app); StoreTableEval reads the same rows through the Swift reader and compares. "
                         "DEBUG resource only.",
               "formatVersion": FORMAT_VERSION, "extractDate": EXTRACT_DATE, "rows": fixture_rows(dec)},
              open(out_fix, "w"), indent=1, ensure_ascii=False)
    timings["app total this run"] = round(time.time() - t_all, 1)
    json.dump(timings, open(TIMINGS, "w"), indent=1)
    report = app_report(rows, dec, blob, reasons, errs, jstats)
    open(os.path.join(C, "app-report.txt"), "w").write(report)
    print(report)


def app_report(rows, dec, blob, reasons, errs, jstats) -> str:
    L = []
    w = L.append
    w(f"StoreTable.bin: {len(blob):,} B raw, {gz(blob):,} B gzip -9; rows {len(rows):,}; "
      f"tiles {len(set(r['tile'] for r in dec)):,}; round trip {len(dec):,}/{len(rows):,}")
    w(f"slice: {dict(reasons)}; join {jstats}")
    for c in SIX:
        w(f"  {c}: {sum(1 for r in rows if c in r['cats']):,}")
    w(f"  big {sum(r['big'] for r in rows):,}; with ring {sum(r['ring'] is not None for r in rows):,}; "
      f"lot {sum(r['lot'] for r in rows):,}; named {sum(bool(r['name']) for r in rows):,}; "
      f"branded {sum(bool(r['brand']) for r in rows):,}; convenience rows kept {sum(r['conv'] for r in rows):,}; "
      f"US {sum(r['cc'] == 'US' for r in rows):,} / CA {sum(r['cc'] == 'CA' for r in rows):,}")
    if errs:
        errs.sort(reverse=True)
        w(f"  ring simplification: largest error {errs[0][0]:.1f} m ({errs[0][1]} {errs[0][2]}); "
          f"rings over 2 m error {sum(1 for e in errs if e[0] > RING_TOL_M):,} of {len(errs):,}")
    by = {f"{r['t']}{r['id']}": r for r in dec}
    for k in FIXTURE_PINNED:
        r = by.get(k)
        w(f"  spot {k}: " + (json.dumps({f: r[f] for f in ('lat6', 'lon6', 'kind', 'brand', 'name', 'big', 'lot', 'ring')})
                             if r else "ABSENT"))
    jl, jo = 361345 * 100, -787594 * 100
    near = [r for r in dec if abs(r["lat6"] - jl) < 20000 and abs(r["lon6"] - jo) < 25000
            and st.metres(jl / 1e6, jo / 1e6, r["lat6"] / 1e6, r["lon6"] / 1e6) <= 1000]
    w(f"  Jack's (361345_-787594): {len(near)} rows within 1 km; Best Buy among them: "
      f"{[r['brand'] or r['name'] for r in near if st.names_agree('Best Buy', r['brand'] or r['name'])]}; "
      f"within 30 m: {[(r['t'] + str(r['id']), r['kind']) for r in near if st.metres(jl / 1e6, jo / 1e6, r['lat6'] / 1e6, r['lon6'] / 1e6) <= 30]}")
    for k in ("w663864462", "n11200673834"):
        r = by.get(k)
        if r:
            w(f"  names_agree('Tractor Supply Co', {r['brand']!r}) = {st.names_agree('Tractor Supply Co', r['brand'])}")
    return "\n".join(L)


if __name__ == "__main__":
    main()
