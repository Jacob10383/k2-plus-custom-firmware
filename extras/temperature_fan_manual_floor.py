# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
# Add a manual speed floor to an existing temperature_fan object.
#
# This keeps temperature_fan as the only owner of the physical fan pin while
# allowing a separate command path, such as M106 P3, to request filtration.
import logging

MAX_FAN_TIME = 5.0
MANUAL_UPDATE_DELAY = 0.100
CURVE_HYSTERESIS = 0.2


def _klog(msg, *args, level=logging.info):
    level("temperature_fan_manual_floor: " + msg, *args)


class TemperatureFanManualFloor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.temperature_fan_name = config.get("temperature_fan", self.name)
        self.status_key = config.get("status_key", "manual_speed")
        self.min_update_delta = config.getfloat(
            "min_update_delta", 0.05, minval=0.0, maxval=1.0
        )
        self.manual_speed = 0.0
        self.auto_stage = 0.0
        self.last_auto_speed = 0.0
        self.last_effective_speed = 0.0
        self.last_raw_speed = 0.0
        self.temperature_fan = None
        self._original_set_speed = None
        self._original_get_status = None

        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command(
            "SET_TEMPERATURE_FAN_MANUAL_SPEED",
            "TEMPERATURE_FAN",
            self.temperature_fan_name,
            self.cmd_SET_TEMPERATURE_FAN_MANUAL_SPEED,
            desc=self.cmd_SET_TEMPERATURE_FAN_MANUAL_SPEED_help,
        )
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        obj_name = "temperature_fan %s" % (self.temperature_fan_name,)
        self.temperature_fan = self.printer.lookup_object(obj_name)
        control_type = self.temperature_fan.control.get_type()
        if control_type != "watermark":
            raise self.printer.config_error(
                "%s requires control: watermark on [%s]; change it back to "
                "watermark or remove [temperature_fan_manual_floor %s]"
                % (self.name, obj_name, self.name)
            )
        self.new_api = hasattr(self.temperature_fan, "set_tf_speed")
        if self.new_api:
            self._original_set_speed = self.temperature_fan.set_tf_speed
            self.temperature_fan.set_tf_speed = self._set_speed
        else:
            self._original_set_speed = self.temperature_fan.set_speed
            self.temperature_fan.set_speed = self._set_speed
        self._original_get_status = self.temperature_fan.get_status
        self.temperature_fan.get_status = self._get_temperature_fan_status
        _klog("%s: manual floor attached to %s", self.name, obj_name)

    def _auto_speed(self, read_time, value):
        temp_fan = self.temperature_fan
        if value <= 0.0 or temp_fan.target_temp <= 0.0:
            self.auto_stage = 0.0
            return 0.0

        current_temp, target_temp = temp_fan.get_temp(read_time)
        diff = current_temp - target_temp

        if not self.auto_stage:
            if diff <= 1.0:
                self.auto_stage = 0.6
            elif diff <= 2.0:
                self.auto_stage = 0.8
            else:
                self.auto_stage = 1.0
        elif self.auto_stage == 0.6:
            if diff > 2.0 + CURVE_HYSTERESIS:
                self.auto_stage = 1.0
            elif diff > 1.0 + CURVE_HYSTERESIS:
                self.auto_stage = 0.8
        elif self.auto_stage == 0.8:
            if diff < 1.0 - CURVE_HYSTERESIS:
                self.auto_stage = 0.6
            elif diff > 2.0 + CURVE_HYSTERESIS:
                self.auto_stage = 1.0
        else:
            if diff < 1.0 - CURVE_HYSTERESIS:
                self.auto_stage = 0.6
            elif diff < 2.0 - CURVE_HYSTERESIS:
                self.auto_stage = 0.8

        return max(
            temp_fan.min_speed,
            min(temp_fan.get_max_speed(), self.auto_stage),
        )

    def _effective_speed(self, auto_speed):
        manual_speed = min(
            self.manual_speed, self.temperature_fan.get_max_speed()
        )
        return max(auto_speed, manual_speed)

    def _set_speed(self, read_time, value):
        temp_fan = self.temperature_fan
        self.last_raw_speed = value
        auto_speed = self._auto_speed(read_time, value)
        effective_speed = self._effective_speed(auto_speed)
        self.last_auto_speed = auto_speed
        self.last_effective_speed = effective_speed

        suppress_small_update = (
            read_time < temp_fan.next_speed_time
            or not temp_fan.last_speed_value
        )
        if (
            suppress_small_update
            and abs(effective_speed - temp_fan.last_speed_value)
            < self.min_update_delta
        ):
            return

        speed_time = read_time + temp_fan.speed_delay
        temp_fan.next_speed_time = speed_time + 0.75 * MAX_FAN_TIME
        temp_fan.last_speed_value = effective_speed
        if self.new_api:
            temp_fan.fan.set_speed(effective_speed, speed_time)
        else:
            temp_fan.fan.set_speed(speed_time, effective_speed)

    def _apply_current_speed(self):
        reactor = self.printer.get_reactor()
        curtime = reactor.monotonic()
        speed_time = (
            self.temperature_fan.fan.get_mcu().estimated_print_time(curtime)
            + MANUAL_UPDATE_DELAY
        )
        auto_speed = self._auto_speed(curtime, self.last_raw_speed)
        effective_speed = self._effective_speed(auto_speed)
        self.last_auto_speed = auto_speed
        self.last_effective_speed = effective_speed
        self.temperature_fan.next_speed_time = (
            speed_time + 0.75 * MAX_FAN_TIME
        )
        self.temperature_fan.last_speed_value = effective_speed
        if self.new_api:
            self.temperature_fan.fan.set_speed(effective_speed, speed_time)
        else:
            self.temperature_fan.fan.set_speed(speed_time, effective_speed)

    cmd_SET_TEMPERATURE_FAN_MANUAL_SPEED_help = (
        "Set manual speed floor for a temperature_fan"
    )

    def cmd_SET_TEMPERATURE_FAN_MANUAL_SPEED(self, gcmd):
        speed = gcmd.get_float("SPEED", minval=0.0, maxval=1.0)
        self.manual_speed = speed
        if self.temperature_fan is not None:
            self._apply_current_speed()

    def _get_temperature_fan_status(self, eventtime):
        status = self._original_get_status(eventtime)
        status[self.status_key] = self.manual_speed
        status["auto_speed"] = self.last_auto_speed
        status["effective_speed"] = self.last_effective_speed
        return status

    def get_status(self, eventtime):
        return {
            "temperature_fan": self.temperature_fan_name,
            self.status_key: self.manual_speed,
            "auto_speed": self.last_auto_speed,
            "effective_speed": self.last_effective_speed,
        }


def load_config_prefix(config):
    return TemperatureFanManualFloor(config)
