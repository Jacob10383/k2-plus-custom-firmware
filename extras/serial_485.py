# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import threading
import time
from collections import deque


PACK_HEAD = 0xF7
PACK_LEN_MIN = 3          # header/status + func + trailing wire crc
PACK_LEN_MAX = 255


def _klog(msg, *args, level=logging.info):
    level("serial_485: " + msg, *args)


class _Serial485Request:
    def __init__(
            self, req_id, request, timeout, attempts, completion,
            session=None, control=None):
        self.req_id = req_id
        self.request = request
        self.timeout = timeout
        self.attempts = attempts
        self.completion = completion
        self.session = session
        self.control = control
        self.response = None
        self.error = None
        self.done = threading.Event()


class _Serial485Session:
    def __init__(self, wrapper):
        self.wrapper = wrapper
        self.token = object()
        self.active = False

    def __enter__(self):
        self.wrapper._session_control(self.token, "begin")
        self.active = True
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        if self.active:
            self.active = False
            self.wrapper._session_control(self.token, "end")

    def cmd_send_data_with_response(self, data, timeout=1.0, attempts=1):
        if not self.active:
            raise RuntimeError("serial485 session is not active")
        return self.wrapper._send_data_with_response(
            data, timeout, attempts, session=self.token)


def crc8(data):
    # Matches Klipper's msgblock_485_crc8() helper.
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def _byte_value(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("serial485 %s must be an integer 0..255" % (name,))
    if value < 0 or value > 0xFF:
        raise ValueError("serial485 %s must be an integer 0..255" % (name,))
    return value


def _payload_bytes(payload):
    if isinstance(payload, int):
        raise TypeError(
            "serial485 payload must be bytes-like or iterable of byte values")
    try:
        return bytes(payload)
    except ValueError as e:
        raise ValueError("serial485 payload bytes must be 0..255") from e


def build_485_body(addr, func, payload=b"", header_byte=0x00):
    payload = _payload_bytes(payload)
    declared_len = len(payload) + PACK_LEN_MIN
    if declared_len > PACK_LEN_MAX:
        raise ValueError("serial485 request payload too long")
    return bytes([
        _byte_value("addr", addr),
        declared_len,
        _byte_value("header_byte", header_byte),
        _byte_value("func", func),
    ]) + payload


def check_485_frame_crc(frame):
    frame = bytes(frame)
    if len(frame) < 4:
        return True, None, None
    expected = crc8(frame[2:-1])
    actual = frame[-1]
    return expected == actual, expected, actual


def get_address_desc(addr):
    if 0x01 <= addr <= 0x04:
        return f"CFS Box {addr}"
    elif addr == 0x21:
        return "MDLX (X-Belt)"
    elif addr == 0x22:
        return "MDLY (Y-Belt)"
    elif addr == 0x81:
        return "Motor X"
    elif addr == 0x82:
        return "Motor Y"
    elif addr == 0x83:
        return "Motor Z"
    elif addr == 0x84:
        return "Motor Z1"
    elif addr == 0x85:
        return "Motor E"
    elif 0x81 <= addr <= 0x8F:
        return f"Motor (0x{addr:02X})"
    elif addr in (0xFE, 0xFF):
        return "Broadcast"
    return f"Unknown (0x{addr:02X})"


def get_func_desc(addr, func):
    # Box functions
    if 0x01 <= addr <= 0x04 or addr in (0xFE, 0xFF):
        box_funcs = {
            0x04: "AUTO_BUFFER_SET",
            0x05: "BUFFER_STATE",
            0x08: "FILAMENT_MASK",
            0x0A: "BOX_STATE",
            0x0E: "ENCODER_QUERY",
            0x10: "LOAD_STAGE",
            0x11: "RETRACT_PHASE",
            0xA0: "CMD_ASSIGN",
            0xA1: "CMD_DISCOVER",
            0xA2: "CMD_HW_STATUS",
        }
        if func in box_funcs:
            return box_funcs[func]
    # Motor functions
    if 0x81 <= addr <= 0x8F:
        motor_funcs = {
            0x06: "SYS_PARAM",
            0x07: "FLASH_PARAM",
            0x0C: "PROTECTION",
            0x0E: "RS485_ADDR",
            0x0F: "TRANSPARENT",
            0x11: "STALL_MODE",
        }
        if func in motor_funcs:
            return motor_funcs[func]
    # Belt functions
    if addr in (0x21, 0x22):
        belt_funcs = {0: "READ_VERSION", 2: "READ_FLASH", 4: "WRITE_FLASH", 6: "READ_ADC", 8: "MOVE_SLIDER"}
        if func in belt_funcs:
            return belt_funcs[func]
    return f"0x{func:02X}"


def build_485_frame(addr, func, payload=b"", header_byte=0x00):
    body = build_485_body(addr, func, payload, header_byte=header_byte)
    return bytes([PACK_HEAD]) + body + bytes([crc8(body[1:])])


class Serial_485_Wrapper:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self._reactor_thread_ident = threading.get_ident()
        self.gcode = self.printer.lookup_object("gcode")
        self.section_name = config.get_name()
        self.name = self.section_name.split()[-1]
        self.serial_port = config.get("serial", "/dev/ttyS5")
        self.baud = config.getint("baud", 230400)
        self.default_timeout = config.getfloat("default_timeout", 1.0)
        self.read_timeout = config.getfloat("read_timeout", 0.05)
        self.connect_settle = config.getfloat("connect_settle", 0.05)
        self.write_timeout = config.getfloat("write_timeout", 1.0)
        self.read_chunk = config.getint("read_chunk", 4096)
        self.max_queued_frames = config.getint("max_queued_frames", 64)

        self._serial = None
        self._reader_thread = None
        self._stop_reader = threading.Event()
        self._request_thread = None
        self._stop_request_worker = threading.Event()
        self._connect_lock = threading.Lock()
        self._tx_lock = threading.Lock()
        self._request_cond = threading.Condition()
        self._request_queue = deque()
        self._active_request = None
        self._request_session = None
        self._request_seq = 0
        self._request_stop_error = None
        self._rx_cond = threading.Condition()
        self._rx_frames = deque()
        self._rx_buffer = bytearray()
        self._connected = False
        self._ready_timer = self.reactor.register_timer(self._ready_timer_handler)
        self._last_log_time = {}
        self._suppressed_counts = {}

        self._stats = {
            "connects": 0,
            "disconnects": 0,
            "tx_frames": 0,
            "tx_bytes": 0,
            "rx_frames": 0,
            "rx_bytes": 0,
            "rx_invalid_crc": 0,
            "rx_invalid_len": 0,
            "rx_unmatched": 0,
            "rx_stale_dropped": 0,
            "timeouts": 0,
            "send_errors": 0,
            "reader_errors": 0,
        }

        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)
        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)
        self.printer.register_event_handler("klippy:firmware_restart", self._handle_firmware_restart)
        self.gcode.register_command(
            "SERIAL_STATUS", self.cmd_SERIAL_STATUS,
            desc="Show RS485 transport status and counters")
        _klog("init section=%s name=%s port=%s baud=%s",
              self.section_name, self.name, self.serial_port, self.baud)
    def _log_warning_ratelimited(self, key, msg, *args, interval=1.0):
        now = time.monotonic()
        last = self._last_log_time.get(key, 0.0)
        self._suppressed_counts[key] = self._suppressed_counts.get(key, 0) + 1
        if now - last >= interval:
            count = self._suppressed_counts[key]
            self._suppressed_counts[key] = 0
            self._last_log_time[key] = now
            suffix = ""
            if count > 1:
                suffix = " (suppressed %d similar warnings)" % (count - 1)
            _klog(msg + suffix, *args, level=logging.warning)

    def _handle_connect(self):
        _klog("received klippy:connect")
        try:
            self._connect()
        except Exception:
            _klog("connect failed during klippy:connect",
                  level=logging.exception)

    def _handle_disconnect(self):
        self._disconnect("klippy:disconnect")

    def _handle_shutdown(self):
        self._disconnect("klippy:shutdown")

    def _handle_firmware_restart(self):
        self._disconnect("klippy:firmware_restart")

    def _connect(self):
        with self._connect_lock:
            if self._connected:
                return

            self._ensure_request_worker()

            _klog("connect begin port=%s baud=%s",
                  self.serial_port, self.baud)
            import serial

            dev = serial.Serial(
                port=self.serial_port,
                baudrate=self.baud,
                timeout=self.read_timeout,
                write_timeout=self.write_timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            try:
                dev.reset_input_buffer()
            except OSError:
                pass
            try:
                dev.reset_output_buffer()
            except OSError:
                pass
            try:
                import termios
                attrs = termios.tcgetattr(dev.fileno())
                attrs[2] &= ~termios.HUPCL
                termios.tcsetattr(dev.fileno(), termios.TCSANOW, attrs)
            except (ImportError, OSError):
                # termios missing on non-POSIX; tcsetattr can fail on some adapters.
                # termios.error subclasses OSError so this catches both.
                pass

            self._serial = dev
            self._rx_buffer = bytearray()
            with self._rx_cond:
                self._rx_frames.clear()
                self._rx_cond.notify()

            self._stop_reader.clear()
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                name="serial485-reader",
                daemon=True)
            self._reader_thread.start()

            if self.connect_settle > 0.0:
                time.sleep(self.connect_settle)

            self._connected = True
            self._stats["connects"] += 1
            _klog("connect complete port=%s baud=%s",
                  self.serial_port, self.baud)
            _klog("scheduling serial_485:ready")
            self._set_ready_timer_async(time.monotonic())

    def _ready_timer_handler(self, eventtime):
        if not self._connected or self._serial is None:
            _klog(
                "skipping serial_485:ready emit because transport disconnected")
            return self.reactor.NEVER
        try:
            self.printer.send_event("serial_485:ready")
            _klog("sending serial_485:ready")
        except Exception:
            _klog("failed sending serial_485:ready",
                  level=logging.exception)
        return self.reactor.NEVER

    def _set_ready_timer_async(self, when):
        def _cb(eventtime):
            self.reactor.update_timer(self._ready_timer, when)
        self.reactor.register_async_callback(_cb)

    def _disconnect(self, reason):
        with self._connect_lock:
            has_request_worker = (
                self._request_thread is not None and self._request_thread.is_alive())
            if not self._serial and not self._connected and not has_request_worker:
                return

            _klog("disconnect begin reason=%s", reason)
            self._set_ready_timer_async(self.reactor.NEVER)
            self._stop_reader.set()
            serial_dev = self._serial
            reader = self._reader_thread
            self._serial = None
            self._connected = False

            if serial_dev is not None:
                try:
                    serial_dev.close()
                except Exception:
                    _klog("close failed reason=%s", reason,
                          level=logging.exception)

            if reader is not None and reader.is_alive():
                reader.join(timeout=1.0)

            with self._rx_cond:
                self._rx_frames.clear()
                self._rx_cond.notify()

            self._reader_thread = None
            self._stats["disconnects"] += 1
            _klog("disconnect complete reason=%s", reason)

            if reason in ("klippy:shutdown", "klippy:firmware_restart"):
                self._stop_request_worker_thread(
                    RuntimeError("serial485 transport disconnected"))

    def _ensure_connected(self):
        if not self._connected or self._serial is None:
            self._connect()

    def _ensure_request_worker(self):
        with self._request_cond:
            thread = self._request_thread
            if thread is not None and thread.is_alive():
                return
            self._stop_request_worker.clear()
            self._request_stop_error = None
            self._request_thread = threading.Thread(
                target=self._request_worker_loop,
                name="serial485-request",
                daemon=True)
            self._request_thread.start()

    def _stop_request_worker_thread(self, error=None):
        with self._request_cond:
            thread = self._request_thread
            if thread is None:
                return
            self._request_stop_error = error
            self._stop_request_worker.set()
            self._request_cond.notify()
        with self._rx_cond:
            self._rx_cond.notify()
        if thread.is_alive():
            thread.join(timeout=1.0)
        with self._request_cond:
            if self._request_thread is thread and not thread.is_alive():
                self._request_thread = None
            if not thread.is_alive():
                self._active_request = None
                self._request_session = None

    def _reader_loop(self):
        try:
            while not self._stop_reader.is_set():
                serial_dev = self._serial
                if serial_dev is None:
                    break
                try:
                    waiting = getattr(serial_dev, "in_waiting", 0)
                    data = serial_dev.read(max(1, min(self.read_chunk, waiting or 1)))
                except Exception:
                    if self._stop_reader.is_set():
                        break
                    self._stats["reader_errors"] += 1
                    _klog("reader failure", level=logging.exception)
                    break

                if not data:
                    continue

                self._stats["rx_bytes"] += len(data)
                self._feed_rx(data)
        finally:
            pass

    def _feed_rx(self, data):
        self._rx_buffer.extend(data)
        while True:
            frame = self._extract_one_frame()
            if frame is None:
                return
            self._stats["rx_frames"] += 1
            with self._rx_cond:
                if len(self._rx_frames) >= self.max_queued_frames:
                    dropped_frame = self._rx_frames.popleft()
                    self._stats["rx_stale_dropped"] += 1
                    if len(dropped_frame) >= 5:
                        dropped_addr = dropped_frame[1]
                        dropped_func = dropped_frame[4]
                        dropped_len = dropped_frame[2]
                        self._log_warning_ratelimited(
                            "rx_stale_dropped",
                            "dropping oldest queued frame: addr=%s (0x%02X) func=%s (0x%02X) len=%d",
                            get_address_desc(dropped_addr), dropped_addr,
                            get_func_desc(dropped_addr, dropped_func), dropped_func,
                            dropped_len
                        )
                    else:
                        self._log_warning_ratelimited(
                            "rx_stale_dropped",
                            "dropping oldest queued frame: short frame len=%d",
                            len(dropped_frame)
                        )
                self._rx_frames.append(frame)
                self._rx_cond.notify()

    def _extract_one_frame(self):
        buf = self._rx_buffer
        while buf and buf[0] != PACK_HEAD:
            buf.pop(0)

        if len(buf) < 3:
            return None

        frame_len = buf[2]
        if frame_len < PACK_LEN_MIN or frame_len > PACK_LEN_MAX:
            self._stats["rx_invalid_len"] += 1
            self._log_warning_ratelimited(
                ("invalid_len", frame_len),
                "invalid frame len=%d", frame_len
            )
            del buf[0]
            return None

        total_len = 3 + frame_len
        if len(buf) < total_len:
            return None

        candidate = bytes(buf[:total_len])
        crc_ok, expected_crc, actual_crc = check_485_frame_crc(candidate)
        if not crc_ok:
            self._stats["rx_invalid_crc"] += 1
            self._log_warning_ratelimited(
                ("invalid_crc", expected_crc, actual_crc),
                "crc mismatch expected=0x%02x actual=0x%02x",
                expected_crc, actual_crc
            )
            del buf[0]
            return None

        del buf[:total_len]
        return candidate

    def _prepare_request(self, data):
        body = bytes(data)
        if len(body) < 4:
            raise ValueError("serial485 request body must be at least 4 bytes")
        if body[0] == PACK_HEAD:
            raise ValueError("serial485 request must be body, not wire frame")
        declared_len = body[1]
        if len(body) != declared_len + 1:
            raise ValueError(
                "serial485 request body length mismatch declared_len=%d actual=%d"
                % (declared_len, len(body)))
        wire = bytes([PACK_HEAD]) + body + bytes([crc8(body[1:])])
        return {
            "wire": wire,
            "addr": body[0],
            "func": body[3],
            "request_hex": body.hex(),
        }

    def _clear_pending_frames(self):
        with self._rx_cond:
            count = len(self._rx_frames)
            if count:
                self._rx_frames.clear()
                self._stats["rx_stale_dropped"] += count
                _klog(
                    "cleared %d stale queued frame(s)", count,
                    level=logging.warning)

    def _matches_response(self, request_addr, request_func, frame):
        if len(frame) < 6 or frame[0] != PACK_HEAD:
            return False

        resp_addr = frame[1]
        resp_func = frame[4]

        if request_addr not in (0xFE, 0xFF) and resp_addr != request_addr:
            return False
        if resp_func != request_func:
            return False
        return True

    def _wait_for_response(self, request_addr, request_func, timeout):
        deadline = time.monotonic() + timeout
        with self._rx_cond:
            while True:
                if self._stop_request_worker.is_set():
                    if self._request_stop_error is not None:
                        raise self._request_stop_error
                    return None
                while self._rx_frames:
                    frame = self._rx_frames.popleft()
                    if self._matches_response(request_addr, request_func, frame):
                        return frame
                    self._stats["rx_unmatched"] += 1
                    self._log_warning_ratelimited(
                        ("unmatched", request_addr, request_func, frame[1], frame[4]),
                        "ignoring unmatched frame: "
                        "req=%s (0x%02X)/%s (0x%02X) "
                        "resp=%s (0x%02X)/%s (0x%02X)",
                        get_address_desc(request_addr), request_addr,
                        get_func_desc(request_addr, request_func), request_func,
                        get_address_desc(frame[1]), frame[1],
                        get_func_desc(frame[1], frame[4]), frame[4]
                    )

                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    self._stats["timeouts"] += 1
                    return None
                self._rx_cond.wait(timeout=remaining)

    def _next_request_id(self):
        with self._request_cond:
            self._request_seq += 1
            return self._request_seq

    def _submit_request(
            self, request, timeout, attempts, session=None, control=None):
        if self.printer.is_shutdown():
            raise RuntimeError("serial485 transport unavailable after shutdown")
        self._ensure_request_worker()
        pending = _Serial485Request(
            self._next_request_id(), request, timeout, attempts,
            self.reactor.completion(), session=session, control=control)
        with self._request_cond:
            self._request_queue.append(pending)
            self._request_cond.notify()
        return pending

    def _pop_request(self):
        if not self._request_queue:
            return None
        if self._request_session is None:
            return self._request_queue.popleft()
        for pending in self._request_queue:
            if pending.session is self._request_session:
                self._request_queue.remove(pending)
                return pending
        return None

    def _can_wait_on_reactor_completion(self):
        if threading.get_ident() != self._reactor_thread_ident:
            return False
        import greenlet

        current = greenlet.getcurrent()
        return hasattr(current, "timer")

    def _await_request_completion(self, pending):
        if self._can_wait_on_reactor_completion():
            result = pending.completion.wait()
        else:
            pending.done.wait()
            result = pending
        if result is None:
            raise RuntimeError("serial485 request wait ended without result")
        if result.error is not None:
            raise result.error
        return result.response

    def _request_worker_loop(self):
        try:
            while True:
                with self._request_cond:
                    pending = self._pop_request()
                    while (pending is None
                           and not self._stop_request_worker.is_set()):
                        self._request_cond.wait()
                        pending = self._pop_request()
                    if self._stop_request_worker.is_set():
                        error = self._request_stop_error
                        queued = list(self._request_queue)
                        self._request_queue.clear()
                        if pending is None and not queued:
                            return
                        if pending is None:
                            pending = queued[0]
                            extras = queued[1:]
                        else:
                            extras = queued
                    else:
                        error = None
                        extras = ()
                    for extra in extras:
                        extra.error = error
                        extra.done.set()
                        self.reactor.async_complete(extra.completion, extra)
                    self._active_request = pending
                try:
                    if error is not None:
                        raise error
                    self._process_request(pending)
                except Exception as e:
                    pending.error = e
                finally:
                    with self._request_cond:
                        if self._active_request is pending:
                            self._active_request = None
                    pending.done.set()
                    self.reactor.async_complete(pending.completion, pending)
        finally:
            pass

    def _process_request(self, pending):
        if pending.control == "begin":
            with self._request_cond:
                if self._request_session is not None:
                    raise RuntimeError("serial485 request session is already active")
                self._request_session = pending.session
            pending.response = True
            return
        if pending.control == "end":
            with self._request_cond:
                if self._request_session is not pending.session:
                    raise RuntimeError("serial485 request session is not active")
                self._request_session = None
                self._request_cond.notify_all()
            pending.response = True
            return
        request = pending.request
        for attempt in range(1, pending.attempts + 1):
            with self._tx_lock:
                self._ensure_connected()
                self._clear_pending_frames()
                try:
                    self._write_frame(request["wire"])
                except Exception:
                    self._stats["send_errors"] += 1
                    _klog(
                        "tx failure id=%d addr=0x%02x func=0x%02x",
                        pending.req_id, request["addr"], request["func"],
                        level=logging.exception)
                    self._disconnect("send_error")
                    raise

                response = self._wait_for_response(
                    request["addr"], request["func"], pending.timeout)
                if response is not None:
                    pending.response = response
                    return

                _klog(
                    "timeout waiting for response id=%d attempt=%d/%d "
                    "addr=0x%02x func=0x%02x",
                    pending.req_id, attempt, pending.attempts,
                    request["addr"], request["func"], level=logging.warning)

        pending.response = None

    def _write_frame(self, wire_frame):
        serial_dev = self._serial
        if serial_dev is None:
            raise RuntimeError("serial485 transport is not connected")
        serial_dev.write(wire_frame)
        serial_dev.flush()
        self._stats["tx_frames"] += 1
        self._stats["tx_bytes"] += len(wire_frame)

    def _send_data_with_response(
            self, data, timeout=1.0, attempts=1, session=None):
        request = self._prepare_request(data)
        timeout = float(timeout if timeout is not None else self.default_timeout)
        attempts = int(attempts)
        if attempts < 1:
            raise ValueError("serial485 attempts must be >= 1")
        pending = self._submit_request(
            request, timeout, attempts, session=session)
        return self._await_request_completion(pending)

    def cmd_send_data_with_response(self, data, timeout=1.0, attempts=1):
        return self._send_data_with_response(data, timeout, attempts)

    def request_session(self):
        return _Serial485Session(self)

    def _session_control(self, token, control):
        pending = self._submit_request(
            None, 0.0, 1, session=token, control=control)
        return self._await_request_completion(pending)

    def stats(self, _eventtime=None):
        msg = (
            "serial485: "
            f"connected={self._connected} "
            f"tx_frames={self._stats['tx_frames']} "
            f"rx_frames={self._stats['rx_frames']} "
            f"tx_bytes={self._stats['tx_bytes']} "
            f"rx_bytes={self._stats['rx_bytes']} "
            f"timeouts={self._stats['timeouts']} "
            f"crc_errors={self._stats['rx_invalid_crc']} "
            f"unmatched={self._stats['rx_unmatched']}"
        )
        return (False, msg)

    def _status_fields(self):
        with self._rx_cond:
            queued_frames = len(self._rx_frames)
        with self._request_cond:
            queued_requests = len(self._request_queue)
            request_worker_alive = (
                self._request_thread is not None and self._request_thread.is_alive())
            active_req = self._active_request
            active_request_id = (
                active_req.req_id if active_req is not None else None)
        return {
            "name": self.name,
            "port": self.serial_port,
            "baud": self.baud,
            "connected": self._connected,
            "queued_frames": queued_frames,
            "queued_requests": queued_requests,
            "active_request_id": active_request_id,
            "request_worker_alive": request_worker_alive,
            "buffered_bytes": len(self._rx_buffer),
            "connects": self._stats["connects"],
            "disconnects": self._stats["disconnects"],
            "tx_frames": self._stats["tx_frames"],
            "tx_bytes": self._stats["tx_bytes"],
            "rx_frames": self._stats["rx_frames"],
            "rx_bytes": self._stats["rx_bytes"],
            "crc_errors": self._stats["rx_invalid_crc"],
            "invalid_len": self._stats["rx_invalid_len"],
            "unmatched": self._stats["rx_unmatched"],
            "stale_dropped": self._stats["rx_stale_dropped"],
            "timeouts": self._stats["timeouts"],
            "send_errors": self._stats["send_errors"],
            "reader_errors": self._stats["reader_errors"],
        }

    def cmd_SERIAL_STATUS(self, gcmd):
        fields = self._status_fields()
        gcmd.respond_info(
            "serial485 "
            f"name={fields['name']} port={fields['port']} baud={fields['baud']} "
            f"connected={fields['connected']} queued_frames={fields['queued_frames']} "
            f"queued_requests={fields['queued_requests']} "
            f"active_request_id={fields['active_request_id']} "
            f"request_worker_alive={fields['request_worker_alive']} "
            f"buffered_bytes={fields['buffered_bytes']}")
        gcmd.respond_info(
            "serial485 counters "
            f"connects={fields['connects']} disconnects={fields['disconnects']} "
            f"tx_frames={fields['tx_frames']} tx_bytes={fields['tx_bytes']} "
            f"rx_frames={fields['rx_frames']} rx_bytes={fields['rx_bytes']}")
        gcmd.respond_info(
            "serial485 errors "
            f"crc_errors={fields['crc_errors']} invalid_len={fields['invalid_len']} "
            f"unmatched={fields['unmatched']} stale_dropped={fields['stale_dropped']} "
            f"timeouts={fields['timeouts']} send_errors={fields['send_errors']} "
            f"reader_errors={fields['reader_errors']}")


def load_config_prefix(config):
    return Serial_485_Wrapper(config)
