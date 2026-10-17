// ESP32-2432S028 (CYD) as a slow USB microSD card reader/writer.
//
// Exposes the card in the board's microSD slot as raw 512-byte sectors over
// USB serial, driven by tools/cyd_sd.py. SD_BAUD 8N1.
//
//   PC  -> ESP   "SDC" | cmd | uint32 lba | uint16 count | payload     (little endian)
//   ESP -> PC    "sdr" | 'O' | payload        on success
//                "sdr" | 'E' | error code     on failure
//
// Every command names its own sectors, so the PC may send the next write
// while the previous one is still being stored, and may repeat any of them.
//
//   cmd 'I'  info         -> card type (1), sector count (4). Also (re)initialises the card.
//       'R'  read         -> count*512 bytes, crc32
//       'W'  write        <- count*512 bytes, crc32. Written, read back and compared.
//       'D'  write deflated <- uint16 n, n bytes of zlib data, crc32 of the inflated data. As 'W'.
//       'Z'  write zeros  count sectors, nothing transferred. Read back and checked.
//       'C'  checksum     -> crc32 of count sectors
//       'T'  link test    <- count*512 bytes, crc32. Checked and discarded; the card is not touched.

#include <Arduino.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <SD.h>
#include "sd_diskio.h"
extern "C" {
#include "ff.h"
#include "diskio.h"
#include "esp32/rom/miniz.h"
}

#ifndef SD_BAUD
#define SD_BAUD 1500000  // 2 Mbaud corrupts data in the ESP32->PC direction on the CH340
#endif

#define SD_CS 5
#define SD_MOSI 23
#define SD_MISO 19
#define SD_SCK 18
#ifndef SD_HZ
#define SD_HZ 16000000
#endif

enum : uint8_t {
  ERR_NO_CARD = 1,
  ERR_BAD_ARGS = 2,
  ERR_RX_TIMEOUT = 3,
  ERR_RX_CRC = 4,
  ERR_WRITE = 5,
  ERR_READ = 6,
  ERR_VERIFY = 7,
};

static const uint16_t MAX_XFER = 64;    // sectors per 'R'/'W'
static const uint16_t MAX_BULK = 8192;  // sectors per 'Z'/'C'

static TFT_eSPI tft;
static SPIClass sdSpi(VSPI);
static uint8_t pdrv = 0xFF;
static uint32_t sectors = 0;
static uint8_t buf[MAX_XFER * 512];
static uint8_t packed[MAX_XFER * 512];
static tinfl_decompressor inflator;

static uint32_t crc32(const uint8_t *p, size_t n, uint32_t crc = 0) {
  static const uint32_t t[16] = {0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC, 0x76DC4190, 0x6B6B51F4,
                                 0x4DB26158, 0x5005713C, 0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C,
                                 0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C};
  crc = ~crc;
  while (n--) {
    crc ^= *p++;
    crc = t[crc & 15] ^ (crc >> 4);
    crc = t[crc & 15] ^ (crc >> 4);
  }
  return ~crc;
}

static void showStatus(const char *line) {
  static uint32_t last = 0;
  static char shown[40] = "";
  if (!strcmp(line, shown) || (last && millis() - last < 250)) return;
  last = millis();
  strlcpy(shown, line, sizeof(shown));
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextDatum(MC_DATUM);
  tft.setTextPadding(320);
  tft.drawString(line, 160, 130, 2);
}

static void showProgress(const char *op, uint32_t lba) {
  char line[40];
  snprintf(line, sizeof(line), "%s %lu MB", op, (unsigned long)(lba / 2048));
  showStatus(line);
}

static bool cardInit() {
  if (pdrv != 0xFF) {
    sdcard_uninit(pdrv);
    pdrv = 0xFF;
  }
  sectors = 0;
  pdrv = sdcard_init(SD_CS, &sdSpi, SD_HZ);
  if (pdrv == 0xFF) return false;
  if (disk_initialize(pdrv) & STA_NOINIT) {
    sdcard_uninit(pdrv);
    pdrv = 0xFF;
    return false;
  }
  sectors = sdcard_num_sectors(pdrv);
  return sectors > 0;
}

static void replyOk(const uint8_t *payload = nullptr, size_t n = 0) {
  Serial.write((const uint8_t *)"sdrO", 4);
  if (n) Serial.write(payload, n);
}

static void replyErr(uint8_t code) {
  const uint8_t r[5] = {'s', 'd', 'r', 'E', code};
  Serial.write(r, 5);
}

static void put32(uint8_t *p, uint32_t v) {
  p[0] = v;
  p[1] = v >> 8;
  p[2] = v >> 16;
  p[3] = v >> 24;
}

static uint32_t get32(const uint8_t *p) {
  return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24);
}

static bool seekMagic() {
  static const char magic[3] = {'S', 'D', 'C'};
  static uint8_t matched = 0;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == magic[matched]) {
      if (++matched == sizeof(magic)) {
        matched = 0;
        return true;
      }
    } else {
      matched = (c == magic[0]) ? 1 : 0;
    }
  }
  return false;
}

static void handleCommand() {
  uint8_t h[7];
  if (Serial.readBytes(h, 7) != 7) return;
  uint8_t cmd = h[0];
  uint32_t lba = get32(h + 1);
  uint16_t count = h[5] | (h[6] << 8);
  size_t bytes = (size_t)count * 512;
  uint8_t out[5];

  if (cmd == 'I') {
    if (!cardInit()) {
      showStatus("No card found");
      return replyErr(ERR_NO_CARD);
    }
    char line[40];
    snprintf(line, sizeof(line), "Card: %lu MB", (unsigned long)(sectors / 2048));
    showStatus(line);
    out[0] = (uint8_t)sdcard_type(pdrv);
    put32(out + 1, sectors);
    return replyOk(out, 5);
  }

  bool bulk = (cmd == 'Z' || cmd == 'C');
  bool known = bulk || cmd == 'R' || cmd == 'W' || cmd == 'D';
  bool badArgs = !known || count == 0 || count > (bulk ? MAX_BULK : MAX_XFER);

  // A write's payload is always consumed so a rejected command can't desync the link.
  uint32_t want = 0;
  if ((cmd == 'W' || cmd == 'T') && count && count <= MAX_XFER) {
    uint8_t tail[4];
    if (Serial.readBytes(buf, bytes) != bytes || Serial.readBytes(tail, 4) != 4) return replyErr(ERR_RX_TIMEOUT);
    want = get32(tail);
    if (crc32(buf, bytes) != want) return replyErr(ERR_RX_CRC);
    if (cmd == 'T') return replyOk();
  }

  if (cmd == 'D') {
    uint8_t n2[2], tail[4];
    if (Serial.readBytes(n2, 2) != 2) return replyErr(ERR_RX_TIMEOUT);
    size_t n = n2[0] | (n2[1] << 8);
    if (n > sizeof(packed)) return replyErr(ERR_BAD_ARGS);
    if (Serial.readBytes(packed, n) != n || Serial.readBytes(tail, 4) != 4) return replyErr(ERR_RX_TIMEOUT);
    if (badArgs) return replyErr(ERR_BAD_ARGS);
    want = get32(tail);
    size_t inLen = n, outLen = bytes;
    tinfl_init(&inflator);
    tinfl_status st = tinfl_decompress(&inflator, packed, &inLen, buf, buf, &outLen,
                                       TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
    if (st != TINFL_STATUS_DONE || outLen != bytes || crc32(buf, bytes) != want) return replyErr(ERR_RX_CRC);
    cmd = 'W';
  }

  if (badArgs) return replyErr(ERR_BAD_ARGS);
  if (pdrv == 0xFF) return replyErr(ERR_NO_CARD);
  if (lba >= sectors || count > sectors - lba) return replyErr(ERR_BAD_ARGS);

  switch (cmd) {
    case 'R':
      if (disk_read(pdrv, buf, lba, count) != RES_OK) return replyErr(ERR_READ);
      showProgress("Reading", lba);
      replyOk(buf, bytes);
      put32(out, crc32(buf, bytes));
      Serial.write(out, 4);
      return;

    case 'W':
      if (disk_write(pdrv, buf, lba, count) != RES_OK) return replyErr(ERR_WRITE);
      if (disk_read(pdrv, buf, lba, count) != RES_OK) return replyErr(ERR_READ);
      if (crc32(buf, bytes) != want) return replyErr(ERR_VERIFY);
      showProgress("Writing", lba);
      return replyOk();

    case 'Z':
      while (count) {
        uint16_t n = count < MAX_XFER ? count : MAX_XFER;
        size_t len = (size_t)n * 512;
        memset(buf, 0, len);
        if (disk_write(pdrv, buf, lba, n) != RES_OK) return replyErr(ERR_WRITE);
        if (disk_read(pdrv, buf, lba, n) != RES_OK) return replyErr(ERR_READ);
        for (size_t i = 0; i < len; i++) {
          if (buf[i]) return replyErr(ERR_VERIFY);
        }
        lba += n;
        count -= n;
      }
      showProgress("Writing", lba);
      return replyOk();

    case 'C': {
      uint32_t crc = 0;
      while (count) {
        uint16_t n = count < MAX_XFER ? count : MAX_XFER;
        if (disk_read(pdrv, buf, lba, n) != RES_OK) return replyErr(ERR_READ);
        crc = crc32(buf, (size_t)n * 512, crc);
        lba += n;
        count -= n;
      }
      showProgress("Checking", lba);
      put32(out, crc);
      return replyOk(out, 4);
    }
  }
}

void setup() {
  Serial.setRxBufferSize(65536);  // room for a whole queued write
  Serial.begin(SD_BAUD);
  Serial.setTimeout(2000);

  tft.init();
  tft.setRotation(1);
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextDatum(MC_DATUM);
  tft.drawString("SD card bridge", 160, 100, 2);

  sdSpi.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
  showStatus("Waiting for the PC...");
}

void loop() {
  if (seekMagic()) handleCommand();
}
