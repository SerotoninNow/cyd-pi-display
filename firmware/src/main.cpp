// ESP32-2432S028 (CYD) as a USB serial display for a Raspberry Pi.
//
// Link protocol (over the board's CH340 USB serial port, LINK_BAUD 8N1):
//
//   Pi  -> ESP   "CYDF" | uint32 length (little endian) | baseline JPEG, <= 320x240
//   ESP -> Pi    "READY\n"        once after boot
//                "K\n"            frame handled, send the next one
//                "T x y p\n"      touch at screen pixel x,y; p=1 down/move, p=0 up
//
// The Pi sends one frame and waits for "K", so the UART can never overrun
// while the ESP32 is busy decoding.

#include <Arduino.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <JPEGDEC.h>
#include <XPT2046_Touchscreen.h>

#ifndef LINK_BAUD
#define LINK_BAUD 921600
#endif

// The touch controller sits on its own SPI bus on this board.
#define TOUCH_IRQ 36
#define TOUCH_MOSI 32
#define TOUCH_MISO 39
#define TOUCH_CLK 25
#define TOUCH_CS 33

// Raw XPT2046 readings at the screen edges. Adjust if touches land off-target.
#define TOUCH_X_MIN 200
#define TOUCH_X_MAX 3700
#define TOUCH_Y_MIN 240
#define TOUCH_Y_MAX 3800

static const int SCREEN_W = 320;
static const int SCREEN_H = 240;
static const size_t MAX_FRAME = 60000;
static const char MAGIC[4] = {'C', 'Y', 'D', 'F'};

static TFT_eSPI tft;
static JPEGDEC jpeg;
static SPIClass touchSpi(VSPI);
static XPT2046_Touchscreen touch(TOUCH_CS, TOUCH_IRQ);
static uint8_t *frameBuf;

static int drawBlock(JPEGDRAW *d) {
  tft.pushImage(d->x, d->y, d->iWidth, d->iHeight, d->pPixels);
  return 1;
}

static void showStatus(const char *line) {
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextDatum(MC_DATUM);
  tft.drawString("Raspberry Pi display", SCREEN_W / 2, SCREEN_H / 2 - 14, 2);
  tft.drawString(line, SCREEN_W / 2, SCREEN_H / 2 + 10, 2);
}

// Consumes serial input until the frame magic has been seen.
static bool seekMagic() {
  static uint8_t matched = 0;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == MAGIC[matched]) {
      if (++matched == sizeof(MAGIC)) {
        matched = 0;
        return true;
      }
    } else {
      matched = (c == MAGIC[0]) ? 1 : 0;
    }
  }
  return false;
}

static void receiveFrame() {
  uint8_t hdr[4];
  if (Serial.readBytes(hdr, 4) != 4) return;
  uint32_t len = hdr[0] | (hdr[1] << 8) | (hdr[2] << 16) | ((uint32_t)hdr[3] << 24);
  // A bad length means we are out of sync; stay quiet and let the Pi time out.
  if (len == 0 || len > MAX_FRAME) return;
  if (Serial.readBytes(frameBuf, len) != len) return;

  if (jpeg.openRAM(frameBuf, (int)len, drawBlock)) {
    int x = (SCREEN_W - jpeg.getWidth()) / 2;
    int y = (SCREEN_H - jpeg.getHeight()) / 2;
    tft.startWrite();
    jpeg.decode(x < 0 ? 0 : x, y < 0 ? 0 : y, 0);
    tft.endWrite();
    jpeg.close();
  }
  Serial.print("K\n");
}

static void pollTouch() {
  static uint32_t lastPoll = 0;
  static bool down = false;
  static int lastX = 0, lastY = 0;

  if (millis() - lastPoll < 25) return;
  lastPoll = millis();

  bool pressed = touch.tirqTouched() && touch.touched();
  if (pressed) {
    TS_Point p = touch.getPoint();
    int x = constrain(map(p.x, TOUCH_X_MIN, TOUCH_X_MAX, 0, SCREEN_W - 1), 0, SCREEN_W - 1);
    int y = constrain(map(p.y, TOUCH_Y_MIN, TOUCH_Y_MAX, 0, SCREEN_H - 1), 0, SCREEN_H - 1);
    if (!down || x != lastX || y != lastY) {
      Serial.printf("T %d %d 1\n", x, y);
      lastX = x;
      lastY = y;
    }
    down = true;
  } else if (down) {
    Serial.printf("T %d %d 0\n", lastX, lastY);
    down = false;
  }
}

void setup() {
  Serial.setRxBufferSize(16384);
  Serial.begin(LINK_BAUD);
  Serial.setTimeout(300);

  frameBuf = (uint8_t *)malloc(MAX_FRAME);

  tft.init();
  tft.setRotation(1);
  tft.setSwapBytes(true);  // JPEGDEC emits little-endian RGB565

  touchSpi.begin(TOUCH_CLK, TOUCH_MISO, TOUCH_MOSI, TOUCH_CS);
  touch.begin(touchSpi);
  touch.setRotation(1);

  showStatus(frameBuf ? "Waiting for the Pi..." : "Out of memory");
  Serial.print("READY\n");
}

void loop() {
  if (frameBuf && seekMagic()) receiveFrame();
  pollTouch();
}
