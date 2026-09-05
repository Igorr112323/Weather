# Browser dependency

## Leaflet 1.9.4

Source: https://registry.npmjs.org/leaflet/-/leaflet-1.9.4.tgz
Upstream: https://leafletjs.com/
License: BSD-2-Clause, retained in `leaflet/LICENSE.txt`.

The shipped JS and CSS are derived from the pinned npm distribution with
esbuild 0.25.10. Code comments and source-map references are removed; the full
copyright and license notice is distributed beside the resulting assets.

`leaflet/manifest.json` records upstream npm integrity, resulting SHA-256 hashes
and browser SRI values. `package-lock.json` locks the source and build tools.
Run `npm ci --ignore-scripts && npm run check:vendor` from `AgroCast` to verify
that the checked-in bytes match the locked input. `npm run vendor` regenerates
the assets; update the HTML SRI attributes and review the diff before release.

AgroCast does not request third-party map tiles in the closed pilot. The map is
an explicitly labelled schematic of the fixed inspection points, not an OSM
basemap or a production forecast layer. This avoids sending user locations to
external map services before the source/privacy review.
