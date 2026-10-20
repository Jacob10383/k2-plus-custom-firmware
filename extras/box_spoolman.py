# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Mirror CFS slot occupancy into Spoolman spool locations.

``SpoolmanLocationSync`` is composed by ``Box``. Every run is a full,
idempotent reconcile: Box state is sampled on the reactor thread, then a
worker thread compares it with Spoolman and patches only what differs.
Spools in a CFS slot get the printer location plus a slot extra field;
spools tagged with one of this printer's slots that are no longer there are
released to the configured location and lose the slot field.
"""

import json
import logging
import re
import threading
import urllib.request

from extras import box_protocol


SPOOLMAN_PROXY_URL = "http://127.0.0.1:7125/server/spoolman/proxy"
SPOOLMAN_TIMEOUT = 5.0
SPOOLMAN_MAX_BYTES = 8 * 1024 * 1024
SPOOLMAN_LOCATION_MAX = 64

SYNC_DEBOUNCE = 1.0
SYNC_RETRY = 60.0

RELEASE_POLICIES = ("if_unchanged", "always", "keep")
SLOT_LETTERS = "ABCD"
FIELD_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _klog(msg, *args, level=logging.info):
    level("box_spoolman: " + msg, *args)


class SpoolmanError(RuntimeError):
    pass


def _spoolman_request(method, path, body=None):
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
        if isinstance(error, dict):
            error = error.get("message") or error
        raise SpoolmanError("%s %s failed: %s" % (method, path, error))
    return result.get("response")


def _decode_extra(value):
    """Spoolman stores extra field values as JSON-encoded strings."""
    if value is None:
        return None
    try:
        value = json.loads(value)
    except (TypeError, ValueError):
        pass
    return None if value is None else str(value)


class SpoolmanLocationSync:
    def __init__(self, box, config):
        self.box = box
        self.reactor = box.reactor
        self.enabled = config.getboolean("spoolman_location_sync", False)
        self.location_format = config.get("spoolman_location", "K2 Plus")
        self.removed_location = config.get(
            "spoolman_removed_location", "Shelf").strip()
        self.release_policy = config.getchoice(
            "spoolman_removed_policy",
            {name: name for name in RELEASE_POLICIES}, "if_unchanged")
        self.slot_field = config.get(
            "spoolman_slot_field", "cfs_slot").strip()
        self.slot_field_name = config.get(
            "spoolman_slot_field_name", "CFS Slot").strip()
        self.slot_format = config.get("spoolman_slot_format", "{box}{letter}")
        self.sync_on_startup = config.getboolean(
            "spoolman_sync_on_startup", True)
        self._validate(config)

        self.ready = False
        self.running = False
        self.dirty = False
        self.field_keys = None
        self.last_result = "never run"
        self.last_changes = ()
        self.timer = self.reactor.register_timer(self._start_run)

    def _validate(self, config):
        if not self.enabled:
            return
        if not FIELD_KEY_RE.match(self.slot_field):
            raise config.error(
                "spoolman_slot_field must be 1-64 characters of a-z, 0-9 or _")
        if len(self.removed_location) > SPOOLMAN_LOCATION_MAX:
            raise config.error("spoolman_removed_location is too long")
        labels = set()
        for slot in range(4 * box_protocol.SLOTS_PER_BOX):
            try:
                label = self.slot_label(slot)
                location = self.location(slot)
            except (KeyError, IndexError, ValueError) as exc:
                raise config.error(
                    "Invalid spoolman_location or spoolman_slot_format: %s"
                    % exc)
            if not label or label in labels:
                raise config.error(
                    "spoolman_slot_format must give every slot a unique "
                    "value; use {box} with {slot}, {letter} or {tool}")
            if not location or len(location) > SPOOLMAN_LOCATION_MAX:
                raise config.error(
                    "spoolman_location must be 1-%d characters"
                    % SPOOLMAN_LOCATION_MAX)
            labels.add(label)

    # ------------------------------------------------------------------
    # Slot naming
    # ------------------------------------------------------------------

    @staticmethod
    def _placeholders(slot):
        local = slot % box_protocol.SLOTS_PER_BOX
        return {
            "box": slot // box_protocol.SLOTS_PER_BOX + 1,
            "slot": local + 1,
            "letter": SLOT_LETTERS[local],
            "tool": slot,
        }

    def slot_label(self, slot):
        return self.slot_format.format(**self._placeholders(slot)).strip()

    def location(self, slot):
        return self.location_format.format(**self._placeholders(slot)).strip()

    # ------------------------------------------------------------------
    # Lifecycle and triggers
    # ------------------------------------------------------------------

    def start(self):
        """Called once Box has drivers and an RFID presence baseline."""
        if not self.enabled:
            return
        self.ready = True
        if self.sync_on_startup:
            self.request()

    def stop(self):
        self.ready = False
        self.dirty = False
        self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def request(self, delay=SYNC_DEBOUNCE):
        if not (self.enabled and self.ready):
            return
        self.dirty = True
        if not self.running:
            self.reactor.update_timer(
                self.timer, self.reactor.monotonic() + delay)

    def _desired_state(self):
        """Sample Box state on the reactor thread.

        Returns ``(desired, owned)``: ``desired`` maps spool ID to the slot
        it occupies, ``owned`` maps this printer's settled slot labels to
        their configured location. Slots with an RFID read or Spoolman
        lookup in flight are left out of both, so a stale profile is never
        claimed or released mid-read.
        """
        box = self.box
        mask = box._presence_mask()
        desired = {}
        owned = {}
        for slot in box.physical_slots:
            if box.slot_identity_pending(slot):
                continue
            owned[self.slot_label(slot)] = self.location(slot)
            if not mask & (1 << slot):
                continue
            spool_id = box.profile(slot)["spoolman_id"]
            if spool_id is None:
                continue
            spool_id = int(spool_id)
            if spool_id in desired:
                _klog("spool %d is linked to %s and %s; using the first",
                      spool_id, box.slot_label(desired[spool_id]),
                      box.slot_label(slot), level=logging.warning)
                continue
            desired[spool_id] = slot
        return desired, owned

    def _start_run(self, eventtime):
        if not (self.enabled and self.ready) or self.running:
            return self.reactor.NEVER
        try:
            desired, owned = self._desired_state()
        except Exception:
            _klog("state sample failed", level=logging.exception)
            return eventtime + SYNC_RETRY
        desired = {
            spool_id: (self.slot_label(slot), self.location(slot),
                       self.box.slot_label(slot))
            for spool_id, slot in desired.items()
        }
        self.dirty = False
        self.running = True
        threading.Thread(
            target=self._worker, args=(desired, owned),
            name="box-spoolman-location", daemon=True).start()
        return self.reactor.NEVER

    # ------------------------------------------------------------------
    # Worker thread: Spoolman I/O only, no Box access
    # ------------------------------------------------------------------

    def _ensure_field(self):
        """Return the defined spool extra-field keys, creating ours if
        missing. Cached until a sync fails, so a field deleted in Spoolman
        is recreated on the next retry."""
        if self.field_keys is not None:
            return self.field_keys
        fields = _spoolman_request("GET", "/v1/field/spool") or []
        keys = {field.get("key") for field in fields
                if isinstance(field, dict)}
        if self.slot_field not in keys:
            _spoolman_request(
                "POST", "/v1/field/spool/%s" % self.slot_field,
                {"name": self.slot_field_name, "field_type": "text"})
            _klog("created spool extra field %s", self.slot_field)
            keys.add(self.slot_field)
        self.field_keys = frozenset(keys)
        return self.field_keys

    def _plan(self, spools, desired, owned, field_keys):
        """Return ``[(spool_id, patch, description)]`` for every change."""
        changes = []
        seen = set()
        for spool in spools:
            if not isinstance(spool, dict):
                continue
            try:
                spool_id = int(spool["id"])
            except (KeyError, TypeError, ValueError):
                continue
            seen.add(spool_id)
            extra = spool.get("extra")
            extra = extra if isinstance(extra, dict) else {}
            current_label = _decode_extra(extra.get(self.slot_field))
            # PATCH replaces all extra values and Spoolman rejects keys of
            # deleted fields, so resend only values of defined fields.
            extra = {key: value for key, value in extra.items()
                     if key in field_keys}
            current_location = (spool.get("location") or "").strip()
            if spool_id in desired:
                label, location, display = desired[spool_id]
                if current_label == label and current_location == location:
                    continue
                extra[self.slot_field] = json.dumps(label)
                changes.append((
                    spool_id, {"location": location, "extra": extra},
                    "spool %d -> %s (%s)" % (spool_id, location, display)))
            elif current_label is not None and current_label in owned:
                extra[self.slot_field] = None
                patch = {"extra": extra}
                still_ours = current_location == owned[current_label]
                if (self.release_policy == "always"
                        or (self.release_policy == "if_unchanged"
                            and still_ours)):
                    patch["location"] = self.removed_location or None
                    description = "spool %d left %s -> %s" % (
                        spool_id, current_label,
                        self.removed_location or "no location")
                else:
                    description = "spool %d left %s; location kept (%s)" % (
                        spool_id, current_label, current_location or "none")
                changes.append((spool_id, patch, description))
        missing = sorted(set(desired) - seen)
        return changes, missing

    def _worker(self, desired, owned):
        applied = []
        failures = []
        missing = []
        try:
            field_keys = self._ensure_field()
            spools = _spoolman_request("GET", "/v1/spool")
            if not isinstance(spools, list):
                raise SpoolmanError("GET /v1/spool returned no list")
            changes, missing = self._plan(
                spools, desired, owned, field_keys)
            for spool_id, patch, description in changes:
                try:
                    _spoolman_request("PATCH", "/v1/spool/%d" % spool_id, patch)
                    applied.append(description)
                except Exception as exc:
                    failures.append("%s: %s" % (description, exc))
            error = "; ".join(failures) or None
        except Exception as exc:
            error = str(exc)
        self.reactor.register_async_callback(
            lambda eventtime: self._finish(applied, missing, error))

    def _finish(self, applied, missing, error):
        self.running = False
        box = self.box
        for description in applied:
            _klog("%s", description)
            box._info(box.gcode, "Spoolman: %s" % description)
        for spool_id in missing:
            box._warn("Spoolman: spool %d is in a CFS slot but was not found "
                      "(deleted or archived?)" % spool_id)
        self.last_changes = tuple(applied)
        if error:
            self.field_keys = None
            self.last_result = "failed: %s" % error
            _klog("sync failed: %s", error, level=logging.warning)
            self.dirty = True
            if self.ready:
                self.reactor.update_timer(
                    self.timer, self.reactor.monotonic() + SYNC_RETRY)
            return
        self.last_result = "ok (%d change%s)" % (
            len(applied), "" if len(applied) == 1 else "s")
        if self.dirty:
            self.request()

    # ------------------------------------------------------------------
    # G-code surface
    # ------------------------------------------------------------------

    def cmd_sync(self, gcmd):
        if not self.enabled:
            raise gcmd.error(
                "[BOX]: Spoolman location sync is disabled; set "
                "spoolman_location_sync: True in [box]")
        if not self.ready:
            raise gcmd.error("[BOX]: CFS is not ready yet")
        self.field_keys = None
        self.request(delay=0.0)
        gcmd.respond_info("[BOX]: Spoolman location sync queued")

    def cmd_status(self, gcmd):
        lines = ["Spoolman location sync: %s" % (
            "enabled" if self.enabled else "disabled")]
        if self.enabled:
            lines.extend((
                "location=%r removed_location=%r removed_policy=%s" % (
                    self.location_format, self.removed_location,
                    self.release_policy),
                "slot_field=%s (%r) slot_format=%r sync_on_startup=%s" % (
                    self.slot_field, self.slot_field_name, self.slot_format,
                    self.sync_on_startup),
                "ready=%s running=%s pending=%s last=%s" % (
                    self.ready, self.running, self.dirty, self.last_result),
            ))
            lines.extend("  " + change for change in self.last_changes)
        gcmd.respond_info("[BOX]: " + "\n".join(lines))
