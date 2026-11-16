# Getting your Origin Energy Session Cookie (`auth0`)

Origin Energy's interactive login endpoint (`POST /usernamepassword/login`) sits behind **Kasada** anti-bot protection, which blocks scripted password logins. However, the silent OAuth PKCE authorization (`/authorize?prompt=none`), token exchange (`/oauth/token`), and Kraken GraphQL API (`api.rx.originenergy.com.au/v1/gateway/graphql`) work over standard HTTP once you have a valid Auth0 SSO cookie.

Auth0 rolls the `auth0` cookie's ~100-day expiry forward on every silent token refresh, and this integration automatically persists the rolled cookie so your session stays alive indefinitely.

## Step-by-Step Instructions

1. Open a normal browser window, go to <https://www.originenergy.com.au> and sign in to **My Account**.
   > **Important:** Keep DevTools **closed** while signing in. Origin's bot protection detects an open DevTools panel during login.
2. In the **same tab**, navigate to:
   ```text
   https://id.originenergy.com.au/.well-known/jwks.json
   ```
   *(This loads a static JSON document on the `id.originenergy.com.au` domain so Chrome/Firefox displays the cookie store for that origin.)*
3. Open DevTools (`F12` or `Cmd+Option+I`) → **Application** tab (Chrome/Edge) or **Storage** tab (Firefox) → **Cookies** → `https://id.originenergy.com.au`.
4. Locate the cookie named **`auth0`** (do **not** copy `auth0_compat`).
5. **If your account has Multi-Factor Authentication (MFA) enabled:**
   - Auth0 also requires the **`auth0-mf`** (and `did`) cookie on `id.originenergy.com.au` to prove MFA was completed.
   - The easiest way to copy all required cookies at once is via the **Network** tab:
     1. On your Origin My Account dashboard (`www.originenergy.com.au`), open DevTools → **Network** tab.
     2. Filter by `id.originenergy.com.au` and reload the page.
     3. Click the `/authorize` request → **Headers** → **Request Headers** → copy the entire **`Cookie`** header value (which contains `auth0=...; auth0-mf=...; did=...`).
   - Or, from **Application → Cookies → `https://id.originenergy.com.au`**, format both cookies as:
     ```text
     auth0=s%3A...; auth0-mf=s%3A...
     ```
6. Paste that value into Home Assistant's Origin Energy configuration dialog.

