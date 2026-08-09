# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Typed request and response handling for the Creality CFS RS-485 protocol.

The decoders enforce firmware-backed envelopes, status sets, and payload
shapes; the driver classes expose those commands as small typed responses.
"""

import logging
import struct
from contextlib import contextmanager
from dataclasses import dataclass

from extras.serial_485 import build_485_body, check_485_frame_crc


DEFAULT_TIMEOUT = 1.0
POLL_TIMEOUT = 0.1
RFID_FORCE_SLOT_TIMEOUT = 190.0
BROADCAST_ADDRESS = 0xFE
UNIID_LENGTH = 12

CMD_RFID_RECORDS = 0x02
CMD_RFID_REMAINING = 0x03
CMD_TRACKING = 0x04
CMD_BUFFER = 0x05
CMD_SLOT_MASK = 0x08
CMD_BOX_STATE = 0x0A
CMD_RFID_CONTROL = 0x0D
CMD_ENCODER = 0x0E
CMD_LOAD = 0x10
CMD_UNLOAD = 0x11

AUTO_ASSIGN = 0xA0
AUTO_DISCOVER = 0xA1
AUTO_QUERY = 0xA2

STATUS_OK = 0x00
STATUS_INVALID_PARAM = 0x01
STATUS_BAD_CRC = 0x02
STATUS_BUSY = 0x03
STATUS_STAGE0_SENSOR_TIMEOUT = 0x05
STATUS_SLOT_EMPTY = 0x08
STATUS_STAGE0_ODOMETER_TIMEOUT = 0x09
STATUS_FEED_TIMEOUT = 0x0A
STATUS_OVERTRAVEL = 0x0B
STATUS_ODOMETER_STALLED = 0x0C
STATUS_BUFFER_NOT_FULL = 0x0D
STATUS_STAGE7_NO_MOTION = 0x0E
STATUS_UNLOAD_BUFFER_TIMEOUT = 0x13
STATUS_UNLOAD_HUB_CLEAR_TIMEOUT = 0x14
STATUS_UNLOAD_NO_FILAMENT = 0x16
STATUS_UNLOAD_INLET_CLEAR = 0x17
STATUS_UNLOAD_MOTOR_BLOCKED = 0x19
STATUS_UNLOAD_ODOMETER_TIMEOUT = 0x1A
STATUS_SLOT_EVENT = 0x30
STATUS_RUNOUT = 0x50
STATUS_BUFFER_REFILL_STALLED = 0x51
STATUS_BUFFER_REFILL_NO_MOTION = 0x52

BOX_STATE_IDLE = 0
BOX_STATE_PRELOAD = 1
BOX_STATE_PRINT = 2
BOX_STATE_RELOAD = 3
BOX_STATE_ERROR = 4
BOX_STATE_TEST = 5

WIRE_ERROR_STATUSES = frozenset((STATUS_INVALID_PARAM, STATUS_BAD_CRC))
QUERY_STATUSES = frozenset((STATUS_OK, STATUS_INVALID_PARAM, STATUS_BAD_CRC))
COMMAND_STATUSES = QUERY_STATUSES | frozenset((STATUS_BUSY,))
LOAD_STAGE_STATUSES = {
    0: COMMAND_STATUSES | frozenset((
        STATUS_STAGE0_SENSOR_TIMEOUT, STATUS_SLOT_EMPTY,
        STATUS_STAGE0_ODOMETER_TIMEOUT, STATUS_UNLOAD_HUB_CLEAR_TIMEOUT,
        STATUS_UNLOAD_INLET_CLEAR, STATUS_UNLOAD_MOTOR_BLOCKED,
        STATUS_UNLOAD_ODOMETER_TIMEOUT,
    )),
    4: QUERY_STATUSES,
    5: QUERY_STATUSES | frozenset((
        STATUS_FEED_TIMEOUT, STATUS_OVERTRAVEL, STATUS_ODOMETER_STALLED,
    )),
    6: QUERY_STATUSES | frozenset((STATUS_BUFFER_NOT_FULL,)),
    7: QUERY_STATUSES | frozenset((STATUS_STAGE7_NO_MOTION,)),
}
LOAD_STAGE_PAYLOAD_LENGTHS = {
    0: (1,), 4: (0,), 5: (4,), 6: (0,), 7: (0,),
}
LOAD_STAGE_STATUS_PAYLOAD_LENGTHS = {
    0: {
        STATUS_INVALID_PARAM: (0, 1),
        STATUS_BAD_CRC: (0,),
    },
}
UNLOAD_PHASE_STATUSES = {
    0: COMMAND_STATUSES | frozenset((
        STATUS_UNLOAD_BUFFER_TIMEOUT, STATUS_UNLOAD_NO_FILAMENT,
    )),
    1: COMMAND_STATUSES | frozenset((
        STATUS_UNLOAD_HUB_CLEAR_TIMEOUT, STATUS_UNLOAD_NO_FILAMENT,
        STATUS_UNLOAD_INLET_CLEAR, STATUS_UNLOAD_MOTOR_BLOCKED,
        STATUS_UNLOAD_ODOMETER_TIMEOUT,
    )),
}
RFID_FORCE_STATUSES = UNLOAD_PHASE_STATUSES[1]

RFID_SLOT_NAMES = ("A", "B", "C", "D")
RFID_FALLBACK_RECORDS = frozenset(("", "busy", "none", "unknown"))
RFID_RECORD_FIELDS = (
    ("month", 0x00, 1), ("day", 0x01, 2), ("year", 0x03, 2),
    ("supplier", 0x05, 4), ("batch", 0x09, 2), ("mat_id", 0x0B, 6),
    ("color", 0x11, 7), ("len", 0x18, 4), ("number", 0x1C, 6),
    ("reserve", 0x22, 6),
)

STATUS_NAMES = {
    STATUS_OK: "OK",
    STATUS_INVALID_PARAM: "INVALID_PARAM",
    STATUS_BAD_CRC: "BAD_CRC",
    STATUS_BUSY: "BUSY",
    STATUS_STAGE0_SENSOR_TIMEOUT: "STAGE0_SENSOR_TIMEOUT",
    STATUS_SLOT_EMPTY: "SLOT_EMPTY",
    STATUS_STAGE0_ODOMETER_TIMEOUT: "STAGE0_ODOMETER_TIMEOUT",
    STATUS_FEED_TIMEOUT: "FEED_TIMEOUT",
    STATUS_OVERTRAVEL: "OVERTRAVEL",
    STATUS_ODOMETER_STALLED: "ODOMETER_STALLED",
    STATUS_BUFFER_NOT_FULL: "BUFFER_NOT_FULL",
    STATUS_STAGE7_NO_MOTION: "STAGE7_NO_MOTION",
    STATUS_UNLOAD_BUFFER_TIMEOUT: "UNLOAD_BUFFER_TIMEOUT",
    STATUS_UNLOAD_HUB_CLEAR_TIMEOUT: "UNLOAD_HUB_CLEAR_TIMEOUT",
    STATUS_UNLOAD_NO_FILAMENT: "UNLOAD_NO_FILAMENT",
    STATUS_UNLOAD_INLET_CLEAR: "UNLOAD_INLET_CLEAR",
    STATUS_UNLOAD_MOTOR_BLOCKED: "UNLOAD_MOTOR_BLOCKED",
    STATUS_UNLOAD_ODOMETER_TIMEOUT: "UNLOAD_ODOMETER_TIMEOUT",
    STATUS_SLOT_EVENT: "SLOT_EVENT",
    STATUS_RUNOUT: "RUNOUT",
    STATUS_BUFFER_REFILL_STALLED: "BUFFER_REFILL_STALLED",
    STATUS_BUFFER_REFILL_NO_MOTION: "BUFFER_REFILL_NO_MOTION",
}
STATE_NAMES = {
    BOX_STATE_IDLE: "IDLE",
    BOX_STATE_PRELOAD: "PRELOAD",
    BOX_STATE_PRINT: "PRINT",
    BOX_STATE_RELOAD: "RELOAD",
    BOX_STATE_ERROR: "ERROR",
    BOX_STATE_TEST: "TEST",
}


def status_name(value):
    return "NO_RESPONSE" if value is None else STATUS_NAMES.get(value, "UNKNOWN")


def state_name(value):
    return "NO_RESPONSE" if value is None else STATE_NAMES.get(value, "UNKNOWN")


def _klog(msg, *args, level=logging.info):
    level("box_protocol: " + msg, *args)

class ProtocolError(ValueError):
    """The peer response violates its firmware wire contract."""


@dataclass(frozen=True)
class Reply:
    address: int
    command: int
    status: int
    payload: bytes
    raw: bytes


@dataclass(frozen=True)
class ScalarReply(Reply):
    value: object


@dataclass(frozen=True)
class BoxStateReply(Reply):
    temp_c: object
    humidity_pct: object
    box_state: object
    downstream_mask: object
    slot_events: object


@dataclass(frozen=True)
class RfidRecordsReply(Reply):
    records: dict
    fields: dict


@dataclass(frozen=True)
class RfidRemainingReply(Reply):
    values: dict


@dataclass(frozen=True)
class AutoAddressReply:
    uniid: bytes


def _protocol_error(message, reply=None, context=None):
    if reply is not None:
        context_text = "" if context is None else " context=%s" % context
        _klog(
            'rejected response%s address=%d command=0x%02x status=0x%02x payload_len=%d payload=%s frame=%s error=%s',
            context_text, reply.address, reply.command, reply.status,
            len(reply.payload), reply.payload.hex(), reply.raw.hex(), message,
            level=logging.warning)
        message = "%s (cmd=0x%02x status=0x%02x)" % (
            message, reply.command, reply.status)
    raise ProtocolError(message)


def _byte(value, name):
    if type(value) is not int or not 0 <= value <= 0xFF:
        raise ValueError("%s must be a byte" % name)
    return value


def _box_address(address):
    _byte(address, "address")
    if not 1 <= address <= 4:
        raise ValueError("box address must be 1..4")
    return address


def _slot_mask(slot):
    if type(slot) is not int or not 0 <= slot <= 3:
        raise ValueError("slot must be 0..3")
    return 1 << slot


def _rfid_mask(mask):
    if type(mask) is not int or not 0 <= mask <= 0x0F:
        raise ValueError("RFID mask must be 0x00..0x0f")
    return mask


def _uniid(value):
    try:
        value = bytes(value)
    except (TypeError, ValueError):
        raise ValueError("UniID must be 12 bytes")
    if len(value) != UNIID_LENGTH or not any(value):
        raise ValueError("UniID must be 12 nonzero bytes")
    return value


def decode_reply(frame, expected_address, expected_command, context=None):
    """Validate the common response envelope and return its decoded fields."""
    expected_address = _byte(expected_address, "expected address")
    expected_command = _byte(expected_command, "expected command")
    try:
        raw = bytes(frame)
    except (TypeError, ValueError):
        _klog(
            'rejected response envelope expected_address=%d expected_command=0x%02x context=%s frame=%r error=response is not bytes',
            expected_address, expected_command, context or "none", frame,
            level=logging.warning)
        raise ProtocolError("response is not bytes")
    error = None
    if len(raw) < 6:
        error = "response is shorter than the six-byte envelope"
    elif raw[0] != 0xF7:
        error = "response header is not 0xf7"
    elif len(raw) != raw[2] + 3:
        error = "response length byte does not match frame length"
    elif raw[1] != expected_address:
        error = "response came from unexpected address"
    elif raw[4] != expected_command:
        error = "response command does not match request"
    elif not check_485_frame_crc(raw)[0]:
        error = "response CRC is invalid"
    if error is not None:
        _klog(
            'rejected response envelope expected_address=%d expected_command=0x%02x context=%s frame=%s error=%s',
            expected_address, expected_command, context or "none", raw.hex(),
            error, level=logging.warning)
        raise ProtocolError(error)
    return Reply(raw[1], raw[4], raw[3], raw[5:-1], raw)


def _validate(
        reply, statuses, payload_lengths, context=None,
        payload_lengths_by_status=None):
    if reply.status not in statuses:
        _protocol_error(
            "status is not valid for this command", reply, context)
    payload_lengths_by_status = payload_lengths_by_status or {}
    expected = payload_lengths_by_status.get(reply.status)
    if expected is None:
        expected = (
            (0,) if reply.status in WIRE_ERROR_STATUSES
            else payload_lengths)
    if len(reply.payload) not in expected:
        _protocol_error(
            "payload length is not valid for this status", reply, context)
    return reply


def _command_reply(frame, address, command, statuses, context=None):
    return _validate(
        decode_reply(frame, address, command, context), statuses, (0,),
        context,
    )


def _stage_reply(reply, stage):
    if stage not in LOAD_STAGE_STATUSES:
        raise ValueError("supported load stages are 0, 4, 5, 6, and 7")
    return _validate(
        reply, LOAD_STAGE_STATUSES[stage], LOAD_STAGE_PAYLOAD_LENGTHS[stage],
        "load_stage=%d" % stage,
        LOAD_STAGE_STATUS_PAYLOAD_LENGTHS.get(stage),
    )


def decode_stage_reply(frame, address, stage):
    return _stage_reply(decode_reply(
        frame, address, CMD_LOAD, "load_stage=%d" % stage), stage)


def decode_unload_reply(frame, address, phase):
    if phase not in UNLOAD_PHASE_STATUSES:
        raise ValueError("unload phase must be 0 or 1")
    return _command_reply(
        frame, address, CMD_UNLOAD, UNLOAD_PHASE_STATUSES[phase],
        "unload_phase=%d" % phase,
    )


def decode_box_state(frame, address):
    reply = decode_reply(frame, address, CMD_BOX_STATE)
    empty = (None, None, None, None)
    if reply.status in WIRE_ERROR_STATUSES:
        if reply.payload:
            _protocol_error("wire-error box-state response has a payload", reply)
        values = empty
        events = None
    elif reply.status == STATUS_SLOT_EVENT and len(reply.payload) == 4:
        if any(event not in range(4) for event in reply.payload):
            _protocol_error("slot-event response has an unknown event code", reply)
        values = empty
        events = tuple(reply.payload)
    elif len(reply.payload) == 6:
        state = reply.payload[3]
        if state not in range(6):
            _protocol_error("unknown box-state value", reply)
        if reply.payload[4] & 0xF0 or reply.payload[5] & 0xF0:
            _protocol_error("slot mask has bits outside the four firmware slots", reply)
        if state == BOX_STATE_PRINT:
            if reply.status not in (STATUS_OK, STATUS_RUNOUT):
                _protocol_error("print-state response has an invalid status", reply)
        elif state != BOX_STATE_ERROR and reply.status != STATUS_OK:
            _protocol_error("non-error box state has a nonzero status", reply)
        values = (
            struct.unpack("b", reply.payload[:1])[0],
            reply.payload[1], state, reply.payload[4],
        )
        events = None
    else:
        _protocol_error("box-state payload has the wrong shape", reply)
    return BoxStateReply(
        reply.address, reply.command, reply.status, reply.payload, reply.raw,
        *values, events,
    )


def _record_fields(record):
    if record.strip().lower() in RFID_FALLBACK_RECORDS or len(record) != 0x28:
        return {}
    return {
        name: record[offset:offset + length]
        for name, offset, length in RFID_RECORD_FIELDS
    }


def decode_rfid_records(frame, address):
    reply = _validate(
        decode_reply(frame, address, CMD_RFID_RECORDS),
        QUERY_STATUSES, range(0xFD),
    )
    if reply.status != STATUS_OK:
        return RfidRecordsReply(
            reply.address, reply.command, reply.status, reply.payload, reply.raw,
            {}, {},
        )
    try:
        text = reply.payload.decode("ascii")
    except UnicodeDecodeError:
        _protocol_error("RFID record payload is not ASCII", reply)
    records = {}
    fields = {}
    for entry in text.split(";"):
        if not entry:
            continue
        if ":" not in entry:
            _protocol_error("RFID record entry has no slot separator", reply)
        slot, record = entry.split(":", 1)
        if slot not in RFID_SLOT_NAMES or slot in records:
            _protocol_error("RFID record has an invalid or duplicate slot", reply)
        record = record.strip("\x00")
        records[slot] = record
        parsed = _record_fields(record)
        if parsed:
            fields[slot] = parsed
    return RfidRecordsReply(
        reply.address, reply.command, reply.status, reply.payload, reply.raw,
        records, fields,
    )


def decode_rfid_remaining(frame, address):
    reply = _validate(
        decode_reply(frame, address, CMD_RFID_REMAINING),
        QUERY_STATUSES, (4,),
    )
    values = {}
    if reply.status == STATUS_OK:
        values = dict(zip(RFID_SLOT_NAMES, reply.payload))
    return RfidRemainingReply(
        reply.address, reply.command, reply.status, reply.payload, reply.raw,
        values,
    )


def decode_auto_reply(frame, command, expected_address, expected_uniid=None):
    """Strictly decode the common A0/A1/A2 identity response."""
    if command not in (AUTO_ASSIGN, AUTO_DISCOVER, AUTO_QUERY):
        raise ValueError("auto-address command must be A0, A1, or A2")
    reply = decode_reply(frame, expected_address, command)
    if reply.status != STATUS_OK or len(reply.payload) != 14:
        _protocol_error("auto-address response has invalid outer status or shape", reply)
    device_type, inner_status = reply.payload[:2]
    if device_type != 1:
        _protocol_error("auto-address response device type is not CFS", reply)
    if inner_status != STATUS_OK:
        _protocol_error("auto-address inner status is nonzero", reply)
    uniid = reply.payload[2:]
    if not any(uniid):
        _protocol_error("auto-address UniID is zero", reply)
    if expected_uniid is not None and uniid != _uniid(expected_uniid):
        _protocol_error("auto-address UniID does not match", reply)
    return AutoAddressReply(uniid)


class BoxDriver:
    """Transport-backed, policy-free protocol client for one box."""

    def __init__(self, serial, address):
        self.serial = serial
        self.address = _box_address(address)

    def _exchange(self, command, payload=(), timeout=DEFAULT_TIMEOUT):
        request = build_485_body(
            self.address, command, payload, header_byte=0xFF)
        return self.serial.cmd_send_data_with_response(request, timeout)

    def load_stage(self, slot, stage, timeout=DEFAULT_TIMEOUT):
        if stage not in LOAD_STAGE_STATUSES:
            raise ValueError("supported load stages are 0, 4, 5, 6, and 7")
        argument = 3 if stage == 7 else 0
        frame = self._exchange(
            CMD_LOAD, (_slot_mask(slot), stage, argument), timeout)
        if not frame:
            return None
        return decode_stage_reply(frame, self.address, stage)

    @contextmanager
    def load_session(self):
        with self.serial.request_session() as transport:
            yield BoxDriver(transport, self.address)

    def unload_buffer(self, timeout=DEFAULT_TIMEOUT):
        frame = self._exchange(CMD_UNLOAD, (0, 0), timeout)
        return None if not frame else decode_unload_reply(
            frame, self.address, 0)

    def unload_path(self, slot, timeout=DEFAULT_TIMEOUT):
        frame = self._exchange(CMD_UNLOAD, (_slot_mask(slot), 1), timeout)
        return None if not frame else decode_unload_reply(
            frame, self.address, 1)

    def set_tracking(self, slot, timeout=DEFAULT_TIMEOUT):
        payload = (0, 1) if slot is None else (_slot_mask(slot), 0)
        frame = self._exchange(CMD_TRACKING, payload, timeout)
        if not frame:
            return None
        return _command_reply(
            frame, self.address, CMD_TRACKING, COMMAND_STATUSES,
        )

    def _query_byte(self, command, payload, timeout):
        frame = self._exchange(command, payload, timeout)
        if not frame:
            return None
        reply = _validate(
            decode_reply(frame, self.address, command),
            QUERY_STATUSES, (1,),
        )
        value = reply.payload[0] if reply.status == STATUS_OK else None
        return ScalarReply(
            reply.address, reply.command, reply.status, reply.payload,
            reply.raw, value,
        )

    def query_slot_mask(self, timeout=DEFAULT_TIMEOUT):
        reply = self._query_byte(CMD_SLOT_MASK, (0,), timeout)
        if reply is not None and reply.value is not None and reply.value & 0xF0:
            _protocol_error("slot mask has bits outside the four firmware slots", reply)
        return reply

    def query_hub_mask(self, timeout=DEFAULT_TIMEOUT):
        reply = self._query_byte(CMD_SLOT_MASK, (1,), timeout)
        if reply is not None and reply.value is not None and reply.value & 0xF0:
            _protocol_error("hub mask has bits outside the four firmware slots", reply)
        return reply

    def query_buffer(self, timeout=DEFAULT_TIMEOUT):
        return self._query_byte(CMD_BUFFER, (), timeout)

    def query_box_state(self, timeout=POLL_TIMEOUT):
        frame = self._exchange(CMD_BOX_STATE, (), timeout)
        return None if not frame else decode_box_state(frame, self.address)

    def query_encoder(self, timeout=POLL_TIMEOUT):
        frame = self._exchange(CMD_ENCODER, (1,), timeout)
        if not frame:
            return None
        reply = _validate(
            decode_reply(frame, self.address, CMD_ENCODER),
            QUERY_STATUSES, (4,),
        )
        value = None
        if reply.status == STATUS_OK:
            value = struct.unpack(">f", reply.payload)[0]
        return ScalarReply(
            reply.address, reply.command, reply.status, reply.payload,
            reply.raw, value,
        )

    def query_rfid_records(self, mask=0x0F, timeout=DEFAULT_TIMEOUT):
        frame = self._exchange(CMD_RFID_RECORDS, (_rfid_mask(mask),), timeout)
        return None if not frame else decode_rfid_records(frame, self.address)

    def query_rfid_remaining(self, mask=0x0F, timeout=DEFAULT_TIMEOUT):
        frame = self._exchange(
            CMD_RFID_REMAINING, (_rfid_mask(mask),), timeout)
        return None if not frame else decode_rfid_remaining(
            frame, self.address)

    def set_rfid_insert_reading(self, enabled, timeout=DEFAULT_TIMEOUT):
        if type(enabled) is not bool:
            raise ValueError("enabled must be bool")
        frame = self._exchange(
            CMD_RFID_CONTROL, (0, int(enabled)), timeout)
        if not frame:
            return None
        return _command_reply(
            frame, self.address, CMD_RFID_CONTROL,
            QUERY_STATUSES,
        )

    def force_rfid_read(self, mask=0x0F, timeout=None):
        mask = _rfid_mask(mask)
        if timeout is None:
            selected = bin(mask).count("1")
            timeout = (RFID_FORCE_SLOT_TIMEOUT * selected
                       if selected else DEFAULT_TIMEOUT)
        frame = self._exchange(CMD_RFID_CONTROL, (mask, 2), timeout)
        if not frame:
            return None
        return _command_reply(
            frame, self.address, CMD_RFID_CONTROL, RFID_FORCE_STATUSES)


class AutoAddressClient:
    """Decoded A0/A1/A2 client; address policy and persistence live elsewhere."""

    def __init__(self, serial):
        self.serial = serial

    def _exchange(self, request_address, command, payload, timeout):
        request = build_485_body(
            request_address, command, payload, header_byte=0x00)
        return self.serial.cmd_send_data_with_response(request, timeout)

    def discover(self, timeout=1.0):
        frame = self._exchange(
            BROADCAST_ADDRESS, AUTO_DISCOVER,
            (BROADCAST_ADDRESS, BROADCAST_ADDRESS), timeout)
        return None if not frame else decode_auto_reply(
            frame, AUTO_DISCOVER, BROADCAST_ADDRESS)

    def query(self, address, timeout=0.5):
        address = _box_address(address)
        frame = self._exchange(address, AUTO_QUERY, (), timeout)
        return None if not frame else decode_auto_reply(
            frame, AUTO_QUERY, address)

    def assign(self, uniid, address, timeout=0.5):
        address = _box_address(address)
        uniid = _uniid(uniid)
        frame = self._exchange(
            BROADCAST_ADDRESS, AUTO_ASSIGN, (address,) + tuple(uniid), timeout)
        return None if not frame else decode_auto_reply(
            frame, AUTO_ASSIGN, address, expected_uniid=uniid)
