#!/usr/bin/env python3
"""Minimal Prometheus exporter for a Shelly Pro EM (2-channel energy meter).

Stdlib only - no pip dependencies. Queries the device's Gen2 RPC API
directly (EM1.GetStatus / EM1Data.GetStatus per channel) on every scrape -
the device is on the LAN and responds fast, so there's no need to cache
like the weather exporter does for its rate-limited remote API.
"""

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOSTNAME = os.environ["SHELLY_EM_HOSTNAME"]
PORT = int(os.environ.get("SHELLY_EM_PORT", "9926"))
CHANNELS = (0, 1)

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


def rpc(method: str, channel: int) -> dict:
    url = f"http://{HOSTNAME}/rpc/{method}?id={channel}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)


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
