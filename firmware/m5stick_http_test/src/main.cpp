// Minimal M5StickS3 connectivity test: join the phone hotspot, treat
// WiFi.gatewayIP() as the phone's own address, and GET
// http://<gateway>:8000/ (a `python -m http.server 8000 --bind 0.0.0.0`
// running on the phone). Runs once in setup(); loop() just idles.
//
// This is a standalone diagnostic, deliberately not wired into
// bridge_server.py / the WebSocket protocol the other firmware/ projects use.
#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "secrets.h"

static const uint16_t HTTP_PORT = 8000;
static const uint32_t WIFI_TIMEOUT_MS = 20000;

static int line = 0;
static void screenLine(const String &text, uint16_t color = TFT_WHITE) {
  M5.Display.setTextColor(color, TFT_BLACK);
  M5.Display.setCursor(0, line * 18);
  M5.Display.println(text);
  line++;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[boot] m5stick_http_test");

  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(1);
  M5.Display.setTextSize(2);
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setCursor(0, 0);
  M5.Display.println("connecting...");

  Serial.printf("connecting to SSID: %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_TIMEOUT_MS) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();

  M5.Display.fillScreen(TFT_BLACK);
  line = 0;

  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("WIFI: FAILED (WiFi.status()=%d)\n", WiFi.status());
    screenLine("WIFI: FAIL", TFT_RED);
    screenLine("code:" + String(WiFi.status()), TFT_RED);
    return;  // nothing else to test without Wi-Fi
  }

  IPAddress ip = WiFi.localIP();
  IPAddress mask = WiFi.subnetMask();
  IPAddress gw = WiFi.gatewayIP();
  int rssi = WiFi.RSSI();

  Serial.println("WIFI: OK");
  Serial.printf("Local IP: %s\n", ip.toString().c_str());
  Serial.printf("Subnet:   %s\n", mask.toString().c_str());
  Serial.printf("Gateway:  %s  (treated as the phone's own IP)\n", gw.toString().c_str());
  Serial.printf("RSSI:     %d dBm\n", rssi);

  screenLine("WIFI: OK", TFT_GREEN);
  screenLine("IP: " + ip.toString());
  screenLine("PHONE: " + gw.toString());
  screenLine("RSSI: " + String(rssi));

  // ---- raw TCP connect, decoupled from HTTP semantics ----
  Serial.println("---- raw TCP test ----");
  WiFiClient tcp;
  bool tcpOk = tcp.connect(gw, HTTP_PORT, 5000);
  Serial.printf("TCP connect to %s:%u -> %s\n", gw.toString().c_str(), HTTP_PORT,
                tcpOk ? "SUCCESS" : "FAILED");
  if (tcpOk) tcp.stop();

  // ---- HTTP GET ----
  String url = "http://" + gw.toString() + ":" + String(HTTP_PORT) + "/";
  Serial.println("---- HTTP TEST ----");
  Serial.printf("Target URL:      %s\n", url.c_str());
  Serial.printf("Destination IP:  %s\n", gw.toString().c_str());

  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(5000);

  if (!http.begin(url)) {
    Serial.println("http.begin() failed to parse/prepare the request");
    screenLine("HTTP: begin() fail", TFT_RED);
    return;
  }

  int code = http.GET();
  Serial.printf("HTTP GET result code: %d\n", code);

  if (code > 0) {
    String body = http.getString();
    Serial.printf("HTTP status: %d\n", code);
    Serial.println("---- first 500 chars of body ----");
    Serial.println(body.substring(0, 500));
    Serial.println("---- end body ----");
    screenLine("HTTP: " + String(code), code == 200 ? TFT_GREEN : TFT_YELLOW);
  } else {
    String err = http.errorToString(code);
    Serial.printf("HTTP error: %s (code %d)\n", err.c_str(), code);
    screenLine("HTTP FAIL", TFT_RED);
    screenLine(err, TFT_RED);
  }
  http.end();

  // ---- diagnostic hints, printed to Serial only ----
  Serial.println("---- diagnosis ----");
  if (!tcpOk) {
    Serial.println("Raw TCP connect failed -> the phone likely never saw a SYN reach the");
    Serial.println("Python server at all. Most likely causes, in order of likelihood:");
    Serial.println("  1. Phone hotspot client isolation is on (blocks device-to-device");
    Serial.println("     traffic even though both are on the same hotspot) — check the");
    Serial.println("     hotspot's advanced settings for 'client isolation' / 'AP isolation'.");
    Serial.println("  2. `python -m http.server 8000 --bind 0.0.0.0` isn't actually running,");
    Serial.println("     or is bound to 127.0.0.1 instead of 0.0.0.0.");
    Serial.println("  3. Android is blocking incoming connections on port 8000 (some Android");
    Serial.println("     versions firewall inbound connections to apps not explicitly serving).");
    Serial.println("  4. WiFi.gatewayIP() isn't actually the phone's own address — check it");
    Serial.println("     against what the phone's hotspot settings screen shows as its own IP.");
  } else if (code <= 0) {
    Serial.println("TCP connected fine but the HTTP layer failed — the port is open and");
    Serial.println("reachable, so this points at the Python server itself (crashed mid-");
    Serial.println("request, or something non-HTTP is listening on 8000) rather than the");
    Serial.println("network path.");
  } else {
    Serial.println("Full success: Wi-Fi, TCP, and HTTP all worked.");
  }
}

void loop() {
  delay(1000);
}
