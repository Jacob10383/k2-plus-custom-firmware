# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Fan tachometer feedback via Creality-proprietary config_fancheck /
# query_fancheck MCU commands present in the K2 Plus nozzle MCU firmware.
#
# Pins on the same MCU are grouped automatically — one config_fancheck OID
# per MCU. Up to MAX_FANS_PER_GROUP fans per MCU.
#
# Protection behaviour per fan (each fan goes in at most one list):
#   shutdown_fans  — stall confirmed over confirm_seconds → heater off + shutdown
#   pause_fans     — stall confirmed over confirm_seconds → pause if printing, else warn
#                    repeats warn every repeat_warn_seconds while still stalled
#   warn_fans      — stall confirmed over confirm_seconds → warn, repeat every repeat_warn_seconds
import logging

MAX_FANS_PER_GROUP = 5

# K2 Plus defaults
DEFAULT_FANS            = "PC6, nozzle_mcu:PA12, nozzle_mcu:PC13"
DEFAULT_SHUTDOWN_FANS   = "PC6"
DEFAULT_PAUSE_FANS      = "nozzle_mcu:PA12, nozzle_mcu:PC13"
DEFAULT_WARN_FANS       = ""
DEFAULT_CONFIRM_SECS    = 20.0
DEFAULT_REPEAT_WARN     = 1800.0   # 30 minutes
DEFAULT_POLL_INTERVAL   = 1.0
# Klipper objects that command each fan (positional, matches DEFAULT_FANS order).
# Used to check if the fan is actually commanded on before counting a stall.
DEFAULT_FAN_DRIVERS     = "heater_fan chamber_heater_fan, heater_fan heatbreak_fan, fan"
DEFAULT_FAN_NAMES       = "chamber heater fan, heatbreak fan, part cooling fan"


def _klog(msg, *args, level=logging.info):
    level("fan_feedback: " + msg, *args)


class FanGroup:
    """One config_fancheck OID covering all monitored fans on a single MCU."""

    def __init__(self, mcu, fans, on_update):
        self.mcu = mcu
        self.fans = fans        # [(label, pin_params), ...]
        self.on_update = on_update
        self.oid = mcu.create_oid()
        self._query_cmd = None
        self._which_fan = (2 ** len(fans)) - 1

        last_pp = fans[-1][1]
        cmd = "config_fancheck oid=%d fan_num=%d" % (self.oid, len(fans))
        for i in range(MAX_FANS_PER_GROUP):
            pp = fans[i][1] if i < len(fans) else last_pp
            cmd += " fan%d_pin=%s pull_up%d=%s" % (i, pp["pin"], i, pp["pullup"])

        mcu.add_config_cmd(cmd)
        mcu.register_response(self._handle_fan_status, "fan_status", self.oid)
        mcu.register_config_callback(self._build_config)

    def _build_config(self):
        self._query_cmd = self.mcu.lookup_command(
            "query_fancheck oid=%c which_fan=%c", cq=None
        )

    def poll(self):
        if self._query_cmd is not None:
            self._query_cmd.send([self.oid, self._which_fan])

    def _handle_fan_status(self, params):
        speeds = {
            label: params.get("fan%d_speed" % i, 0)
            for i, (label, _) in enumerate(self.fans)
        }
        self.on_update(speeds)


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

        fan_pins = [p.strip() for p in config.getlist("fans", DEFAULT_FANS.split(","))]

        def _parse_fan_list(key, default):
            raw = config.get(key, default)
            if not raw.strip():
                return set()
            return {p.strip() for p in raw.split(",")}

        self.shutdown_fans = _parse_fan_list("shutdown_fans", DEFAULT_SHUTDOWN_FANS)
        self.pause_fans    = _parse_fan_list("pause_fans",    DEFAULT_PAUSE_FANS)
        self.warn_fans     = _parse_fan_list("warn_fans",     DEFAULT_WARN_FANS)

        # Map each tach pin to the Klipper object that commands its fan.
        # Stall is only counted while that specific fan is commanded on.
        driver_specs = [
            d.strip()
            for d in config.get("fan_drivers", DEFAULT_FAN_DRIVERS).split(",")
        ]
        if len(driver_specs) != len(fan_pins):
            raise config.error(
                "fan_feedback: fan_drivers count (%d) must match fans count (%d)"
                % (len(driver_specs), len(fan_pins))
            )
        self._fan_drivers = dict(zip(fan_pins, driver_specs))

        name_specs = [
            n.strip()
            for n in config.get("fan_names", DEFAULT_FAN_NAMES).split(",")
        ]
        if len(name_specs) != len(fan_pins):
            raise config.error(
                "fan_feedback: fan_names count (%d) must match fans count (%d)"
                % (len(name_specs), len(fan_pins))
            )
        self._fan_names = dict(zip(fan_pins, name_specs))

        # Group pins by MCU, preserving order
        ppins = self.printer.lookup_object("pins")
        by_mcu = {}
        for pin_spec in fan_pins:
            pp = ppins.lookup_pin(pin_spec, can_invert=False, can_pullup=True)
            mcu = pp["chip"]
            by_mcu.setdefault(mcu, []).append((pin_spec, pp))

        self.speeds = {label: 0 for label in fan_pins}
        self.groups = [
            FanGroup(mcu, fans, self._on_group_update)
            for mcu, fans in by_mcu.items()
        ]

        # stall tracking: label -> seconds spent stalled while fan commanded on
        self._stall_ticks = {label: 0.0 for label in fan_pins}
        # last time a warn/pause action fired per fan (0 = never)
        self._last_warn_eventtime = {label: 0.0 for label in fan_pins}

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

    def _on_group_update(self, group_speeds):
        self.speeds.update(group_speeds)

    def _handle_ready(self):
        reactor = self.printer.get_reactor()
        self._timer = reactor.register_timer(
            self._poll, reactor.monotonic() + 1.0
        )

        # Inject RPM directly into live fan drivers' get_status
        for pin_label, driver_name in self._fan_drivers.items():
            obj = self.printer.lookup_object(driver_name, None)
            if obj is not None:
                orig_get_status = obj.get_status
                def make_wrapper(orig_func, pin):
                    return lambda eventtime: {
                        **orig_func(eventtime),
                        "rpm": self.speeds.get(pin, 0.0)
                    }
                obj.get_status = make_wrapper(orig_get_status, pin_label)

    # -- Protection logic -----------------------------------------------------

    def _fan_commanded_on(self, label):
        """True if the Klipper fan object driving this tach pin has speed > 0."""
        driver_name = self._fan_drivers.get(label)
        if not driver_name:
            return True  # no driver configured — always monitor
        obj = self.printer.lookup_object(driver_name, None)
        if obj is None:
            return True  # object missing — monitor for safety
        try:
            eventtime = self.printer.get_reactor().monotonic()
            st = obj.get_status(eventtime)
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
        for group in self.groups:
            group.poll()
        self._check_protection(eventtime)
        return eventtime + self.poll_interval

    # -- GCode + webhooks -----------------------------------------------------

    def _fan_display_name(self, label):
        if label in self._fan_names:
            return self._fan_names[label]
        driver = self._fan_drivers.get(label, "")
        # "heater_fan heatbreak_fan" -> "heatbreak fan", "fan" -> "fan"
        parts = driver.split(None, 1)
        name = parts[1] if len(parts) == 2 else (parts[0] if parts else label)
        return name.replace("_", " ")

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
