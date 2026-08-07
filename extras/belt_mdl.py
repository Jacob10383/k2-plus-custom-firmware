# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
# Automatic K2 Plus belt tension modules


from statistics import median

from extras.serial_485 import build_485_body


FRAME_HEAD = 0xF7
MODULE_ADDRESSES = {"x": 0x21, "y": 0x22}
MODULE_ALIASES = {"x": "x", "y": "y", "mdlx": "x", "mdly": "y"}
UNINITIALIZED_WORD = b"\xff\xff\xff\xff"
FLASH_WORDS = 3
MAX_MOVE_UNITS = 0xFFFE
MIN_CALIBRATION_ADC_SPAN = 50000
RECOMMENDED_CALIBRATION_ADC_SPAN = 100000
SETTLE_CENTER = 175.0
SETTLE_ENVELOPE = 350.0


def _pack_u32(value):
    return int(value).to_bytes(4, "big", signed=False)


def _pack_i32(value):
    return int(value).to_bytes(4, "big", signed=True)


def _unpack_u32(data):
    if len(data) != 4:
        raise ValueError("expected one 32-bit word")
    return int.from_bytes(data, "big", signed=False)


def _unpack_i32(data):
    if len(data) != 4:
        raise ValueError("expected one 32-bit word")
    return int.from_bytes(data, "big", signed=True)


def calibration_line(low_adc, high_adc, low_tension, high_tension):
    if not low_adc or not high_adc:
        raise ValueError("calibration ADC anchors must be nonzero")
    if abs(high_adc - low_adc) < MIN_CALIBRATION_ADC_SPAN:
        raise ValueError(
            "calibration ADC span %d is below required %d"
            % (abs(high_adc - low_adc), MIN_CALIBRATION_ADC_SPAN)
        )
    slope = (high_tension - low_tension) / (high_adc - low_adc)
    return slope, low_tension - slope * low_adc


class BeltProtocolError(Exception):
    pass


class BeltProtocol:
    READ_VERSION = 0x00
    READ_FLASH = 0x02
    WRITE_FLASH = 0x04
    READ_ADC = 0x06
    MOVE = 0x08
    MOVE_SLIDER = MOVE

    def __init__(self, axis="x"):
        self.axis = None
        self.addr = None
        self.set_module(axis)

    def set_module(self, module_name):
        axis = MODULE_ALIASES.get(str(module_name).lower())
        if axis is None:
            raise ValueError("belt axis must be X or Y")
        self.axis = axis
        self.addr = MODULE_ADDRESSES[axis]

    def build(self, function, payload=b""):
        return build_485_body(self.addr, function, payload)

    def decode(self, raw, expected_function=None):
        if raw is None:
            raise BeltProtocolError("no response")
        frame = bytes(raw)
        if len(frame) < 6 or frame[0] != FRAME_HEAD:
            raise BeltProtocolError("malformed response")
        if frame[1] != self.addr:
            raise BeltProtocolError(
                "response address 0x%02x does not match 0x%02x"
                % (frame[1], self.addr)
            )
        if len(frame) != frame[2] + 3:
            raise BeltProtocolError("response length mismatch")
        if frame[3] != 0:
            raise BeltProtocolError("device status 0x%02x" % (frame[3],))
        if expected_function is not None and frame[4] != expected_function:
            raise BeltProtocolError(
                "response function 0x%02x does not match 0x%02x"
                % (frame[4], expected_function)
            )
        return frame[5:-1]


class BeltMdl:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        section_name = config.get_name().split()[-1].lower()
        self.axis = MODULE_ALIASES.get(section_name)
        if self.axis is None:
            raise config.error(
                "belt_mdl section suffix must be x or y, not %s"
                % (section_name,)
            )

        self.protocol = BeltProtocol(self.axis)
        self._serial = self.printer.lookup_object("serial_485 serial485")
        self.target_tension = config.getfloat(
            "target_tension", 140.0, above=0.0)
        self.tolerance = config.getfloat(
            "tension_tolerance", 0.02, above=0.0, below=1.0)
        self.low_tension = config.getfloat(
            "calibration_low_tension", 140.0)
        self.high_tension = config.getfloat(
            "calibration_high_tension", 160.0,
            above=self.low_tension)
        self.tighten_direction = config.getint(
            "tighten_direction", 1, minval=0, maxval=1)
        self.control_gain = config.getfloat(
            "control_gain", 1.0, above=0.0)
        self.max_adjust_step = config.getint(
            "max_adjust_step", 12, minval=1, maxval=MAX_MOVE_UNITS)
        self.max_adjustments = config.getint(
            "max_adjustments", 200, minval=1)
        self.settle_rounds = config.getint(
            "settle_rounds", 2, minval=1)
        self.settle_distance = config.getfloat(
            "settle_distance", 50.0, minval=0.0)
        self.settle_speed = config.getfloat(
            "settle_speed", 60.0, above=0.0)
        self.settle_margin = config.getfloat(
            "settle_margin", 5.0, minval=0.0)
        self.relax_time = config.getfloat(
            "relax_time", 0.25, minval=0.0)
        self.move_seconds_per_unit = config.getfloat(
            "move_seconds_per_unit", 0.007, minval=0.0065536)
        self.post_move_settle = config.getfloat(
            "post_move_settle", 0.15, minval=0.0)
        self.adc_samples = config.getint(
            "adc_samples", 3, minval=1)
        self.adc_sample_interval = config.getfloat(
            "adc_sample_interval", 0.12, minval=0.0)
        self.serial_timeout = config.getfloat(
            "serial_timeout", 1.0, above=0.0)

        self.position = None
        self.low_adc = None
        self.high_adc = None
        self.current_adc = None
        self.current_tension = None
        self.hardware_version = None
        self.software_version = None
        self._pending_low_adc = None

    def _error(self, message):
        return self.printer.command_error(
            "belt tension %s: %s" % (self.axis.upper(), message))

    def _pause(self, delay):
        if delay <= 0.0:
            return
        reactor = self.printer.get_reactor()
        reactor.pause(reactor.monotonic() + delay)

    def _request(self, function, payload=b""):
        try:
            raw = self._serial.cmd_send_data_with_response(
                self.protocol.build(function, payload), self.serial_timeout)
            return self.protocol.decode(raw, function)
        except BeltProtocolError as exc:
            raise self._error(str(exc))

    def _read_version(self):
        payload = self._request(BeltProtocol.READ_VERSION)
        if len(payload) != 4:
            raise self._error(
                "version reply is %d bytes, expected 4" % (len(payload),))
        self.hardware_version = tuple(payload[:2])
        self.software_version = tuple(payload[2:])
        return payload

    def _read_flash(self):
        payload = self._request(BeltProtocol.READ_FLASH, bytes([FLASH_WORDS]))
        if len(payload) != 13 or payload[0] != FLASH_WORDS:
            raise self._error("invalid three-word flash reply")
        words = (payload[1:5], payload[5:9], payload[9:13])
        self.position = (
            None if words[0] == UNINITIALIZED_WORD else _unpack_u32(words[0]))
        self.low_adc = (
            None if words[1] == UNINITIALIZED_WORD else _unpack_i32(words[1]))
        self.high_adc = (
            None if words[2] == UNINITIALIZED_WORD else _unpack_i32(words[2]))
        return self.position, self.low_adc, self.high_adc

    def _write_flash(self):
        if self.position is None:
            raise self._error("cannot write a missing position")
        payload = (
            bytes([FLASH_WORDS])
            + _pack_u32(self.position)
            + (UNINITIALIZED_WORD
               if self.low_adc is None else _pack_i32(self.low_adc))
            + (UNINITIALIZED_WORD
               if self.high_adc is None else _pack_i32(self.high_adc))
        )
        reply = self._request(BeltProtocol.WRITE_FLASH, payload)
        if reply != payload:
            raise self._error("flash readback does not match written values")

    def _calibration(self):
        if self.low_adc is None or self.high_adc is None:
            raise self._error(
                "not calibrated; capture CALIBRATE POINT=LOW and POINT=HIGH")
        try:
            return calibration_line(
                self.low_adc, self.high_adc,
                self.low_tension, self.high_tension)
        except ValueError as exc:
            raise self._error(str(exc))

    def _read_adc_once(self):
        payload = self._request(BeltProtocol.READ_ADC)
        if len(payload) != 4:
            raise self._error(
                "ADC reply is %d bytes, expected 4" % (len(payload),))
        return _unpack_i32(payload)

    def _read_adc(self):
        samples = []
        for index in range(self.adc_samples):
            if index:
                self._pause(self.adc_sample_interval)
            samples.append(self._read_adc_once())
        self.current_adc = int(median(samples))
        return self.current_adc

    def _read_tension(self):
        slope, intercept = self._calibration()
        self.current_tension = self._read_adc() * slope + intercept
        return self.current_tension

    def _move(self, direction, distance):
        distance = int(distance)
        if distance == 0:
            return
        if direction not in (0, 1) or not 1 <= distance <= MAX_MOVE_UNITS:
            raise self._error("invalid move direction or distance")
        payload = bytes([direction]) + _pack_u32(distance)
        reply = self._request(BeltProtocol.MOVE, payload)
        if reply != payload:
            raise self._error("move acknowledgement does not match request")
        # Firmware replies before starting and has no completion message.
        self._pause(
            distance * self.move_seconds_per_unit + self.post_move_settle)

    def _move_to(self, target):
        if self.position is None:
            raise self._error("position is uninitialized; run BELT_TENSION_ZERO")
        target = int(target)
        if not 0 <= target <= 0xFFFFFFFF:
            raise self._error("position is outside unsigned 32-bit storage")
        delta = target - self.position
        if not delta:
            return
        direction = (
            self.tighten_direction if delta > 0 else 1 - self.tighten_direction)
        self._move(direction, abs(delta))
        self.position = target

    def _release_motors(self):
        self.printer.lookup_object("stepper_enable").motor_off()
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        self._pause(self.relax_time)

    def _prepare_settle(self):
        self.gcode.run_script_from_command("G28 X Y")
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.manual_move(
            [SETTLE_CENTER, SETTLE_CENTER], self.settle_speed)
        toolhead.wait_moves()
        headroom = min(
            SETTLE_CENTER, SETTLE_ENVELOPE - SETTLE_CENTER)
        return max(
            0.0, min(self.settle_distance, headroom - self.settle_margin))

    def _force_settle_and_release(self, distance):
        try:
            for stepper, amount in (
                    ("stepper_x", -distance),
                    ("stepper_y", distance),
                    ("stepper_x", distance * 2.0),
                    ("stepper_y", -distance * 2.0),
                    ("stepper_x", -distance),
                    ("stepper_y", distance)):
                if amount:
                    self.gcode.run_script_from_command(
                        "FORCE_MOVE STEPPER=%s DISTANCE=%.6f VELOCITY=%.6f"
                        % (stepper, amount, self.settle_speed))
            self.printer.lookup_object("toolhead").wait_moves()
        finally:
            self._release_motors()

    def _adjust_tension(self, target):
        moved = False
        tightened = 0
        loosened = 0
        step_limit = self.max_adjust_step
        last_direction = None
        try:
            for _ in range(self.max_adjustments):
                tension = self._read_tension()
                error = target - tension
                if abs(error) <= target * self.tolerance:
                    return tension, tightened, loosened
                direction = (
                    self.tighten_direction
                    if error > 0 else 1 - self.tighten_direction
                )
                if last_direction is not None and direction != last_direction:
                    step_limit = max(1, step_limit // 2)
                steps = max(1, min(
                    step_limit, int(round(abs(error) * self.control_gain))))
                position_delta = steps if direction == self.tighten_direction else -steps
                next_position = self.position + position_delta
                if not 0 <= next_position <= 0xFFFFFFFF:
                    raise self._error(
                        "target needs movement beyond unsigned position storage")
                self._move(direction, steps)
                self.position = next_position
                if position_delta > 0:
                    tightened += steps
                else:
                    loosened += steps
                moved = True
                last_direction = direction
            raise self._error(
                "failed to reach %.3f within %d adjustments"
                % (target, self.max_adjustments))
        except Exception:
            if moved:
                self._write_flash()
            raise

    def _format_status(self):
        calibrated = False
        if self.low_adc is not None and self.high_adc is not None:
            try:
                calibration_line(
                    self.low_adc, self.high_adc,
                    self.low_tension, self.high_tension)
                calibrated = True
            except ValueError:
                pass
        return (
            "axis=%s hw=%s sw=%s position=%s adc=%s tension=%s "
            "low_adc=%s high_adc=%s calibration_span=%s calibrated=%s"
            % (
                self.axis.upper(),
                self.hardware_version,
                self.software_version,
                self.position,
                self.current_adc,
                self.current_tension,
                self.low_adc,
                self.high_adc,
                (None if self.low_adc is None or self.high_adc is None
                 else abs(self.high_adc - self.low_adc)),
                calibrated,
            )
        )

    def _refresh_status(self):
        self._read_version()
        self._read_flash()
        self.current_adc = self._read_adc()
        self.current_tension = None
        if self.low_adc is not None and self.high_adc is not None:
            try:
                slope, intercept = calibration_line(
                    self.low_adc, self.high_adc,
                    self.low_tension, self.high_tension)
                self.current_tension = self.current_adc * slope + intercept
            except ValueError:
                pass

    def cmd_BELT_TENSION_SET(self, gcmd):
        target = gcmd.get_float("TENSION", self.target_tension, above=0.0)
        self._read_flash()
        self._calibration()
        if self.position is None:
            raise self._error("stored position is missing")
        starting_position = self.position
        tightened = 0
        loosened = 0
        try:
            distance = self._prepare_settle()
            for _ in range(self.settle_rounds):
                self._force_settle_and_release(distance)
                _, round_tightened, round_loosened = self._adjust_tension(
                    target)
                tightened += round_tightened
                loosened += round_loosened
        except Exception:
            if self.position != starting_position:
                self._write_flash()
            raise
        self._write_flash()
        gcmd.respond_info(
            "belt tension %s: final=%.3f target=%.3f tightened=%d "
            "loosened=%d net=%+d position=%d->%d "
            "(motors released; home before motion)"
            % (
                self.axis.upper(), self.current_tension, target,
                tightened, loosened, tightened - loosened,
                starting_position, self.position,
            ))

    def cmd_BELT_TENSION_MOVE_TO(self, gcmd):
        target = gcmd.get_int(
            "POSITION", minval=0, maxval=0xFFFFFFFF)
        self._read_flash()
        self._move_to(target)
        self._write_flash()
        gcmd.respond_info(
            "belt tension %s: position=%d"
            % (self.axis.upper(), self.position))

    def cmd_BELT_TENSION_MOVE_BY(self, gcmd):
        distance = gcmd.get_int(
            "DISTANCE", minval=-MAX_MOVE_UNITS, maxval=MAX_MOVE_UNITS)
        update_position = gcmd.get_int(
            "UPDATE_POSITION", 1, minval=0, maxval=1)
        if update_position:
            self._read_flash()
            if self.position is None:
                raise self._error(
                    "position is uninitialized; run BELT_TENSION_ZERO")
            starting_position = self.position
            self._move_to(self.position + distance)
            self._write_flash()
            gcmd.respond_info(
                "belt tension %s: moved by=%d position=%d->%d"
                % (self.axis.upper(), distance,
                   starting_position, self.position))
            return
        direction = (
            self.tighten_direction
            if distance >= 0 else 1 - self.tighten_direction)
        self._move(direction, abs(distance))
        gcmd.respond_info(
            "belt tension %s: moved by=%d (stored position unchanged)"
            % (self.axis.upper(), distance))

    def cmd_BELT_TENSION_CALIBRATE(self, gcmd):
        point = gcmd.get("POINT").strip().upper()
        if point == "LOW":
            self._read_flash()
            self._release_motors()
            self.position = 0
            self._pending_low_adc = self._read_adc()
            gcmd.respond_info(
                "belt tension %s: LOW adc=%d captured; install the printed "
                "calibration jig, then capture POINT=HIGH"
                % (self.axis.upper(), self._pending_low_adc))
            return
        if point != "HIGH":
            raise gcmd.error("POINT must be LOW or HIGH")
        if self._pending_low_adc is None:
            raise gcmd.error(
                "capture POINT=LOW before installing the calibration jig")
        self._release_motors()
        high_adc = self._read_adc()
        try:
            calibration_line(
                self._pending_low_adc, high_adc,
                self.low_tension, self.high_tension)
        except ValueError as exc:
            raise self._error(str(exc))
        span = abs(high_adc - self._pending_low_adc)
        if span < RECOMMENDED_CALIBRATION_ADC_SPAN:
            gcmd.respond_info(
                "WARNING: calibration ADC span %d was accepted but is below "
                "Creality's recommended %d"
                % (span, RECOMMENDED_CALIBRATION_ADC_SPAN))
        self.low_adc = self._pending_low_adc
        self.high_adc = high_adc
        self.position = 0
        self._write_flash()
        self._pending_low_adc = None
        self.current_tension = self.high_tension
        gcmd.respond_info(
            "belt tension %s: calibration saved low_adc=%d high_adc=%d; "
            "remove the jig and home before motion"
            % (self.axis.upper(), self.low_adc, self.high_adc))

    def cmd_BELT_TENSION_ZERO(self, gcmd):
        self._read_flash()
        self.position = 0
        self._write_flash()
        gcmd.respond_info(
            "belt tension %s: position=0" % (self.axis.upper(),))

    def get_status(self, _eventtime):
        return {
            "axis": self.axis,
            "position": self.position,
            "adc": self.current_adc,
            "tension": self.current_tension,
            "calibration_low_adc": self.low_adc,
            "calibration_high_adc": self.high_adc,
        }


class BeltCommands:
    COMMANDS = (
        ("BELT_TENSION_SET", "cmd_BELT_TENSION_SET",
         "Set AXIS=X|Y to TENSION=<value>"),
        ("BELT_TENSION_MOVE_TO", "cmd_BELT_TENSION_MOVE_TO",
         "Move AXIS=X|Y to stored POSITION=<absolute>"),
        ("BELT_TENSION_MOVE_BY", "cmd_BELT_TENSION_MOVE_BY",
         "Move AXIS=X|Y by DISTANCE=<signed>; UPDATE_POSITION=0 bypasses bookkeeping"),
        ("BELT_TENSION_CALIBRATE", "cmd_BELT_TENSION_CALIBRATE",
         "Calibrate AXIS=X|Y at POINT=LOW|HIGH"),
        ("BELT_TENSION_ZERO", "cmd_BELT_TENSION_ZERO",
         "Set AXIS=X|Y position bookkeeping to zero"),
    )

    def __init__(self, printer):
        self.printer = printer
        self.modules = {}
        gcode = printer.lookup_object("gcode")
        gcode.register_command(
            "BELT_TENSION_STATUS", self.cmd_BELT_TENSION_STATUS,
            desc="Report live status for AXIS=X|Y or both axes")
        for command, method, desc in self.COMMANDS:
            def handler(gcmd, method=method):
                return getattr(self._module(gcmd), method)(gcmd)
            gcode.register_command(command, handler, desc=desc)

    def _module(self, gcmd):
        axis = gcmd.get("AXIS").strip().lower()
        module = self.modules.get(axis)
        if module is None:
            raise gcmd.error("AXIS must be X or Y")
        return module

    def cmd_BELT_TENSION_STATUS(self, gcmd):
        axis = gcmd.get("AXIS", None)
        if axis is None:
            modules = [self.modules[key] for key in sorted(self.modules)]
        else:
            modules = [self._module(gcmd)]
        if not modules:
            raise gcmd.error("no belt tension modules are configured")
        for module in modules:
            module._refresh_status()
        gcmd.respond_info(
            "\n".join(module._format_status() for module in modules))


def load_config_prefix(config):
    printer = config.get_printer()
    module = BeltMdl(config)
    commands = printer.lookup_object("belt_tension_commands", None)
    if commands is None:
        commands = BeltCommands(printer)
        printer.add_object("belt_tension_commands", commands)
    commands.modules[module.axis] = module
    return module
