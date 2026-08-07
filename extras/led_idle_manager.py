# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# LED auto-off manager based on user activity and print state.
#
# Keeps LED on while printing, otherwise turns it off after a timeout
# from last user-originated command activity.

import logging

TIMER_INTERVAL = 1.0
VALUE_EPSILON = 0.000001


def _klog(msg, *args, level=logging.info):
    level("led_idle_manager: " + msg, *args)


class LedIdleManager:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.webhooks = self.printer.lookup_object("webhooks", None)
        self.print_stats = None
        self.output_pin = None
        self._webhook_script_patched = False

        self.pin = config.get("pin", "LED")
        self.timeout = config.getfloat("timeout", 300.0, above=0.0)
        self.on_value = config.getfloat("on_value", 1.0, minval=0.0, maxval=1.0)
        self.off_value = config.getfloat("off_value", 0.0, minval=0.0, maxval=1.0)

        ignore_defaults = "M105,STATUS,GET_POSITION,M114,QUERY_ENDSTOPS"
        ignore_cmds = config.get("ignore_commands", ignore_defaults)
        self.ignore_commands = self._parse_ignore_commands(ignore_cmds)

        self.last_user_activity = self.reactor.monotonic()
        self.activity_suppressed_until = 0.0
        self.led_is_on = None
        self.last_user_brightness = self.on_value
        self.enabled = True
        self.manually_off = False
        self._setting_led = False

        self.timer = self.reactor.register_timer(
            self._timer_event, self.reactor.NEVER
        )

        self.gcode.register_command(
            "SET_LED_IDLE_MANAGER",
            self.cmd_SET_LED_IDLE_MANAGER,
            desc=self.cmd_SET_LED_IDLE_MANAGER_help,
        )
        self.gcode.register_command(
            "LED_IDLE_MANAGER_OFF",
            self.cmd_LED_IDLE_MANAGER_OFF,
            desc=self.cmd_LED_IDLE_MANAGER_OFF_help,
        )
        self.gcode.register_command(
            "LED_IDLE_MANAGER_ON",
            self.cmd_LED_IDLE_MANAGER_ON,
            desc=self.cmd_LED_IDLE_MANAGER_ON_help,
        )
        self.gcode.register_command(
            "LED_IDLE_MANAGER_STATUS",
            self.cmd_LED_IDLE_MANAGER_STATUS,
            desc=self.cmd_LED_IDLE_MANAGER_STATUS_help,
        )

        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler(
            "gcode:user_activity", self._handle_user_activity
        )

    def _parse_ignore_commands(self, raw):
        commands = set()
        for token in raw.replace(",", " ").split():
            token = token.strip().upper()
            if token:
                commands.add(token)
        return commands

    def _extract_led_pin_value(self, origline):
        if not origline:
            return None
        parts = origline.strip().split()
        if not parts or parts[0].upper() != "SET_PIN":
            return None
        pin_name = None
        value = None
        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.upper()
                if k == "PIN":
                    pin_name = v.upper()
                elif k == "VALUE":
                    try:
                        value = float(v)
                    except ValueError:
                        pass
        if pin_name is None or value is None:
            return None
        if pin_name != self.pin.upper():
            return None
        return value

    def _is_led_off_value(self, value):
        return value <= self.off_value + VALUE_EPSILON

    def _handle_ready(self):
        self._install_webhook_activity_hook()
        self.print_stats = self.printer.lookup_object("print_stats", None)
        self._set_enabled(self.enabled, refresh_activity=True)

    def _extract_webhook_activity(self, script):
        for line in script.split("\n"):
            line = line.strip()
            if not line or line.startswith(";") or line.startswith("#"):
                continue
            cmd = line.split(None, 1)[0].upper()
            return cmd, line
        return "", script

    def _install_webhook_activity_hook(self):
        if self._webhook_script_patched or self.webhooks is None:
            return
        endpoints = getattr(self.webhooks, "_endpoints", None)
        if not isinstance(endpoints, dict):
            _klog(
                "webhooks endpoint table unavailable",
                level=logging.warning,
            )
            return
        original = endpoints.get("gcode/script")
        if original is None:
            _klog(
                "gcode/script webhook endpoint missing",
                level=logging.warning,
            )
            return

        def wrapped(web_request):
            script = web_request.get_str("script")
            cmd, origline = self._extract_webhook_activity(script)
            try:
                self.printer.send_event("gcode:user_activity", cmd, origline)
            except Exception:
                _klog(
                    "failed to emit webhook user activity",
                    level=logging.exception)
            return original(web_request)

        endpoints["gcode/script"] = wrapped
        self._webhook_script_patched = True

    def _handle_user_activity(self, cmd, origline):
        if self._setting_led:
            return
        if self.reactor.monotonic() < self.activity_suppressed_until:
            return
        cmd = (cmd or "").strip().upper()
        if not cmd and origline:
            parts = origline.strip().split()
            if parts:
                cmd = parts[0].upper()
        if not self.enabled:
            return
        if cmd in (
            "SET_LED_IDLE_MANAGER",
            "LED_IDLE_MANAGER_OFF",
            "LED_IDLE_MANAGER_ON",
            "LED_IDLE_MANAGER_STATUS",
            "_SET_TIMELAPSE_SETUP",
            "HYPERLAPSE",
        ):
            return
        if cmd in self.ignore_commands:
            return

        led_value = None
        if cmd == "SET_PIN":
            led_value = self._extract_led_pin_value(origline)
            # If the user explicitly turned off the LED, enter manually_off
            # state without forgetting their previous brightness.
            if led_value is not None and self._is_led_off_value(led_value):
                self.manually_off = True
                return

        # Any other real activity clears the manually_off state.
        self.manually_off = False
        if led_value is not None:
            self.last_user_brightness = led_value
        self.last_user_activity = self.reactor.monotonic()
        if not self._is_printing():
            self._set_led(True)
        self.reactor.update_timer(self.timer, self.reactor.NOW + 0.05)

    def _set_enabled(self, enabled, refresh_activity=False):
        self.enabled = bool(enabled)
        if refresh_activity:
            self.last_user_activity = self.reactor.monotonic()
        if self.enabled:
            self.manually_off = False
            self.last_user_activity = self.reactor.monotonic()
            self._refresh_brightness_from_led()
            if not self._is_printing():
                self._set_led(True)
            self.reactor.update_timer(self.timer, self.reactor.NOW + 0.05)
        else:
            self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def _get_print_state(self):
        if self.print_stats is None:
            self.print_stats = self.printer.lookup_object("print_stats", None)
            if self.print_stats is None:
                return None
        return self.print_stats.state

    def _is_printing(self):
        return self._get_print_state() == "printing"

    def _get_led_value(self, eventtime):
        if self.output_pin is None:
            self.output_pin = self.printer.lookup_object("output_pin " + self.pin, None)
            if self.output_pin is None:
                return None
        try:
            return self.output_pin.get_status(eventtime).get("value")
        except Exception:
            _klog("failed to read output_pin %s", self.pin,
                  level=logging.exception)
            return None

    def _get_led_is_on(self, eventtime):
        value = self._get_led_value(eventtime)
        if value is None:
            return None
        return not self._is_led_off_value(value)

    def _refresh_brightness_from_led(self):
        value = self._get_led_value(self.reactor.monotonic())
        if value is not None and not self._is_led_off_value(value):
            self.last_user_brightness = value

    def _is_klipper_ready(self):
        return getattr(self.gcode, "is_printer_ready", True)

    def _set_led(self, on):
        if not self._is_klipper_ready():
            return
        eventtime = self.reactor.monotonic()
        actual = self._get_led_is_on(eventtime)
        if self.led_is_on is not None and self.led_is_on == on and (actual is None or actual == on):
            return
        value = self.last_user_brightness if on else self.off_value
        script = "SET_PIN PIN=%s VALUE=%g" % (self.pin, value)
        try:
            self._setting_led = True
            self.gcode.run_script_from_command(script)
            self.led_is_on = on
        except Exception:
            _klog("failed to set LED via %s", script,
                  level=logging.exception)
        finally:
            self._setting_led = False

    def _timer_event(self, eventtime):
        if self.printer.is_shutdown():
            return self.reactor.NEVER
        if not self.enabled:
            return self.reactor.NEVER

        print_state = self._get_print_state()
        printing = print_state == "printing"

        if printing:
            self.last_user_activity = eventtime
            if self.manually_off:
                self._set_led(False)
            else:
                self._set_led(True)
            return eventtime + TIMER_INTERVAL

        idle_for = eventtime - self.last_user_activity
        if idle_for >= self.timeout or self.manually_off:
            self._set_led(False)
        else:
            self._set_led(True)
        return eventtime + TIMER_INTERVAL

    cmd_SET_LED_IDLE_MANAGER_help = "Enable/disable LED idle manager for this Klipper session"

    def cmd_SET_LED_IDLE_MANAGER(self, gcmd):
        enabled = bool(gcmd.get_int("ENABLE", 1, minval=0, maxval=1))
        self._set_enabled(enabled)
        gcmd.respond_info("led_idle_manager: ENABLE=%d (session only)" % (1 if self.enabled else 0))

    cmd_LED_IDLE_MANAGER_OFF_help = "Disable LED idle manager for this Klipper session"

    def cmd_LED_IDLE_MANAGER_OFF(self, gcmd):
        self._set_enabled(False)
        gcmd.respond_info("led_idle_manager: disabled for this session")

    cmd_LED_IDLE_MANAGER_ON_help = "Enable LED idle manager for this Klipper session"

    def cmd_LED_IDLE_MANAGER_ON(self, gcmd):
        self._set_enabled(True)
        gcmd.respond_info("led_idle_manager: enabled for this session")

    cmd_LED_IDLE_MANAGER_STATUS_help = "Show LED idle manager runtime status"

    def cmd_LED_IDLE_MANAGER_STATUS(self, gcmd):
        # Status query should not change idle tracking, including follow-up
        # helper commands emitted by UI integrations.
        prev_activity = self.last_user_activity
        self.activity_suppressed_until = self.reactor.monotonic() + 1.0
        status = self.get_status(self.reactor.monotonic())
        seconds_to_off = status["seconds_to_off"]
        if seconds_to_off is None:
            seconds_to_off_text = "n/a"
        else:
            seconds_to_off_text = "%.2fs" % (seconds_to_off,)
        msg = (
            "led_idle_manager: enabled=%d printing=%d timeout=%.2fs idle_for=%.2fs "
            "seconds_to_off=%s desired_led_on=%s actual_led_on=%s"
            % (
                1 if status["enabled"] else 0,
                1 if status["printing"] else 0,
                status["timeout"],
                status["idle_for"],
                seconds_to_off_text,
                str(status["led_is_on"]),
                str(status["actual_led_is_on"]),
            )
        )
        gcmd.respond_info(msg)
        self.last_user_activity = prev_activity

    def get_status(self, eventtime):
        idle_for = max(0.0, eventtime - self.last_user_activity)
        printing = self._is_printing()
        if (not self.enabled) or printing:
            seconds_to_off = None
        else:
            seconds_to_off = max(0.0, self.timeout - idle_for)
        return {
            "enabled": self.enabled,
            "printing": printing,
            "timeout": self.timeout,
            "idle_for": idle_for,
            "seconds_to_off": seconds_to_off,
            "led_is_on": self.led_is_on,
            "actual_led_value": self._get_led_value(eventtime),
            "actual_led_is_on": self._get_led_is_on(eventtime),
            "last_user_brightness": self.last_user_brightness,
            "manually_off": self.manually_off,
            "ignore_commands": sorted(self.ignore_commands),
        }


def load_config(config):
    return LedIdleManager(config)
