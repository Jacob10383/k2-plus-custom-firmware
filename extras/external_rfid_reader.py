# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Klipper integration for the K2 standalone RFID spool reader.

Latched PA5 notifications fetch tag records for ``Box``. Successful scans also
sound the K2 buzzer when enabled.
"""

import errno
import logging
import os
import threading
import time

from extras.serial_485 import build_485_body


READER_ADDR = 0x11
CMD_GET_RECORD = 0x02
RECORD_SIZE = 0x28
READ_ATTEMPTS = 2
REQUEST_TIMEOUT = 0.5
NOT_PIN = "!PA5"

BEEP_DURATION = 0.2
BEEP_FREQUENCY = 4000
BEEP_PWM_CHIP = "/sys/class/pwm/pwmchip0"
BEEP_PWM_CHANNEL = 6
BEEP_PINMUX_SELECT = (
    "/sys/kernel/debug/pinctrl/2000000.pinctrl/pinmux-select")
BEEP_PINMUX = "PF4 pwm6"

RECORD_FIELDS = (
    ("month", 0x00, 1),
    ("day", 0x01, 2),
    ("year", 0x03, 2),
    ("supplier", 0x05, 4),
    ("batch", 0x09, 2),
    ("mat_id", 0x0B, 6),
    ("color", 0x11, 7),
    ("len", 0x18, 4),
    ("number", 0x1C, 6),
    ("reserve", 0x22, 6),
)


def _ascii(data):
    return bytes(data).decode("ascii", errors="replace").rstrip("\x00")


def parse_reader_response(response):
    """Decode a transport-validated cached-record response."""
    if not response:
        return None
    frame = bytes(response)
    if (len(frame) < 6 or frame[0] != 0xF7 or frame[1] != READER_ADDR
            or len(frame) != frame[2] + 3 or frame[3] != 0
            or frame[4] != CMD_GET_RECORD):
        return None

    payload = frame[5:-1]
    if payload == b"unknown":
        return {"state": "unknown", "fields": {}}
    if len(payload) != RECORD_SIZE:
        return None

    return {
        "state": "record",
        "record_hex": payload.hex(),
        "record_ascii": _ascii(payload),
        "fields": {
            name: _ascii(payload[offset:offset + size])
            for name, offset, size in RECORD_FIELDS
        },
    }


def _klog(msg, *args, level=logging.info):
    level("external_rfid_reader: " + msg, *args)

class ExternalRfidReader:
    CONSOLE_PREFIX = "[RFID READER]: "

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.beep_enabled = config.getboolean("beep_enabled", True)

        self._serial = None
        self._reading = False
        self._last_record = None
        self._last_error = None
        self._beep_lock = threading.Lock()

        for event in ("klippy:disconnect", "klippy:shutdown"):
            self.printer.register_event_handler(event, self._handle_disconnect)
        self.printer.register_event_handler(
            "serial_485:ready", self._handle_serial_ready)

        buttons = self.printer.load_object(config, "buttons")
        buttons.register_buttons([NOT_PIN], self._button_handler)
        self.gcode.register_command(
            "RFID_READER_READ", self.cmd_read,
            desc="Fetch the cached RFID record")

    def _handle_serial_ready(self):
        self._serial = self.printer.lookup_object("serial_485 serial485")

    def _handle_disconnect(self):
        self._serial = None

    def _request_record(self):
        if self._serial is None:
            raise RuntimeError("serial_485 is not ready")
        body = build_485_body(
            READER_ADDR, CMD_GET_RECORD, header_byte=0xFF)
        for _attempt in range(READ_ATTEMPTS):
            response = self._serial.cmd_send_data_with_response(
                body, REQUEST_TIMEOUT)
            parsed = parse_reader_response(response)
            if parsed is not None:
                return parsed
        return None

    def _read_record(self):
        if self._reading:
            return None
        self._reading = True
        try:
            try:
                parsed = self._request_record()
            except Exception as exc:
                self._last_error = str(exc)
                _klog(
                    'record fetch failed: %s', exc, level=logging.warning)
                return None
            if parsed is None:
                self._last_error = "no valid response"
                return None

            self._last_error = None
            if parsed["state"] == "record":
                self._publish_record(parsed)
            return parsed
        finally:
            self._reading = False

    def _publish_record(self, parsed):
        self._last_record = {
            "record_hex": parsed["record_hex"],
            "record_ascii": parsed["record_ascii"],
            "fields": dict(parsed["fields"]),
        }
        self.printer.send_event(
            "external_rfid_reader:record", dict(self._last_record))

    def _button_handler(self, _eventtime, state):
        if not state or self._serial is None:
            return
        parsed = self._read_record()
        if parsed and parsed["state"] == "record":
            self._start_beep()

    @staticmethod
    def _write_sysfs(path, value):
        with open(path, "w") as sysfs_file:
            sysfs_file.write(str(value))

    def _beep_worker(self):
        if not self._beep_lock.acquire(False):
            return
        pwm_dir = os.path.join(
            BEEP_PWM_CHIP, "pwm%d" % BEEP_PWM_CHANNEL)
        enable = os.path.join(pwm_dir, "enable")
        try:
            self._write_sysfs(BEEP_PINMUX_SELECT, BEEP_PINMUX)
            if not os.path.isdir(pwm_dir):
                try:
                    self._write_sysfs(
                        os.path.join(BEEP_PWM_CHIP, "export"),
                        BEEP_PWM_CHANNEL)
                except OSError as exc:
                    if exc.errno != errno.EBUSY:
                        raise
                for _unused in range(20):
                    if os.path.isdir(pwm_dir):
                        break
                    time.sleep(0.01)
            period = 1000000000 // BEEP_FREQUENCY
            duty = os.path.join(pwm_dir, "duty_cycle")
            self._write_sysfs(enable, 0)
            self._write_sysfs(duty, 0)
            self._write_sysfs(os.path.join(pwm_dir, "period"), period)
            self._write_sysfs(duty, period // 2)
            self._write_sysfs(enable, 1)
            time.sleep(BEEP_DURATION)
        except OSError as exc:
            _klog('PWM beep failed: %s', exc, level=logging.warning)
        finally:
            try:
                self._write_sysfs(enable, 0)
            except OSError:
                pass
            self._beep_lock.release()

    def _start_beep(self):
        if not self.beep_enabled:
            return
        threading.Thread(
            target=self._beep_worker,
            name="rfid-reader-beep",
            daemon=True,
        ).start()

    def cmd_read(self, gcmd):
        parsed = self._read_record()
        if parsed is None:
            message = "no valid response"
        elif parsed["state"] == "unknown":
            message = "unknown"
        else:
            fields = parsed["fields"]
            message = "record supplier=%s mat_id=%s color=%s reserve=%s" % (
                fields["supplier"], fields["mat_id"],
                fields["color"], fields["reserve"])
        gcmd.respond_info(self.CONSOLE_PREFIX + message)

    def get_status(self, _eventtime):
        return {
            "connected": self._serial is not None,
            "last_error": self._last_error,
            "record": None if self._last_record is None
            else dict(self._last_record),
        }


def load_config(config):
    return ExternalRfidReader(config)
