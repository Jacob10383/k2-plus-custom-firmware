# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Pre-BedMesh extended-zone routing transform.

This module sits above BedMesh in the gcode_move transform chain and applies
extended-zone safety routing on unsplit moves. BedMesh then applies mesh
compensation to each routed segment.
"""

import logging
import math


_COORD_EPSILON = 1.0e-9


def _klog(msg, *args, level=logging.info):
    level("extended_zone_transform: " + msg, *args)


class ExtendedZoneTransform:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.reactor = self.printer.get_reactor()

        self.standard_y_max = config.getfloat("standard_y_max", 352.0)
        self.extended_y_max = config.getfloat("extended_y_max", 380.0)
        self.safe_x_min = config.getfloat("safe_x_min", 122.0)
        self.safe_x_max = config.getfloat("safe_x_max", 230.0)
        # compat: old cfgs may still set this
        config.getboolean("debug", False)

        if self.extended_y_max < self.standard_y_max:
            raise config.error(
                "extended_zone_transform: extended_y_max must be >= standard_y_max"
            )
        if self.safe_x_min > self.safe_x_max:
            raise config.error(
                "extended_zone_transform: safe_x_min must be <= safe_x_max"
            )

        self.toolhead = None
        self.next_transform = None
        self.last_position = [0.0, 0.0, 0.0, 0.0]
        self._installed = False
        self._active_y_max = None
        self._arc_handlers_installed = False
        self._arc_plane = "xy"

        self.gcode.register_command(
            "EXTENDED_ZONE_TRANSFORM_STATUS",
            self.cmd_EXTENDED_ZONE_TRANSFORM_STATUS,
            desc=self.cmd_EXTENDED_ZONE_TRANSFORM_STATUS_help,
        )

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        self._install_transform_chain()
        self._install_arc_handlers()
        self._apply_y_envelope(self.standard_y_max)

    def _install_transform_chain(self):
        if self._installed and self.next_transform is not None:
            return
        gcode_move = self.printer.lookup_object("gcode_move")
        old_transform = gcode_move.set_move_transform(self, force=True)
        if old_transform is self and self.next_transform is not None:
            self._installed = True
            return
        self.next_transform = old_transform
        self.last_position = list(self.next_transform.get_position())
        self._installed = True
        _klog(
            "installed above %s",
            self.next_transform.__class__.__module__
            + "."
            + self.next_transform.__class__.__name__,
        )

    def _is_safe_x(self, x):
        return (
            self.safe_x_min - _COORD_EPSILON
            <= x
            <= self.safe_x_max + _COORD_EPSILON
        )

    def _is_extended_y(self, y):
        return y > self.standard_y_max + _COORD_EPSILON

    def _apply_y_envelope(self, desired_y_max):
        if self._active_y_max is not None and abs(self._active_y_max - desired_y_max) < _COORD_EPSILON:
            return
        kin = self.toolhead.kin
        y_rail = kin.rails[1]
        y_rail.position_max = desired_y_max
        kin.axes_max = self.toolhead.Coord(
            kin.axes_max.x,
            desired_y_max,
            kin.axes_max.z,
            kin.axes_max.e,
        )

        curtime = self.reactor.monotonic()
        kin_status = kin.get_status(curtime) or {}
        homed_axes = kin_status.get("homed_axes", [])
        x_is_homed = "x" in homed_axes
        y_is_homed = "y" in homed_axes
        if x_is_homed and y_is_homed:
            min_y, _ = kin.limits[1]
            kin.limits[1] = (min_y, desired_y_max)

        self._active_y_max = desired_y_max
        _klog("y envelope switched to %.1f", desired_y_max)

    def _check_homed_xy(self):
        curtime = self.reactor.monotonic()
        kin_status = self.toolhead.kin.get_status(curtime)
        homed_axes = kin_status.get("homed_axes", [])
        x_is_homed = "x" in homed_axes
        y_is_homed = "y" in homed_axes
        if x_is_homed and y_is_homed:
            return
        raise self.gcode.error(
            "Move out of range: Cannot enter/move in extended Y zone (Y > %.1f) "
            "without homing X and Y axes." % (self.standard_y_max)
        )

    def _plan_move_segments(self, current, target):
        standard_y_max = self.standard_y_max
        current_x, current_y = current[0], current[1]
        target_x, target_y = target[0], target[1]

        special_state = getattr(self.toolhead, "special_queuing_state", None)
        if not (self._is_extended_y(current_y) or self._is_extended_y(target_y)) \
                or special_state == "Drip":
            return [target]

        if abs(current_x - target_x) < _COORD_EPSILON and abs(current_y - target_y) < _COORD_EPSILON:
            return [target]

        self._check_homed_xy()

        if self._is_extended_y(current_y) and not self._is_safe_x(current_x):
            raise self.gcode.error(
                "Move out of range: Current position (X=%.1f, Y=%.1f) is in an invalid state. "
                "X must be between %.1f-%.1f when Y > %.1f. "
                "Homing recommended to recover."
                % (
                    current_x,
                    current_y,
                    self.safe_x_min,
                    self.safe_x_max,
                    standard_y_max,
                )
            )

        if self._is_extended_y(target_y) and not self._is_safe_x(target_x):
            raise self.gcode.error(
                "Move out of range: Target (X=%.1f, Y=%.1f) is outside safe extended zone "
                "(Safe X: %.1f-%.1f)"
                % (target_x, target_y, self.safe_x_min, self.safe_x_max)
            )

        if self._is_extended_y(current_y) and self._is_extended_y(target_y):
            return [target]

        y_diff = target_y - current_y
        if abs(y_diff) < _COORD_EPSILON:
            intersect_x = current_x
        else:
            intersect_x = (
                current_x
                + (target_x - current_x) * (standard_y_max - current_y) / y_diff
            )

        if self._is_safe_x(intersect_x):
            return [target]

        extrude_delta = target[3] - current[3]
        is_travel_move = abs(extrude_delta) < _COORD_EPSILON
        is_retraction_move = extrude_delta < -_COORD_EPSILON
        if not (is_travel_move or is_retraction_move):
            raise self.gcode.error(
                "Move out of range: Path crosses boundary Y=%.1f at X=%.1f, which is unsafe.\n"
                "  Current pos: X=%.3f Y=%.3f Z=%.3f E=%.5f\n"
                "  Target pos:  X=%.3f Y=%.3f Z=%.3f E=%.5f\n"
                "  Extrusion delta: %.5f (positive extrusion, cannot auto-recover)\n"
                "  Safe X corridor at Y=%.1f boundary: %.1f-%.1f\n"
                "  Intersection X: %.3f"
                % (
                    standard_y_max,
                    intersect_x,
                    current[0],
                    current[1],
                    current[2],
                    current[3],
                    target[0],
                    target[1],
                    target[2],
                    target[3],
                    extrude_delta,
                    standard_y_max,
                    self.safe_x_min,
                    self.safe_x_max,
                    intersect_x,
                )
            )

        mid = list(target)
        if self._is_extended_y(current_y):
            # Exit extended zone at a safe X before continuing.
            mid[:2] = [current_x, standard_y_max]
        else:
            # Enter extended zone only after X is already in safe corridor.
            mid[:2] = [target_x, current_y]

        _klog(
            "routing move via midpoint %s (current=%s target=%s intersect_x=%.3f)",
            mid,
            current,
            target,
            intersect_x,
        )
        return [mid, target]

    def _route(self, current, target):
        """Plan the segments for a move and open/close the Y envelope for it."""
        segments = self._plan_move_segments(current, target)
        desired_y_max = (
            self.extended_y_max
            if self._is_extended_y(current[1]) or self._is_extended_y(target[1])
            else self.standard_y_max
        )
        self._apply_y_envelope(desired_y_max)
        return segments

    def move(self, newpos, speed):
        if self.next_transform is None:
            self._install_transform_chain()
        if self.next_transform is None:
            raise self.gcode.error("extended_zone_transform: transform chain not initialized")

        target = list(newpos)
        current = list(self.last_position)

        segments = self._route(current, target)
        for idx, segment in enumerate(segments):
            try:
                self.next_transform.move(segment, speed)
            except self.printer.command_error:
                self._log_move_failure_context(current, target, segments, idx)
                raise
        self.last_position[:] = target

    def manual_move(self, coord, speed):
        """Extended-zone aware replacement for toolhead.manual_move().

        Toolhead-space moves never reach this transform, so they would
        otherwise miss the safe-X checks, midpoint routing and envelope
        switching that G0/G1 moves get. Bed mesh compensation is still
        skipped, as it is for any toolhead-space move.
        """
        if self.toolhead is None:
            self.toolhead = self.printer.lookup_object("toolhead")
        toolhead = self.toolhead

        current = list(toolhead.get_position())
        target = list(current)
        for axis, value in enumerate(coord):
            if value is not None:
                target[axis] = value

        segments = self._route(current, target)
        for idx, segment in enumerate(segments):
            try:
                toolhead.manual_move(segment, speed)
            except self.printer.command_error:
                self._log_move_failure_context(current, target, segments, idx)
                raise

    def _log_move_failure_context(self, current, target, segments, failed_idx):
        """Dump gcode_move offset/mode state and zone config when a downstream
        move is rejected. Helps identify G91 leaks or stale SET_GCODE_OFFSET
        when the bare 'Move out of range' message isn't enough on its own.
        """
        try:
            gcode_move = self.printer.lookup_object("gcode_move", None)
            kin_y_limits = None
            if self.toolhead is not None:
                try:
                    kin_y_limits = self.toolhead.kin.limits[1]
                except Exception:
                    kin_y_limits = None
            gm_state = "<unavailable>"
            if gcode_move is not None:
                try:
                    status = gcode_move.get_status()
                    gm_state = (
                        "abs_coord=%s abs_extrude=%s "
                        "homing_origin=%s gcode_position=%s position=%s"
                        % (
                            status.get("absolute_coordinates"),
                            status.get("absolute_extrude"),
                            tuple(status.get("homing_origin", ())),
                            tuple(status.get("gcode_position", ())),
                            tuple(status.get("position", ())),
                        )
                    )
                except Exception as e:
                    gm_state = "<status failed: %s>" % e
            lines = [
                "downstream move rejected"
                " (segment %d/%d)" % (failed_idx + 1, len(segments)),
                "  current=[%.3f, %.3f, %.3f, %.3f]" % tuple(current[:4]),
                "  target =[%.3f, %.3f, %.3f, %.3f]" % tuple(target[:4]),
                "  segment=[%.3f, %.3f, %.3f, %.3f]"
                % tuple(segments[failed_idx][:4]),
                "  zone: standard_y_max=%.1f extended_y_max=%.1f"
                " safe_x=%.1f-%.1f active_envelope=%s"
                % (
                    self.standard_y_max,
                    self.extended_y_max,
                    self.safe_x_min,
                    self.safe_x_max,
                    self._active_y_max,
                ),
                "  kin_y_limits=%s" % (kin_y_limits,),
                "  gcode_move: %s" % gm_state,
            ]
            msg = "\n".join(lines)
            _klog(msg, level=logging.warning)
            response_msg = "extended_zone_transform: " + msg
            try:
                self.gcode.respond_info(response_msg)
            except Exception:
                pass
        except Exception as diag_err:
            _klog(
                "diagnostic logger failed: %s",
                diag_err,
                level=logging.warning,
            )

    def _install_arc_handlers(self):
        if self._arc_handlers_installed:
            return
        self._arc_handlers_installed = True
        for cmd, clockwise in (("G2", True), ("G3", False)):
            self._wrap_gcode_handler(
                cmd,
                lambda original, cmd=cmd, clockwise=clockwise:
                    self._make_arc_handler(cmd, clockwise, original),
            )
        for cmd, plane in (("G17", "xy"), ("G18", "xz"), ("G19", "yz")):
            self._wrap_gcode_handler(
                cmd,
                lambda original, plane=plane: self._make_plane_handler(plane, original),
            )

    def _wrap_gcode_handler(self, cmd, wrapper_factory):
        try:
            handlers = self.gcode.ready_gcode_handlers
            original = handlers.get(cmd)
            if original is None:
                return
            wrapper = wrapper_factory(original)
            handlers[cmd] = wrapper

            active_handlers = getattr(self.gcode, "gcode_handlers", None)
            if (
                active_handlers is not None
                and active_handlers is not handlers
                and active_handlers.get(cmd) is original
            ):
                active_handlers[cmd] = wrapper
        except Exception as e:
            _klog(
                "could not wrap %s handler for arc sanitizing: %s",
                cmd,
                e,
                level=logging.warning,
            )

    def _make_plane_handler(self, plane, original):
        def _handler(gcmd):
            original(gcmd)
            self._arc_plane = plane
        return _handler

    def _make_arc_handler(self, cmd, _clockwise, original):
        def _handler(gcmd):
            try:
                if self._should_sanitize_no_xy_helical_arc(gcmd):
                    self._run_sanitized_arc_lift(gcmd)
                    return
            except Exception as e:
                _klog(
                    "%s sanitizer failed; passing original command through: %s",
                    cmd,
                    e,
                    level=logging.warning,
                )
            original(gcmd)
        return _handler

    def _should_sanitize_no_xy_helical_arc(self, gcmd):
        if self._arc_plane != "xy":
            return False
        params = gcmd.get_command_parameters()
        if "X" in params or "Y" in params or "E" in params:
            return False
        if "Z" not in params or "R" in params:
            return False

        offset_x = self._get_float_param(params, "I", 0.0)
        offset_y = self._get_float_param(params, "J", 0.0)
        if abs(offset_x) < _COORD_EPSILON and abs(offset_y) < _COORD_EPSILON:
            return False

        if "F" in params:
            self._get_float_param(params, "F")

        gcode_move = self.printer.lookup_object("gcode_move", None)
        if gcode_move is None:
            return False
        status = gcode_move.get_status()
        if not status.get("absolute_coordinates", True):
            return False
        current = tuple(status.get("gcode_position", ()))
        if len(current) < 3:
            return False

        current_x = float(current[0])
        current_y = float(current[1])
        current_z = float(current[2])
        target_z = self._get_float_param(params, "Z")
        if abs(target_z - current_z) < _COORD_EPSILON:
            return False
        if not self._is_extended_y(current_y):
            return False
        if current_y > self.extended_y_max + _COORD_EPSILON:
            return False
        if not self._is_safe_x(current_x):
            return False

        radius = math.hypot(offset_x, offset_y)
        arc_y_max = current_y + offset_y + radius
        return arc_y_max > self.extended_y_max + _COORD_EPSILON

    def _get_float_param(self, params, key, default=None):
        value = params.get(key)
        if value is None:
            if default is None:
                raise ValueError("missing %s" % key)
            return default
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("%s is not a valid float" % value)
        return result

    def _run_sanitized_arc_lift(self, gcmd):
        gcode_move = self.printer.lookup_object("gcode_move", None)
        if gcode_move is None:
            raise ValueError("gcode_move unavailable")
        params = gcmd.get_command_parameters()
        g1_params = {"Z": self._get_float_param(params, "Z")}
        command = "G1 Z%s" % g1_params["Z"]
        if "F" in params:
            g1_params["F"] = self._get_float_param(params, "F")
            command += " F%s" % g1_params["F"]
        _klog(
            "sanitizing no-XY helical %s to %s in extended safe zone",
            gcmd.get_command(),
            command,
        )

        last_position = list(gcode_move.last_position)
        speed = gcode_move.speed
        try:
            g1_gcmd = self.gcode.create_gcode_command("G1", "G1", g1_params)
            gcode_move.cmd_G1(g1_gcmd)
        except Exception:
            gcode_move.last_position[:] = last_position
            gcode_move.speed = speed
            raise

    def get_position(self):
        if self.next_transform is None:
            toolhead = self.printer.lookup_object("toolhead", None)
            pos = [0.0, 0.0, 0.0, 0.0] if toolhead is None else list(toolhead.get_position())
        else:
            pos = list(self.next_transform.get_position())
        self.last_position[:] = pos
        return pos

    cmd_EXTENDED_ZONE_TRANSFORM_STATUS_help = "Show extended-zone transform status"

    def cmd_EXTENDED_ZONE_TRANSFORM_STATUS(self, gcmd):
        chain = "uninitialized"
        if self.next_transform is not None:
            chain = (
                self.next_transform.__class__.__module__
                + "."
                + self.next_transform.__class__.__name__
            )
        gcmd.respond_info(
            "extended_zone_transform: active y_max=%.1f standard_y_max=%.1f extended_y_max=%.1f safe_x=%.1f-%.1f chain_next=%s"
            % (
                self._active_y_max if self._active_y_max is not None else self.standard_y_max,
                self.standard_y_max,
                self.extended_y_max,
                self.safe_x_min,
                self.safe_x_max,
                chain,
            )
        )


def load_config(config):
    return ExtendedZoneTransform(config)
