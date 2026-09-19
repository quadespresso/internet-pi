#!/usr/bin/env python3
"""Minimal Prometheus exporter for Open-Meteo current conditions.

Stdlib only - no pip dependencies. Polls the free, keyless Open-Meteo
forecast API for WEATHER_LATITUDE/WEATHER_LONGITUDE and caches the result
for WEATHER_CACHE_SECONDS so repeated Prometheus scrapes don't hammer the
API.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LATITUDE = os.environ["WEATHER_LATITUDE"]
LONGITUDE = os.environ["WEATHER_LONGITUDE"]
PORT = int(os.environ.get("WEATHER_PORT", "9812"))
CACHE_SECONDS = int(os.environ.get("WEATHER_CACHE_SECONDS", "300"))

CURRENT_VARS = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "rain",
    "pressure_msl",
    "cloud_cover",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "weather_code",
]

METRIC_NAMES = {
    "temperature_2m": ("weather_temperature_celsius", "Air temperature at 2m"),
    "apparent_temperature": ("weather_apparent_temperature_celsius", "Apparent (feels-like) temperature"),
    "relative_humidity_2m": ("weather_relative_humidity_percent", "Relative humidity at 2m"),
    "precipitation": ("weather_precipitation_mm", "Total precipitation (last hour)"),
    "rain": ("weather_rain_mm", "Rain (last hour)"),
    "pressure_msl": ("weather_pressure_msl_hpa", "Mean sea level pressure"),
    "cloud_cover": ("weather_cloud_cover_percent", "Total cloud cover"),
    "wind_speed_10m": ("weather_wind_speed_kmh", "Wind speed at 10m"),
    "wind_gusts_10m": ("weather_wind_gusts_kmh", "Wind gusts at 10m"),
    "wind_direction_10m": ("weather_wind_direction_degrees", "Wind direction at 10m"),
    "weather_code": ("weather_code", "WMO weather interpretation code"),
}

FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LATITUDE}&longitude={LONGITUDE}"
    f"&current={','.join(CURRENT_VARS)}&timezone=auto"
)

_lock = threading.Lock()
_cache = {"text": "", "fetched_at": 0.0}


def fetch_metrics() -> str:
    lines = []
    try:
        with urllib.request.urlopen(FORECAST_URL, timeout=10) as resp:
            data = json.load(resp)
        current = data["current"]
        for key in CURRENT_VARS:
            if key not in current:
                continue
            name, help_text = METRIC_NAMES[key]
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {current[key]}")
        lines.append("# HELP weather_exporter_scrape_success Whether the last Open-Meteo fetch succeeded")
        lines.append("# TYPE weather_exporter_scrape_success gauge")
        lines.append("weather_exporter_scrape_success 1")
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
        lines.append("# HELP weather_exporter_scrape_success Whether the last Open-Meteo fetch succeeded")
        lines.append("# TYPE weather_exporter_scrape_success gauge")
        lines.append("weather_exporter_scrape_success 0")
        print(f"weather exporter: fetch failed: {exc}", flush=True)
    return "\n".join(lines) + "\n"


def get_metrics() -> str:
    with _lock:
        age = time.time() - _cache["fetched_at"]
        if age > CACHE_SECONDS or not _cache["text"]:
            _cache["text"] = fetch_metrics()
            _cache["fetched_at"] = time.time()
        return _cache["text"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = get_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        del format, args


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"weather exporter listening on :{PORT}, caching {CACHE_SECONDS}s", flush=True)
    server.serve_forever()
