
DOMAIN = "ezlopi"
EZLOPI_API_URL_BASE = "https://api-cloud.ezlo.com/v1/request"
# v4 REST login endpoint. Returns the JWT plus the legacy MMS token pair at the
# top level: {"token": ..., "legacy_token": {"auth": ..., "sig": ...}}. The
# legacy pair (MMSAuth/MMSAuthSig) is what the NMA broker's loginUserMios call
# expects, so we use the `login/1` variant which includes it (the `login/3`
# variant returned only the JWT).
EZLOPI_LOGIN_URL = "https://api-cloud.ezlo.com/api/v4/login/1/sessions"
# v4 REST controller listing. GET with the JWT in the `x-access-token` header;
# returns {"controllers": [...], "pagination": {...}} with no envelope (the
# legacy v1 call wrapped it in {"data": {"controllers": [...]}}).
EZLOPI_CONTROLLER_LIST_URL = "https://api-cloud.ezlo.com/api/v4/controller_list/3/controllers"
EZLOPI_API = "EZLOPI_API"
WS_API = "ws_api"
LOCK = "lock"
# Public ezloPi firmware release manifest — the same source the controllers use
# for OTA. Its top-level `version` is the latest available firmware. This is the
# LEGACY path, kept only as a fallback behind the `firmware_catalog` feature flag
# (OFF): new clients ask the firmware catalog instead and must not read this.
EZLOPI_FIRMWARE_MANIFEST_URL = "https://dl.mios.com/ezloPI/manifest.json"
# Server-side firmware catalog (cloud/firmware-catalog). GET /decision returns
# the single image to flash for a product+version; the server does the compare
# and never offers a downgrade. Anonymous, no token. This is the default path.
EZLOPI_FIRMWARE_CATALOG_BASE = "https://api-cloud.ezlo.com/api/v4/firmware_catalog/1"
# Remote feature-flag service (cloud/feature-flags). GET /flags returns a small
# document of booleans (the kill switch the mobile apps use); GET /snapshot is
# the same document with defaults, baked in as the offline fallback.
EZLOPI_FEATURE_FLAGS_BASE = "https://api-cloud.ezlo.com/api/v4/feature_flags/1"