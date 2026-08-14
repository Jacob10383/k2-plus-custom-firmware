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
import threading
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, replace

from extras import box_protocol
from extras.box_addr import ADDRESS_WEDGE_WARNING, MAX_ADDRESSES, AutoAddressManager
from extras.box_change import BoxChangeEngine
from extras.box_catalog import resolve_material
from extras.motion_limits import restore_motion_limits, save_motion_limits


SLOTS_PER_BOX = 4
EXTERNAL_PROFILE_KEY = "external"
API_VERSION = 1
LEGACY_WIDGET_VERSION = 2
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

CLOG_EXTRUDER_MM = 80.0
CLOG_ENCODER_RESET_MM = 18.0

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
CFS_ADVISORY_STATUSES = frozenset((
    box_protocol.STATUS_INVALID_PARAM,
    box_protocol.STATUS_BAD_CRC,
    box_protocol.STATUS_BUSY,
    box_protocol.STATUS_STAGE7_NO_MOTION,
))

CLEAN_LIMIT_VELOCITY = 800
CLEAN_LIMIT_ACCEL = 10000
CLEAN_MINIMUM_CRUISE_RATIO = 0.5
CLEAN_LIMIT_SCV = 5
CLEAN_SERPENTINE_Y_STEP = 2.0
CLEAN_SCRAPER_PASSES = 3

SNAP_RETRACT_MM = 1.2

CUT_SAFE_Z = 2.0
CUT_LIMIT_VELOCITY = 800
CUT_LIMIT_ACCEL = 7500
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


@dataclass(frozen=True)
class TrackingOwner:
    address: int
    slot: int
    epoch: int


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

    def profile(self, slot):
        value = self.data["slots"].get(str(slot), {})
        return {
            "material": str(value.get("material", "")).strip().upper(),
            "color": str(value.get("color", "")).strip().upper(),
            "brand": str(value.get("brand", "")).strip(),
            "name": str(value.get("name", "")).strip(),
            "spoolman_id": value.get("spoolman_id"),
            "rfid_reserve": str(value.get(
                "rfid_reserve", "")).strip().strip("\x00").strip(),
        }

    def set_profile(self, slot, profile):
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
        self.data["slots"][str(slot)] = clean
        self.save()

    def clear_profile(self, slot):
        if self.data["slots"].pop(str(slot), None) is not None:
            self.save()

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

        self.clean_pad_left_x = config.getfloat("clean_pad_left_x", 154.0)
        self.clean_pad_right_x = config.getfloat("clean_pad_right_x", 166.0)
        self.clean_pad_front_y = config.getfloat("clean_pad_front_y", 367.0)
        self.clean_pad_back_y = config.getfloat("clean_pad_back_y", 378.0)
        self.clean_pad_passes = config.getint(
            "clean_pad_passes", 1, minval=1)
        for legacy_name in (
                "clean_left_pos_x", "clean_right_pos_x", "clean_right_pos_y"):
            config.get(legacy_name, None)
        if (self.clean_pad_left_x >= self.clean_pad_right_x
                or self.clean_pad_front_y >= self.clean_pad_back_y):
            raise config.error("Invalid clean pad boundaries")
        self.wastebin_x = config.getfloat("wastebin_pos_x", 133.0)
        self.wastebin_y = config.getfloat("wastebin_pos_y", 378.0)
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
        self.retract_velocity = config.getfloat(
            "retract_velocity", 3000.0, above=0.0)
        self.external_feed_velocity = config.getfloat(
            "external_feed_velocity", 600.0, above=0.0)
        self.pre_cut_cal_x = config.getfloat("pre_cut_cal_pos_x", -5.0)
        self.cut_check_max_x = config.getfloat("check_cut_pos_x_max", -5.5)
        self.cut_check_min_x = config.getfloat("check_cut_pos_x_min", -9.5)

        self.serial = None
        self.drivers = {}
        self.address_errors = ()
        self.drivers_ready = False
        self.snapshot = BoxSnapshot(loaded_slot=None)
        self.operation_depth = 0
        self.tracking_epoch = 0
        self.tracking_owner = None
        self.path_owner = None
        self._clear_runout_state()
        self.runout_defer_active = False
        self.runout_feature = None
        self.fault_generation = 0
        self.last_fatal_reason = None
        self.fault_episodes = {}
        self.rfid_percent = {}
        self.unknown_rfid = {}
        self.rfid_presence = {}
        self.rfid_pending = set()
        self.rfid_snapshot = {}
        self.rfid_seen_invalid = set()
        self.rfid_live_slots = set()
        self.box_replies = {}
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
        self.tx_registered = False
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

    # ------------------------------------------------------------------
    # Lifecycle and public status
    # ------------------------------------------------------------------

    def _register_commands(self):
        commands = (
            ("BOX_LOAD", self.cmd_load, "Load filament from a CFS slot"),
            ("BOX_UNLOAD", self.cmd_unload, "Fully unload the active filament"),
            ("BOX_DEBUG", self.cmd_debug, "Show complete box diagnostics"),
            ("BOX_BUFFER_RETRACT", self.cmd_buffer_retract,
             "Run the CFS buffer retract phase"),
            ("BOX_CUT", self.cmd_cut, "Cut the active filament"),
            ("NOZZLE_CLEAN", self.cmd_nozzle_clean, "Clean the nozzle"),
            ("BOX_GO_TO_WASTEBIN", self.cmd_wastebin, "Move to the wastebin"),
            ("PARSE_FLUSH_VOLUMES", self.change_engine.parse_flush_volumes,
             "Parse slicer flush metadata"),
            ("BOX_RUNOUT_CHECK", self.cmd_runout,
             "Handle CFS runout"),
            ("_BOX_RESUME_CHECK", self.cmd_resume_check,
             "Validate or recover Box state before print resume"),
            ("_FLUSH_CLEAN_SNAP", self.cmd_flush_clean_snap,
             "Internal flush snap and clean"),
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
        result = self.address_manager.enumerate(client)
        self.address_errors = tuple(result.errors)
        if ADDRESS_WEDGE_WARNING in self.address_errors:
            self._warn(ADDRESS_WEDGE_WARNING)
        self.store.set_known_addresses(result.known)
        self.drivers = {
            address: box_protocol.BoxDriver(self.serial, address)
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
        if self.tx_registered:
            return
        for slot in self.physical_slots + (self.external_slot,):
            name = "T%d" % slot
            self.gcode.register_command(
                name,
                lambda gcmd, target=slot: self.change_engine.change(
                    gcmd, target, bool(gcmd.get_int("FLUSH", 1))),
                desc="Change to box slot T%d" % slot,
            )
        self.tx_registered = True

    def _klippy_ready(self, *args):
        self.klippy_ready = True
        self.reactor.register_callback(self._install_runout_source_observer)
        if self.drivers_ready:
            self.reactor.update_timer(
                self.poll_timer, self.reactor.monotonic() + POLL_START_DELAY)

    def _install_runout_source_observer(self, eventtime):
        sd = self.printer.lookup_object("virtual_sdcard", None)
        if sd is None or isinstance(sd.gcode, _VirtualSDGCodeObserver):
            return
        sd.gcode = _VirtualSDGCodeObserver(sd.gcode, self)

    def _handle_filament_runout(self, _eventtime, sensor):
        if sensor != "filament_sensor":
            return
        sd = self.printer.lookup_object("virtual_sdcard", None)
        try:
            if (sd is not None and sd.is_active()
                    and self.filament_sensor_enabled()):
                self.runout_defer_active = True
        except Exception:
            _klog('runout defer arm failed', level=logging.exception)

    def _observe_sd_line(self, line):
        marker = line.lstrip()
        if marker.upper().startswith(";TYPE:"):
            self.runout_feature = marker.split(":", 1)[1].strip().lower()
        if (self.runout_defer_active
                and "infill" in (self.runout_feature or "")):
            self.gcode.run_script("BOX_RUNOUT_CHECK")

    def cancel_runout_defer(self):
        self.runout_defer_active = False
        sensor = self.printer.lookup_object(
            "filament_switch_sensor filament_sensor", None)
        try:
            reset = getattr(
                getattr(sensor, "runout_helper", None),
                "reset_runout_distance_info", None)
            if reset is not None:
                reset()
        except Exception:
            _klog('runout distance cleanup failed', level=logging.exception)

    def _reset_runout_defer(self, *args):
        self.cancel_runout_defer()
        self.runout_feature = None

    def _disconnect(self, *args):
        self._invalidate_tracking_session()
        self.spoolman_generation += 1
        self.spoolman_tokens.clear()
        self.serial = None
        self.reactor.update_timer(self.poll_timer, self.reactor.NEVER)

    def get_status(self, eventtime):
        snap = self.snapshot
        physical = self._slot_statuses(snap)
        slots = physical + [self._external_status(snap)]
        return {
            "api_version": API_VERSION,
            "fluidd_widget_version": LEGACY_WIDGET_VERSION,
            "data_ready": snap.data_ready,
            "status": box_protocol.status_name(snap.status_code),
            "status_code": snap.status_code,
            "state": box_protocol.state_name(snap.state_code),
            "state_code": snap.state_code,
            "temp_c": snap.temp_c,
            "humidity_pct": snap.humidity_pct,
            "loaded_slot": snap.loaded_slot,
            "loaded_mask": snap.loaded_mask,
            "slot_filament_mask": snap.slot_mask,
            "slots": slots,
            "materials": self.store.materials,
            "runout": self._runout_status(physical, snap),
            "runout_swap_enabled": self.runout_swap_enabled,
            "unload_after_print_enabled": self.unload_after_print_enabled,
            "rfid_insert_reading_enabled": self.rfid_insert_reading_enabled,
            "rfid_startup_reading_enabled": self.rfid_startup_reading_enabled,
            "tracking_active": snap.tracking,
            "filament_detected": snap.filament_detected,
            "filament_sensor_error": snap.filament_sensor_error,
            "load_path": self._load_path_status(snap),
            "recovery": self.change_engine.recovery_status(),
            "driver_ready": self.drivers_ready,
        }

    def _slot_statuses(self, snap):
        return [self._slot_status(slot, snap) for slot in self.physical_slots]

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
            "loaded_slot": snap.loaded_slot,
            "loaded_mask": snap.loaded_mask,
            "slot_filament_mask": snap.slot_mask,
            "box_addr": snap.path_box,
            "tracking_active": snap.tracking,
            "encoder": {
                "position_mm": snap.encoder_mm,
                "active": snap.path_box is not None and snap.tracking,
            },
            "buffer": {
                "status_code": snap.buffer_status,
                "state_code": snap.buffer_state,
                "active": snap.buffer_state not in (None, 0),
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

    def profile(self, slot):
        return self.store.profile(self._runtime_slot_key(slot))

    def set_profile(self, slot, profile):
        self.store.set_profile(self._runtime_slot_key(slot), profile)

    def clear_profile(self, slot):
        self.store.clear_profile(self._runtime_slot_key(slot))

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
            sensor = self.printer.lookup_object(
                "filament_switch_sensor filament_sensor")
            status = sensor.get_status(self.reactor.monotonic())
            return bool(status["filament_detected"]), None
        except Exception as exc:
            return None, str(exc)

    def filament_detected(self):
        detected, error = self.get_filament_sensor_state()
        if error:
            raise BoxError("Printhead filament sensor is unavailable: %s" % error)
        return detected

    def filament_sensor_enabled(self):
        sensor = self.printer.lookup_object(
            "filament_switch_sensor filament_sensor")
        return bool(sensor.runout_helper.sensor_enabled)

    def enable_filament_sensor(self):
        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=1")

    def disable_filament_sensor(self):
        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=filament_sensor ENABLE=0")

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
        return {"loaded_slot": source, "chain": chain}

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
                "recoverable": False, "reason": "no active CFS runout",
                "loaded_slot": snap.loaded_slot, "target_slot": None,
            }
        if not runout:
            return {
                "recoverable": False, "reason": "no loaded CFS slot",
                "loaded_slot": snap.loaded_slot, "target_slot": None,
            }
        if not runout["chain"]:
            return {
                "recoverable": False,
                "reason": "no matching present replacement slot",
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
            extruder_delta is not None and extruder_delta > CLOG_EXTRUDER_MM
            and encoder_delta <= CLOG_ENCODER_RESET_MM)
        state = "disabled" if self.runout_active else (
            "inactive" if not self.snapshot.tracking else (
                "triggered" if triggered else "active"))
        return {
            "state": state,
            "baseline_ready": self.clog_baseline is not None,
            "extruder_delta_mm": extruder_delta,
            "encoder_delta_mm": encoder_delta,
            "extruder_threshold_mm": CLOG_EXTRUDER_MM,
            "encoder_reset_mm": CLOG_ENCODER_RESET_MM,
            "triggered": triggered,
            "event_count": self.clog_event_count,
            "last_event": dict(self.last_clog),
        }

    # ------------------------------------------------------------------
    # G-code command wrappers
    # ------------------------------------------------------------------

    def cmd_load(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", 0, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX - 1)
        try:
            already_loaded = self.physical_load(slot)
        except Exception as exc:
            raise gcmd.error(
                "[BOX]: " + box_protocol.format_failed("BOX_LOAD", exc))
        if already_loaded:
            self._info(gcmd, "T%d already loaded; tracking active" % slot)
        else:
            self._info(gcmd, "T%d loaded" % slot)
        self.last_loaded_slot = slot
        self.activate_spool(slot)

    def cmd_unload(self, gcmd):
        if "SLOT" in gcmd.get_command_parameters():
            raise gcmd.error("[BOX]: BOX_UNLOAD no longer accepts SLOT")
        manual = bool(gcmd.get_int("MANUAL", 0, minval=0, maxval=1))
        self.change_engine.unload(gcmd, manual=manual)

    def cmd_buffer_retract(self, gcmd):
        try:
            with self._operation():
                live = self.read_live_state(include_topology=False)
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
            raise gcmd.error("[BOX]: BOX_BUFFER_RETRACT failed: %s" % exc)
        self._info(gcmd, "Box %d buffer retract complete" % address)

    def cmd_cut(self, gcmd):
        try:
            self.cut_filament(force=bool(gcmd.get_int("FORCE", 0)))
        except Exception as exc:
            raise gcmd.error("[BOX]: BOX_CUT failed: %s" % exc)

    def cmd_nozzle_clean(self, gcmd):
        self.nozzle_clean()

    def cmd_flush_clean_snap(self, gcmd):
        self.flush_clean_snap(retract=bool(gcmd.get_int("RETRACT", 1)))

    def cmd_wastebin(self, gcmd):
        self.move_to_wastebin()
        self._info(gcmd, "Moved to wastebin")

    def cmd_resume_check(self, gcmd):
        retry = bool(gcmd.get_int("RETRY", 0, minval=0, maxval=1))
        self.change_engine.resume_check(gcmd, retry=retry)

    def cmd_slot_set(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX)
        if slot is None:
            raise gcmd.error("[BOX]: SLOT is required")
        if not self.is_valid_slot(slot):
            raise gcmd.error("[BOX]: T%d is not an online box slot" % slot)
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
        self._info(gcmd, "Saved T%d profile" % slot)

    def cmd_slot_clear(self, gcmd):
        slot = gcmd.get_int(
            "SLOT", None, minval=0,
            maxval=MAX_ADDRESSES * SLOTS_PER_BOX)
        if slot is None:
            raise gcmd.error("[BOX]: SLOT is required")
        if not self.is_valid_slot(slot):
            raise gcmd.error("[BOX]: T%d is not an online box slot" % slot)
        self.clear_profile(slot)
        self._info(gcmd, "Cleared T%d profile" % slot)

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
        """Apply persisted reader policy and establish presence baselines."""
        for address, driver in sorted(self.drivers.items()):
            try:
                reply = driver.set_rfid_insert_reading(
                    self.rfid_insert_reading_enabled, timeout=1.0)
                self._require_reply(reply, "box %d RFID insertion policy" % address)
                slots = self._require_reply(
                    driver.query_slot_mask(timeout=0.5),
                    "box %d RFID presence baseline" % address)
                local_mask = slots.value & 0x0F
                self.rfid_presence[address] = local_mask
                if self.rfid_startup_reading_enabled and local_mask:
                    cached = self._require_reply(
                        driver.query_rfid_records(
                            local_mask, timeout=0.5),
                        "box %d cached RFID query" % address)
                    unread = 0
                    for local, name in enumerate(
                            box_protocol.RFID_SLOT_NAMES):
                        bit = 1 << local
                        if not local_mask & bit:
                            continue
                        record = cached.records.get(
                            name, "").strip("\x00")
                        if (record.lower() != "busy"
                                and (len(record) != 40
                                     or not cached.fields.get(name))):
                            unread |= bit
                    if unread:
                        self._force_rfid_results(
                            address, driver, unread, "startup")
            except Exception as exc:
                self._warn("RFID initialization failed on box %d: %s" % (
                    address, exc))
        self.snapshot = replace(
            self.snapshot, slot_mask=self._presence_mask())

    def _presence_mask(self):
        mask = 0
        for address, local_mask in self.rfid_presence.items():
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
        self.rfid_live_slots.discard(slot)
        self.rfid_percent.pop(slot, None)
        self._invalidate_spoolman(slot)
        self.rfid_snapshot.pop(slot, None)
        self.rfid_seen_invalid.discard(slot)
        if self.rfid_insert_reading_enabled:
            self.rfid_pending.add(slot)
            self._snapshot_rfid_cache(slot)

    def _rfid_removed(self, slot):
        self._clear_rfid_watch(slot)
        self.rfid_live_slots.discard(slot)
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

    def _query_box_snapshot(self, address, driver, include_topology):
        if include_topology:
            self._query_presence(address, driver)
        for _attempt in range(STATE_EVENT_DRAIN):
            self._watch_pending_rfid(address, driver)
            reply = driver.query_box_state(timeout=0.5)
            if reply is None:
                return None
            if reply.slot_events is None:
                self._finish_pending_rfid(address, reply)
                return reply
            self._query_presence(address, driver)
            self._handle_slot_events(address, reply.slot_events)
        _klog("box %d slot events did not drain after %d reads",
             address, STATE_EVENT_DRAIN, level=logging.warning)
        return None

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
        self._warn("Unknown RFID tag in T%d: CODE=%s" % (
            self._runtime_slot(slot_key), code))
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

    def _apply_rfid_profile(self, slot, fields):
        profile, _code, _raw_code, target, resolved = self._rfid_profile(fields)
        if not resolved:
            return False
        self._ensure_material(profile["material"], target)
        self.unknown_rfid.pop(self._runtime_slot_key(slot), None)
        self.set_profile(slot, profile)
        return True

    def _invalidate_spoolman(self, slot):
        slot_key = self._runtime_slot_key(slot)
        self.spoolman_tokens[slot_key] = (
            self.spoolman_tokens.get(slot_key, 0) + 1)

    def _apply_rfid_record(self, slot, record, fields):
        record = record.rstrip("\x00")
        if len(record) != 40 or not record.strip():
            return False
        display_slot = self._runtime_slot(slot)
        code = self._clean_rfid(fields.get("mat_id")) or "unknown"
        reserve = self._clean_rfid(fields.get("reserve")) or "none"
        self._info(
            self.gcode,
            "RFID tag read for T%d: code=%s reserve=%s"
            % (display_slot, code, reserve))
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
        if self._apply_rfid_profile(slot, fields):
            self._info(
                self.gcode, "RFID T%d: RFID profile applied" % display_slot)
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

    def _read_rfid_remaining(self, slot):
        address, local = self._address_slot(slot)
        driver = self.drivers.get(address)
        if driver is None or slot not in self.rfid_live_slots:
            return
        reply = driver.query_rfid_remaining(1 << local, timeout=0.5)
        if reply is None or reply.status != box_protocol.STATUS_OK:
            return
        value = reply.values.get(box_protocol.RFID_SLOT_NAMES[local])
        if isinstance(value, int) and 0 <= value <= 100:
            self.rfid_percent[slot] = value

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
            self.rfid_live_slots.discard(slot)
            self.rfid_percent.pop(slot, None)
            return record.lower() or "unknown"
        if not self._rfid_should_apply(slot, sample):
            return "stale"
        self._clear_rfid_watch(slot)
        if not self._apply_rfid_record(slot, record, fields):
            self.rfid_live_slots.discard(slot)
            self.rfid_percent.pop(slot, None)
            return "invalid"
        self.rfid_live_slots.add(slot)
        self._read_rfid_remaining(slot)
        return "record"

    def _force_rfid_results(self, address, driver, mask, reason):
        selected = tuple(
            local for local in range(SLOTS_PER_BOX) if mask & (1 << local))
        tools = tuple(self._global_slot(address, local) for local in selected)
        self._info(
            self.gcode, "Reading RFID for %s (%s)" % (
                ",".join("T%d" % slot for slot in tools), reason))
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
            if len(record) != 40 or not fields:
                _klog("T%d forced RFID result was invalid", slot)
                continue
            if self._apply_rfid_record(slot, record, fields):
                applied.add(slot)
                self.rfid_live_slots.add(slot)
                self._read_rfid_remaining(slot)
            self._clear_rfid_watch(slot)
        return applied

    def _refresh_rfid_remaining(self):
        if self.operation_depth:
            return
        by_address = {}
        for slot in self.rfid_live_slots:
            if self.snapshot.slot_mask & (1 << slot):
                address, local = self._address_slot(slot)
                by_address[address] = by_address.get(address, 0) | (1 << local)
        for address, mask in sorted(by_address.items()):
            driver = self.drivers.get(address)
            if driver is None:
                continue
            try:
                reply = driver.query_rfid_remaining(mask, timeout=0.5)
            except Exception as exc:
                _klog("RFID remaining query failed on box %d: %s", address, exc)
                continue
            if reply is None or reply.status != box_protocol.STATUS_OK:
                continue
            for local, name in enumerate(box_protocol.RFID_SLOT_NAMES):
                slot = self._global_slot(address, local)
                if slot not in self.rfid_live_slots:
                    continue
                value = reply.values.get(name)
                if isinstance(value, int) and 0 <= value <= 100:
                    self.rfid_percent[slot] = value

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
            self.gcode, "RFID T%d: fetching Spoolman ID %d"
            % (display_slot, requested_id))

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
                        "RFID T%d: Spoolman ID %d result discarded; slot changed"
                        % (display_slot, requested_id))
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
                    if not applied:
                        raw_code = self._clean_rfid(
                            fields.get("mat_id")).upper()
                        code, _resolved = self._resolve_rfid(raw_code)
                        self._record_unknown_rfid(
                            slot_key, code, raw_code, record, fields)
                    self._info(
                        self.gcode,
                        "RFID T%d: Spoolman ID %d unavailable or incomplete; %s"
                        % (display_slot, requested_id, "RFID profile applied"
                           if applied else "RFID mapping required"))
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
                if resolved:
                    message = "Spoolman ID %d profile applied" % spool_id
                else:
                    message = (
                        "unknown RFID code %s resolved via Spoolman ID %d"
                        % (code, spool_id))
                self._info(self.gcode, "RFID T%d: %s" % (
                    display_slot, message))
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
            gcmd, "operation_depth=%d tracking=%s path=%s runout=%s origin=%s faults=%d" % (
                self.operation_depth, self.tracking_owner, self.path_owner,
                self.runout_active, self.runout_origin,
                self.fault_generation))
        try:
            live = self.read_live_state(include_topology=True)
            self._info(gcmd, "live loaded=%s present=0x%04x tracking=%s" % (
                live.loaded_slot, live.slot_mask, live.tracking))
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
            self._info(gcmd, "--- Box %d (T%d-T%d) ---" % (
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
                self._info(gcmd, "unknown RFID T%d CODE=%s" % (
                    self._runtime_slot(slot), item["code"]))
                self._info(gcmd, "map: %s" % self._rfid_map_command(item["code"]))
        else:
            self._info(gcmd, "unknown RFID: none")
        self._info(gcmd, "=== END DEBUG DUMP ===")

    def _info(self, responder, message):
        responder.respond_info(self.CONSOLE_PREFIX + str(message))

    def _warn(self, message):
        self.gcode.respond_raw("!! " + self.CONSOLE_PREFIX + str(message))

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

    def nozzle_clean(self):
        toolhead = self.printer.lookup_object("toolhead")
        save_motion_limits(
            self.printer, self.gcode, "_box_clean_limits", include_gcode=True)
        try:
            self.move_to_wastebin()
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT VELOCITY=%d ACCEL=%d "
                "MINIMUM_CRUISE_RATIO=%g SQUARE_CORNER_VELOCITY=%d"
                % (CLEAN_LIMIT_VELOCITY, CLEAN_LIMIT_ACCEL,
                   CLEAN_MINIMUM_CRUISE_RATIO, CLEAN_LIMIT_SCV))
            left = self.clean_pad_left_x
            right = self.clean_pad_right_x
            front = self.clean_pad_front_y
            back = self.clean_pad_back_y
            y_steps = max(
                1, int(round((back - front) / CLEAN_SERPENTINE_Y_STEP)))
            center_x = (left + right) / 2.0
            amplitude_x = (right - left) / 2.0
            segments = y_steps * 8
            self.gcode.run_script_from_command(
                "G0 Y%g F%.0f" % (back, self.travel_velocity))
            self.gcode.run_script_from_command(
                "G0 X%g F%.0f" % (left, self.clean_velocity))
            for _index in range(CLEAN_SCRAPER_PASSES):
                self.gcode.run_script_from_command(
                    "G0 X%g F%.0f" % (
                        self.wastebin_x, self.clean_velocity))
                self.gcode.run_script_from_command(
                    "G0 X%g F%.0f" % (left, self.clean_velocity))
            self.gcode.run_script_from_command(
                "G0 X%g F%.0f" % (right, self.clean_velocity))
            for pass_index in range(self.clean_pad_passes):
                direction = -1.0 if (pass_index * y_steps) % 2 else 1.0
                for index in range(1, segments + 1):
                    progress = index / float(segments)
                    x = center_x + direction * amplitude_x * math.cos(
                        math.pi * y_steps * progress)
                    y = back + (front - back) * progress
                    self.gcode.run_script_from_command(
                        "G0 X%.3f Y%.3f F%.0f" % (
                            x, y, self.clean_velocity))
                self.gcode.run_script_from_command(
                    "G0 X%g F%.0f" % (center_x, self.clean_velocity))
                self.gcode.run_script_from_command(
                    "G0 Y%g F%.0f" % (back, self.clean_velocity))
            self.gcode.run_script_from_command(
                "G0 X%g F%.0f" % (self.wastebin_x, self.clean_velocity))
            self.gcode.run_script_from_command(
                "G0 Y%g F%.0f" % (self.wastebin_y, self.travel_velocity))
            toolhead.wait_moves()
        finally:
            restore_motion_limits(
                self.gcode, "_box_clean_limits", include_gcode=True, move=0)

    def flush_clean_snap(self, retract=True, fan_after=None):
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=_box_snap_clean")
        try:
            with self.part_fan_override(self.snap_fan_speed, restore=fan_after):
                self.gcode.run_script_from_command(
                    "G4 P%d" % self.snap_fan_dwell_ms)
                self.gcode.run_script_from_command("M83")
                if retract:
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
        self.gcode.run_script_from_command("HOME_IF_NEEDED AXIS=XY")
        save_motion_limits(
            self.printer, self.gcode, "_box_wastebin_limits", include_gcode=True)
        try:
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT VELOCITY=%d ACCEL=%d "
                "MINIMUM_CRUISE_RATIO=%g SQUARE_CORNER_VELOCITY=%d"
                % (CLEAN_LIMIT_VELOCITY, CLEAN_LIMIT_ACCEL,
                   CLEAN_MINIMUM_CRUISE_RATIO, CLEAN_LIMIT_SCV))
            self.gcode.run_script_from_command(
                "G0 X%g Y%g F%.0f" % (
                    self.wastebin_x, self.wastebin_y, self.travel_velocity))
        finally:
            restore_motion_limits(
                self.gcode, "_box_wastebin_limits", include_gcode=True, move=0)

    def filament_retry_motion(self, message):
        toolhead = self.printer.lookup_object("toolhead")
        homed = toolhead.get_status(
            self.reactor.monotonic()).get("homed_axes", "")
        if "x" not in homed or "y" not in homed:
            return False
        self._info(self.gcode, message)
        save_motion_limits(
            self.printer, self.gcode, "_box_filament_retry",
            include_gcode=True)
        try:
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT VELOCITY=%d ACCEL=%d "
                "MINIMUM_CRUISE_RATIO=%g SQUARE_CORNER_VELOCITY=%d"
                % (CLEAN_LIMIT_VELOCITY, CLEAN_LIMIT_ACCEL,
                   CLEAN_MINIMUM_CRUISE_RATIO, CLEAN_LIMIT_SCV))
            wastebin = "X%g Y%g" % (self.wastebin_x, self.wastebin_y)
            for move in (
                    wastebin, "Y350", "X300", "Y50", "X50", wastebin):
                self.gcode.run_script_from_command(
                    "G0 %s F%.0f" % (move, self.travel_velocity))
            toolhead.wait_moves()
        finally:
            restore_motion_limits(
                self.gcode, "_box_filament_retry",
                include_gcode=True, move=0)
        return True

    def cut_filament(self, force=False):
        detected, sensor_error = self.get_filament_sensor_state()
        if not force and sensor_error is None and not detected:
            self._info(self.gcode, "No filament loaded; skipping cut")
            return
        if self.cut_x is None:
            raise BoxError("cut_pos_x is not defined in [box]")

        toolhead = self.printer.lookup_object("toolhead")
        rail = toolhead.kin.rails[0]
        old_position_min = rail.position_min
        old_limit = toolhead.kin.limits[0]
        old_axes_min = toolhead.kin.axes_min
        lifted_z = None
        limits_saved = False
        try:
            save_motion_limits(
                self.printer, self.gcode, "_box_cut_limits", include_gcode=True)
            limits_saved = True
            lifted_z = self._lift_for_cut(toolhead)
            self.gcode.run_script_from_command("HOME_IF_NEEDED AXIS=XY")
            self._set_cut_x_limit(toolhead, min(old_position_min, self.cut_x - 5.0))
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT VELOCITY=%d ACCEL=%d "
                "MINIMUM_CRUISE_RATIO=%g SQUARE_CORNER_VELOCITY=%d"
                % (CUT_LIMIT_VELOCITY, CUT_LIMIT_ACCEL,
                   CUT_LIMIT_CRUISE, CUT_LIMIT_SCV))
            self.gcode.run_script_from_command(
                "G0 X%.2f Y%.2f F%.0f" % (
                    self.pre_cut_x, self.cut_y, self.travel_velocity))
            toolhead.wait_moves()
            if not self.get_cut_sensor_state():
                raise BoxError("Cut sensor is not in standby")

            self._info(self.gcode, "Cutting filament")
            returned = False
            for attempt in range(3):
                if attempt:
                    self._info(self.gcode, "Cut retry %d/3" % (attempt + 1))
                self.gcode.run_script_from_command(
                    "G0 X%.2f F%.0f" % (self.cut_x, self.cut_velocity))
                toolhead.wait_moves()
                self.gcode.run_script_from_command(
                    "G0 X%.2f F%.0f" % (self.pre_cut_x, self.travel_velocity))
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

            if returned:
                return

            warning = (
                "Cut attempts exhausted and the cutter sensor did not return to "
                "standby; proceeding as configured")
            self._warn(warning)
        finally:
            rail.position_min = old_position_min
            toolhead.kin.limits[0] = old_limit
            toolhead.kin.axes_min = old_axes_min
            if lifted_z is not None:
                try:
                    self.gcode.run_script_from_command("G90")
                    self.gcode.run_script_from_command(
                        "G0 Z%.3f F%.0f" % (lifted_z, self.z_velocity))
                    toolhead.wait_moves()
                except Exception:
                    _klog("failed restoring cut Z", level=logging.exception)
            if limits_saved:
                try:
                    restore_motion_limits(
                        self.gcode, "_box_cut_limits", include_gcode=True, move=0)
                except Exception:
                    _klog("failed restoring cut motion limits", level=logging.exception)

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

    @staticmethod
    def _set_cut_x_limit(toolhead, minimum):
        kin = toolhead.kin
        kin.rails[0].position_min = minimum
        kin.limits[0] = (minimum, kin.limits[0][1])
        kin.axes_min = toolhead.Coord(
            minimum, kin.axes_min.y, kin.axes_min.z, kin.axes_min.e)

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

    def pause_print(self, synchronous=False, skip_retract_wipe=False):
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
        self.reactor.register_async_callback(
            lambda _eventtime: self.gcode.run_script("PAUSE"))
        return True

    def _clear_runout_state(self):
        self.runout_active = False
        self.runout_origin = None
        self.runout_key = None

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
            raise BoxError(
                "CFS tracking owner T%d conflicts with loaded path T%d"
                % (owner.slot, slot))
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

    def _latch_fatal_fault(self, address, status, reason):
        key = self._fault_key(status, "fatal")
        if self.fault_episodes.get(address) == key:
            return False
        self.fault_episodes[address] = key
        self.fault_generation += 1
        self.last_fatal_reason = str(reason)
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

    def _record_command_fault(self, reply, context):
        if reply is None or reply.status not in CFS_COMMAND_FATAL_STATUSES:
            return False
        reason = box_protocol.status_detail(reply.status)
        latched = self._latch_fatal_fault(
            reply.address, reply.status, reason)
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

    def read_live_state(self, include_topology=True):
        """Read one coherent safety snapshot; callers never mix cache epochs."""
        detected, sensor_error = self.get_filament_sensor_state()
        loaded = -1
        loaded_mask = 0
        replies = {}
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
            replies[address] = reply
            local = self._local_from_mask(reply.downstream_mask or 0)
            if local >= 0:
                candidate = self._global_slot(address, local)
                if loaded >= 0:
                    raise BoxError(
                        "Multiple CFS boxes report loaded paths: T%d and T%d"
                        % (loaded, candidate))
                loaded = candidate
                loaded_mask |= 1 << candidate
        self.box_replies = replies
        if loaded >= 0:
            self.path_owner = loaded
        elif detected is False:
            self.path_owner = None
        self._reconcile_tracking_owner(replies)
        for address, reply in replies.items():
            self._observe_fault(address, reply)

        path_owner = self.path_owner
        owner_reply_missing = False
        if loaded < 0 and detected is not False and self.is_physical_slot(path_owner):
            path_address, _local = self._address_slot(path_owner)
            loaded = path_owner
            loaded_mask = 1 << path_owner
            owner_reply_missing = path_address not in replies

        if loaded < 0 and detected is True:
            loaded = self.external_slot

        topology = self._presence_mask()

        path_address = None
        if self.is_physical_slot(loaded):
            path_address, _local = self._address_slot(loaded)
        elif replies:
            path_address = min(replies)
        driver = self.drivers.get(path_address)
        state_reply = replies.get(path_address)
        encoder_reply = None
        buffer_reply = None
        if driver:
            buffer_reply = driver.query_buffer(timeout=0.5)
            if (self.is_physical_slot(loaded) and state_reply is not None
                    and state_reply.box_state == box_protocol.BOX_STATE_PRINT):
                encoder_reply = driver.query_encoder(timeout=0.5)

        status_code = state_reply.status if state_reply else None
        state_code = state_reply.box_state if state_reply else None
        owner = self.tracking_owner
        tracking = (
            owner is not None and owner.address == path_address
            and state_code == box_protocol.BOX_STATE_PRINT
            and not self._fatal_episode(path_address, status_code))
        snap = BoxSnapshot(
            data_ready=self.drivers_ready and not owner_reply_missing,
            status_code=status_code,
            state_code=state_code,
            temp_c=state_reply.temp_c if state_reply else None,
            humidity_pct=state_reply.humidity_pct if state_reply else None,
            loaded_slot=loaded,
            loaded_mask=loaded_mask,
            slot_mask=topology,
            tracking=tracking,
            filament_detected=detected,
            filament_sensor_error=sensor_error,
            path_box=path_address if self.is_physical_slot(loaded) else None,
            encoder_mm=encoder_reply.value if encoder_reply else None,
            buffer_status=buffer_reply.status if buffer_reply else None,
            buffer_state=buffer_reply.value if buffer_reply else None,
        )
        self.snapshot = snap
        return snap

    def _poll(self, eventtime):
        if self.operation_depth:
            return eventtime + 0.25
        if not self.drivers_ready:
            return eventtime + IDLE_POLL
        include_topology = eventtime - self.last_topology_refresh >= TOPOLOGY_POLL
        try:
            snap = self.read_live_state(include_topology=include_topology)
            if include_topology:
                self.last_topology_refresh = eventtime
            if snap.tracking and not self.runout_active:
                self._check_clog(eventtime, snap)
            else:
                self.clog_baseline = None
            if eventtime - self.last_rfid_refresh >= RFID_REFRESH:
                self._refresh_rfid_remaining()
                self.last_rfid_refresh = eventtime
        except Exception:
            _klog("status poll failed", level=logging.exception)
            return eventtime + ERROR_BACKOFF
        return eventtime + (ACTIVE_POLL if self.snapshot.tracking else IDLE_POLL)

    def _observe_fault(self, address, reply):
        status = reply.status
        if status is None:
            return
        if status == box_protocol.STATUS_OK:
            self.fault_episodes.pop(address, None)
            return

        tracking_owner = self.tracking_owner
        tracking_owned = (
            tracking_owner is not None
            and tracking_owner.address == address)
        path_address = (
            None if self.path_owner is None
            else self._address_slot(self.path_owner)[0])
        path_owned = path_address == address

        if status == box_protocol.STATUS_RUNOUT:
            valid_runout = (
                tracking_owned
                and reply.box_state == box_protocol.BOX_STATE_PRINT)
            key = self._fault_key(
                status, "runout" if valid_runout else "advisory")
            is_new = self.fault_episodes.get(address) != key
            if valid_runout:
                self.runout_active = True
                self.runout_origin = tracking_owner.slot
                self.runout_key = (address, tracking_owner.epoch)
                if is_new:
                    self._info(
                        self.gcode,
                        "CFS box %d spool runout %s"
                        % (address, box_protocol.status_detail(status)))
            elif is_new:
                self._warn(
                    "CFS box %d reported %s without active tracking; advisory only"
                    % (address, box_protocol.status_name(status)))
            self.fault_episodes[address] = key
            return

        if (status == box_protocol.STATUS_BUFFER_REFILL_STALLED
                and self.runout_key is not None
                and self.runout_key[0] == address):
            key = self._fault_key(status, "runout")
            is_new = self.fault_episodes.get(address) != key
            if is_new:
                _klog(
                    "CFS box %d BUFFER_REFILL_STALLED during runout %s",
                    address, box_protocol.status_detail(status))
            self.fault_episodes[address] = key
            return

        fatal = path_owned and status not in CFS_ADVISORY_STATUSES
        key = self._fault_key(
            status, "fatal" if fatal else "advisory")
        if self.fault_episodes.get(address) == key:
            return
        detail = "CFS box %d %s %s state=%s" % (
            address,
            "fault" if fatal else "status",
            box_protocol.status_detail(status),
            box_protocol.state_name(reply.box_state))
        if not fatal:
            self.fault_episodes[address] = key
            self._warn(detail + "; advisory only")
            return

        if self._latch_fatal_fault(address, status, detail):
            if self.change_engine.resume_recovery is not None:
                detail = self.change_engine.recovery_notice()
            self._warn(detail)

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
        if extruder_delta <= CLOG_EXTRUDER_MM:
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
            retry_command = (
                "T%d" % target if self.is_valid_slot(target) else None)
            self.change_engine.block_resume(
                detail,
                target=target if retry_command else None,
                automatic=False,
                retry_command=retry_command)
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
            raise BoxError("Box %d is offline (T%d)" % (address, slot))
        return driver, address, local

    def physical_load(self, slot, fault_generation=None):
        if not self.is_physical_slot(slot):
            raise BoxError("Physical load requires an online CFS slot")
        if fault_generation is None:
            fault_generation = self.fault_generation
        self.check_operation_abort(fault_generation)
        driver, address, local = self._driver_for_slot(slot)
        warning = None
        with self._operation():
            live = self.read_live_state(include_topology=False)
            self.check_operation_abort(fault_generation)
            if live.loaded_slot == self.external_slot:
                raise BoxError(
                    "External filament is loaded; unload it before loading T%d" % slot)
            if (self.is_physical_slot(live.loaded_slot)
                    and live.loaded_slot != slot):
                raise BoxError(
                    "T%d is already loaded; unload it before loading T%d"
                    % (live.loaded_slot, slot))
            if (live.loaded_slot == slot and live.filament_detected
                    and self._fatal_episode(address, live.status_code)):
                return self._recover_loaded_path(
                    slot, driver, address, local, fault_generation)
            if live.loaded_slot == slot and live.filament_detected:
                self.activate_tracking(slot)
                return True

            self._set_tracking(
                driver, address, None, "disable CFS tracking")
            self.check_operation_abort(fault_generation)
            slots = self._require_reply(
                self._query_presence(address, driver), "slot-presence query")
            self.check_operation_abort(fault_generation)
            self._info(self.gcode, "Loading T%d" % slot)
            if slot in self.rfid_pending:
                state = self.box_replies.get(address)
                if (live.loaded_slot == -1
                        and slots.value & (1 << local)
                        and state is not None
                        and state.box_state == box_protocol.BOX_STATE_IDLE
                        and not state.downstream_mask):
                    try:
                        sample = self._query_rfid_sample(slot)
                        if (sample is not None
                                and sample[0].lower() != "busy"):
                            self._force_rfid_results(
                                address, driver, 1 << local,
                                "deferred insertion")
                    except Exception as exc:
                        self._warn(
                            "T%d deferred RFID read failed: %s; loading without metadata"
                            % (slot, exc))
                self._clear_rfid_watch(slot)
            load_encoder_start = self._optional_encoder(driver)
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
                warning = (
                    "CFS did not respond to the final load nudge; printhead "
                    "sensor was already confirmed")
            elif stage7.status == 0x0E:
                warning = (
                    "CFS final nudge did not reach 3mm; printhead sensor was "
                    "already confirmed")
            elif stage7.status != 0x00:
                raise BoxError(box_protocol.status_detail(stage7.status))

            self.check_operation_abort(fault_generation)
            self.activate_tracking(slot)
            final = self._wait_for_state(
                slot, True, True, fault_generation=fault_generation)
            if not (final.loaded_slot == slot and final.filament_detected and final.tracking):
                raise BoxError("T%d did not reach verified loaded state" % slot)
            self.runout_active = False
            self.runout_origin = None
            self.mark_hotend_feed_pending(slot)
        if warning:
            self._info(self.gcode, warning)
        self._report_encoder_delta(driver, load_encoder_start, "fed")
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
                "T%d recovery did not reach verified loaded state" % slot)
        return True

    def physical_unload(self, allow_extruder_retract=True,
                        fault_generation=None):
        if fault_generation is None:
            fault_generation = self.fault_generation
        self.check_operation_abort(fault_generation)
        previous_sensor = self.filament_sensor_enabled()
        success = False
        saved_gcode = False
        retract_toolhead = None
        with self._operation():
            try:
                live = self.read_live_state(include_topology=False)
                self.check_operation_abort(fault_generation)
                if live.loaded_slot in (-1, self.external_slot):
                    success = True
                    return
                if not self.is_physical_slot(live.loaded_slot):
                    raise BoxError("CFS loaded-slot state is unavailable")
                slot = live.loaded_slot
                driver, address, local = self._driver_for_slot(slot)
                self._info(self.gcode, "Unloading T%d" % slot)
                self.disable_filament_sensor()
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
                for attempt in range(2):
                    try:
                        path_reply = driver.unload_path(
                            local, timeout=PATH_RETRACT_TIMEOUT)
                    finally:
                        if retract_toolhead is not None:
                            retract_toolhead.wait_moves()
                    if (path_reply is None
                            or path_reply.status
                            != box_protocol.STATUS_UNLOAD_MOTOR_BLOCKED
                            or attempt):
                        break
                    if not self.filament_retry_motion(
                            "CFS unload motor blocked; moving toolhead and retrying unload"):
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
                success = True
            finally:
                try:
                    if saved_gcode:
                        self.gcode.run_script_from_command(
                            "RESTORE_GCODE_STATE NAME=_box_unload_retract MOVE=0")
                finally:
                    if success or previous_sensor:
                        self.enable_filament_sensor()
                    else:
                        self.disable_filament_sensor()
        self._report_encoder_delta(driver, unload_encoder_start, "retracted")

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
                raise BoxError(
                    "CFS box %d remains in fatal state %s"
                    % (last.path_box, box_protocol.status_detail(episode[0])))
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
        try:
            end = self._encoder(driver)
            if start is not None and end is not None:
                self._info(self.gcode, "CFS %s %.2f m of filament." % (
                    action, abs(end - start) / 1000.0))
        except Exception:
            pass

    def _extruder_can_move(self):
        try:
            return self.printer.lookup_object(
                "extruder").get_heater().can_extrude
        except Exception:
            return False


def load_config(config):
    return Box(config)
