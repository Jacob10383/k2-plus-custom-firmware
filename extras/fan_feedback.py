# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Fan tachometer policy using the native tachometer_pin support on each fan.
#
# Protection behaviour per fan (each fan goes in at most one list):
#   shutdown_fans  — stall confirmed over confirm_seconds → heater off + shutdown
#   pause_fans     — stall confirmed over confirm_seconds → pause if printing, else warn
#                    repeats warn every repeat_warn_seconds while still stalled
#   warn_fans      — stall confirmed over confirm_seconds → warn, repeat every repeat_warn_seconds
import logging

# K2 Plus defaults
DEFAULT_FAN_OBJECTS     = (
    "heater_fan chamber_heater_fan",
    "heater_fan heatbreak_fan",
    "fan",
)
DEFAULT_SHUTDOWN_FANS   = ("heater_fan chamber_heater_fan",)
DEFAULT_PAUSE_FANS      = ("heater_fan heatbreak_fan", "fan")
DEFAULT_WARN_FANS       = ()
DEFAULT_CONFIRM_SECS    = 20.0
DEFAULT_REPEAT_WARN     = 1800.0   # 30 minutes
DEFAULT_POLL_INTERVAL   = 1.0
DEFAULT_FAN_NAMES       = (
    "chamber heater fan",
    "heatbreak fan",
    "part cooling fan",
)


def _klog(msg, *args, level=logging.info):
    level("fan_feedback: " + msg, *args)


def _config_list(config, key, default):
    raw = config.get(key, ", ".join(default))
    return tuple(item.strip() for item in raw.split(",") if item.strip())


class FanFeedback:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.poll_interval = config.getfloat(
            "poll_interval", DEFAULT_POLL_INTERVAL, minval=0.1
        )
        self.confirm_secs = config.getfloat(
            "confirm_seconds", DEFAULT_CONFIRM_SECS, minval=1.0
        )
        self.repeat_warn_secs = config.getfloat(
            "repeat_warn_seconds", DEFAULT_REPEAT_WARN, minval=60.0
        )

        self.fan_objects = _config_list(config, "fan_objects", DEFAULT_FAN_OBJECTS)
        if not self.fan_objects:
            raise config.error("fan_feedback: fan_objects must not be empty")
        self.shutdown_fans = set(_config_list(
            config, "shutdown_fans", DEFAULT_SHUTDOWN_FANS
        ))
        self.pause_fans = set(_config_list(config, "pause_fans", DEFAULT_PAUSE_FANS))
        self.warn_fans = set(_config_list(config, "warn_fans", DEFAULT_WARN_FANS))
        unknown = (self.shutdown_fans | self.pause_fans | self.warn_fans) - set(
            self.fan_objects
        )
        if unknown:
            raise config.error(
                "fan_feedback: policy references unknown fan_objects: %s"
                % ", ".join(sorted(unknown))
            )

        names = _config_list(config, "fan_names", DEFAULT_FAN_NAMES)
        if len(names) != len(self.fan_objects):
            raise config.error(
                "fan_feedback: fan_names count (%d) must match fan_objects count (%d)"
                % (len(names), len(self.fan_objects))
            )
        self._fan_names = dict(zip(self.fan_objects, names))
        self._fans = {}
        self.speeds = {name: 0.0 for name in self.fan_objects}

        # stall tracking: label -> seconds spent stalled while fan commanded on
        self._stall_ticks = {name: 0.0 for name in self.fan_objects}
        # last time a warn/pause action fired per fan (0 = never)
        self._last_warn_eventtime = {name: 0.0 for name in self.fan_objects}

        self.print_stats = self.printer.load_object(config, "print_stats")
        self.pause_resume = self.printer.load_object(config, "pause_resume")
        self._timer = None

        gcode = self.printer.lookup_object("gcode")
        self.gcode = gcode
        gcode.register_command(
            "FAN_FEEDBACK_STATUS",
            self.cmd_FAN_FEEDBACK_STATUS,
            desc="Report fan tachometer speeds from MCU feedback",
        )

        webhooks = self.printer.lookup_object("webhooks")
        webhooks.register_endpoint("fan_feedback/status", self._handle_webhook)

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        reactor = self.printer.get_reactor()
        for name in self.fan_objects:
            fan = self.printer.lookup_object(name, None)
            if fan is None:
                raise self.printer.config_error(
                    "fan_feedback: unknown fan object '%s'" % name
                )
            if fan.get_status(reactor.monotonic()).get("rpm") is None:
                raise self.printer.config_error(
                    "fan_feedback: %s needs tachometer_pin" % name
                )
            self._fans[name] = fan
        self._timer = reactor.register_timer(
            self._poll, reactor.monotonic() + 1.0
        )

    # -- Protection logic -----------------------------------------------------

    def _fan_commanded_on(self, label):
        """True if the Klipper fan object driving this tach pin has speed > 0."""
        try:
            eventtime = self.printer.get_reactor().monotonic()
            st = self._fans[label].get_status(eventtime)
            # heater_fan / fan_generic / temperature_fan expose 'speed'
            # output_pin exposes 'value'
            return st.get("speed", st.get("value", 0)) > 0
        except Exception:
            return True

    def _is_printing(self):
        try:
            eventtime = self.printer.get_reactor().monotonic()
            return self.print_stats.get_status(eventtime).get("state") == "printing"
        except Exception:
            return False

    def _warn(self, msg):
        _klog("%s", msg, level=logging.warning)
        try:
            self.gcode.respond_raw("!! fan_feedback: %s" % msg)
        except Exception:
            pass

    def _check_protection(self, eventtime):
        interval = self.poll_interval

        for label, speed in self.speeds.items():
            is_shutdown = label in self.shutdown_fans
            is_pause    = label in self.pause_fans
            is_warn     = label in self.warn_fans

            if not (is_shutdown or is_pause or is_warn):
                continue

            if speed == 0 and self._fan_commanded_on(label):
                self._stall_ticks[label] += interval
            else:
                # Fan recovered — reset everything
                self._stall_ticks[label] = 0.0
                self._last_warn_eventtime[label] = 0.0
                continue

            stalled = self._stall_ticks[label]
            if stalled < self.confirm_secs:
                continue

            if is_shutdown:
                name = self._fan_display_name(label)
                self._warn(
                    "STALL on %s for %.0fs — turning off chamber heater and shutting down"
                    % (name, stalled)
                )
                try:
                    self.gcode.run_script_from_command("M141 S0")
                except Exception:
                    pass
                self.printer.invoke_shutdown(
                    "fan_feedback: %s stalled — chamber heater emergency stop" % name
                )

            elif is_pause or is_warn:
                last = self._last_warn_eventtime[label]
                if last == 0.0 or (eventtime - last) >= self.repeat_warn_secs:
                    self._last_warn_eventtime[label] = eventtime
                    name = self._fan_display_name(label)
                    if is_pause and self._is_printing():
                        self._warn(
                            "STALL on %s for %.0fs — pausing print" % (name, stalled)
                        )
                        # Async PAUSE delivery - see motor_control for rationale.
                        try:
                            if not self.pause_resume.pause_command_sent:
                                reactor = self.printer.get_reactor()
                                self.pause_resume.send_pause_command()
                                reactor.register_async_callback(
                                    lambda e: self.gcode.run_script("PAUSE"))
                        except Exception:
                            _klog(
                                "pause request failed for %s",
                                name,
                                level=logging.exception)
                    else:
                        self._warn("STALL on %s for %.0fs" % (name, stalled))

    # -- Poll timer -----------------------------------------------------------

    def _poll(self, eventtime):
        if self.printer.is_shutdown():
            return self.printer.get_reactor().NEVER
        for name, fan in self._fans.items():
            self.speeds[name] = fan.get_status(eventtime)["rpm"]
        self._check_protection(eventtime)
        return eventtime + self.poll_interval

    # -- GCode + webhooks -----------------------------------------------------

    def _fan_display_name(self, label):
        if label in self._fan_names:
            return self._fan_names[label]
        return label.replace("_", " ")

    def cmd_FAN_FEEDBACK_STATUS(self, gcmd):
        if not self.speeds:
            gcmd.respond_info("fan_feedback: no fans configured")
            return
        lines = ["Fan feedback status:"]
        for label, speed in self.speeds.items():
            stalled = self._stall_ticks.get(label, 0.0)
            if speed > 0:
                prefix = "[OK]"
                suffix = ""
            elif stalled > 0:
                prefix = "[!!]"
                suffix = "  stalled %.0fs" % stalled
            else:
                prefix = "    "
                suffix = ""
            name = self._fan_display_name(label)
            lines.append(
                "  %s %-16s %5d RPM%s" % (prefix, name, speed, suffix)
            )
        gcmd.respond_info("\n".join(lines))

    def _handle_webhook(self, web_request):
        web_request.send(self.get_status(None))

    def get_status(self, eventtime):
        return dict(self.speeds)


def load_config(config):
    return FanFeedback(config)
