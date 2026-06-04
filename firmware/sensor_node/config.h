#pragma once

// ─────────────────────────────────────────────────────────────
// Sensor Node Config — edit this file per board before flashing
// ─────────────────────────────────────────────────────────────

// WiFi credentials
#define WIFI_SSID      "REDACTED_WIFI_SSID"
#define WIFI_PASSWORD  "REDACTED_WIFI_PASSWORD"

// Unique name for this sensor's location.
// Examples: "basement", "main_floor", "upstairs", "office"
#define LOCATION       "basement: gracie"

// Your Mac's local IP address and hub port.
// Find your Mac's IP: System Settings → Wi-Fi → Details
// Or run in Terminal:  ipconfig getifaddr en0
#define HUB_URL        "http://10.0.0.216:5001/sensor"

// How often to send a reading (milliseconds). 300000 = 5 minutes.
#define INTERVAL_MS    300000

// GPIO pin the DHT11 DATA wire is connected to
#define DHT_PIN        2
