# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Tool-change, unload, runout, and purge sequencing for the CFS box.

Live hardware determines reversible progress, while the few irreversible
steps are checkpointed for safe same-command retries.
"""

import math
import re
from dataclasses import dataclass


FILAMENT_AREA = math.pi * (1.75 / 2.0) ** 2
DEFAULT_FALLBACK_PURGE_MM = 100.0
PURGE_CHUNK_MM = 100.0
GCODE_TAIL_BYTES = 50000
RETRACT_SPEED_F = 1500
EXTERNAL_RETRACT_F = 3000
EXTERNAL_PULL_MM = 125.0
EXTERNAL_FEED_MM = 30.0
EXTERNAL_FEED_F = 600
RUNOUT_RETRACT_MM = 3.0
EXTERNAL_WAIT = 30.0
SENSOR_POLL = 0.1
TEMP_TOLERANCE = 4.0
TEMP_POLL = 0.5
LONG_TEMP_WAIT = 5.0
SNAP_RETRACT_MM = 1.2
SERVICE_STATE = "box_change_recovery"
SERVICE_XY_F = 18000
SERVICE_Z_F = 600


@dataclass
class ChangeRequest:
    target: int
    source: object
    flush: bool
    print_context: bool
    resume: bool
    kind: str = "normal"
    retracted_source: object = None
    cut_source: object = None
    flush_done: bool = False
    prepared_filament: bool = False
    last_step: str = None
    last_error: str = None
    restore_target: object = None
    runout_recovery: bool = False
    return_position: object = None
    service_z: object = None
    rebase_pause: bool = False


@dataclass(frozen=True)
class TemperaturePlan:
    target: int
    minimum: int
    final: int


@dataclass(frozen=True)
class ResumeRecovery:
    target: object
    reason: str
    automatic: bool
    retry_command: object = None
    resume_temperature: object = None


class BoxChangeEngine:
    """One change executor composed by ``Box``.

    It checkpoints source retraction, source cutting, and destination flushing.
    Reissuing the same T command resumes this request. A different target
    replaces it while retaining any saved runout position until the replacement
    succeeds.
    """

    def __init__(self, box, config):
        self.box = box
        self.printer = box.printer
        self.gcode = box.gcode
        self.pause_resume = box.pause_resume
        self.retract_length = config.getfloat("retract_length", 30.0, minval=0.0)
        self.purge_speed = config.getfloat("purge_speed", 20.0, above=0.0)
        self.hotend_feed_speed = config.getfloat(
            "hotend_feed_speed", 15.0, above=0.0)
        self.hotend_feed_length = config.getfloat(
            "hotend_feed_length", 63.0, minval=0.0)
        self.print_prime_speed = config.getfloat(
            "print_prime_speed", 15.0, above=0.0)
        self.print_prime_length = config.getfloat(
            "print_prime_length", 20.0, minval=0.0)
        self.fallback_purge_length = config.getfloat(
            "fallback_purge_length", DEFAULT_FALLBACK_PURGE_MM, minval=0.0)
        self.default_temp = config.getint("default_temp", 220, minval=170, maxval=350)
        self.toolchange_z_hop = config.getfloat(
            "toolchange_z_hop", 2.0, minval=0.0)
        self.pending = None
        self.matrix = None
        self.temp_print = None
        self.temp_initial_layer = None
        self.parsed_epoch = None
        self.prepared_epoch = None
        self.last_purge_length = 0.0
        self.resume_recovery = None
        self.resume_prepared = False

    # ------------------------------------------------------------------
    # Public entry points owned and registered by Box
    # ------------------------------------------------------------------

    def prime_for_power_loss_recovery(self, gcmd, target, temperature):
        final = int(round(float(temperature)))
        if not self.box.is_valid_slot(target) or not 170 <= final <= 350:
            raise RuntimeError("Invalid power-loss prime target")
        generation = self.box.fault_generation
        self._check_abort(generation, None)
        live = self.box.read_live_state()
        if (not self._target_ready(target, live)
                or self.box.hotend_feed_pending(target)):
            raise RuntimeError("Power-loss filament path is not ready")
        plan = TemperaturePlan(final + 5, final, final)
        self.box.move_to_wastebin()
        self._start_heat(plan)
        return self._prepare_filament(
            gcmd, target, target, allow_purge=False,
            prime_reason="power-loss recovery", prepared_plan=plan,
            fault_generation=generation, force_prime=True)

    def repush_after_filament_prepare(self):
        if self.last_purge_length or self.print_prime_length:
            self._relative_extrude("box_prepare_repush", SNAP_RETRACT_MM, RETRACT_SPEED_F)

    def change(self, gcmd, target, flush=True, within_resume=False):
        if not self.box.is_valid_slot(target):
            raise gcmd.error(
                "[BOX]: T%d is not an online CFS slot or external T%d"
                % (target, self.box.external_slot))
        started = self.printer.get_reactor().monotonic()
        fault_generation = self.box.fault_generation

        inherited_recovery = (
            self.pending
            if (self.pending and self.pending.target != target
                and (self.pending.runout_recovery
                     or self.pending.return_position is not None
                     or self.pending.service_z is not None))
            else None)

        try:
            live = self.box.read_live_state()
        except Exception as exc:
            if self.pending and self.pending.target == target:
                return self._operation_failed(
                    gcmd, self.pending, exc, fault_generation,
                    raise_error=within_resume)
            raise gcmd.error("[BOX]: Unable to read CFS state: %s" % exc)
        if live.loaded_slot is None:
            raise gcmd.error("[BOX]: CFS loaded-slot state is unavailable")
        if live.loaded_slot != target:
            if self.box.is_valid_slot(live.loaded_slot):
                self._info(
                    gcmd, "Changing T%d -> T%d" % (live.loaded_slot, target))
            else:
                self._info(gcmd, "Changing to T%d" % target)

        request = self.pending if self.pending and self.pending.target == target else None
        fresh_change = request is None and inherited_recovery is None
        if request is None:
            print_context = self._is_print_file_command() or self._is_print_active()
            if inherited_recovery:
                source = inherited_recovery.source
            elif self.box.is_valid_slot(live.loaded_slot):
                source = live.loaded_slot
            else:
                source = self.box.last_loaded_slot
            request = ChangeRequest(
                target=target,
                source=source,
                flush=bool(flush),
                print_context=(inherited_recovery.print_context
                               if inherited_recovery else print_context),
                resume=(inherited_recovery.resume if inherited_recovery else
                        self._is_print_active() and not self._is_print_paused()),
                retracted_source=(inherited_recovery.retracted_source
                                  if inherited_recovery else None),
                cut_source=(inherited_recovery.cut_source
                            if inherited_recovery else None),
                restore_target=(inherited_recovery.restore_target
                                if inherited_recovery else None),
                runout_recovery=(inherited_recovery.runout_recovery
                                 if inherited_recovery else False),
                return_position=(inherited_recovery.return_position
                                 if inherited_recovery else None),
                service_z=(inherited_recovery.service_z
                           if inherited_recovery else None),
                rebase_pause=(inherited_recovery.rebase_pause
                              if inherited_recovery else False),
            )
        self.pending = request
        request.last_error = None

        noop_reported = False
        if live.loaded_slot == target and request.kind == "runout":
            self._info(gcmd, "Preparing T%d" % target)

        try:
            self._check_abort(fault_generation, request)
            if (target == self.box.external_slot and live.loaded_slot != target
                    and self._is_print_active() and not self._is_print_paused()):
                self._capture_service_origin(request)
                request.last_step = "attendance"
                request.last_error = "External T%d requires attended loading" % target
                self.block_resume(request.last_error)
                self._warn(request.last_error +
                           "; insert filament, then run RESUME to continue")
                self.box.pause_print()
                return False
            if request.kind == "runout":
                if request.runout_recovery:
                    if request.last_step != "restore":
                        self._move_to_wastebin(request)
                        self._execute_runout(
                            gcmd, request, live, fault_generation)
                        request.last_step = "restore"
                    self._finish_runout(request)
                else:
                    self._restore_runout_target(request)
            else:
                noop_reported = self._execute(
                    gcmd, request, live, fault_generation, fresh_change)
                if request.runout_recovery:
                    request.last_step = "restore"
                    self._finish_runout(request)
                elif self._return_from_service(request):
                    if request.rebase_pause and self._is_print_paused():
                        self.gcode.run_script_from_command(
                            "SAVE_GCODE_STATE NAME=PAUSE_STATE")
            self._check_abort(fault_generation, request)
        except Exception as exc:
            return self._operation_failed(
                gcmd, request, exc, fault_generation,
                raise_error=within_resume)

        self.pending = None
        self.resume_prepared = bool(request.prepared_filament)
        self.clear_resume_recovery()
        if request.resume and not within_resume:
            self.gcode.run_script_from_command("RESUME")
        elif not request.print_context and request.flush:
            self.gcode.run_script_from_command("M104 S0")
        if not noop_reported:
            self._info(gcmd, "T%d active in %.1fs" % (
                target, self.printer.get_reactor().monotonic() - started))
        return True

    def unload(self, gcmd, manual=False):
        """Complete user-facing unload for physical or external filament."""
        fault_generation = self.box.fault_generation
        try:
            live = self.box.read_live_state()
        except Exception as exc:
            raise gcmd.error("[BOX]: BOX_UNLOAD failed: %s" % exc)
        if live.loaded_slot is None:
            raise gcmd.error("[BOX]: CFS loaded-slot state is unavailable")
        if live.loaded_slot < 0 and live.filament_detected is False:
            self.box.enable_filament_sensor()
            self.box.clear_hotend_feed_pending()
            self._clear_active_spool(self.box.last_loaded_slot)
            self._info(gcmd, "No filament is loaded")
            return

        physical = self.box.is_physical_slot(live.loaded_slot)
        if manual and not physical:
            raise gcmd.error(
                "[BOX]: BOX_UNLOAD MANUAL=1 requires a physical CFS slot")
        previous_sensor = self.box.filament_sensor_enabled()
        success = False
        heater_used = False
        self._remember_hotend_filament(live.loaded_slot)
        if physical:
            self.box.disable_filament_sensor()
        try:
            self._check_abort(fault_generation)
            if physical:
                source = live.loaded_slot
                if manual:
                    self.box.physical_unload(
                        allow_extruder_retract=True,
                        fault_generation=fault_generation)
                elif live.filament_detected is False:
                    self.box.physical_unload(
                        allow_extruder_retract=False,
                        fault_generation=fault_generation)
                else:
                    heater_used = True
                    plan = self._temperature_plan(source, None)
                    can_cut = self._cutter_ready()
                    self._start_heat_home_and_wait(
                        gcmd, plan, fault_generation)
                    if can_cut:
                        self.box.retract_for_cut(
                            self.retract_length,
                            fault_generation=fault_generation)
                        self._check_abort(fault_generation)
                        self.box.cut_filament(
                            force=not bool(live.filament_detected))
                        self._check_abort(fault_generation)
                    self.box.move_to_wastebin()
                    self._check_abort(fault_generation)
                    self.box.physical_unload(
                        allow_extruder_retract=True,
                        fault_generation=fault_generation)
                self._clear_active_spool(source)
            else:
                # CFS empty plus a detected sensor is the external path.
                heater_used = True
                self._unload_external(
                    gcmd, None, fault_generation)

            self._check_abort(fault_generation)
            if heater_used:
                self.gcode.run_script_from_command("M104 S0")
            success = True
            self._info(
                gcmd, "Unload complete%s"
                % (", heater off" if heater_used else ""))
        except Exception as exc:
            raise gcmd.error("[BOX]: BOX_UNLOAD failed: %s" % exc)
        finally:
            if physical:
                if success or previous_sensor:
                    self.box.enable_filament_sensor()
                else:
                    self.box.disable_filament_sensor()

    def runout(self, gcmd):
        fault_generation = self.box.fault_generation
        self._info(gcmd, "Filament sensor: runout detected")
        external = self.box.last_loaded_slot == self.box.external_slot
        recovery = None
        if self.box.runout_swap_enabled:
            try:
                recovery = self.box.runout_recovery()
            except Exception as exc:
                return self._pause_runout(
                    gcmd, "unable to read CFS state: %s" % exc,
                    feed_tail=False)
        if external and (
                recovery is None
                or (not recovery.get("recoverable")
                    and not self.box.runout_active)):
            self._remember_hotend_filament(self.box.external_slot)
            self._clear_active_spool(self.box.external_slot)
            return self._pause_runout(gcmd, "external spool empty; automatic swap is unavailable")
        if recovery is None:
            return self._pause_runout(gcmd, "automatic runout swap disabled")
        if not recovery.get("recoverable"):
            return self._pause_runout(gcmd, recovery.get("reason", "no replacement"))

        source = recovery["loaded_slot"]
        target = recovery["target_slot"]
        resume_after_failure = (
            self._is_print_active() and not self._is_print_paused())
        request = ChangeRequest(
            target=target,
            source=source,
            flush=True,
            print_context=True,
            resume=False,
            kind="runout",
            retracted_source=source,
            cut_source=source,
            restore_target=self._extruder_target(),
        )
        self._info(gcmd, "Auto runout swap: T%d -> T%d" % (source, target))
        request.runout_recovery = True
        self.pending = request
        wiped = False
        try:
            self._capture_service_origin(request)
            wiped = self._runout_retract_wipe()
            self._move_to_wastebin(request)
            self._check_abort(fault_generation, request)
            self._feed_runout_tail(
                gcmd, EXTERNAL_FEED_MM + (RUNOUT_RETRACT_MM if wiped else 0.0))
            self._check_abort(fault_generation, request)
            self._execute_runout(
                gcmd, request, self.box.read_live_state(), fault_generation)
            request.last_step = "restore"
            self._finish_runout(request)
            self._check_abort(fault_generation, request)
        except Exception as exc:
            if not self._abort_active(fault_generation):
                request.resume = resume_after_failure
            else:
                request.resume = False
            self._restore_runout_target(request)
            return self._pause_runout(
                gcmd, str(exc), feed_tail=False,
                skip_retract_wipe=wiped)

        self.pending = None
        self.clear_resume_recovery()
        self._info(gcmd, "Auto runout swap complete: T%d active" % target)
        return True

    def parse_flush_volumes(self, gcmd):
        self._clear_parsed_data()
        sd = self.printer.lookup_object("virtual_sdcard")
        path = sd.get_status(self.printer.get_reactor().monotonic()).get("file_path")
        if not path:
            raise gcmd.error("[BOX]: No file currently loaded")
        try:
            with open(path, "rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - GCODE_TAIL_BYTES))
                tail = stream.read().decode("utf-8", errors="ignore")
        except OSError as exc:
            raise gcmd.error("[BOX]: Failed to read gcode file: %s" % exc)

        match = re.search(r";\s*flush_volumes_matrix\s*=\s*([0-9.,]+)", tail)
        if not match:
            self._info(gcmd, "flush_volumes_matrix not found in gcode")
            return
        values = [float(value) for value in match.group(1).split(",") if value.strip()]
        size = int(math.sqrt(len(values)))
        if not size or size * size != len(values):
            raise gcmd.error(
                "[BOX]: flush_volumes_matrix size %d is not a perfect square"
                % len(values))
        self.matrix = [values[i * size:(i + 1) * size] for i in range(size)]
        self.temp_print = self._parse_temp_array(tail, "nozzle_temperature", size)
        self.temp_initial_layer = self._parse_temp_array(
            tail, "nozzle_temperature_initial_layer", size)
        self.parsed_epoch = self._print_epoch()
        self._info(gcmd, "Flush volumes: parsed %dx%d matrix" % (size, size))

    def debug_status(self):
        request = self.pending
        return {
            "pending": None if request is None else {
                "target": request.target,
                "source": request.source,
                "kind": request.kind,
                "flush": request.flush,
                "retracted_source": request.retracted_source,
                "cut_source": request.cut_source,
                "flush_done": request.flush_done,
                "prepared_filament": request.prepared_filament,
                "resume": request.resume,
                "restore_target": request.restore_target,
                "runout_recovery": request.runout_recovery,
                "last_step": request.last_step,
                "last_error": request.last_error,
            },
            "parsed_flush_current": self._parsed_is_current(),
            "last_purge_length": self.last_purge_length,
            "prepared_epoch": self.prepared_epoch,
            "resume_recovery": self.recovery_status(),
        }

    def block_resume(
            self, reason, target=None, automatic=None, retry_command=None):
        request = self.pending
        if request is not None and request.print_context:
            target = request.target
            if automatic is None:
                automatic = True
            resume_temperature = self._request_resume_temperature(request)
        else:
            automatic = bool(automatic)
            resume_temperature = None
        if retry_command is None and automatic:
            retry_command = "RESUME"
        self.resume_prepared = False
        self.resume_recovery = ResumeRecovery(
            target=target,
            reason=str(reason),
            automatic=bool(automatic),
            retry_command=retry_command,
            resume_temperature=resume_temperature,
        )

    def clear_resume_recovery(self):
        self.resume_recovery = None

    def reset_print_recovery(self, *args):
        self.pending = None
        self.resume_prepared = False
        self.clear_resume_recovery()

    def recovery_status(self):
        recovery = self.resume_recovery
        request = self.pending
        if recovery is None:
            return {
                "blocked": False,
                "automatic": False,
                "target": None,
                "step": None,
                "reason": None,
                "retry_command": None,
                "resume_prepared": self.resume_prepared,
                "resume_temperature": None,
            }
        request_matches = (
            request is not None and request.target == recovery.target)
        return {
            "blocked": True,
            "automatic": recovery.automatic,
            "target": recovery.target,
            "step": request.last_step if request_matches else None,
            "reason": recovery.reason,
            "retry_command": recovery.retry_command,
            "resume_prepared": self.resume_prepared,
            "resume_temperature": recovery.resume_temperature,
        }

    def resume_check(self, gcmd, retry=False):
        recovery = self.resume_recovery
        if recovery is None:
            return True
        if not retry or not recovery.automatic or recovery.target is None:
            raise gcmd.error("[BOX]: " + self.recovery_instruction())
        if not self._is_print_paused():
            raise gcmd.error("[BOX]: Box recovery requires a paused print")

        self.resume_prepared = False
        request = self.pending
        if request is None and recovery.target == self.box.external_slot:
            request = ChangeRequest(
                target=recovery.target,
                source=self.box.last_loaded_slot,
                flush=True,
                print_context=True,
                resume=False,
                last_step="attendance",
            )
            self.pending = request
        flush = (
            request.flush
            if request is not None and request.target == recovery.target
            else True)
        self._info(
            gcmd, "RESUME retrying T%d after: %s"
            % (recovery.target, self.recovery_status()["reason"]))
        try:
            if not self.change(
                    gcmd, recovery.target, flush=flush, within_resume=True):
                raise gcmd.error("[BOX]: " + self.recovery_instruction())
        except Exception:
            self.gcode.run_script_from_command("M104 S140")
            raise
        self._info(gcmd, "T%d recovery complete; continuing RESUME"
                   % recovery.target)
        return True

    def recovery_instruction(self):
        status = self.recovery_status()
        reason = status["reason"] or "Box recovery is incomplete"
        action = self._recovery_action(status)
        return "Resume blocked: %s; %s, or CANCEL_PRINT" % (reason, action)

    def recovery_notice(self):
        status = self.recovery_status()
        reason = status["reason"] or "Box recovery is incomplete"
        pause_state = (
            "print remains paused" if self._is_print_paused()
            else "pausing print")
        return "%s; %s. Please %s, or CANCEL_PRINT" % (
            reason, pause_state, self._recovery_action(status))

    @staticmethod
    def _recovery_action(status):
        target = status["target"]
        if status["automatic"] and target is not None:
            return "fix the issue and run RESUME to retry T%d" % target
        elif status["retry_command"]:
            return "fix the issue, run %s, then RESUME" % status["retry_command"]
        return "resolve the filament path and select a T command before RESUME"

    def _request_resume_temperature(self, request):
        if request.kind == "runout" and request.restore_target:
            return int(request.restore_target)
        if request.flush:
            return int(self._effective_temp(request.target))
        return None

    # ------------------------------------------------------------------
    # Unified execution
    # ------------------------------------------------------------------

    def _execute(self, gcmd, request, live, fault_generation, report_noop):
        self._check_abort(fault_generation, request)
        target = request.target
        source = request.source
        prepared_plan = None
        ready = self._target_ready(target, live)
        external_capture = (
            target == self.box.external_slot
            and ready
            and request.last_step in ("attendance", "load")
            and not self.box.hotend_feed_pending(target))
        needs_preparation = request.flush and (
            (request.runout_recovery and not request.flush_done)
            or self.box.hotend_feed_pending(target)
            or self._print_needs_prime()
            or self._same_slot_material_changed(source, target))
        retrying_finish = request.last_step in ("flush", "final_temperature")
        already_prepared = (ready and not external_capture
                            and not needs_preparation and not retrying_finish)
        report_noop = report_noop and already_prepared
        if live.loaded_slot == target:
            message = (
                "T%d already loaded and primed"
                if report_noop
                else "Preparing T%d")
            self._info(gcmd, message % target)
        if already_prepared:
            self._check_abort(fault_generation, request)
            self.box.activate_tracking(target) if self.box.is_physical_slot(target) else None
            self._commit_loaded_slot(target)
            return report_noop

        self._capture_service_origin(request)

        if live.loaded_slot != target:
            if self.box.is_physical_slot(live.loaded_slot):
                source = live.loaded_slot
                request.source = source
                self._unload_physical(
                    gcmd, request, live, fault_generation)
            elif live.loaded_slot == self.box.external_slot:
                source = self.box.external_slot
                request.source = source
                self._unload_external(
                    gcmd, request, fault_generation)

            live = self.box.read_live_state()
            self._check_abort(fault_generation, request)
            if live.loaded_slot not in (-1, target):
                raise RuntimeError("Unexpected loaded slot T%s after unload" % live.loaded_slot)

        if not self._target_ready(target, live) or external_capture:
            request.last_step = "load"
            if self.box.is_physical_slot(target):
                if request.flush:
                    prepared_plan = self._prepare_hotend(
                        source, target, fault_generation, request)
                self.box.physical_load(
                    target, fault_generation=fault_generation)
            else:
                prepared_plan = self._load_external(
                    gcmd, source, fault_generation, request)
            live = self.box.read_live_state()
            self._check_abort(fault_generation, request)
            if not self._target_ready(target, live):
                raise RuntimeError("T%d load completed without verified final state" % target)

        self._check_abort(fault_generation, request)
        self._commit_loaded_slot(target)
        if request.flush and not request.flush_done:
            request.last_step = "flush"
            prepared = self._prepare_filament(
                gcmd, source, target, prepared_plan=prepared_plan,
                fault_generation=fault_generation, request=request)
            request.prepared_filament = bool(
                request.prepared_filament or prepared)
            request.flush_done = True
            if request.print_context:
                self.prepared_epoch = self._print_epoch()
                request.last_step = "final_temperature"
                self._wait_for_final_temperature(
                    target, fault_generation, request)
        elif (request.flush and request.flush_done and request.print_context
              and request.last_step == "final_temperature"):
            self._wait_for_final_temperature(
                target, fault_generation, request)
        return False

    def _execute_runout(self, gcmd, request, live, fault_generation):
        self._check_abort(fault_generation, request)
        source = request.source
        target = request.target
        prepared_plan = None
        if live.loaded_slot is None:
            raise RuntimeError("CFS loaded-slot state is unavailable")
        if live.loaded_slot != target:
            if self.box.is_physical_slot(live.loaded_slot):
                request.last_step = "unload"
                self.box.physical_unload(
                    allow_extruder_retract=False,
                    fault_generation=fault_generation)
                live = self.box.read_live_state()
                self._check_abort(fault_generation, request)
            elif live.loaded_slot == self.box.external_slot:
                raise RuntimeError("External filament is loaded during runout recovery")
            if live.loaded_slot not in (-1, target):
                raise RuntimeError(
                    "Unexpected loaded slot T%s during runout recovery"
                    % live.loaded_slot)

        if not self._target_ready(target, live):
            request.last_step = "load"
            prepared_plan = self._prepare_hotend(
                source, target, fault_generation, request)
            self.box.physical_load(
                target, fault_generation=fault_generation)
            live = self.box.read_live_state()
            self._check_abort(fault_generation, request)
            if not self._target_ready(target, live):
                raise RuntimeError(
                    "T%d load completed without verified final state" % target)

        self._check_abort(fault_generation, request)
        self._commit_loaded_slot(target)
        if not request.flush_done:
            request.last_step = "flush"
            prepared = self._prepare_filament(
                gcmd, target, target, temperature_source=source,
                allow_purge=False, prime_reason="runout replacement",
                prepared_plan=prepared_plan,
                fault_generation=fault_generation, request=request)
            request.prepared_filament = bool(
                request.prepared_filament or prepared)
            request.flush_done = True
            self.prepared_epoch = self._print_epoch()

    def _finish_runout(self, request):
        self._restore_runout_target(request)
        if request.kind == "runout" and request.prepared_filament:
            self.repush_after_filament_prepare()
        self._return_from_service(request)
        if request.rebase_pause and self._is_print_paused():
            self.gcode.run_script_from_command(
                "SAVE_GCODE_STATE NAME=PAUSE_STATE")
        request.runout_recovery = False

    def _restore_runout_target(self, request):
        if not request.restore_target or request.restore_target <= 0:
            return
        try:
            self.gcode.run_script_from_command(
                "M104 S%d" % request.restore_target)
        except Exception:
            self._warn("Unable to restore the pre-runout heater target")

    def _unload_physical(self, gcmd, request, live, fault_generation):
        self._check_abort(fault_generation, request)
        source = live.loaded_slot
        self._remember_hotend_filament(source)
        sensor_clear = live.filament_detected is False
        source_prepared = (
            request.retracted_source == source and request.cut_source == source)
        can_cut = not sensor_clear and self._cutter_ready()
        previous_sensor = self.box.filament_sensor_enabled()
        self.box.disable_filament_sensor()
        try:
            if not sensor_clear and request.retracted_source != source:
                request.last_step = "retract"
                self._start_heat_home_and_wait(
                    gcmd, self._temperature_plan(source, None),
                    fault_generation, request)
                self.box.retract_for_cut(
                    self.retract_length,
                    fault_generation=fault_generation)
                request.retracted_source = source
            if not sensor_clear and request.cut_source != source:
                if can_cut:
                    request.last_step = "cut"
                    self._check_abort(fault_generation, request)
                    self._cut_filament(
                        request, force=not bool(live.filament_detected))
                request.cut_source = source
            if not sensor_clear:
                self._check_abort(fault_generation, request)
                self._move_to_wastebin(request)
            request.last_step = "unload"
            self._check_abort(fault_generation, request)
            self.box.physical_unload(
                allow_extruder_retract=not sensor_clear and not source_prepared,
                fault_generation=fault_generation)
            self._clear_active_spool(source)
        except Exception:
            if previous_sensor:
                self.box.enable_filament_sensor()
            else:
                self.box.disable_filament_sensor()
            raise

    def _unload_external(self, gcmd, request, fault_generation):
        self._check_abort(fault_generation, request)
        source = self.box.external_slot
        self._remember_hotend_filament(source)
        can_cut = self._cutter_ready()
        previous_sensor = self.box.filament_sensor_enabled()
        success = False
        self.box.disable_filament_sensor()
        try:
            if request is None or request.retracted_source != source:
                self._start_heat_home_and_wait(
                    gcmd, self._temperature_plan(source, None),
                    fault_generation, request)
                self._relative_extrude(
                    "box_external_unload", -self.retract_length,
                    EXTERNAL_RETRACT_F)
                if request:
                    request.retracted_source = source
            if request is None or request.cut_source != source:
                if can_cut:
                    self._check_abort(fault_generation, request)
                    self._cut_filament(request, force=True)
                if request:
                    request.cut_source = source
            self._check_abort(fault_generation, request)
            self._move_to_wastebin(request)
            self._check_abort(fault_generation, request)
            self._relative_extrude(
                "box_external_pull", -EXTERNAL_PULL_MM, EXTERNAL_RETRACT_F)
            self.gcode.run_script_from_command(
                "SET_STEPPER_ENABLE STEPPER=extruder ENABLE=0")
            try:
                self._wait_for_sensor(
                    gcmd, False, timeout=None,
                    fault_generation=fault_generation, request=request)
            finally:
                self.gcode.run_script_from_command(
                    "SET_STEPPER_ENABLE STEPPER=extruder ENABLE=1")
            self.box.clear_hotend_feed_pending(source)
            self._clear_active_spool(source)
            success = True
        finally:
            if success or previous_sensor:
                self.box.enable_filament_sensor()
            else:
                self.box.disable_filament_sensor()

    def _load_external(self, gcmd, source, fault_generation, request):
        plan = self._temperature_plan(source, self.box.external_slot)
        self._start_heat_home_and_wait(
            gcmd, plan, fault_generation, request)
        self._check_abort(fault_generation, request)
        self._move_to_wastebin(request)
        self.box.enable_filament_sensor()
        self._wait_for_sensor(
            gcmd, True, timeout=EXTERNAL_WAIT,
            fault_generation=fault_generation, request=request)
        self._check_abort(fault_generation, request)
        self._relative_extrude("box_external_feed", EXTERNAL_FEED_MM, EXTERNAL_FEED_F)
        self.box.mark_hotend_feed_pending(self.box.external_slot)
        return plan

    # ------------------------------------------------------------------
    # Flush and temperature policy
    # ------------------------------------------------------------------

    def _temperature_plan(self, source, target):
        source_temp = self._effective_temp(source) if self.box.is_valid_slot(source) else None
        hotend = self.box.hotend_filament()
        if hotend and hotend["temperature"] is not None:
            source_temp = max(source_temp or 0, hotend["temperature"])
        target_temp = self._effective_temp(target) if self.box.is_valid_slot(target) else None
        if target_temp is None and source_temp is None:
            target_temp = source_temp = self.default_temp
        final = target_temp if target_temp is not None else source_temp
        minimum = max(value for value in (source_temp, target_temp) if value is not None)
        return TemperaturePlan(minimum + 5, minimum, final)

    def _start_heat_home_and_wait(
            self, gcmd, plan, fault_generation, request=None):
        self._start_heat(plan)
        self._enter_service(request)
        self.gcode.run_script_from_command("HOME_IF_NEEDED AXIS=XY")
        self._check_abort(fault_generation, request)
        self._wait_for_heat(gcmd, plan, fault_generation, request)

    def _start_heat(self, plan):
        self.gcode.run_script_from_command("M104 S%d" % plan.target)

    def _wait_for_heat(self, gcmd, plan, fault_generation, request=None):
        heater = self.printer.lookup_object("heaters").lookup_heater("extruder")
        current, _target = heater.get_temp(self.printer.get_reactor().monotonic())
        if current < plan.minimum:
            self._info(gcmd, "Waiting for extruder >=%dC" % plan.minimum)
        self._wait_for_temperature(
            plan.minimum, None, fault_generation, request)

    def _prepare_hotend(
            self, source, target, fault_generation, request,
            temperature_source=None):
        plan = self._temperature_plan(
            source if temperature_source is None else temperature_source,
            target)
        self._start_heat(plan)
        self._check_abort(fault_generation, request)
        self._move_to_wastebin(request)
        self._check_abort(fault_generation, request)
        return plan

    def _prepare_filament(
            self, gcmd, source, target, temperature_source=None,
            allow_purge=True, prime_reason=None, prepared_plan=None,
            fault_generation=None, request=None, force_prime=False):
        if fault_generation is None:
            fault_generation = self.box.fault_generation
        self._check_abort(fault_generation, request)
        feed = (
            self.hotend_feed_length
            if self.box.hotend_feed_pending(target) else 0.0)
        purge, purge_reason = self._purge_plan(source, target, allow_purge)
        prime = 0.0
        print_start = self._print_needs_prime()
        if purge <= 0.0 and (
                feed > 0.0 or force_prime or print_start):
            prime = self.print_prime_length
            if prime_reason is None and print_start:
                prime_reason = "print start"

        if feed <= 0.0 and purge <= 0.0 and prime <= 0.0:
            return False
        plan = prepared_plan
        if plan is None:
            plan = self._temperature_plan(
                source if temperature_source is None else temperature_source,
                target)
            self._move_to_wastebin(request)
            self._check_abort(fault_generation, request)
            self._start_heat(plan)
        self._wait_for_heat(gcmd, plan, fault_generation, request)
        if plan.final != plan.target:
            # Preserve the accepted policy: restore destination target before extrusion.
            self.gcode.run_script_from_command("M104 S%d" % plan.final)

        self.last_purge_length = purge
        feed_f = self.hotend_feed_speed / FILAMENT_AREA * 60.0
        purge_f = self.purge_speed / FILAMENT_AREA * 60.0
        prime_f = self.print_prime_speed / FILAMENT_AREA * 60.0
        guard = self.box.part_fan_override(0.0)
        with guard:
            self.gcode.run_script_from_command(
                "SAVE_GCODE_STATE NAME=box_filament_prepare")
            try:
                self.gcode.run_script_from_command("M83")
                clog_epoch = self.box.clog_event_count
                if feed > 0.0:
                    self._info(
                        gcmd,
                        "Feeding %.1fmm from printhead gears to hotend" % feed)
                    self._check_abort(
                        fault_generation, request, clog_epoch)
                    self.gcode.run_script_from_command(
                        "G1 E%.4f F%.1f" % (feed, feed_f))
                    self.printer.lookup_object("toolhead").wait_moves()
                    self._check_abort(
                        fault_generation, request, clog_epoch)
                if purge > 0.0:
                    self._info(
                        gcmd, "Purging %.1fmm (%s)" % (purge, purge_reason))
                    self._purge_moves(
                        purge, purge_f, clog_epoch,
                        fault_generation, request)
                elif prime > 0.0:
                    self._info(
                        gcmd, "Priming %.1fmm%s" % (
                            prime, " (%s)" % prime_reason if prime_reason else ""))
                    self._purge_moves(
                        prime, prime_f, clog_epoch,
                        fault_generation, request)
            finally:
                self.gcode.run_script_from_command(
                    "RESTORE_GCODE_STATE NAME=box_filament_prepare MOVE=0")
        self.box.set_hotend_filament(target, self._effective_temp(target))
        return True

    def _purge_plan(self, source, target, allow_purge):
        if not allow_purge:
            return 0.0, None
        if self._same_slot_material_changed(source, target):
            return self.fallback_purge_length, "same-slot material change T%d" % target
        if source == target:
            return 0.0, None
        if not self.box.is_valid_slot(source):
            return self.fallback_purge_length, "fallback unknown -> T%d" % target
        volume = self._matrix_volume(source, target)
        if volume is not None:
            return max(0.0, volume / FILAMENT_AREA), (
                "slicer matrix T%d -> T%d" % (source, target))
        return self.fallback_purge_length, (
            "fallback T%d -> T%d" % (source, target))

    def _purge_moves(
            self, distance, feedrate, clog_epoch,
            fault_generation, request):
        chunks = int(math.ceil(distance / PURGE_CHUNK_MM))
        for index in range(chunks):
            self._check_abort(fault_generation, request, clog_epoch)
            amount = distance / chunks
            if index < chunks - 1:
                amount += SNAP_RETRACT_MM
            self.gcode.run_script_from_command(
                "G1 E%.4f F%.1f" % (amount, feedrate))
            self._check_abort(fault_generation, request, clog_epoch)
            self.box.flush_clean_snap(fan_after=0.0)
            self._check_abort(fault_generation, request, clog_epoch)
        self._check_abort(fault_generation, request, clog_epoch)

    def _wait_for_temperature(
            self, minimum, maximum, fault_generation, request):
        heater = self.printer.lookup_object("heaters").lookup_heater("extruder")

        def waiting(eventtime):
            self._check_abort(fault_generation, request)
            current, _target = heater.get_temp(eventtime)
            return current < minimum or (
                maximum is not None and current > maximum)

        self.printer.wait_while(waiting, interval=TEMP_POLL)
        self._check_abort(fault_generation, request)

    def _wait_for_final_temperature(self, target, fault_generation, request):
        final = self._effective_temp(target)
        reactor = self.printer.get_reactor()
        started = reactor.monotonic()
        self.gcode.run_script_from_command("M104 S%d" % final)
        self._wait_for_temperature(
            final - TEMP_TOLERANCE, final + TEMP_TOLERANCE,
            fault_generation, request)
        if reactor.monotonic() - started > LONG_TEMP_WAIT:
            self._check_abort(fault_generation, request)
            self._enter_service(request)
            self.box.nozzle_clean()
        self._check_abort(fault_generation, request)
        self.repush_after_filament_prepare()
        self._check_abort(fault_generation, request)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _target_ready(self, target, live):
        if target == self.box.external_slot:
            return live.loaded_slot == target and bool(live.filament_detected)
        return (live.loaded_slot == target and bool(live.filament_detected)
                and bool(live.tracking))

    def _commit_loaded_slot(self, slot):
        if slot == self.box.external_slot:
            self.box.note_external_source()
        self.box.last_loaded_slot = slot
        self.box.activate_spool(slot)

    def _remember_hotend_filament(self, slot):
        if (self.box.is_valid_slot(slot) and self.box.hotend_filament() is None
                and not self.box.hotend_feed_pending(slot)):
            self.box.set_hotend_filament(slot, self._effective_temp(slot))

    def _effective_temp(self, slot):
        if not self.box.is_valid_slot(slot):
            return self.default_temp
        if self._parsed_is_current():
            values = self.temp_initial_layer if self._is_first_layer() else self.temp_print
            if values and slot < len(values):
                return int(values[slot])
        profile_temp = self.box.slot_target_temp(slot)
        return int(profile_temp if profile_temp is not None else self.default_temp)

    def _same_slot_material_changed(self, source, target):
        if source != target or not self.box.hotend_feed_pending(target):
            return False
        hotend = self.box.hotend_filament()
        if hotend is None:
            return True
        previous = hotend["material"], hotend["color"]
        current = self.box.filament_identity(target)
        if not all(previous) or not all(current):
            return True
        return previous != current

    def _matrix_volume(self, source, target):
        if not self._parsed_is_current() or not self.box.is_valid_slot(source):
            return None
        if source < 0 or target < 0 or source >= len(self.matrix):
            return None
        row = self.matrix[source]
        if target >= len(row):
            return None
        return 0.0 if source == target else row[target]

    def _print_needs_prime(self):
        epoch = self._print_epoch()
        return epoch is not None and self.prepared_epoch != epoch

    def _parsed_is_current(self):
        epoch = self._print_epoch()
        return epoch is not None and self.parsed_epoch == epoch and self.matrix is not None

    def _clear_parsed_data(self):
        self.matrix = None
        self.temp_print = None
        self.temp_initial_layer = None
        self.parsed_epoch = None

    def _parse_temp_array(self, text, key, expected):
        match = re.search(r";\s*" + re.escape(key) + r"\s*=\s*([0-9,]+)", text)
        if not match:
            return None
        values = [int(value) for value in match.group(1).split(",") if value.strip()]
        return values[:expected] if len(values) >= expected else None

    def _print_epoch(self):
        return self.printer.lookup_object("print_stats").print_start_time

    def _is_first_layer(self):
        layer = self.printer.lookup_object("print_stats").info_current_layer
        return layer is None or layer <= 1

    def _is_print_file_command(self):
        return self.printer.lookup_object("virtual_sdcard").is_cmd_from_sd()

    def _is_print_active(self):
        if self.printer.lookup_object("print_stats").state in ("printing", "paused"):
            return True
        return self.printer.lookup_object("virtual_sdcard").is_active()

    def _is_print_paused(self):
        if self.pause_resume.is_paused:
            return True
        return self.printer.lookup_object("print_stats").state == "paused"

    def _cut_filament(self, request, force=False):
        self._enter_service(request)
        self.box.cut_filament(force=force)

    def _cutter_ready(self):
        if self.box.cut_x is not None:
            return True
        self._warn(
            "Skipping cutting because cutter is not calibrated; "
            "run CALIBRATE_CUT_POS")
        return False

    def _move_to_wastebin(self, request):
        self._enter_service(request)
        self.box.move_to_wastebin()

    def _enter_service(self, request):
        self._capture_service_origin(request)
        if request is None:
            return
        if request.service_z is None:
            eventtime = self.printer.get_reactor().monotonic()
            status = self.printer.lookup_object("toolhead").get_status(eventtime)
            if "z" not in status.get("homed_axes", ""):
                return
            position = self.printer.lookup_object(
                "gcode_move").get_status(eventtime)["gcode_position"]
            gcode_z_max = (
                position.z + status["axis_maximum"].z - status["position"].z)
            origin_z = (request.return_position[2]
                        if request.return_position is not None else position.z)
            request.service_z = max(
                origin_z, min(origin_z + self.toolchange_z_hop, gcode_z_max))
        self._raise_to_service_z(request)

    def _capture_service_origin(self, request):
        if request is None or not request.print_context:
            return
        if request.return_position is not None:
            return
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        eventtime = self.printer.get_reactor().monotonic()
        status = toolhead.get_status(eventtime)
        if "z" not in status.get("homed_axes", ""):
            return
        position = self.printer.lookup_object(
            "gcode_move").get_status(eventtime)["gcode_position"]
        self.gcode.run_script_from_command(
            "SAVE_GCODE_STATE NAME=%s" % SERVICE_STATE)
        request.return_position = (position.x, position.y, position.z)
        request.rebase_pause = not self._is_print_paused()

    def _raise_to_service_z(self, request):
        if request.service_z is None:
            return
        eventtime = self.printer.get_reactor().monotonic()
        current = self.printer.lookup_object(
            "gcode_move").get_status(eventtime)["gcode_position"].z
        if current >= request.service_z - 0.0001:
            return
        restore = request.return_position is None
        if restore:
            self.gcode.run_script_from_command(
                "SAVE_GCODE_STATE NAME=%s" % SERVICE_STATE)
        try:
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command(
                "G0 Z%.3f F%d" % (request.service_z, SERVICE_Z_F))
            self.printer.lookup_object("toolhead").wait_moves()
        finally:
            if restore:
                self.gcode.run_script_from_command(
                    "RESTORE_GCODE_STATE NAME=%s MOVE=0"
                    % SERVICE_STATE)

    def _return_from_service(self, request):
        if request.return_position is None or request.service_z is None:
            return False
        request.last_step = "return"
        self._raise_to_service_z(request)
        x, y, z = request.return_position
        toolhead = self.printer.lookup_object("toolhead")
        self.gcode.run_script_from_command("G90")
        self.gcode.run_script_from_command(
            "G0 X%.3f Y%.3f F%d" % (x, y, SERVICE_XY_F))
        toolhead.wait_moves()
        self.gcode.run_script_from_command(
            "G0 Z%.3f F%d" % (z, SERVICE_Z_F))
        toolhead.wait_moves()
        self.gcode.run_script_from_command(
            "RESTORE_GCODE_STATE NAME=%s MOVE=0" % SERVICE_STATE)
        return True

    # ------------------------------------------------------------------
    # Small host actions
    # ------------------------------------------------------------------

    def _operation_failed(
            self, gcmd, request, exc, fault_generation, raise_error=False):
        request.last_error = str(exc)
        if self._abort_active(fault_generation):
            request.resume = False
        if request.runout_recovery:
            self._restore_runout_target(request)
        message = "T%d %s failed: %s" % (
            request.target, request.last_step or "change", request.last_error)
        if request.print_context:
            self.block_resume(message)
            if not self._is_print_paused():
                self.box.pause_print()
            if raise_error:
                raise gcmd.error("[BOX]: " + self.recovery_instruction())
            self._warn(self.recovery_notice())
            return False
        self._warn(message)
        raise gcmd.error("[BOX]: %s" % request.last_error)

    def _wait_for_sensor(
            self, gcmd, detected, timeout,
            fault_generation, request=None):
        reactor = self.printer.get_reactor()
        deadline = None if timeout is None else reactor.monotonic() + timeout
        action = "Insert external filament" if detected else "Pull external filament out"
        self._info(gcmd, action)
        while self.box.filament_detected() != detected:
            self._check_abort(fault_generation, request)
            now = reactor.monotonic()
            if deadline is not None and now >= deadline:
                raise RuntimeError("External filament insertion timed out")
            wake = now + SENSOR_POLL
            reactor.pause(min(wake, deadline) if deadline is not None else wake)
        self._check_abort(fault_generation, request)

    def _relative_extrude(self, name, distance, speed):
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=%s" % name)
        try:
            self.gcode.run_script_from_command("M83")
            self.gcode.run_script_from_command("G1 E%.4f F%d" % (distance, speed))
            self.printer.lookup_object("toolhead").wait_moves()
        finally:
            self.gcode.run_script_from_command(
                "RESTORE_GCODE_STATE NAME=%s MOVE=0" % name)

    def _pause_runout(
            self, gcmd, reason, feed_tail=True, skip_retract_wipe=False):
        target = self.pending.target if self.pending is not None else (
            self.box.runout_origin
            if self.box.is_valid_slot(self.box.runout_origin)
            else self.box.last_loaded_slot)
        automatic = self.box.is_valid_slot(target)
        self.block_resume(
            "Filament runout: %s" % reason,
            target=target if automatic else None,
            automatic=automatic)
        self._warn(self.recovery_notice())
        if not self.box.pause_print(
                synchronous=True, skip_retract_wipe=skip_retract_wipe):
            raise RuntimeError("PAUSE did not settle before runout recovery")
        if feed_tail:
            self._feed_runout_tail(gcmd)
        return False

    def _abort_active(self, fault_generation):
        return (
            fault_generation != self.box.fault_generation
            or (self.pause_resume.pause_command_sent
                and not self.pause_resume.is_paused))

    def _check_abort(self, fault_generation, request=None, clog_epoch=None):
        try:
            self.box.check_operation_abort(fault_generation)
            if (clog_epoch is not None
                    and self.box.clog_event_count != clog_epoch):
                raise RuntimeError("clog detected during filament preparation")
        except Exception as exc:
            if request is not None:
                request.resume = False
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(str(exc))

    def _runout_retract_wipe(self):
        try:
            can_extrude = self.printer.lookup_object(
                "extruder").get_heater().can_extrude
        except Exception:
            can_extrude = False
        if not can_extrude:
            return False
        try:
            self.gcode.run_script_from_command(
                "_RETRACT_WIPE RETRACT=%.1f" % RUNOUT_RETRACT_MM)
        except Exception:
            self._warn("Unable to retract/wipe before automatic runout swap")
            return False
        return True

    def _feed_runout_tail(self, gcmd, distance=EXTERNAL_FEED_MM):
        try:
            can_extrude = self.printer.lookup_object(
                "extruder").get_heater().can_extrude
        except Exception:
            can_extrude = False
        if not can_extrude:
            return
        self._info(gcmd, "Feeding %.0fmm to clear the extruder gears" % distance)
        self._relative_extrude("box_runout_tail", distance, EXTERNAL_FEED_F)

    def _extruder_target(self):
        try:
            return self.printer.lookup_object(
                "extruder").get_heater().get_status(0)["target"]
        except Exception:
            return None

    def _clear_active_spool(self, slot):
        self.box.clear_active_spool(slot)

    def _info(self, responder, message):
        responder.respond_info("[BOX]: " + str(message))

    def _warn(self, message):
        self.gcode.respond_raw("!! [BOX]: " + str(message))
