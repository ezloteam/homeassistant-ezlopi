
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