#!/usr/bin/env python3
"""Minimal Prometheus exporter for a Shelly Pro EM (2-channel energy meter).

Stdlib only - no pip dependencies. Queries the device's Gen2 RPC API
directly (EM1.GetStatus / EM1Data.GetStatus per channel) on every scrape -
the device is on the LAN and responds fast, so there's no need to cache
like the weather exporter does for its rate-limited remote API.

Also tracks running electricity cost (both channels combined) using a
simple time-of-use rate: a free window (e.g. 9pm-midnight) and a flat
rate otherwise, applied in local time (the container shares the host's
/etc/localtime, so this follows NZ time including DST automatically).
Cost tracking starts from whenever this exporter first runs, not
retroactively over the device's lifetime energy total. State persists to
a JSON file so a container restart doesn't lose the running total.
"""

import datetime
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOSTNAME = os.environ["SHELLY_EM_HOSTNAME"]
PORT = int(os.environ.get("SHELLY_EM_PORT", "9926"))
CHANNELS = (0, 1)

RATE_DOLLARS_PER_KWH = float(os.environ.get("SHELLY_EM_RATE_DOLLARS_PER_KWH", "0"))
FREE_HOUR_START = int(os.environ.get("SHELLY_EM_FREE_HOUR_START", "-1"))
FREE_HOUR_END = int(os.environ.get("SHELLY_EM_FREE_HOUR_END", "-1"))
STATE_FILE = os.environ.get("SHELLY_EM_STATE_FILE", "/state/cost_state.json")

INSTANT_METRICS = {
    "voltage": ("shelly_em_voltage_volts", "AC voltage"),
    "current": ("shelly_em_current_amps", "AC current"),
    "act_power": ("shelly_em_active_power_watts", "Real power"),
    "aprt_power": ("shelly_em_apparent_power_va", "Apparent power"),
    "pf": ("shelly_em_power_factor", "Power factor"),
    "freq": ("shelly_em_frequency_hz", "AC frequency"),
}

ENERGY_METRICS = {
    "total_act_energy": ("shelly_em_energy_watt_hours_total", "Cumulative active energy"),
    "total_act_ret_energy": ("shelly_em_energy_returned_watt_hours_total", "Cumulative returned (exported) active energy"),
}

_state_lock = threading.Lock()


def rpc(method: str, channel: int) -> dict:
    url = f"http://{HOSTNAME}/rpc/{method}?id={channel}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)


def current_rate() -> float:
    if FREE_HOUR_START <= FREE_HOUR_END and FREE_HOUR_START <= datetime.datetime.now().hour < FREE_HOUR_END:
        return 0.0
    return RATE_DOLLARS_PER_KWH


DEFAULT_STATE = {
    "baseline_energy_wh": None,
    "cost_dollars_total": 0.0,
    "cost_dollars_today": 0.0,
    "today_key": None,
    "cost_dollars_month": 0.0,
    "month_key": None,
}


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return {**DEFAULT_STATE, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f)
    os.replace(tmp_path, STATE_FILE)


def update_cost(total_energy_wh: float) -> dict:
    """Update the persisted running cost totals given the latest combined
    energy reading, and return the updated state."""
    with _state_lock:
        state = load_state()
        now = datetime.datetime.now()
        today_key = now.date().isoformat()
        month_key = f"{now.year:04d}-{now.month:02d}"

        if state["today_key"] != today_key:
            state["cost_dollars_today"] = 0.0
            state["today_key"] = today_key
        if state["month_key"] != month_key:
            state["cost_dollars_month"] = 0.0
            state["month_key"] = month_key

        baseline = state["baseline_energy_wh"]
        if baseline is None:
            state["baseline_energy_wh"] = total_energy_wh
            save_state(state)
            return state

        delta_wh = total_energy_wh - baseline
        if delta_wh < 0:
            # Device counter reset/rebooted - start a fresh baseline rather
            # than let the running totals go backwards.
            state["baseline_energy_wh"] = total_energy_wh
            save_state(state)
            return state

        delta_cost = (delta_wh / 1000.0) * current_rate()
        state["cost_dollars_total"] += delta_cost
        state["cost_dollars_today"] += delta_cost
        state["cost_dollars_month"] += delta_cost
        state["baseline_energy_wh"] = total_energy_wh
        save_state(state)
        return state


def fetch_metrics() -> str:
    lines = []
    metric_values = {name: [] for name, _ in {**INSTANT_METRICS, **ENERGY_METRICS}.values()}
    success = 1
    for channel in CHANNELS:
        try:
            status = rpc("EM1.GetStatus", channel)
            for key, (name, _) in INSTANT_METRICS.items():
                if key in status:
                    metric_values[name].append((channel, status[key]))
            data = rpc("EM1Data.GetStatus", channel)
            for key, (name, _) in ENERGY_METRICS.items():
                if key in data:
                    metric_values[name].append((channel, data[key]))
        except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
            success = 0
            print(f"shelly-em exporter: channel {channel} fetch failed: {exc}", flush=True)

    for key, (name, help_text) in {**INSTANT_METRICS, **ENERGY_METRICS}.items():
        values = metric_values[name]
        if not values:
            continue
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for channel, value in values:
            lines.append(f'{name}{{channel="{channel}"}} {value}')

    energy_values = metric_values["shelly_em_energy_watt_hours_total"]
    if energy_values:
        combined_energy_wh = sum(value for _, value in energy_values)
        state = update_cost(combined_energy_wh)
        lines.append("# HELP shelly_em_cost_dollars_total Running electricity cost since this exporter started tracking, both channels combined")
        lines.append("# TYPE shelly_em_cost_dollars_total counter")
        lines.append(f"shelly_em_cost_dollars_total {state['cost_dollars_total']}")
        lines.append("# HELP shelly_em_cost_dollars_today Electricity cost so far today (local time), resets at midnight")
        lines.append("# TYPE shelly_em_cost_dollars_today gauge")
        lines.append(f"shelly_em_cost_dollars_today {state['cost_dollars_today']}")
        lines.append("# HELP shelly_em_cost_dollars_month Electricity cost so far this month (local time), resets on the 1st")
        lines.append("# TYPE shelly_em_cost_dollars_month gauge")
        lines.append(f"shelly_em_cost_dollars_month {state['cost_dollars_month']}")
        lines.append("# HELP shelly_em_electricity_rate_dollars_per_kwh Currently effective electricity rate")
        lines.append("# TYPE shelly_em_electricity_rate_dollars_per_kwh gauge")
        lines.append(f"shelly_em_electricity_rate_dollars_per_kwh {current_rate()}")

    lines.append("# HELP shelly_em_exporter_scrape_success Whether all channel fetches succeeded")
    lines.append("# TYPE shelly_em_exporter_scrape_success gauge")
    lines.append(f"shelly_em_exporter_scrape_success {success}")
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = fetch_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        del format, args


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"shelly-em exporter listening on :{PORT}, querying {HOSTNAME}", flush=True)
    server.serve_forever()
