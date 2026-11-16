"""Constants for the Origin Energy integration."""

DOMAIN = "origin_energy"

AUTH_DOMAIN = "id.originenergy.com.au"
APP_DOMAIN = "www.originenergy.com.au"
API_DOMAIN = "api.rx.originenergy.com.au"

# Public SPA client_id/audience used by originenergy.com.au's "My Account" web app.
CLIENT_ID = "yOHRT97N3yH85jzTDlqN2A7Cf2D0cmQe"
AUDIENCE = "https://digitalapi"
REDIRECT_URI = f"https://{APP_DOMAIN}/auth/callback"
SCOPE = "openid profile email read:api all"

AUTHORIZE_URL = f"https://{AUTH_DOMAIN}/authorize"
TOKEN_URL = f"https://{AUTH_DOMAIN}/oauth/token"
GRAPHQL_URL = f"https://{API_DOMAIN}/v1/gateway/graphql"

# Browser identity headers required by Origin's WAF so requests to /authorize
# and the GraphQL gateway are not blocked with HTTP 403.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": f"https://{APP_DOMAIN}",
    "Referer": f"https://{APP_DOMAIN}/",
}

# auth0-spa-js telemetry header sent by the SPA on silent /authorize calls.
AUTH0_SPA_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMTAuMCJ9"

# Name of the Auth0 SSO session cookie on AUTH_DOMAIN.
SESSION_COOKIE_NAME = "auth0"

CONF_SESSION_COOKIE = "session_cookie"
CONF_AGREEMENT_ID = "agreement_id"
CONF_SERVICE_TYPE = "service_type"
CONF_TIMEZONE = "timezone"
CONF_TIME_UNIT = "time_unit"
CONF_UPDATE_INTERVAL_HOURS = "update_interval_hours"
CONF_BACKFILL_COMPLETE = "backfill_complete"

SERVICE_TYPES = (
    "ELECTRICITY",
    "EMBEDDED_ELECTRICITY",
    "EMBEDDED_HOT_WATER",
    "GAS",
)

PREFERRED_TIME_UNITS = ("HOURLY", "DAILY")
DEFAULT_TIME_UNIT = "DAILY"
DEFAULT_TIMEZONE = "Australia/Sydney"

DEFAULT_UPDATE_INTERVAL_HOURS = 4
MIN_UPDATE_INTERVAL_HOURS = 1
MAX_UPDATE_INTERVAL_HOURS = 24

# Re-fetch a trailing window on every update to capture late revisions.
OVERLAP_DAYS = 5

# Upper bound on the first-run backfill (bounded below by service.validFrom).
FIRST_RUN_HISTORY_DAYS = 730

REQUEST_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2

# Sub-daily granularities cap at 9 days on Origin's backend; DAILY caps at 1000 points.
MAX_QUERY_DAYS = {"HALF_HOURLY": 7, "HOURLY": 7, "DAILY": 900, "MONTHLY": 900}
DEFAULT_QUERY_DAYS = 7

