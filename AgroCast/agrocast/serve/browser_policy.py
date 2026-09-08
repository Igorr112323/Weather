from agrocast.core.settings import bundle_static_dir

STATIC = bundle_static_dir()
ASSET_NAMES = (
    "dom.js", "client.js", "index.js", "index.css", "report.js", "report.css", "value.js", "value.css",
    "desktop.js", "desktop.css", "agrocast.png",
    "vendor/leaflet/leaflet.js", "vendor/leaflet/leaflet.css", "vendor/leaflet/LICENSE.txt",
    "vendor/leaflet/images/layers.png", "vendor/leaflet/images/layers-2x.png",
    "vendor/leaflet/images/marker-icon.png", "vendor/leaflet/images/marker-icon-2x.png", "vendor/leaflet/images/marker-shadow.png",
)
ASSETS = {"/assets/" + name: STATIC / name for name in ASSET_NAMES}
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'none'",
    "script-src 'self'",
    "script-src-attr 'none'",
    "style-src 'self'",
    "style-src-attr 'none'",
    "img-src 'self' data: https://tile.openstreetmap.org https://*.basemaps.cartocdn.com https://*.cartocdn.com https://*.tile.openstreetmap.fr https://tile.openstreetmap.fr https://*.tile.openstreetmap.org",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-src 'none'",
    "frame-ancestors 'self' https://arena.ai https://*.arena.ai",
    "worker-src 'none'",
    "manifest-src 'self'",
    "upgrade-insecure-requests",
))
BROWSER_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-AgroCast-UI-Policy": "dom-csp-v1",
}


def browser_headers(headers):
    headers.update(BROWSER_HEADERS)
    headers.add_vary_header("Cookie")
