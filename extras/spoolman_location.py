# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Mirror which CFS slot holds each linked spool into Spoolman locations.

A standalone consumer of the published ``printer.box`` status: it never calls
into Box. Only spools this printer placed are ever released, and a released
spool is only changed while its location still reads what was written.
"""

import json
import logging
import os
import threading
import urllib.request


SPOOLMAN_PROXY_URL = "http://127.0.0.1:7125/server/spoolman/proxy"
SPOOLMAN_TIMEOUT = 2.0
SPOOLMAN_MAX_BYTES = 8 * 1024 * 1024
STATE_PATH = "/mnt/UDISK/printer_data/spoolman_location.json"
LOCATION_MAX = 64
SLOTS_PER_BOX = 4
MAX_BOXES = 4

POLL_INTERVAL = 5.0
# Covers an RFID read plus its Spoolman lookup after a spool goes in.
SETTLE_TIME = 10.0
RETRY_DELAY = 60.0

CONSOLE_PREFIX = "Spoolman location: "


def _klog(msg, *args, level=logging.info):
    level("spoolman_location: " + msg, *args)


class SpoolmanError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _spoolman_request(method, path, body=None):
    """Call Spoolman through Moonraker's proxy and return its response."""
    payload = {
        "request_method": method, "path": path, "use_v2_response": True,
    }
    if body is not None:
        payload["body"] = body
    request = urllib.request.Request(
        SPOOLMAN_PROXY_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=SPOOLMAN_TIMEOUT) as response:
        raw = response.read(SPOOLMAN_MAX_BYTES + 1)
    if len(raw) > SPOOLMAN_MAX_BYTES:
        raise SpoolmanError("%s %s response too large" % (method, path))
    result = json.loads(raw.decode()).get("result", {})
    error = result.get("error")
    if error is not None:
        status = error.get("status_code") if isinstance(error, dict) else None
        raise SpoolmanError("%s %s failed: %s" % (method, path, error), status)
    return result.get("response")


def _apply(placements, releases, removed_location, stale):
    """Write locations off the reactor thread. Stops at the first failure,
    or once ``stale()`` reports the slots changed, and returns what finished
    so it is not repeated."""
    result = {"placed": [], "missing": [], "archived": [], "released": [],
              "error": None}
    try:
        for spool_id, slot, location in placements:
            if stale():
                return result
            path = "/v1/spool/%d" % spool_id
            try:
                spool = _spoolman_request("PATCH", path, {"location": location})
            except SpoolmanError as exc:
                if exc.status != 404:
                    raise
                result["missing"].append((spool_id, slot))
                continue
            result["placed"].append((spool_id, slot, location))
            if isinstance(spool, dict) and spool.get("archived"):
                result["archived"].append(spool_id)
        for spool_id, location in releases:
            if stale():
                return result
            path = "/v1/spool/%d" % spool_id
            try:
                spool = _spoolman_request("GET", path)
                # Moved by hand or by another printer: leave it alone.
                if isinstance(spool, dict) and spool.get("location") == location:
                    if stale():
                        return result
                    _spoolman_request(
                        "PATCH", path, {"location": removed_location})
            except SpoolmanError as exc:
                if exc.status != 404:
                    raise
            result["released"].append(spool_id)
    except Exception as exc:
        result["error"] = str(exc) or exc.__class__.__name__
    return result


class SpoolmanLocation:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.location_format = config.get(
            "location", "Box {box} Slot {slot}").strip()
        self.update_removed = config.getboolean("update_removed", True)
        # Empty clears the location of a spool that leaves the CFS.
        self.removed_location = config.get(
            "removed_location", "").strip() or None
        try:
            location = self.location(MAX_BOXES * SLOTS_PER_BOX - 1)
        except (AttributeError, IndexError, KeyError, ValueError):
            raise config.error("location may only use {box} and {slot}")
        if not 0 < len(location) <= LOCATION_MAX:
            raise config.error(
                "location must be 1-%d characters" % LOCATION_MAX)
        if len(self.removed_location or "") > LOCATION_MAX:
            raise config.error("removed_location is too long")

        self.state_path = STATE_PATH
        self.placed = self._load_placed()
        self.dirty = False
        self.save_failing = False
        self.box = None
        self.closed = False
        self.observed = {}  # slot -> ((present, spool_id, online), since)
        self.generation = 0  # bumped whenever an observation changes
        self.missing = set()  # (slot, spool_id) links that got a 404
        self.warned_duplicates = {}  # spool_id -> slots already warned about
        self.running = False
        self.retry_at = 0.0
        self.failing = False
        self.timer = self.reactor.register_timer(self._poll)
        self.printer.register_event_handler("klippy:ready", self._ready)
        self.printer.register_event_handler("klippy:disconnect", self._close)
        self.printer.register_event_handler("klippy:shutdown", self._close)

    def location(self, slot):
        return self.location_format.format(
            box=slot // SLOTS_PER_BOX + 1,
            slot=slot % SLOTS_PER_BOX + 1).strip()

    def _warn(self, message):
        _klog("%s", message, level=logging.warning)
        self.gcode.respond_raw("!! " + CONSOLE_PREFIX + message)

    # ------------------------------------------------------------------
    # Placed spools: id -> slot and the location written
    # ------------------------------------------------------------------

    def _load_placed(self):
        try:
            with open(self.state_path, "r") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            _klog("ignoring unreadable %s: %s", self.state_path, exc,
                  level=logging.warning)
            return {}
        placed = {}
        for key, entry in (value if isinstance(value, dict) else {}).items():
            try:
                placed[int(key)] = {
                    "slot": int(entry["slot"]),
                    "location": str(entry["location"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
        return placed

    def _save_placed(self):
        directory = os.path.dirname(self.state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = self.state_path + ".tmp"
        with open(temporary, "w") as stream:
            json.dump({str(spool_id): entry
                       for spool_id, entry in sorted(self.placed.items())},
                      stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, self.state_path)

    def _persist(self):
        """Save placed spools; a failure is retried on the next poll."""
        try:
            self._save_placed()
        except OSError as exc:
            self.dirty = True
            if not self.save_failing:
                self._warn("can't save %s: %s; retrying" % (
                    self.state_path, exc))
            self.save_failing = True
            return
        self.dirty = False
        self.save_failing = False

    # ------------------------------------------------------------------
    # Reading Box status
    # ------------------------------------------------------------------

    def _ready(self, *args):
        self.box = self.printer.lookup_object("box", None)
        if self.box is None:
            self._warn("[spoolman_location] needs [box]; sync is off")
            return
        self.reactor.update_timer(
            self.timer, self.reactor.monotonic() + POLL_INTERVAL)

    def _close(self, *args):
        # A running worker stops before its next request.
        self.closed = True
        self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def _observe(self, status, eventtime):
        online = {}
        for box in status.get("boxes") or ():
            for slot in box.get("slots") or ():
                online[slot] = bool(box.get("online"))
        observed = {}
        for slot in status.get("slots") or ():
            if slot.get("external"):
                continue
            index = int(slot["index"])
            spool_id = slot.get("spoolman_id")
            value = (bool(slot.get("present")),
                     None if spool_id is None else int(spool_id),
                     online.get(index, False))
            previous = self.observed.get(index)
            if previous and previous[0] == value:
                observed[index] = previous
            else:
                observed[index] = (value, eventtime)
        if observed != self.observed:
            self.generation += 1
        self.observed = observed

    def _settled(self, slot, eventtime):
        """Online, and nothing about the slot changed for SETTLE_TIME."""
        entry = self.observed.get(slot)
        return (entry is not None and entry[0][2]
                and eventtime - entry[1] >= SETTLE_TIME)

    def _changes(self, eventtime):
        loaded = {}
        for slot, ((present, spool_id, _online), _since) in sorted(
                self.observed.items()):
            if present and spool_id is not None and self._settled(slot, eventtime):
                loaded.setdefault(spool_id, []).append(slot)

        # Warnings and missing-spool retries follow the slot links themselves,
        # so a box going offline and back doesn't repeat them.
        linked = {}
        for slot, ((_present, spool_id, _online), _since) in sorted(
                self.observed.items()):
            if spool_id is not None:
                linked.setdefault(spool_id, []).append(slot)
        duplicates = {spool_id: tuple(slots)
                      for spool_id, slots in loaded.items() if len(slots) > 1}
        for spool_id, slots in duplicates.items():
            warned = self.warned_duplicates.get(spool_id, frozenset())
            if not warned.issuperset(slots):
                self._warn("spool %d is linked to %s; not updating it until "
                           "only one slot links it" % (spool_id, " and ".join(
                               self.location(slot) for slot in slots)))
                self.warned_duplicates[spool_id] = warned.union(slots)
        self.warned_duplicates = {
            spool_id: slots
            for spool_id, slots in self.warned_duplicates.items()
            if len(linked.get(spool_id, ())) > 1}
        # Changing a slot's link gives that link another try.
        self.missing = {(slot, spool_id) for slot, spool_id in self.missing
                        if slot in linked.get(spool_id, ())}

        placements = []
        for spool_id, slots in sorted(loaded.items()):
            if spool_id in duplicates or (slots[0], spool_id) in self.missing:
                continue
            slot = slots[0]
            location = self.location(slot)
            if self.placed.get(spool_id) != {"slot": slot, "location": location}:
                placements.append((spool_id, slot, location))
        # A spool whose old slot can't be judged right now is left alone.
        releases = [
            (spool_id, entry["location"])
            for spool_id, entry in sorted(self.placed.items())
            if spool_id not in loaded and self._settled(entry["slot"], eventtime)
        ]
        return placements, releases

    def _poll(self, eventtime):
        if self.closed:
            return self.reactor.NEVER
        try:
            if self.dirty:
                self._persist()
            status = self.box.get_status(eventtime)
            if not status.get("driver_ready"):
                self.observed = {}
            else:
                self._observe(status, eventtime)
                self._sync(eventtime)
        except Exception:
            _klog("sync failed", level=logging.exception)
        return eventtime + POLL_INTERVAL

    # ------------------------------------------------------------------
    # Writing Spoolman
    # ------------------------------------------------------------------

    def _sync(self, eventtime):
        if self.running or eventtime < self.retry_at:
            return
        placements, releases = self._changes(eventtime)
        if releases and not self.update_removed:
            for spool_id, _location in releases:
                self.placed.pop(spool_id, None)
                _klog("spool %d left the CFS; location kept", spool_id)
            releases = []
            self._persist()
        if not placements and not releases:
            return
        removed_location = self.removed_location
        generation = self.generation

        def stale():
            return self.closed or self.generation != generation

        def worker():
            result = _apply(placements, releases, removed_location, stale)
            self.reactor.register_async_callback(
                lambda eventtime: self._finish(eventtime, result))

        threading.Thread(
            target=worker, name="spoolman-location", daemon=True).start()
        self.running = True

    def _finish(self, eventtime, result):
        self.running = False
        try:
            self._record(eventtime, result)
        except Exception:
            _klog("recording sync result failed", level=logging.exception)

    def _record(self, eventtime, result):
        for spool_id, slot, location in result["placed"]:
            self.placed[spool_id] = {"slot": slot, "location": location}
            _klog("spool %d location set to %s", spool_id, location)
        for spool_id, _slot in result["missing"]:
            self.placed.pop(spool_id, None)
        for spool_id in result["released"]:
            self.placed.pop(spool_id, None)
            _klog("spool %d left the CFS", spool_id)
        self._persist()
        for spool_id, slot in result["missing"]:
            self.missing.add((slot, spool_id))
            self._warn("spool %d is linked to a CFS slot but doesn't exist in "
                       "Spoolman; relink the slot" % spool_id)
        for spool_id in result["archived"]:
            self._warn("spool %d is linked to a CFS slot but is archived in "
                       "Spoolman" % spool_id)
        error = result["error"]
        if error is None:
            self.failing = False
            return
        if not self.failing:
            self._warn("sync failed: %s; retrying every minute" % error)
        else:
            _klog("sync failed: %s", error, level=logging.warning)
        self.failing = True
        self.retry_at = eventtime + RETRY_DELAY


def load_config(config):
    return SpoolmanLocation(config)
