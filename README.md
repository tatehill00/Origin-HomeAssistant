# Origin Energy — Home Assistant Custom Integration

A custom Home Assistant integration for **Origin Energy (Australia)** that pulls electricity usage, costs, tariff rates, billing cycle totals, and metered hot water data directly from Origin's Kraken-backed GraphQL gateway (`api.rx.originenergy.com.au/v1/gateway/graphql`).

## Features

1. **Home Assistant Energy Dashboard Integration (Long-Term Statistics)**
   - Automatically imports hourly (or daily) **electricity consumption (`kWh`)** and **cost (`$`)** directly into Home Assistant's Recorder external statistics (`origin_energy:<agreement>_energy_consumption` and `origin_energy:<agreement>_energy_cost`).
   - Supports both standard retail (`ELECTRICITY`) and embedded network (`EMBEDDED_ELECTRICITY`) accounts.
   - Automatically imports **Solar feed-in export (`kWh`) & earnings (`$`)**, **Hot Water consumption (`L`)**, and **Gas consumption (`kWh`) & cost (`$`)** into external statistics when present on your account.
   - Performs an automatic background backfill up to 2 years (or `service.validFrom`) on first setup, plus a 5-day rolling overlap window on every poll to capture late meter revisions.

2. **Live Tariff, Billing Cycle, Projection, Yesterday, & Water/Gas Sensors**
   - `sensor.origin_energy_yesterday_electricity_usage` (`kWh` used on the most recent full day)
   - `sensor.origin_energy_yesterday_electricity_cost` (`$` cost for the most recent full day)
   - `sensor.origin_energy_billing_cycle_electricity_usage` (`kWh` used in the current billing period)
   - `sensor.origin_energy_billing_cycle_electricity_cost` (`$` accrued in the current billing period, with supply vs. usage charge breakdown attributes)
   - `sensor.origin_energy_projected_bill_cost` (`$` projected full-cycle bill total based on your current daily burn rate and supply charges)
   - `sensor.origin_energy_days_until_next_bill` (`days` countdown to your next bill date)
   - `sensor.origin_energy_next_bill_date` (timestamp of your next scheduled bill)
   - `sensor.origin_energy_account_balance` (`$` current account balance and due charges)
   - `sensor.origin_energy_electricity_tariff_rate` (`$/kWh` active tariff rate, plus full time-of-use/step rate schedule in attributes)
   - `sensor.origin_energy_daily_supply_charge` (`$` daily standing supply charge)
   - `sensor.origin_energy_solar_feed_in_tariff` (`$/kWh` solar export rate, created when solar is present or configured)
   - `sensor.origin_energy_hot_water_meter_reading` (`L` cumulative meter reading and tariff rates when an `EMBEDDED_HOT_WATER` service is present)
   - `sensor.origin_energy_hot_water_billing_cycle_cost` (`$` hot water charges in the current billing cycle)
   - `sensor.origin_energy_billing_cycle_gas_usage` (`MJ` gas usage in the current billing cycle when a `GAS` service is present)
   - `sensor.origin_energy_last_update` (timestamp of last successful poll)

3. **Action Buttons, Tariff Overrides, & Diagnostics**
   - `button.origin_energy_refresh_usage_now` — trigger an immediate poll from the device page.
   - `button.origin_energy_re_run_history_backfill` (and `origin_energy.backfill_history` service) — trigger a full historical backfill and cost recalculation.
   - **Manual Tariff Overrides** in the integration's **Configure** dialog (`Override electricity usage rate`, `Override daily supply charge`, `Override solar feed-in tariff`) for embedded networks or custom plans.
   - **Download Diagnostics** support (`diagnostics.py`) with automatic redaction of cookies, agreement IDs, NMIs, and account numbers.

4. **Self-Renewing Auth0 Session**
   - Uses your browser's `auth0` and `did` SSO cookies from `id.originenergy.com.au` to perform silent OAuth PKCE exchanges (`/authorize?prompt=none`) and persists the rolled cookies on each poll.

## Installation

### Option 1: Manual Copy
Copy the `custom_components/origin_energy` folder into your Home Assistant `config/custom_components/` directory and restart Home Assistant.

### Option 2: HACS (Custom Repository)
Add this repository URL as a Custom Repository (Integration) in HACS, install **Origin Energy**, and restart Home Assistant.

## Setup

### Option A: Automated Browser Login + Direct Push to Home Assistant (No Cookie Copying!)

Instead of opening DevTools and copying cookies manually, use `tools/login_helper.py`. It opens a browser window for you to log into Origin Energy normally, captures the `auth0` session automatically the moment you sign in, validates it against Origin's GraphQL API, and pushes it straight into Home Assistant using your **Long-Lived Access Token (LLAT)**:

```bash
pip install playwright aiohttp yarl
playwright install chromium

export HA_URL="http://homeassistant.local:8123"
export HA_TOKEN="your_long_lived_access_token"
python3 tools/login_helper.py
```
*(If you omit `HA_URL` or `HA_TOKEN`, the script will prompt you interactively.)*

What `login_helper.py` does automatically in Home Assistant:
- **First-time setup:** Starts and completes the `origin_energy` Config Flow via the HA REST API (`/api/config/config_entries/flow`).
- **Pending Re-auth:** Detects any open `reauth_confirm` flow and completes it automatically.
- **Already Configured:** Calls the `origin_energy.set_session_cookie` service to update the stored session cookie and reload the integration in place.

### Option B: Manual Setup via Browser Cookie

1. Follow [docs/session-cookie.md](docs/session-cookie.md) to grab your `auth0` cookie from `id.originenergy.com.au`.
2. In Home Assistant, go to **Settings → Devices & Services → Add Integration** and search for **Origin Energy**.
3. Paste your `auth0` session cookie.

### Configuring the Home Assistant Energy Dashboard
In **Settings → Dashboards → Energy → Electricity grid**, add:
- **Grid consumption:** `Origin Energy consumption` (`origin_energy:<agreement_id>_energy_consumption`)
- **Use an entity tracking the total costs:** `Origin Energy cost` (`origin_energy:<agreement_id>_energy_cost`)

## Local Testing

You can test your cookie and inspect your live account services, tariffs, billing cycle totals, and hot water meters from your terminal before installing in Home Assistant:

```bash
python3 tools/test_connection.py
```

