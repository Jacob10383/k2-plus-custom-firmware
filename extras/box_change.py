# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Tool-change, unload, runout, and purge sequencing for the CFS box.

Live hardware determines reversible progress, while the few irreversible
steps are checkpointed for safe same-command retries.
"""

import copy
import logging
import math
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass

from extras import box_protocol
from extras.box_gcode import read_metadata


FILAMENT_AREA = math.pi * (1.75 / 2.0) ** 2
PURGE_CHUNK_MM = 100.0
EXTERNAL_PULL_MM = 125.0
EXTERNAL_FEED_MM = 30.0
RUNOUT_RETRACT_MM = 3.0
EXTERNAL_WAIT = 30.0
SENSOR_POLL = 0.1
TEMP_TOLERANCE = 2.0
TEMP_POLL = 0.5
LONG_TEMP_WAIT = 5.0
SNAP_RETRACT_MM = 1.2
# The CFS answers nothing while it pulls the path back, so its progress is
# estimated at the hub's measured pull speed and never shown as finished.
UNLOAD_ESTIMATE_RATE = 110.0
UNLOAD_ESTIMATE_CAP = 0.97
SERVICE_STATE = "box_change_recovery"
# What a paused print needs next. Cancelling is always possible and unsaid.
RETRY_ACTION = "Resume to try again."
LOAD_FILAMENT_ACTION = "Load filament, then resume."
LOAD_SLOT_ACTION = "Load a slot, then resume."
PULL_EXTERNAL_ACTION = "Pull the External filament out, then resume."
INSERT_EXTERNAL_ACTION = "Insert the External filament, then resume."


class UserStep(Exception):
    """A change stops for something only the user can do with the filament.

    A print never waits at the printhead for a person: it pauses, and RESUME
    is how the user says it is done. Outside a print the change ends with
    idle_message, and selecting the slot again carries on.
    """

    def __init__(self, reason, action, idle_message=None,
                 release_extruder=False):
        Exception.__init__(self, reason)
        self.reason = reason
        self.action = action
        self.idle_message = idle_message
        self.release_extruder = release_extruder


@dataclass
class ChangeRequest:
    target: int
    source: object
    flush: bool
    print_context: bool
    kind: str = "normal"
    retracted_source: object = None
    cut_source: object = None
    flush_done: bool = False
    prepared_filament: bool = False
    last_step: str = None
    last_error: str = None
    pre_box_target: object = None
    source_tool: object = None
    target_tool: object = None
    runout_recovery: bool = False
    return_position: object = None
    service_z: object = None
    rebase_pause: bool = False
    settled_temperature: object = None


class Operation:
    """What a Box command is doing, step by step, for printer.box.operation.

    Steps are planned up front and may be inserted (a heat wait that was not
    expected) or skipped (a cut with nothing loaded) as the command runs.
    Progress in mm is either reported (the CFS odometer) or estimated from a
    rate while a move runs that cannot be read back.
    """

    def __init__(self, reactor, kind, source, target, source_tool=None,
                 target_tool=None, steps=()):
        self.reactor = reactor
        self.kind = kind
        self.source = source
        self.target = target
        self.source_tool = source_tool
        self.target_tool = target_tool
        self.steps = [self._new(name) for name in steps]
        self.active = None
        self.state = "running"
        self.message = None
        self.since = reactor.monotonic()
        self.motion = None

    @staticmethod
    def _new(name, state="pending"):
        return {"name": name, "state": state, "done_mm": None,
                "total_mm": None, "estimated": False, "temperature": None}

    def step(self, name, temperature=None, total_mm=None):
        """Make name the active step; earlier steps not reached are skipped."""
        if (self.active is not None
                and self.steps[self.active]["name"] == name):
            current = self.steps[self.active]
        else:
            self._close_active()
            start = 0 if self.active is None else self.active + 1
            index = next(
                (i for i in range(start, len(self.steps))
                 if self.steps[i]["name"] == name
                 and self.steps[i]["state"] == "pending"), None)
            if index is None:
                index = start
                self.steps.insert(index, self._new(name))
            for skipped in self.steps[start:index]:
                if skipped["state"] == "pending":
                    skipped["state"] = "skipped"
            current = self.steps[index]
            current["state"] = "active"
            self.active = index
        if temperature is not None:
            current["temperature"] = float(temperature)
        if total_mm is not None:
            current["total_mm"] = float(total_mm)

    def planned(self, name):
        """Whether name is still to come."""
        start = 0 if self.active is None else self.active + 1
        return any(step["name"] == name and step["state"] == "pending"
                   for step in self.steps[start:])

    def progress(self, done_mm, total_mm=None, estimated=False):
        """Set how far the active step has moved, in mm."""
        if self.active is None:
            return
        self.motion = None
        current = self.steps[self.active]
        current["done_mm"] = max(0.0, float(done_mm))
        current["estimated"] = bool(estimated)
        if total_mm is not None:
            current["total_mm"] = float(total_mm)

    def moving(self, done_mm, until_mm, rate, estimated=False):
        """A move from done_mm toward until_mm at rate mm/s is under way."""
        if self.active is None:
            return
        self.progress(done_mm, estimated=estimated)
        self.motion = (float(done_mm), float(until_mm), float(rate),
                       self.reactor.monotonic())

    def _done_mm(self, eventtime):
        start, until, rate, started = self.motion
        return min(until, start + max(0.0, eventtime - started) * rate)

    def _close_active(self):
        if self.active is None:
            return
        current = self.steps[self.active]
        if self.motion is not None:
            current["done_mm"] = self.motion[1]
            self.motion = None
        current["state"] = "done"

    def finish(self):
        self._close_active()
        self.active = None
        for step in self.steps:
            if step["state"] == "pending":
                step["state"] = "skipped"

    def stop(self, state, message):
        """The command ended without finishing: waiting on the user, or failed."""
        if self.motion is not None:
            self.steps[self.active]["done_mm"] = self._done_mm(
                self.reactor.monotonic())
            self.motion = None
        self.state = state
        self.message = message
        self.since = self.reactor.monotonic()

    def status(self, eventtime):
        steps = []
        for index, step in enumerate(self.steps):
            if step["state"] == "skipped":
                continue
            step = dict(step)
            if index == self.active and self.motion is not None:
                step["done_mm"] = self._done_mm(eventtime)
            for key in ("done_mm", "total_mm"):
                if step[key] is not None:
                    step[key] = round(step[key], 1)
            steps.append(step)
        return {
            "kind": self.kind,
            "state": self.state,
            "source": self.source,
            "target": self.target,
            "source_tool": self.source_tool,
            "target_tool": self.target_tool,
            "elapsed": int(max(0.0, eventtime - self.since)),
            "message": self.message,
            "steps": steps,
        }


@dataclass(frozen=True)
class ResumeRecovery:
    target: object
    reason: str
    automatic: bool
    action: str


@dataclass(frozen=True)
class PauseThermalState:
    target: object


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
            "fallback_purge_length", 100.0, minval=0.0)
        self.default_temp = config.getint("default_temp", 220, minval=170, maxval=350)
        self.toolchange_z_hop = config.getfloat(
            "toolchange_z_hop", 2.0, minval=0.0)
        # The External filament is backed out of the extruder and waits in
        # the printhead for the user to pull it out.
        self.external_released = False
        self.operation = None
        self.pending = None
        self.matrix = None
        self.temp_print = None
        self.temp_initial_layer = None
        self.parsed_epoch = None
        self.prepared_epoch = None
        self.last_purge_length = 0.0
        self.resume_recovery = None
        self.resume_prepared = False
        self.pause_thermal = None
        self.reset_print_mapping()

    # ------------------------------------------------------------------
    # Public entry points owned and registered by Box
    # ------------------------------------------------------------------

    def reset_print_mapping(self):
        self.tool_map = {}
        self.mapping_filename = None
        self.active_tool = None
        self.active_slot = None

    def mapping_status(self):
        return {"filename": self.mapping_filename,
                "map": {str(tool): slot for tool, slot in self.tool_map.items()},
                "active_tool": self.active_tool, "active_slot": self.active_slot}

    def mapping_checkpoint(self):
        """Persist external slot identities across CFS topology changes."""
        state = self.mapping_status()
        state["map"] = {tool: self.box._runtime_slot_key(slot)
                        for tool, slot in state["map"].items()}
        if self.active_slot is not None:
            state["active_slot"] = self.box._runtime_slot_key(self.active_slot)
        return state

    def restore_print_mapping(self, state):
        self.reset_print_mapping()
        if state is None or state.get("filename") is None:
            return
        try:
            mapping = {int(tool): self.box._runtime_slot(slot)
                       for tool, slot in state["map"].items()}
            if (not isinstance(state["filename"], str)
                    or any(not 0 <= tool <= 255 or type(slot) is not int
                           or not self.box.is_valid_slot(slot)
                           for tool, slot in mapping.items())):
                raise ValueError()
            tool = state["active_tool"]
            slot = self.box._runtime_slot(state["active_slot"])
            if tool is not None and (type(tool) is not int or tool not in mapping
                                     or type(slot) is not int
                                     or not self.box.is_valid_slot(slot)):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("Invalid saved Box print mapping")
        self.box._register_tools(mapping)
        self.box._install_print_map(mapping, state["filename"], "restored")
        self.active_tool, self.active_slot = tool, slot

    def select_tool(self, gcmd, tool):
        flush = bool(gcmd.get_int("FLUSH", 1))
        if self.mapping_filename is None:
            return self.change(gcmd, tool, flush)
        if tool not in self.tool_map:
            reason = "This print has no slot chosen for T%d" % tool
            if not self._is_print_file_command():
                # A console command only reports; the print is unaffected.
                raise gcmd.error("[BOX]: " + reason)
            # An error from the file would end the print. Pause like any
            # other Box change failure and let the user choose a slot.
            self.block_resume(reason)
            self._warn(self.recovery_notice())
            self.box.pause_print()
            return False
        return self.change(gcmd, self.tool_map[tool], flush, logical_tool=tool)

    def select_slot(self, gcmd, slot, flush=True):
        """Load a physical slot, whatever the print's map says."""
        return self.change(
            gcmd, slot, flush, logical_tool=self._slot_tool(slot))

    def _slot_tool(self, slot):
        """The file tool a directly selected slot stands for, if it is clear."""
        if self.mapping_filename is None:
            return None
        request = self.pending
        if (request is not None and request.target == slot
                and request.target_tool is not None):
            # Finishing a failed change keeps the tool it was loading.
            return request.target_tool
        if slot == self.active_slot:
            return self.active_tool
        # Several tools can share a slot; only a single one names it.
        tools = [tool for tool, mapped in self.tool_map.items()
                 if mapped == slot]
        # The active tool's mapped slot, after a runout moved the tool
        # elsewhere, holds another spool: purge it as unknown.
        if len(tools) != 1 or tools[0] == self.active_tool:
            return None
        return tools[0]

    def _slot_label(self, slot, tool=None):
        """Physical slots get Box/slot names; mapped jobs also name the file tool."""
        label = box_protocol.slot_label(slot, self.box.external_slot)
        return label if tool is None else "T%d (%s)" % (tool, label)

    def _loaded_label(self, slot):
        return self._slot_label(
            slot, self.active_tool if slot == self.active_slot else None)

    def _commit_print_tool(self, request):
        if self.mapping_filename is None:
            return
        self.active_tool = request.target_tool
        self.active_slot = request.target if request.target_tool is not None else None

    def _settle_print_tool(self, gcmd, slot, fault_generation):
        """Another file tool on the loaded slot needs only its own temperature."""
        temperature = self._effective_temp(slot)
        if self._is_print_paused():
            # RESUME heats to the new tool's temperature, not the paused one.
            self.pause_thermal = PauseThermalState(target=temperature)
            return
        # The head is over the print, so only wait to heat; cooling can run on.
        self._start_heat(temperature)
        self._wait_for_heat(gcmd, temperature, fault_generation)

    def prime_for_power_loss_recovery(self, gcmd, target, temperature):
        final = int(round(float(temperature)))
        if not self.box.is_valid_slot(target) or not 170 <= final <= 350:
            raise RuntimeError("Invalid power-loss prime target")
        generation = self.box.fault_generation
        self._check_abort(generation)
        live = self.box.read_live_state()
        if (not self._target_ready(target, live)
                or self.box.hotend_feed_pending(target)):
            raise RuntimeError("Power-loss filament path is not ready")
        self.box.move_to_wastebin()
        self._start_heat(final)
        return self._prepare_filament(
            gcmd, target, target, allow_purge=False,
            prime_reason="power-loss recovery", prepared_temperature=final,
            fault_generation=generation, force_prime=True)

    def wait_for_power_loss_recovery_temperature(self, temperature):
        final = int(round(float(temperature)))
        if not 170 <= final <= 350:
            raise RuntimeError("Invalid power-loss recovery temperature")
        generation = self.box.fault_generation
        elapsed = self._wait_for_temperature_target(final, generation)
        self._clean_after_temperature_wait(elapsed, generation, None)

    def repush_after_filament_prepare(self):
        if self.last_purge_length or self.print_prime_length:
            self._relative_extrude(
                "box_prepare_repush", SNAP_RETRACT_MM, self.box.retract_velocity)

    def change(self, gcmd, target, flush=True, within_resume=False, logical_tool=None):
        if not self.box.is_valid_slot(target):
            raise gcmd.error(
                "[BOX]: %s is not an online CFS slot or the external spool"
                % self._slot_label(target, logical_tool))
        started = self.printer.get_reactor().monotonic()
        fault_generation = self.box.fault_generation

        inherited_recovery = (
            self.pending
            if (self.pending and self.pending.target != target
                and (self.pending.runout_recovery
                     or self.pending.return_position is not None
                     or self.pending.service_z is not None))
            else None)

        read_error = None
        try:
            live = self.box.read_live_state()
        except Exception as exc:
            if self.pending and self.pending.target == target:
                return self._operation_failed(
                    gcmd, self.pending, exc, fault_generation,
                    raise_error=within_resume)
            # A path conflict met by the print file before the poll: the
            # file has moved past this change, so it fails below and RESUME
            # retries it. A console selection just errors.
            if (within_resume or self.box.path_conflict is None
                    or not self._is_print_file_command()):
                raise gcmd.error("[BOX]: Unable to read CFS state: %s" % exc)
            live, read_error = None, exc
        if live is not None and live.loaded_slot is None:
            raise gcmd.error("[BOX]: CFS loaded-slot state is unavailable")
        loaded = self.box.path_owner if live is None else live.loaded_slot

        request = self.pending if self.pending and self.pending.target == target else None
        fresh_change = request is None and inherited_recovery is None
        if request is None:
            print_context = self._is_print_file_command() or self._is_print_active()
            if inherited_recovery:
                source = inherited_recovery.source
            elif self.box.is_valid_slot(loaded):
                source = loaded
            else:
                source = self.box.last_loaded_slot
            pre_box_target = (
                inherited_recovery.pre_box_target
                if inherited_recovery else self._print_target(print_context))
            request = ChangeRequest(
                target=target,
                source=source,
                source_tool=(inherited_recovery.source_tool if inherited_recovery
                             else self.active_tool if source == self.active_slot else None),
                target_tool=(inherited_recovery.target_tool
                             if within_resume and inherited_recovery else logical_tool),
                flush=bool(flush),
                print_context=(inherited_recovery.print_context
                               if inherited_recovery else print_context),
                retracted_source=(inherited_recovery.retracted_source
                                  if inherited_recovery else None),
                cut_source=(inherited_recovery.cut_source
                            if inherited_recovery else None),
                pre_box_target=pre_box_target,
                runout_recovery=(inherited_recovery.runout_recovery
                                 if inherited_recovery else False),
                return_position=(inherited_recovery.return_position
                                 if inherited_recovery else None),
                service_z=(inherited_recovery.service_z
                           if inherited_recovery else None),
                rebase_pause=(inherited_recovery.rebase_pause
                              if inherited_recovery else False),
            )
        elif not within_resume:
            request.target_tool = logical_tool
        self.pending = request
        request.last_error = None
        if read_error is not None:
            return self._operation_failed(
                gcmd, request, read_error, fault_generation)
        # Nothing loaded, or the target already: a load, whatever filament
        # the purge has to clear from the nozzle.
        # The title names what is loaded now; a retargeted failed change may
        # keep an older source for its purge.
        changing = (self.box.is_valid_slot(live.loaded_slot)
                    and live.loaded_slot != target)
        if request.kind == "runout":
            shown, shown_tool = request.source, request.source_tool
        elif changing:
            shown = live.loaded_slot
            shown_tool = (request.source_tool if live.loaded_slot == request.source
                          else self.active_tool
                          if live.loaded_slot == self.active_slot else None)
        else:
            shown, shown_tool = None, None
        self._op_start(
            request.kind if request.kind == "runout"
            else "change" if changing else "load",
            shown, target, self._change_plan(request, live),
            shown_tool, request.target_tool)

        target_label = self._slot_label(target, request.target_tool)
        if live.loaded_slot != target:
            if self.box.is_valid_slot(live.loaded_slot):
                self._info(gcmd, "Changing %s -> %s" % (
                    self._loaded_label(live.loaded_slot), target_label))
            else:
                self._info(gcmd, "Changing to %s" % target_label)

        noop_reported = False
        if live.loaded_slot == target and request.kind == "runout":
            self._info(gcmd, "Preparing %s" % target_label)

        try:
            self._check_abort(fault_generation)
            if request.kind == "runout":
                if request.runout_recovery:
                    if request.last_step != "restore":
                        self._move_to_wastebin(request)
                        self._execute_runout(
                            gcmd, request, live, fault_generation)
                        request.last_step = "restore"
                    self._finish_runout(request)
                else:
                    self._restore_pre_box_target(request)
            else:
                noop_reported = self._execute(
                    gcmd, request, live, fault_generation, fresh_change)
                if request.runout_recovery:
                    request.last_step = "restore"
                    self._finish_runout(request)
                else:
                    self._return_from_service(request)
            self._check_abort(fault_generation)
        except UserStep as step:
            return self._stop_for_user(
                gcmd, request, step, raise_error=within_resume)
        except Exception as exc:
            return self._operation_failed(
                gcmd, request, exc, fault_generation,
                raise_error=within_resume)

        self._op_end()
        self._commit_print_tool(request)
        self.pending = None
        self.resume_prepared = bool(request.prepared_filament)
        if (request.source != request.target
                and request.settled_temperature is not None
                and self._is_print_paused()):
            # RESUME continues at the new filament's temperature.
            self.pause_thermal = PauseThermalState(
                target=request.settled_temperature)
        self.clear_resume_recovery()
        if not request.print_context and request.flush:
            self.gcode.run_script_from_command("M104 S0")
        if not noop_reported:
            self._info(gcmd, "%s active in %.1fs" % (
                target_label, self.printer.get_reactor().monotonic() - started))
        return True

    def unload(self, gcmd, manual=False, cfs_only=False):
        """Complete user-facing unload for physical or external filament.

        cfs_only leaves anything not fed from a CFS slot alone.
        """
        fault_generation = self.box.fault_generation
        try:
            live = self.box.read_live_state()
        except Exception as exc:
            raise gcmd.error(
                "[BOX]: " + box_protocol.format_failed("Unload", exc))
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
        if cfs_only and not physical:
            logging.info(
                "box: BOX_UNLOAD CFS_ONLY=1 skipped: loaded_slot=%s, "
                "filament_detected=%s", live.loaded_slot,
                live.filament_detected)
            return
        self._op_start(
            "unload", live.loaded_slot, None, self._unload_plan(
                live, physical,
                physical and self.box.tail_loaded(live, note=False),
                manual))
        heater_used = False
        self._remember_hotend_filament(live.loaded_slot)
        sensor = (self.box.printhead_sensor_off() if physical
                  else nullcontext())
        with sensor:
            try:
                self._check_abort(fault_generation)
                if physical:
                    source = live.loaded_slot
                    if self.box.tail_loaded(live):
                        # The CFS cannot pull a tail back: purge it out, or
                        # with MANUAL=1 release it for the user to pull out.
                        heater_used = True
                        if manual:
                            self._unload_external(
                                gcmd, fault_generation, source=source)
                        else:
                            self._eject_tail(
                                gcmd, None, source, fault_generation)
                    elif manual:
                        self._op_step("unload")
                        self.box.physical_unload(
                            allow_extruder_retract=True,
                            fault_generation=fault_generation)
                    elif live.filament_detected is False:
                        self._op_step("unload")
                        self.box.physical_unload(
                            allow_extruder_retract=False,
                            fault_generation=fault_generation)
                    else:
                        heater_used = True
                        temperature = self._purge_temperature(source, None)
                        can_cut = self._cutter_ready()
                        self._start_heat_home_and_wait(
                            gcmd, temperature, fault_generation)
                        if can_cut:
                            self._op_step("cut")
                            self.box.retract_for_cut(
                                self.retract_length,
                                fault_generation=fault_generation)
                            self._check_abort(fault_generation)
                            self.box.cut_filament(
                                force=not bool(live.filament_detected))
                            self._check_abort(fault_generation)
                        self.box.move_to_wastebin()
                        self._check_abort(fault_generation)
                        self._op_step("unload")
                        self.box.physical_unload(
                            allow_extruder_retract=True,
                            fault_generation=fault_generation)
                    self._clear_active_spool(source)
                else:
                    # CFS empty plus a detected sensor is the external path.
                    heater_used = True
                    self._unload_external(gcmd, fault_generation)

                self._check_abort(fault_generation)
                if heater_used:
                    self.gcode.run_script_from_command("M104 S0")
                self._op_end()
                self._info(
                    gcmd, "Unload complete%s"
                    % (", heater off" if heater_used else ""))
            except Exception as exc:
                self._op_end()
                message = box_protocol.format_failed("Unload", exc)
                if heater_used:
                    self.gcode.run_script_from_command("M104 S0")
                    message = "%s. Heater off." % message.rstrip(".")
                raise gcmd.error("[BOX]: " + message)

    def runout(self, gcmd):
        fault_generation = self.box.fault_generation
        external = self.box.last_loaded_slot == self.box.external_slot
        slot = self._runout_slot()
        ran_out = ("%s ran out" % self._slot_label(slot)
                   if self.box.is_valid_slot(slot)
                   else "Filament ran out at the printhead")
        recovery = None
        if self.box.runout_swap_enabled:
            try:
                recovery = self.box.runout_recovery()
            except Exception as exc:
                return self._pause_runout(
                    gcmd, "%s, and the CFS could not be read: %s"
                    % (ran_out, exc), feed_tail=False)
        if self.box.runout_active:
            # Said earlier that the spool ended; this is its tail ending.
            self._info(gcmd, "The leftover filament is used up.")
        if external and (
                recovery is None
                or (not recovery.get("recoverable")
                    and not self.box.runout_active)):
            self._remember_hotend_filament(self.box.external_slot)
            self._clear_active_spool(self.box.external_slot)
            return self._pause_runout(gcmd, ran_out)
        if recovery is None:
            return self._pause_runout(gcmd, ran_out)
        if not recovery.get("recoverable"):
            return self._pause_runout(gcmd, recovery.get("reason", ran_out))

        source = recovery["loaded_slot"]
        target = recovery["target_slot"]
        request = ChangeRequest(
            target=target,
            source=source,
            flush=True,
            print_context=True,
            kind="runout",
            source_tool=self.active_tool,
            target_tool=self.active_tool,
            retracted_source=source,
            cut_source=source,
            pre_box_target=self._extruder_target(),
        )
        self._info(gcmd, "Auto runout swap: %s -> %s" % (
            self._slot_label(source), self._slot_label(target)))
        request.runout_recovery = True
        self.pending = request
        self._op_start("runout", source, target,
                       ["unload", "load", "purge", "return"],
                       self.active_tool, self.active_tool)
        wiped = False
        try:
            self._capture_service_origin(request)
            wiped = self._runout_retract_wipe()
            self._move_to_wastebin(request)
            self._check_abort(fault_generation)
            self._feed_runout_tail(
                gcmd, EXTERNAL_FEED_MM + (RUNOUT_RETRACT_MM if wiped else 0.0))
            self._check_abort(fault_generation)
            self._execute_runout(
                gcmd, request, self.box.read_live_state(), fault_generation)
            request.last_step = "restore"
            self._finish_runout(request)
            self._check_abort(fault_generation)
        except Exception as exc:
            self._restore_pre_box_target(request)
            try:
                return self._pause_runout(
                    gcmd, box_protocol.format_failed(
                        "Runout swap to %s" % self._slot_label(target), exc),
                    feed_tail=False, skip_retract_wipe=wiped,
                    action=RETRY_ACTION)
            finally:
                self._op_stop("failed")

        self._op_end()
        self._commit_print_tool(request)
        self.pending = None
        self.clear_resume_recovery()
        self._info(gcmd, "Auto runout swap complete: %s active"
                   % self._slot_label(target))
        return True

    def parse_flush_volumes(self, gcmd):
        self._clear_parsed_data()
        sd = self.printer.lookup_object("virtual_sdcard")
        path = sd.get_status(self.printer.get_reactor().monotonic()).get("file_path")
        if not path:
            raise gcmd.error("[BOX]: No file currently loaded")
        try:
            metadata = read_metadata(path)
        except OSError as exc:
            raise gcmd.error("[BOX]: Failed to read gcode file: %s" % exc)

        self.matrix = metadata["matrix"]
        self.temp_print = metadata["temp_print"]
        self.temp_initial_layer = metadata["temp_initial_layer"]
        self.parsed_epoch = self._print_epoch()
        if self.matrix is None:
            self._info(gcmd, "No usable flush_volumes_matrix; using fallback purge")
        else:
            size = len(self.matrix)
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
                "pre_box_target": request.pre_box_target,
                "runout_recovery": request.runout_recovery,
                "last_step": request.last_step,
                "last_error": request.last_error,
            },
            "parsed_flush_current": self._parsed_is_current(),
            "last_purge_length": self.last_purge_length,
            "prepared_epoch": self.prepared_epoch,
            "pause_thermal": self.pause_thermal_status(),
            "resume_prepared": self.resume_prepared,
            "resume_recovery": self.recovery_status(),
        }

    # ------------------------------------------------------------------
    # Operation progress (printer.box.operation)
    # ------------------------------------------------------------------

    def operation_status(self, eventtime):
        operation = self.operation
        return None if operation is None else operation.status(eventtime)

    def running_operation(self):
        operation = self.operation
        if operation is None or operation.state != "running":
            return None
        return operation

    def _op_start(self, kind, source, target, steps,
                  source_tool=None, target_tool=None):
        self.operation = Operation(
            self.printer.get_reactor(), kind,
            source if self.box.is_valid_slot(source) else None,
            target if self.box.is_valid_slot(target) else None,
            source_tool, target_tool, steps)

    def _op_step(self, name, temperature=None, total_mm=None):
        operation = self.running_operation()
        if operation is not None:
            operation.step(name, temperature=temperature, total_mm=total_mm)

    def op_progress(self, done_mm, total_mm=None, estimated=False):
        operation = self.running_operation()
        if operation is not None:
            operation.progress(done_mm, total_mm, estimated)

    def _op_moving(self, done_mm, until_mm, rate, estimated=False):
        """An extruder move at rate mm/s, which the speed override scales."""
        operation = self.running_operation()
        if operation is None:
            return
        try:
            factor = float(self.printer.lookup_object("gcode_move").get_status(
                self.printer.get_reactor().monotonic()).get("speed_factor", 1.0))
        except Exception:
            factor = 1.0
        operation.moving(
            done_mm, until_mm, rate * (factor if factor > 0 else 1.0),
            estimated)

    def _op_stop(self, state):
        """The print paused: keep the steps on show with what it needs."""
        operation = self.running_operation()
        if operation is not None:
            operation.stop(state, self.recovery_instruction())

    def _op_end(self):
        self.operation = None

    def _change_plan(self, request, live):
        target = request.target
        loaded = live.loaded_slot
        steps = []
        if loaded != target:
            if self.box.is_physical_slot(loaded):
                if request.kind == "runout" or live.filament_detected is False:
                    steps.append("unload")
                elif self.box.tail_loaded(live, note=False):
                    steps += ["heat", "unload"]
                else:
                    steps += ["heat", "cut", "unload"]
            elif loaded == self.box.external_slot:
                steps += ["heat", "cut", "pull"]
        # The new filament may need it hotter than the unload did, or the
        # nozzle may be cold with nothing to unload.
        source = loaded if self.box.is_valid_slot(loaded) else request.source
        load_temperature = self._purge_temperature(source, target)
        if "heat" in steps:
            load_heat = (load_temperature
                         > self._purge_temperature(source, None)
                         + TEMP_TOLERANCE)
        else:
            load_heat = self._nozzle_below(load_temperature)
        if loaded != target:
            if target == self.box.external_slot:
                if load_heat:
                    # The External feed waits for a hot nozzle first.
                    steps.append("heat")
                    load_heat = False
                steps.append("insert")
            steps.append("load")
        if request.flush and load_heat:
            # It heats while the CFS feeds; the purge waits for it.
            steps.append("heat")
        if request.flush:
            steps.append("purge")
        if request.print_context and request.flush and request.kind != "runout":
            steps.append("temperature")
        if request.print_context and not self._is_print_paused():
            steps.append("return")
        return steps

    def _nozzle_below(self, temperature):
        try:
            heater = self.printer.lookup_object("heaters").lookup_heater(
                "extruder")
            current, _target = heater.get_temp(
                self.printer.get_reactor().monotonic())
        except Exception:
            return False
        return current < temperature - TEMP_TOLERANCE

    @staticmethod
    def _unload_plan(live, physical, tail, manual):
        if not physical:
            return ["heat", "cut", "pull"]
        if tail:
            return ["heat", "cut", "pull"] if manual else ["heat", "unload"]
        if manual or live.filament_detected is False:
            return ["unload"]
        return ["heat", "cut", "unload"]

    def block_resume(self, reason, target=None, automatic=None, action=None):
        request = self.pending
        if request is not None and request.print_context:
            target = request.target
            if automatic is None:
                automatic = True
        else:
            automatic = bool(automatic)
        if request is not None and request.print_context:
            self._begin_pause_thermal(request.pre_box_target)
        elif self._is_print_active():
            self._begin_pause_thermal(None)
        self.resume_prepared = False
        if action is None:
            action = (RETRY_ACTION if automatic and target is not None
                      else LOAD_SLOT_ACTION)
        self.resume_recovery = ResumeRecovery(
            target=target,
            reason=str(reason),
            automatic=bool(automatic),
            action=action,
        )

    def clear_resume_recovery(self):
        self.resume_recovery = None
        if self.operation is not None and self.operation.state != "running":
            self.operation = None

    def reset_print_recovery(self, *args):
        self.reset_print_mapping()
        self.operation = None
        self.pending = None
        self.resume_prepared = False
        self.clear_resume_recovery()
        self.pause_thermal = None

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
            }
        request_matches = (
            request is not None and request.target == recovery.target)
        return {
            "blocked": True,
            "automatic": recovery.automatic,
            "target": recovery.target,
            "step": request.last_step if request_matches else None,
            "reason": recovery.reason,
        }

    def pause_thermal_status(self):
        state = self.pause_thermal
        return {
            "active": state is not None,
            "resume_temperature": None if state is None else state.target,
        }

    def capture_pause(self, _gcmd=None):
        self._begin_pause_thermal(None)
        # Only filament prepared during this pause may skip the resume prime.
        self.resume_prepared = False

    def prepare_resume(self, gcmd):
        """Finish Box recovery, then heat and prime at the wastebin.

        The head stays off the print; RESUME_BASE makes the only trip back.
        """
        # Whatever the recovery says, nothing feeds while the path is unknown.
        self._refuse_path_conflict(gcmd)
        self.resume_check(gcmd, retry=True)
        self._refuse_path_conflict(gcmd)
        slot = self.box.last_loaded_slot
        if not self.box.is_valid_slot(slot):
            raise gcmd.error(
                "[BOX]: No loaded slot is known. " + LOAD_SLOT_ACTION)
        try:
            detected = self.box.filament_detected()
        except Exception:
            detected = None
        if detected is False and not self.box.runout_defer_active:
            # Unloaded during the pause: which slot comes next is the
            # user's choice. A runout still waiting to be handled is not
            # that; it swaps or pauses once the print moves again.
            raise gcmd.error(
                "[BOX]: No filament is loaded. " + LOAD_SLOT_ACTION)
        generation = self.box.fault_generation
        temperature = self._resume_temperature(slot)
        # A load after the prepared change leaves filament short of the nozzle.
        prime = not self.resume_prepared or self.box.hotend_feed_pending(slot)
        self._op_start(
            "resume", None, slot,
            ["heat", "purge", "temperature"] if prime else ["temperature"],
            target_tool=self.active_tool if slot == self.active_slot else None)
        try:
            self.box.move_to_wastebin()
            if prime:
                self._start_heat(temperature)
                self._prepare_filament(
                    gcmd, slot, slot, allow_purge=False,
                    prime_reason="resume", prepared_temperature=temperature,
                    fault_generation=generation, force_prime=True)
            self._op_step("temperature", temperature=temperature)
            elapsed = self._wait_for_temperature_target(
                temperature, generation)
            self._clean_after_temperature_wait(elapsed, generation, None)
            if prime:
                self.repush_after_filament_prepare()
            conflict = self.box.path_conflict
            if conflict is not None:
                # Came back while heating: cool down like any failure here.
                raise RuntimeError(conflict[1])
        except Exception as exc:
            message = "RESUME preparation failed: %s" % exc
            # The print stays paused: keep the steps up with what failed.
            operation = self.running_operation()
            if operation is not None:
                operation.stop("failed", message)
            self.gcode.run_script_from_command("M104 S140")
            raise gcmd.error("[BOX]: " + message)
        self._op_end()
        self.resume_check(gcmd)

    def _refuse_path_conflict(self, gcmd):
        conflict = self.box.path_conflict
        if conflict is not None:
            raise gcmd.error("[BOX]: %s Then resume." % conflict[1])

    def _resume_temperature(self, slot):
        target = self.pause_resume_temperature()
        if target is not None and target > 0:
            return target
        return self._effective_temp(slot)

    def complete_pause_resume(self, _gcmd=None):
        self.pause_thermal = None
        self.resume_prepared = False

    def pause_resume_temperature(self):
        state = self.pause_thermal
        return None if state is None else state.target

    def _begin_pause_thermal(self, target):
        if self.pause_thermal is not None:
            return
        if target is None:
            target = self._extruder_target()
        try:
            target = None if target is None else float(target)
        except (TypeError, ValueError):
            target = None
        self.pause_thermal = PauseThermalState(target=target)

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
                source_tool=self.active_tool,
                target_tool=self.active_tool,
                flush=True,
                print_context=True,
                last_step="attendance",
                pre_box_target=self.pause_resume_temperature(),
            )
            self.pending = request
        flush = (
            request.flush
            if request is not None and request.target == recovery.target
            else True)
        self._info(gcmd, "Retrying %s" % self._slot_label(recovery.target))
        # A retry of the active slot keeps its file tool's temperature and purge.
        logical_tool = (self.active_tool
                        if recovery.target == self.active_slot else None)
        try:
            if not self.change(
                    gcmd, recovery.target, flush=flush, within_resume=True,
                    logical_tool=logical_tool):
                raise gcmd.error("[BOX]: " + self.recovery_instruction())
        except Exception:
            self.gcode.run_script_from_command("M104 S140")
            raise
        return True

    def recovery_instruction(self):
        """Why RESUME is refused, and what it needs first."""
        return "%s %s" % self._recovery_text()

    def recovery_notice(self):
        """What paused the print, then what it needs: one line each."""
        return "%s\nPrint paused. %s" % self._recovery_text()

    def _recovery_text(self):
        recovery = self.resume_recovery
        if recovery is None:
            return "Box recovery is incomplete.", LOAD_SLOT_ACTION
        reason = recovery.reason.rstrip()
        if reason[-1:] not in (".", "!", "?", "]"):
            reason += "."
        return reason, recovery.action

    # ------------------------------------------------------------------
    # Unified execution
    # ------------------------------------------------------------------

    def _execute(self, gcmd, request, live, fault_generation, report_noop):
        self._check_abort(fault_generation)
        target = request.target
        source = request.source
        prepared_temperature = None
        ready = self._target_ready(target, live)
        # Sensed External filament still has to be fed in: just inserted
        # for this change, or released earlier and never pulled out.
        external_capture = (
            target == self.box.external_slot
            and ready
            and (self.external_released
                 or (request.last_step in ("attendance", "load")
                     and not self.box.hotend_feed_pending(target))))
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
                "%s already loaded and primed"
                if report_noop
                else "Preparing %s")
            self._info(gcmd, message % self._slot_label(target, request.target_tool))
        if already_prepared:
            self._check_abort(fault_generation)
            self.box.activate_tracking(target) if self.box.is_physical_slot(target) else None
            self._commit_loaded_slot(target)
            if (self.mapping_filename is not None
                    and request.target_tool != request.source_tool and request.flush):
                self._settle_print_tool(gcmd, target, fault_generation)
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
                self._release_external(gcmd, request, fault_generation)
                # Only the user can take it out, and only they know when
                # the printhead is ready for the next filament.
                request.last_step = "attendance"
                self._op_step("pull")
                raise UserStep(
                    "%s is next" % self._slot_label(
                        target, request.target_tool),
                    PULL_EXTERNAL_ACTION,
                    "Pull the External filament out, then load %s."
                    % self._slot_label(target),
                    release_extruder=True)

            live = self.box.read_live_state()
            self._check_abort(fault_generation)
            if live.loaded_slot not in (-1, target):
                raise RuntimeError("Unexpected loaded %s after unload"
                                   % self._slot_label(live.loaded_slot))

        if not self._target_ready(target, live) or external_capture:
            request.last_step = "load"
            if self.box.is_physical_slot(target):
                self._op_step("load")
                if request.flush:
                    prepared_temperature = self._prepare_hotend(
                        source, target, fault_generation, request)
                self.box.physical_load(
                    target, fault_generation=fault_generation)
                prepared_temperature = self._reheat_after_load(
                    source, target, prepared_temperature)
            else:
                if request.print_context and not live.filament_detected:
                    request.last_step = "attendance"
                    self._op_step("insert")
                    raise UserStep(
                        "The External spool has no filament"
                        if source == self.box.external_slot
                        else "The External spool is next",
                        INSERT_EXTERNAL_ACTION)
                prepared_temperature = self._load_external(
                    gcmd, source, fault_generation, request)
            live = self.box.read_live_state()
            self._check_abort(fault_generation)
            if not self._target_ready(target, live):
                raise RuntimeError("%s load completed without verified final state"
                                   % self._slot_label(target))

        self._check_abort(fault_generation)
        self._commit_loaded_slot(target)
        if request.flush and not request.flush_done:
            request.last_step = "flush"
            # The purge's fan guard stays open until the final temperature
            # wait, so the last snap's fan runs straight into a cooldown.
            with ExitStack() as purge_fan:
                prepared = self._prepare_filament(
                    gcmd, source, target,
                    prepared_temperature=prepared_temperature,
                    fault_generation=fault_generation, request=request,
                    purge_fan=purge_fan if request.print_context else None)
                request.prepared_filament = bool(
                    request.prepared_filament or prepared)
                request.flush_done = True
                if request.print_context:
                    self.prepared_epoch = self._print_epoch()
                    request.last_step = "final_temperature"
                    self._wait_for_final_temperature(
                        target, fault_generation, request,
                        purge_fan=purge_fan)
        elif (request.flush and request.flush_done and request.print_context
              and request.last_step == "final_temperature"):
            self._wait_for_final_temperature(
                target, fault_generation, request)
        return False

    def _execute_runout(self, gcmd, request, live, fault_generation):
        self._check_abort(fault_generation)
        source = request.source
        target = request.target
        prepared_temperature = None
        if live.loaded_slot is None:
            raise RuntimeError("CFS loaded-slot state is unavailable")
        if live.loaded_slot != target:
            if self.box.is_physical_slot(live.loaded_slot):
                request.last_step = "unload"
                self._op_step("unload")
                if self.box.tail_loaded(live):
                    self._eject_tail(
                        gcmd, request, live.loaded_slot, fault_generation)
                else:
                    self.box.physical_unload(
                        allow_extruder_retract=False,
                        fault_generation=fault_generation)
                live = self.box.read_live_state()
                self._check_abort(fault_generation)
            elif live.loaded_slot == self.box.external_slot:
                raise RuntimeError("External filament is loaded during runout recovery")
            if live.loaded_slot not in (-1, target):
                raise RuntimeError(
                    "Unexpected loaded %s during runout recovery"
                    % self._slot_label(live.loaded_slot))

        if not self._target_ready(target, live):
            request.last_step = "load"
            self._op_step("load")
            prepared_temperature = self._prepare_hotend(
                source, target, fault_generation, request)
            self.box.physical_load(
                target, fault_generation=fault_generation)
            prepared_temperature = self._reheat_after_load(
                source, target, prepared_temperature)
            live = self.box.read_live_state()
            self._check_abort(fault_generation)
            if not self._target_ready(target, live):
                raise RuntimeError(
                    "%s load completed without verified final state"
                    % self._slot_label(target))

        self._check_abort(fault_generation)
        self._commit_loaded_slot(target)
        if not request.flush_done:
            request.last_step = "flush"
            prepared = self._prepare_filament(
                gcmd, target, target, temperature_source=source,
                allow_purge=False, prime_reason="runout replacement",
                prepared_temperature=prepared_temperature,
                fault_generation=fault_generation, request=request)
            request.prepared_filament = bool(
                request.prepared_filament or prepared)
            request.flush_done = True
            self.prepared_epoch = self._print_epoch()

    def _finish_runout(self, request):
        self._restore_pre_box_target(request)
        if request.kind == "runout" and request.prepared_filament:
            self.repush_after_filament_prepare()
        self._return_from_service(request)
        request.runout_recovery = False

    def _restore_pre_box_target(self, request):
        if not request.pre_box_target or request.pre_box_target <= 0:
            return
        request.settled_temperature = request.pre_box_target
        try:
            self.gcode.run_script_from_command(
                "M104 S%g" % request.pre_box_target)
        except Exception:
            self._warn("Unable to restore the pre-Box heater target")

    def _unload_physical(self, gcmd, request, live, fault_generation):
        self._check_abort(fault_generation)
        source = live.loaded_slot
        if self.box.tail_loaded(live):
            self._eject_tail(gcmd, request, source, fault_generation)
            self._clear_active_spool(source)
            return
        self._remember_hotend_filament(source)
        sensor_clear = live.filament_detected is False
        source_prepared = (
            request.retracted_source == source and request.cut_source == source)
        can_cut = not sensor_clear and self._cutter_ready()
        # The unload itself turns it back on once the path is clear.
        with self.box.printhead_sensor_off(after="leave"):
            if not sensor_clear and request.retracted_source != source:
                request.last_step = "retract"
                self._start_heat_home_and_wait(
                    gcmd, self._purge_temperature(source, None),
                    fault_generation, request)
                self._op_step("cut")
                self.box.retract_for_cut(
                    self.retract_length,
                    fault_generation=fault_generation)
                request.retracted_source = source
            if not sensor_clear and request.cut_source != source:
                if can_cut:
                    request.last_step = "cut"
                    self._op_step("cut")
                    self._check_abort(fault_generation)
                    self._cut_filament(
                        request, force=not bool(live.filament_detected))
                request.cut_source = source
            if not sensor_clear:
                self._check_abort(fault_generation)
                self._move_to_wastebin(request)
            request.last_step = "unload"
            self._op_step("unload")
            self._check_abort(fault_generation)
            self.box.physical_unload(
                allow_extruder_retract=not sensor_clear and not source_prepared,
                fault_generation=fault_generation)
            self._clear_active_spool(source)

    def _eject_tail(self, gcmd, request, source, fault_generation):
        """Purge a tail out through the nozzle until the sensor clears."""
        self._check_abort(fault_generation)
        self.box.release_tail(source)
        self._remember_hotend_filament(source)
        with self.box.printhead_sensor_off():
            if request is not None:
                request.last_step = "unload"
            self._start_heat_home_and_wait(
                gcmd, self._purge_temperature(source, None),
                fault_generation, request)
            self._move_to_wastebin(request)
            self._check_abort(fault_generation)
            self._info(gcmd, "%s ran out; purging its leftover filament"
                       % self._loaded_label(source))
            self._op_step("unload")
            feedrate = self.purge_speed / FILAMENT_AREA * 60.0
            clog_epoch = self.box.clog_event_count
            with self.box.part_fan_override(0.0):
                self.gcode.run_script_from_command(
                    "SAVE_GCODE_STATE NAME=box_tail_eject")
                try:
                    self.gcode.run_script_from_command("M83")
                    remaining = self.box.tail_budget()
                    purged = 0.0
                    while self.box.filament_detected():
                        if remaining <= 0.0:
                            self.box.drop_tail()
                            raise RuntimeError(
                                "the leftover filament did not run out; "
                                "unload again to pull it out by hand")
                        amount = min(PURGE_CHUNK_MM, remaining)
                        purged = self._purge_moves(
                            amount, feedrate, clog_epoch, fault_generation,
                            done=purged)
                        remaining -= amount
                    # The end still has to pass the extruder gears.
                    self._purge_moves(
                        EXTERNAL_FEED_MM + SNAP_RETRACT_MM, feedrate,
                        clog_epoch, fault_generation, done=purged)
                finally:
                    self.gcode.run_script_from_command(
                        "RESTORE_GCODE_STATE NAME=box_tail_eject MOVE=0")
            self.box.finish_tail(source)

    def _free_filament(self, gcmd, request, fault_generation, source):
        """Retract, cut, and back the filament out of the extruder gears."""
        self._remember_hotend_filament(source)
        can_cut = self._cutter_ready()
        # A retry may start from a nozzle the pause cooled; the pull below
        # is an extruder move too.
        self._start_heat_home_and_wait(
            gcmd, self._purge_temperature(source, None),
            fault_generation, request)
        self._op_step("cut")
        if request is None or request.retracted_source != source:
            self._relative_extrude(
                "box_external_unload", -self.retract_length,
                self.box.retract_velocity)
            if request:
                request.retracted_source = source
        if request is None or request.cut_source != source:
            if can_cut:
                self._check_abort(fault_generation)
                self._cut_filament(request, force=True)
            if request:
                request.cut_source = source
        self._check_abort(fault_generation)
        self._move_to_wastebin(request)
        self._check_abort(fault_generation)
        self._relative_extrude(
            "box_external_pull", -EXTERNAL_PULL_MM, self.box.retract_velocity)

    def printhead_cleared(self):
        """The printhead sensor went clear: released filament was pulled."""
        self.external_released = False

    def _release_external(self, gcmd, request, fault_generation):
        """Free the External filament for the user to pull out, once."""
        source = self.box.external_slot
        if self.external_released:
            return
        self._check_abort(fault_generation)
        with self.box.printhead_sensor_off(after="restore"):
            self._free_filament(gcmd, request, fault_generation, source)
        self.external_released = True
        self.box.clear_hotend_feed_pending(source)
        self._clear_active_spool(source)

    def _unload_external(self, gcmd, fault_generation, source=None):
        """Release filament and wait for the user to pull it out: the
        External spool's, or a tail's."""
        self._check_abort(fault_generation)
        tail = source is not None
        if tail:
            self.box.release_tail(source)
        else:
            source = self.box.external_slot
        with self.box.printhead_sensor_off():
            self._free_filament(gcmd, None, fault_generation, source)
            self._op_step("pull")
            self.gcode.run_script_from_command(
                "SET_STEPPER_ENABLE STEPPER=extruder ENABLE=0")
            try:
                self._wait_for_sensor(
                    gcmd, False, timeout=None,
                    fault_generation=fault_generation,
                    action="Pull the leftover filament out" if tail else None)
            finally:
                self.gcode.run_script_from_command(
                    "SET_STEPPER_ENABLE STEPPER=extruder ENABLE=1")
            if tail:
                self.box.finish_tail(source)
            else:
                self.external_released = False
                self.box.clear_hotend_feed_pending(source)
                self._clear_active_spool(source)

    def _load_external(self, gcmd, source, fault_generation, request):
        temperature = self._purge_temperature(
            source, self.box.external_slot)
        self._start_heat_home_and_wait(
            gcmd, temperature, fault_generation, request)
        self._check_abort(fault_generation)
        self._move_to_wastebin(request)
        self.box.enable_filament_sensor()
        self._op_step("insert")
        self._wait_for_sensor(
            gcmd, True, timeout=EXTERNAL_WAIT,
            fault_generation=fault_generation)
        self._check_abort(fault_generation)
        self._op_step("load", total_mm=EXTERNAL_FEED_MM)
        self._op_moving(
            0.0, EXTERNAL_FEED_MM, self.box.external_feed_velocity / 60.0)
        self._relative_extrude(
            "box_external_feed", EXTERNAL_FEED_MM, self.box.external_feed_velocity)
        self.op_progress(EXTERNAL_FEED_MM)
        self.box.mark_hotend_feed_pending(self.box.external_slot)
        return temperature

    # ------------------------------------------------------------------
    # Flush and temperature policy
    # ------------------------------------------------------------------

    def _purge_temperature(self, source, target):
        source_temp = (
            self._effective_temp(source, source=True)
            if self.box.is_valid_slot(source) else None)
        hotend = self.box.hotend_filament()
        if hotend and hotend["temperature"] is not None:
            source_temp = max(source_temp or 0, hotend["temperature"])
        target_temp = (
            self._effective_temp(target)
            if self.box.is_valid_slot(target) else None)
        if target_temp is None and source_temp is None:
            target_temp = source_temp = self.default_temp
        return max(
            value for value in (source_temp, target_temp)
            if value is not None)

    def _start_heat_home_and_wait(
            self, gcmd, temperature, fault_generation, request=None):
        self._start_heat(temperature)
        self._enter_service(request)
        self.gcode.run_script_from_command("HOME_IF_NEEDED AXIS=XY")
        self._check_abort(fault_generation)
        self._wait_for_heat(gcmd, temperature, fault_generation)

    def _start_heat(self, temperature):
        self.gcode.run_script_from_command("M104 S%d" % temperature)

    def _wait_for_heat(self, gcmd, temperature, fault_generation):
        minimum = temperature - TEMP_TOLERANCE
        heater = self.printer.lookup_object("heaters").lookup_heater("extruder")
        current, _target = heater.get_temp(self.printer.get_reactor().monotonic())
        operation = self.running_operation()
        if current < minimum or (
                operation is not None and operation.planned("heat")):
            # A planned heat step shows as done even when no wait is left.
            self._op_step("heat", temperature=temperature)
        if current < minimum:
            self._info(gcmd, "Waiting for extruder >=%dC" % minimum)
        self._wait_for_temperature(minimum, None, fault_generation)

    def _prepare_hotend(
            self, source, target, fault_generation, request,
            temperature_source=None):
        temperature = self._purge_temperature(
            source if temperature_source is None else temperature_source,
            target)
        self._start_heat(temperature)
        self._check_abort(fault_generation)
        self._move_to_wastebin(request)
        self._check_abort(fault_generation)
        return temperature

    def _reheat_after_load(self, source, target, prepared):
        """A tag read during the load can make the target need more heat."""
        if prepared is None:
            return None
        temperature = self._purge_temperature(source, target)
        if temperature <= prepared:
            return prepared
        self._start_heat(temperature)
        return temperature

    def _prepare_filament(
            self, gcmd, source, target, temperature_source=None,
            allow_purge=True, prime_reason=None, prepared_temperature=None,
            fault_generation=None, request=None, force_prime=False,
            purge_fan=None):
        """purge_fan, an ExitStack, takes over the fan guard so the last
        snap's fan keeps running until the stack closes."""
        if fault_generation is None:
            fault_generation = self.box.fault_generation
        self._check_abort(fault_generation)
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
        temperature = prepared_temperature
        if temperature is None:
            temperature = self._purge_temperature(
                source if temperature_source is None else temperature_source,
                target)
            self._move_to_wastebin(request)
            self._check_abort(fault_generation)
            self._start_heat(temperature)
        self._wait_for_heat(gcmd, temperature, fault_generation)
        self._op_step("purge", total_mm=feed + max(purge, prime))

        self.last_purge_length = purge
        feed_f = self.hotend_feed_speed / FILAMENT_AREA * 60.0
        purge_f = self.purge_speed / FILAMENT_AREA * 60.0
        prime_f = self.print_prime_speed / FILAMENT_AREA * 60.0
        guard = self.box.part_fan_override(0.0)
        fan_after = 0.0
        if purge_fan is not None:
            purge_fan.enter_context(guard)
            guard = nullcontext()
            fan_after = self.box.snap_fan_speed
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
                    self._check_abort(fault_generation, clog_epoch)
                    self._op_moving(0.0, feed, feed_f / 60.0)
                    self.gcode.run_script_from_command(
                        "G1 E%.4f F%.1f" % (feed, feed_f))
                    self.printer.lookup_object("toolhead").wait_moves()
                    self.op_progress(feed)
                    self._check_abort(fault_generation, clog_epoch)
                if purge > 0.0:
                    self._info(
                        gcmd, "Purging %.1fmm (%s)" % (purge, purge_reason))
                    self._purge_moves(
                        purge, purge_f, clog_epoch, fault_generation,
                        done=feed, fan_after=fan_after)
                elif prime > 0.0:
                    self._info(
                        gcmd, "Priming %.1fmm%s" % (
                            prime, " (%s)" % prime_reason if prime_reason else ""))
                    self._purge_moves(
                        prime, prime_f, clog_epoch, fault_generation,
                        done=feed, fan_after=fan_after)
            finally:
                self.gcode.run_script_from_command(
                    "RESTORE_GCODE_STATE NAME=box_filament_prepare MOVE=0")
        self.box.set_hotend_filament(
            target, self._effective_temp(target))
        return True

    def _purge_plan(self, source, target, allow_purge):
        if not allow_purge:
            return 0.0, None
        if self._same_slot_material_changed(source, target):
            return self.fallback_purge_length, (
                "same-slot material change in %s" % self._slot_label(target))
        if source == target:
            return 0.0, None
        if not self.box.is_valid_slot(source):
            return self.fallback_purge_length, (
                "fallback unknown -> %s" % self._slot_label(target))
        volume = self._matrix_volume(source, target)
        if volume is not None:
            # The matrix is indexed by the file's tools, not physical slots.
            return max(0.0, volume / FILAMENT_AREA), (
                "slicer matrix T%d -> T%d" % (
                    self._metadata_tool(source, source=True),
                    self._metadata_tool(target)))
        return self.fallback_purge_length, (
            "fallback %s -> %s" % (self._slot_label(source), self._slot_label(target)))

    def _purge_moves(self, distance, feedrate, clog_epoch, fault_generation,
                     done=0.0, fan_after=0.0):
        """Purge in chunks with a snap and wipe after each; returns done + distance.

        The fan stops between chunks; the last snap leaves it at fan_after."""
        chunks = int(math.ceil(distance / PURGE_CHUNK_MM))
        for index in range(chunks):
            self._check_abort(fault_generation, clog_epoch)
            amount = distance / chunks
            if index < chunks - 1:
                amount += SNAP_RETRACT_MM
            self._op_moving(done, done + distance / chunks, feedrate / 60.0)
            self.gcode.run_script_from_command(
                "G1 E%.4f F%.1f" % (amount, feedrate))
            self._check_abort(fault_generation, clog_epoch)
            self.box.flush_clean_snap(
                fan_after=fan_after if index == chunks - 1 else 0.0)
            done += distance / chunks
            self.op_progress(done)
            self._check_abort(fault_generation, clog_epoch)
        self._check_abort(fault_generation, clog_epoch)
        return done

    def _wait_for_temperature(self, minimum, maximum, fault_generation):
        heater = self.printer.lookup_object("heaters").lookup_heater("extruder")

        def waiting(eventtime):
            self._check_abort(fault_generation)
            current, _target = heater.get_temp(eventtime)
            return current < minimum or (
                maximum is not None and current > maximum)

        self.printer.wait_while(waiting, interval=TEMP_POLL)
        self._check_abort(fault_generation)

    def _wait_for_final_temperature(self, target, fault_generation, request,
                                    purge_fan=None):
        final = self._effective_temp(target)
        request.settled_temperature = final
        self._op_step("temperature", temperature=final)
        elapsed = self._wait_for_temperature_target(
            final, fault_generation, purge_fan)
        self._clean_after_temperature_wait(
            elapsed, fault_generation, request)
        self.repush_after_filament_prepare()
        self._check_abort(fault_generation)

    def _wait_for_temperature_target(self, final, fault_generation,
                                     purge_fan=None):
        """Wait for final, with the fan on to cool. Closing purge_fan puts
        back the print's fan the purge left running."""
        reactor = self.printer.get_reactor()
        started = reactor.monotonic()
        self.gcode.run_script_from_command("M104 S%d" % final)
        heater = self.printer.lookup_object("heaters").lookup_heater("extruder")
        current, _target = heater.get_temp(reactor.monotonic())
        fan = purge_fan if purge_fan is not None else ExitStack()
        with fan:
            if current > final + TEMP_TOLERANCE:
                fan.enter_context(self.box.part_fan_override(1.0))
            else:
                fan.close()  # no blowing on a nozzle that has to reheat
            self._wait_for_temperature(
                final - TEMP_TOLERANCE, final + TEMP_TOLERANCE,
                fault_generation)
        return reactor.monotonic() - started

    def _clean_after_temperature_wait(
            self, elapsed, fault_generation, request):
        if elapsed > LONG_TEMP_WAIT:
            self._check_abort(fault_generation)
            self._enter_service(request)
            self.box.nozzle_clean()
        self._check_abort(fault_generation)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _target_ready(self, target, live):
        if target == self.box.external_slot:
            return live.loaded_slot == target and bool(live.filament_detected)
        return (live.loaded_slot == target and bool(live.filament_detected)
                and bool(live.tracking))

    def _commit_loaded_slot(self, slot):
        self.external_released = False
        if slot == self.box.external_slot:
            self.box.note_external_source()
        self.box.last_loaded_slot = slot
        self.box.activate_spool(slot)

    def _remember_hotend_filament(self, slot):
        if (self.box.is_valid_slot(slot) and self.box.hotend_filament() is None
                and not self.box.hotend_feed_pending(slot)):
            self.box.set_hotend_filament(slot, self._effective_temp(slot))

    def _effective_temp(self, slot, source=False):
        if not self.box.is_valid_slot(slot):
            return self.default_temp
        if self._parsed_is_current():
            values = (
                self.temp_initial_layer
                if self._is_first_layer() else self.temp_print)
            tool = self._metadata_tool(slot, source=source)
            if values and tool is not None and 0 <= tool < len(values):
                return int(values[tool])
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

    def _metadata_tool(self, slot, source=False):
        if self.mapping_filename is None:
            return slot
        request = self.pending
        if request is not None:
            if source and slot == request.source:
                return request.source_tool
            if not source and slot == request.target:
                return request.target_tool
        return self.active_tool if slot == self.active_slot else None

    def _matrix_volume(self, source, target):
        if (not self._parsed_is_current() or self.matrix is None
                or not self.box.is_valid_slot(source)):
            return None
        if source == target:
            return 0.0
        source = self._metadata_tool(source, source=True)
        target = self._metadata_tool(target)
        if (source is None or target is None or source < 0 or target < 0
                or source >= len(self.matrix)):
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
        return epoch is not None and self.parsed_epoch == epoch

    def _clear_parsed_data(self):
        self.matrix = None
        self.temp_print = None
        self.temp_initial_layer = None
        self.parsed_epoch = None

    def _print_epoch(self):
        return self.printer.lookup_object("print_stats").print_start_time

    def _is_first_layer(self):
        layer = self.printer.lookup_object("print_stats").info_current_layer
        return layer is not None and layer <= 1

    def _print_target(self, print_context):
        if not print_context:
            return None
        captured = self.pause_resume_temperature()
        return captured if captured is not None else self._extruder_target()

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
                "G0 Z%.3f F%.0f" % (request.service_z, self.box.z_velocity))
            self.printer.lookup_object("toolhead").wait_moves()
        finally:
            if restore:
                self.gcode.run_script_from_command(
                    "RESTORE_GCODE_STATE NAME=%s MOVE=0"
                    % SERVICE_STATE)

    def _return_from_service(self, request):
        if request.return_position is None or request.service_z is None:
            return
        request.last_step = "return"
        if not self._is_print_paused():
            self._op_step("return")
        if self._is_print_paused():
            # RESUME_BASE makes the only trip back to the print.
            if request.rebase_pause:
                self._rebase_pause_state()
        else:
            self._raise_to_service_z(request)
            x, y, z = request.return_position
            toolhead = self.printer.lookup_object("toolhead")
            self.gcode.run_script_from_command("G90")
            self.gcode.run_script_from_command(
                "G0 X%.3f Y%.3f F%.0f" % (x, y, self.box.travel_velocity))
            toolhead.wait_moves()
            self.gcode.run_script_from_command(
                "G0 Z%.3f F%.0f" % (z, self.box.z_velocity))
            toolhead.wait_moves()
        self.gcode.run_script_from_command(
            "RESTORE_GCODE_STATE NAME=%s MOVE=0" % SERVICE_STATE)

    def _rebase_pause_state(self):
        # The pause landed mid-change, so PAUSE_STATE holds a service
        # position. Resume from the print state saved before the change.
        states = self.printer.lookup_object("gcode_move").saved_states
        if SERVICE_STATE not in states:
            raise RuntimeError("Saved print position is unavailable")
        states["PAUSE_STATE"] = copy.deepcopy(states[SERVICE_STATE])

    # ------------------------------------------------------------------
    # Small host actions
    # ------------------------------------------------------------------

    def _stop_for_user(self, gcmd, request, step, raise_error=False):
        request.last_error = step.reason
        if request.runout_recovery:
            self._restore_pre_box_target(request)
        if request.print_context:
            self.block_resume(step.reason, action=step.action)
            self._op_stop("waiting")
            paused = self._is_print_paused()
            if paused:
                # PAUSE cools the nozzle for a wait; this pause is older.
                self.gcode.run_script_from_command("M104 S140")
            if ((paused or not self.box.pause_print(
                    release_extruder=step.release_extruder))
                    and step.release_extruder):
                self._release_extruder()
            if raise_error:
                raise gcmd.error("[BOX]: " + self.recovery_instruction())
            self._warn(self.recovery_notice())
            return False
        # Nothing to resume outside a print: the change ends here, and
        # selecting the slot again carries on.
        self._op_end()
        self.pending = None
        if request.retracted_source is not None:
            self.gcode.run_script_from_command("M104 S0")
        if step.release_extruder:
            self._release_extruder()
        self._info(gcmd, step.idle_message)
        return False

    def _release_extruder(self):
        # Lets the user pull the filament through; the next extruder move
        # enables the motor again.
        self.gcode.run_script_from_command(
            "SET_STEPPER_ENABLE STEPPER=extruder ENABLE=0")

    def _operation_failed(
            self, gcmd, request, exc, fault_generation, raise_error=False):
        request.last_error = str(exc)
        if request.runout_recovery:
            self._restore_pre_box_target(request)
        # Retract, cut, and unload act on the outgoing slot, not the target.
        if (request.last_step in ("retract", "cut", "unload")
                and request.source is not None):
            label = self._loaded_label(request.source)
        else:
            label = self._slot_label(request.target, request.target_tool)
        message = box_protocol.format_failed(
            "%s %s" % (label, request.last_step or "change"), request.last_error)
        if request.print_context:
            self.block_resume(message)
            self._op_stop("failed")
            if not self._is_print_paused():
                self.box.pause_print()
            if raise_error:
                raise gcmd.error("[BOX]: " + self.recovery_instruction())
            self._warn(self.recovery_notice())
            return False
        # Nothing will come back for a hot nozzle outside a print.
        self._op_end()
        self.gcode.run_script_from_command("M104 S0")
        raise gcmd.error("[BOX]: %s. Heater off." % message.rstrip("."))

    def _wait_for_sensor(
            self, gcmd, detected, timeout, fault_generation, action=None):
        reactor = self.printer.get_reactor()
        deadline = None if timeout is None else reactor.monotonic() + timeout
        if action is None:
            action = ("Insert external filament" if detected
                      else "Pull external filament out")
        self._info(gcmd, action)
        while self.box.filament_detected() != detected:
            self._check_abort(fault_generation)
            now = reactor.monotonic()
            if deadline is not None and now >= deadline:
                raise RuntimeError("External filament insertion timed out")
            wake = now + SENSOR_POLL
            reactor.pause(min(wake, deadline) if deadline is not None else wake)
        self._check_abort(fault_generation)

    def _relative_extrude(self, name, distance, speed):
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=%s" % name)
        try:
            self.gcode.run_script_from_command("M83")
            self.gcode.run_script_from_command(
                "G1 E%.4f F%.0f" % (distance, speed))
            self.printer.lookup_object("toolhead").wait_moves()
        finally:
            self.gcode.run_script_from_command(
                "RESTORE_GCODE_STATE NAME=%s MOVE=0" % name)

    def _runout_slot(self):
        origin = self.box.runout_origin
        return (origin if self.box.is_valid_slot(origin)
                else self.box.last_loaded_slot)

    def _pause_runout(
            self, gcmd, reason, feed_tail=True, skip_retract_wipe=False,
            action=LOAD_FILAMENT_ACTION):
        target = (self.pending.target if self.pending is not None
                  else self._runout_slot())
        automatic = self.box.is_valid_slot(target)
        if not automatic:
            # RESUME has no slot to retry; only a Box load clears this.
            action = LOAD_SLOT_ACTION
        self.block_resume(
            reason,
            target=target if automatic else None,
            automatic=automatic, action=action)
        self._warn(self.recovery_notice())
        if not self.box.pause_print(
                synchronous=True, skip_retract_wipe=skip_retract_wipe):
            raise RuntimeError("PAUSE did not settle before runout recovery")
        if feed_tail:
            self._feed_runout_tail(gcmd)
        return False

    def _check_abort(self, fault_generation, clog_epoch=None):
        try:
            self.box.check_operation_abort(fault_generation)
            if (clog_epoch is not None
                    and self.box.clog_event_count != clog_epoch):
                raise RuntimeError("clog detected during filament preparation")
        except Exception as exc:
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
        self._relative_extrude(
            "box_runout_tail", distance, self.box.external_feed_velocity)

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
        for line in str(message).splitlines():
            self.gcode.respond_raw("!! [BOX]: " + line)
