# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Durable K2 print recovery from completed VirtualSD command boundaries."""

import copy
import hashlib
import json
import logging
import math
import os
import threading
import time
import zlib
from collections import deque


TIMER_INTERVAL = 0.25
FINGERPRINT_BYTES = 64 * 1024
CHECKPOINT = "checkpoint"
TOMBSTONE = "tombstone"
EMERGENCY = "emergency"
_RECORD_MAGIC = b"K2PLR"
_STORE_OWNERS_LOCK = threading.Lock()
_STORE_OWNERS = set()
_STORE_POISONED = set()


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _encode_record(kind, context_id, dynamic_bytes, generation=None):
    metadata = {"kind": kind, "context_id": context_id}
    if generation is not None:
        metadata["generation"] = generation
    metadata_bytes = _canonical(metadata)
    crc = "%08x" % (zlib.crc32(
        metadata_bytes + b"\n" + dynamic_bytes) & 0xffffffff)
    return (b"\n".join((_RECORD_MAGIC, metadata_bytes, dynamic_bytes,
                         crc.encode("ascii"))) + b"\n")


def _decode_record(raw):
    lines = raw.splitlines()
    if len(lines) != 4 or lines[0] != _RECORD_MAGIC:
        raise ValueError("invalid PLR record frame")
    metadata_bytes, dynamic_bytes = lines[1], lines[2]
    expected = "%08x" % (zlib.crc32(
        metadata_bytes + b"\n" + dynamic_bytes) & 0xffffffff)
    if lines[3].decode("ascii") != expected:
        raise ValueError("record CRC mismatch")
    metadata = json.loads(metadata_bytes.decode("ascii"))
    if not isinstance(metadata, dict):
        raise ValueError("invalid record metadata")
    kind = metadata.get("kind")
    generation = metadata.get("generation")
    context_id = metadata.get("context_id")
    dynamic = json.loads(dynamic_bytes.decode("ascii"))
    if kind == TOMBSTONE:
        if dynamic is not None or context_id is not None:
            raise ValueError("tombstone contains a payload")
    elif kind in (CHECKPOINT, EMERGENCY):
        if (not isinstance(dynamic, dict) or not isinstance(context_id, str)
                or len(context_id) != 64
                or any(char not in "0123456789abcdef" for char in context_id)):
            raise ValueError("checkpoint payload is missing")
    else:
        raise ValueError("unknown record kind")
    if kind != EMERGENCY and (
            type(generation) is not int or generation < 0):
        raise ValueError("invalid record generation")
    if kind == EMERGENCY and generation is not None:
        raise ValueError("emergency record has a generation")
    return {"kind": kind, "generation": generation,
            "context_id": context_id, "dynamic": dynamic,
            "dynamic_bytes": dynamic_bytes}


def _lineage(payload):
    if not isinstance(payload, dict):
        return None
    session, file_state = payload.get("session_id"), payload.get("file")
    if (not isinstance(session, str) or not session
            or not isinstance(file_state, dict)
            or not isinstance(file_state.get("path"), str)
            or not file_state.get("path")
            or not isinstance(file_state.get("identity"), dict)):
        return None
    return session, file_state["path"], file_state["identity"]


def _position(payload):
    file_state = payload.get("file") if isinstance(payload, dict) else None
    value = file_state.get("position") if isinstance(file_state, dict) else None
    return value if type(value) is int and value >= 0 else -1


def _klog(msg, *args, level=logging.info):
    level("power_loss_recovery: " + msg, *args)

class AtomicSnapshotStore:
    """Owns durable contexts, the A/B journal, and emergency checkpoint."""

    def __init__(self, directory, start_worker=True):
        self.directory = os.path.realpath(os.path.expanduser(directory))
        self._journal_paths = tuple(
            os.path.join(self.directory, "checkpoint.%s" % slot)
            for slot in ("a", "b"))
        self._emergency_path = os.path.join(self.directory, "emergency")
        self._condition = threading.Condition()
        self._pending_checkpoint = None
        self._pending_context = None
        self._pending_emergency = None
        self._pending_tombstone = False
        self._writing = self._stopping = False
        self._writing_context = False
        self._writing_context_signature = None
        self._available = True
        self._generation = 0
        self._kind = TOMBSTONE
        self._normal = self._emergency = self._selected = None
        self._selected_record = self._source = self._staged_emergency = None
        self._context_signature = self._context_id = None
        self._context_bytes = 0
        self._contexts = {}
        self._last_error = self._emergency_error = None
        self._thread = None
        self._owns_directory = False
        if start_worker:
            with _STORE_OWNERS_LOCK:
                if self.directory in _STORE_POISONED:
                    self._available = False
                    self._last_error = (
                        "storage disabled until the Klippy process restarts")
                elif self.directory in _STORE_OWNERS:
                    self._available = False
                    self._last_error = "storage worker is still active"
                else:
                    _STORE_OWNERS.add(self.directory)
                    self._owns_directory = True
        if not self._available:
            return
        try:
            self._load()
        except Exception as exc:
            self._available = False
            self._last_error = str(exc)
            _klog('storage load failed', level=logging.exception)
        if not start_worker:
            return
        if not self._available:
            self._release_owner()
            return
        try:
            self._thread = threading.Thread(
                target=self._worker_main, name="k2-plr-storage", daemon=True)
            self._thread.start()
        except Exception as exc:
            self._available = False
            self._last_error = str(exc)
            self._release_owner()
            _klog('storage worker start failed', level=logging.exception)

    def _release_owner(self):
        if not self._owns_directory:
            return
        with _STORE_OWNERS_LOCK:
            _STORE_OWNERS.discard(self.directory)
        self._owns_directory = False

    def _poison_directory(self):
        with _STORE_OWNERS_LOCK:
            _STORE_POISONED.add(self.directory)

    def _select(self):
        self._selected = self._selected_record = self._source = None
        if self._kind != CHECKPOINT or self._normal is None:
            return
        selected, source = self._normal, "journal"
        if (_lineage(self._normal["snapshot"]) is not None
                and _lineage(self._normal["snapshot"])
                == _lineage(self._emergency and self._emergency["snapshot"])
                and _position(self._emergency["snapshot"])
                > _position(self._normal["snapshot"])):
            selected, source = self._emergency, EMERGENCY
        self._selected_record, self._source = selected, source
        self._selected = selected["snapshot"]

    def _context_path(self, context_id):
        return os.path.join(
            self.directory, "context-%s.json" % context_id)

    def _load_context(self, context_id):
        cached = self._contexts.get(context_id)
        if cached is not None:
            return cached
        with open(self._context_path(context_id), "rb") as stream:
            raw = stream.read()
        if hashlib.sha256(raw).hexdigest() != context_id:
            raise ValueError("context hash mismatch")
        context = json.loads(raw.decode("ascii"))
        file_state = context.get("file") if isinstance(context, dict) else None
        exclude = context.get("exclude") if isinstance(context, dict) else None
        mesh = context.get("mesh") if isinstance(context, dict) else None
        cfs = context.get("cfs") if isinstance(context, dict) else None
        if (not isinstance(context, dict)
                or not isinstance(context.get("session_id"), str)
                or not isinstance(file_state, dict)
                or not isinstance(file_state.get("path"), str)
                or not isinstance(file_state.get("identity"), dict)
                or (exclude is not None
                    and (not isinstance(exclude, dict)
                         or not isinstance(exclude.get("objects"), list)))
                or not isinstance(mesh, dict)
                or type(mesh.get("active")) is not bool
                or not isinstance(cfs, dict)
                or type(cfs.get("parsed")) is not bool):
            raise ValueError("invalid context")
        cached = context, len(raw)
        self._contexts[context_id] = cached
        return cached

    @staticmethod
    def _compose(context, dynamic):
        snapshot = dict(dynamic)
        snapshot.pop("captured_at", None)
        position = snapshot.pop("file_position")
        dynamic_mesh = snapshot.pop("mesh")
        dynamic_cfs = dict(snapshot.pop("cfs"))
        current_object = snapshot.pop("current_object")
        snapshot.update({
            "session_id": context["session_id"],
            "file": dict(context["file"], position=position),
        })
        mesh = dict(context["mesh"])
        if mesh["active"]:
            mesh.update(dynamic_mesh)
        snapshot["mesh"] = mesh
        change = dict(context["cfs"])
        change["current_layer"] = dynamic_cfs.pop("current_layer")
        snapshot["cfs"] = dict(dynamic_cfs, change=change)
        exclude = context["exclude"]
        snapshot["exclude"] = None if exclude is None else {
            "objects": exclude["objects"], "current": current_object}
        return snapshot

    def _inflate(self, record):
        context, context_bytes = self._load_context(record["context_id"])
        record["snapshot"] = self._compose(context, dict(record["dynamic"]))
        record["context_bytes"] = context_bytes
        return record

    def _load(self):
        records, ambiguous = [], False
        for path in self._journal_paths:
            try:
                with open(path, "rb") as stream:
                    raw = stream.read()
            except FileNotFoundError:
                raw = None
            if raw is not None:
                try:
                    record = _decode_record(raw)
                    if record["kind"] == EMERGENCY:
                        raise ValueError("emergency record in journal")
                    if record["kind"] == CHECKPOINT:
                        self._inflate(record)
                    records.append(record)
                except Exception as exc:
                    ambiguous = True
                    _klog(
                        'ignoring %s: %s', path, exc, level=logging.warning)
            if os.path.exists(path + ".tmp"):
                ambiguous = True
        try:
            with open(self._emergency_path, "rb") as stream:
                raw = stream.read()
        except FileNotFoundError:
            raw = None
        if raw is not None:
            try:
                record = _decode_record(raw)
                if record["kind"] != EMERGENCY:
                    raise ValueError("journal record in emergency slot")
                self._emergency = self._inflate(record)
            except Exception as exc:
                self._emergency = None
                self._emergency_error = str(exc)
        if ambiguous:
            self._generation = max(
                (item["generation"] for item in records), default=0)
            self._last_error = "ambiguous journal suppressed recovery"
            self._pending_tombstone = True
            return
        if records:
            latest = max(records, key=lambda item: item["generation"])
            self._generation, self._kind = (
                latest["generation"], latest["kind"])
            self._normal = latest if self._kind == CHECKPOINT else None
        self._select()

    def request_context(self, signature, context):
        if not isinstance(context, dict):
            return None
        with self._condition:
            if (self._stopping or not self._available
                    or self._pending_tombstone):
                return None
            if signature == self._context_signature:
                return self._context_id
            if (self._pending_context is None
                    or self._pending_context[0] != signature):
                self._pending_context = signature, context
                self._condition.notify()
        return None

    def context_id(self, signature):
        with self._condition:
            if signature == self._context_signature:
                return self._context_id
            return None

    def context_pending(self, signature):
        with self._condition:
            return ((self._pending_context is not None
                     and self._pending_context[0] == signature)
                    or self._writing_context_signature == signature)

    def prepare(self, context_id, dynamic):
        if not isinstance(dynamic, dict):
            return None
        try:
            dynamic_bytes = _canonical(dynamic)
            with self._condition:
                if (self._stopping or not self._available
                        or self._pending_tombstone):
                    return None
                context, context_bytes = self._contexts[context_id]
            snapshot = self._compose(context, dict(dynamic))
            emergency = _encode_record(
                EMERGENCY, context_id, dynamic_bytes)
        except Exception as exc:
            with self._condition:
                self._emergency_error = str(exc)
            return None
        return {"context_id": context_id, "dynamic": dynamic,
                "dynamic_bytes": dynamic_bytes, "snapshot": snapshot,
                "context_bytes": context_bytes,
                "emergency_bytes": emergency}

    def submit(self, prepared):
        if not isinstance(prepared, dict) or "dynamic_bytes" not in prepared:
            return False
        with self._condition:
            if (self._stopping or not self._available
                    or self._pending_tombstone):
                return False
            self._pending_checkpoint = prepared
            self._condition.notify()
        return True

    def _emergency_eligible(self, prepared):
        snapshot = prepared and prepared.get("snapshot")
        return (self._kind == CHECKPOINT and not self._pending_tombstone
                and self._normal is not None
                and _lineage(self._normal["snapshot"]) is not None
                and _lineage(self._normal["snapshot"]) == _lineage(snapshot)
                and _position(snapshot) > _position(self._normal["snapshot"]))

    def stage_emergency(self, prepared):
        with self._condition:
            if self._stopping or not self._emergency_eligible(prepared):
                return False
            self._staged_emergency = prepared
        return True

    def queue_emergency(self):
        with self._condition:
            staged = self._staged_emergency
            if (self._stopping or not self._available or staged is None
                    or not self._emergency_eligible(staged)):
                return False
            self._pending_emergency = staged
            self._condition.notify()
        return True

    def _set_tombstone_state(self):
        self._kind = TOMBSTONE
        self._normal = self._emergency = self._selected = None
        self._selected_record = self._source = self._staged_emergency = None
        self._pending_checkpoint = None
        self._pending_context = None
        self._pending_emergency = None
        self._context_signature = self._context_id = None
        self._context_bytes = 0

    def discard(self):
        with self._condition:
            if self._stopping or not self._available:
                return False
            self._set_tombstone_state()
            self._pending_tombstone = True
            self._condition.notify()
        return True

    def checkpoint(self):
        with self._condition:
            return copy.deepcopy(self._selected)

    def recovery_point(self):
        with self._condition:
            if self._selected is None:
                return None
            state = self._selected["file"]
            return state["path"], state["position"]

    def status(self):
        with self._condition:
            record = self._selected_record
            captured_at = (record and record["dynamic"].get("captured_at"))
            return {
                "generation": self._generation,
                "recoverable": self._selected is not None,
                "recovery_source": self._source,
                "write_pending": bool(
                    self._writing or self._pending_tombstone
                    or self._pending_checkpoint is not None
                    or self._pending_context is not None
                    or self._pending_emergency is not None),
                "context_id": (self._context_id or (
                    record and record["context_id"])),
                "context_pending": bool(
                    self._pending_context is not None
                    or self._writing_context),
                "context_bytes": (self._context_bytes or (
                    record and record["context_bytes"]) or 0),
                "dynamic_bytes": (len(record["dynamic_bytes"])
                                  if record is not None else 0),
                "checkpoint_age": (max(0.0, time.time() - captured_at)
                                   if isinstance(captured_at, (int, float))
                                   else None),
                "last_error": self._last_error,
                "emergency_error": self._emergency_error,
                "available": self._available,
                "directory": self.directory,
            }

    def close(self):
        with self._condition:
            self._stopping = True
            self._condition.notify()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if not self._thread.is_alive():
                self._release_owner()
        else:
            self._release_owner()

    def _worker_main(self):
        try:
            self._worker()
        finally:
            self._release_owner()

    def _worker(self):
        while True:
            with self._condition:
                while (not self._pending_tombstone
                       and self._pending_checkpoint is None
                       and self._pending_context is None
                       and self._pending_emergency is None):
                    if self._stopping or not self._available:
                        return
                    self._condition.wait()
                if self._pending_tombstone:
                    task, kind, prepared = "record", TOMBSTONE, None
                    self._pending_tombstone = False
                elif self._pending_emergency is not None:
                    task, prepared = "emergency", self._pending_emergency
                    self._pending_emergency = None
                    kind = context = None
                elif self._pending_context is not None:
                    task, context = "context", self._pending_context
                    self._pending_context = None
                    kind = prepared = None
                else:
                    task, kind, prepared = (
                        "record", CHECKPOINT, self._pending_checkpoint)
                    self._pending_checkpoint = None
                generation = self._generation + 1
                self._writing = True
                self._writing_context = task == "context"
                self._writing_context_signature = (
                    context[0] if task == "context" else None)
            try:
                if task == "context":
                    context_id, context_bytes, durable_context = (
                        self._commit_context(context))
                elif task == "emergency":
                    with self._condition:
                        eligible = (not self._pending_tombstone
                                    and self._emergency_eligible(prepared))
                    if eligible:
                        self._write_direct(prepared["emergency_bytes"])
                else:
                    self._commit(generation, kind, prepared)
                    if kind == CHECKPOINT:
                        self._seed_emergency(prepared)
            except Exception as exc:
                if task == "emergency":
                    with self._condition:
                        self._emergency_error = str(exc)
                        self._writing = False
                        self._condition.notify_all()
                    _klog(
                        'emergency write failed', level=logging.exception)
                    continue
                with self._condition:
                    log_failure = task == "context" or kind != TOMBSTONE or self._available
                    self._last_error = str(exc)
                    self._available = False
                    self._set_tombstone_state()
                    self._writing = False
                    self._writing_context = False
                    self._writing_context_signature = None
                    if kind == TOMBSTONE:
                        self._pending_tombstone = True
                    stopping = self._stopping
                    if stopping and kind == TOMBSTONE:
                        self._poison_directory()
                    self._condition.notify_all()
                if log_failure:
                    _klog(
                        '%s write failed', "context" if task == "context" else kind, level=logging.exception)
                invalidated = self._invalidate_recovery_files()
                if invalidated:
                    with self._condition:
                        self._pending_tombstone = False
                if task == "context":
                    return
                if stopping and kind == TOMBSTONE:
                    return
                if kind == TOMBSTONE and not invalidated:
                    with self._condition:
                        if self._stopping:
                            self._poison_directory()
                            return
                        self._condition.wait(timeout=30.0)
                continue
            with self._condition:
                if task == "context":
                    self._contexts[context_id] = durable_context, context_bytes
                    if not self._pending_tombstone:
                        self._context_signature = context[0]
                        self._context_id = context_id
                        self._context_bytes = context_bytes
                elif task == "emergency":
                    if eligible and not self._pending_tombstone:
                        self._emergency = prepared
                        self._emergency_error = None
                        if self._staged_emergency is prepared:
                            self._staged_emergency = None
                        self._select()
                else:
                    self._generation = generation
                    if kind == TOMBSTONE or not self._pending_tombstone:
                        self._kind = kind
                        self._normal = prepared if kind == CHECKPOINT else None
                        self._select()
            if task != "record" or kind != TOMBSTONE:
                try:
                    self._prune_contexts()
                except Exception:
                    _klog(
                        'context cleanup failed', level=logging.exception)
            with self._condition:
                if self._available:
                    self._last_error = None
                self._writing = False
                self._writing_context = False
                self._writing_context_signature = None
                self._condition.notify_all()

    def _sync_directory(self):
        directory = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _atomic_write(self, path, encoded):
        os.makedirs(self.directory, exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        self._sync_directory()

    def _commit_context(self, pending):
        signature, context = pending
        encoded = _canonical(context)
        context_id = hashlib.sha256(encoded).hexdigest()
        path = self._context_path(context_id)
        try:
            with open(path, "rb") as stream:
                existing = stream.read()
            if existing != encoded:
                raise ValueError("existing context hash collision")
        except FileNotFoundError:
            self._atomic_write(path, encoded)
        return context_id, len(encoded), json.loads(encoded.decode("ascii"))

    def _commit(self, generation, kind, prepared):
        if kind == TOMBSTONE:
            context_id, dynamic_bytes = None, b"null"
        else:
            context_id = prepared["context_id"]
            dynamic_bytes = prepared["dynamic_bytes"]
        encoded = _encode_record(
            kind, context_id, dynamic_bytes, generation)
        target = self._journal_paths[generation & 1]
        self._atomic_write(target, encoded)
        if kind == TOMBSTONE:
            self._atomic_write(
                self._journal_paths[(generation & 1) ^ 1], encoded)
            for path in self._journal_paths:
                try:
                    os.unlink(path + ".tmp")
                except FileNotFoundError:
                    pass
            self._remove_session_files()

    def _remove_session_files(self):
        removed = False
        try:
            os.unlink(self._emergency_path)
            removed = True
        except FileNotFoundError:
            pass
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            names = ()
        except OSError as exc:
            _klog(
                'context cleanup skipped: %s', exc, level=logging.warning)
            return
        for name in names:
            if not name.startswith("context-"):
                continue
            try:
                os.unlink(os.path.join(self.directory, name))
                removed = True
            except FileNotFoundError:
                pass
        if removed:
            self._sync_directory()
        self._contexts.clear()

    def _prune_contexts(self):
        keep = set()
        with self._condition:
            records = (
                self._normal, self._emergency, self._staged_emergency,
                self._pending_emergency, self._pending_checkpoint)
            if self._context_id is not None:
                keep.add(self._context_id)
            keep.update(record["context_id"] for record in records
                        if isinstance(record, dict)
                        and isinstance(record.get("context_id"), str))
        for path in self._journal_paths + (self._emergency_path,):
            try:
                with open(path, "rb") as stream:
                    record = _decode_record(stream.read())
            except FileNotFoundError:
                continue
            except Exception as exc:
                _klog(
                    'context cleanup skipped: %s', exc, level=logging.warning)
                return
            if record["context_id"] is not None:
                keep.add(record["context_id"])
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            names = ()
        except OSError as exc:
            _klog(
                'context cleanup skipped: %s', exc, level=logging.warning)
            return
        removed = set()
        for name in names:
            if (len(name) != 77 or not name.startswith("context-")
                    or not name.endswith(".json")):
                continue
            context_id = name[8:-5]
            if (context_id in keep
                    or any(char not in "0123456789abcdef"
                           for char in context_id)):
                continue
            try:
                os.unlink(os.path.join(self.directory, name))
                removed.add(context_id)
            except FileNotFoundError:
                pass
            except OSError as exc:
                _klog(
                    'context cleanup failed: %s', exc, level=logging.warning)
        with self._condition:
            for context_id in tuple(self._contexts):
                if context_id not in keep:
                    self._contexts.pop(context_id, None)
        if removed:
            try:
                self._sync_directory()
            except OSError as exc:
                _klog(
                    'context cleanup sync failed: %s', exc, level=logging.warning)

    def _invalidate_recovery_files(self):
        removed, ok = False, True
        for path in self._journal_paths + (self._emergency_path,):
            for target in (path, path + ".tmp"):
                try:
                    os.unlink(target)
                    removed = True
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    ok = False
                    _klog(
                        'failed to invalidate %s: %s', target, exc, level=logging.error)
        if removed:
            try:
                self._sync_directory()
            except OSError as exc:
                ok = False
                _klog(
                    'invalidation sync failed: %s', exc, level=logging.error)
        return ok

    def _seed_emergency(self, prepared):
        if os.path.exists(self._emergency_path):
            return
        with self._condition:
            if self._emergency_error is not None:
                return
        try:
            self._atomic_write(
                self._emergency_path, prepared["emergency_bytes"])
        except Exception as exc:
            with self._condition:
                self._emergency_error = str(exc)
            _klog('emergency seed failed', level=logging.exception)

    def _write_direct(self, encoded):
        fd = os.open(
            self._emergency_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o644)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short emergency write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)


def _passive(fn):
    def wrapped(self, *args):
        try:
            return fn(self, *args)
        except Exception:
            _klog('%s failed', fn.__name__, level=logging.exception)
    return wrapped


class _VirtualSDGCodeProxy:
    def __init__(self, delegate, owner):
        self.delegate = delegate
        self.owner = owner
        self.failed = False
        self.disabled = False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def reset_failure(self):
        self.failed = False

    def run_script(self, script):
        try:
            result = self.delegate.run_script(script)
        except BaseException:
            self.failed = True
            raise
        if (not self.failed and not self.disabled
                and self.owner.candidate_due):
            try:
                self.owner._after_sd_line(script)
            except Exception:
                self.disabled = True
                try:
                    self.owner._disable("source-line observer")
                except Exception:
                    _klog(
                        'observer disable failed', level=logging.exception)
        return result


class PowerLossRecovery:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.startup_output_handlers = len(self.gcode.output_callbacks)
        self.store = AtomicSnapshotStore(config.get(
            "state_path", "/mnt/UDISK/printer_data/power_loss_recovery"))
        self.candidate_interval = config.getfloat(
            "candidate_interval", 0.5, minval=0.1)
        self.checkpoint_interval = config.getfloat(
            "checkpoint_interval", 10.0, minval=1.0)
        self.recovery_lift = config.getfloat("recovery_lift", 5.0, minval=1.0)
        self.maximum_recovery_z = config.getfloat(
            "maximum_recovery_z", 350.0, minval=25.0, maxval=359.0)
        self.recovery_travel_speed = config.getfloat(
            "recovery_travel_speed", 100.0, above=0.0)
        self.recovery_z_speed = config.getfloat(
            "recovery_z_speed", 15.0, above=0.0)
        self.nozzle_standby = config.getfloat(
            "nozzle_standby", 140.0, minval=0.0)

        self.v_sd = self.toolhead = self.gcode_move = self.print_stats = None
        self.z_align = None
        self.motion_mcus = ()
        self.proxy = None
        self.enabled = False
        self.disabled_reason = "printer not ready"
        self.recovering = False
        self.printing = False
        self.started = False
        self.fresh_start = False
        self.recovery_start = False
        self.recovered = False
        self.inhibited = False
        self.session = 0
        self.session_id = self.session_file = self.session_identity = None
        self.max_print_z = None
        self.candidate_due = False
        self.candidates = deque(maxlen=64)
        self.candidate_id = self.submitted_id = 0
        self.last_candidate_time = self.last_submit_time = -1.0e30
        self.safe_candidate = self.completion = None
        self.pending_box = None
        self._context_signature = self._context_sources = None
        self.context_id = None
        self.fans = {"part": 0.0, "aux": 0.0}

        for name, handler in (
            ("PLR_STATUS", self.cmd_PLR_STATUS),
            ("PLR_DISCARD", self.cmd_PLR_DISCARD),
            ("PLR_RECOVER", self.cmd_PLR_RECOVER),
        ):
            self.gcode.register_command(name, handler)
        for event, handler in (
            ("klippy:ready", self._handle_ready),
            ("klippy:shutdown", self._handle_power_signal),
            ("klippy:disconnect", self._handle_disconnect),
            ("virtual_sdcard:load_file", self._handle_load_file),
            ("virtual_sdcard:reset_file", self._handle_reset_file),
            ("print_stats:start_printing", self._handle_print_start),
            ("print_stats:paused_printing", self._handle_print_pause),
            ("print_stats:complete_printing", self._handle_print_complete),
            ("print_stats:error_printing", self._handle_print_error),
            ("print_stats:cancelled_printing", self._handle_print_cancel),
            ("motor_control:protection_signal", self._handle_power_signal),
        ):
            self.printer.register_event_handler(event, handler)

    # Lifecycle -------------------------------------------------------

    def _handle_ready(self):
        try:
            if not self.store.status()["available"]:
                raise RuntimeError(self.store.status()["last_error"])
            self.v_sd = self.printer.lookup_object("virtual_sdcard")
            self.toolhead = self.printer.lookup_object("toolhead")
            self.gcode_move = self.printer.lookup_object("gcode_move")
            self.print_stats = self.printer.lookup_object("print_stats")
            self.z_align = self.printer.lookup_object("z_align")
            self.motion_mcus = (
                self.printer.lookup_object("mcu"),
                self.printer.lookup_object("mcu nozzle_mcu"),
            )
            self.proxy = _VirtualSDGCodeProxy(self.v_sd.gcode, self)
            self.v_sd.gcode = self.proxy
            self._install_fan_shadows()
            self.reactor.register_timer(
                self._checkpoint_timer,
                self.reactor.monotonic() + TIMER_INTERVAL)
            self.reactor.register_timer(
                self._recovery_notice_timer,
                self.reactor.monotonic() + TIMER_INTERVAL)
            self.enabled = True
            self.disabled_reason = None
        except Exception as exc:
            self.disabled_reason = str(exc)
            _klog('initialization failed', level=logging.exception)

    def _recovery_notice_timer(self, eventtime):
        try:
            if self.store.recovery_point() is None:
                return self.reactor.NEVER
            if len(self.gcode.output_callbacks) <= self.startup_output_handlers:
                return eventtime + TIMER_INTERVAL
            snapshot = self.store.checkpoint()
            if snapshot is not None:
                self._validate_snapshot(snapshot)
                state = snapshot["file"]
                self.gcode.respond_raw(
                    "!! [PLR] Recovery available for %s at byte %d. Run "
                    "PLR_RECOVER CONFIRM=1 to resume; starting a new print "
                    "discards it automatically."
                    % (state["path"], state["position"]))
                self._emit_recovery_prompt(state)
        except Exception as exc:
            _klog('recovery notice skipped: %s', exc, level=logging.info)
        return self.reactor.NEVER

    def _emit_recovery_prompt(self, state):
        # Fluidd/Mainsail action:prompt dialog.  An unanswered prompt has no
        # following prompt_end in Moonraker's G-code store, so a fresh UI
        # load replays it even when no client was connected at emit time.
        self.gcode.respond_raw("// action:prompt_begin Power-Loss Recovery")
        self.gcode.respond_raw(
            "// action:prompt_text Recovery available for %s at byte %d. "
            "Starting a new print discards it automatically."
            % (state["path"], state["position"]))
        self.gcode.respond_raw(
            "// action:prompt_button Resume print|"
            "PLR_RECOVER CONFIRM=1|primary")
        self.gcode.respond_raw(
            "// action:prompt_button Discard|PLR_DISCARD|error")
        self.gcode.respond_raw("// action:prompt_show")

    def _close_recovery_prompt(self):
        try:
            self.gcode.respond_raw("// action:prompt_end")
        except Exception as exc:
            _klog('prompt close skipped: %s', exc, level=logging.info)

    @_passive
    def _handle_disconnect(self):
        self.store.close()

    def _disable(self, reason):
        self.enabled = False
        self.disabled_reason = reason
        self.inhibited = True
        self.candidate_due = False
        self.candidates.clear()
        self.safe_candidate = self.completion = None
        _klog('disabled after %s', reason, level=logging.error)

    def _reset_session(self, discard=False):
        self.session += 1
        self.printing = self.started = self.fresh_start = False
        self.recovery_start = self.recovered = self.inhibited = False
        self.session_id = self.session_file = self.session_identity = None
        self.max_print_z = self.pending_box = None
        self.candidate_due = False
        self.candidates.clear()
        self.safe_candidate = self.completion = None
        self._context_signature = self._context_sources = None
        self.context_id = None
        self.submitted_id = self.candidate_id
        if self.proxy is not None:
            self.proxy.reset_failure()
        if discard:
            self.store.discard()

    @_passive
    def _handle_load_file(self):
        if self.recovering:
            return
        discard = self.started
        self._reset_session(discard=discard)
        self.fresh_start = True
        self._start_loaded_session()

    @_passive
    def _handle_reset_file(self):
        if not self.recovering:
            self._reset_session(discard=self.started)

    @_passive
    def _handle_print_start(self):
        if self.fresh_start and not self.recovery_start:
            self.store.discard()
            self._close_recovery_prompt()
            self.fresh_start = False
        self.printing = self.started = True
        self.candidate_due = True
        if self.session_file is None:
            self._start_loaded_session()
        if self.proxy is not None:
            self.proxy.reset_failure()
        if self.recovery_start:
            try:
                self._bind_box_state()
            except Exception:
                self.printing = False
                self.v_sd.must_pause_work = True
                raise
            self.recovered = True
            self.recovery_start = False

    def _handle_print_pause(self):
        self.printing = False
        self.candidate_due = False

    @_passive
    def _handle_print_error(self):
        if not self.recovering:
            self._reset_session(discard=not self.recovered)

    @_passive
    def _handle_print_cancel(self):
        if not self.recovering:
            self._reset_session(discard=True)

    @_passive
    def _handle_print_complete(self):
        self.printing = False
        self.candidate_due = False
        if self.session_file is not None:
            self.completion = {"session": self.session}
            self._attach_watermark(self.completion)

    @_passive
    def _handle_power_signal(self, _detail=None):
        candidate = self.safe_candidate
        if (self.enabled and self.session_file is not None
                and not self.recovering and not self.inhibited
                and candidate is not None
                and candidate["session"] == self.session):
            self.store.queue_emergency()

    def _start_loaded_session(self):
        path = self.v_sd.file_path()
        if not path:
            return
        path = os.path.realpath(path)
        identity = self._file_identity(path)
        fans = self._read_fans()
        self.session_file = path
        self.session_identity = identity
        self.session_id = os.urandom(16).hex()
        self.fans.update(fans)

    def get_status(self, _eventtime):
        try:
            storage = self.store.status()
            point = self.store.recovery_point()
            return {
                "enabled": self.enabled,
                "disabled_reason": self.disabled_reason,
                "recovering": self.recovering,
                "recoverable": point is not None,
                "file": point and point[0],
                "position": point and point[1],
                "context_id": storage.get("context_id"),
                "context_pending": storage.get("context_pending", False),
                "context_bytes": storage.get("context_bytes", 0),
                "dynamic_bytes": storage.get("dynamic_bytes", 0),
                "checkpoint_age": storage.get("checkpoint_age"),
                "storage": storage,
            }
        except Exception as exc:
            return {"enabled": False, "recoverable": False,
                    "recovering": self.recovering,
                    "disabled_reason": str(exc)}

    # Passive tracking ------------------------------------------------

    def _install_fan_shadows(self):
        for key, object_name in (("part", "fan"), ("aux", "fan_generic aux_fans")):
            fan = self.printer.lookup_object(object_name).fan
            original = fan.set_speed_from_command

            def set_speed(value, original=original, key=key):
                result = original(value)
                try:
                    self.fans[key] = max(0.0, min(1.0, float(value)))
                except Exception:
                    _klog('fan shadow failed', level=logging.exception)
                return result

            fan.set_speed_from_command = set_speed

    def _read_fans(self):
        return {
            key: float(self.printer.lookup_object(name).fan.last_req_value)
            for key, name in (
                ("part", "fan"), ("aux", "fan_generic aux_fans"))
        }

    def _after_sd_line(self, _line=None):
        self.candidate_due = False
        if (not self.enabled or self.recovering or self.inhibited
                or not self.printing
                or not self.v_sd.is_cmd_from_sd()
                or self.session_file is None):
            return
        now = self.reactor.monotonic()
        self.last_candidate_time = now
        z = float(self.gcode_move.last_position[2])
        if self.max_print_z is None or z > self.max_print_z:
            self.max_print_z = z
            if self._clearance(z) is None:
                self._inhibit("printed Z exceeds recovery clearance")
                return
        if self._tuning_active() or self._exclude_active():
            self._inhibit("unsupported move transform")
            return
        if not self._homed(now):
            return
        self._capture_candidate(self.v_sd.get_file_position(), now)

    def _capture_candidate(self, position, eventtime):
        if self.inhibited:
            return
        sources = self._read_context_sources()
        if sources is None:
            return
        signature = sources["signature"]
        if signature != self._context_signature:
            self._context_signature = signature
            self._context_sources = sources
            self.context_id = self.store.request_context(
                signature, self._capture_context(sources))
            self.candidates.clear()
            self.safe_candidate = None
            return
        if self.context_id is None:
            self.context_id = self.store.context_id(signature)
            if (self.context_id is None
                    and not self.store.context_pending(signature)):
                self.store.request_context(
                    signature, self._capture_context(sources))
        if self.context_id is None:
            return
        frame = self.z_align.capture_reference_frame()
        if frame is None or self.max_print_z is None:
            return
        payload = self._capture_state(
            position, eventtime, self._dynamic_context_state(sources), frame)
        if payload["heaters"]["extruder"] < 150.0:
            return
        self.candidate_id += 1
        candidate = {
            "id": self.candidate_id,
            "session": self.session,
            "context_id": self.context_id,
            "payload": payload,
            "staged": False,
            "prepared": None,
        }
        self._attach_watermark(candidate)
        self.candidates.append(candidate)

    def _attach_watermark(self, candidate):
        move = self.toolhead.lookahead.get_last()
        delay = float(self.toolhead.kin_flush_delay)
        if move is None:
            candidate["target"] = float(self.toolhead.print_time)
            candidate["flush_target"] = min(
                candidate["target"] + delay,
                float(self.toolhead.need_flush_time))
            return

        def scheduled(end_time, candidate=candidate, delay=delay):
            candidate["target"] = float(end_time)
            candidate["flush_target"] = float(end_time) + delay

        move.timing_callbacks.append(scheduled)

    def _checkpoint_timer(self, eventtime):
        if not self.enabled or self.printer.is_shutdown():
            return self.reactor.NEVER
        try:
            self._promote(eventtime)
            if (self.printing
                    and not self.inhibited and not self.recovering
                    and eventtime - self.last_candidate_time
                    >= self.candidate_interval):
                self.candidate_due = True
        except Exception:
            self._disable("checkpoint timer")
            return self.reactor.NEVER
        return eventtime + TIMER_INTERVAL

    def _promote(self, eventtime):
        if self.printer.is_shutdown():
            return
        storage = self.store.status()
        if not storage["available"]:
            raise IOError(storage["last_error"] or "storage unavailable")
        if self.session_file is not None and (
                self._tuning_active() or self._exclude_active()):
            self._inhibit("unsupported move transform")
        if self.completion is not None and self._mature(self.completion, eventtime):
            self.store.discard()
            self._reset_session()
            return
        newest = next((candidate for candidate in reversed(self.candidates)
                       if candidate["session"] == self.session
                       and candidate["id"] > self.submitted_id
                       and self._mature(candidate, eventtime)), None)
        if newest is not None:
            self.safe_candidate = newest
            if newest["prepared"] is None:
                newest["prepared"] = self.store.prepare(
                    newest["context_id"], newest["payload"])
            if newest["prepared"] is None:
                return
            if not newest["staged"]:
                newest["staged"] = self.store.stage_emergency(
                    newest["prepared"])
        if (newest is None
                or eventtime - self.last_submit_time < self.checkpoint_interval):
            return
        if self.store.submit(newest["prepared"]):
            self.last_submit_time = eventtime
            self.submitted_id = newest["id"]
            while self.candidates and self.candidates[0]["id"] <= newest["id"]:
                self.candidates.popleft()

    def _mature(self, candidate, eventtime):
        target = candidate.get("target")
        flush_target = candidate.get("flush_target")
        if target is None or flush_target is None:
            return False
        if self.toolhead.last_flush_time < flush_target:
            return False
        if "flushed_at" not in candidate:
            candidate["flushed_at"] = float(eventtime)
            return False
        for mcu in self.motion_mcus:
            clocksync = getattr(mcu, "_clocksync", None)
            if clocksync is None:
                return False
            sample = float(clocksync.last_prediction_time)
            if (sample <= candidate["flushed_at"]
                    or mcu.estimated_print_time(sample) <= target):
                return False
        return True

    def _inhibit(self, reason):
        if self.inhibited:
            return
        self.inhibited = True
        self.candidate_due = False
        self.candidates.clear()
        self.safe_candidate = None
        self.store.discard()
        _klog('session inhibited: %s', reason, level=logging.info)

    def _clearance(self, saved_z):
        limit = min(self.maximum_recovery_z, float(self.z_align.zmax))
        target = float(saved_z) + self.recovery_lift
        return target if target <= limit else None

    def _homed(self, eventtime):
        axes = self.toolhead.get_kinematics().get_status(eventtime)["homed_axes"]
        return all(axis in axes for axis in "xyz")

    def _tuning_active(self):
        obj = self.printer.lookup_object("tuning_tower", None)
        return obj is not None and obj.is_active()

    def _exclude(self):
        return self.printer.lookup_object("exclude_object", None)

    def _exclude_active(self):
        obj = self._exclude()
        return obj is not None and bool(obj.excluded_objects)

    def _capture_exclude(self):
        obj = self._exclude()
        if obj is None:
            return None
        return {"objects": obj.objects,
                "current": obj.current_object}

    def _restore_exclude(self, state):
        obj = self._exclude()
        if obj is None or state is None:
            return
        obj.objects = copy.deepcopy(state["objects"])
        obj.current_object = state["current"]
        obj.excluded_objects = []
        obj.in_excluded_region = False

    # Box state -------------------------------------------------------

    def _capture_box_state(self):
        box = self.printer.lookup_object("box", None)
        if box is None:
            return {
                "dynamic": {"kind": "none",
                            "current_layer": self.print_stats.info_current_layer},
                "static": {"parsed": False, "matrix": None,
                           "temp_print": None,
                           "temp_initial_layer": None,
                           "total_layer": self.print_stats.info_total_layer},
                "signature": (False, None, None, None,
                              self.print_stats.info_total_layer),
            }
        if (box.operation_depth or box.runout_active
                or box.change_engine.pending is not None):
            return None
        slot = box.last_loaded_slot
        if box.is_physical_slot(slot):
            dynamic = {"kind": "physical", "slot": int(slot)}
        elif slot == box.external_slot:
            dynamic = {"kind": "external"}
        else:
            return None
        engine = box.change_engine
        epoch = self.print_stats.print_start_time
        parsed = (epoch is not None and engine.parsed_epoch == epoch
                  and engine.matrix is not None)
        static = {
            "parsed": parsed,
            "matrix": engine.matrix if parsed else None,
            "temp_print": engine.temp_print if parsed else None,
            "temp_initial_layer": (
                engine.temp_initial_layer if parsed else None),
            "total_layer": self.print_stats.info_total_layer,
        }
        dynamic["current_layer"] = self.print_stats.info_current_layer
        return {
            "dynamic": dynamic,
            "static": static,
            "signature": (
                parsed, id(static["matrix"]), id(static["temp_print"]),
                id(static["temp_initial_layer"]), static["total_layer"]),
        }

    def _stage_box_state(self, token):
        self.pending_box = None
        if token["kind"] == "none":
            return
        state = token["change"]
        if state["parsed"] and not isinstance(state["matrix"], list):
            raise ValueError("invalid saved CFS matrix")
        engine = self.printer.lookup_object("box").change_engine
        engine.matrix = copy.deepcopy(state["matrix"]) if state["parsed"] else None
        engine.temp_print = copy.deepcopy(state["temp_print"])
        engine.temp_initial_layer = copy.deepcopy(state["temp_initial_layer"])
        engine.parsed_epoch = engine.prepared_epoch = None
        self.print_stats.info_total_layer = state["total_layer"]
        self.print_stats.info_current_layer = state["current_layer"]
        self.pending_box = state["parsed"]

    def _bind_box_state(self):
        if self.pending_box is None:
            return
        parsed = self.pending_box
        engine = self.printer.lookup_object("box").change_engine
        epoch = self.print_stats.print_start_time
        engine.parsed_epoch = epoch if parsed else None
        engine.prepared_epoch = epoch
        self.pending_box = None

    # Snapshot --------------------------------------------------------

    def _read_context_sources(self):
        exclude = self._exclude()
        bed = self.printer.lookup_object("bed_mesh", None)
        mesh = None if bed is None else bed.get_mesh()
        box = self._capture_box_state()
        if box is None:
            return None
        objects = None if exclude is None else exclude.objects
        return {
            "signature": (self.session_id, id(objects),
                          id(mesh), box["signature"]),
            "exclude": exclude,
            "objects": objects,
            "bed": bed,
            "mesh": mesh,
            "box": box,
        }

    def _capture_context(self, sources):
        mesh = sources["mesh"]
        if mesh is None:
            mesh_state = {"active": False}
        else:
            mesh_state = {
                "active": True,
                "profile": mesh.get_profile_name(),
                "params": mesh.get_mesh_params(),
                "matrix": mesh.get_probed_matrix(),
            }
        return {
            "session_id": self.session_id,
            "file": {"path": self._relative_path(self.session_file),
                     "identity": self.session_identity},
            "exclude": (None if sources["objects"] is None else
                        {"objects": sources["objects"]}),
            "mesh": mesh_state,
            "cfs": sources["box"]["static"],
        }

    @staticmethod
    def _dynamic_context_state(sources):
        mesh = sources["mesh"]
        bed = sources["bed"]
        exclude = sources["exclude"]
        return {
            "mesh": ({} if mesh is None else {
                "offsets": list(mesh.mesh_offsets),
                "tool_offset": float(bed.tool_offset),
            }),
            "cfs": sources["box"]["dynamic"],
            "current_object": (
                None if exclude is None else exclude.current_object),
        }

    def _capture_state(self, position, eventtime, context_state, frame):
        heaters = self.printer.lookup_object("heaters")
        chamber = self.printer.lookup_object(
            "temperature_fan chamber_exhaust_fans")
        floor = self.printer.lookup_object(
            "temperature_fan_manual_floor chamber_exhaust_fans")
        extruder = self.toolhead.get_extruder()
        stepper = extruder.extruder_stepper
        kin = self.toolhead.get_kinematics()
        arcs = self.printer.lookup_object("gcode_arcs", None)
        move = self.gcode_move
        return {
            "captured_at": time.time(),
            "file_position": int(position),
            "gcode": {
                "absolute_coord": bool(move.absolute_coord),
                "absolute_extrude": bool(move.absolute_extrude),
                "base_position": list(move.base_position[:4]),
                "last_position": list(move.last_position[:4]),
                "homing_position": list(move.homing_position[:4]),
                "speed": float(move.speed),
                "speed_factor": float(move.speed_factor),
                "extrude_factor": float(move.extrude_factor),
                "max_print_z": float(self.max_print_z),
            },
            "z_frame": copy.deepcopy(frame),
            "mesh": context_state["mesh"],
            "heaters": {name: float(heaters.lookup_heater(name).get_status(
                eventtime)["target"])
                for name in ("extruder", "heater_bed", "chamber_heater")},
            "fans": {"part": self.fans["part"], "aux": self.fans["aux"],
                     "chamber_target": float(chamber.target_temp),
                     "chamber_min": float(chamber.min_speed),
                     "chamber_max": float(chamber.max_speed),
                     "chamber_manual": float(floor.manual_speed)},
            "pressure_advance": {"advance": float(stepper.pressure_advance),
                                 "smooth": float(
                                     stepper.pressure_advance_smooth_time)},
            "limits": {"velocity": float(self.toolhead.max_velocity),
                       "accel": float(self.toolhead.max_accel),
                       "cruise": float(self.toolhead.min_cruise_ratio),
                       "corner": float(self.toolhead.square_corner_velocity),
                       "z_velocity": float(kin.max_z_velocity),
                       "z_accel": float(kin.max_z_accel)},
            "arc_plane": 0 if arcs is None else int(arcs.plane),
            "cfs": context_state["cfs"],
            "current_object": context_state["current_object"],
        }

    def _relative_path(self, path):
        root = os.path.realpath(self.v_sd.sdcard_dirname)
        relative = os.path.relpath(os.path.realpath(path), root)
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            raise ValueError("G-code path is outside Virtual SD")
        return relative

    @staticmethod
    def _file_identity(path):
        stat = os.stat(path)
        digest = hashlib.sha256(str(stat.st_size).encode("ascii"))
        with open(path, "rb") as stream:
            digest.update(stream.read(FINGERPRINT_BYTES))
            if stat.st_size > FINGERPRINT_BYTES:
                stream.seek(stat.st_size - FINGERPRINT_BYTES)
                digest.update(stream.read(FINGERPRINT_BYTES))
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "head_tail_sha256": digest.hexdigest()}

    # Explicit recovery ----------------------------------------------

    def cmd_PLR_STATUS(self, gcmd):
        """Report power-loss recovery status."""
        status = self.get_status(self.reactor.monotonic())
        storage = status.get("storage", {})
        gcmd.respond_info(
            "PLR enabled=%s recoverable=%s source=%s position=%s "
            "context=%s pending=%s context_bytes=%s dynamic_bytes=%s "
            "checkpoint_age=%s error=%s" % (
                status.get("enabled"), status.get("recoverable"),
                storage.get("recovery_source"), status.get("position"),
                status.get("context_id"), status.get("context_pending"),
                status.get("context_bytes"), status.get("dynamic_bytes"),
                status.get("checkpoint_age"),
                storage.get("last_error")))

    def cmd_PLR_DISCARD(self, gcmd):
        """Discard the saved power-loss recovery checkpoint."""
        self.inhibited = True
        self.candidates.clear()
        self.safe_candidate = self.completion = None
        if not self.store.discard():
            raise gcmd.error("PLR storage is unavailable")
        gcmd.respond_info("PLR checkpoint discard queued")
        self._close_recovery_prompt()

    def cmd_PLR_RECOVER(self, gcmd):
        """Resume from the saved power-loss recovery checkpoint."""
        if gcmd.get_int("CONFIRM", 0) != 1:
            raise gcmd.error("PLR_RECOVER requires CONFIRM=1")
        if not self.enabled:
            raise gcmd.error("PLR is disabled: %s" % self.disabled_reason)
        if self.recovering or self.v_sd.is_active() or self.print_stats.state in (
                "printing", "paused"):
            raise gcmd.error("A print or recovery is already active")
        snapshot = self.store.checkpoint()
        if snapshot is None:
            raise gcmd.error("No durable PLR checkpoint")
        self.recovering = True
        loaded = False
        try:
            path = self._validate_snapshot(snapshot)
            self.v_sd._reset_file()
            self.v_sd._load_file(
                gcmd, snapshot["file"]["path"], check_subdirs=True)
            loaded = True
            self._stage_box_state(snapshot["cfs"])
            self._restore_exclude(snapshot.get("exclude"))
            position = snapshot["file"]["position"]
            self.v_sd.file_position = self.v_sd.next_file_position = position
            self.session += 1
            self.session_id = snapshot["session_id"]
            self.session_file = path
            self.session_identity = copy.deepcopy(snapshot["file"]["identity"])
            self.max_print_z = float(snapshot["gcode"]["max_print_z"])
            self.candidates.clear()
            self.safe_candidate = self.completion = None
            self.last_candidate_time = self.last_submit_time = -1.0e30
            self.fresh_start = self.inhibited = False
            if self.proxy is not None:
                self.proxy.reset_failure()
            self._restore_machine(gcmd, snapshot)
            self._verify_boundary(snapshot, path)
            self.recovery_start = True
            self.v_sd.do_resume()
            self._close_recovery_prompt()
        except Exception as exc:
            _klog('recovery failed', level=logging.exception)
            self.recovery_start = False
            self._cleanup_failed_recovery(loaded)
            raise gcmd.error("PLR recovery failed: %s" % exc)
        finally:
            self.recovering = False

    def _validate_snapshot(self, snapshot):
        if self._tuning_active() or self._exclude_active():
            raise ValueError("unsupported move transform is active")
        if not isinstance(snapshot.get("session_id"), str):
            raise ValueError("invalid session identity")
        state = snapshot["file"]
        root = os.path.realpath(self.v_sd.sdcard_dirname)
        path = os.path.realpath(os.path.join(root, state["path"]))
        if os.path.commonpath((root, path)) != root or path == root:
            raise ValueError("snapshot path escapes Virtual SD")
        identity = self._file_identity(path)
        if identity != state["identity"]:
            raise ValueError("G-code file changed")
        if (not isinstance(state["position"], int) or state["position"] <= 0
                or state["position"] > identity["size"]):
            raise ValueError("invalid G-code byte position")
        gstate = snapshot["gcode"]
        values = (gstate["base_position"] + gstate["last_position"]
                  + gstate["homing_position"])
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in values):
            raise ValueError("invalid G-code coordinates")
        self.z_align.validate_reference_frame(snapshot["z_frame"])
        saved_z = float(gstate["last_position"][2])
        max_z = float(gstate["max_print_z"])
        if saved_z < -3.0 or max_z < saved_z or self._clearance(max_z) is None:
            raise ValueError("unsafe saved Z position")
        if float(snapshot["heaters"]["extruder"]) < 150.0:
            raise ValueError("saved nozzle was not printing")
        return path

    def _verify_boundary(self, snapshot, path):
        if self._file_identity(path) != snapshot["file"]["identity"]:
            raise ValueError("G-code changed during recovery")
        if self._exclude_active():
            raise ValueError("exclude-object became active")

    def _restore_machine(self, gcmd, snapshot):
        heaters, fans, state = (
            snapshot["heaters"], snapshot["fans"], snapshot["gcode"])
        saved = [float(value) for value in state["last_position"][:3]]
        self._clear_references()
        for command in (
            "M220 S100", "M107", "SET_FAN_SPEED FAN=aux_fans SPEED=0",
            "SET_TEMPERATURE_FAN_MANUAL_SPEED"
            " TEMPERATURE_FAN=chamber_exhaust_fans SPEED=0",
            "M140 S%.3f" % heaters["heater_bed"],
            "M141 S%.3f" % heaters["chamber_heater"],
            "M104 S%.3f" % min(heaters["extruder"], self.nozzle_standby),
        ):
            self._run(command)
        if not self.z_align.start_prepare():
            raise ValueError("Z alignment unexpectedly reported Z homed")
        prepared = self.z_align.wait_prepare_complete()
        self._run("G28 X Y")
        clearance = self._clearance(state["max_print_z"])
        if clearance > float(prepared.get("prepared_zmax", self.z_align.zmax)):
            raise ValueError("recovery clearance exceeds Z reference")
        self.z_align.perform_blocking_rise(
            target_z=clearance, rise_speed=self.recovery_z_speed,
            reference_frame=snapshot["z_frame"])

        self.gcode_move.absolute_coord = True
        self.gcode_move.absolute_extrude = False
        self.gcode_move.base_position[:3] = [0.0, 0.0, 0.0]
        self.gcode_move.homing_position[:3] = [0.0, 0.0, 0.0]
        self.gcode_move.reset_last_position()
        if heaters["heater_bed"] > 0.0:
            self._run("M190 S%.3f" % heaters["heater_bed"])
        if heaters["chamber_heater"] > 40.0:
            objects = self.printer.lookup_object("heaters")
            objects.set_temperature(
                objects.lookup_heater("chamber_heater"),
                heaters["chamber_heater"], wait=True)
        box_prepared = self._restore_box(
            gcmd, snapshot["cfs"], heaters["extruder"])
        self._restore_mesh(snapshot["mesh"])
        if box_prepared:
            self.printer.lookup_object(
                "box").change_engine.repush_after_filament_prepare()
        self._run("G1 X%.5f Y%.5f F%.3f" % (
            saved[0], saved[1], self.recovery_travel_speed * 60.0))
        self._run("G1 Z%.5f F%.3f" % (
            saved[2], self.recovery_z_speed * 60.0))
        self._restore_tuning(snapshot)
        self._restore_gcode_state(state)
        self._restore_fans(fans)

    def _restore_box(self, gcmd, token, temperature):
        box = self.printer.lookup_object("box", None)
        kind = token.get("kind")
        if kind == "none":
            if box is not None:
                raise ValueError("checkpoint has no CFS state")
            self._run("M109 S%.3f" % temperature)
            return False
        if box is None:
            raise ValueError("checkpoint requires CFS")
        if kind == "external":
            target = box.external_slot
        elif kind == "physical":
            target = token.get("slot")
            if type(target) is not int or not box.is_physical_slot(target):
                raise ValueError("invalid saved CFS slot")
        else:
            raise ValueError("invalid saved CFS state")

        if not box.filament_sensor_enabled():
            box.enable_filament_sensor()
        engine = box.change_engine
        live = box.read_live_state(include_topology=False)
        ready = (engine.pending is None and engine._target_ready(target, live)
                 and not box.hotend_feed_pending(target))
        prepared = False
        if not ready:
            if not engine.change(gcmd, target, flush=True):
                raise ValueError("CFS could not restore T%d" % target)
            prepared = engine.resume_prepared
        if not prepared:
            prepared = engine.prime_for_power_loss_recovery(
                gcmd, target, temperature)
        engine.wait_for_power_loss_recovery_temperature(temperature)
        return prepared

    def _clear_references(self):
        bed = self.printer.lookup_object("bed_mesh", None)
        if bed is not None:
            bed.set_mesh(None)
        if self.z_align.is_active():
            self.z_align.abort_internal("PLR recovery restart", motor_off=False)
        self.z_align.invalidate_homing_state()
        self.toolhead.get_kinematics().clear_homing_state("xyz")
        self.gcode_move.reset_last_position()

    def _restore_mesh(self, state):
        bed = self.printer.lookup_object("bed_mesh", None)
        if not state["active"]:
            if bed is not None:
                bed.set_mesh(None)
            return
        if bed is None:
            raise ValueError("checkpoint requires bed_mesh")
        from extras import bed_mesh as bed_mesh_module
        mesh = bed_mesh_module.ZMesh(copy.deepcopy(state["params"]), state["profile"])
        mesh.build_mesh(copy.deepcopy(state["matrix"]))
        mesh.set_mesh_offsets(list(state["offsets"]))
        bed.set_mesh(mesh)
        self._run("BED_MESH_OFFSET X=%.8f Y=%.8f ZFADE=%.8f" % (
            state["offsets"][0], state["offsets"][1], state["tool_offset"]))

    def _restore_tuning(self, snapshot):
        limit = snapshot["limits"]
        self._run(
            "SET_VELOCITY_LIMIT VELOCITY=%.8f ACCEL=%.8f"
            " MINIMUM_CRUISE_RATIO=%.8f SQUARE_CORNER_VELOCITY=%.8f"
            " Z_VELOCITY=%.8f Z_ACCEL=%.8f" % (
                limit["velocity"], limit["accel"], limit["cruise"],
                limit["corner"], limit["z_velocity"], limit["z_accel"]))
        pa = snapshot["pressure_advance"]
        self._run("SET_PRESSURE_ADVANCE ADVANCE=%.8f SMOOTH_TIME=%.8f" % (
            pa["advance"], pa["smooth"]))
        plane = int(snapshot["arc_plane"])
        if plane not in (0, 1, 2):
            raise ValueError("invalid arc plane")
        self._run(("G17", "G18", "G19")[plane])

    def _restore_gcode_state(self, state):
        name = "__POWER_LOSS_RECOVERY"
        self.gcode_move.saved_states[name] = {
            key: copy.deepcopy(state[key]) for key in (
                "absolute_coord", "absolute_extrude", "base_position",
                "last_position", "homing_position", "speed",
                "speed_factor", "extrude_factor")}
        try:
            self._run("RESTORE_GCODE_STATE NAME=%s MOVE=0" % name)
        finally:
            self.gcode_move.saved_states.pop(name, None)

    def _restore_fans(self, fans):
        for command in (
            "M106 S%d" % round(fans["part"] * 255.0),
            "SET_FAN_SPEED FAN=aux_fans SPEED=%.8f" % fans["aux"],
            "SET_TEMPERATURE_FAN_TARGET TEMPERATURE_FAN=chamber_exhaust_fans"
            " TARGET=%.8f MIN_SPEED=%.8f MAX_SPEED=%.8f" % (
                fans["chamber_target"], fans["chamber_min"], fans["chamber_max"]),
            "SET_TEMPERATURE_FAN_MANUAL_SPEED"
            " TEMPERATURE_FAN=chamber_exhaust_fans SPEED=%.8f" % (
                fans["chamber_manual"]),
        ):
            self._run(command)

    def _cleanup_failed_recovery(self, loaded):
        self.pending_box = None
        try:
            self._run("_RESET_PRINT_STATE")
        except Exception:
            _klog('cleanup failed', level=logging.exception)
        if loaded:
            try:
                self._clear_references()
            except Exception:
                _klog('reference cleanup failed', level=logging.exception)
            try:
                self.v_sd._reset_file()
            except Exception:
                _klog('file cleanup failed', level=logging.exception)

    def _run(self, command):
        self.gcode.run_script_from_command(command)


def load_config(config):
    return PowerLossRecovery(config)


__all__ = ["PowerLossRecovery", "load_config"]
