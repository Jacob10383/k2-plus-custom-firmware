# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
# Adaptive purge line in front of (or beside) the printed objects.
#
# LINE_PURGE draws a short purge line next to the objects' bounding box,
# retracts, and breaks the string. The retract is repaid on the first move
# that extrudes while moving in X or Y, i.e. in place at the start of the
# print, after the travel to it. Until then G0/G1/G2/G3 are wrapped; the
# wrappers remove themselves on that move.
#
# [line_purge]
# purge_height: 0.6    # Nozzle Z while purging, mm.
# purge_margin: 35     # Gap between the objects and the purge line, mm.
# purge_amount: 25     # Line length and filament purged, mm.
# flow_rate: 12        # Purge speed setting (line speed = flow_rate / 5 mm/s).
# tip_distance: 0      # Filament pushed before the line starts, mm.
# retract_length: 0.5  # Retract after the line, repaid on the first print move, mm.
# retract_speed: 35    # Retract and repay speed, mm/s.
import logging

MOVE_COMMANDS = ("G0", "G1", "G2", "G3")
STRING_BREAK = 10.0
EPSILON = 1.0e-6
ROUNDING = 0.001


def _klog(msg, *args, level=logging.info):
    level("line_purge: " + msg, *args)


class LinePurge:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.purge_height = config.getfloat("purge_height", 0.6, above=0.0)
        self.purge_margin = config.getfloat("purge_margin", 35.0, minval=0.0)
        self.purge_amount = config.getfloat("purge_amount", 25.0, above=0.0)
        self.flow_rate = config.getfloat("flow_rate", 12.0, above=0.0)
        self.tip_distance = config.getfloat("tip_distance", 0.0, minval=0.0)
        self.retract_length = config.getfloat(
            "retract_length", 0.5, minval=0.0)
        self.retract_speed = config.getfloat("retract_speed", 35.0, above=0.0)
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode_move = None
        self.toolhead = None
        self.owed = 0.0
        self._originals = {}
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        for event in ("print_stats:complete_printing",
                      "print_stats:cancelled_printing",
                      "print_stats:error_printing",
                      "print_stats:paused_printing",
                      "virtual_sdcard:reset_file",
                      "klippy:shutdown"):
            self.printer.register_event_handler(event, self._drop)

    def _handle_ready(self):
        self.gcode_move = self.printer.lookup_object("gcode_move")
        self.toolhead = self.printer.lookup_object("toolhead")
        # After every macro has registered: an old LINE_PURGE macro (KAMP)
        # gives way to this one.
        previous = self.gcode.register_command("LINE_PURGE", None)
        if previous is not None:
            _klog("replaced an existing LINE_PURGE; [include kamp.cfg] is no "
                  "longer needed", level=logging.warning)
        self.gcode.register_command(
            "LINE_PURGE", self.cmd_LINE_PURGE, desc=self.cmd_LINE_PURGE_help)

    # Purge line
    def _objects_box(self):
        exclude = self.printer.lookup_object("exclude_object", None)
        # enable_exclude_object: False leaves the object without its lists.
        objects = getattr(exclude, "objects", None) or []
        points = [p for obj in objects for p in obj.get("polygon") or ()]
        if not points:
            return None
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        return min(xs), max(xs), min(ys), max(ys)

    def _limits(self):
        """Usable bed in G-code coordinates: (x_lo, x_hi, y_lo, y_hi)."""
        eventtime = self.printer.get_reactor().monotonic()
        status = self.toolhead.get_status(eventtime)
        x_min, y_min = status["axis_minimum"][0], status["axis_minimum"][1]
        x_max, y_max = status["axis_maximum"][0], status["axis_maximum"][1]
        zone = self.printer.lookup_object("extended_zone_transform", None)
        if zone is not None:
            y_max = min(y_max, float(zone.standard_y_max))
        # Machine minus G-code position: G92 and SET_GCODE_OFFSET alike.
        move = self.gcode_move.get_status(eventtime)
        dx = move["position"][0] - move["gcode_position"][0]
        dy = move["position"][1] - move["gcode_position"][1]
        # Emitted coordinates are rounded to 0.001; keep them inside.
        return (max(0.0, x_min) - dx + ROUNDING, x_max - dx - ROUNDING,
                max(0.0, y_min) - dy + ROUNDING, y_max - dy - ROUNDING)

    def plan(self):
        """Purge line start and direction (x, y, axis), or None if no side
        of the objects has room for the line and its string break."""
        x_lo, x_hi, y_lo, y_hi = self._limits()
        span = self.purge_amount + STRING_BREAK
        if x_hi - x_lo < span or y_hi - y_lo < span:
            return None
        box = self._objects_box()
        if box is None:
            return x_lo, y_lo, "Y"
        x_min, x_max, y_min, y_max = box
        margin = self.purge_margin

        def along_x(y):
            x = (x_min + x_max) / 2.0 - self.purge_amount / 2.0
            return min(max(x, x_lo), x_hi - span), y, "X"

        def along_y(x):
            y = (y_min + y_max) / 2.0 - self.purge_amount / 2.0
            return x, min(max(y, y_lo), y_hi - span), "Y"

        # In front, then left, then behind, then right of the objects.
        if y_lo <= y_min - margin <= y_hi:
            return along_x(y_min - margin)
        if x_lo <= x_min - margin <= x_hi:
            return along_y(x_min - margin)
        if y_lo <= y_max + margin <= y_hi:
            return along_x(y_max + margin)
        if x_lo <= x_max + margin <= x_hi:
            return along_y(x_max + margin)
        return None

    def script(self, x, y, axis):
        travel = float(self.toolhead.get_status(
            self.printer.get_reactor().monotonic())["max_velocity"]) * 60.0
        purge_speed = self.flow_rate / 5.0 * 60.0
        start = x if axis == "X" else y
        lines = [
            "G90",
            "G0 X%.3f Y%.3f F%.1f" % (x, y, travel),
            "G0 Z%.3f" % (self.purge_height,),
            "M83",
        ]
        if self.tip_distance > 0.0:
            lines.append("G1 E%.3f F%.1f" % (self.tip_distance, purge_speed))
        # Extrude exactly the emitted travel (1 mm of filament per mm).
        start, end = round(start, 3), round(start + self.purge_amount, 3)
        lines.append("G1 %s%.3f E%.3f F%.1f" % (
            axis, end, end - start, purge_speed))
        if self.retract_length > 0.0:
            lines.append("G1 E-%.3f F%.1f" % (
                self.retract_length, self.retract_speed * 60.0))
        lines += [
            "G0 %s%.3f F%.1f" % (
                axis, start + self.purge_amount + STRING_BREAK, travel),
            "G0 Z%.3f" % (self.purge_height * 2.0,),
        ]
        return "\n".join(lines)

    def _run_saved(self, name, script):
        # Restore the slicer's modes even if a move is rejected part way.
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=" + name)
        try:
            self.gcode.run_script_from_command(script)
        finally:
            self.gcode.run_script_from_command(
                "RESTORE_GCODE_STATE NAME=" + name)

    cmd_LINE_PURGE_help = "Purge a line next to the printed objects"

    def cmd_LINE_PURGE(self, gcmd):
        # A purge that was never repaid stays owed; the new one adds to it.
        owed = self._unwrap()
        extruder = self.toolhead.get_extruder()
        # The line extrudes 1 mm of filament per mm travelled.
        flow = float(self.gcode_move.get_status()["extrude_factor"])
        if getattr(extruder, "max_extrude_ratio", flow) < flow:
            gcmd.respond_info("LINE_PURGE: max_extrude_cross_section is too "
                              "small for the purge line; purge skipped.")
            self._arm(owed)
            return
        plan = self.plan()
        if plan is None:
            gcmd.respond_info("LINE_PURGE: no room for the purge line next "
                              "to the objects; purge skipped.")
            self._arm(owed)
            return
        x, y, axis = plan
        if self._objects_box() is None:
            gcmd.respond_info(
                "LINE_PURGE: no labelled objects; purging at the front-left "
                "corner. Turn on Label objects in the slicer.")
        gcmd.respond_info("LINE_PURGE: %.0f mm along %s from X%.1f Y%.1f" % (
            self.purge_amount, axis, x, y))
        try:
            self._run_saved("_LINE_PURGE", self.script(x, y, axis))
        except Exception:
            self._arm(owed)
            raise
        self._arm(owed + self.retract_length)

    # Deferred unretract
    def _arm(self, length):
        if length <= 0.0:
            return
        self.owed = length
        for cmd in MOVE_COMMANDS:
            original = self.gcode.register_command(cmd, None)
            if original is None:
                continue
            self._originals[cmd] = original
            self.gcode.register_command(cmd, self._wrapper(cmd, original))

    def _unwrap(self):
        owed, self.owed = self.owed, 0.0
        originals, self._originals = self._originals, {}
        for cmd, original in originals.items():
            self.gcode.register_command(cmd, None)
            self.gcode.register_command(cmd, original)
        return owed

    def _drop(self, *_args):
        owed = self._unwrap()
        if owed:
            _klog("print ended before the purge retract was repaid")

    def _wrapper(self, cmd, original):
        def handler(gcmd):
            if self._extrudes_in_xy(cmd, gcmd):
                owed = self._unwrap()
                try:
                    self._run_saved("_LINE_PURGE_REPAY", "M83\nG1 E%.3f F%.1f"
                                    % (owed, self.retract_speed * 60.0))
                except Exception:
                    self._arm(owed)
                    raise
            original(gcmd)
        return handler

    def _extrudes_in_xy(self, cmd, gcmd):
        params = gcmd.get_command_parameters()
        if "E" not in params:
            return False
        status = self.gcode_move.get_status()
        absolute = status["absolute_coordinates"]
        current = status["gcode_position"]
        try:
            e = float(params["E"])
            if cmd in ("G0", "G1"):
                moved = False
                for index, axis in ((0, "X"), (1, "Y")):
                    if axis in params:
                        delta = float(params[axis])
                        if absolute:
                            delta -= float(current[index])
                        moved = moved or abs(delta) > EPSILON
                if not moved:
                    return False
        except ValueError:
            return False
        if absolute and status["absolute_extrude"]:
            e -= float(current[3])
        return e > EPSILON

    def get_status(self, eventtime=None):
        return {"unretract_owed": self.owed}


def load_config(config):
    return LinePurge(config)
