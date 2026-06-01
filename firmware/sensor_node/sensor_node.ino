/**
 * Nest Fan Optimizer — ESP32-C3 Sensor Node (Deep Sleep)
 * =======================================================
 * On each wake cycle:
 *   1. Connect to WiFi
 *   2. Read DHT11
 *   3. POST heartbeat to hub
 *   4. Deep sleep for INTERVAL_MS
 *
 * Deep sleep draws ~5µA vs ~80mA when idle — dramatically extends
 * battery life for sensors without a permanent power source.
 *
 * Arduino IDE setup:
 *   Tools → Board     → ESP32C3 Dev Module
 *   Tools → USB CDC On Boot → Enabled   (for Serial over USB)
 *
 * Libraries (Library Manager):
 *   - "DHT sensor library" by Adafruit
 *   - "Adafruit Unified Sensor" by Adafruit
 *
 * Wiring:
 *   DHT11 VCC  → 3.3V
 *   DHT11 GND  → GND
 *   DHT11 DATA → GPIO pin set in config.h (default: 2)
 *
 * Battery wiring:
 *   Battery (+) → 3.3V pin  (supply must be 2.7–3.6V)
 *   Battery (-) → GND pin
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <DHT.h>
#include "esp_sleep.h"
#include "config.h"

#define DHTTYPE  DHT11
#define LED_PIN  8      // onboard LED, active LOW on C3 Supermini

DHT dht(DHT_PIN, DHTTYPE);

// ── Helpers ───────────────────────────────────────────────────

void blink(int times, int ms = 120) {
  for (int i = 0; i < times; i++) {
    digitalWrite(LED_PIN, LOW);
    delay(ms);
    digitalWrite(LED_PIN, HIGH);
    delay(ms);
  }
}

bool connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("Connecting to %s", WIFI_SSID);
  for (int i = 0; i < 30; i++) {     // 15 s timeout
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("\nConnected: %s\n", WiFi.localIP().toString().c_str());
      blink(3);
      return true;
    }
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi timeout");
  return false;
}

void sendHeartbeat(float temp_f, float temp_c, float humidity) {
  String body = String("{")
    + "\"location\":\"" + LOCATION      + "\","
    + "\"temp_f\":"     + String(temp_f,   1) + ","
    + "\"temp_c\":"     + String(temp_c,   1) + ","
    + "\"humidity\":"   + String(humidity, 0)
    + "}";

  Serial.printf("[%s] %s\n", LOCATION, body.c_str());

  HTTPClient http;
  http.begin(HUB_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(8000);
  int code = http.POST(body);
  Serial.printf("HTTP %d\n", code);
  http.end();

  blink(code == 200 ? 1 : 5, code == 200 ? 120 : 60);
}

void goToSleep() {
  Serial.printf("Sleeping %d s...\n", INTERVAL_MS / 1000);
  Serial.flush();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  esp_sleep_enable_timer_wakeup((uint64_t)INTERVAL_MS * 1000ULL);  // µs
  esp_deep_sleep_start();
  // execution never reaches here
}

// ── Main — runs once per wake cycle ───────────────────────────

void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);   // LED off

  Serial.printf("\n=== Wake: %s ===\n", LOCATION);

  dht.begin();
  delay(1000);   // DHT11 needs ~1 s to stabilise after power-up

  // Read sensor (up to 5 attempts)
  float temp_f = NAN, temp_c = NAN, humidity = NAN;
  for (int i = 0; i < 5 && (isnan(temp_f) || isnan(humidity)); i++) {
    temp_c   = dht.readTemperature();
    temp_f   = dht.readTemperature(true);
    humidity = dht.readHumidity();
    if (isnan(temp_f)) delay(2000);
  }

  if (isnan(temp_f) || isnan(humidity)) {
    Serial.println("Sensor read failed — sleeping until next cycle");
    blink(5, 80);
    goToSleep();
    return;
  }

  if (!connectWifi()) {
    Serial.println("WiFi failed — sleeping until next cycle");
    blink(10, 80);
    goToSleep();
    return;
  }

  sendHeartbeat(temp_f, temp_c, humidity);
  goToSleep();
}

void loop() {
  // Never runs — deep sleep re-enters setup() on each wake
}
