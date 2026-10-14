#!/usr/bin/env python3
"""Read and write the microSD card in a CYD running the SD bridge firmware.

    python3 tools/cyd_sd.py info                  # card details and a Raspberry Pi boot check
    python3 tools/cyd_sd.py write image.img.xz    # write an OS image (erases the card)
    python3 tools/cyd_sd.py setup                 # set the user, Wi-Fi and SSH on a Raspberry Pi OS card

See firmware/src/sdcard.cpp for the link protocol.
"""
import argparse
import getpass
import hashlib
import json
import lzma
import os
import re
import shutil
import subprocess
import struct
import sys
import tempfile
import time
import zlib

import serial

CHUNK = 64  # sectors per write; must not exceed MAX_XFER in the firmware
READ_CHUNK = 16  # long ESP32->PC bursts can lose bytes on the CH340; short ones arrive intact
WINDOW = 2  # writes in flight: one being stored while the next is on the wire
ERRORS = {
    1: "no card found in the CYD's slot",
    2: "bad arguments (outside the card?)",
    3: "the ESP32 timed out waiting for data",
    4: "data was corrupted on the USB link",
    5: "the card rejected a write",
    6: "the card failed a read",
    7: "data read back from the card did not match what was written",
}
CARD_TYPES = {0: "none", 1: "MMC", 2: "SD", 3: "SDHC/SDXC"}


class BridgeError(Exception):
    def __init__(self, code):
        super().__init__(ERRORS.get(code, f"error {code}"))
        self.code = code


class Bridge:
    def __init__(self, port, baud):
        self.port = serial.Serial(port, baud, timeout=5)
        time.sleep(1.5)  # opening the port can reset the ESP32

    def _send(self, cmd, lba=0, count=0, payload=b""):
        self.port.write(b"SDC" + cmd + struct.pack("<IH", lba, count) + payload)

    def _recv(self, reply=0, timeout=5):
        self.port.timeout = timeout
        seen = b""
        while not seen.endswith(b"sdr"):
            c = self.port.read(1)
            if not c:
                raise TimeoutError("no reply from the CYD; is the SD bridge firmware flashed?")
            seen = (seen + c)[-3:]
        status = self.port.read(1)
        if status == b"E":
            raise BridgeError(self.port.read(1)[0])
        data = self.port.read(reply)
        if status != b"O" or len(data) != reply:
            raise TimeoutError("truncated reply from the CYD")
        return data

    def _command(self, cmd, lba=0, count=0, payload=b"", reply=0, timeout=5):
        self.port.reset_input_buffer()
        self._send(cmd, lba, count, payload)
        return self._recv(reply, timeout)

    def info(self):
        kind, sectors = struct.unpack("<BI", self._command(b"I", reply=5, timeout=10))
        return kind, sectors

    def read(self, lba, count=1):
        out = b""
        while count:
            n = min(count, READ_CHUNK)
            for attempt in range(6):
                try:
                    data = self._command(b"R", lba, n, reply=n * 512 + 4, timeout=1)
                except TimeoutError:
                    continue
                if zlib.crc32(data[:-4]) == struct.unpack("<I", data[-4:])[0]:
                    break
            else:
                raise IOError(f"sector {lba}: data kept arriving corrupted")
            out += data[:-4]
            lba += n
            count -= n
        return out

    def checksum(self, lba, count):
        return struct.unpack("<I", self._command(b"C", lba, count, reply=4, timeout=60))[0]

    def write_all(self, chunks, progress=lambda lba, sent: None):
        """Writes an iterable of (lba, data) chunks, keeping WINDOW commands in flight."""
        pending, self.retries = [], 0

        def packet(lba, data):
            count = len(data) // 512
            if data.count(0) == len(data):
                return b"Z", lba, count, b""
            crc = struct.pack("<I", zlib.crc32(data))
            small = zlib.compress(data, 6)
            if len(small) < len(data) * 0.9:
                return b"D", lba, count, struct.pack("<H", len(small)) + small + crc
            return b"W", lba, count, data + crc

        def settle():
            pkt = pending[0]
            try:
                self._recv(timeout=20)
            except (BridgeError, TimeoutError) as err:
                if isinstance(err, BridgeError) and err.code in (1, 2):
                    raise
                # Let the ESP32 finish or abandon whatever it has, then redo everything unconfirmed.
                for pkt in pending:
                    for attempt in range(5):
                        self.retries += 1
                        time.sleep(3)
                        try:
                            self._command(*pkt, timeout=20)
                            break
                        except (BridgeError, TimeoutError) as again:
                            last = again
                    else:
                        raise IOError(f"sector {pkt[1]}: giving up after repeated failures ({last})")
                    progress(pkt[1] + pkt[2], len(pkt[3]))
                pending.clear()
                return
            pending.pop(0)
            progress(pkt[1] + pkt[2], len(pkt[3]))

        self.port.reset_input_buffer()
        for lba, data in chunks:
            pkt = packet(lba, data)
            self._send(*pkt)
            pending.append(pkt)
            while len(pending) >= WINDOW:
                settle()
        while pending:
            settle()


def size_str(sectors):
    return f"{sectors * 512 / 1e9:.1f} GB"


def fat_root(read, start):
    """Lists the root directory of a FAT16/FAT32 volume as {NAME: size}."""
    bpb = read(start)
    bps, spc, reserved, fats, root_entries = struct.unpack_from("<HBHBH", bpb, 11)
    fat_size = struct.unpack_from("<H", bpb, 22)[0] or struct.unpack_from("<I", bpb, 36)[0]
    if bpb[510:512] != b"\x55\xaa" or bps != 512 or spc == 0 or fats == 0:
        return None
    fat_start = start + reserved
    root_sectors = (root_entries * 32 + 511) // 512
    data_start = fat_start + fats * fat_size + root_sectors

    if root_entries:  # FAT16: fixed root directory
        raw = read(fat_start + fats * fat_size, root_sectors)
    else:  # FAT32: the root is a cluster chain
        raw, cluster, hops = b"", struct.unpack_from("<I", bpb, 44)[0], 0
        while 2 <= cluster < 0x0FFFFFF8 and hops < 64:
            raw += read(data_start + (cluster - 2) * spc, spc)
            entry = read(fat_start + cluster * 4 // 512)
            cluster = struct.unpack_from("<I", entry, cluster * 4 % 512)[0] & 0x0FFFFFFF
            hops += 1

    files = {}
    for i in range(0, len(raw), 32):
        e = raw[i : i + 32]
        if e[0] == 0:
            break
        if e[0] == 0xE5 or e[11] & 0x08:  # deleted, volume label or long-name entry
            continue
        name = e[0:8].decode("latin-1").rstrip()
        ext = e[8:11].decode("latin-1").rstrip()
        full = (name + "." + ext if ext else name) + ("/" if e[11] & 0x10 else "")
        files[full] = struct.unpack_from("<I", e, 28)[0]
    return files


def cmd_info(bridge, args):
    kind, sectors = bridge.info()
    print(f"Card: {CARD_TYPES.get(kind, kind)}, {size_str(sectors)} ({sectors} sectors)")

    mbr = bridge.read(0)
    if mbr[510:512] != b"\x55\xaa":
        blank = "all zeros" if not any(mbr) else "no partition table signature"
        print(f"Sector 0: {blank}. The card has no partition table, so it is blank or wiped.")
        return
    parts = []
    for i in range(4):
        e = mbr[446 + i * 16 : 462 + i * 16]
        ptype, start, count = e[4], *struct.unpack_from("<II", e, 8)
        if ptype and count:
            parts.append((i + 1, ptype, start, count))
            print(f"Partition {i + 1}: type 0x{ptype:02x}, starts at sector {start}, {size_str(count)}")
    if not parts:
        print("Partition table is present but empty.")
        return

    for num, ptype, start, count in parts:
        if start + count > sectors:
            print(f"Partition {num} runs past the end of the card: the image is for a larger card or is corrupt.")
        if ptype == 0x83:
            sb = bridge.read(start + 2, 2)
            ok = sb[56:58] == b"\x53\xef"
            print(f"Partition {num}: {'valid ext4 signature' if ok else 'NO ext4 signature (damaged)'}")

    fat = next((p for p in parts if p[1] in (0x0B, 0x0C, 0x0E, 0x06)), None)
    if not fat:
        print("No FAT boot partition found: a Pi 3 B cannot boot from this card.")
        return
    files = fat_root(bridge.read, fat[2])
    if files is None:
        print(f"Partition {fat[0]} is marked FAT but has no valid filesystem: the boot partition is corrupt.")
        return
    print(f"Boot partition has {len(files)} entries in its root folder.")
    if args.list:
        for name, size in sorted(files.items()):
            print(f"  {name:<14}{size:>10}")

    print("Raspberry Pi 3 B boot files:")
    missing = []
    for wanted in ("BOOTCODE.BIN", "START.ELF", "FIXUP.DAT", "CONFIG.TXT", "CMDLINE.TXT"):
        size = files.get(wanted)
        note = "missing" if size is None else ("EMPTY" if size == 0 else f"{size} bytes")
        print(f"  {wanted:<14}{note}")
        if not size:
            missing.append(wanted)
    kernels = [n for n in files if n.startswith("KERNEL") and n.endswith(".IMG") and files[n]]
    print(f"  {'kernel':<14}{', '.join(sorted(kernels)) or 'missing'}")
    if not kernels:
        missing.append("kernel")
    if missing:
        print(f"Verdict: not bootable; {', '.join(missing)} missing or empty.")
    else:
        print("Verdict: the boot files a Pi 3 B needs are all present.")


def image_chunks(src, sectors, start):
    lba = 0
    while True:
        data = src.read(CHUNK * 512)
        if not data:
            return
        if len(data) % 512:
            data += bytes(512 - len(data) % 512)
        if lba + len(data) // 512 > sectors:
            raise SystemExit("\nThe image is larger than the card.")
        if lba >= start:
            yield lba, data
        lba += len(data) // 512


def cmd_write(bridge, args):
    kind, sectors = bridge.info()
    print(f"Card: {CARD_TYPES.get(kind, kind)}, {size_str(sectors)}")
    if not args.yes:
        if input("This ERASES the card. Type 'yes' to continue: ").strip() != "yes":
            return
    began, sent = time.monotonic(), 0

    def progress(lba, nbytes):
        nonlocal sent
        sent += nbytes
        elapsed = time.monotonic() - began
        done = (lba - args.start) * 512
        print(
            f"\r{lba * 512 / 1e6:8.1f} MB on card, {done / 1e3 / max(elapsed, 0.1):4.0f} kB/s"
            f" ({sent / 1e3 / max(elapsed, 0.1):3.0f} on the wire), {elapsed / 60:5.1f} min, {bridge.retries} retries ",
            end="",
            flush=True,
        )

    src = lzma.open(args.image) if args.image.endswith(".xz") else open(args.image, "rb")
    with src:
        bridge.write_all(image_chunks(src, sectors, args.start), progress)
    print("\nDone: every block was read back from the card and matched.")


USER_GROUPS = "users,adm,dialout,audio,netdev,video,plugdev,cdrom,games,input,gpio,spi,i2c,render,sudo"


def ask_settings():
    """Asks for the first-boot settings here, in the terminal, so secrets stay on this machine."""

    def ask(prompt, default=None, secret=False, pattern=None, rule=""):
        while True:
            label = f"{prompt} [{default}]: " if default else f"{prompt}: "
            value = (getpass.getpass(label) if secret else input(label)).strip() or default
            if value and (not pattern or re.fullmatch(pattern, value)):
                return value
            if value:
                print(rule)

    s = {
        "user": ask(
            "Username for the Pi (lowercase)",
            "pi",
            pattern=r"[a-z_][a-z0-9_-]{0,31}",
            rule="Linux usernames must be lowercase letters, digits, - or _, and start with a letter.",
        )
    }
    while True:
        s["password"] = ask("Password for that user (hidden)", secret=True)
        if getpass.getpass("Repeat the password: ").strip() == s["password"]:
            break
        print("Those didn't match.")
    s["hostname"] = ask(
        "Hostname", "raspberrypi", pattern=r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", rule="Letters, digits and - only."
    )
    s["ssid"] = ask("Wi-Fi network name")
    s["wifi_password"] = ask("Wi-Fi password (hidden)", secret=True)
    s["country"] = ask("Wi-Fi country code, two letters (US, GB, DE...)", pattern=r"[A-Za-z]{2}", rule="Two letters.").upper()
    try:
        zone = os.path.realpath("/etc/localtime").split("/zoneinfo/")[1]
    except IndexError:
        zone = "Etc/UTC"
    s["timezone"] = ask("Timezone", zone)
    return s


def cloud_init_files(s):
    """Builds the cloud-init files Raspberry Pi OS (Trixie and later) reads from its boot partition."""
    q = json.dumps  # a JSON string is also a valid YAML string
    crypted = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"], input=s["password"] + "\n", text=True, capture_output=True, check=True
    ).stdout.strip()
    psk = hashlib.pbkdf2_hmac("sha1", s["wifi_password"].encode(), s["ssid"].encode(), 4096, 32).hex()
    user_data = f"""#cloud-config
hostname: {q(s["hostname"])}
manage_etc_hosts: true
timezone: {q(s["timezone"])}
users:
- name: {q(s["user"])}
  groups: {USER_GROUPS}
  shell: /bin/bash
  lock_passwd: false
  passwd: {q(crypted)}
enable_ssh: true
ssh_pwauth: true
runcmd:
- [ rfkill, unblock, wifi ]
- [ raspi-config, nonint, do_wifi_country, {q(s["country"])} ]
- [ systemctl, enable, --now, ssh ]
"""
    network_config = f"""network:
  version: 2
  wifis:
    wlan0:
      dhcp4: true
      optional: true
      regulatory-domain: {q(s["country"])}
      access-points:
        {q(s["ssid"])}:
          password: {q(psk)}
"""
    # A new instance id makes cloud-init apply the settings even if the card has already been booted.
    meta_data = f"dsmode: local\ninstance_id: cyd-setup-{int(time.time())}\n"
    return {"user-data": user_data, "network-config": network_config, "meta-data": meta_data, "ssh": ""}


def load_boot_volume(bridge, path):
    """Makes a sparse local copy of the card's FAT32 boot partition holding only its
    file tables and root folder, which is all mtools needs to add files to it.
    Returns the partition's start sector."""
    mbr = bridge.read(0)
    ptype, start, count = mbr[450], *struct.unpack_from("<II", mbr, 454)
    if mbr[510:512] != b"\x55\xaa" or ptype not in (0x0B, 0x0C):
        raise SystemExit("The card's first partition is not a FAT32 boot partition. Write an OS image first.")
    bpb = bridge.read(start)
    bps, spc, reserved, fats, root_entries = struct.unpack_from("<HBHBH", bpb, 11)
    fat_size = struct.unpack_from("<I", bpb, 36)[0]
    if bpb[510:512] != b"\x55\xaa" or bps != 512 or root_entries or not spc or not fat_size:
        raise SystemExit("The card's boot partition is not a valid FAT32 filesystem.")
    meta = reserved + fats * fat_size
    print("Reading the card's file tables...")
    head = bridge.read(start, meta)
    fat = head[reserved * 512 : (reserved + fat_size) * 512]
    with open(path, "wb") as f:
        f.truncate(count * 512)
        f.write(head)
        cluster, hops = struct.unpack_from("<I", bpb, 44)[0], 0
        while 2 <= cluster < 0x0FFFFFF8 and hops < 1024:
            offset = meta + (cluster - 2) * spc
            f.seek(offset * 512)
            f.write(bridge.read(start + offset, spc))
            cluster = struct.unpack_from("<I", fat, cluster * 4)[0] & 0x0FFFFFFF
            hops += 1
    return start


def changed_runs(before, after, base):
    """Yields (lba, data) for each run of sectors that differs between two volume files."""
    changed = {}
    with open(before, "rb") as a, open(after, "rb") as b:
        sector = 0
        while True:
            old, new = a.read(1 << 20), b.read(1 << 20)
            if not new:
                break
            if old != new:
                for i in range(0, len(new), 512):
                    if old[i : i + 512] != new[i : i + 512]:
                        changed[sector + i // 512] = new[i : i + 512]
            sector += len(new) // 512
    run = []
    for sector in sorted(changed):
        if run and (sector != run[-1] + 1 or len(run) == CHUNK):
            yield base + run[0], b"".join(changed[n] for n in run)
            run = []
        run.append(sector)
    if run:
        yield base + run[0], b"".join(changed[n] for n in run)


def cmd_setup(bridge, args):
    for tool in ("mcopy", "openssl"):
        if not shutil.which(tool):
            raise SystemExit(f"'{tool}' is needed for this (packages: mtools, openssl).")
    bridge.info()
    with tempfile.TemporaryDirectory() as tmp:
        before, after = os.path.join(tmp, "before.img"), os.path.join(tmp, "after.img")
        start = load_boot_volume(bridge, before)
        files = cloud_init_files(ask_settings())
        shutil.copyfile(before, after)
        for name, text in files.items():
            path = os.path.join(tmp, name)
            with open(path, "w", newline="\n") as f:
                f.write(text)
            subprocess.run(["mcopy", "-o", "-i", after, path, "::" + name], check=True)
        # Only sectors mtools touched are written, so files changed on the card since imaging are left alone.
        runs = list(changed_runs(before, after, start))
        bridge.write_all(runs)
        print(f"Done: {', '.join(files)} written to the card's boot partition and verified.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=1500000, help="must match SD_BAUD in the firmware")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("info", help="show card details and check Raspberry Pi boot files")
    p.add_argument("--list", action="store_true", help="list every file in the boot partition's root")
    p.set_defaults(func=cmd_info)
    p = sub.add_parser("write", help="write an .img or .img.xz to the card, erasing it")
    p.add_argument("image")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--start", type=int, default=0, metavar="SECTOR", help="resume an interrupted write from this sector")
    p.set_defaults(func=cmd_write)
    p = sub.add_parser("setup", help="set the user, Wi-Fi and SSH on a card holding Raspberry Pi OS (Trixie or later)")
    p.set_defaults(func=cmd_setup)
    args = ap.parse_args()

    try:
        args.func(Bridge(args.port, args.baud), args)
    except (BridgeError, TimeoutError, IOError) as err:
        sys.exit(f"error: {err}")


if __name__ == "__main__":
    main()
