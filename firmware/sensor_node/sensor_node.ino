/**
 * Nest Fan Optimizer — ESP32-C3 Sensor Node
 * ==========================================
 * Reads a DHT11 temperature/humidity sensor and POSTs a JSON
 * heartbeat to the hub every INTERVAL_MS milliseconds.
 *
 * Arduino IDE setup:
 *   1. File → Preferences → Additional boards URL:
 *      https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
 *   2. Tools → Board Manager → search "esp32" → install "esp32 by Espressif"
 *   3. Tools → Board → ESP32C3 Dev Module
 *   4. Tools → USB CDC On Boot → Enabled  (so Serial works over USB)
 *
 * Libraries (Sketch → Include Library → Manage Libraries):
 *   - "DHT sensor library" by Adafruit
 *   - "Adafruit Unified Sensor" by Adafruit  (dependency)
 *
 * Wiring:
 *   DHT11 VCC  → 3.3V
 *   DHT11 GND  → GND
 *   DHT11 DATA → GPIO 2   (set DHT_PIN in config.h)
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <DHT.h>
#include "config.h"

// ── Hardware ─────────────────────────────────────────────────
#define DHTTYPE     DHT11
#define LED_PIN     8       // onboard LED on ESP32-C3 Supermini (active LOW)

DHT dht(DHT_PIN, DHTTYPE);

// ── Helpers ───────────────────────────────────────────────────
void blink(int times, int ms = 150) {
  for (int i = 0; i < times; i++) {
    digitalWrite(LED_PIN, LOW);   // active low
    delay(ms);
    digitalWrite(LED_PIN, HIGH);
    delay(ms);
  }
}

bool connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;

  Serial.printf("Connecting to %s", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  for (int i = 0; i < 30; i++) {   // up to 15 s
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("\nConnected! IP: %s\n", WiFi.localIP().toString().c_str());
      blink(3);   // triple-blink = WiFi OK
      return true;
    }
    delay(500);
    Serial.print(".");
  }

  Serial.println("\nWiFi FAILED");
  blink(10, 80);   // rapid blink = error
  return false;
}

// ── Heartbeat ─────────────────────────────────────────────────
void sendHeartbeat() {
  // Read sensor (DHT11 needs ~1 s between reads)
  float temp_f   = dht.readTemperature(/*Fahrenheit=*/true);
  float temp_c   = dht.readTemperature(/*Fahrenheit=*/false);
  float humidity = dht.readHumidity();

  if (isnan(temp_f) || isnan(humidity)) {
    Serial.println("Sensor read failed — skipping heartbeat");
    blink(5, 80);
    return;
  }

  // Build JSON payload
  String body = String("{")
    + "\"location\":\"" + LOCATION + "\","
    + "\"temp_f\":"     + String(temp_f,   1) + ","
    + "\"temp_c\":"     + String(temp_c,   1) + ","
    + "\"humidity\":"   + String(humidity, 0)
    + "}";

  Serial.printf("[%s] Sending: %s\n", LOCATION, body.c_str());

  HTTPClient http;
  http.begin(HUB_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(10000);

  int code = http.POST(body);

  if (code == 200) {
    Serial.printf("OK (HTTP %d)\n", code);
    blink(1);   // single blink = success
  } else {
    Serial.printf("Error (HTTP %d)\n", code);
    blink(5, 80);
  }

  http.end();
}

// ── Arduino lifecycle ─────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);   // LED off (active low)

  Serial.printf("\nSensor node — location: %s\n", LOCATION);
  Serial.printf("Hub: %s\n", HUB_URL);

  dht.begin();

  if (!connectWifi()) {
    Serial.println("Rebooting in 30s...");
    delay(30000);
    ESP.restart();
  }

  // Send first heartbeat immediately on boot
  sendHeartbeat();
}

void loop() {
  if (!connectWifi()) {
    delay(10000);
    return;
  }

  delay(INTERVAL_MS);
  sendHeartbeat();
}
