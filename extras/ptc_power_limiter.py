# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
# Limit chamber PTC output against bed heater load.
#
# This module intentionally wraps a configured chamber heater's final PWM
# command instead of modifying core heater control code.
import logging


def _klog(msg, *args, level=logging.info):
    level("ptc_power_limiter: " + msg, *args)


class PTCPowerLimiter:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.bed_name = config.get("bed", "heater_bed")
        self.chamber_name = config.get("chamber", "heater_generic chamber_heater")
        self.enabled = config.getboolean("enabled", True)
        self.bed_full_load = config.getfloat("bed_full_load", 1.0, above=0.0)
        self.chamber_full_load = config.getfloat("chamber_full_load", 0.35,
                                                 above=0.0)
        self.max_combined_load = config.getfloat("max_combined_load", 1.0,
                                                 above=0.0)
        self.minimum_useful_chamber_power = config.getfloat(
            "minimum_useful_chamber_power", 0.20, minval=0.0, maxval=1.0
        )
        self.debug = config.getboolean("debug", False)
        self.bed = self.chamber = self._original_set_pwm = None
        self.last_cap = 1.0
        self.last_bed_power = 0.0
        self.last_requested = 0.0
        self.last_applied = 0.0
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        self.bed = self.printer.lookup_object(self.bed_name)
        self.chamber = self.printer.lookup_object(self.chamber_name)
        self._original_set_pwm = self.chamber.set_pwm
        self.chamber.set_pwm = self._limited_set_pwm
        _klog(
            "%s: limiting %s against %s, max_combined_load=%.3f, "
            "bed_full_load=%.3f, chamber_full_load=%.3f",
            self.name, self.chamber_name, self.bed_name,
            self.max_combined_load, self.bed_full_load, self.chamber_full_load,
        )

    def _limited_set_pwm(self, read_time, value):
        cap = 1.0
        bed_power = 0.0
        if self.enabled:
            status = self.bed.get_status(read_time)
            bed_power = max(0.0, min(1.0, status.get("power", 0.0)))
            available_load = self.max_combined_load - bed_power * self.bed_full_load
            cap = max(0.0, min(1.0, available_load / self.chamber_full_load))
            if 0.0 < cap < self.minimum_useful_chamber_power:
                cap = 0.0
        applied = max(0.0, min(value, cap))
        self.last_cap = cap
        self.last_bed_power = bed_power
        self.last_requested = value
        self.last_applied = applied
        if self.debug and value != applied:
            _klog(
                "%s: requested=%.3f applied=%.3f cap=%.3f bed_power=%.3f",
                self.name, value, applied, cap, bed_power,
            )
        self._original_set_pwm(read_time, applied)

    def get_status(self, eventtime):
        return {
            "enabled": self.enabled,
            "cap": self.last_cap,
            "bed_power": self.last_bed_power,
            "requested_power": self.last_requested,
            "applied_power": self.last_applied,
            "max_combined_load": self.max_combined_load,
            "bed_full_load": self.bed_full_load,
            "chamber_full_load": self.chamber_full_load,
        }


def load_config(config):
    return PTCPowerLimiter(config)
