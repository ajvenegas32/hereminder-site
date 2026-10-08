# The store table's license — OpenStreetMap, ODbL 1.0 (AP-12 Part BH; fixed AP-13 Part BI)

*This is a legal obligation, not a preference (INBOX resume #28). It is written from the license
text and the OpenStreetMap Foundation's published guidance, not legal advice; where it interprets,
it says so.*

## Source

- **Data:** OpenStreetMap, © OpenStreetMap contributors.
- **Extracts:** Geofabrik GmbH, `https://download.geofabrik.de/north-america/us-latest.osm.pbf` and
  `…/canada-latest.osm.pbf`, which on 2026-09-25 redirected to **`us-260924.osm.pbf`** and
  **`canada-260924.osm.pbf`** — the extracts of **2026-09-24** (OSM data up to that day).
  Sizes 12,165,160,484 and 6,497,217,695 bytes; MD5 `aa052009acb78268002753451dd7944f` (US) and
  `ac6eaa9bdd7c3030941a45970a649a1a` (Canada), both matching Geofabrik's published `.md5` files.
- **Built by:** `Docs/store-table/build.py --app` (this repo). The offered file is that `--app` slice,
  not the full working table: rows whose kind maps to one of six categories (grocery, hardware,
  pharmacy, electronics, general, garden), with no `amenity=fuel` rows and no convenience-kind rows
  except those of the chains flagged `convenienceRowOK` in `chain-carries.json`. Per row it carries the
  point, OSM type + id, the kind (with its category mask and convenience flag), `brand`, the OSM `name`
  (only when there is no brand or it disagrees with the brand), `big`, `lot`, and a building outline
  (≤ 24 vertices) for big rows that have one. `far` is not stored. Exactly which rows ship:
  `StoreTable-format.md`, section "Which rows ship" (AP-13-fix S8).
- **License:** Open Database License 1.0 — `https://opendatacommons.org/licenses/odbl/1-0/`.
  Individual contents: Database Contents License 1.0.

## What the ODbL asks of us

1. **Attribution wherever the data is used publicly** (ODbL §4.3). The app shows it, and so does
   any page offering the table. OSMF's attribution guideline asks for the words below plus a link
   to `https://www.openstreetmap.org/copyright`, reachable where the data shows up; the notice also
   carries the ODbL's own URI (§4.2(b), review S2). For an app with no map that is a line in Settings
   (AP-13 BK puts it in Settings → Help; the App Store description is a good second place).
2. **Share-alike for the database itself** (§4.4). The extracted table is a *Derivative Database*
   (we selected and transformed OSM data), and it is "publicly used" once it ships inside an app
   (the definition of "Publicly" is in §1 — conveyed to the public). So **the table as shipped must be
   offered under the ODbL** (§4.4, §4.6: "a copy of the Derivative Database … or a file containing all
   of the alterations … or the algorithm"). **The offered file is the shipped table as used** —
   `HereMinder/StoreTable.bin` byte for byte — which carries our own columns merged into it: the
   category mask per kind, the convenience flag, `big` and `lot` (review S2). What to publish, and
   where, is `publish/README.md`; publishing `build.py` and `StoreTable-format.md` alongside is the
   belt to that suspenders.
3. **No technical restriction on the offered copy** (§4.7) — no DRM on the published file. Shipping it
   inside the app bundle is fine as long as the public copy is freely downloadable.
4. **Our own data stays ours — except what we merge into the table.** The app's code, the chain
   carries table and the brand lists (Swift, and the frozen JSON here) are not pulled under the ODbL
   by sitting next to the table (*interpretation*: they are separate works, not a Derivative
   Database; OSMF's "Collective Database" guidance supports this). **It does not hold for the
   columns merged into the table** (review S2): the category mask, convenience flag, `big` and `lot`
   inside `StoreTable.bin` are part of the Derivative Database and are offered under the ODbL with it.

## The notice to carry

**In the app** (Settings → Help, AP-13 BK; both links live — the ODbL URI is required, §4.2(b)):

> Store data © OpenStreetMap contributors, available under the Open Database License.
> https://www.openstreetmap.org/copyright · https://opendatacommons.org/licenses/odbl/1-0/

**With the published table** (a README beside the file):

> This database is derived from OpenStreetMap data (Geofabrik extract of 2026-09-24) and is made
> available under the Open Database License 1.0: https://opendatacommons.org/licenses/odbl/1-0/.
> Any rights in individual contents of the database are licensed under the Database Contents
> License: https://opendatacommons.org/licenses/dbcl/1-0/.
> © OpenStreetMap contributors — https://www.openstreetmap.org/copyright.
> Built by HereMinder's `build.py` (published alongside, with `StoreTable-format.md`, the byte layout).

## Not done here (binding scope)

Publishing the table is Alex's (`publish/README.md`); **before build 81 reaches anyone but Alex**, the
file must be hosted. The in-app credit lands in AP-13 BK.
