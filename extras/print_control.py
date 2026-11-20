# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Per-print options, the heat soak, and keeping the printer heated after a
print.

A client lists the options from printer.print_control.options, draws a
control for each and sends the choices with PRINT_OPTIONS just before the
command that starts the print. The choices wait for the next file to load and
then last for that print, in printer.print_control.active.

Two options are built in and acted on here, by id. A [print_option <id>]
section changes what it sets of a built-in option, or adds an option for the
macros or its own module to read from active; enabled: False removes one.

- soak: START_PRINT calls HEAT_SOAK, which uses the chosen time. The wait holds
  the G-code lock, so no G-code command can reach it; clients change it
  through the print_control/soak endpoint, which runs outside that lock.
- keep_heated: the macros end a print with PRINT_HEATERS_OFF KEEP=1, which
  leaves the bed and chamber on for the chosen time. A heater whose target is
  changed meanwhile, also to off, is no longer kept: it stays as it was set.
"""

import logging
import math
import re

MAX_SOAK_MINUTES = 240.
# The heaters keep_heated leaves on, with the names used in messages.
KEPT_HEATERS = (("heater_bed", "bed"),
                ("heater_generic chamber_heater", "chamber"))
# M141 S0 also ends chamber temperature control by the exhaust fans.
HEATERS_OFF_SCRIPT = "TURN_OFF_HEATERS\nM141 S0"
# How often kept heaters are checked for a changed target and the time left.
KEEP_CHECK_SECONDS = 1.
# PRINT_OPTIONS and the start command are sent as one script, so the choices
# only need to outlast the gap between two of its lines.
PENDING_SECONDS = 30.

TYPES = ("switch", "minutes")
ID_PATTERN = re.compile(r"[a-z][a-z0-9_]*$")
# The options every printer offers, in this order. A [print_option <id>]
# section with the same id changes only what it sets.
BUILT_IN_OPTIONS = (
    ("soak", {
        "label": "Heat soak", "section": "Before printing",
        "type": "minutes", "max": 240, "step": 5,
        "default_variable": "_START_PRINT_VARS.heat_soak"}),
    ("keep_heated", {
        "label": "Keep bed and chamber heated", "section": "After printing",
        "type": "minutes", "optional": True, "value_label": "Turn off after",
        "min": 1, "max": 240, "step": 15, "default": 30.}),
)


class PrintOption:
    def __init__(self, printer, option_id, config=None, built_in=None):
        """config is the option's section, if any; built_in its built-in
        values, which the section's own values replace."""
        self.printer = printer
        self.id = option_id
        if config is not None and not ID_PATTERN.match(self.id):
            raise config.error(
                "[%s]: the name after print_option must be letters, digits "
                "and '_', starting with a letter" % (config.get_name(),))
        built_in = built_in or {}
        # A section that sets its own default replaces the built-in macro
        # variable too, or its default would only be a fallback.
        if config is not None and "default_variable" in built_in \
                and config.get("default", None, note_valid=False) is not None:
            built_in = {key: value for key, value in built_in.items()
                        if key != "default_variable"}

        def read(getter, key, *default, **limits):
            if key in built_in:
                default = (built_in[key],)
            if config is None:
                return default[0]
            return getattr(config, getter)(key, *default, **limits)

        # A section can remove an option, built in or not.
        self.enabled = read("getboolean", "enabled", True)
        if not self.enabled:
            # The rest of a removed option's section is left as written.
            for key in config.get_prefix_options(""):
                config.get(key, None)
            return
        self.label = read("get", "label")
        self.group = read("get", "section", "")
        self.hint = read("get", "hint", "")
        if config is None:
            self.type = built_in["type"]
        else:
            self.type = config.getchoice(
                "type", {t: t for t in TYPES},
                *([built_in["type"]] if "type" in built_in else []))
        # The printer object this option needs, such as a calibration module.
        self.requires = read("get", "requires", None)
        self.default_variable = None
        if self.type == "switch":
            self.default = read("getboolean", "default", False)
            return
        # A minutes option with a switch is off unless the client turns it on.
        self.optional = read("getboolean", "optional", False)
        # What the time means, shown next to it once the switch is on.
        self.value_label = read("get", "value_label", "")
        self.max = read("getint", "max", 240, minval=1)
        self.min = read(
            "getint", "min", 1 if self.optional else 0,
            minval=1 if self.optional else 0, maxval=self.max)
        self.step = read("getint", "step", 5, minval=1)
        # A section that narrows the range moves the built-in time into it.
        if "default" in built_in:
            built_in = dict(built_in, default=min(
                max(built_in["default"], self.min), self.max))
        self.default = float(read(
            "getfloat", "default", self.min, minval=self.min, maxval=self.max))
        # "<macro>.<variable>": a gcode_macro variable that holds the default,
        # for a setting people already change there.
        variable = read("get", "default_variable", None)
        if variable is not None:
            if "." not in variable:
                raise config.error(
                    "[%s]: default_variable must be <macro>.<variable>"
                    % (config.get_name(),))
            macro, name = variable.rsplit(".", 1)
            self.default_variable = (macro.strip(), name.strip().lower())

    def available(self):
        return (self.requires is None
                or self.printer.lookup_object(self.requires, None) is not None)

    def _default(self):
        if self.default_variable is None:
            return self.default
        macro = self.printer.lookup_object(
            "gcode_macro " + self.default_variable[0], None)
        value = getattr(macro, "variables", {}).get(self.default_variable[1])
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not self.min <= value <= self.max:
            return self.default
        return value

    def parse(self, gcmd):
        """The value to keep for this print, or None for 'not chosen'."""
        name = self.id.upper()
        if self.type == "switch":
            return bool(gcmd.get_int(name, minval=0, maxval=1))
        value = gcmd.get_float(name, minval=0., maxval=self.max)
        if self.optional and value == 0.:
            return None
        if value < self.min:
            raise gcmd.error("%s must be at least %d" % (name, self.min))
        return value

    def describe(self):
        info = {"id": self.id, "label": self.label, "section": self.group,
                "hint": self.hint, "type": self.type}
        if self.type == "switch":
            info["default"] = self.default
        else:
            info.update(optional=self.optional, value_label=self.value_label,
                        min=self.min, max=self.max, step=self.step,
                        default=self._default())
        return info


class PrintControl:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        sections = {}
        for section in config.get_prefix_sections("print_option "):
            option_id = section.get_name().split(None, 1)[1].strip().lower()
            if option_id in sections:
                raise config.error("Two [print_option] sections share a name")
            sections[option_id] = section
        built_in = dict(BUILT_IN_OPTIONS)
        ids = [option_id for option_id, _values in BUILT_IN_OPTIONS]
        ids += [option_id for option_id in sections if option_id not in built_in]
        options = [PrintOption(self.printer, option_id, sections.get(option_id),
                               built_in.get(option_id))
                   for option_id in ids]
        self.options = [option for option in options if option.enabled]
        # Choices from PRINT_OPTIONS, waiting for the next file to load.
        self.pending = None
        self.pending_expires = 0.
        # Choices of the loaded print.
        self.active = {}
        self.soak_end = None
        # Report the time left at the next step: set on start and on a change.
        self.soak_report_now = False
        self.soak_next_report = 0
        # When the heaters kept on after a print turn off.
        self.keep_end = None
        # The loaded print's heaters have been kept once already.
        self.keep_used = False
        # (heater, label, target) of each heater being kept.
        self.kept = []
        # The display message that says so, cleared if it still shows.
        self.keep_message = None
        self.keep_timer = self.reactor.register_timer(self._check_keeping)
        self.gcode.register_command(
            "PRINT_OPTIONS", self.cmd_PRINT_OPTIONS,
            desc=self.cmd_PRINT_OPTIONS_help)
        self.gcode.register_command(
            "HEAT_SOAK", self.cmd_HEAT_SOAK, desc=self.cmd_HEAT_SOAK_help)
        self.gcode.register_command(
            "PRINT_HEATERS_OFF", self.cmd_PRINT_HEATERS_OFF,
            desc=self.cmd_PRINT_HEATERS_OFF_help)
        self.printer.lookup_object("webhooks").register_endpoint(
            "print_control/soak", self._handle_soak)
        # A start resets the file and then loads the new one, so the reset
        # leaves the waiting choices alone.
        self.printer.register_event_handler(
            "virtual_sdcard:reset_file", self._handle_reset_file)
        self.printer.register_event_handler(
            "virtual_sdcard:load_file", self._handle_load_file)
        # A start command that fails ends its script, so the choices sent
        # ahead of it must not wait for some later print.
        self.printer.register_event_handler(
            "gcode:command_error", self._handle_command_error)

    def _available(self):
        return [option for option in self.options if option.available()]

    def _print_state(self):
        stats = self.printer.lookup_object("print_stats", None)
        return getattr(stats, "state", "")

    def _respond(self, prefix, msg):
        if prefix:
            self.gcode.respond_raw("%s %s" % (prefix, msg))
        else:
            self.gcode.respond_info(msg)

    # -- options ---------------------------------------------------------

    cmd_PRINT_OPTIONS_help = (
        "Set options for the print that starts next; no options clears them")

    def cmd_PRINT_OPTIONS(self, gcmd):
        self.pending = None
        options = {option.id.upper(): option for option in self._available()}
        params = gcmd.get_command_parameters()
        unknown = sorted(name for name in params if name not in options)
        if unknown:
            raise gcmd.error(
                "Unknown print option: %s" % (", ".join(unknown),))
        chosen = {}
        for name in params:
            value = options[name].parse(gcmd)
            if value is not None:
                chosen[options[name].id] = value
        if not params:
            return
        self.pending = chosen
        self.pending_expires = self.reactor.monotonic() + PENDING_SECONDS
        # Kalico logs this reply, so it also records the choices in klippy.log.
        gcmd.respond_info("Options for the next print: %s" % (
            ", ".join("%s=%g" % item for item in sorted(chosen.items()))
            or "none",))

    def _handle_command_error(self):
        self.pending = None

    def _handle_reset_file(self):
        self.active = {}

    def _handle_load_file(self):
        fresh = (self.pending is not None
                 and self.reactor.monotonic() <= self.pending_expires)
        self.active = self.pending if fresh else {}
        self.pending = None
        self.keep_used = False

    # -- heat soak -------------------------------------------------------

    def _soak_left(self, eventtime):
        return max(0., self.soak_end - eventtime)

    def _report_soak(self, prefix, left):
        seconds = int(math.ceil(left))
        if not self.soak_report_now and seconds > self.soak_next_report:
            return
        self.soak_report_now = False
        # The next report is at the next whole minute below this one.
        self.soak_next_report = (seconds - 1) // 60 * 60
        msg = "Soaking: %d min %d sec left" % divmod(seconds, 60)
        self.gcode.run_script_from_command("M117 %s" % (msg,))
        self._respond(prefix, msg)

    cmd_HEAT_SOAK_help = (
        "Wait MINUTES for the printer to heat soak, or the time chosen for "
        "the running print")

    def cmd_HEAT_SOAK(self, gcmd):
        minutes = gcmd.get_float("MINUTES", 0.)
        prefix = gcmd.get("PREFIX", "")
        # The choices outlast their print, so one only counts while it runs.
        chosen = self.active.get("soak")
        if chosen is not None and self._print_state() == "printing":
            minutes = chosen
            self._respond(
                prefix, "Heat soak set at print start: %g min" % (minutes,))
        minutes = min(minutes, MAX_SOAK_MINUTES)
        if minutes <= 0.:
            return
        # Taken before the queued moves drain, so an interrupt sent meanwhile
        # still ends the soak.
        tokens = hasattr(gcmd, "wait_while")
        counter = None if tokens else self.gcode.get_interrupt_counter()
        self.printer.lookup_object("toolhead").wait_moves()
        self.soak_end = self.reactor.monotonic() + minutes * 60.
        self.soak_report_now = True

        def soaking(eventtime):
            left = self._soak_left(eventtime)
            if left > 0.:
                self._report_soak(prefix, left)
            return left > 0.
        # HEATER_INTERRUPT or a shutdown aborts the soak and the macro running
        # it, as they abort TEMPERATURE_WAIT. Skip only ends the soak early.
        try:
            if tokens:
                # Kalico branches where a command waits on its interrupt token.
                gcmd.wait_while(soaking)
            else:
                self._wait_while(soaking, counter)
        finally:
            self.soak_end = None

    def _wait_while(self, waiting, counter):
        eventtime = self.reactor.monotonic()
        while True:
            if self.printer.is_shutdown() \
                    or counter != self.gcode.get_interrupt_counter():
                raise self.printer.wait_interrupted("Command interrupted")
            if not waiting(eventtime):
                return
            eventtime = self.reactor.pause(
                eventtime + min(1., self._soak_left(eventtime)))

    def _handle_soak(self, web_request):
        """action=add|set with minutes, or action=skip."""
        if self.soak_end is None:
            raise web_request.error("No heat soak is running")
        action = web_request.get_str("action")
        eventtime = self.reactor.monotonic()
        if action == "skip":
            left = 0.
        elif action in ("add", "set"):
            left = web_request.get_float("minutes") * 60.
            # JSON allows NaN and Infinity, which no time can be made from.
            if not math.isfinite(left):
                raise web_request.error("minutes must be a number")
            if action == "add":
                left += self._soak_left(eventtime)
        else:
            raise web_request.error(
                "Unknown heat soak action '%s'" % (action,))
        left = min(max(left, 0.), MAX_SOAK_MINUTES * 60.)
        self.soak_end = eventtime + left
        self.soak_report_now = True
        logging.info("print_control: soak %s, %.0f s left", action, left)
        web_request.send({"remaining": int(math.ceil(left))})

    # -- keep heated -----------------------------------------------------

    def _kept_heaters(self):
        pheaters = self.printer.lookup_object("heaters")
        found = []
        for name, label in KEPT_HEATERS:
            try:
                found.append((pheaters.lookup_heater(name), label))
            except Exception:
                # A printer without this heater.
                pass
        return pheaters, found

    def _stop_keeping(self):
        self.keep_end = None
        self.kept = []
        self.reactor.update_timer(self.keep_timer, self.reactor.NEVER)
        display = self.printer.lookup_object("display_status", None)
        if self.keep_message is not None \
                and getattr(display, "message", None) == self.keep_message:
            display.message = None
        self.keep_message = None

    cmd_PRINT_HEATERS_OFF_help = (
        "Turn the heaters off after a print; KEEP=1 leaves the bed and "
        "chamber on when that was chosen for the print")

    def cmd_PRINT_HEATERS_OFF(self, gcmd):
        keep = bool(gcmd.get_int("KEEP", 0, minval=0, maxval=1))
        # A print can end more than once: an error after END_PRINT, or a
        # cancel after an error. Its heaters stay kept through that, and the
        # time is not started again.
        holding = keep and self.keep_used and self.keep_end is not None
        # A print's heaters are kept once, so ending it again after the time
        # is up turns them off.
        minutes = 0.
        if keep and not self.keep_used:
            minutes = self.active.get("keep_heated", 0.)
        eventtime = self.reactor.monotonic()
        pheaters, heaters = self._kept_heaters()
        targets = [(heater, label, heater.get_temp(eventtime)[1])
                   for heater, label in heaters]
        targets = [item for item in targets if item[2] > 0.]
        if holding:
            # The check must not see the heaters off in between.
            self.reactor.update_timer(self.keep_timer, self.reactor.NEVER)
            try:
                self.gcode.run_script_from_command(HEATERS_OFF_SCRIPT)
                for heater, label, target in targets:
                    pheaters.set_temperature(heater, target)
            finally:
                self.reactor.update_timer(self.keep_timer, self.reactor.NOW)
            return
        self._stop_keeping()
        self.gcode.run_script_from_command(HEATERS_OFF_SCRIPT)
        if minutes <= 0. or not targets:
            return
        for heater, label, target in targets:
            pheaters.set_temperature(heater, target)
        self.keep_used = True
        self.kept = targets
        self.keep_end = self.reactor.monotonic() + minutes * 60.
        self.reactor.update_timer(
            self.keep_timer, self.reactor.monotonic() + KEEP_CHECK_SECONDS)
        msg = "Keeping the %s for %g min" % (" and ".join(
            "%s at %g\u00b0C" % (label, target)
            for heater, label, target in targets), minutes)
        self.gcode.run_script_from_command("M117 %s" % (msg,))
        self.keep_message = getattr(
            self.printer.lookup_object("display_status", None), "message", None)
        self.gcode.respond_info(msg)

    def _check_keeping(self, eventtime):
        if self.keep_end is None:
            return self.reactor.NEVER
        # A heater set to something else meanwhile, also to off, is no longer
        # kept and stays as it was set.
        self.kept = [item for item in self.kept
                     if item[0].get_temp(eventtime)[1] == item[2]]
        # A print that never turned the heaters off is using them; turn them
        # off once it is over.
        if self.kept and (eventtime < self.keep_end
                          or self._print_state() in ("printing", "paused")):
            return eventtime + KEEP_CHECK_SECONDS
        kept = self.kept
        self._stop_keeping()
        for heater, label, target in kept:
            heater.set_temp(0.)
        if kept:
            self.gcode.respond_info("Turned off the %s" % (
                " and ".join(label for heater, label, target in kept),))
        return self.reactor.NEVER

    def get_status(self, eventtime):
        soaking = self.soak_end is not None
        keeping = self.keep_end is not None
        return {
            "version": 1,
            "options": [option.describe() for option in self._available()],
            "active": dict(self.active),
            "soak": {
                "active": soaking,
                "remaining": (int(math.ceil(self._soak_left(eventtime)))
                              if soaking else 0),
            },
            "keep_heated": {
                "active": keeping,
                "remaining": (
                    int(math.ceil(max(0., self.keep_end - eventtime)))
                    if keeping else 0),
                # The heaters still kept, by name, with their targets.
                "targets": {label: target
                            for heater, label, target in self.kept},
            },
        }


def load_config(config):
    return PrintControl(config)
