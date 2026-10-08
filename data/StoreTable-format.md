# StoreTable.bin — byte layout (format version 1)

*AP-13 Part BI, 2026-09-26. Written by `build.py --app` (`enc_app`); the reference decoder is
`build.py`'s `dec_app`; the app's reader is `HereMinder/StoreTable.swift`. All three must agree —
change one, change all three, and bump the format version.*

The file ships **uncompressed** in the app bundle (every configuration) so the app can memory-map it.
All integers are **little-endian**. Offsets are absolute byte offsets from the start of the file
unless a field says otherwise. Nothing is aligned; readers use unaligned loads.

## Header (48 bytes)

| Offset | Type | Field |
| ---: | :--- | :--- |
| 0 | 4 × u8 | magic `HMST` |
| 4 | u16 | format version (`1`) |
| 6 | u16 | tiles per degree (`100` → 0.01° tiles) |
| 8 | 8 × ASCII | OSM extract date, `YYYYMMDD` (`20260924`) |
| 16 | u32 | row count |
| 20 | u32 | tile count |
| 24 | u32 | offset of the kind table |
| 28 | u32 | offset of the brand string table |
| 32 | u32 | offset of the name string table |
| 36 | u32 | offset of the tile index |
| 40 | u32 | offset of the body |
| 44 | u32 | body length in bytes |

## String table (used three times)

| Type | Field |
| :--- | :--- |
| u32 | `n`, the number of strings |
| (n + 1) × u32 | offsets of each string's first byte, relative to the blob start; entry `n` is the blob length |
| bytes | the blob: the strings' UTF-8 bytes back to back, no terminators |

String `i` is `blob[off[i] ..< off[i+1]]`.

## Kind table

| Type | Field |
| :--- | :--- |
| u32 | `k`, the number of kinds |
| k × (u8, u8) | per kind: **category mask**, **flags** |
| string table | the kind strings, e.g. `shop=supermarket`, `amenity=pharmacy`, `shop=coffee;tea` |

Category mask bits: 0 grocery · 1 hardware · 2 pharmacy · 3 electronics · 4 general · 5 garden ·
6 other — the categories `category-map.json` gives that kind (a semicolon value is the union of its
parts). Flags bit 0 = convenience. The mask is **our** mapping, shipped so the app and the pipeline
never disagree about a kind.

## Tile index

`tile count` entries of 12 bytes, sorted by (lat key, lon key):

| Type | Field |
| :--- | :--- |
| i16 | lat key = ⌊lat × 1e6 / 10 000⌋ (floor division, so −78.54 → −7855) |
| i16 | lon key, the same |
| u32 | the tile's first byte, relative to the body start |
| u32 | rows in the tile |

A reader finds a tile by binary search on (lat key, lon key).

## Body: rows, tile by tile

Coordinates are integers in **1e-6 degrees**. `varint` = unsigned LEB128; `zigzag` = `(n << 1) ^ (n >> 63)`,
decoded `(u >> 1) ^ -(u & 1)`. Within a tile rows are sorted by lat, then lon.

The **delta base resets at each tile** to the tile's south-west corner (`lat key × 10 000`,
`lon key × 10 000`), so any tile decodes without reading the ones before it.

Per row:

| Type | Field |
| :--- | :--- |
| zigzag varint | lat − previous lat (the tile corner for the first row) |
| zigzag varint | lon − previous lon |
| varint | kind index |
| varint | brand index + 1 (0 = no brand) — OSM `brand`, as tagged |
| varint | name index + 1 (0 = none) — OSM `name`, as tagged; shipped only when the row has no brand, or its name does not agree with its brand (`names_agree`), e.g. a franchise under its owner's name |
| u8 | flags: bits 0–1 OSM type (0 node, 1 way, 2 relation) · bit 2 `lot` · bit 3 `big` · bit 4 has ring |
| varint | OSM id (the number; the type is in the flags) |
| *if has ring:* varint | vertex count `v` (3 … 24) |
| *if has ring:* v × (zigzag varint, zigzag varint) | vertex lat, lon — the first as a delta from the row's point, each next from the previous vertex. The ring is **not** closed (the last vertex does not repeat the first). |

## What the fields mean

- **Point:** a node's position, or an area's planar centroid (which can fall outside an L-shaped building).
- **`big`:** a big-format brand (`big-format-brands.json`, matched on brand or name) **and** a big-store
  kind (`build.py` `BIG_KINDS`): never fuel, pharmacy, convenience, alcohol or car repair (review M1).
- **Ring:** only on `big` rows with an outline — the row's own area, or the smallest `building` area
  containing a node. Douglas–Peucker at 2 m; where more than 24 vertices remain the tolerance rises
  until 24 do. `far` is **not** stored: the app measures it from its own fence pin (review M2).
- **`lot`:** a parking area has a vertex within 25 m of the full (unsimplified) outline (the AZ rule).
  False for a row with no outline.

## Which rows ship

A row ships when its kind maps to at least one of grocery / hardware / pharmacy / electronics /
general / garden, **except** `amenity=fuel` rows and convenience-kind rows. A convenience-kind row
still ships when its brand matches a `chain-carries.json` chain marked `convenienceRowOK`
(drugstores and dollar stores OSM tags as convenience).
