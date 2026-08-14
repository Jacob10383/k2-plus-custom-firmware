#!/usr/bin/env python3
# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.

import hashlib
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.request
import binascii
from contextlib import contextmanager

BASE_URL = "https://firmware.jacobean.xyz"
FIRMWARE_VERSION = "6.18"
RELEASE_INDEX_URL = f"{BASE_URL}/{FIRMWARE_VERSION}/index"
TELEMETRY_URL = f"{BASE_URL}/telemetry/install"
TELEMETRY_TIMEOUT = 2
# Stable K2 hardware IDs used only to derive a pseudonymous install hash.
# The raw eMMC CID and MAC addresses are never included in telemetry payloads.
TELEMETRY_ID_PATHS = (
    "/sys/block/mmcblk0/device/cid",
    "/sys/class/net/eth0/address",
    "/sys/class/net/wlan0/address",
)

ROOTFS_SHA256 = "ded633761c625a5cfdc673f2d9b9165874131990d21783e35c6eaa0882f863c7"
KERNEL_SHA256 = "d0244555154bc2498e80ecf746198f0bfea3e698695058b0e2f3a483081222f0"
SWAP_SHA256 = "9a94a8b7457fa42d403d323642688c07bc63120e4280c6086e80edc34c738d97"
HELIX_VERSION = "v0.99.112"
HELIX_ARCHIVE = f"helixscreen-k2-{HELIX_VERSION}.tar.gz"
HELIX_URL = (
    "https://github.com/prestonbrown/helixscreen/releases/download/"
    f"{HELIX_VERSION}/{HELIX_ARCHIVE}"
)
HELIX_SHA256 = "17fcccc233fcf84254fb745d7819b0ecfc17d9ce373a52ba7de2ad6a5a1c61ed"


def object_url(digest):
    return f"{BASE_URL}/{FIRMWARE_VERSION}/o/{digest}"


ROOTFS_URL = object_url(ROOTFS_SHA256)
KERNEL_URL = object_url(KERNEL_SHA256)
SWAP_URL = object_url(SWAP_SHA256)

ROOTFS_A = "/dev/mmcblk0p6"
ROOTFS_B = "/dev/mmcblk0p7"
BOOT_A = "/dev/mmcblk0p4"
BOOT_B = "/dev/mmcblk0p5"
ANDROID_BOOT_MAGIC = b"ANDROID!"
EXT_SUPERBLOCK_MAGIC_OFFSET = 1024 + 56
EXT_SUPERBLOCK_MAGIC = b"\x53\xef"

STAGING_DIR = "/mnt/UDISK/.install"
SWAP_INSTALL_PATH = "/usr/bin/swap"
UDISK = "/mnt/UDISK"
CUSTOM_ROOTFS_SENTINEL = "/etc/.k2_custom"
CUSTOM_KERNEL_MARKER = "-k2jt"
SLOTS_DIR = os.path.join(UDISK, ".slots")
CUSTOM_SLOT_DIR = os.path.join(SLOTS_DIR, "custom")
CUSTOM_HELIX_DIR = os.path.join(CUSTOM_SLOT_DIR, "helixscreen")
NETWORK_ATTEMPTS = 3
NETWORK_DELAY_SECONDS = 3
INSTALL_SPACE_MARGIN = 64 * 1024 * 1024
HELIX_PRESERVE_TOP_LEVEL = {
    "cache",
    "custom",
    "custom_images",
    "data",
    "logs",
    "state",
    "themes",
    "user",
    "user_themes",
    "userdata",
}

ENV_SIZE = 0x20000
ENV_DEVICES = {
    "p2": "/dev/mmcblk0p2",
    "p3": "/dev/mmcblk0p3",
}
PARTITION_PAIRS = {
    "A": {"root": "/dev/mmcblk0p6", "boot_env": "bootA", "root_env": "rootfsA"},
    "B": {"root": "/dev/mmcblk0p7", "boot_env": "bootB", "root_env": "rootfsB"},
}
EXPECTED_CMDLINE_PARTITIONS = (
    "bootA@mmcblk0p4",
    "bootB@mmcblk0p5",
    "rootfsA@mmcblk0p6",
    "rootfsB@mmcblk0p7",
    "UDISK@mmcblk0p14",
)

_current_step = "preflight"
_telemetry_context = {}
_telemetry_failure_done = False


def _fmt(seconds):
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def header(message):
    print(f"\033[1m{message}\033[0m")
    print(f"\033[36m{'=' * len(message)}\033[0m", flush=True)


def section(message):
    print(f"\n\033[1;36m==> {message}\033[0m", flush=True)


def log(message):
    print(f"  {message}", flush=True)


def log_warn(message):
    print(f"  \033[33m{message}\033[0m", flush=True)


def _telemetry_id_value(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        value = f.read().strip().lower()
    compact = value.replace(":", "").replace("-", "")
    if compact.startswith("0x"):
        compact = compact[2:]
    if not compact or set(compact) == {"0"}:
        return None
    return value


def telemetry_install_id():
    parts = []
    try:
        for path in TELEMETRY_ID_PATHS:
            try:
                value = _telemetry_id_value(path)
            except OSError:
                continue
            if value:
                parts.append(f"{path}={value}")
        if not parts:
            return None
        return hashlib.sha256(("k2cf-install-id-v1|" + "|".join(parts)).encode()).hexdigest()
    except BaseException:
        return None


def send_telemetry(event, **fields):
    try:
        if "--no-telemetry" in sys.argv[1:]:
            return
        payload = {
            "event": event,
            "version": FIRMWARE_VERSION,
            "step": _current_step,
            **_telemetry_context,
        }
        install_id = telemetry_install_id()
        if install_id:
            payload["install_id"] = install_id
        payload.update(fields)
        data = json.dumps(payload, sort_keys=True).encode()
        request = urllib.request.Request(
            TELEMETRY_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TELEMETRY_TIMEOUT):
            pass
    except BaseException:
        pass


def send_failure_telemetry(**fields):
    global _telemetry_failure_done
    if _telemetry_failure_done:
        return
    _telemetry_failure_done = True
    send_telemetry("install_error", **fields)


@contextmanager
def step(title):
    global _current_step
    previous_step = _current_step
    _current_step = title
    section(title)
    try:
        yield
    finally:
        _current_step = previous_step


def success(message):
    print(f"\n\033[1;32m{message}\033[0m", flush=True)


def die(msg):
    global _telemetry_failure_done
    if str(msg) == "install cancelled":
        _telemetry_failure_done = True
    else:
        send_failure_telemetry(error_kind="controlled", message=str(msg))
    print(f"\n\033[1;31mERROR:\033[0m {msg}")
    sys.exit(1)


def retry_network(label, action, attempts=NETWORK_ATTEMPTS, delay=NETWORK_DELAY_SECONDS):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            last_exc = exc
            if attempt == attempts:
                break
            log_warn(
                f"{label} failed ({type(exc).__name__}: {exc}); "
                f"retrying in {delay}s ({attempt}/{attempts - 1})"
            )
            time.sleep(delay)
    raise last_exc


def check_root():
    if os.geteuid() != 0:
        die("Must run as root")


def ignore_sighup():
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, OSError, ValueError):
        pass


def check_stock():
    with open("/proc/mounts") as f:
        for line in f:
            parts = line.split()
            if parts[1] == "/":
                if parts[2] != "overlay":
                    die(
                        f"Root filesystem is '{parts[2]}' — does not look like stock firmware"
                    )
                break

    rootfs_custom = os.path.exists(CUSTOM_ROOTFS_SENTINEL)
    kernel_custom = CUSTOM_KERNEL_MARKER in os.uname().release
    if rootfs_custom != kernel_custom:
        die("Kernel/rootfs mismatch detected; refusing to run full installer")
    if rootfs_custom:
        die("Already running Jacobean's firmware; run the full installer from stock firmware")

    if not os.path.isdir("/rom"):
        die("/rom not found — does not look like stock firmware")


def check_udisk():
    if not os.path.isdir(UDISK):
        die(f"{UDISK} does not exist")
    if not os.path.ismount(UDISK):
        die(f"{UDISK} is not a mountpoint")
    probe = os.path.join(UDISK, ".install-write-test")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok\n")
            f.flush()
            os.fsync(f.fileno())
        os.remove(probe)
    except OSError as exc:
        die(f"{UDISK} is not writable: {exc}")


def get_partitions():
    with open("/proc/cmdline") as f:
        cmdline = f.read()

    if "root=/dev/mmcblk0p6" in cmdline:
        active_slot = "A"
        target_rootfs = ROOTFS_B
        target_boot = BOOT_B
        target_slot = "B"
    elif "root=/dev/mmcblk0p7" in cmdline:
        active_slot = "B"
        target_rootfs = ROOTFS_A
        target_boot = BOOT_A
        target_slot = "A"
    else:
        die("Cannot determine active partition from /proc/cmdline")

    return active_slot, target_slot, target_rootfs, target_boot


def read_fw_env(key):
    result = subprocess.run(
        ["fw_printenv", key],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    prefix = f"{key}="
    if not line.startswith(prefix):
        return None
    return line[len(prefix):]


def _boot_contract_error(cmdline, active_slot, boot_partition, root_partition):
    missing = [token for token in EXPECTED_CMDLINE_PARTITIONS if token not in cmdline]
    if missing:
        return "Unexpected K2 partition map; missing " + ", ".join(missing)

    pair = PARTITION_PAIRS[active_slot]
    if boot_partition != pair["boot_env"] or root_partition != pair["root_env"]:
        return (
            f"U-Boot env does not match active rootfs{active_slot}: "
            f"expected {pair['boot_env']}/{pair['root_env']}, "
            f"got {boot_partition or '?'}/{root_partition or '?'}"
        )
    return None


def check_boot_contract(active_slot):
    with open("/proc/cmdline") as f:
        cmdline = f.read()
    error = _boot_contract_error(
        cmdline,
        active_slot,
        read_fw_env("boot_partition"),
        read_fw_env("root_partition"),
    )
    if error:
        die(error)


def prepare_staging_dir():
    if os.path.exists(STAGING_DIR):
        log(f"removing stale {STAGING_DIR}")
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
    os.makedirs(STAGING_DIR, exist_ok=True)


def remote_size(url, label):
    def attempt():
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request) as response:
            total = int(response.headers.get("Content-Length", "0") or "0")
            if total <= 0:
                raise ValueError(f"{label} response did not include Content-Length")
            return total

    try:
        return retry_network(f"checking {label} size", attempt)
    except Exception as exc:
        die(f"failed to check {label} size: {exc}")


def check_staging_space():
    sizes = [
        remote_size(ROOTFS_URL, "root file system"),
        remote_size(KERNEL_URL, "kernel"),
        remote_size(SWAP_URL, "swap utility"),
        remote_size(HELIX_URL, "HelixScreen"),
    ]
    required = sum(sizes) + INSTALL_SPACE_MARGIN
    free = shutil.disk_usage(UDISK).free
    if free < required:
        die(
            f"{UDISK} has {free / 1024 / 1024:.1f} MB free; "
            f"need {(required) / 1024 / 1024:.1f} MB for installer staging"
        )


def download_sha256(url, dest, label, expected_sha256):
    if not expected_sha256:
        die(f"{label} has no expected SHA256 configured")

    def attempt():
        with urllib.request.urlopen(url) as response, open(dest, "wb") as out:
            total = int(response.headers.get("Content-Length", "0") or "0")
            downloaded = 0
            start = time.monotonic()
            show_eta = total >= 1024 * 1024
            sha256 = hashlib.sha256()
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                sha256.update(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = min(100, downloaded * 100 // total)
                    done = downloaded / 1024 / 1024
                    total_mb = total / 1024 / 1024
                    elapsed = max(time.monotonic() - start, 1e-6)
                    rate = downloaded / elapsed
                    line = (
                        f"\r\033[K  {pct:3d}%  {done:.1f}/{total_mb:.1f} MB  "
                        f"{rate / 1024 / 1024:.1f} MB/s"
                    )
                    if show_eta:
                        eta = (total - downloaded) / rate if rate > 0 else 0
                        line += f"  ETA {_fmt(eta)}"
                    print(line, end="", flush=True)

            if total > 0:
                elapsed = max(time.monotonic() - start, 1e-6)
                rate = downloaded / elapsed if downloaded else 0
                done = downloaded / 1024 / 1024
                total_mb = total / 1024 / 1024
                line = (
                    f"\r\033[K  100%  {done:.1f}/{total_mb:.1f} MB  "
                    f"{rate / 1024 / 1024:.1f} MB/s"
                )
                if show_eta:
                    line += "  ETA 0.0s"
                print(line, flush=True)
            else:
                log(f"downloaded {label}")

        actual_sha256 = sha256.hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"{label} checksum mismatch "
                f"(expected {expected_sha256}, got {actual_sha256})"
            )

    try:
        retry_network(f"downloading {label}", attempt)
    except Exception as exc:
        try:
            os.remove(dest)
        except OSError:
            pass
        die(f"failed to download {label}: {exc}")


def fetch_json(url, label):
    def attempt():
        with urllib.request.urlopen(url) as response:
            return json.load(response)

    try:
        return retry_network(f"fetching {label}", attempt)
    except Exception as exc:
        die(f"failed to fetch {label}: {exc}")


def _validate_index_entry(index, key, expected_sha256):
    entry = index.get(key)
    if not isinstance(entry, dict):
        die(f"release index missing {key!r} entry")
    digest = entry.get("sha256")
    if digest != expected_sha256:
        die(f"release index {key} SHA mismatch: expected {expected_sha256}, got {digest}")
    size = entry.get("size")
    if not isinstance(size, int) or size <= 0:
        die(f"release index {key} has invalid size")


def check_release_index():
    index = fetch_json(RELEASE_INDEX_URL, "release index")
    if not isinstance(index, dict):
        die("release index did not contain an object")
    if index.get("format") != "k2-release-index-v1" or index.get("version") != FIRMWARE_VERSION:
        die("release index has an unsupported format")
    _validate_index_entry(index, "rootfs", ROOTFS_SHA256)
    _validate_index_entry(index, "kernel", KERNEL_SHA256)
    _validate_index_entry(index, "swap", SWAP_SHA256)
    return index


def flash(image, partition):
    result = subprocess.run(
        ["dd", f"if={image}", f"of={partition}", "bs=4M", "conv=fsync"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        die(f"Flash failed:\n{result.stdout}{result.stderr}")


def validate_rootfs_image(path):
    try:
        with open(path, "rb") as f:
            f.seek(EXT_SUPERBLOCK_MAGIC_OFFSET)
            magic = f.read(2)
    except OSError as exc:
        die(f"could not inspect rootfs image: {exc}")
    if magic != EXT_SUPERBLOCK_MAGIC:
        die(f"{path} does not look like a raw ext filesystem image")


def validate_android_boot_image(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        die(f"could not inspect kernel image: {exc}")
    header = data[:2048]
    if not header.startswith(ANDROID_BOOT_MAGIC):
        die(f"{path} does not look like an Android boot image")
    if len(header) < 44:
        die(f"{path} has a truncated Android boot header")
    kernel_size = struct.unpack_from("<I", header, 8)[0]
    page_size = struct.unpack_from("<I", header, 36)[0]
    image_size = len(data)
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        die(f"{path} has invalid Android boot page size {page_size}")
    if kernel_size <= 0 or image_size < page_size + kernel_size:
        die(f"{path} has invalid Android boot kernel size {kernel_size}")
    kernel = data[page_size:page_size + kernel_size]
    if b"\xd0\x0d\xfe\xed" not in kernel:
        die(f"{path} has no appended mainline DTB in the kernel payload")


def install_swap(script_path):
    shutil.copy2(script_path, SWAP_INSTALL_PATH)
    os.chmod(SWAP_INSTALL_PATH, 0o755)
    subprocess.run(["sync"], check=True)


def _safe_extract_tar(archive_path, dest_dir, label):
    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tf:
        members = tf.getmembers()
        for member in members:
            target = os.path.abspath(os.path.join(dest_dir, member.name))
            if os.path.commonpath([dest_dir, target]) != dest_dir:
                die(f"{label} archive contains unsafe path {member.name!r}")
            if not (member.isdir() or member.isfile() or member.issym()):
                die(f"{label} archive contains unsupported entry {member.name!r}")
            if member.issym():
                link_target = os.path.abspath(
                    os.path.join(os.path.dirname(target), member.linkname)
                )
                if os.path.commonpath([dest_dir, link_target]) != dest_dir:
                    die(f"{label} archive contains unsafe symlink {member.name!r}")
        tf.extractall(dest_dir, members)


def _release_dir_from_extract(extract_dir):
    entries = [
        name for name in os.listdir(extract_dir)
        if name not in {".DS_Store", "__MACOSX"}
    ]
    if len(entries) == 1:
        candidate = os.path.join(extract_dir, entries[0])
        if os.path.isdir(candidate):
            return candidate
    return extract_dir


def _validate_helix_release(release_dir):
    required = [
        os.path.join(release_dir, "bin", "helix-screen"),
        os.path.join(release_dir, "bin", "helix-launcher.sh"),
    ]
    for path in required:
        if not os.path.isfile(path):
            die(f"HelixScreen archive is missing {path[len(release_dir) + 1:]}")
        if not os.access(path, os.X_OK):
            die(f"HelixScreen archive entry is not executable: {path}")


def _copy_path(src, dest):
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest, follow_symlinks=False)


def _preserve_existing_helix_state(existing_dir, new_dir):
    if not os.path.isdir(existing_dir):
        return

    release_top_level = set(os.listdir(new_dir))
    for name in sorted(os.listdir(existing_dir)):
        src = os.path.join(existing_dir, name)
        dest = os.path.join(new_dir, name)
        if name in HELIX_PRESERVE_TOP_LEVEL or name not in release_top_level:
            _copy_path(src, dest)

    existing_config = os.path.join(existing_dir, "config")
    new_config = os.path.join(new_dir, "config")
    if os.path.isdir(existing_config):
        os.makedirs(new_config, exist_ok=True)
        for name in sorted(os.listdir(existing_config)):
            src = os.path.join(existing_config, name)
            dest = os.path.join(new_config, name)
            _copy_path(src, dest)


def seed_custom_helix_archive(archive_path):
    extract_dir = os.path.join(STAGING_DIR, "helixscreen-extract")
    new_dir = os.path.join(CUSTOM_SLOT_DIR, ".helixscreen.new")
    old_dir = os.path.join(CUSTOM_SLOT_DIR, ".helixscreen.old")

    for path in (extract_dir, new_dir, old_dir):
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)

    _safe_extract_tar(archive_path, extract_dir, "HelixScreen")
    release_dir = _release_dir_from_extract(extract_dir)
    _validate_helix_release(release_dir)

    os.makedirs(CUSTOM_SLOT_DIR, exist_ok=True)
    shutil.copytree(release_dir, new_dir, symlinks=True)
    _preserve_existing_helix_state(CUSTOM_HELIX_DIR, new_dir)

    try:
        if os.path.exists(CUSTOM_HELIX_DIR):
            os.replace(CUSTOM_HELIX_DIR, old_dir)
        os.replace(new_dir, CUSTOM_HELIX_DIR)
    except OSError:
        if os.path.exists(CUSTOM_HELIX_DIR):
            shutil.rmtree(CUSTOM_HELIX_DIR, ignore_errors=True)
        if os.path.exists(old_dir):
            os.replace(old_dir, CUSTOM_HELIX_DIR)
        raise
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        shutil.rmtree(new_dir, ignore_errors=True)

    shutil.rmtree(old_dir, ignore_errors=True)
    subprocess.run(["sync"], check=True)
    log(f"seeded HelixScreen {HELIX_VERSION} into custom UDISK slot")


# ---------------------------------------------------------------------------
# U-Boot env helpers (minimal inline copy for writing custom env.bin)
# ---------------------------------------------------------------------------

def _env_crc(blob):
    return binascii.crc32(blob[5:]) & 0xFFFFFFFF


def _parse_env_blob(blob):
    if len(blob) != ENV_SIZE:
        raise ValueError(f"env blob is {len(blob)} bytes, expected {ENV_SIZE}")
    stored_crc = struct.unpack("<I", blob[:4])[0]
    computed_crc = _env_crc(blob)
    values = {}
    for raw in blob[5:].split(b"\0"):
        if not raw:
            break
        text = raw.decode(errors="replace")
        if "=" in text:
            k, v = text.split("=", 1)
            values[k] = v
    return {
        "valid_crc": stored_crc == computed_crc,
        "flag": blob[4],
        "values": values,
    }


def _env_entries(blob):
    entries = []
    for raw in blob[5:].split(b"\0"):
        if not raw:
            break
        text = raw.decode(errors="replace")
        if "=" in text:
            key, value = text.split("=", 1)
            entries.append((key, value))
    return entries


def _read_env_device(name):
    with open(ENV_DEVICES[name], "rb") as f:
        blob = f.read(ENV_SIZE)
    if len(blob) != ENV_SIZE:
        raise SystemExit(f"short read from {ENV_DEVICES[name]}: {len(blob)} bytes")
    return blob


def _select_current_env(infos, label):
    valid = {n: i for n, i in infos.items() if i["valid_crc"]}
    if not valid:
        raise SystemExit(f"{label} has no CRC-valid env sectors")
    if len(valid) == 1:
        name = next(iter(valid))
        return name, valid[name]
    p2 = valid["p2"]
    p3 = valid["p3"]
    if p2["flag"] == p3["flag"]:
        return "p2", p2
    diff = (p2["flag"] - p3["flag"]) & 0xFF
    return ("p2", p2) if diff < 128 else ("p3", p3)


def _build_env_blob(source_blob, updates, label):
    seen = set()
    rebuilt = bytearray()
    for key, value in _env_entries(source_blob):
        if key in updates:
            value = updates[key]
        seen.add(key)
        rebuilt.extend(f"{key}={value}".encode())
        rebuilt.append(0)

    for key, value in updates.items():
        if key not in seen:
            rebuilt.extend(f"{key}={value}".encode())
            rebuilt.append(0)

    rebuilt.append(0)
    payload_size = ENV_SIZE - 5
    if len(rebuilt) > payload_size:
        raise SystemExit(f"{label} env does not fit in {ENV_SIZE:#x} bytes")

    data = bytearray(ENV_SIZE)
    data[4] = source_blob[4]
    data[5:5 + len(rebuilt)] = rebuilt
    data[:4] = struct.pack("<I", _env_crc(data))
    return bytes(data)


def _validate_env_for_slot(info, target_slot, label):
    pair = PARTITION_PAIRS[target_slot]
    if not info["valid_crc"]:
        raise SystemExit(f"{label} env has invalid CRC")
    if info["values"].get("boot_partition") != pair["boot_env"]:
        raise SystemExit(f"{label} env has wrong boot_partition")
    if info["values"].get("root_partition") != pair["root_env"]:
        raise SystemExit(f"{label} env has wrong root_partition")


def _normalize_env_for_slot(source_blob, target_slot, label):
    target_slot = target_slot.upper()
    if target_slot not in PARTITION_PAIRS:
        die(f"unknown partition slot {target_slot}")
    source_info = _parse_env_blob(source_blob)
    if not source_info["valid_crc"]:
        raise SystemExit(f"{label} source env has invalid CRC")
    for key in ("boot_partition", "root_partition"):
        if key not in source_info["values"]:
            raise SystemExit(f"{label} source env is missing {key}")

    pair = PARTITION_PAIRS[target_slot]
    updated = _build_env_blob(
        source_blob,
        {
            "boot_partition": pair["boot_env"],
            "root_partition": pair["root_env"],
        },
        label,
    )
    info = _parse_env_blob(updated)
    _validate_env_for_slot(info, target_slot, label)
    return updated, info


def _write_blob_file(path, blob):
    with open(path, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())


def read_live_env():
    blobs = {}
    infos = {}
    for name in ENV_DEVICES:
        blob = _read_env_device(name)
        blobs[name] = blob
        infos[name] = _parse_env_blob(blob)
    current_device, current_info = _select_current_env(infos, "live env")
    return blobs, infos, current_device, current_info


def preflight_env(active_slot, target_slot):
    blobs, _infos, current_device, _current_info = read_live_env()
    custom_blob, _custom_info = _normalize_env_for_slot(
        blobs[current_device],
        target_slot,
        "custom preflight",
    )
    return custom_blob


def write_custom_env_blob(target_slot, custom_blob):
    target_slot = target_slot.upper()
    if target_slot not in PARTITION_PAIRS:
        die(f"unknown partition slot {target_slot}")
    pair = PARTITION_PAIRS[target_slot]
    info = _parse_env_blob(custom_blob)
    _validate_env_for_slot(info, target_slot, "custom snapshot")
    log(
        f"custom env snapshot targets {pair['boot_env']} / "
        f"{pair['root_env']} ({pair['root']})"
    )

    custom_dir = os.path.join(SLOTS_DIR, "custom")
    os.makedirs(custom_dir, exist_ok=True)
    _write_blob_file(os.path.join(custom_dir, "env.bin"), custom_blob)
    subprocess.run(["sync"], check=True)


def run_swap():
    subprocess.run([SWAP_INSTALL_PATH, "--no-reboot", "--yes"], check=True)


def confirm_install(active_slot, target_slot, target_rootfs, target_boot):
    print()
    firmware_version = read_fw_env("version") or "unknown"
    current_rootfs = f"rootfs{active_slot}"
    current_boot = f"boot{active_slot}"
    target_rootfs_name = f"rootfs{target_slot}"
    target_boot_name = f"boot{target_slot}"

    log(
        f"You are running Creality firmware {firmware_version} on "
        f"{current_rootfs} and {current_boot}."
    )
    log(
        f"This will download and install Jacobean's firmware on the inactive slot "
        f"({target_rootfs_name} / {target_boot_name})."
    )
    if "--no-telemetry" not in sys.argv[1:]:
        print()
        log_warn(
            "This installer uses pseudonymous install/error telemetry. "
            "Rerun installer with --no-telemetry to disable."
        )
        log_warn(
            "Telemetry is only used by this installer. "
            "The installed firmware includes no telemetry."
        )

    try:
        answer = input("\nContinue? [y/N]: ").strip().lower()
    except EOFError:
        answer = ""

    if answer not in {"y", "yes"}:
        die("install cancelled")


def shutdown_device():
    subprocess.run(["sync"], check=True)
    subprocess.run(["poweroff"], check=True)


def main():
    header("Jacobean's K2 Plus Firmware Installer")

    ignore_sighup()
    check_root()
    check_stock()
    check_udisk()
    active_slot, target_slot, target_rootfs, target_boot = get_partitions()
    check_boot_contract(active_slot)
    _telemetry_context.update({"active_slot": active_slot, "target_slot": target_slot})
    custom_blob = preflight_env(active_slot, target_slot)
    check_release_index()

    confirm_install(active_slot, target_slot, target_rootfs, target_boot)
    send_telemetry("install_started")

    prepare_staging_dir()
    check_staging_space()
    rootfs_path = os.path.join(STAGING_DIR, "rootfs.ext2")
    kernel_path = os.path.join(STAGING_DIR, "kernel.img")
    swap_path = os.path.join(STAGING_DIR, "swap")
    helix_path = os.path.join(STAGING_DIR, HELIX_ARCHIVE)

    try:
        with step("Downloading root file system"):
            download_sha256(ROOTFS_URL, rootfs_path, "root file system", ROOTFS_SHA256)
        with step("Downloading kernel.img"):
            download_sha256(KERNEL_URL, kernel_path, "kernel", KERNEL_SHA256)
        with step("Downloading swap utility"):
            download_sha256(SWAP_URL, swap_path, "swap utility", SWAP_SHA256)
        with step("Downloading HelixScreen"):
            download_sha256(HELIX_URL, helix_path, "HelixScreen", HELIX_SHA256)

        with step("Validating images"):
            validate_rootfs_image(rootfs_path)
            validate_android_boot_image(kernel_path)

        with step("Preparing custom UDISK payloads"):
            seed_custom_helix_archive(helix_path)

        with step("Flashing root file system"):
            flash(rootfs_path, target_rootfs)
        with step("Flashing kernel"):
            flash(kernel_path, target_boot)

        with step("Installing swap utility"):
            install_swap(swap_path)

        with step("Preparing custom env snapshot"):
            write_custom_env_blob(target_slot, custom_blob)

        with step("Swapping to Jacobean's firmware"):
            run_swap()
    finally:
        shutil.rmtree(STAGING_DIR, ignore_errors=True)

    success("Install complete")
    send_telemetry("install_success", step="complete")
    log("Hard power cycle your printer and run 'bootstrap'")
    time.sleep(2)
    shutdown_device()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            send_failure_telemetry(error_kind="system_exit", message=str(exc.code))
        raise
    except Exception as exc:
        send_failure_telemetry(
            error_kind="exception",
            exception_type=type(exc).__name__,
            message=str(exc),
            traceback=traceback.format_exc(),
        )
        raise
