# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Klipper integration for CFS boxes, filament state, and nozzle operations.

``Box`` owns discovery, coherent live state, persistence, RFID metadata, and
the public G-code surface, composing change sequencing through ``BoxChangeEngine``.
"""

import json
import logging
import math
import os
import random
import threading
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, replace

from extras import box_protocol
from extras.box_addr import ADDRESS_WEDGE_WARNING, MAX_ADDRESSES, AutoAddressManager
from extras.box_change import (
    UNLOAD_ESTIMATE_CAP, UNLOAD_ESTIMATE_RATE, BoxChangeEngine)
from extras.box_gcode import read_metadata
from extras.box_catalog import resolve_material
from extras.motion_limits import saved_motion, set_service_motion


SLOTS_PER_BOX = box_protocol.SLOTS_PER_BOX
EXTERNAL_PROFILE_KEY = "external"
# printer.box contract: docs/status-api.md (published as
# docs/box-status-api.md in the firmware repo). API_VERSION changes only on a
# break clients must handle; API_REVISION on every additive schema change.
API_VERSION = 1
API_REVISION = 6
# A BOX_PRINT_MAP map is dropped if no print file loads within this time.
NEXT_PRINT_MAP_TIMEOUT = 600.0
SAFE_WIDGET_COMMANDS = frozenset((
    "_BOX_SLOT_SET",
    "_BOX_SLOT_CLEAR",
    "_BOX_MATERIAL_SET",
    "_BOX_SET_RUNOUT_SWAP",
    "_BOX_SET_UNLOAD_AFTER_PRINT",
    "_BOX_SET_RFID_INSERT_READING",
    "_BOX_SET_RFID_STARTUP_READING",
))

DEFAULT_MATERIALS = {
    "ABS": {"target_temp": 245},
    "ASA": {"target_temp": 245},
    "PLA": {"target_temp": 220},
    "PETG": {"target_temp": 245},
}

POLL_START_DELAY = 5.0
ACTIVE_POLL = 1.0
IDLE_POLL = 5.0
TOPOLOGY_POLL = 15.0
RFID_REFRESH = 30.0
ERROR_BACKOFF = 10.0
# Consecutive missed state reads before a CFS reports offline; one dropped
# RS-485 frame must not flicker a box offline or hide its slots.
OFFLINE_POLLS = 3
# How often a box that was online at startup and lost power is looked for.
# Each try is one discovery broadcast, up to a second of bus time when
# nothing answers.
RECONNECT_INTERVAL = 15.0
# Host-side fault for filament that snapped between a slot and the hub. It
# has no CFS status code.
SNAPPED_STATUS = "FILAMENT_SNAPPED"
# Host-side fault for filament at more than one hub lane. The loaded slot is
# unknown until it clears, so loads and unloads are refused.
PATH_CONFLICT_STATUS = "PATH_CONFLICT"
PATH_CONFLICT_TITLE = "Filament in more than one hub lane"
# A CFS reply the poll could not use, though the box answered.
_DRAINING = object()
STATE_TIMEOUT = 5.0
STATE_POLL = 0.1
STATE_EVENT_DRAIN = 4
LOAD_TIMEOUT = 45.0
STAGE5_POLL = 0.1
PATH_RETRACT_TIMEOUT = 45.0
BUFFER_RETRACT_TIMEOUT = 7.0

UNLOAD_RETRACT_MM = 25.0
UNLOAD_CLEAR_MIN_MM = 50.0
UNLOAD_RETRY_MM = 25.0
UNLOAD_RETRIES = 3
ENCODER_CLEAR_MM = 20.0

# The CFS refills the buffer in ~25-30mm chunks, so any single refill moves
# the encoder past this and proves filament is flowing.
CLOG_ENCODER_RESET_MM = 18.0

# A spool that runs out leaves a tail between the CFS and the printhead. Its
# end has left the CFS, so only the extruder can move it. The saved setting
# names its slot until the printhead sensor clears.
TAIL_SETTING = "tail"
# The CFS stops a feed at 3000 mm, so no load path or tail is longer.
TAIL_MAX_MM = 3000.0
# What each CFS fed on its recent clean loads, in mm: the length of its load
# path. A full unload that pulls back much less left filament in the tube.
PATH_SETTING = "path_lengths"
PATH_SAMPLES = 5
PATH_MIN_SAMPLES = 3
PATH_MIN_MM = 300.0
PATH_SHORT_MM = 300.0

CFS_COMMAND_FATAL_STATUSES = frozenset((
    box_protocol.STATUS_STAGE0_SENSOR_TIMEOUT,
    box_protocol.STATUS_SLOT_EMPTY,
    box_protocol.STATUS_STAGE0_ODOMETER_TIMEOUT,
    box_protocol.STATUS_FEED_TIMEOUT,
    box_protocol.STATUS_OVERTRAVEL,
    box_protocol.STATUS_ODOMETER_STALLED,
    box_protocol.STATUS_BUFFER_FILL_TIMEOUT,
    box_protocol.STATUS_BUFFER_NOT_FULL,
    box_protocol.STATUS_UNLOAD_BUFFER_TIMEOUT,
    box_protocol.STATUS_UNLOAD_HUB_PE_TIMEOUT,
    box_protocol.STATUS_UNLOAD_NO_FILAMENT,
    box_protocol.STATUS_UNLOAD_INLET_CLEAR,
    box_protocol.STATUS_UNLOAD_ALL_EMPTY,
    box_protocol.STATUS_UNLOAD_MOTOR_BLOCKED,
    box_protocol.STATUS_UNLOAD_ODOMETER_TIMEOUT,
    box_protocol.STATUS_BUFFER_REFILL_STALLED,
    box_protocol.STATUS_BUFFER_REFILL_NO_MOTION,
))
# Not user problems: logged, never shown as a fault.
CFS_LOG_ONLY_STATUSES = frozenset((
    box_protocol.STATUS_BUSY,
    box_protocol.STATUS_STAGE7_NO_MOTION,
))
# An unused box's status is not announced again sooner than this, so an
# error that returns after each clear cannot flood the console.
ADVISORY_REPEAT = 60.0
# One corrupted frame is noise; repeated ones point at the cable.
CRC_FAULT_COUNT = 3
CRC_FAULT_WINDOW = 60.0

CLEAN_LIMIT_VELOCITY = 800
CLEAN_LIMIT_ACCEL = 10000
CLEAN_MINIMUM_CRUISE_RATIO = 0.5
CLEAN_LIMIT_SCV = 5
CLEAN_SERPENTINE_Y_STEP = 2.0
CLEAN_SCRAPER_PASSES = 3
# Toolhead moves between the wastebin visits of a filament retry.
DEFAULT_FILAMENT_RETRY_MOVES = "Y350, X300, Y50, X50"

SNAP_RETRACT_MM = 1.2

CUT_SAFE_Z = 2.0
CUT_LIMIT_VELOCITY = 800
CUT_LIMIT_CRUISE = 1.0 / 3.0
CUT_LIMIT_SCV = 10
CUT_RETURN_WAIT = 3.0
CUT_RETRY_SETTLE = 0.15
CUT_POST_RETRACT_MM = 3.0

SPOOLMAN_PROXY_URL = "http://127.0.0.1:7125/server/spoolman/proxy"
SPOOLMAN_TIMEOUT = 2.0
SPOOLMAN_MAX_BYTES = 8 * 1024 * 1024


def _klog(msg, *args, level=logging.info):
    level("box: " + msg, *args)


class _VirtualSDGCodeObserver:
    def __init__(self, delegate, owner):
        self.delegate = delegate
        self.owner = owner
        self.enabled = True

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def run_script(self, script):
        result = self.delegate.run_script(script)
        if self.enabled:
            try:
                self.owner._observe_sd_line(script)
            except Exception:
                self.enabled = False
                _klog('runout feature observer disabled', level=logging.exception)
        return result


def _parse_retry_moves(text, error):
    """Parse comma-separated XY moves such as "Y350, X300 Y50"."""
    moves = []
    for item in str(text).split(","):
        move = {}
        try:
            words = item.upper().split()
            if not words:
                raise ValueError(item)
            for word in words:
                axis, value = word[0].lower(), float(word[1:])
                if axis not in "xy" or axis in move or not math.isfinite(value):
                    raise ValueError(item)
                move[axis] = value
        except ValueError:
            raise error(
                "Invalid filament_retry_moves entry %r" % (item.strip(),))
        moves.append(move)
    return tuple(moves)


def _spool_id_from_reserve(value):
    value = str(value or "").strip()
    # Some stock spools have 1 in this field, so it can't be used as a spool ID.
    return int(value) if value.isdigit() and int(value) > 1 else None


def _fetch_spoolman_spool(spool_id):
    body = json.dumps({
        "request_method": "GET", "path": "/v1/spool/%d" % spool_id,
        "use_v2_response": True,
    }).encode()
    request = urllib.request.Request(
        SPOOLMAN_PROXY_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=SPOOLMAN_TIMEOUT) as response:
        raw = response.read(SPOOLMAN_MAX_BYTES + 1)
    if len(raw) > SPOOLMAN_MAX_BYTES:
        return None
    result = json.loads(raw.decode()).get("result", {})
    spool = result.get("response") if result.get("error") is None else None
    if not isinstance(spool, dict) or spool.get("archived"):
        return None
    try:
        return spool if int(spool["id"]) == spool_id else None
    except (KeyError, TypeError, ValueError):
        return None


def _join_and(items):
    """'1', '1 and 2', '1, 2 and 3'."""
    if len(items) < 2:
        return "".join(items)
    return "%s and %s" % (", ".join(items[:-1]), items[-1])


class BoxError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoxSnapshot:
    data_ready: bool = False
    status_code: object = None
    state_code: object = None
    temp_c: object = None
    humidity_pct: object = None
    loaded_slot: object = None
    loaded_mask: int = 0
    slot_mask: int = 0
    tracking: bool = False
    filament_detected: object = None
    filament_sensor_error: object = None
    path_box: object = None
    encoder_mm: object = None
    buffer_status: object = None
    buffer_state: object = None
    # Legacy top-level status: the feeding box, else the lowest responding.
    # Only the legacy fields (docs/status-api.md) use these, temp_c,
    # humidity_pct, and the relay fallback beyond the buffer.
    relay_status_code: object = None
    relay_state_code: object = None
    # Why the loaded path is unknown (filament at more than one hub lane), or
    # None. Operations refuse while it is set.
    conflict: object = None


@dataclass(frozen=True)
class TrackingOwner:
    address: int
    slot: int
    epoch: int


def box_profile_key(uniid, local_slot):
    """Persisted profile key: the CFS's 12-byte UniID plus its local slot."""
    return "%s:%d" % (bytes(uniid).hex(), local_slot)


class BoxStore:
    """Small atomic JSON store for profiles, settings, and runtime identity."""

    def __init__(self, path):
        self.path = path
        self.data = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {
                "materials": {name: dict(value) for name, value in DEFAULT_MATERIALS.items()},
                "slots": {},
                "rfid_mappings": {},
                "runtime": {},
                "addresses": {},
            }
        try:
            with open(self.path, "r") as stream:
                data = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise BoxError("Unable to read %s: %s" % (self.path, exc))
        if not isinstance(data, dict):
            raise BoxError("%s must contain a JSON object" % self.path)
        sections = {
            name: data.get(name, {})
            for name in ("materials", "slots", "rfid_mappings", "runtime", "addresses")
        }
        for name, value in sections.items():
            if not isinstance(value, dict):
                raise BoxError("%s.%s must be an object" % (self.path, name))
        sections["materials"] = self._materials(sections["materials"])
        sections["rfid_mappings"] = self._rfid_mappings(
            sections["rfid_mappings"])
        sections["addresses"] = self._addresses(sections["addresses"])
        return sections

    @staticmethod
    def _materials(values):
        result = {}
        for key, value in values.items():
            name = str(key).strip().upper()
            if not name or not isinstance(value, dict):
                raise BoxError("Invalid material entry %r" % key)
            target = value.get("target_temp")
            if target is not None and (
                    isinstance(target, bool) or not isinstance(target, int)):
                raise BoxError("Invalid target temperature for %s" % name)
            result[name] = {"target_temp": target}
        return result

    @staticmethod
    def _rfid_mappings(values):
        result = {}
        for key, value in values.items():
            code, _product = resolve_material(key)
            if not code or not isinstance(value, dict):
                raise BoxError("Invalid RFID mapping %r" % key)
            material = str(value.get("material", "")).strip().upper()
            if not material:
                raise BoxError("RFID mapping %s has no material" % code)
            target = value.get("target_temp")
            if target is not None and (
                    isinstance(target, bool) or not isinstance(target, int)):
                raise BoxError("Invalid target temperature for RFID %s" % code)
            result[code] = {
                "material": material,
                "brand": str(value.get("brand", "")).strip(),
                "name": str(value.get("name", "")).strip(),
                "target_temp": target,
            }
        return result

    @staticmethod
    def _addresses(values):
        result = {}
        for key, value in values.items():
            try:
                address = int(key)
                uid = bytes.fromhex(value)
            except (TypeError, ValueError):
                raise BoxError("Invalid box address entry %r" % key)
            if not 1 <= address <= 4 or len(uid) != 12 or not any(uid):
                raise BoxError("Invalid box identity at address %s" % key)
            result[str(address)] = uid.hex()
        return result

    def save(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w") as stream:
            json.dump(self.data, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, self.path)

    @property
    def materials(self):
        return {name: dict(value) for name, value in self.data["materials"].items()}

    def set_material(self, name, target):
        key = str(name).strip().upper()
        if not key:
            raise ValueError("material is required")
        self.data["materials"][key] = {"target_temp": int(target)}
        self.save()
        return key

    def profile(self, key):
        value = self.data["slots"].get(str(key))
        if not isinstance(value, dict):
            value = {}
        return {
            "material": str(value.get("material", "")).strip().upper(),
            "color": str(value.get("color", "")).strip().upper(),
            "brand": str(value.get("brand", "")).strip(),
            "name": str(value.get("name", "")).strip(),
            "spoolman_id": value.get("spoolman_id"),
            "rfid_reserve": str(value.get(
                "rfid_reserve", "")).strip().strip("\x00").strip(),
        }

    def set_profile(self, key, profile):
        clean = {
            "material": str(profile.get("material", "")).strip().upper(),
            "color": str(profile.get("color", "")).strip().upper(),
            "brand": str(profile.get("brand", "")).strip(),
            "name": str(profile.get("name", "")).strip(),
            "spoolman_id": profile.get("spoolman_id"),
        }
        reserve = str(profile.get(
            "rfid_reserve", "")).strip().strip("\x00").strip()
        if reserve:
            clean["rfid_reserve"] = reserve
        # A spool is only in one place: the newest link wins.
        unlinked = []
        if clean["spoolman_id"] is not None:
            for other, value in self.data["slots"].items():
                if (other != str(key)
                        and value.get("spoolman_id") == clean["spoolman_id"]):
                    value["spoolman_id"] = None
                    unlinked.append(other)
        self.data["slots"][str(key)] = clean
        self.save()
        return unlinked

    def clear_profile(self, key):
        if self.data["slots"].pop(str(key), None) is not None:
            self.save()

    def migrate_slot_profiles(self, identities):
        """Move legacy global-slot keys onto the CFS identity at that address.

        A legacy key whose address has no identity is left untouched; a later
        enumeration that identifies the address migrates it. A legacy key
        whose identity profile already exists is stale: it is logged and
        dropped so it cannot resurface on another box later.
        """
        slots = self.data["slots"]
        moved = {}
        for key in list(slots):
            if not key.isdigit() or int(key) >= MAX_ADDRESSES * SLOTS_PER_BOX:
                continue
            address, local = divmod(int(key), SLOTS_PER_BOX)
            uniid = identities.get(address + 1)
            if uniid is None:
                continue
            target = box_profile_key(uniid, local)
            if target in slots:
                _klog("dropped stale legacy profile %s=%s; %s already exists",
                      key, slots.pop(key), target, level=logging.warning)
            else:
                slots[target] = slots.pop(key)
            moved[key] = target
        if moved:
            self.save()
            _klog("migrated slot profiles %s", moved)

    def setting(self, name, default=None):
        return self.data["runtime"].get(name, default)

    def set_setting(self, name, value):
        self.data["runtime"][name] = value
        self.save()

    def clear_settings(self, *names):
        changed = False
        for name in names:
            changed |= self.data["runtime"].pop(name, None) is not None
        if changed:
            self.save()

    def rfid_mapping(self, code):
        normalized, _product = resolve_material(code)
        value = self.data["rfid_mappings"].get(normalized)
        return None if value is None else dict(value)

    def set_rfid_mapping(self, code, value):
        normalized, _product = resolve_material(code)
        if not normalized:
            raise ValueError("RFID code is required")
        clean = self._rfid_mappings({normalized: value})
        self.data["rfid_mappings"][normalized] = clean[normalized]
        self.save()
        return normalized

    def delete_rfid_mapping(self, code):
        normalized, _product = resolve_material(code)
        if self.data["rfid_mappings"].pop(normalized, None) is not None:
            self.save()
        return normalized

    @property
    def known_addresses(self):
        return {int(address): bytes.fromhex(uid) for address, uid in self.data["addresses"].items()}

    def set_known_addresses(self, mapping):
        value = {
            str(int(address)): bytes(uid).hex() for address, uid in mapping.items()
        }
        if value != self.data["addresses"]:
            self.data["addresses"] = value
            self.save()


class Box:
    CONSOLE_PREFIX = "[BOX]: "

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.pause_resume = self.printer.load_object(config, "pause_resume")
        self.box_count = config.getint(
            "box_count", MAX_ADDRESSES, minval=1, maxval=MAX_ADDRESSES)
        self.store = BoxStore(config.get(
            "state_path", "/mnt/UDISK/printer_data/filament_box.json"))

        # Positions are kept to 3 decimals. Service moves land within 1e-9
        # of their target, well inside the 1e-6 "already at the wastebin"
        # test.
        self.wastebin_x = round(config.getfloat("wastebin_pos_x", 133.0), 3)
        self.wastebin_y = round(config.getfloat("wastebin_pos_y", 378.0), 3)
        # Staging point for the climb to the wastebin row; must sit right of
        # and in front of the bin so the flap is only ever struck in -X.
        self.wastebin_approach_x = round(
            config.getfloat("wastebin_approach_x", 160.0), 3)
        self.wastebin_approach_y = round(
            config.getfloat("wastebin_approach_y", 350.0), 3)
        if (self.wastebin_approach_x <= self.wastebin_x
                or self.wastebin_approach_y >= self.wastebin_y):
            raise config.error(
                "wastebin_approach_x must be greater than wastebin_pos_x and "
                "wastebin_approach_y less than wastebin_pos_y")
        # The pad's back edge is the wastebin row the scraper passes run on.
        self.clean_pad_left_x = config.getfloat("clean_pad_left_x", 154.0)
        self.clean_pad_right_x = config.getfloat("clean_pad_right_x", 166.0)
        self.clean_pad_front_y = config.getfloat("clean_pad_front_y", 367.0)
        self.clean_pad_passes = config.getint(
            "clean_pad_passes", 1, minval=1)
        for legacy_name in (
                "clean_left_pos_x", "clean_right_pos_x", "clean_right_pos_y",
                "clean_pad_back_y"):
            config.get(legacy_name, None)
        if (self.clean_pad_left_x >= self.clean_pad_right_x
                or self.clean_pad_front_y >= self.wastebin_y):
            raise config.error("Invalid clean pad boundaries")
        self.filament_retry_moves = _parse_retry_moves(config.get(
            "filament_retry_moves", DEFAULT_FILAMENT_RETRY_MOVES),
            config.error)
        self.travel_velocity = config.getfloat(
            "travel_velocity", 18000.0, above=0.0)
        self.z_velocity = config.getfloat(
            "z_velocity", 600.0, above=0.0)
        self.clean_velocity = config.getfloat(
            "clean_velocity", 12000.0, above=0.0)
        self.snap_fan_speed = config.getfloat(
            "snap_fan_speed", 1.0, minval=0.0, maxval=1.0)
        self.snap_fan_dwell_ms = config.getint(
            "snap_fan_dwell_ms", 2000, minval=0)
        self.pre_cut_x = config.getfloat("pre_cut_pos_x", 10.0)
        self.cut_x = config.getfloat("cut_pos_x", None)
        self.cut_y = config.getfloat("cut_pos_y", 200.0)
        self.cut_velocity = config.getfloat(
            "cut_velocity", 30000.0, above=0.0)
        self.cut_accel = config.getfloat("cut_accel", 7500.0, above=0.0)
        self.retract_velocity = config.getfloat(
            "retract_velocity", 3000.0, above=0.0)
        self.external_feed_velocity = config.getfloat(
            "external_feed_velocity", 600.0, above=0.0)
        self.pre_cut_cal_x = config.getfloat("pre_cut_cal_pos_x", -5.0)
        self.cut_check_max_x = config.getfloat("check_cut_pos_x_max", -5.5)
        self.cut_check_min_x = config.getfloat("check_cut_pos_x_min", -9.5)
        self.clog_detection = config.getboolean("clog_detection", True)
        # Below one refill chunk, normal buffer draw would read as a clog.
        self.clog_extruder_length = config.getfloat(
            "clog_extruder_length", 80.0, minval=30.0)

        self.serial = None
        self.drivers = {}
        self.box_ids = {}  # address -> UniID of each box online at enumeration
        self.address_errors = ()
        self.drivers_ready = False
        self.snapshot = BoxSnapshot(loaded_slot=None)
        self.operation_depth = 0
        self.tracking_epoch = 0
        self.tracking_owner = None
        self.path_owner = None
        self.tail_baseline = None
        self.tail_travel = None  # extruder mm since the tail's spool ran out
        self._clear_runout_state()
        self.runout_defer_active = False
        self._kalico_runout_taken = False
        self.runout_feature = None
        self.fault_generation = 0
        self.last_fatal_reason = None
        self.fault_episodes = {}
        # address -> (episode, message): replaces STATUS_TEXT's message for
        # as long as that episode object lasts.
        self.fault_messages = {}
        self.advised = {}  # address -> (status, time) reported as unused
        self.snapped = None  # loaded slot whose filament snapped in the box
        self.last_reconnect = 0.0
        self.reconnect_noted = set()  # errors already reported once
        self.offline_handled = set()  # boxes whose outage paused a print
        # (print start time,) of the print the current conflict paused.
        self.conflict_paused = None
        self.crc_errors = {}
        self.crc_warned = set()
        self.rfid_percent = {}
        self.unknown_rfid = {}
        self.rfid_presence = {}
        self.rfid_pending = set()
        self.rfid_snapshot = {}
        self.rfid_seen_invalid = set()
        self.box_replies = None  # until the first poll; then address -> reply
        self.box_last = {}
        self.box_misses = {}
        # Filament at more than one hub lane: (addresses, message) or None.
        self.path_conflict = None
        # Failing poll steps: key -> the error last logged for it.
        self.contained_errors = {}
        self.last_status = None
        self.last_rfid_refresh = 0.0
        self.last_topology_refresh = 0.0
        self.spoolman_generation = 0
        self.spoolman_tokens = {}
        self.clog_event_count = 0
        self.clog_baseline = None
        self.last_clog = {"extruder_mm": None, "encoder_mm": None}

        pins = self.printer.lookup_object("pins")
        pins.allow_multi_use_pin("nozzle_mcu:PB9")
        buttons = self.printer.load_object(config, "buttons")
        self.cut_sensor_state = False
        buttons.register_buttons(["!nozzle_mcu:PB9"], self._cut_sensor_callback)

        self.address_manager = AutoAddressManager(self.box_count, self.store.known_addresses)
        self.change_engine = BoxChangeEngine(self, config)

        self.poll_timer = self.reactor.register_timer(self._poll)
        self.enumeration_started = False
        self.klippy_ready = False
        self.registered_tools = set()
        self.print_info = None
        self.next_print_map = None
        self._register_commands()
        self.printer.register_event_handler("serial_485:ready", self._serial_ready)
        self.printer.register_event_handler("klippy:ready", self._klippy_ready)
        self.printer.register_event_handler("klippy:disconnect", self._disconnect)
        self.printer.register_event_handler("klippy:shutdown", self._disconnect)
        self.printer.register_event_handler(
            "filament:runout", self._handle_filament_runout)
        self.printer.register_event_handler(
            "external_rfid_reader:record", self._external_rfid_record)
        for event in (
                "print_stats:complete_printing",
                "print_stats:error_printing",
                "print_stats:cancelled_printing",
                "print_stats:reset",
                "virtual_sdcard:load_file",
                "virtual_sdcard:reset_file"):
            self.printer.register_event_handler(
                event, self.change_engine.reset_print_recovery)
            self.printer.register_event_handler(
                event, self._reset_runout_defer)
        # After reset_print_recovery, which clears the previous job's map.
        self.printer.register_event_handler(
            "virtual_sdcard:load_file", self._apply_next_print_map)
        self.printer.register_event_handler(
            "print_stats:complete_printing",
            lambda *args: self._report_tail_after_print(True))
        for event in ("print_stats:error_printing",
                      "print_stats:cancelled_printing"):
            self.printer.register_event_handler(
                event, lambda *args: self._report_tail_after_print(False))

    # ------------------------------------------------------------------
    # Lifecycle and public status
    # ------------------------------------------------------------------

    def _register_commands(self):
        commands = (
            ("BOX_PRINT_INFO", self.cmd_print_info, "Inspect tools used by a print"),
            ("BOX_PRINT_START", self.cmd_print_start, "Start a print with a tool map"),
            ("BOX_PRINT_MAP", self.cmd_print_map,
             "Set the tool map for the next print start"),
            ("BOX_SELECT_SLOT", self.cmd_select_slot,
             "Switch to SLOT (unload, load, purge), whatever the print's tool map says"),
            ("BOX_LOAD", self.cmd_load,
             "Feed SLOT to the printhead only; no unload, heating, or purge"),
            ("BOX_UNLOAD", self.cmd_unload, "Fully unload the active filament"),
            ("BOX_DEBUG", self.cmd_debug, "Show complete box diagnostics"),
            ("BOX_BUFFER_RETRACT", self.cmd_buffer_retract,
             "Run the CFS buffer retract phase"),
            ("BOX_CUT", self.cmd_cut, "Cut the active filament"),
            ("NOZZLE_CLEAN", self.cmd_nozzle_clean,
             "Clean the nozzle; RETURN=1 comes back afterwards"),
            ("BOX_GO_TO_WASTEBIN", self.cmd_wastebin, "Move to the wastebin"),
            ("PARSE_FLUSH_VOLUMES", self.change_engine.parse_flush_volumes,
             "Parse slicer flush metadata"),
            ("BOX_RUNOUT_CHECK", self.cmd_runout,
             "Handle CFS runout"),
            ("_BOX_PAUSE_CAPTURE", self.change_engine.capture_pause,
             "Capture the temperature to resume this pause at"),
            ("_BOX_RESUME_PREPARE", self.change_engine.prepare_resume,
             "Recover Box state, then heat and prime at the wastebin"),
            ("_BOX_RESUME_COMMIT",
             self.change_engine.complete_pause_resume,
             "Clear completed pause state"),
            ("_BOX_SLOT_SET", self.cmd_slot_set, "Save slot metadata"),
            ("_BOX_SLOT_CLEAR", self.cmd_slot_clear, "Clear slot metadata"),
            ("_BOX_MATERIAL_SET", self.cmd_material_set, "Save material metadata"),
            ("_BOX_SET_RUNOUT_SWAP", self.cmd_runout_swap,
             "Set automatic runout swapping"),
            ("_BOX_SET_UNLOAD_AFTER_PRINT", self.cmd_unload_after_print,
             "Set automatic unload after printing"),
            ("_BOX_SET_RFID_INSERT_READING", self.cmd_rfid_insert,
             "Set RFID insertion reads"),
            ("_BOX_SET_RFID_STARTUP_READING", self.cmd_rfid_startup,
             "Set RFID startup reads"),
            ("_BOX_RFID_MAP_SET", self.cmd_rfid_map_set, "Save an RFID mapping"),
            ("_BOX_RFID_MAP_DELETE", self.cmd_rfid_map_delete,
             "Delete an RFID mapping"),
        )
        for name, handler, description in commands:
            if name in SAFE_WIDGET_COMMANDS:
                handler = self._guard_widget_command(name, handler)
            self.gcode.register_command(name, handler, desc=description)

    def _guard_widget_command(self, name, handler):
        def guarded(gcmd):
            try:
                return handler(gcmd)
            except self.gcode.error:
                raise
            except Exception as exc:
                raise gcmd.error(
                    "[BOX]: " + box_protocol.format_failed(name, exc))
        return guarded

    def cmd_runout(self, gcmd):
        self.cancel_runout_defer()
        return self.change_engine.runout(gcmd)

    def _serial_ready(self, *args):
        if self.enumeration_started:
            return
        self.enumeration_started = True
        self.reactor.register_callback(self._enumerate)

    def _enumerate(self, eventtime):
        self._invalidate_tracking_session()
        self.serial = self.printer.lookup_object("serial_485 serial485")
        client = box_protocol.AutoAddressClient(self.serial)
        reactor = self.reactor
        result = self.address_manager.enumerate(
            client,
            pause=lambda delay: reactor.pause(reactor.monotonic() + delay))
        self.address_errors = tuple(result.errors)
        if ADDRESS_WEDGE_WARNING in self.address_errors:
            self._warn(ADDRESS_WEDGE_WARNING)
        # Legacy profiles belong to the box persisted at that address when they
        # were written; the online box only claims addresses with no record.
        owners = dict(result.online)
        owners.update(self.store.known_addresses)
        self.store.migrate_slot_profiles(owners)
        self.store.set_known_addresses(result.known)
        self.box_ids = dict(result.online)
        self.drivers = {
            address: box_protocol.BoxDriver(
                self.serial, address, on_bad_crc=self._observe_crc_error)
            for address in sorted(result.online)
        }
        self.drivers_ready = True
        self._initialize_rfid()
        self._register_t_commands()
        self.printer.send_event("box:ready")
        if self.klippy_ready:
            self.reactor.update_timer(
                self.poll_timer, self.reactor.monotonic() + POLL_START_DELAY)

    def _register_t_commands(self):
        self._register_tools(self.physical_slots + (self.external_slot,))

    def _register_tools(self, tools):
        for tool in tools:
            if tool in self.registered_tools:
                continue
            self.gcode.register_command(
                "T%d" % tool,
                lambda gcmd, tool=tool: self.change_engine.select_tool(gcmd, tool),
                desc="Select tool T%d" % tool,
            )
            self.registered_tools.add(tool)

    def _klippy_ready(self, *args):
        self.klippy_ready = True
        self.reactor.register_callback(self._install_runout_source_observer)
        if self.drivers_ready:
            self.reactor.update_timer(
                self.poll_timer, self.reactor.monotonic() + POLL_START_DELAY)

    def _install_runout_source_observer(self, eventtime):
        helper = getattr(self._filament_sensor(), "runout_helper", None)
        original_handler = getattr(helper, "_runout_event_handler", None)
        if original_handler is not None:
            def handler(eventtime):
                if not self._kalico_runout_taken:
                    return original_handler(eventtime)

            helper._runout_event_handler = handler
        original_note = getattr(helper, "note_filament_present", None)
        if original_note is not None:
            def note(eventtime, *args, **kwargs):
                was_present = helper.filament_present
                original_note(eventtime, *args, **kwargs)
                if was_present and not helper.filament_present:
                    try:
                        self._printhead_cleared()
                    except Exception:
                        _klog('tail clear failed', level=logging.exception)

            helper.note_filament_present = note
        sd = self.printer.lookup_object("virtual_sdcard", None)
        if sd is None or isinstance(sd.gcode, _VirtualSDGCodeObserver):
            return
        sd.gcode = _VirtualSDGCodeObserver(sd.gcode, self)

    def _handle_filament_runout(self, _eventtime, sensor):
        if sensor != "filament_sensor":
            return
        sd = self.printer.lookup_object("virtual_sdcard", None)
        try:
            armed = (sd is not None and sd.is_active()
                     and self.filament_sensor_enabled())
            self._kalico_runout_taken = False
            if armed:
                self.runout_defer_active = True
            _klog("filament sensor runout event; %s",
                  "watching for infill" if armed else "infill watch not armed")
        except Exception:
            _klog('runout defer arm failed', level=logging.exception)

    def _observe_sd_line(self, line):
        marker = line.lstrip()
        if marker.upper().startswith(";TYPE:"):
            self.runout_feature = marker.split(":", 1)[1].strip().lower()
        if (self.runout_defer_active
                and "infill" in (self.runout_feature or "")):
            _klog(
                "runout defer dispatching BOX_RUNOUT_CHECK at feature=%s",
                self.runout_feature)
            self.gcode.run_script("BOX_RUNOUT_CHECK")

    def cancel_runout_defer(self):
        self.runout_defer_active = False
        self._kalico_runout_taken = True
        try:
            helper = self._filament_sensor().runout_helper
            helper.reset_runout_distance_info()
            if helper.min_event_systime == self.reactor.NEVER:
                helper.min_event_systime = (
                    self.reactor.monotonic() + helper.event_delay)
        except Exception:
            _klog('runout distance cleanup failed', level=logging.exception)

    def _reset_runout_defer(self, *args):
        self.cancel_runout_defer()
        self.runout_feature = None

    def _disconnect(self, *args):
        self.change_engine.reset_print_mapping()
        self.discard_next_print_map("Klipper disconnected")
        self._invalidate_tracking_session()
        self.spoolman_generation += 1
        self.spoolman_tokens.clear()
        self.serial = None
        # Nothing is polled after this; report no data rather than the last.
        self.drivers_ready = False
        self.snapshot = BoxSnapshot(loaded_slot=None)
        self.box_replies = {}
        self.box_last.clear()
        self.box_misses = {address: OFFLINE_POLLS for address in self.drivers}
        self.snapped = None
        self.path_conflict = None
        self.conflict_paused = None
        self.reactor.update_timer(self.poll_timer, self.reactor.NEVER)

    def get_status(self, eventtime):
        """printer.box. Keep docs/status-api.md in step with every change.

        Klipper's status timer serves every object, so this never raises: a
        failure serves the last good status, marked not ready.
        """
        try:
            status = self._status(eventtime)
        except Exception as exc:
            # Served several times a second: log once until it recovers.
            self._log_contained("status", exc, repeat=False)
            if self.last_status is not None:
                return dict(self.last_status, data_ready=False)
            return self._empty_status()
        self._clear_contained("status")
        self.last_status = status
        return status

    def _empty_status(self):
        """Every field clients read unconditionally, nothing to show.

        Fluidd reads slots and load_path even when data_ready is false.
        """
        return {
            "api_version": API_VERSION,
            "api_revision": API_REVISION,
            "print_mapping_version": 1,
            "print_info": None,
            "print_mapping": {"filename": None, "map": {},
                              "active_tool": None, "active_slot": None},
            "data_ready": False,
            "temp_c": None,
            "humidity_pct": None,
            "status": box_protocol.status_name(None),
            "status_code": None,
            "state": box_protocol.state_name(None),
            "state_code": None,
            "loaded_slot": None,
            "slots": [],
            "boxes": [],
            "materials": {},
            "runout": None,
            "runout_swap_enabled": False,
            "unload_after_print_enabled": False,
            "rfid_insert_reading_enabled": False,
            "rfid_startup_reading_enabled": False,
            "filament_detected": None,
            "filament_sensor_error": None,
            "load_path": {
                "source_slot": None,
                "box_addr": None,
                "tracking_active": False,
                "encoder": {"position_mm": None, "active": False},
                "buffer": {"state": None, "state_code": None},
                "printhead_sensor": {"detected": None, "error": None},
                "clog_detection": None,
            },
            "recovery": {"blocked": False, "automatic": False,
                         "target": None, "step": None, "reason": None},
            "operation": None,
            "driver_ready": self.drivers_ready,
        }

    def _status(self, eventtime):
        snap = self.snapshot
        physical = self._slot_statuses(snap)
        slots = physical + [self._external_status(snap)]
        return {
            "api_version": API_VERSION,
            "api_revision": API_REVISION,
            # Legacy, frozen at 1: print mapping follows API_VERSION/API_REVISION.
            "print_mapping_version": 1,
            "print_info": self.print_info,
            "print_mapping": self.change_engine.mapping_status(),
            "data_ready": snap.data_ready,
            # Legacy, see docs/status-api.md: per-box data is in boxes.
            "temp_c": snap.temp_c,
            "humidity_pct": snap.humidity_pct,
            "status": box_protocol.status_name(snap.relay_status_code),
            "status_code": snap.relay_status_code,
            "state": box_protocol.state_name(snap.relay_state_code),
            "state_code": snap.relay_state_code,
            "loaded_slot": snap.loaded_slot,
            "slots": slots,
            "boxes": self._boxes_status(),
            "materials": self.store.materials,
            "runout": self._runout_status(physical, snap),
            "runout_swap_enabled": self.runout_swap_enabled,
            "unload_after_print_enabled": self.unload_after_print_enabled,
            "rfid_insert_reading_enabled": self.rfid_insert_reading_enabled,
            "rfid_startup_reading_enabled": self.rfid_startup_reading_enabled,
            "filament_detected": snap.filament_detected,
            "filament_sensor_error": snap.filament_sensor_error,
            "load_path": self._load_path_status(snap),
            "recovery": self.change_engine.recovery_status(),
            "operation": self.change_engine.operation_status(eventtime),
            "driver_ready": self.drivers_ready,
        }

    def _slot_statuses(self, snap):
        return [self._slot_status(slot, snap) for slot in self.physical_slots]

    def _boxes_status(self):
        """Per-CFS readings from the last poll, keyed by RS-485 address."""
        if self.box_replies is None:
            return []
        boxes = []
        for address in sorted(self.drivers):
            online = self._box_online(address)
            reply = self.box_last.get(address) if online else None
            status_code = reply.status if reply else None
            state_code = reply.box_state if reply else None
            boxes.append({
                "address": address,
                "online": online,
                "slots": [self._global_slot(address, local)
                          for local in range(SLOTS_PER_BOX)],
                "temp_c": reply.temp_c if reply else None,
                "humidity_pct": reply.humidity_pct if reply else None,
                "status": box_protocol.status_name(status_code),
                "status_code": status_code,
                "state": box_protocol.state_name(state_code),
                "state_code": state_code,
                "fault": self._fault_status(address),
            })
        return boxes

    def _fault_status(self, address):
        snapped = self.snapped
        if snapped is not None and self._address_slot(snapped)[0] == address:
            title, message = self._snapped_text(snapped)
            return {
                "category": "fatal",
                "status": SNAPPED_STATUS,
                "status_code": None,
                "title": title,
                "message": message,
            }
        conflict = self.path_conflict
        if conflict is not None and address in conflict[0]:
            return {
                "category": "fatal",
                "status": PATH_CONFLICT_STATUS,
                "status_code": None,
                "title": PATH_CONFLICT_TITLE,
                "message": conflict[1],
            }
        episode = self.fault_episodes.get(address)
        if episode is not None:
            status_code, category = episode[0], episode[-1]
        elif len(self._recent_crc_errors(address)) >= CRC_FAULT_COUNT:
            status_code, category = box_protocol.STATUS_BAD_CRC, "advisory"
        else:
            return None
        title, message = box_protocol.status_text(status_code)
        exact = self.fault_messages.get(address)
        if exact is not None and exact[0] is episode and exact[1]:
            message = exact[1]
        return {
            "category": category,
            "status": box_protocol.status_name(status_code),
            "status_code": status_code,
            "title": title,
            "message": message,
        }

    def _box_online(self, address):
        return (address in self.drivers
                and self.box_misses.get(address, 0) < OFFLINE_POLLS)

    def _external_status(self, snap):
        return self._slot_status(self.external_slot, snap, external=True)

    def _slot_status(self, slot, snap, external=False):
        profile = self.profile(slot)
        return {
            "index": slot,
            "present": external or bool(snap.slot_mask & (1 << slot)),
            "loaded": snap.loaded_slot == slot or (
                not external and bool(snap.loaded_mask & (1 << slot))),
            "material": profile["material"],
            "color": profile["color"],
            "brand": profile["brand"],
            "name": profile["name"],
            "spoolman_id": profile["spoolman_id"],
            "rfid_percent": None if external else self.rfid_percent.get(slot),
            "rfid_reserve": profile["rfid_reserve"],
            "external": external,
        }

    def _load_path_status(self, snap):
        clog = self._clog_status()
        return {
            "source_slot": snap.loaded_slot if self.is_valid_slot(snap.loaded_slot) else None,
            "box_addr": snap.path_box,
            "tracking_active": snap.tracking,
            "encoder": {
                "position_mm": snap.encoder_mm,
                "active": snap.path_box is not None and snap.tracking,
            },
            "buffer": {
                "state": (box_protocol.buffer_state_name(snap.buffer_state)
                          if snap.buffer_status == box_protocol.STATUS_OK
                          else None),
                # Legacy, see docs/status-api.md.
                "state_code": snap.buffer_state,
            },
            "printhead_sensor": {
                "detected": snap.filament_detected,
                "error": snap.filament_sensor_error,
            },
            "clog_detection": clog,
        }

    # ------------------------------------------------------------------
    # Profiles, settings, and public integration seam
    # ------------------------------------------------------------------

    @property
    def physical_slots(self):
        return tuple(
            self._global_slot(address, local)
            for address in sorted(self.drivers)
            for local in range(SLOTS_PER_BOX)
        )

    @property
    def max_physical_slot(self):
        return self.physical_slots[-1] if self.physical_slots else -1

    @property
    def external_slot(self):
        return self.max_physical_slot + 1

    def is_physical_slot(self, slot):
        if not isinstance(slot, int) or slot < 0:
            return False
        address, _local = self._address_slot(slot)
        return address in self.drivers

    def is_valid_slot(self, slot):
        return self.is_physical_slot(slot) or slot == self.external_slot

    def slot_label(self, slot):
        return box_protocol.slot_label(slot, self.external_slot)

    def profile(self, slot):
        return self.store.profile(self._profile_key(slot))

    def set_profile(self, slot, profile):
        unlinked = self.store.set_profile(self._profile_key(slot), profile)
        if not unlinked:
            return
        labels = {str(self._profile_key(other)): self.slot_label(other)
                  for other in self.physical_slots + (self.external_slot,)}
        for key in unlinked:
            self._info(self.gcode, "Spoolman spool %s unlinked from %s" % (
                profile["spoolman_id"], labels.get(key, "a disconnected CFS")))

    def clear_profile(self, slot):
        self.store.clear_profile(self._profile_key(slot))

    def _profile_key(self, slot):
        """Profiles follow the CFS, so they are stored by UniID, not address.

        An address with no identified box keeps its legacy global-slot key,
        which migration moves once a box is identified there.
        """
        key = self._runtime_slot_key(slot)
        if key == EXTERNAL_PROFILE_KEY:
            return key
        address, local = self._address_slot(key)
        uniid = self.box_ids.get(address)
        return key if uniid is None else box_profile_key(uniid, local)

    def _runtime_slot_key(self, slot):
        if slot == EXTERNAL_PROFILE_KEY or slot == self.external_slot:
            return EXTERNAL_PROFILE_KEY
        return int(slot)

    def _runtime_slot(self, value):
        return self.external_slot if value == EXTERNAL_PROFILE_KEY else value

    @property
    def last_loaded_slot(self):
        return self._runtime_slot(self.store.setting("last_loaded_slot"))

    @last_loaded_slot.setter
    def last_loaded_slot(self, slot):
        self.store.set_setting("last_loaded_slot", self._runtime_slot_key(slot))

    def filament_identity(self, slot):
        profile = self.profile(slot)
        return profile["material"], profile["color"]

    def hotend_filament(self):
        value = self.store.setting("hotend_filament")
        if not isinstance(value, dict):
            return None
        temperature = value.get("temperature")
        return {
            "slot": self._runtime_slot(value.get("slot")),
            "material": str(value.get("material", "")).strip().upper(),
            "color": str(value.get("color", "")).strip().upper(),
            "temperature": int(temperature) if temperature is not None else None,
        }

    def set_hotend_filament(self, slot, temperature):
        material, color = self.filament_identity(slot)
        runtime = self.store.data["runtime"]
        runtime["hotend_filament"] = {
            "slot": self._runtime_slot_key(slot),
            "material": material,
            "color": color,
            "temperature": int(temperature),
        }
        runtime.pop("hotend_feed_pending", None)
        self.store.save()

    def mark_hotend_feed_pending(self, slot):
        self.store.set_setting(
            "hotend_feed_pending", self._runtime_slot_key(slot))

    def hotend_feed_pending(self, slot):
        return self.store.setting("hotend_feed_pending") == self._runtime_slot_key(slot)

    def clear_hotend_feed_pending(self, slot=None):
        if slot is None or self.hotend_feed_pending(slot):
            self.store.clear_settings("hotend_feed_pending")

    @property
    def runout_swap_enabled(self):
        return bool(self.store.setting("runout_swap_enabled", True))

    @property
    def unload_after_print_enabled(self):
        return bool(self.store.setting("unload_after_print_enabled", False))

    @property
    def rfid_insert_reading_enabled(self):
        return bool(self.store.setting("rfid_insert_reading_enabled", False))

    @property
    def rfid_startup_reading_enabled(self):
        return bool(self.store.setting("rfid_startup_reading_enabled", False))

    def slot_target_temp(self, slot):
        material = self.profile(slot)["material"]
        value = self.store.materials.get(material)
        return value.get("target_temp") if value else None

    def activate_spool(self, slot):
        spoolman_id = self.profile(slot)["spoolman_id"]
        if spoolman_id is not None:
            self._set_active_spool(int(spoolman_id))

    def clear_active_spool(self, slot):
        if (self.is_valid_slot(slot)
                and self.profile(slot)["spoolman_id"] is not None):
            self._set_active_spool(None)

    def _set_active_spool(self, spool_id):
        try:
            webhooks = self.printer.lookup_object("webhooks", None)
            if webhooks is not None:
                webhooks.call_remote_method(
                    "spoolman_set_active_spool", spool_id=spool_id)
        except Exception:
            _klog('active Spoolman update failed', level=logging.exception)

    def get_filament_sensor_state(self):
        try:
            status = self._filament_sensor().get_status(
                self.reactor.monotonic())
            return bool(status["filament_detected"]), None
        except Exception as exc:
            return None, str(exc)

    def _filament_sensor(self):
        return self.printer.lookup_object(
            "filament_switch_sensor filament_sensor", None)

    def filament_detected(self):
        detected, error = self.get_filament_sensor_state()
        if error:
            raise BoxError("Printhead filament sensor is unavailable: %s" % error)
        return detected

    def filament_sensor_enabled(self):
        return bool(self._filament_sensor().runout_helper.sensor_enabled)

    def enable_filament_sensor(self):
        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=1")

    def disable_filament_sensor(self):
        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0")

    @contextmanager
    def printhead_sensor_off(self, after="on"):
        """The printhead sensor off while filament moves, so no move reads
        as a runout. Afterwards it is on ("on"), as the moves left it
        ("leave"), or as it was ("restore"); after a failure, always as it
        was."""
        previous = self.filament_sensor_enabled()
        self.disable_filament_sensor()

        def restore():
            if previous:
                self.enable_filament_sensor()
            else:
                self.disable_filament_sensor()
        try:
            yield
        except BaseException:
            restore()
            raise
        if after == "on":
            self.enable_filament_sensor()
        elif after == "restore":
            restore()

    def get_cut_calibration_config(self):
        return {
            "pre_cut_pos_x": self.pre_cut_x,
            "cut_pos_x": self.cut_x,
            "cut_pos_y": self.cut_y,
            "pre_cut_cal_pos_x": self.pre_cut_cal_x,
            "check_cut_pos_x_max": self.cut_check_max_x,
            "check_cut_pos_x_min": self.cut_check_min_x,
        }

    def set_cut_position(self, value):
        self.cut_x = float(value)

    def get_cut_sensor_state(self):
        return bool(self.cut_sensor_state)

    def _runout_status(self, physical_slots, snap):
        source = self.runout_origin if self.runout_active else snap.loaded_slot
        if not self.is_physical_slot(source):
            return None
        profile = next(
            (item for item in physical_slots if item["index"] == source), None)
        if profile is None:
            return None
        material, color = profile["material"], profile["color"]
        chain = []
        if material and color:
            chain = [
                item["index"] for item in physical_slots
                if item["index"] != source and item["present"]
                and item["material"] == material and item["color"] == color
            ]
        return {"loaded_slot": source, "chain": chain,
                "leftover_mm": self.tail_remaining()}

    def runout_recovery(self):
        snap = self.read_live_state()
        owner = self.tracking_owner
        if (snap.status_code == box_protocol.STATUS_RUNOUT
                and owner is not None
                and snap.path_box == owner.address):
            self.runout_active = True
            self.runout_origin = owner.slot
            self.runout_key = (owner.address, owner.epoch)
        physical = self._slot_statuses(snap)
        runout = self._runout_status(physical, snap)
        if not self.runout_active:
            return {
                "recoverable": False,
                "reason": "Filament ran out at the printhead",
                "loaded_slot": snap.loaded_slot, "target_slot": None,
            }
        if not runout:
            return {
                "recoverable": False,
                "reason": "Filament ran out at the printhead",
                "loaded_slot": snap.loaded_slot, "target_slot": None,
            }
        if not runout["chain"]:
            return {
                "recoverable": False,
                "reason": "%s ran out and no other slot has the same filament"
                          % self.slot_label(runout["loaded_slot"]),
                "loaded_slot": runout["loaded_slot"], "target_slot": None,
            }
        return {
            "recoverable": True,
            "reason": "matching replacement slot found",
            "loaded_slot": runout["loaded_slot"],
            "target_slot": runout["chain"][0],
        }

    def _clog_status(self):
        if self.clog_baseline is None:
            extruder_delta = encoder_delta = None
        else:
            extruder_delta = self.clog_baseline.get("last_extruder", 0.0) - self.clog_baseline["extruder"]
            encoder_delta = abs(
                self.clog_baseline.get("last_encoder", 0.0) - self.clog_baseline["encoder"])
        triggered = (
            extruder_delta is not None
            and extruder_delta > self.clog_extruder_length
            and encoder_delta <= CLOG_ENCODER_RESET_MM)
        if not self.clog_detection or self.runout_active:
            state = "disabled"
        elif not self.snapshot.tracking:
            state = "inactive"
        else:
            state = "triggered" if triggered else "active"
        return {
            "state": state,
            "enabled": self.clog_detection,
            "baseline_ready": self.clog_baseline is not None,
            "extruder_delta_mm": extruder_delta,
            "encoder_delta_mm": encoder_delta,
            "extruder_threshold_mm": self.clog_extruder_length,
            "encoder_reset_mm": CLOG_ENCODER_RESET_MM,
            "triggered": triggered,
            "event_count": self.clog_event_count,
            "last_event": dict(self.last_clog),
        }

    # ------------------------------------------------------------------
    # G-code command wrappers
    # ------------------------------------------------------------------

    def _print_idle(self, gcmd):
        if self.change_engine._is_print_active() or self.change_engine._is_print_paused():
            raise gcmd.error("[BOX]: Finish or cancel the current print first")
        if self.operation_depth:
            raise gcmd.error("[BOX]: Wait for the current Box operation to finish")
        return self.printer.lookup_object("virtual_sdcard")

    def _print_path(self, gcmd, sd):
        # Match SDCARD_PRINT_FILE, which accepts a leading slash.
        filename = gcmd.get("FILENAME")
        if filename.startswith("/"):
            filename = filename[1:]
        root = os.path.realpath(sd.sdcard_dirname)
        path = os.path.realpath(os.path.join(root, filename))
        if (not filename or os.path.isabs(filename)
                or os.path.commonpath((root, path)) != root
                or not filename.lower().endswith((".gcode", ".gco", ".g"))):
            raise gcmd.error("[BOX]: Select a text G-code file inside Virtual SD")
        return filename, path

    def _inspect_print(self, gcmd, sd):
        filename, path = self._print_path(gcmd, sd)
        try:
            metadata = read_metadata(path)
        except OSError as exc:
            raise gcmd.error("[BOX]: Unable to inspect print: %s" % exc)
        self.print_info = {"filename": filename, "tools": metadata["tools"]}
        return self.print_info

    def cmd_print_info(self, gcmd):
        self._inspect_print(gcmd, self._print_idle(gcmd))

    def _parse_print_map(self, gcmd):
        try:
            mapping = {}
            for entry in gcmd.get("MAP", "").split(","):
                if not entry.strip():
                    continue
                tool, slot = entry.split(":")
                tool, slot = int(tool), int(slot)
                if tool in mapping or not 0 <= tool <= 255 or slot < 0:
                    raise ValueError()
                mapping[tool] = slot
        except ValueError:
            raise gcmd.error("[BOX]: MAP must contain unique tool:slot pairs, e.g. 2:0,3:1")
        return mapping

    def _used_tools(self, error, tools):
        if not tools:
            raise error("[BOX]: No filament usage metadata; start this file normally")
        return {item["tool"] for item in tools}

    def _check_map_tools(self, error, used, mapping):
        if set(mapping) != used:
            raise error("[BOX]: Map every tool used by this file: %s" %
                        ", ".join("T%d" % tool for tool in sorted(used)))

    def _check_print_targets(self, error, mapping):
        if not self.drivers_ready:
            raise error("[BOX]: Filament slots are not ready")
        try:
            live = self.read_live_state()
        except Exception as exc:
            raise error("[BOX]: Unable to read CFS state: %s" % exc)
        for slot in mapping.values():
            if not self.is_valid_slot(slot) or (
                    self.is_physical_slot(slot)
                    and not self._box_online(self._address_slot(slot)[0])):
                raise error("[BOX]: %s is offline" % self.slot_label(slot))
            if self.is_physical_slot(slot) and not live.slot_mask & (1 << slot):
                raise error("[BOX]: %s has no filament" % self.slot_label(slot))

    def cmd_print_start(self, gcmd):
        # A map from BOX_PRINT_MAP that no print used yet is replaced, not merged.
        self.discard_next_print_map("cleared by BOX_PRINT_START")
        sd = self._print_idle(gcmd)
        info = self._inspect_print(gcmd, sd)
        used = self._used_tools(gcmd.error, info["tools"])
        mapping = self._parse_print_map(gcmd)
        self._check_map_tools(gcmd.error, used, mapping)
        self._check_print_targets(gcmd.error, mapping)
        try:
            self._register_tools(used)
        except Exception as exc:
            raise gcmd.error("[BOX]: Unable to register print tools: %s" % exc)
        # Load/reset events clear the old map. Install the new job's map only
        # after those events and before Virtual SD schedules its first command.
        sd._reset_file()
        try:
            sd._load_file(gcmd, info["filename"], check_subdirs=True)
            self._install_print_map(mapping, info["filename"])
            sd.do_resume()
        except Exception as exc:
            sd._reset_file()
            if isinstance(exc, self.gcode.error):
                raise
            # Any other exception from a G-code handler shuts Klipper down.
            raise gcmd.error("[BOX]: Unable to start %s: %s"
                             % (info["filename"], exc))

    def cmd_print_map(self, gcmd):
        mapping = self._parse_print_map(gcmd)
        if not mapping:
            # An empty MAP clears a map an earlier start never used.
            self.discard_next_print_map("cleared by an empty BOX_PRINT_MAP")
            return
        self._print_idle(gcmd)
        self._check_print_targets(gcmd.error, mapping)
        self.next_print_map = (
            mapping, self.reactor.monotonic() + NEXT_PRINT_MAP_TIMEOUT)
        # Kalico logs this reply, so it also records the map in klippy.log.
        self._info(gcmd, "Tool map set for the next print: %s"
                   % self._describe_print_map(mapping))

    def discard_next_print_map(self, reason="power-loss recovery"):
        pending, self.next_print_map = self.next_print_map, None
        if pending is not None:
            _klog("dropped the next print's tool map (%s): %s",
                  reason, self._describe_print_map(pending[0]))

    def _describe_print_map(self, mapping):
        return "; ".join("T%d -> %s" % (tool, self.slot_label(slot))
                         for tool, slot in sorted(mapping.items()))

    def _install_print_map(self, mapping, filename, action="using"):
        self.change_engine.tool_map = mapping
        self.change_engine.mapping_filename = filename
        _klog("%s tool map for %s: %s",
              action, filename, self._describe_print_map(mapping))

    def _apply_next_print_map(self, *args):
        """Install a BOX_PRINT_MAP map on the file the next print loaded.

        Runs inside the start command, so raising refuses the print before
        Virtual SD schedules its first line. The map is used once either way.
        """
        pending = self.next_print_map
        if pending is None:
            return
        mapping, expires = pending
        if self.reactor.monotonic() > expires:
            self.discard_next_print_map("expired before a print loaded")
            return
        self.next_print_map = None
        sd = self.printer.lookup_object("virtual_sdcard")
        path = sd.current_file.name
        filename = os.path.relpath(path, sd.sdcard_dirname)
        error = self.gcode.error
        try:
            used = self._used_tools(error, read_metadata(path)["tools"])
            self._check_map_tools(error, used, mapping)
            self._check_print_targets(error, mapping)
            self._register_tools(used)
            self._install_print_map(mapping, filename)
        except Exception as exc:
            _klog("dropped the next print's tool map (print refused): %s",
                  self._describe_print_map(mapping))
            sd._reset_file()
            if isinstance(exc, error):
                raise
            # Any other exception from a G-code handler shuts Klipper down.
            raise error("[BOX]: Unable to start %s: %s" % (filename, exc))

    def cmd_select_slot(self, gcmd):
        self.change_engine.select_slot(
            gcmd, gcmd.get_int("SLOT", minval=0),
            bool(gcmd.get_int("FLUSH", 1)))

    def cmd_load(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", 0, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX - 1)
        try:
            already_loaded = self.physical_load(slot)
        except Exception as exc:
            raise gcmd.error("[BOX]: " + box_protocol.format_failed(
                "%s load" % self.slot_label(slot), exc))
        if already_loaded:
            self._info(gcmd, "%s already loaded; tracking active" % self.slot_label(slot))
        else:
            self._info(gcmd, "%s loaded" % self.slot_label(slot))
        self.last_loaded_slot = slot
        self.activate_spool(slot)

    def cmd_unload(self, gcmd):
        if "SLOT" in gcmd.get_command_parameters():
            raise gcmd.error("[BOX]: BOX_UNLOAD no longer accepts SLOT")
        manual = bool(gcmd.get_int("MANUAL", 0, minval=0, maxval=1))
        cfs_only = bool(gcmd.get_int("CFS_ONLY", 0, minval=0, maxval=1))
        self.change_engine.unload(gcmd, manual=manual, cfs_only=cfs_only)

    def cmd_buffer_retract(self, gcmd):
        try:
            with self._operation():
                live = self.read_live_state(include_topology=False)
                if self.tail_loaded(live):
                    raise BoxError(
                        "%s ran out; the CFS holds none of its filament"
                        % self.slot_label(live.loaded_slot))
                if self.is_physical_slot(live.loaded_slot):
                    driver, address, _local = self._driver_for_slot(live.loaded_slot)
                else:
                    if not self.drivers:
                        raise BoxError("No CFS box is online")
                    address = min(self.drivers)
                    driver = self.drivers[address]
                self._set_tracking(
                    driver, address, None, "disable CFS tracking")
                self._require_reply(
                    driver.unload_buffer(timeout=BUFFER_RETRACT_TIMEOUT),
                    "buffer retract")
        except Exception as exc:
            raise gcmd.error(
                "[BOX]: " + box_protocol.format_failed("Buffer retract", exc))
        self._info(gcmd, "Box %d buffer retract complete" % address)

    def cmd_cut(self, gcmd):
        try:
            self.cut_filament(force=bool(gcmd.get_int("FORCE", 0)))
        except Exception as exc:
            raise gcmd.error("[BOX]: BOX_CUT failed: %s" % exc)

    def cmd_nozzle_clean(self, gcmd):
        # RETURN=1 ends at the exact toolhead position the clean started
        # from, as a probe's nozzle_scrubber_gcode must.
        come_back = gcmd.get_int("RETURN", 0, minval=0, maxval=1)
        toolhead = self.printer.lookup_object("toolhead")
        start = toolhead.get_position()[:3]
        self.nozzle_clean()
        if come_back:
            self._zone().manual_move(start, self.travel_velocity / 60.0)
            toolhead.wait_moves()

    def cmd_wastebin(self, gcmd):
        self.move_to_wastebin()
        self._info(gcmd, "Moved to wastebin")

    def cmd_slot_set(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX)
        if slot is None:
            raise gcmd.error("[BOX]: SLOT is required")
        if not self.is_valid_slot(slot):
            raise gcmd.error("[BOX]: %s is not online" % self.slot_label(slot))
        material = self._param(gcmd, "MATERIAL")
        if not material:
            raise gcmd.error("[BOX]: MATERIAL is required")
        color = self._normal_color(self._param(gcmd, "COLOR"))
        if color is None:
            raise gcmd.error("[BOX]: COLOR must be #RRGGBB")
        profile = self.profile(slot)
        params = gcmd.get_command_parameters()
        profile["material"] = str(material).strip().upper()
        profile["color"] = color
        for field in ("BRAND", "NAME"):
            if field in params:
                profile[field.lower()] = str(self._param(gcmd, field) or "").strip()
        if "SPOOLMAN_ID" in params:
            try:
                spool = int(self._param(gcmd, "SPOOLMAN_ID"))
            except (TypeError, ValueError):
                raise gcmd.error("[BOX]: SPOOLMAN_ID must be an integer")
            profile["spoolman_id"] = None if spool < 0 else spool
        self.set_profile(slot, profile)
        self._info(gcmd, "Saved %s profile" % self.slot_label(slot))

    def cmd_slot_clear(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX)
        if slot is None:
            raise gcmd.error("[BOX]: SLOT is required")
        if not self.is_valid_slot(slot):
            raise gcmd.error("[BOX]: %s is not online" % self.slot_label(slot))
        self.clear_profile(slot)
        self._info(gcmd, "Cleared %s profile" % self.slot_label(slot))

    def cmd_material_set(self, gcmd):
        material = self._param(gcmd, "MATERIAL")
        target = gcmd.get_int("TARGET_TEMP", None, minval=170, maxval=350)
        if not material or target is None:
            raise gcmd.error("[BOX]: MATERIAL and TARGET_TEMP are required")
        key = self.store.set_material(material, target)
        self._info(gcmd, "Saved material %s: %dC" % (key, target))

    def cmd_runout_swap(self, gcmd):
        enabled = bool(gcmd.get_int("ENABLE", 1, minval=0, maxval=1))
        self.store.set_setting("runout_swap_enabled", enabled)
        self._info(gcmd, "Runout swap %s" % ("enabled" if enabled else "disabled"))

    def cmd_unload_after_print(self, gcmd):
        enabled = bool(gcmd.get_int("ENABLE", 0, minval=0, maxval=1))
        self.store.set_setting("unload_after_print_enabled", enabled)
        self._info(gcmd, "Unload after print %s" % (
            "enabled" if enabled else "disabled"))

    def cmd_rfid_insert(self, gcmd):
        enabled = bool(gcmd.get_int("ENABLE", 0, minval=0, maxval=1))
        self.store.set_setting("rfid_insert_reading_enabled", enabled)
        if not enabled:
            self.rfid_pending.clear()
            self.rfid_snapshot.clear()
            self.rfid_seen_invalid.clear()
        for driver in self.drivers.values():
            driver.set_rfid_insert_reading(enabled)
        self._info(gcmd, "RFID insertion reading %s" % (
            "enabled" if enabled else "disabled"))

    def cmd_rfid_startup(self, gcmd):
        enabled = bool(gcmd.get_int("ENABLE", 0, minval=0, maxval=1))
        self.store.set_setting("rfid_startup_reading_enabled", enabled)
        self._info(gcmd, "RFID startup reading %s" % (
            "enabled" if enabled else "disabled"))

    def cmd_rfid_map_set(self, gcmd):
        code = self._param(gcmd, "CODE")
        material = self._param(gcmd, "MATERIAL")
        brand = self._param(gcmd, "BRAND")
        name = self._param(gcmd, "NAME")
        if not all((code, material, brand, name)):
            raise gcmd.error("[BOX]: CODE, MATERIAL, BRAND, and NAME are required")
        target = self._param(gcmd, "TARGET_TEMP")
        if target not in (None, ""):
            try:
                target = int(target)
            except (TypeError, ValueError):
                raise gcmd.error("[BOX]: TARGET_TEMP must be an integer")
            if not 170 <= target <= 350:
                raise gcmd.error("[BOX]: TARGET_TEMP must be 170..350")
        else:
            target = None
        normalized = self.store.set_rfid_mapping(code, {
            "material": material,
            "brand": brand,
            "name": name,
            "target_temp": target,
        })
        if target is not None and str(material).strip().upper() not in self.store.materials:
            self.store.set_material(material, target)
        self._apply_new_mapping(normalized)
        self._info(gcmd, "Saved RFID mapping %s" % normalized)

    def cmd_rfid_map_delete(self, gcmd):
        code = self._param(gcmd, "CODE")
        if not code:
            raise gcmd.error("[BOX]: CODE is required")
        normalized = self.store.delete_rfid_mapping(code)
        self._info(gcmd, "Deleted RFID mapping %s" % normalized)

    @staticmethod
    def _param(gcmd, name):
        value = gcmd.get_command_parameters().get(name)
        if isinstance(value, str) and value.startswith("="):
            value = value[1:]
        if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except ValueError:
                pass
        return value

    @staticmethod
    def _normal_color(value):
        if value is None:
            return None
        text = str(value).strip().upper()
        if text.startswith("#"):
            text = text[1:]
        elif len(text) == 7:
            # Creality/K2-RFID stores 0RRGGBB; RGB starts at the second nibble.
            text = text[1:]
        if len(text) != 6:
            return None
        try:
            int(text, 16)
        except ValueError:
            return None
        return "#" + text

    # ------------------------------------------------------------------
    # RFID metadata and optional Spoolman association
    # ------------------------------------------------------------------

    def _initialize_rfid(self):
        """Apply persisted reader policy and establish presence baselines.

        With startup reading on, unread spools are then read, but only when
        no box's hub and not the printhead hold filament: a read spins
        filament out toward the shared path, and CFS boxes don't know about
        each other, so it could hit what another box or the External spool
        has loaded.
        """
        reading = self.rfid_startup_reading_enabled
        unread = {}
        clear = True
        for address, driver in sorted(self.drivers.items()):
            try:
                local_mask = self._initialize_box_rfid(address, driver)
                if reading:
                    hub = self._require_reply(
                        driver.query_hub_mask(timeout=0.5),
                        "box %d hub query" % address)
                    clear = clear and not hub.value & 0x0F
                    if local_mask:
                        unread[address] = self._unread_rfid(
                            address, driver, local_mask)
            except Exception as exc:
                clear = False
                self._warn("RFID initialization failed on box %d: %s" % (
                    address, exc))
        unread = {address: mask for address, mask in unread.items() if mask}
        if unread:
            detected, error = self.get_filament_sensor_state()
            if not clear or detected is not False or error:
                _klog("startup RFID reads skipped: filament may be in the "
                      "path (hubs clear=%s printhead=%s %s)",
                      clear, detected, error or "", level=logging.info)
                unread = {}
        for address, mask in sorted(unread.items()):
            try:
                self._force_rfid_results(
                    address, self.drivers[address], mask, "startup")
            except Exception as exc:
                self._warn("RFID initialization failed on box %d: %s" % (
                    address, exc))
        self.snapshot = replace(
            self.snapshot, slot_mask=self._presence_mask())

    def _initialize_box_rfid(self, address, driver):
        """Send the reader policy and take the presence baseline.

        At startup and when a box reconnects. Returns the inlet mask.
        """
        reply = driver.set_rfid_insert_reading(
            self.rfid_insert_reading_enabled, timeout=1.0)
        self._require_reply(reply, "box %d RFID insertion policy" % address)
        slots = self._require_reply(
            driver.query_slot_mask(timeout=0.5),
            "box %d RFID presence baseline" % address)
        local_mask = slots.value & 0x0F
        self.rfid_presence[address] = local_mask
        return local_mask

    def _unread_rfid(self, address, driver, local_mask):
        """Present slots the CFS holds no RFID record for."""
        cached = self._require_reply(
            driver.query_rfid_records(local_mask, timeout=0.5),
            "box %d cached RFID query" % address)
        unread = 0
        for local, name in enumerate(box_protocol.RFID_SLOT_NAMES):
            bit = 1 << local
            if not local_mask & bit:
                continue
            record = cached.records.get(name, "").strip("\x00")
            if (record.lower() != "busy" and not self._rfid_record_ready(
                    (record, cached.fields.get(name)))):
                unread |= bit
        return unread

    def _presence_mask(self):
        mask = 0
        for address, local_mask in self.rfid_presence.items():
            if self._box_online(address):
                mask |= (local_mask & 0x0F) << ((address - 1) * SLOTS_PER_BOX)
        return mask

    def _clear_rfid_watch(self, slot):
        self.rfid_pending.discard(slot)
        self.rfid_snapshot.pop(slot, None)
        self.rfid_seen_invalid.discard(slot)

    def _snapshot_rfid_cache(self, slot):
        try:
            sample = self._query_rfid_sample(slot)
        except Exception:
            return
        if sample is None:
            return
        self.rfid_snapshot[slot] = self._rfid_cache_key(sample)
        if not self._rfid_record_ready(sample):
            self.rfid_seen_invalid.add(slot)

    def _rfid_inserted(self, slot):
        self.rfid_percent.pop(slot, None)
        self._invalidate_spoolman(slot)
        self.rfid_snapshot.pop(slot, None)
        self.rfid_seen_invalid.discard(slot)
        if self.rfid_insert_reading_enabled:
            self.rfid_pending.add(slot)
            self._snapshot_rfid_cache(slot)

    def _rfid_removed(self, slot):
        self._clear_rfid_watch(slot)
        self.rfid_percent.pop(slot, None)
        self._invalidate_spoolman(slot)

    def _reconcile_presence(self, address, current):
        current &= 0x0F
        previous = self.rfid_presence.get(address, current)
        self.rfid_presence[address] = current
        changed = previous ^ current
        for local in range(SLOTS_PER_BOX):
            bit = 1 << local
            if not changed & bit:
                continue
            slot = self._global_slot(address, local)
            if current & bit:
                self._rfid_inserted(slot)
            else:
                self._rfid_removed(slot)

    def _query_presence(self, address, driver):
        reply = driver.query_slot_mask(timeout=0.5)
        if reply is not None and reply.status == box_protocol.STATUS_OK:
            self._reconcile_presence(address, reply.value)
        return reply

    def _handle_slot_events(self, address, events):
        for local, event in enumerate(events):
            if event == 0:
                continue
            slot = self._global_slot(address, local)
            if event == 1:
                self._rfid_inserted(slot)
            elif event == 2:
                self._rfid_removed(slot)
            elif event == 3 and self.rfid_insert_reading_enabled:
                self._read_rfid_result(slot)

    def _pending_rfid_slots(self, address):
        first = (address - 1) * SLOTS_PER_BOX
        return tuple(
            slot for slot in self.rfid_pending
            if first <= slot < first + SLOTS_PER_BOX)

    @staticmethod
    def _rfid_cache_key(sample):
        if not sample:
            return None
        return (sample[0] or "").strip("\x00")

    @staticmethod
    def _rfid_record_ready(sample):
        if not sample:
            return False
        record, fields = sample
        return len((record or "").strip("\x00")) == 40 and bool(fields)

    def _rfid_should_apply(self, slot, sample):
        if not self._rfid_record_ready(sample):
            return False
        if slot in self.rfid_seen_invalid:
            return True
        if slot not in self.rfid_snapshot:
            return False
        return self._rfid_cache_key(sample) != self.rfid_snapshot[slot]

    def _watch_pending_rfid(self, address, driver):
        slots = self._pending_rfid_slots(address)
        if not slots:
            return
        try:
            reply = driver.query_rfid_records(timeout=0.5)
        except Exception:
            return
        if reply is None or reply.status != box_protocol.STATUS_OK:
            return
        for slot in slots:
            _, local = self._address_slot(slot)
            name = box_protocol.RFID_SLOT_NAMES[local]
            sample = (
                reply.records.get(name, "").strip("\x00"),
                reply.fields.get(name),
            )
            if not self._rfid_record_ready(sample):
                self.rfid_seen_invalid.add(slot)

    def _finish_pending_rfid(self, address, state_reply):
        if state_reply.box_state == box_protocol.BOX_STATE_PRELOAD:
            return
        for slot in self._pending_rfid_slots(address):
            self._read_rfid_result(slot)

    def _stalled_motors(self, address, driver):
        """Sent once, in the next state reply after the stall."""
        try:
            reply = self._query_box_snapshot(address, driver, False)
        except Exception as exc:
            _klog("box %d stalled-motor query failed: %s", address, exc)
            return 0
        if reply is None or reply is _DRAINING:
            return 0
        return box_protocol.stalled_motors(reply)

    def _query_box_snapshot(self, address, driver, include_topology):
        """The box's state reply, None when it did not answer, or _DRAINING.

        Presence and RFID work is contained: its failure never discards a
        reply the box sent, which would count the box as missed.
        """
        if include_topology:
            self._contained("box %d presence" % address, self._query_presence,
                            address, driver)
        for _attempt in range(STATE_EVENT_DRAIN):
            self._contained("box %d rfid watch" % address,
                            self._watch_pending_rfid, address, driver)
            reply = driver.query_box_state(timeout=0.5)
            if reply is None:
                return None
            if reply.slot_events is None:
                self._contained("box %d rfid result" % address,
                                self._finish_pending_rfid, address, reply)
                return reply
            self._contained("box %d presence" % address, self._query_presence,
                            address, driver)
            self._contained("box %d slot events" % address,
                            self._handle_slot_events, address,
                            reply.slot_events)
        _klog("box %d slot events did not drain after %d reads",
             address, STATE_EVENT_DRAIN, level=logging.warning)
        return _DRAINING

    @staticmethod
    def _clean_rfid(value):
        return str(value or "").strip().strip("\x00")

    @staticmethod
    def _rfid_map_command(code):
        return (
            '_BOX_RFID_MAP_SET CODE=%s MATERIAL=PLA BRAND="Brand" '
            'NAME="Name" TARGET_TEMP=220' % code)

    def _resolve_rfid(self, raw_code):
        code, product = resolve_material(raw_code)
        mapping = self.store.rfid_mapping(code)
        if mapping:
            return code, mapping
        if product:
            return code, {
                "material": product["material"],
                "brand": product["brand"],
                "name": product["name"],
                "target_temp": product["default_temp"],
            }
        return code, None

    def _record_unknown_rfid(self, slot, code, raw_code, record, fields):
        if not code:
            return
        slot_key = self._runtime_slot_key(slot)
        previous = self.unknown_rfid.get(slot_key)
        self.unknown_rfid[slot_key] = {
            "code": code,
            "raw_code": raw_code,
            "record": record,
            "fields": dict(fields),
        }
        if previous and previous.get("code") == code:
            return
        self._warn("Unknown RFID tag in %s: CODE=%s" % (
            self.slot_label(self._runtime_slot(slot_key)), code))
        self._warn("Map it with: %s" % self._rfid_map_command(code))

    def _ensure_material(self, material, target=None):
        material = str(material or "").strip().upper()
        if material and material not in self.store.materials:
            fallback = target is None
            target = self.change_engine.default_temp if fallback else target
            self.store.set_material(material, target)
            if fallback:
                self._info(
                    self.gcode,
                    "New material %s: flush temperature defaulted to %dC. "
                    "Update it in the slot UI or run _BOX_MATERIAL_SET "
                    'MATERIAL="%s" TARGET_TEMP=230'
                    % (material, target, material))

    def _rfid_profile(self, fields):
        raw_code = self._clean_rfid(fields["mat_id"]).upper()
        code, resolved = self._resolve_rfid(raw_code)
        reserve = self._clean_rfid(fields["reserve"]).upper()
        color = self._normal_color(fields["color"]) or ""
        if resolved:
            material = str(resolved["material"]).strip().upper()
            brand = str(resolved.get("brand", "")).strip()
            name = str(resolved.get("name", "")).strip()
            target = resolved.get("target_temp")
        else:
            material = raw_code
            brand = self._clean_rfid(fields["supplier"])
            name = ""
            target = None
        return ({
            "material": material, "color": color, "brand": brand,
            "name": name, "spoolman_id": None,
            "rfid_reserve": reserve,
        }, code, raw_code, target, resolved is not None)

    @staticmethod
    def _profile_summary(profile):
        title = " ".join(
            part for part in (profile.get("brand"), profile.get("name")) if part)
        details = ", ".join(
            part for part in (profile.get("material"), profile.get("color")) if part)
        if title and details:
            return "%s (%s)" % (title, details)
        return title or details

    def _apply_rfid_profile(self, slot, fields):
        """Save the profile the tag's code maps to; returns it, or None."""
        profile, _code, _raw_code, target, resolved = self._rfid_profile(fields)
        if not resolved:
            return None
        self._ensure_material(profile["material"], target)
        self.unknown_rfid.pop(self._runtime_slot_key(slot), None)
        self.set_profile(slot, profile)
        return profile

    def _invalidate_spoolman(self, slot):
        slot_key = self._runtime_slot_key(slot)
        self.spoolman_tokens[slot_key] = (
            self.spoolman_tokens.get(slot_key, 0) + 1)

    def _apply_rfid_record(self, slot, record, fields):
        record = record.rstrip("\x00")
        if len(record) != 40 or not record.strip():
            return False
        display_slot = self._runtime_slot(slot)
        requested_id = _spool_id_from_reserve(fields.get("reserve"))
        if requested_id is not None:
            self._request_spoolman_profile(
                slot, requested_id, record, dict(fields))
            return True
        self._invalidate_spoolman(slot)
        raw_code = self._clean_rfid(fields.get("mat_id")).upper()
        normalized, resolved = self._resolve_rfid(raw_code)
        if not resolved:
            self._record_unknown_rfid(
                slot, normalized, raw_code, record, fields)
            return True
        profile = self._apply_rfid_profile(slot, fields)
        if profile is not None:
            self._info(self.gcode, "%s: RFID %s applied, code %s" % (
                self.slot_label(display_slot), self._profile_summary(profile),
                self._clean_rfid(fields.get("mat_id"))))
        return True

    def _apply_new_mapping(self, code):
        mapping = self.store.rfid_mapping(code)
        if not mapping:
            return
        self._ensure_material(mapping["material"], mapping.get("target_temp"))
        for slot, unknown in list(self.unknown_rfid.items()):
            if unknown.get("code") != code:
                continue
            fields = unknown.get("fields")
            if not isinstance(fields, dict):
                continue
            self._invalidate_spoolman(slot)
            self._apply_rfid_profile(slot, fields)

    def _query_rfid_sample(self, slot):
        address, local = self._address_slot(slot)
        driver = self.drivers.get(address)
        if driver is None:
            return None
        name = box_protocol.RFID_SLOT_NAMES[local]
        reply = driver.query_rfid_records(1 << local, timeout=0.5)
        if reply is None or reply.status != box_protocol.STATUS_OK:
            return None
        return (
            reply.records.get(name, "").strip("\x00"),
            reply.fields.get(name),
        )

    def _read_rfid_result(self, slot):
        sample = self._query_rfid_sample(slot)
        if sample is None:
            return "error"
        record, fields = sample
        if record.lower() == "busy":
            self.rfid_seen_invalid.add(slot)
            return "busy"
        if record.lower() == "none":
            self._rfid_removed(slot)
            return "none"
        if not self._rfid_record_ready(sample):
            self.rfid_seen_invalid.add(slot)
            self.rfid_percent.pop(slot, None)
            return record.lower() or "unknown"
        if not self._rfid_should_apply(slot, sample):
            return "stale"
        self._clear_rfid_watch(slot)
        if not self._apply_rfid_record(slot, record, fields):
            self.rfid_percent.pop(slot, None)
            return "invalid"
        address, local = self._address_slot(slot)
        self._mirror_rfid_remaining(address, self.drivers[address], 1 << local)
        return "record"

    def _force_rfid_results(self, address, driver, mask, reason):
        selected = tuple(
            local for local in range(SLOTS_PER_BOX) if mask & (1 << local))
        tools = tuple(self._global_slot(address, local) for local in selected)
        self._info(
            self.gcode, "Reading RFID for Box %d slot %s (%s)" % (
                address, ", ".join(str(local + 1) for local in selected), reason))
        self._require_reply(
            driver.force_rfid_read(mask),
            "box %d forced RFID read" % address)
        after = self._require_reply(
            driver.query_rfid_records(mask, timeout=0.5),
            "box %d post-RFID query" % address)
        applied = set()
        for local, slot in zip(selected, tools):
            name = box_protocol.RFID_SLOT_NAMES[local]
            record = after.records.get(name, "").strip("\x00")
            fields = after.fields.get(name)
            if not self._rfid_record_ready((record, fields)):
                _klog("%s forced RFID result was invalid", self.slot_label(slot))
                continue
            if self._apply_rfid_record(slot, record, fields):
                applied.add(slot)
            self._clear_rfid_watch(slot)
        if applied:
            self._mirror_rfid_remaining(address, driver, sum(
                1 << self._address_slot(slot)[1] for slot in applied))
        return applied

    def _refresh_rfid_remaining(self, addresses=None):
        """Mirror each present slot's RFID remaining % from its CFS.

        A slot has a % only while the CFS holds its record. Without one the
        CFS still answers the remaining query, with a placeholder 100, so
        the record is checked first. A busy slot or a failed reply keeps
        the last value.
        """
        if self.operation_depth:
            return
        for address, driver in sorted(self.drivers.items()):
            if addresses is not None and address not in addresses:
                continue
            if not self._box_online(address):
                continue
            present = self.rfid_presence.get(address, 0) & 0x0F
            for local in range(SLOTS_PER_BOX):
                if not present & (1 << local):
                    self.rfid_percent.pop(
                        self._global_slot(address, local), None)
            try:
                self._mirror_rfid_remaining(address, driver, present)
            except Exception as exc:
                _klog("RFID remaining query failed on box %d: %s", address, exc)

    def _mirror_rfid_remaining(self, address, driver, present):
        """The % of the slots in the local mask present, from the CFS."""
        names = box_protocol.RFID_SLOT_NAMES
        if not present:
            return
        records = driver.query_rfid_records(present, timeout=0.5)
        if records is None or records.status != box_protocol.STATUS_OK:
            return
        held = 0
        for local in range(SLOTS_PER_BOX):
            bit = 1 << local
            if not present & bit or names[local] not in records.records:
                continue
            record = records.records[names[local]].strip("\x00")
            if len(record) == 40:
                held |= bit
            elif record.lower() != "busy":
                self.rfid_percent.pop(self._global_slot(address, local), None)
        if not held:
            return
        reply = driver.query_rfid_remaining(held, timeout=0.5)
        if reply is None or reply.status != box_protocol.STATUS_OK:
            return
        for local in range(SLOTS_PER_BOX):
            if not held & (1 << local):
                continue
            slot = self._global_slot(address, local)
            value = reply.values.get(names[local])
            if isinstance(value, int) and 0 <= value <= 100:
                self.rfid_percent[slot] = value
            else:
                self.rfid_percent.pop(slot, None)

    def _external_rfid_record(self, event):
        self._apply_rfid_record(
            EXTERNAL_PROFILE_KEY, event["record_ascii"], event["fields"])

    def _request_spoolman_profile(self, slot, requested_id, record, fields):
        slot_key = self._runtime_slot_key(slot)
        display_slot = self._runtime_slot(slot_key)
        token = self.spoolman_tokens.get(slot_key, 0) + 1
        self.spoolman_tokens[slot_key] = token
        generation = self.spoolman_generation
        self._info(
            self.gcode, "%s: RFID spool ID %d, fetching from Spoolman"
            % (self.slot_label(display_slot), requested_id))

        def worker():
            try:
                spool = _fetch_spoolman_spool(requested_id)
            except Exception:
                spool = None

            def complete(eventtime):
                if (generation != self.spoolman_generation
                        or self.spoolman_tokens.get(slot_key) != token):
                    self._info(
                        self.gcode,
                        "%s: Spoolman ID %d result discarded; slot changed"
                        % (self.slot_label(display_slot), requested_id))
                    return

                try:
                    spool_id = int(spool["id"])
                except (KeyError, TypeError, ValueError):
                    spool_id = None
                filament = spool.get("filament") if isinstance(spool, dict) else None
                filament = filament if isinstance(filament, dict) else {}
                material = str(filament.get("material") or "").strip().upper()
                color = self._normal_color(filament.get("color_hex"))
                if spool_id != requested_id or not material or color is None:
                    applied = self._apply_rfid_profile(slot_key, fields)
                    if applied is None:
                        raw_code = self._clean_rfid(
                            fields.get("mat_id")).upper()
                        code, _resolved = self._resolve_rfid(raw_code)
                        self._record_unknown_rfid(
                            slot_key, code, raw_code, record, fields)
                    self._info(
                        self.gcode,
                        "%s: Spoolman ID %d unavailable or incomplete; %s"
                        % (self.slot_label(display_slot), requested_id,
                           "RFID %s applied" % self._profile_summary(applied)
                           if applied is not None else "RFID mapping required"))
                    return
                vendor = filament.get("vendor")
                vendor = vendor if isinstance(vendor, dict) else {}
                reserve = self._clean_rfid(fields.get("reserve")).upper()
                profile = {
                    "spoolman_id": spool_id,
                    "material": material,
                    "color": color,
                    "brand": str(vendor.get("name") or "").strip(),
                    "name": str(filament.get("name") or "").strip(),
                    "rfid_reserve": reserve,
                }
                self._ensure_material(material)
                self.unknown_rfid.pop(slot_key, None)
                self.set_profile(slot_key, profile)
                raw_code = self._clean_rfid(fields.get("mat_id")).upper()
                code, resolved = self._resolve_rfid(raw_code)
                summary = self._profile_summary(profile)
                if resolved:
                    message = "Spoolman ID %d %s applied" % (spool_id, summary)
                else:
                    message = (
                        "unknown RFID code %s resolved via Spoolman ID %d: %s"
                        % (code, spool_id, summary))
                self._info(self.gcode, "%s: %s" % (
                    self.slot_label(display_slot), message))
                if self.snapshot.loaded_slot == display_slot:
                    self.activate_spool(display_slot)

            self.reactor.register_async_callback(complete)

        threading.Thread(
            target=worker, name="box-spoolman-rfid", daemon=True).start()

    # ------------------------------------------------------------------
    # User-facing diagnostics
    # ------------------------------------------------------------------

    def cmd_debug(self, gcmd):
        raw = bool(gcmd.get_int("RAW", 0, minval=0, maxval=1))
        self._info(gcmd, "=== BOX DEBUG DUMP ===")
        self._info(gcmd, "drivers=%s known=%s errors=%s" % (
            sorted(self.drivers), sorted(self.store.known_addresses),
            list(self.address_errors) or "none"))
        self._info(
            gcmd, "operation_depth=%d tracking=%s path=%s runout=%s origin=%s tail=%s faults=%d" % (
                self.operation_depth, self.tracking_owner, self.path_owner,
                self.runout_active, self.runout_origin, self.tail_slot,
                self.fault_generation))
        try:
            live = self.read_live_state(include_topology=True, strict=False)
            self._info(gcmd, "live loaded=%s present=0x%04x tracking=%s" % (
                live.loaded_slot, live.slot_mask, live.tracking))
            if live.conflict:
                self._info(gcmd, "conflict lanes=0x%04x: %s" % (
                    live.loaded_mask, live.conflict))
            self._info(gcmd, "sensor=%s sensor_error=%s cut_sensor=%s" % (
                live.filament_detected, live.filament_sensor_error,
                self.get_cut_sensor_state()))
            self._info(gcmd, "status=%s state=%s temp=%sC humidity=%s%%" % (
                box_protocol.status_name(live.status_code),
                box_protocol.state_name(live.state_code), live.temp_c,
                live.humidity_pct))
        except Exception as exc:
            self._info(gcmd, "live state FAILED: %s" % exc)

        self._info(gcmd, "hotend=%s feed_pending=%s" % (
            self.hotend_filament(),
            self._runtime_slot(self.store.setting("hotend_feed_pending"))))
        self._info(gcmd, "change=%s" % self.change_engine.debug_status())
        self._info(gcmd, "clog=%s" % self._clog_status())
        for address, driver in sorted(self.drivers.items()):
            self._info(gcmd, "--- Box %d (slot indices %d-%d) ---" % (
                address, self._global_slot(address, 0),
                self._global_slot(address, 3)))
            queries = (
                ("slots", driver.query_slot_mask),
                ("hub", driver.query_hub_mask),
                ("buffer", driver.query_buffer),
                ("encoder", driver.query_encoder),
                ("state", driver.query_box_state),
                ("rfid", driver.query_rfid_records),
                ("remaining", driver.query_rfid_remaining),
            )
            for label, query in queries:
                try:
                    reply = query(timeout=0.5)
                    if reply is None:
                        detail = "NO RESPONSE"
                    elif label == "state" and reply.slot_events is not None:
                        detail = "status=%s events=%s" % (
                            box_protocol.status_name(reply.status),
                            reply.slot_events)
                    elif label == "state":
                        detail = "status=%s state=%s temp=%s humidity=%s" % (
                            box_protocol.status_name(reply.status),
                            box_protocol.state_name(reply.box_state),
                            reply.temp_c, reply.humidity_pct)
                    elif label == "rfid":
                        detail = "status=%s records=%s" % (
                            box_protocol.status_name(reply.status), reply.records)
                    elif label == "remaining":
                        detail = "status=%s values=%s" % (
                            box_protocol.status_name(reply.status), reply.values)
                    else:
                        detail = "status=%s value=%s" % (
                            box_protocol.status_name(reply.status), reply.value)
                    if raw and reply is not None:
                        detail += " raw=" + reply.raw.hex()
                except Exception as exc:
                    detail = "FAILED: %s" % exc
                self._info(gcmd, "%s: %s" % (label, detail))
        if self.unknown_rfid:
            for slot, item in sorted(
                    self.unknown_rfid.items(),
                    key=lambda entry: self._runtime_slot(entry[0])):
                self._info(gcmd, "unknown RFID in %s CODE=%s" % (
                    self.slot_label(self._runtime_slot(slot)), item["code"]))
                self._info(gcmd, "map: %s" % self._rfid_map_command(item["code"]))
        else:
            self._info(gcmd, "unknown RFID: none")
        self._info(gcmd, "=== END DEBUG DUMP ===")

    def _info(self, responder, message):
        responder.respond_info(self.CONSOLE_PREFIX + str(message))

    def _warn(self, message):
        for line in str(message).splitlines():
            self.gcode.respond_raw("!! " + self.CONSOLE_PREFIX + line)

    # ------------------------------------------------------------------
    # Box-owned cutter, cleaning, and wastebin motion
    # ------------------------------------------------------------------

    def _cut_sensor_callback(self, eventtime, state):
        self.cut_sensor_state = bool(state)

    @contextmanager
    def part_fan_override(self, speed, restore=None):
        fan = self.printer.lookup_object("fan")
        saved = fan.get_status(self.reactor.monotonic())["value"]
        restore = saved if restore is None else restore
        setter = fan.fan.set_speed_from_command
        try:
            setter(max(0.0, min(float(speed), 1.0)))
            yield
        finally:
            setter(max(0.0, min(float(restore), 1.0)))

    def _zone(self):
        return self.printer.lookup_object("extended_zone_transform")

    @contextmanager
    def _clean_motion(self, name):
        with saved_motion(self.printer, self.gcode, name):
            set_service_motion(
                self.gcode, CLEAN_LIMIT_VELOCITY, CLEAN_LIMIT_ACCEL,
                CLEAN_MINIMUM_CRUISE_RATIO, CLEAN_LIMIT_SCV)
            yield

    @staticmethod
    def _wipe_phase():
        """Random zig-zag phase, so the turnarounds don't land on the same
        spots of the pad every clean."""
        return random.uniform(0.0, 2.0 * math.pi)

    def nozzle_clean(self):
        toolhead = self.printer.lookup_object("toolhead")
        zone = self._zone()
        with self._clean_motion("_box_clean_limits"):
            self.move_to_wastebin()
            left = self.clean_pad_left_x
            right = self.clean_pad_right_x
            front = self.clean_pad_front_y
            back = self.wastebin_y
            y_steps = max(
                1, int(round((back - front) / CLEAN_SERPENTINE_Y_STEP)))
            center_x = (left + right) / 2.0
            amplitude_x = (right - left) / 2.0
            segments = y_steps * 8
            zone.service_move(y=back, velocity=self.travel_velocity)
            zone.service_move(x=left, velocity=self.clean_velocity)
            for _index in range(CLEAN_SCRAPER_PASSES):
                zone.service_move(
                    x=self.wastebin_x, velocity=self.clean_velocity)
                zone.service_move(x=left, velocity=self.clean_velocity)
            # The scraper leaves the nozzle at the left edge; slide along the
            # back row to wherever this clean's zig-zag starts.
            phase = self._wipe_phase()
            zone.service_move(
                x=center_x + amplitude_x * math.cos(phase),
                velocity=self.clean_velocity)
            for pass_index in range(self.clean_pad_passes):
                direction = -1.0 if (pass_index * y_steps) % 2 else 1.0
                for index in range(1, segments + 1):
                    progress = index / float(segments)
                    x = center_x + direction * amplitude_x * math.cos(
                        math.pi * y_steps * progress + phase)
                    y = back + (front - back) * progress
                    zone.service_move(
                        x=x, y=y, velocity=self.clean_velocity)
                zone.service_move(x=center_x, velocity=self.clean_velocity)
                zone.service_move(y=back, velocity=self.clean_velocity)
            self.move_to_wastebin()
            toolhead.wait_moves()

    def flush_clean_snap(self, fan_after=None):
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=_box_snap_clean")
        try:
            with self.part_fan_override(self.snap_fan_speed, restore=fan_after):
                self.gcode.run_script_from_command(
                    "G4 P%d" % self.snap_fan_dwell_ms)
                self.gcode.run_script_from_command("M83")
                self.gcode.run_script_from_command(
                    "G1 E-%.1f F%.0f" % (
                        SNAP_RETRACT_MM, self.retract_velocity))
                toolhead.wait_moves()
                self.nozzle_clean()
                toolhead.wait_moves()
        finally:
            self.gcode.run_script_from_command(
                "RESTORE_GCODE_STATE NAME=_box_snap_clean MOVE=0")

    def move_to_wastebin(self):
        toolhead = self.printer.lookup_object("toolhead")
        homed = toolhead.get_status(
            self.reactor.monotonic()).get("homed_axes", "")
        # Cleaning may run inside HOME_IF_NEEDED during Z homing.
        if "x" not in homed or "y" not in homed:
            self.gcode.run_script_from_command("HOME_IF_NEEDED AXIS=XY")
        zone = self._zone()
        position = zone.service_position()
        if (abs(position[0] - self.wastebin_x) < 1.0e-6
                and abs(position[1] - self.wastebin_y) < 1.0e-6):
            return
        with self._clean_motion("_box_wastebin_limits"):
            # The flapper must be struck in X. It reaches forward to about
            # Y355 left of about X140, so stage diagonally at the approach
            # point (clear at any X), climb Y in the clear X column, then
            # make the X stroke. Already at wastebin Y, only the X stroke is
            # needed. Like every flapper or pad contact, the stroke uses
            # clean_velocity.
            if abs(position[1] - self.wastebin_y) >= 1.0e-6:
                approach_x = self.wastebin_approach_x
                if abs(position[0] - approach_x) >= 1.0e-6:
                    zone.service_move(
                        x=approach_x, y=self.wastebin_approach_y,
                        velocity=self.travel_velocity)
                zone.service_move(
                    y=self.wastebin_y, velocity=self.travel_velocity)
            zone.service_move(x=self.wastebin_x, velocity=self.clean_velocity)

    def filament_retry_motion(self, message):
        toolhead = self.printer.lookup_object("toolhead")
        homed = toolhead.get_status(
            self.reactor.monotonic()).get("homed_axes", "")
        if "x" not in homed or "y" not in homed:
            return False
        self._info(self.gcode, message)
        zone = self._zone()
        with self._clean_motion("_box_filament_retry"):
            self.move_to_wastebin()
            for move in self.filament_retry_moves:
                zone.service_move(velocity=self.travel_velocity, **move)
            self.move_to_wastebin()
            toolhead.wait_moves()
        return True

    def cut_filament(self, force=False):
        detected, sensor_error = self.get_filament_sensor_state()
        if not force and sensor_error is None and not detected:
            self._info(self.gcode, "No filament loaded; skipping cut")
            return
        if self.cut_x is None:
            raise BoxError("cut_pos_x is not defined in [box]")

        toolhead = self.printer.lookup_object("toolhead")
        zone = self._zone()
        with saved_motion(self.printer, self.gcode, "_box_cut_limits"):
            lifted_z = self._lift_for_cut(toolhead)
            try:
                self.gcode.run_script_from_command("HOME_IF_NEEDED AXIS=XY")
                with zone.widened_x_min(self.cut_x - 5.0):
                    set_service_motion(
                        self.gcode, CUT_LIMIT_VELOCITY, self.cut_accel,
                        CUT_LIMIT_CRUISE, CUT_LIMIT_SCV)
                    self._cut_strokes(toolhead, zone)
            finally:
                if lifted_z is not None:
                    try:
                        self.gcode.run_script_from_command("G90")
                        self.gcode.run_script_from_command(
                            "G0 Z%.3f F%.0f" % (lifted_z, self.z_velocity))
                        toolhead.wait_moves()
                    except Exception:
                        _klog("failed restoring cut Z", level=logging.exception)

    def _cut_strokes(self, toolhead, zone):
        zone.service_move(
            x=self.pre_cut_x, y=self.cut_y, velocity=self.travel_velocity)
        toolhead.wait_moves()
        if not self.get_cut_sensor_state():
            raise BoxError("Cut sensor is not in standby")

        self._info(self.gcode, "Cutting filament")
        returned = False
        for attempt in range(3):
            if attempt:
                self._info(self.gcode, "Cut retry %d/3" % (attempt + 1))
            zone.service_move(x=self.cut_x, velocity=self.cut_velocity)
            toolhead.wait_moves()
            zone.service_move(x=self.pre_cut_x, velocity=self.travel_velocity)
            toolhead.wait_moves()
            if self._wait_cut_return(1.0):
                returned = True
            self._cut_extruder_jog(
                toolhead, -CUT_POST_RETRACT_MM, self.retract_velocity)
            if self._wait_cut_return(CUT_RETURN_WAIT):
                returned = True
                break
            if attempt < 2:
                self._cut_extruder_jog(
                    toolhead, CUT_POST_RETRACT_MM, self.retract_velocity)
                self.reactor.pause(
                    self.reactor.monotonic() + CUT_RETRY_SETTLE)

        if not returned:
            self._warn(
                "Cut attempts exhausted and the cutter sensor did not return "
                "to standby; proceeding as configured")

    def _lift_for_cut(self, toolhead):
        status = toolhead.get_status(self.reactor.monotonic())
        if "z" not in status.get("homed_axes", ""):
            return None
        current = toolhead.get_position()[2]
        if current >= CUT_SAFE_Z:
            return None
        self.gcode.run_script_from_command("G90")
        self.gcode.run_script_from_command(
            "G0 Z%.3f F%.0f" % (CUT_SAFE_Z, self.z_velocity))
        toolhead.wait_moves()
        return current

    def _wait_cut_return(self, timeout):
        deadline = self.reactor.monotonic() + timeout
        while self.reactor.monotonic() < deadline:
            if self.get_cut_sensor_state():
                return True
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        return False

    def _cut_extruder_jog(self, toolhead, distance, feed):
        extruder = self.printer.lookup_object("extruder")
        temperature = extruder.get_heater().get_status(0)["temperature"]
        if temperature <= 170:
            return False
        self.gcode.run_script_from_command("G92 E0")
        self.gcode.run_script_from_command(
            "G1 E%.4f F%.0f" % (distance, feed))
        toolhead.wait_moves()
        return True

    def pause_print(self, synchronous=False, skip_retract_wipe=False,
                    release_extruder=False):
        if synchronous:
            if not self.pause_resume.is_paused:
                command = (
                    "PAUSE SKIP_RETRACT_WIPE=1"
                    if skip_retract_wipe else "PAUSE")
                self.gcode.run_script_from_command(command)
            return self.pause_resume.is_paused
        if (self.pause_resume.is_paused
                or self.pause_resume.pause_command_sent):
            return False
        self.pause_resume.send_pause_command()
        script = "PAUSE"
        if release_extruder:
            # The nozzle is empty and the user pulls the filament out by
            # hand, so nothing is retracted and the gears are let go.
            script = ("PAUSE SKIP_RETRACT_WIPE=1\n"
                      "SET_STEPPER_ENABLE STEPPER=extruder ENABLE=0")
        self.reactor.register_async_callback(
            lambda _eventtime: self.gcode.run_script(script))
        return True

    def _clear_runout_state(self):
        self.runout_active = False
        self.runout_origin = None
        self.runout_key = None

    # ------------------------------------------------------------------
    # Runout tail (see TAIL_SETTING)
    # ------------------------------------------------------------------

    @property
    def tail_slot(self):
        key = self.store.setting(TAIL_SETTING)
        if key is None:
            return None
        for slot in self.physical_slots:
            if self._profile_key(slot) == key:
                return slot
        return None

    def _note_tail(self, slot, ran_out_now=False):
        key = self._profile_key(slot)
        saved = self.store.setting(TAIL_SETTING)
        if saved != key:
            self.store.set_setting(TAIL_SETTING, key)
            self.tail_baseline = None
            # Only a runout seen as it happened starts with a full tail; a
            # tail saved under another key is an older one renamed.
            self.tail_travel = 0.0 if ran_out_now and saved is None else None

    def _forget_tail(self):
        self.store.clear_settings(TAIL_SETTING)
        self.tail_baseline = None
        self.tail_travel = None

    def _printhead_cleared(self):
        """The printhead sensor's own edge, so no poll can miss it."""
        tail = self.tail_slot
        self._forget_tail()
        if tail is not None and self.path_owner == tail:
            self.path_owner = None
        self.change_engine.printhead_cleared()

    def _restore_tail(self, detected, loaded):
        if self.store.setting(TAIL_SETTING) is None:
            return
        if detected is False:
            self._forget_tail()
            return
        tail = self.tail_slot
        if tail is None or not detected:
            return
        if self.path_owner is None and loaded < 0:
            # After a restart the CFS reports nothing loaded for a tail.
            self.path_owner = tail
        if self.path_owner == tail and not self.runout_active:
            self.runout_active = True
            self.runout_origin = tail
            self.runout_key = (self._address_slot(tail)[0], self.tracking_epoch)

    def snapped_slot(self, live):
        """The loaded slot when only a broken-off piece is left of it.

        The CFS still sees filament in the slot's hub lane, but the slot's
        inlet and the printhead sensor see none. The CFS refuses to pull that
        piece back and the extruder cannot reach it.
        """
        if live.filament_detected is not False:
            return None
        return self._inlet_dark(live)

    def _inlet_dark(self, live):
        """The loaded slot when its own inlet reads empty, else None.

        Only a box that answers and has a presence reading counts.
        """
        slot = live.loaded_slot
        if not self.is_physical_slot(slot) or live.slot_mask & (1 << slot):
            return None
        address, _local = self._address_slot(slot)
        if not self._box_online(address) or address not in self.rfid_presence:
            return None
        return slot

    def _snapped_text(self, slot):
        _address, local = self._address_slot(slot)
        return ("Filament snapped",
                "A piece of slot %d's filament is stuck between the slot and "
                "the hub. Pull it out by hand." % (local + 1))

    def _snapped_detail(self, slot):
        return "%s. %s" % self._snapped_text(slot)

    def _watch_snapped(self, live):
        current = self.snapped
        if current is not None:
            address = self._address_slot(current)[0]
            if (self._box_online(address)
                    and address not in (self.box_replies or {})):
                return  # One missed reply says nothing about the piece.
        slot = self.snapped_slot(live)
        if slot is not None and slot != self.snapped:
            self._warn("Box %d: %s" % (
                self._address_slot(slot)[0], self._snapped_detail(slot)))
        self.snapped = slot

    def _snapped_in_retract(self, address, driver, local):
        """After a retract that never cleared the hub: did the filament snap?

        The inlet is dark but the slot's hub lane still sees filament.
        """
        bit = 1 << local
        try:
            inlet = self._query_presence(address, driver)
            hub = driver.query_hub_mask(timeout=0.5)
        except Exception as exc:
            _klog("box %d snap check failed: %s", address, exc,
                  level=logging.warning)
            return False
        ok = box_protocol.STATUS_OK
        return bool(
            inlet is not None and inlet.status == ok
            and hub is not None and hub.status == ok
            and not (inlet.value or 0) & bit and (hub.value or 0) & bit)

    def tail_loaded(self, live, note=True):
        """True when the loaded filament is a tail.

        A loaded slot with an empty inlet is one as well: the CFS refuses to
        pull that back. note=False only asks, saving nothing.
        """
        slot = live.loaded_slot
        if not self.is_physical_slot(slot):
            return False
        if self.tail_slot == slot:
            return True
        if not live.filament_detected or self._inlet_dark(live) is None:
            return False
        if note:
            self._note_tail(slot)
        return True

    def tail_budget(self):
        """Extruder travel left before a tail must have run out.

        Counted from when this session first saw the tail. At zero the
        loaded filament cannot be that tail, so it is dropped.
        """
        tail = self.tail_slot
        if tail is None:
            return 0.0
        mcu = self.printer.lookup_object("mcu")
        extruder = self.printer.lookup_object("toolhead").get_extruder()
        position = extruder.find_past_position(
            mcu.estimated_print_time(self.reactor.monotonic()))
        if self.tail_baseline is None:
            self.tail_baseline = position
        travel = position - self.tail_baseline
        if self.tail_travel is not None:
            self.tail_travel = travel
        remaining = TAIL_MAX_MM - travel
        if remaining > 0.0:
            return remaining
        self.drop_tail()
        return 0.0

    def tail_remaining(self):
        """Roughly how much tail is still to come, in mm, or None.

        Known only for a runout this session saw happen, on a box whose load
        path length is known.
        """
        tail = self.tail_slot
        if tail is None or self.tail_travel is None:
            return None
        length = self.path_length(self._address_slot(tail)[0])
        if length is None:
            return None
        return int(round(max(0.0, length - self.tail_travel)))

    def drop_tail(self):
        """Stop treating the loaded filament as a tail; it reads as External."""
        tail = self.tail_slot
        if tail is None:
            return
        self._forget_tail()
        if self.path_owner == tail:
            self.path_owner = None
        self._release_box(self._address_slot(tail)[0])
        self._clear_runout_state()
        self.clear_active_spool(tail)
        self.last_loaded_slot = self.external_slot
        self._info(
            self.gcode,
            "%s leftover filament did not run out within %.0f m; treating "
            "the loaded filament as the external spool"
            % (self.slot_label(tail), TAIL_MAX_MM / 1000.0))

    def release_tail(self, slot):
        """Idle the tail's box so its hub feeds nothing in behind the tail."""
        with self._operation():
            self._release_box(self._address_slot(slot)[0])

    def finish_tail(self, slot):
        """The tail has left the printhead; its path is empty."""
        with self._operation():
            self._release_box(self._address_slot(slot)[0])
            live = self.read_live_state(include_topology=False)
            if live.filament_detected is not False:
                raise BoxError(
                    "The printhead sensor still detects filament")
            self._clear_runout_state()
            self.clear_hotend_feed_pending(slot)

    def _release_box(self, address):
        """Return a CFS that feeds nothing to idle, clearing its error.

        The host stops tracking it either way. A busy box is prefeeding and
        the poll releases it later.
        """
        self._clear_tracking_owner(address)
        driver = self.drivers.get(address)
        if driver is None:
            return False
        try:
            reply = driver.set_tracking(None, timeout=1.0)
        except Exception as exc:
            _klog("box %d release failed: %s", address, exc,
                  level=logging.warning)
            return False
        return reply is not None and reply.status == box_protocol.STATUS_OK

    def _release_unused_boxes(self):
        """Idle every box left in an error or print state off the path.

        In either state a CFS starts no prefeed or RFID read for a spool
        that is inserted, and shows its error until something clears it.
        """
        path_address = (
            None if self.path_owner is None
            else self._address_slot(self.path_owner)[0])
        for address, reply in sorted((self.box_replies or {}).items()):
            if (address != path_address and reply.box_state in (
                    box_protocol.BOX_STATE_PRINT,
                    box_protocol.BOX_STATE_ERROR)):
                self._release_box(address)

    def _report_tail_after_print(self, completed):
        tail = self.tail_slot
        if tail is None or (completed and self.unload_after_print_enabled):
            return
        self._info(
            self.gcode,
            "%s ran out and its leftover filament is still loaded. The next "
            "print will use it or purge it. To pull it out by hand, run "
            "BOX_UNLOAD MANUAL=1" % self.slot_label(tail))

    def _set_tracking_owner(self, address, slot, renew=False):
        owner = self.tracking_owner
        if (not renew and owner is not None and owner.address == address
                and owner.slot == slot):
            return owner
        self.tracking_epoch += 1
        owner = TrackingOwner(int(address), int(slot), self.tracking_epoch)
        self.tracking_owner = owner
        self.path_owner = owner.slot
        self.fault_episodes.pop(owner.address, None)
        self._clear_runout_state()
        return owner

    def _clear_tracking_owner(self, address=None, clear_runout=False):
        owner = self.tracking_owner
        if owner is not None and (address is None or owner.address == address):
            self.tracking_epoch += 1
            self.tracking_owner = None
            self.fault_episodes.pop(owner.address, None)
        if clear_runout:
            self._clear_runout_state()

    def _invalidate_tracking_session(self):
        self.tracking_epoch += 1
        self.tracking_owner = None
        self.path_owner = None
        self.fault_episodes.clear()
        self._clear_runout_state()

    def _set_tracking(self, driver, address, local, context):
        reply = self._require_reply(
            driver.set_tracking(local, timeout=1.0), context)
        # The CFS cleared its error, so a detailed message is stale now.
        self.fault_messages.pop(address, None)
        if local is None:
            self._clear_tracking_owner(address)
        else:
            self._set_tracking_owner(
                address, self._global_slot(address, local), renew=True)
        return reply

    def note_external_source(self):
        if self.tracking_owner is None:
            self.tracking_epoch += 1
            self.fault_episodes.clear()
        self._clear_tracking_owner(clear_runout=True)
        self.path_owner = None
        self._forget_tail()

    def _reconcile_tracking_owner(self, replies):
        candidates = []
        for address, reply in replies.items():
            if (reply.status != box_protocol.STATUS_OK
                    or reply.box_state != box_protocol.BOX_STATE_PRINT):
                continue
            local = self._local_from_mask(reply.downstream_mask or 0)
            if local >= 0:
                candidates.append((address, self._global_slot(address, local)))

        owner = self.tracking_owner
        if owner is not None:
            reply = replies.get(owner.address)
            if ((reply is None and candidates) or (
                    reply is not None
                    and reply.status == box_protocol.STATUS_OK
                    and reply.box_state in (
                        box_protocol.BOX_STATE_IDLE,
                        box_protocol.BOX_STATE_PRELOAD,
                        box_protocol.BOX_STATE_RELOAD,
                        box_protocol.BOX_STATE_TEST))):
                self._clear_tracking_owner(owner.address)
                owner = None

        if len(candidates) != 1:
            return
        address, slot = candidates[0]
        if owner is not None and (owner.address, owner.slot) != (address, slot):
            # The CFS's own loaded path is what it feeds and reports faults
            # for; tracking a slot it isn't feeding would misattribute both.
            self._warn("The CFS is feeding %s, not %s as expected. "
                       "Box follows the CFS."
                       % (self.slot_label(slot), self.slot_label(owner.slot)))
        self._set_tracking_owner(address, slot)

    def _fault_key(self, status, category):
        print_epoch = self.printer.lookup_object("print_stats").print_start_time
        return int(status), self.tracking_epoch, print_epoch, category

    def _fatal_episode(self, address, status=None):
        episode = self.fault_episodes.get(address)
        return bool(
            episode is not None
            and episode[-1] == "fatal"
            and (status is None or episode[0] == status))

    def _latch_fatal_fault(self, address, status, reason, detail=None):
        key = self._fault_key(status, "fatal")
        if self.fault_episodes.get(address) == key:
            if detail:
                self.fault_messages[address] = (
                    self.fault_episodes[address], detail)
            return False
        self.fault_episodes[address] = key
        self.fault_messages[address] = (key, detail)
        self.fault_generation += 1
        # Operations prefix their own label: "<slot> load failed (CODE): ..."
        self.last_fatal_reason = detail or box_protocol.status_detail(status)
        stats = self.printer.lookup_object("print_stats")
        if stats.state in ("printing", "paused"):
            pending = self.change_engine.pending
            self.change_engine.block_resume(
                reason,
                target=self.path_owner,
                automatic=(None if pending is not None
                           else self.is_physical_slot(self.path_owner)))
        if stats.state == "printing":
            self.pause_print()
        return True

    def _record_command_fault(self, reply, context, detail=None):
        if reply is None or reply.status not in CFS_COMMAND_FATAL_STATUSES:
            return False
        reason = (
            "Box %d: %s" % (reply.address, detail) if detail
            else box_protocol.status_line(reply.address, reply.status))
        latched = self._latch_fatal_fault(
            reply.address, reply.status, reason, detail)
        if latched:
            _klog(
                "CFS command fault address=%d command=0x%02x status=%s "
                "context=%s raw=%s",
                reply.address, reply.command,
                box_protocol.status_name(reply.status), context,
                reply.raw.hex() if reply.raw else "none",
                level=logging.warning)
        return latched

    def check_operation_abort(self, generation):
        if generation != self.fault_generation:
            raise BoxError(
                self.last_fatal_reason or "CFS fault detected during operation")
        if (self.pause_resume.pause_command_sent
                and not self.pause_resume.is_paused):
            raise BoxError("Pause requested during box operation")

    # ------------------------------------------------------------------
    # Coherent live state and budgeted polling
    # ------------------------------------------------------------------

    @contextmanager
    def _operation(self):
        self.operation_depth += 1
        try:
            yield
        finally:
            self.operation_depth -= 1
            if self.operation_depth == 0:
                self.clog_baseline = None

    def _require_reply(self, reply, context, allowed=(0x00,)):
        if reply is None:
            raise BoxError("CFS did not respond during %s" % context)
        if reply.status not in allowed:
            self._record_command_fault(reply, context)
            raise BoxError(box_protocol.status_detail(reply.status))
        return reply

    @staticmethod
    def _local_from_mask(mask):
        if mask == 0:
            return -1
        if mask & (mask - 1):
            raise BoxError("CFS reports multiple loaded paths (mask=0x%02x)" % mask)
        return mask.bit_length() - 1

    @staticmethod
    def _global_slot(address, local_slot):
        return (address - 1) * SLOTS_PER_BOX + local_slot

    @staticmethod
    def _address_slot(global_slot):
        return global_slot // SLOTS_PER_BOX + 1, global_slot % SLOTS_PER_BOX

    def read_live_state(self, include_topology=True, strict=True):
        """Read one coherent safety snapshot; callers never mix cache epochs.

        The snapshot is always published. With filament at more than one hub
        lane the loaded slot is unknown: strict callers, the operations, then
        get a BoxError; the poll passes strict=False and carries on.
        """
        detected, sensor_error = self.get_filament_sensor_state()
        loaded = -1
        loaded_mask = 0
        replies = {}
        responded = set()
        lanes = {}
        for address, driver in sorted(self.drivers.items()):
            try:
                reply = self._query_box_snapshot(
                    address, driver, include_topology)
            except Exception as exc:
                _klog("box %d status query failed: %s", address, exc,
                     level=logging.warning)
                continue
            if reply is None:
                continue
            responded.add(address)
            if reply is _DRAINING:
                continue
            replies[address] = reply
            mask = (reply.downstream_mask or 0) & 0x0F
            if mask:
                lanes[address] = mask
        self.box_replies = replies
        for address in self.drivers:
            if address in responded:
                self.box_misses[address] = 0
                self.offline_handled.discard(address)
                if address in replies:
                    self.box_last[address] = replies[address]
            else:
                self.box_misses[address] = self.box_misses.get(address, 0) + 1
                if self.box_misses[address] == OFFLINE_POLLS:
                    self._warn("Box %d disconnected" % address)
        conflict = self._update_path_conflict(lanes, replies)
        if conflict is not None:
            # Nothing is inferred from an ambiguous path: ownership, tail
            # and tracking keep what they had until it clears.
            for address, mask in lanes.items():
                loaded_mask |= mask << ((address - 1) * SLOTS_PER_BOX)
            for address, reply in replies.items():
                self._observe_fault(address, reply)
        else:
            for address, mask in lanes.items():
                loaded = self._global_slot(address, mask.bit_length() - 1)
                loaded_mask = 1 << loaded
            if loaded >= 0:
                self.path_owner = loaded
            elif detected is False:
                self.path_owner = None
            self._restore_tail(detected, loaded)
            self._reconcile_tracking_owner(replies)
            for address, reply in replies.items():
                self._observe_fault(address, reply)
            if (detected and self.runout_active
                    and self.path_owner is not None
                    and self.path_owner == self.runout_origin):
                self._note_tail(self.path_owner, ran_out_now=True)

            path_owner = self.path_owner
            if (loaded < 0 and detected is not False
                    and self.is_physical_slot(path_owner)):
                # The box feeding the path missed this reply: keep its slot.
                loaded = path_owner
                loaded_mask = 1 << path_owner

            if loaded < 0 and detected is True:
                loaded = self.external_slot

        topology = self._presence_mask()

        path_address = None
        if self.is_physical_slot(loaded):
            path_address, _local = self._address_slot(loaded)
        # The buffer is shared, so any responding CFS can report it. The same
        # box feeds the legacy top-level temp/RH and status.
        relay_address = path_address
        if relay_address is None and replies:
            relay_address = min(replies)
        state_reply = replies.get(path_address)
        relay_reply = replies.get(relay_address)
        encoder_reply = None
        buffer_reply = None
        # Like the state reads above, a failed query leaves its value unknown
        # rather than keeping the previous snapshot.
        if relay_address in self.drivers:
            try:
                buffer_reply = self.drivers[relay_address].query_buffer(
                    timeout=0.5)
            except Exception as exc:
                _klog("box %d buffer query failed: %s", relay_address, exc,
                      level=logging.warning)
        if (state_reply is not None
                and state_reply.box_state == box_protocol.BOX_STATE_PRINT):
            try:
                encoder_reply = self.drivers[path_address].query_encoder(
                    timeout=0.5)
            except Exception as exc:
                _klog("box %d encoder query failed: %s", path_address, exc,
                      level=logging.warning)

        status_code = state_reply.status if state_reply else None
        state_code = state_reply.box_state if state_reply else None
        owner = self.tracking_owner
        tracking = (
            owner is not None and owner.address == path_address
            and state_code == box_protocol.BOX_STATE_PRINT
            and not self._fatal_episode(path_address, status_code))
        snap = BoxSnapshot(
            data_ready=self.drivers_ready,
            status_code=status_code,
            state_code=state_code,
            temp_c=relay_reply.temp_c if relay_reply else None,
            humidity_pct=relay_reply.humidity_pct if relay_reply else None,
            loaded_slot=loaded,
            loaded_mask=loaded_mask,
            slot_mask=topology,
            tracking=tracking,
            filament_detected=detected,
            filament_sensor_error=sensor_error,
            path_box=path_address,
            encoder_mm=encoder_reply.value if encoder_reply else None,
            buffer_status=buffer_reply.status if buffer_reply else None,
            buffer_state=buffer_reply.value if buffer_reply else None,
            relay_status_code=relay_reply.status if relay_reply else None,
            relay_state_code=relay_reply.box_state if relay_reply else None,
            conflict=None if conflict is None else conflict[1],
        )
        self.snapshot = snap
        if strict and conflict is not None:
            raise BoxError(conflict[1])
        return snap

    def _update_path_conflict(self, lanes, replies):
        """Filament at more than one hub lane, across every box.

        Returns (addresses, message) or None and keeps it in path_conflict.
        A box in the conflict that missed this reply says nothing about it,
        so the conflict holds until every box in it answers.
        """
        previous = self.path_conflict
        # Boxes in the conflict that missed this reply keep their last lanes.
        held = () if previous is None else tuple(
            address for address in previous[0]
            if address not in replies and self._box_online(address))
        for address in held:
            last = self.box_last.get(address)
            mask = 0 if last is None else (last.downstream_mask or 0) & 0x0F
            if mask:
                lanes[address] = mask
        lit = sorted(lanes.items())
        conflict = None
        if len(lit) > 1 or (lit and lit[0][1] & (lit[0][1] - 1)):
            conflict = (tuple(address for address, _mask in lit),
                        self._path_conflict_message(lit))
        elif held:
            conflict = previous
        self.path_conflict = conflict
        if conflict is not None and (
                previous is None or previous[1] != conflict[1]):
            self._warn("%s Loads and unloads are refused until then."
                       % conflict[1])
        elif conflict is None and previous is not None:
            _klog("hub lane conflict cleared", level=logging.info)
            # Its pause changed no slot: RESUME primes what is loaded.
            recovery = self.change_engine.resume_recovery
            if recovery is not None and recovery.reason == PATH_CONFLICT_TITLE:
                self.change_engine.clear_resume_recovery()
        if conflict is None:
            self.conflict_paused = None
        return conflict

    @staticmethod
    def _path_conflict_message(lit):
        def slots(mask):
            return _join_and([str(local + 1) for local in range(SLOTS_PER_BOX)
                              if mask & (1 << local)])
        if len(lit) == 1:
            address, mask = lit[0]
            return ("Box %d sees filament at the hub in slots %s. Pull out "
                    "the filament that shouldn't be there." % (
                        address, slots(mask)))
        return ("Filament is at the hub in %s. Pull out the filament that "
                "shouldn't be there." % _join_and([
                    "Box %d slot%s %s" % (
                        address, "s" if mask & (mask - 1) else "",
                        slots(mask))
                    for address, mask in lit]))

    def _poll(self, eventtime):
        if self.operation_depth:
            return eventtime + 0.25
        if not self.drivers_ready:
            return eventtime + IDLE_POLL
        include_topology = eventtime - self.last_topology_refresh >= TOPOLOGY_POLL
        try:
            snap = self.read_live_state(
                include_topology=include_topology, strict=False)
        except Exception as exc:
            self._log_contained("status poll", exc)
            return eventtime + ERROR_BACKOFF
        self._clear_contained("status poll")
        if include_topology:
            self.last_topology_refresh = eventtime
        # Each step on its own: one failing never starves the rest.
        if snap.conflict is not None:
            self._contained("conflict watch", self.pause_for_path_conflict)
        else:
            # These infer from the loaded slot, unknown during a conflict.
            self._contained("release", self._release_unused_boxes)
            self._contained("offline watch",
                            self._watch_loaded_box_offline, snap)
            self._contained("snapped watch", self._watch_snapped, snap)
            if snap.filament_detected and self.tail_slot is not None:
                self._contained("tail budget", self.tail_budget)
        self._contained("clog check", self._poll_clog, eventtime, snap)
        if eventtime - self.last_rfid_refresh >= RFID_REFRESH:
            self.last_rfid_refresh = eventtime
            self._contained("rfid refresh", self._refresh_rfid_remaining)
        if eventtime - self.last_reconnect >= RECONNECT_INTERVAL:
            self._contained("reconnect", self._reconnect_lost_boxes, eventtime)
        return eventtime + (ACTIVE_POLL if self.snapshot.tracking else IDLE_POLL)

    def _poll_clog(self, eventtime, snap):
        if self.clog_detection and snap.tracking and not self.runout_active:
            self._check_clog(eventtime, snap)
        else:
            self.clog_baseline = None

    def _contained(self, key, func, *args):
        """Run one poll step; a failure is logged, never raised."""
        try:
            result = func(*args)
        except Exception as exc:
            self._log_contained(key, exc)
            return None
        self._clear_contained(key)
        return result

    def _log_contained(self, key, exc, repeat=True):
        """The traceback once per distinct error, then one line each time."""
        message = "%s: %s" % (type(exc).__name__, exc)
        if self.contained_errors.get(key) != message:
            self.contained_errors[key] = message
            _klog("%s failed", key, level=logging.exception)
        elif repeat:
            _klog("%s still failing: %s", key, message,
                  level=logging.warning)

    def _clear_contained(self, key):
        if self.contained_errors.pop(key, None) is not None:
            _klog("%s recovered", key, level=logging.info)

    def pause_for_path_conflict(self):
        """Pause a print while filament is at more than one hub lane.

        A tool change would refuse and end the print. Once the conflict
        clears, RESUME primes the loaded slot as after any pause. Once per
        print and conflict; returns whether the print is held.
        """
        stats = self.printer.lookup_object("print_stats")
        if self.path_conflict is None \
                or stats.state not in ("printing", "paused"):
            return False
        job = (stats.print_start_time,)
        if self.conflict_paused == job:
            return True
        self.conflict_paused = job
        # A recovery already waiting keeps its own instructions. This one
        # clears with the conflict; nothing is retried.
        if self.change_engine.resume_recovery is None:
            self.change_engine.block_resume(
                PATH_CONFLICT_TITLE, automatic=False,
                action="Pull out the filament that shouldn't be there, "
                       "then resume.")
            self._warn(self.change_engine.recovery_notice())
        if stats.state == "printing":
            self.pause_print()
        return True

    def _watch_loaded_box_offline(self, snap):
        """Pause when the box feeding a print goes offline.

        Nothing feeds the filament or watches for clogs while it is gone.
        Resume retries the slot, which needs the box back online.
        """
        slot = snap.loaded_slot
        if not self.is_physical_slot(slot):
            return
        address, _local = self._address_slot(slot)
        # Once an outage: other reads can count the miss that takes the box
        # offline, so this poll may not be the one that did.
        if self._box_online(address) or address in self.offline_handled:
            return
        stats = self.printer.lookup_object("print_stats")
        if stats.state not in ("printing", "paused"):
            return
        self.offline_handled.add(address)
        # A recovery already waiting keeps its own instructions.
        if self.change_engine.resume_recovery is None:
            self.change_engine.block_resume(
                "Box %d went offline with %s loaded"
                % (address, self.slot_label(slot)),
                target=slot, automatic=True,
                action="Wait for Box %d to reconnect, then resume." % address)
            self._warn(self.change_engine.recovery_notice())
        if stats.state == "printing":
            self.pause_print()

    def _homing(self):
        homing = self.printer.lookup_object("homing", None)
        return homing is not None and (
            homing.has_active_homing_session()
            or homing.is_homing_abort_in_progress())

    def _reconnect_lost_boxes(self, eventtime):
        """Re-address a box that lost power, without a Klipper restart.

        A CFS that keeps power keeps its address and the poll finds it again
        by itself; one that was power-cycled answers only discovery. Not
        while homing, where a motor fault must not wait behind the broadcast.
        """
        lost = {
            address: self.box_ids[address]
            for address in self.drivers
            if address in self.box_ids and not self._box_online(address)}
        if not lost or self.serial is None or self._homing():
            return
        self.last_reconnect = eventtime
        reactor = self.reactor
        address, errors = self.address_manager.reclaim(
            box_protocol.AutoAddressClient(self.serial), lost,
            pause=lambda delay: reactor.pause(reactor.monotonic() + delay))
        for error in errors:
            if error in self.reconnect_noted:
                continue
            self.reconnect_noted.add(error)
            _klog("box reconnect: %s", error, level=logging.warning)
            if error == ADDRESS_WEDGE_WARNING:
                self._warn(error)
        if address is not None:
            self._box_reconnected(address)

    def _box_reconnected(self, address):
        """A power-cycled CFS answers again: forget what it had before."""
        self.box_misses[address] = 0
        self.offline_handled.discard(address)
        self.fault_episodes.pop(address, None)
        self.advised.pop(address, None)
        self.crc_errors.pop(address, None)
        self.crc_warned.discard(address)
        self.reconnect_noted.clear()
        self._clear_tracking_owner(address)
        # The CFS restarted with its default reader policy and no RFID
        # records, so tag reads and Spoolman lookups under way are stale.
        # It reads no tags here (reading after Klipper starts is the
        # startup setting); the % refresh clears what it no longer holds.
        first = (address - 1) * SLOTS_PER_BOX
        for slot in range(first, first + SLOTS_PER_BOX):
            self._clear_rfid_watch(slot)
            self._invalidate_spoolman(slot)
        self.rfid_presence.pop(address, None)
        try:
            self._initialize_box_rfid(address, self.drivers[address])
        except Exception as exc:
            self._warn("RFID initialization failed on box %d: %s" % (
                address, exc))
        self.snapshot = replace(
            self.snapshot, slot_mask=self._presence_mask())
        self._refresh_rfid_remaining((address,))
        self._info(self.gcode, "Box %d reconnected" % address)

    def _observe_fault(self, address, reply):
        status = reply.status
        if status is None:
            return
        if status == box_protocol.STATUS_OK:
            self.fault_episodes.pop(address, None)
            shown = self.advised.get(address)
            if (shown is not None and self.reactor.monotonic() - shown[1]
                    >= ADVISORY_REPEAT):
                del self.advised[address]
            return
        if status == box_protocol.STATUS_INVALID_PARAM:
            _klog("box %d rejected a status query as invalid", address,
                  level=logging.warning)
            return
        if status in CFS_LOG_ONLY_STATUSES:
            _klog("box %d reported %s", address,
                  box_protocol.status_name(status), level=logging.debug)
            return
        if status == box_protocol.STATUS_BAD_CRC:
            return  # Counted by the driver for every reply.

        tracking_owner = self.tracking_owner
        tracking_owned = (
            tracking_owner is not None
            and tracking_owner.address == address)
        path_address = (
            None if self.path_owner is None
            else self._address_slot(self.path_owner)[0])
        path_owned = path_address == address

        if status == box_protocol.STATUS_RUNOUT:
            runout_slot = None
            if (tracking_owned
                    and reply.box_state == box_protocol.BOX_STATE_PRINT):
                runout_slot = tracking_owner.slot
            elif path_owned and self.path_owner == self.tail_slot:
                # A tail restored after a restart has no tracking owner yet.
                runout_slot = self.path_owner
            valid_runout = runout_slot is not None
            key = self._fault_key(
                status, "runout" if valid_runout else "advisory")
            is_new = self.fault_episodes.get(address) != key
            if valid_runout:
                self.runout_active = True
                self.runout_origin = runout_slot
                self.runout_key = (address, self.tracking_epoch)
                if is_new:
                    self._report_runout(runout_slot)
            elif not path_owned:
                self._advise_unused(address, status)
            elif is_new:
                self._warn(box_protocol.status_line(address, status))
            self.fault_episodes[address] = key
            return

        if (status == box_protocol.STATUS_BUFFER_REFILL_STALLED
                and self.runout_key is not None
                and self.runout_key[0] == address):
            key = self._fault_key(status, "runout")
            is_new = self.fault_episodes.get(address) != key
            if is_new:
                _klog("box %d BUFFER_REFILL_STALLED during runout", address)
            self.fault_episodes[address] = key
            return

        fatal = path_owned
        key = self._fault_key(
            status, "fatal" if fatal else "advisory")
        if self.fault_episodes.get(address) == key:
            return
        _klog("box %d %s status=%s state=%s", address,
              "fault" if fatal else "advisory",
              box_protocol.status_name(status),
              box_protocol.state_name(reply.box_state),
              level=logging.warning)
        if not fatal:
            self.fault_episodes[address] = key
            self._advise_unused(address, status)
            return
        detail = box_protocol.status_line(address, status)

        if self._latch_fatal_fault(address, status, detail):
            if self.change_engine.resume_recovery is not None:
                detail = self.change_engine.recovery_notice()
            self._warn(detail)

    def _report_runout(self, slot):
        message = "%s ran out." % self.slot_label(slot)
        if self.printer.lookup_object("print_stats").state == "printing":
            message += " The print carries on with the filament left in the tube."
        self._info(self.gcode, message)

    def _advise_unused(self, address, status):
        """One line per status: the poll idles this box, clearing it."""
        shown = self.advised.get(address)
        if shown is None or shown[0] != status:
            self.advised[address] = (status, self.reactor.monotonic())
            self._info(self.gcode, box_protocol.status_line(
                address, status, note="not in use"))

    def _recent_crc_errors(self, address):
        now = self.reactor.monotonic()
        recent = [stamp for stamp in self.crc_errors.get(address, ())
                  if now - stamp < CRC_FAULT_WINDOW]
        self.crc_errors[address] = recent
        return recent

    def _observe_crc_error(self, address):
        # Shown by _fault_status while the window holds enough errors, so it
        # needs no episode and survives tracking changes.
        # Warn once per run of errors less than a window apart.
        recent = self._recent_crc_errors(address)
        if not recent:
            self.crc_warned.discard(address)
        recent.append(self.reactor.monotonic())
        _klog("box %d rejected a corrupted message (%d in %ds)",
              address, len(recent), CRC_FAULT_WINDOW, level=logging.warning)
        if (len(recent) >= CRC_FAULT_COUNT
                and address not in self.crc_warned
                and not self._fatal_episode(address)):
            self.crc_warned.add(address)
            self._warn(box_protocol.status_line(
                address, box_protocol.STATUS_BAD_CRC))

    def _check_clog(self, eventtime, snap):
        if snap.encoder_mm is None:
            return
        toolhead = self.printer.lookup_object("toolhead")
        mcu = self.printer.lookup_object("mcu")
        extruder = toolhead.get_extruder()
        position = extruder.find_past_position(mcu.estimated_print_time(eventtime))
        if self.clog_baseline is None:
            self.clog_baseline = {
                "extruder": position, "encoder": snap.encoder_mm,
                "last_extruder": position, "last_encoder": snap.encoder_mm,
            }
            return
        self.clog_baseline["last_extruder"] = position
        self.clog_baseline["last_encoder"] = snap.encoder_mm
        extruder_delta = position - self.clog_baseline["extruder"]
        encoder_delta = abs(snap.encoder_mm - self.clog_baseline["encoder"])
        if encoder_delta > CLOG_ENCODER_RESET_MM:
            self.clog_baseline = {
                "extruder": position, "encoder": snap.encoder_mm,
                "last_extruder": position, "last_encoder": snap.encoder_mm,
            }
            return
        if extruder_delta <= self.clog_extruder_length:
            return
        self.clog_event_count += 1
        self.last_clog = {
            "extruder_mm": extruder_delta, "encoder_mm": encoder_delta}
        detail = (
            "Likely clog: extruder moved %.1fmm while CFS encoder moved %.1fmm"
            % (extruder_delta, encoder_delta))
        self.clog_baseline = {
            "extruder": position, "encoder": snap.encoder_mm,
            "last_extruder": position, "last_encoder": snap.encoder_mm,
        }
        stats = self.printer.lookup_object("print_stats")
        if stats.state == "printing":
            target = snap.loaded_slot
            automatic = self.is_valid_slot(target)
            self.change_engine.block_resume(
                detail,
                target=target if automatic else None,
                automatic=automatic)
            self._warn(self.change_engine.recovery_notice())
            self.pause_print()
        else:
            self._warn(detail)

    # ------------------------------------------------------------------
    # Physical CFS operations
    # ------------------------------------------------------------------

    def _driver_for_slot(self, slot):
        address, local = self._address_slot(slot)
        driver = self.drivers.get(address)
        if driver is None:
            raise BoxError("Box %d is offline (%s)" % (address, self.slot_label(slot)))
        return driver, address, local

    def physical_load(self, slot, fault_generation=None):
        if not self.is_physical_slot(slot):
            raise BoxError("Physical load requires an online CFS slot")
        if fault_generation is None:
            fault_generation = self.fault_generation
        self.check_operation_abort(fault_generation)
        driver, address, local = self._driver_for_slot(slot)
        if not self._box_online(address):
            raise BoxError("Box %d is not responding (%s)"
                           % (address, self.slot_label(slot)))
        with self._operation():
            live = self.read_live_state(include_topology=False)
            self.check_operation_abort(fault_generation)
            if live.loaded_slot == self.external_slot:
                raise BoxError(
                    "External filament is loaded; unload it before loading %s"
                    % self.slot_label(slot))
            if (self.is_physical_slot(live.loaded_slot)
                    and live.loaded_slot != slot):
                raise BoxError(
                    "%s is already loaded; unload it before loading %s"
                    % (self.slot_label(live.loaded_slot), self.slot_label(slot)))
            if (live.loaded_slot == slot and live.filament_detected
                    and self._fatal_episode(address, live.status_code)):
                return self._recover_loaded_path(
                    slot, driver, address, local, fault_generation)
            if live.loaded_slot == slot and live.filament_detected:
                self.activate_tracking(slot)
                return True

            # Only a load into an empty path measures the whole path.
            clean_load = live.loaded_slot == -1
            self._set_tracking(
                driver, address, None, "disable CFS tracking")
            self.check_operation_abort(fault_generation)
            slots = self._require_reply(
                self._query_presence(address, driver), "slot-presence query")
            self.check_operation_abort(fault_generation)
            self._info(self.gcode, "Loading %s" % self.slot_label(slot))
            if slot in self.rfid_pending:
                try:
                    # Read the state again: the clear above ends the error
                    # or print state that kept the CFS from reading the tag.
                    state = self._query_box_snapshot(address, driver, False)
                    if (slot in self.rfid_pending
                            and live.loaded_slot == -1
                            and slots.value & (1 << local)
                            and state is not None
                            and state.box_state == box_protocol.BOX_STATE_IDLE
                            and not state.downstream_mask):
                        sample = self._query_rfid_sample(slot)
                        if (sample is not None
                                and sample[0].lower() != "busy"):
                            self._force_rfid_results(
                                address, driver, 1 << local,
                                "deferred insertion")
                except Exception as exc:
                    self._warn(
                        "%s deferred RFID read failed: %s; loading without metadata"
                        % (self.slot_label(slot), exc))
                self._clear_rfid_watch(slot)
            load_encoder_start = self._optional_encoder(driver)
            path_total = self.path_length(address)
            self.change_engine.op_progress(0.0, path_total)
            self._require_reply(
                driver.load_stage(local, 0, timeout=45.0), "load stage 0")
            self.check_operation_abort(fault_generation)

            with driver.load_session() as load_driver:
                stage4 = load_driver.load_stage(local, 4, timeout=1.0)
                if stage4 is not None and stage4.status != 0x00:
                    raise BoxError(box_protocol.status_detail(stage4.status))
                self.check_operation_abort(fault_generation)

                deadline = self.reactor.monotonic() + LOAD_TIMEOUT
                sensor_confirmed = False
                stall_retried = False
                while self.reactor.monotonic() < deadline:
                    self.check_operation_abort(fault_generation)
                    detected, error = self.get_filament_sensor_state()
                    if error:
                        raise BoxError(
                            "Printhead filament sensor is unavailable during load: %s"
                            % error)
                    if detected:
                        sensor_confirmed = True
                        break

                    stage5 = load_driver.load_stage(local, 5, timeout=1.0)
                    if stage5 is None:
                        self.reactor.pause(
                            self.reactor.monotonic() + STAGE5_POLL)
                        continue
                    if stage5.status in (0x0A, 0x0B):
                        self._record_command_fault(stage5, "load stage 5")
                        raise BoxError(box_protocol.status_detail(stage5.status))
                    if stage5.status == box_protocol.STATUS_ODOMETER_STALLED:
                        detected, error = self.get_filament_sensor_state()
                        if not error and detected:
                            sensor_confirmed = True
                            break
                        if (not stall_retried
                                and self.filament_retry_motion(
                                    "CFS odometer stalled; moving toolhead and retrying load")):
                            stall_retried = True
                            clean_load = False
                            self.check_operation_abort(fault_generation)
                            stage4 = load_driver.load_stage(
                                local, 4, timeout=1.0)
                            if stage4 is not None and stage4.status != 0x00:
                                raise BoxError(
                                    box_protocol.status_detail(stage4.status))
                            continue
                        self._record_command_fault(stage5, "load stage 5")
                        raise BoxError(box_protocol.status_detail(stage5.status))
                    if stage5.status != 0x00:
                        self._record_command_fault(stage5, "load stage 5")
                        raise BoxError(box_protocol.status_detail(stage5.status))
                    # The feed reply carries the odometer: live progress
                    # without another request on the bus.
                    odometer = box_protocol.stage_odometer(stage5)
                    if odometer is not None:
                        self.snapshot = replace(
                            self.snapshot, encoder_mm=odometer)
                        if load_encoder_start is not None:
                            self.change_engine.op_progress(
                                abs(odometer - load_encoder_start),
                                path_total)
                    self.reactor.pause(
                        self.reactor.monotonic() + STAGE5_POLL)

                if not sensor_confirmed:
                    try:
                        timeout_reply = load_driver.load_stage(
                            local, 6, timeout=5.0)
                        self._record_command_fault(
                            timeout_reply, "load timeout stop")
                    finally:
                        raise BoxError(
                            "Load timed out before printhead sensor arrival")

                self.check_operation_abort(fault_generation)
                self._require_reply(
                    load_driver.load_stage(local, 6, timeout=5.0),
                    "load stage 6")
                self.check_operation_abort(fault_generation)
            stage7 = driver.load_stage(local, 7, timeout=2.0)
            if stage7 is None:
                _klog("box %d did not respond to the final load nudge; "
                      "printhead sensor was already confirmed", address,
                      level=logging.warning)
            elif stage7.status == box_protocol.STATUS_STAGE7_NO_MOTION:
                _klog("box %d final load nudge moved under 1.5 mm; printhead "
                      "sensor was already confirmed", address)
            elif stage7.status != 0x00:
                raise BoxError(box_protocol.status_detail(stage7.status))

            self.check_operation_abort(fault_generation)
            self.activate_tracking(slot)
            final = self._wait_for_state(
                slot, True, True, fault_generation=fault_generation)
            if not (final.loaded_slot == slot and final.filament_detected and final.tracking):
                raise BoxError(
                    "%s did not reach verified loaded state" % self.slot_label(slot))
            self.runout_active = False
            self.runout_origin = None
            self.mark_hotend_feed_pending(slot)
        fed = self._report_encoder_delta(driver, load_encoder_start, "fed")
        if fed is not None:
            self.change_engine.op_progress(
                fed, max(fed, path_total or 0.0))
        if clean_load and fed is not None:
            self._save_path_length(address, fed)
        return False

    def _recover_loaded_path(
            self, slot, driver, address, local, fault_generation):
        self._set_tracking(
            driver, address, None, "disable CFS tracking for recovery")
        self.check_operation_abort(fault_generation)
        self._require_reply(
            driver.load_stage(local, 6, timeout=5.0),
            "loaded-path recovery buffer validation")
        self.check_operation_abort(fault_generation)
        self.activate_tracking(slot)
        final = self._wait_for_state(
            slot, True, True, fault_generation=fault_generation)
        if (final.status_code != box_protocol.STATUS_OK
                or final.loaded_slot != slot
                or not final.filament_detected
                or not final.tracking):
            raise BoxError(
                "%s recovery did not reach verified loaded state" % self.slot_label(slot))
        return True

    def physical_unload(self, allow_extruder_retract=True,
                        fault_generation=None):
        if fault_generation is None:
            fault_generation = self.fault_generation
        self.check_operation_abort(fault_generation)
        saved_gcode = False
        retract_toolhead = None
        with self._operation(), self.printhead_sensor_off():
            try:
                live = self.read_live_state(include_topology=False)
                self.check_operation_abort(fault_generation)
                if live.loaded_slot in (-1, self.external_slot):
                    return
                if not self.is_physical_slot(live.loaded_slot):
                    raise BoxError("CFS loaded-slot state is unavailable")
                slot = live.loaded_slot
                driver, address, local = self._driver_for_slot(slot)
                if self.snapped_slot(live) == slot:
                    self.snapped = slot
                    raise BoxError(self._snapped_detail(slot))
                # Only filament that reached the printhead spans the path.
                full_unload = bool(live.filament_detected)
                self._info(self.gcode, "Unloading %s" % self.slot_label(slot))
                self._set_tracking(
                    driver, address, None, "disable CFS tracking")
                self.check_operation_abort(fault_generation)
                unload_encoder_start = self._optional_encoder(driver)
                self._require_reply(
                    self._query_presence(address, driver), "unload slot query")
                self.check_operation_abort(fault_generation)

                if (allow_extruder_retract
                        and live.filament_detected is not False
                        and self._extruder_can_move()):
                    self.gcode.run_script_from_command(
                        "SAVE_GCODE_STATE NAME=_box_unload_retract")
                    saved_gcode = True
                    self.gcode.run_script_from_command("M83")
                    retract_toolhead = self.printer.lookup_object("toolhead")
                    self._buffer_retract(driver, "before extruder retract")
                    total = 0.0
                    previous_encoder = self._encoder(driver)
                    for attempt in range(UNLOAD_RETRIES + 1):
                        self.check_operation_abort(fault_generation)
                        amount = UNLOAD_RETRACT_MM if attempt == 0 else UNLOAD_RETRY_MM
                        self.gcode.run_script_from_command(
                            "G1 E-%.0f F%.0f" % (
                                amount, self.retract_velocity))
                        retract_toolhead.wait_moves()
                        self.check_operation_abort(fault_generation)
                        total += amount
                        self._buffer_retract(driver, "after extruder retract")
                        current = self._encoder(driver)
                        detected, error = self.get_filament_sensor_state()
                        if error is None and detected is False:
                            break
                        if previous_encoder is not None and current is not None:
                            delta = abs(current - previous_encoder)
                            threshold = min(ENCODER_CLEAR_MM, amount * 0.8)
                            if total >= UNLOAD_CLEAR_MIN_MM and delta < threshold:
                                break
                        previous_encoder = current
                    self.check_operation_abort(fault_generation)
                    self.gcode.run_script_from_command(
                        "G1 E-%.0f F%.0f"
                        % (UNLOAD_RETRACT_MM, self.retract_velocity))
                    retract_toolhead.flush_step_generation()
                    self.gcode.run_script_from_command(
                        "RESTORE_GCODE_STATE NAME=_box_unload_retract MOVE=0")
                    saved_gcode = False
                path_total = self.path_length(address)
                for attempt in range(2):
                    operation = self.change_engine.running_operation()
                    if operation is not None and path_total:
                        # Nothing answers until the pull ends: estimate it.
                        operation.step("unload", total_mm=path_total)
                        operation.moving(
                            0.0, path_total * UNLOAD_ESTIMATE_CAP,
                            UNLOAD_ESTIMATE_RATE, estimated=True)
                    try:
                        path_reply = driver.unload_path(
                            local, timeout=PATH_RETRACT_TIMEOUT)
                    finally:
                        if retract_toolhead is not None:
                            retract_toolhead.wait_moves()
                    stalled = (
                        path_reply is not None and path_reply.status
                        == box_protocol.STATUS_UNLOAD_MOTOR_BLOCKED)
                    if stalled:
                        motors = self._stalled_motors(address, driver)
                        stall_detail = box_protocol.motor_stall_detail(
                            address, motors)
                    if not stalled or attempt:
                        break
                    if not self.filament_retry_motion(
                            "%s stalled; moving the toolhead and trying again"
                            % (box_protocol.stalled_motor_names(address, motors)
                               or "Box motor")):
                        break
                    self.check_operation_abort(fault_generation)
                    self._set_tracking(
                        driver, address, None,
                        "disable CFS tracking for unload retry")
                    self.check_operation_abort(fault_generation)
                    self._require_reply(
                        self._query_presence(address, driver),
                        "unload retry slot query")
                    self.check_operation_abort(fault_generation)
                if (path_reply is not None
                        and path_reply.status
                        == box_protocol.STATUS_UNLOAD_HUB_PE_TIMEOUT
                        and self._snapped_in_retract(address, driver, local)):
                    detail = self._snapped_detail(slot)
                    self._record_command_fault(
                        path_reply, "loaded-path retract", detail)
                    self.snapped = slot
                    raise BoxError(detail)
                if stalled:
                    self._record_command_fault(
                        path_reply, "loaded-path retract", stall_detail)
                    raise BoxError(stall_detail)
                self._require_reply(path_reply, "loaded-path retract")
                self.check_operation_abort(fault_generation)
                final = self._wait_for_state(
                    -1, False, False, fault_generation=fault_generation)
                if final.filament_sensor_error:
                    raise BoxError(
                        "Unable to verify unload: %s" % final.filament_sensor_error)
                if final.filament_detected:
                    raise BoxError(
                        "CFS retracted the path but the printhead sensor still detects filament")
                if final.loaded_slot != -1:
                    raise BoxError(
                        "CFS retracted the path but still reports loaded slot %s"
                        % final.loaded_slot)
                self.check_operation_abort(fault_generation)
                self.snapshot = replace(
                    final, loaded_slot=-1, loaded_mask=0, tracking=False)
                self.path_owner = None
                self._clear_runout_state()
                self.clear_hotend_feed_pending(slot)
            finally:
                if saved_gcode:
                    self.gcode.run_script_from_command(
                        "RESTORE_GCODE_STATE NAME=_box_unload_retract MOVE=0")
        retracted = self._report_encoder_delta(
            driver, unload_encoder_start, "retracted")
        if retracted is not None:
            self.change_engine.op_progress(
                retracted, max(retracted, path_total or 0.0))
        if full_unload and retracted is not None:
            self._check_retraction(address, retracted)

    def retract_for_cut(self, distance, fault_generation=None):
        if fault_generation is None:
            fault_generation = self.fault_generation
        self.check_operation_abort(fault_generation)
        live = self.read_live_state(include_topology=False)
        self.check_operation_abort(fault_generation)
        if not self.is_physical_slot(live.loaded_slot):
            raise BoxError("No physical CFS slot is loaded")
        driver, address, _local = self._driver_for_slot(live.loaded_slot)
        remaining = float(distance)
        with self._operation():
            self.gcode.run_script_from_command(
                "SAVE_GCODE_STATE NAME=_box_retract_cut")
            try:
                self.gcode.run_script_from_command("M83")
                self._set_tracking(
                    driver, address, None, "disable CFS tracking")
                while remaining > 0:
                    self.check_operation_abort(fault_generation)
                    self._buffer_retract(driver, "cut retract")
                    amount = min(15.0, remaining)
                    self.gcode.run_script_from_command(
                        "G1 E-%.4f F%.0f" % (amount, self.retract_velocity))
                    self.printer.lookup_object("toolhead").wait_moves()
                    self.check_operation_abort(fault_generation)
                    remaining -= amount
            finally:
                self.gcode.run_script_from_command(
                    "RESTORE_GCODE_STATE NAME=_box_retract_cut MOVE=0")

    def _buffer_retract(self, driver, context):
        self._require_reply(
            driver.unload_buffer(timeout=BUFFER_RETRACT_TIMEOUT),
            "buffer retract (%s)" % context)

    def activate_tracking(self, slot):
        driver, address, local = self._driver_for_slot(slot)
        self._set_tracking(
            driver, address, local, "enable CFS tracking")
        self.enable_filament_sensor()
        self.snapshot = replace(
            self.snapshot, loaded_slot=slot, loaded_mask=1 << slot,
            tracking=True, filament_detected=True,
            filament_sensor_error=None)

    def _wait_for_state(self, loaded_slot, detected, tracking,
                        fault_generation=None):
        if fault_generation is None:
            fault_generation = self.fault_generation
        deadline = self.reactor.monotonic() + STATE_TIMEOUT
        last = self.read_live_state(include_topology=False)
        while True:
            self.check_operation_abort(fault_generation)
            episode = self.fault_episodes.get(last.path_box)
            if episode is not None and episode[-1] == "fatal":
                raise BoxError(box_protocol.status_detail(episode[0]))
            if (last.loaded_slot == loaded_slot
                    and last.filament_detected == detected
                    and last.tracking == tracking):
                return last
            if self.reactor.monotonic() >= deadline:
                return last
            self.reactor.pause(self.reactor.monotonic() + STATE_POLL)
            last = self.read_live_state(include_topology=False)

    def _encoder(self, driver):
        reply = driver.query_encoder(timeout=0.5)
        return reply.value if reply is not None and reply.status == 0x00 else None

    def _optional_encoder(self, driver):
        try:
            return self._encoder(driver)
        except Exception:
            return None

    def _report_encoder_delta(self, driver, start, action):
        """Print what the CFS moved and return it in mm, or None."""
        try:
            end = self._encoder(driver)
        except Exception:
            return None
        if start is None or end is None:
            return None
        travel = abs(end - start)
        self._info(self.gcode, "CFS %s %.2f m of filament." % (
            action, travel / 1000.0))
        return travel

    def _path_key(self, address):
        """Path lengths follow the CFS, like its slot profiles."""
        uniid = self.box_ids.get(address)
        return str(address) if uniid is None else bytes(uniid).hex()

    def _path_samples(self, address):
        saved = self.store.setting(PATH_SETTING)
        samples = (saved.get(self._path_key(address))
                   if isinstance(saved, dict) else None)
        if not isinstance(samples, list):
            return []
        return [float(value) for value in samples
                if isinstance(value, (int, float))
                and not isinstance(value, bool)]

    def path_length(self, address):
        """The box's usual load path length in mm, or None until it is known."""
        samples = sorted(self._path_samples(address))
        if len(samples) < PATH_MIN_SAMPLES:
            return None
        return samples[len(samples) // 2]

    def _save_path_length(self, address, fed):
        if not PATH_MIN_MM <= fed <= TAIL_MAX_MM:
            return
        saved = self.store.setting(PATH_SETTING)
        lengths = dict(saved) if isinstance(saved, dict) else {}
        samples = self._path_samples(address) + [round(fed, 1)]
        lengths[self._path_key(address)] = samples[-PATH_SAMPLES:]
        self.store.set_setting(PATH_SETTING, lengths)

    def _check_retraction(self, address, retracted):
        normal = self.path_length(address)
        # A cut unload retracts this much before the measurement starts.
        allowed = PATH_SHORT_MM + self.change_engine.retract_length
        if normal is None or retracted >= normal - allowed:
            return
        self._warn(
            "Your retraction path is normally %.2f m but this retraction was "
            "only %.2f m.\nFilament may have broke in the tube."
            % (normal / 1000.0, retracted / 1000.0))

    def _extruder_can_move(self):
        try:
            return self.printer.lookup_object(
                "extruder").get_heater().can_extrude
        except Exception:
            return False


def load_config(config):
    return Box(config)
