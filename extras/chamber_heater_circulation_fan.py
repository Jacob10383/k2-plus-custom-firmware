# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
# Chamber heater circulation fan control based on heater pwm

from . import fan

PIN_MIN_TIME = 0.100
POLL_INTERVAL = 1.0
HEATER_PWM_THRESHOLD = 0.01


class ChamberHeaterCirculationFan:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.printer.load_object(config, "heaters")
        self.print_stats = self.printer.load_object(config, "print_stats")
        self.heater_name = config.get("heater", "chamber_heater")
        self.fluidd_alias = config.get(
            "fluidd_alias", "heater_fan %s" % (self.name,)
        )
        self.preheat_speed = config.getfloat(
            "preheat_speed", 1.0, minval=0.0, maxval=1.0
        )
        self.printing_speed = config.getfloat(
            "printing_speed", 0.30, minval=0.0, maxval=1.0
        )
        self.fan = fan.Fan(config, default_shutdown_speed=0.0)
        self.heater = None
        self.last_speed = 0.0
        if self.fluidd_alias:
            self.printer.add_object(self.fluidd_alias, self)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        pheaters = self.printer.lookup_object("heaters")
        self.heater = pheaters.lookup_heater(self.heater_name)
        import inspect
        sig = inspect.signature(self.fan.set_speed)
        self.new_api = list(sig.parameters.keys())[0] == "value"
        reactor = self.printer.get_reactor()
        reactor.register_timer(
            self._callback, reactor.monotonic() + PIN_MIN_TIME
        )

    def _actual_printing(self, eventtime):
        status = self.print_stats.get_status(eventtime)
        return (
            status.get("state") == "printing"
            and status.get("print_duration", 0.0) > 0.0
        )

    def _callback(self, eventtime):
        if self.printer.is_shutdown():
            return self.printer.get_reactor().NEVER
        heater_status = self.heater.get_status(eventtime)
        power = heater_status.get("power", 0.0)

        speed = 0.0
        if power > HEATER_PWM_THRESHOLD:
            if self._actual_printing(eventtime):
                speed = self.printing_speed
            else:
                speed = self.preheat_speed

        if speed != self.last_speed:
            self.last_speed = speed
            curtime = self.printer.get_reactor().monotonic()
            print_time = self.fan.get_mcu().estimated_print_time(curtime)
            if self.new_api:
                self.fan.set_speed(speed, print_time + PIN_MIN_TIME)
            else:
                self.fan.set_speed(print_time + PIN_MIN_TIME, speed)
        return eventtime + POLL_INTERVAL

    def get_status(self, eventtime):
        return self.fan.get_status(eventtime)


def load_config_prefix(config):
    return ChamberHeaterCirculationFan(config)
