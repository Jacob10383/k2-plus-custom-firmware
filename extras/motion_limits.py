# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Shared helpers for saving/restoring Klipper motion limits."""

_saved_motion_limits = {}


def snapshot_motion_limits(printer):
    """Read current global motion limits from toolhead status."""
    toolhead = printer.lookup_object("toolhead", None)
    if toolhead is None:
        return None
    status = toolhead.get_status(printer.get_reactor().monotonic())
    if (
        "max_velocity" not in status
        or status["max_velocity"] is None
        or "max_accel" not in status
        or status["max_accel"] is None
        or "square_corner_velocity" not in status
        or status["square_corner_velocity"] is None
    ):
        return None
    minimum_cruise_ratio = status.get("minimum_cruise_ratio")
    if minimum_cruise_ratio is None:
        return None
    return {
        "max_velocity": status["max_velocity"],
        "max_accel": status["max_accel"],
        "minimum_cruise_ratio": minimum_cruise_ratio,
        "square_corner_velocity": status["square_corner_velocity"],
    }


def save_motion_limits(printer, gcode, name, include_gcode=False):
    """Save current motion limits under name, optionally also save gcode state."""
    limits = snapshot_motion_limits(printer)
    if limits is None:
        raise RuntimeError("Unable to snapshot current motion limits")
    _saved_motion_limits[name] = limits
    if include_gcode:
        gcode.run_script_from_command("SAVE_GCODE_STATE NAME=%s" % name)


def restore_motion_limits(gcode, name, include_gcode=False, move=0, clear=True):
    """Restore previously saved motion limits by name, optionally restore gcode state."""
    limits = _saved_motion_limits.get(name)
    if limits is None:
        raise RuntimeError("No saved motion limits for NAME=%s" % name)
    gcode.run_script_from_command(
        "SET_VELOCITY_LIMIT VELOCITY=%g ACCEL=%g MINIMUM_CRUISE_RATIO=%g SQUARE_CORNER_VELOCITY=%g"
        % (
            limits["max_velocity"],
            limits["max_accel"],
            limits["minimum_cruise_ratio"],
            limits["square_corner_velocity"],
        )
    )
    if include_gcode:
        gcode.run_script_from_command(
            "RESTORE_GCODE_STATE NAME=%s MOVE=%d" % (name, 1 if int(move) else 0)
        )
    if clear:
        _saved_motion_limits.pop(name, None)
class MotionLimits:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command(
            "SAVE_MOTION_LIMITS", self.cmd_SAVE_MOTION_LIMITS,
            desc="Save current velocity/accel limits"
        )
        self.gcode.register_command(
            "RESTORE_MOTION_LIMITS", self.cmd_RESTORE_MOTION_LIMITS,
            desc="Restore previously saved velocity/accel limits"
        )

    def cmd_SAVE_MOTION_LIMITS(self, gcmd):
        name = gcmd.get("NAME", "default")
        include_gcode = gcmd.get_int("INCLUDE_GCODE", 0) == 1
        save_motion_limits(self.printer, self.gcode, name, include_gcode=include_gcode)

    def cmd_RESTORE_MOTION_LIMITS(self, gcmd):
        name = gcmd.get("NAME", "default")
        include_gcode = gcmd.get_int("INCLUDE_GCODE", 0) == 1
        move = gcmd.get_int("MOVE", 0)
        restore_motion_limits(self.gcode, name, include_gcode=include_gcode, move=move)


def load_config(config):
    return MotionLimits(config)
