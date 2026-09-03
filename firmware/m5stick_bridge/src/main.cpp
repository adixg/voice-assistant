// M5StickS3 push-to-talk mic/speaker bridge for bridge_server.py.
//
// Protocol over one WebSocket connection (raw PCM s16le mono @ SAMPLE_RATE
// both ways, framed with tiny text control messages):
//   BtnA held   -> client sends text "start", then binary mic chunks
//   BtnA release-> client sends text "stop"
//   server replies with text "reply:<text>" (shown on screen), then binary
//   audio chunks, then text "end" -> client plays the buffered reply.
#include <M5Unified.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include "secrets.h"

static const uint32_t SAMPLE_RATE = 16000;
static const size_t MIC_CHUNK_SAMPLES = 512;  // ~32ms/chunk

WebSocketsClient webSocket;

enum State { IDLE, RECORDING };
static State state = IDLE;
static int16_t micBuf[MIC_CHUNK_SAMPLES];

static uint8_t *replyBuf = nullptr;
static size_t replyCap = 0;
static size_t replyLen = 0;
static bool receivingReply = false;

static void ensureReplyCap(size_t need) {
  if (replyCap >= need) return;
  size_t newCap = need + 64 * 1024;
  uint8_t *nb = (uint8_t *)ps_realloc(replyBuf, newCap);
  if (nb) {
    replyBuf = nb;
    replyCap = newCap;
  }
}

static void showStatus(const String &line1, const String &line2 = "") {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setCursor(0, 0);
  M5.Display.println(line1);
  if (line2.length()) M5.Display.println(line2);
  Serial.printf("[status] %s %s\n", line1.c_str(), line2.c_str());
}

static void webSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      showStatus("connected", WS_HOST);
      break;
    case WStype_DISCONNECTED:
      showStatus("disconnected...");
      break;
    case WStype_TEXT: {
      String msg((char *)payload, length);
      if (msg.startsWith("reply:")) {
        showStatus("Rina:", msg.substring(6));
        receivingReply = true;
        replyLen = 0;
      } else if (msg == "end") {
        receivingReply = false;
        if (replyLen > 0) {
          M5.Mic.end();
          M5.Speaker.begin();
          M5.Speaker.playRaw((int16_t *)replyBuf, replyLen / 2, SAMPLE_RATE, false);
        }
        showStatus("ready", "hold BtnA to talk");
      }
      break;
    }
    case WStype_BIN:
      if (receivingReply) {
        ensureReplyCap(replyLen + length);
        memcpy(replyBuf + replyLen, payload, length);
        replyLen += length;
      }
      break;
    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[boot] m5stick_bridge starting");

  auto cfg = M5.config();
  M5.begin(cfg);

  M5.Display.setRotation(1);
  M5.Display.setTextSize(2);
  showStatus("booting...");

  {
    auto mic_cfg = M5.Mic.config();
    mic_cfg.sample_rate = SAMPLE_RATE;
    mic_cfg.stereo = false;
    M5.Mic.config(mic_cfg);

    auto spk_cfg = M5.Speaker.config();
    spk_cfg.sample_rate = SAMPLE_RATE;
    spk_cfg.stereo = false;
    M5.Speaker.config(spk_cfg);
    M5.Speaker.setVolume(255);  // M5Unified defaults to a conservative volume
  }

  replyCap = 200 * 1024;
  replyBuf = (uint8_t *)ps_malloc(replyCap);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  showStatus("wifi...", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    delay(200);
    Serial.print(".");
    M5.update();
  }
  Serial.println();
  showStatus("wifi ok", WiFi.localIP().toString());

  webSocket.begin(WS_HOST, WS_PORT, WS_PATH);
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(3000);

  M5.Mic.begin();
  showStatus("ready", "hold BtnA to talk");
}

void loop() {
  M5.update();
  webSocket.loop();

  if (state == IDLE && M5.BtnA.wasPressed()) {
    state = RECORDING;
    M5.Speaker.end();
    M5.Mic.begin();
    webSocket.sendTXT("start");
    showStatus("recording...");
  }

  if (state == RECORDING) {
    if (M5.Mic.record(micBuf, MIC_CHUNK_SAMPLES, SAMPLE_RATE)) {
      webSocket.sendBIN((uint8_t *)micBuf, MIC_CHUNK_SAMPLES * sizeof(int16_t));
    }
    if (M5.BtnA.wasReleased()) {
      state = IDLE;
      webSocket.sendTXT("stop");
      showStatus("thinking...");
    }
  }
}
