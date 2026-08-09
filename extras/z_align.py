# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# MCU-assisted Z prep helper for integrated homing.
#
# Coordinates the MCU-side dual-Z photoelectric alignment before final host
# Z homing. XY homing can run while the MCU alignment is active; the post-align
# bed rise is a normal homing move.
#
# Example config with current Python defaults:
# [z_align]
# quick_speed: 30  # mm/s for the MCU-side fast drop into the switches.
# slow_speed: 10  # mm/s for the MCU-side slow settle pass.
# rising_dist: 10  # mm lift between the fast and slow MCU passes.
# safe_dist: 40  # mm max extra one-sided drop after the other switch triggers.
# filter_cnt: 10  # Consecutive switch samples required before accepting a trigger.
# timeout: 30  # Seconds allowed for a single MCU z_align attempt.
# retries: 5  # Max MCU retry attempts before aborting.
# retry_tolerance: 10  # Allowed left/right mismatch in MCU-reported steps.
# endstop_pin_z: PA15, PA8  # Bottom photoelectric switch pins, one per Z motor.
# zd_up: 0  # Direction level that moves Z away from the switches.
# zes_untrig: 1  # Logic level reported by an untriggered switch.
# rise_distance: 340  # mm rise after switch alignment before final Z home.
# rise_speed: 50  # mm/s for that long post-align rise.
# temp_rise_max_z_accel: 200  # Temporary accel cap for the post-align rise.
# zmax: 350  # Logical Z coordinate after MCU bottom-switch alignment.
#
import json
import logging
import math
import os

import mcu

from .motor_control import MOTOR_COMMAND_TIMEOUT


def _klog(msg, *args, level=logging.info):
    level("z_align: " + msg, *args)


POLL_INTERVAL = 0.010
TEMP_RISE_MAX_Z_VELOCITY = 100.0
TEMP_RISE_MAX_Z_ACCEL = 200.0
STARTUP_RISE_PRIME_MAX_DIST = 0.1
STARTUP_RISE_PRIME_SPEED = 10.0
MOTOR_ZDOWN_TIMEOUT = -10000
MOTOR_PROTECT_ERROR = -10001
TILT_BIAS_FILE = '/mnt/UDISK/printer_data/z_align_tilt_bias.json'
MAX_TILT_BIAS = 1.0


class ZAlignNullEndstop:
    def __init__(self, steppers, reactor):
        self._steppers = tuple(steppers)
        self._reactor = reactor

    def get_steppers(self):
        return list(self._steppers)

    def home_start(self, print_time, sample_time, sample_count,
                   rest_time, triggered=True):
        return self._reactor.completion()

    def home_wait(self, home_end_time):
        return 0.0

    def query_endstop(self, print_time):
        return False


class ZAlign:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.endstop_pin_z = config.getlist('endstop_pin_z', ('PA15', 'PA8'))
        self.zd_up = config.getint('zd_up', 0)
        self.zes_untrig = config.getint('zes_untrig', 1)
        self.fast_drop_speed = config.getfloat('quick_speed', 30.0, above=0.0)
        self.settle_drop_speed = config.getfloat('slow_speed', 10.0, above=0.0)
        self.mcu_rising_dist = config.getfloat('rising_dist', 10.0, above=0.0)
        self.mcu_filter_count = config.getint('filter_cnt', 10, minval=1)
        self.retries = config.getint('retries', 5, minval=1)
        self.retry_tolerance = config.getint('retry_tolerance', 10, minval=0)
        self.rise_distance = config.getfloat('rise_distance', 340.0, above=0.0)
        self.rise_speed = config.getfloat('rise_speed', 50.0, above=0.0)
        self.temp_rise_max_z_accel = config.getfloat(
            'temp_rise_max_z_accel', TEMP_RISE_MAX_Z_ACCEL, above=0.0)
        self.safe_dist = config.getfloat('safe_dist', 40.0, above=0.0)
        self.timeout = config.getfloat('timeout', 30.0, above=0.0)
        self.zmax = config.getfloat('zmax', 350.0, above=0.0)
        self._toolhead = None
        self._z_align_query_cmd = None
        self._z_align_force_stop_cmd = None
        self._main_mcu = mcu.get_printer_mcu(self.printer, "mcu")
        self._oidz = self._main_mcu.create_oid()
        self._main_mcu.register_config_callback(self._build_config)
        self._step_distance = None
        self._z_align_status = {}
        self._orig_serial_handle_default = None
        self._state = 'idle'
        self._error = None
        self._phase_deadline = None
        self._prepared_zmax = None
        self._target_z = None
        self._logical_z_applied = False
        self._settle_attempt = 0
        self._last_retry_delta_mm = 0.0
        self._last_retry_delta_steps = 0
        self._z_tilt = None
        self._tilt_bias = 0.0
        self._learn_delta = None
        self._bottom_anchor = None
        self.force_stop_flag = False
        self.endstop_pin_status = [0] * min(len(self.endstop_pin_z), 8)
        self.pin_len = min(len(self.endstop_pin_z), 8)
        self._timer = self.reactor.register_timer(
            self._handle_timer, self.reactor.NEVER)
        self.gcode.register_command("ZDOWN", self.cmd_ZDOWN)
        self.gcode.register_command("ZDOWN_FORCE_STOP", self.cmd_ZDOWN_FORCE_STOP)
        webhooks = self.printer.lookup_object('webhooks')
        webhooks.register_endpoint("zdown_force_stop", self.zdown_force_stop)
        buttons = self.printer.load_object(config, 'buttons')
        buttons.register_buttons(self.endstop_pin_z, self._button_handler)
        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler(
            'klippy:shutdown', self._handle_shutdown)
        self.printer.register_event_handler(
            'stepper_enable:motor_off', self._handle_motor_off)
        self.printer.register_event_handler(
            'gcode:request_restart', self._handle_request_restart)

    def _build_config(self):
        config_z_align = "config_z_align oid=%d" % (self._oidz,)
        self._main_mcu.add_config_cmd(config_z_align)
        for stepper_index, endstop_pin in enumerate(self.endstop_pin_z):
            section_name = 'stepper_z' if stepper_index == 0 else 'stepper_z%d' % (stepper_index,)
            section = self.config.getsection(section_name)
            step_pin = section.get('step_pin')
            dir_pin = section.get('dir_pin')
            if dir_pin.startswith('!'):
                dir_pin = dir_pin[1:]
            config_z_align_add = (
                "config_z_align_add oid=%d z_indx=%d zs_pin=%s zd_pin=%s "
                "zd_up=%d zes_pin=%s zes_untrig=%d"
                % (
                    self._oidz,
                    stepper_index,
                    step_pin,
                    dir_pin,
                    self.zd_up,
                    endstop_pin,
                    self.zes_untrig,
                ))
            self._main_mcu.add_config_cmd(config_z_align_add)
            _klog(
                "config_z_align_add oid=%d z_indx=%d zs_pin=%s "
                "zd_pin=%s zd_up=%d zes_pin=%s zes_untrig=%d",
                self._oidz, stepper_index, step_pin, dir_pin, self.zd_up,
                endstop_pin, self.zes_untrig)

    def get_switch_states(self, state):
        return [1 if (state & (1 << i)) == 0 else 0 for i in range(self.pin_len)]

    def _button_handler(self, _eventtime, state):
        self.endstop_pin_status = self.get_switch_states(state)

    def _handle_connect(self):
        self._toolhead = self.printer.lookup_object('toolhead')
        self._install_status_cache_hook()
        self._z_align_query_cmd = self._main_mcu.lookup_query_command(
            "query_z_align oid=%c enable=%c quickSpeed=%u slowSpeed=%u"
            " risingDist=%u filterCnt=%c safeDist=%u",
            "z_align_status oid=%c flag=%i deltaError1=%i",
            oid=self._oidz)
        self._z_align_force_stop_cmd = self._main_mcu.lookup_command(
            "z_align_force_stop oid=%c", cq=None)
        steppers = {
            stepper.get_name(): stepper
            for stepper in self._toolhead.get_kinematics().get_steppers()
        }
        stepper_z = steppers.get('stepper_z')
        if stepper_z is None:
            raise self.printer.config_error(
                'z_align requires the stepper_z object')
        self._step_distance = stepper_z.get_step_dist()
        self._reset_runtime()

    def _handle_ready(self):
        try:
            self._install_tilt_learning()
        except Exception:
            self._z_tilt = None
            _klog('learned Z-tilt setup failed; continuing without it',
                  level=logging.exception)

    def _install_tilt_learning(self):
        self._load_tilt_bias()
        z_tilt = self.printer.lookup_object('z_tilt', None)
        if z_tilt is None or len(z_tilt.z_helper.z_steppers) != 2:
            return
        original_finalize = z_tilt.probe_helper.finalize_callback
        original_adjust = z_tilt.z_helper.adjust_steppers

        def finalize(offsets, positions):
            try:
                result = original_finalize(offsets, positions)
            except Exception:
                self._learn_delta = None
                raise
            if self._learn_delta is not None and z_tilt.z_status.applied:
                learned = self._tilt_bias + self._learn_delta
                self._learn_delta = None
                self._save_tilt_bias(learned)
            return result

        def adjust(adjustments, speed):
            result = original_adjust(adjustments, speed)
            if self._learn_delta is not None:
                try:
                    self._learn_delta += (
                        float(adjustments[1]) - float(adjustments[0]))
                except Exception:
                    self._learn_delta = None
                    _klog('discarding invalid Z-tilt learning data',
                          level=logging.exception)
            return result

        z_tilt.probe_helper.finalize_callback = finalize
        z_tilt.z_helper.adjust_steppers = adjust
        self._z_tilt = z_tilt

    def _handle_motor_off(self, _print_time):
        self._learn_delta = None
        self._bottom_anchor = None

    def _load_tilt_bias(self):
        self._tilt_bias = 0.0
        try:
            with open(TILT_BIAS_FILE) as stream:
                value = float(json.load(stream)['bias_mm'])
            if not math.isfinite(value) or abs(value) > MAX_TILT_BIAS:
                raise ValueError('bias outside +/-%.1fmm' % MAX_TILT_BIAS)
            self._tilt_bias = value
        except FileNotFoundError:
            pass
        except Exception:
            _klog('ignoring invalid tilt-bias file %s', TILT_BIAS_FILE,
                  level=logging.exception)

    def _save_tilt_bias(self, value):
        try:
            value = round(float(value), 6)
            if not math.isfinite(value) or abs(value) > MAX_TILT_BIAS:
                raise ValueError('learned bias outside +/-%.1fmm' % MAX_TILT_BIAS)
            os.makedirs(os.path.dirname(TILT_BIAS_FILE), exist_ok=True)
            temp = TILT_BIAS_FILE + '.tmp'
            with open(temp, 'w') as stream:
                json.dump({'version': 1, 'bias_mm': value}, stream)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, TILT_BIAS_FILE)
            self._tilt_bias = value
            _klog('learned Z-tilt bias %.6fmm', value)
        except Exception:
            _klog('unable to save learned Z-tilt bias',
                  level=logging.exception)

    def _apply_tilt_bias(self):
        if self._z_tilt is None:
            return False
        if self._tilt_bias:
            self._z_tilt.z_helper.adjust_steppers(
                [0.0, self._tilt_bias], self.settle_drop_speed)
            self._toolhead.wait_moves()
        self._learn_delta = 0.0
        return bool(self._tilt_bias)

    def _handle_z_align_status(self, params, source="callback"):
        self._z_align_status = dict(params)
        _klog(
            "status[%s] oid=%s flag=%s delta=%s sent=%s recv=%s",
            source,
            params.get('oid'),
            params.get('flag'),
            params.get('deltaError1'),
            params.get('#sent_time'),
            params.get('#receive_time'))

    def _install_status_cache_hook(self):
        serial = self._main_mcu._serial
        if self._orig_serial_handle_default is not None:
            return
        self._orig_serial_handle_default = serial.handle_default

        def wrapped_handle_default(params):
            if isinstance(params, dict) and params.get("#name") == "z_align_status":
                self._handle_z_align_status(params, source="default")
                return
            return self._orig_serial_handle_default(params)

        serial.handle_default = wrapped_handle_default
        _klog("installed serial default hook for z_align_status")

    def _handle_request_restart(self, _print_time):
        self._learn_delta = None
        self._bottom_anchor = None
        self._reset_runtime()

    def _handle_shutdown(self):
        self.reactor.update_timer(self._timer, self.reactor.NEVER)
        self._reset_runtime()

    def _reset_runtime(self):
        self._state = 'idle'
        self._error = None
        self._phase_deadline = None
        self._prepared_zmax = None
        self._target_z = None
        self._logical_z_applied = False
        self._settle_attempt = 0
        self._last_retry_delta_mm = 0.0
        self._last_retry_delta_steps = 0
        self._z_align_status = {}
        self.force_stop_flag = False

    def _say(self, msg):
        _klog('%s', msg)
        try:
            self.gcode.respond_info('Z prep: %s' % (msg,))
        except Exception:
            _klog(
                'failed to echo console message',
                level=logging.exception)

    def _request_homing_abort(self, reason):
        homing = self.printer.lookup_object('homing')
        if not homing.has_active_homing_session():
            return
        homing.request_homing_abort(
            reason=reason,
            detail={'source': 'z_align'},
            abort_z_align=False)

    def _ensure_ready(self):
        if self._toolhead is None:
            raise self.printer.command_error('Z_ALIGN is not connected')
        if self._z_align_query_cmd is None or self._z_align_force_stop_cmd is None:
            raise self.printer.command_error('Z_ALIGN MCU path is not connected')

    def _is_toolhead_z_homed(self):
        if self._toolhead is None:
            return False
        try:
            eventtime = self.reactor.monotonic()
            homed_axes = self._toolhead.get_kinematics().get_status(
                eventtime).get('homed_axes', '')
        except Exception:
            _klog(
                'failed reading toolhead Z homed state',
                level=logging.exception)
            return False
        return 'z' in homed_axes

    def _homing_session_aborted(self):
        homing = self.printer.lookup_object('homing')
        return bool(homing.is_homing_session_aborted())

    def _get_homing_abort_reason(self):
        homing = self.printer.lookup_object('homing')
        return homing.motor_fault_abort_reason

    def _z_protection_active(self):
        motor_control = self.printer.lookup_object('motor_control')
        try:
            result = motor_control.query_kinematic_protection_status(
                data=11, timeout=MOTOR_COMMAND_TIMEOUT)
        except Exception as err:
            _klog(
                'failed querying Z protection before logical Z apply',
                level=logging.exception)
            raise self.printer.command_error(
                'Z prep could not verify Z motor protection state: %s'
                % (err,))
        return any(bool(result.get(axis, {}).get('has_error'))
                   for axis in ('z', 'z1'))

    def _calc_speed_ticks(self, speed):
        mcu_freq = self._main_mcu._serial.msgparser.get_constant_float('CLOCK_FREQ')
        return max(1, int((self._step_distance / speed) * mcu_freq / 2.0))

    def _calc_distance_steps(self, distance):
        return max(1, int(distance / self._step_distance) * 2)

    def _enable_z_align_steppers(self):
        stepper_enable = self.printer.lookup_object('stepper_enable', None)
        if stepper_enable is None or not hasattr(stepper_enable, 'motor_debug_enable'):
            raise self.printer.command_error(
                'Z_ALIGN requires stepper_enable.motor_debug_enable')
        for stepper_index in range(len(self.endstop_pin_z)):
            stepper_name = 'stepper_z' if stepper_index == 0 else 'stepper_z%d' % (
                stepper_index,)
            try:
                stepper_enable.motor_debug_enable(stepper_name, 1)
            except Exception as err:
                raise self.printer.command_error(
                    'Z_ALIGN failed enabling %s before MCU z_align: %s'
                    % (stepper_name, err))

    def _force_stop_mcu_z_align(self):
        if self._z_align_force_stop_cmd is None:
            return
        try:
            self._z_align_force_stop_cmd.send([self._oidz])
        except Exception:
            _klog(
                'failed to force stop MCU z_align',
                level=logging.exception)

    def _consume_startup_rise_prime(self):
        homing = self.printer.lookup_object('homing')
        return bool(homing.consume_z_align_rise_startup_prime())

    def _start_mcu_z_align_attempt(self):
        quick_ticks = self._calc_speed_ticks(self.fast_drop_speed)
        slow_ticks = self._calc_speed_ticks(self.settle_drop_speed)
        rising_steps = self._calc_distance_steps(self.mcu_rising_dist)
        safe_steps = self._calc_distance_steps(self.safe_dist)
        if not self._settle_attempt:
            self._settle_attempt = 1
        self._z_align_status = {}
        result = self._z_align_query_cmd.send([
            self._oidz, 1, quick_ticks, slow_ticks,
            rising_steps, self.mcu_filter_count, safe_steps])
        _klog("initial query_z_align response=%s", result)
        self._phase_deadline = self.reactor.monotonic() + self.timeout
        self._state = 'mcu_wait'

    def _poll_mcu_z_align(self, eventtime):
        if self.force_stop_flag:
            self.force_stop_flag = False
            self._force_stop_mcu_z_align()
            self._fail('MCU z_align force-stopped', request_homing_abort=True)
            return self.reactor.NEVER
        if self._phase_deadline is not None and eventtime > self._phase_deadline:
            _klog("timeout waiting status=%s", self._z_align_status)
            self._force_stop_mcu_z_align()
            self._fail(
                'timed out waiting for MCU z_align',
                request_homing_abort=True)
            return self.reactor.NEVER
        status = self._z_align_status
        flag = int(status.get('flag', 0) or 0)
        if flag == 0:
            return eventtime + POLL_INTERVAL
        if flag == 2:
            self._force_stop_mcu_z_align()
            if self._settle_attempt >= self.retries:
                self._fail(
                    'MCU z_align reported photoelectric error'
                    ' after %d/%d attempts'
                    % (self._settle_attempt, self.retries),
                    request_homing_abort=True)
                return self.reactor.NEVER
            self._say(
                'mcu z_align reported photoelectric error; retrying attempt %d/%d'
                % (self._settle_attempt + 1, self.retries))
            self._settle_attempt += 1
            self._phase_deadline = None
            self._state = 'mcu_start'
            return self.reactor.NOW
        delta_steps = int(status.get('deltaError1', 0) or 0)
        delta_mm = delta_steps * (self._step_distance or 0.0)
        self._last_retry_delta_steps = delta_steps
        self._last_retry_delta_mm = delta_mm
        tolerance_mm = self.retry_tolerance * (self._step_distance or 0.0)
        if abs(delta_steps) <= self.retry_tolerance:
            self._say(
                'MCU z-align attempt %d/%d delta %.4fmm (%d steps)'
                % (self._settle_attempt, self.retries, delta_mm, delta_steps))
            self._phase_deadline = None
            self._prepared_zmax = self.zmax
            self._target_z = max(0.0, self._prepared_zmax - self.rise_distance)
            self._state = 'prepared'
            return self.reactor.NEVER
        if self._settle_attempt >= self.retries:
            self._fail(
                'too many MCU z_align retries:'
                ' delta_steps=%d retry_tolerance=%d retries=%d'
                % (delta_steps, self.retry_tolerance, self.retries),
                request_homing_abort=True)
            return self.reactor.NEVER
        self._settle_attempt += 1
        self._say(
            'MCU z-align attempt %d/%d delta %.4fmm (%d steps) exceeds tolerance %.4fmm'
            % (self._settle_attempt - 1, self.retries,
               delta_mm, delta_steps, tolerance_mm))
        self._state = 'mcu_start'
        return self.reactor.NOW

    def _get_z_home_endstops(self):
        kin = self._toolhead.get_kinematics()
        rails = getattr(kin, 'rails', None)
        if rails is None or len(rails) < 3:
            raise self.printer.command_error(
                'Z prep requires Z rail endstops for safety-monitored rise')
        return rails[2].get_endstops()

    def _get_z_limit_snapshot(self):
        kin = self._toolhead.get_kinematics()
        if (not hasattr(kin, 'max_z_velocity')
                or not hasattr(kin, 'max_z_accel')):
            raise self.printer.command_error(
                'Z prep requires kinematics support for max_z_velocity/max_z_accel')
        return {
            'max_z_velocity': kin.max_z_velocity,
            'max_z_accel': kin.max_z_accel,
        }

    def _apply_temp_z_limits(self):
        kin = self._toolhead.get_kinematics()
        snapshot = self._get_z_limit_snapshot()
        kin.max_z_velocity = TEMP_RISE_MAX_Z_VELOCITY
        kin.max_z_accel = self.temp_rise_max_z_accel
        return snapshot

    def _restore_z_limits(self, snapshot):
        if snapshot is None:
            return
        kin = self._toolhead.get_kinematics()
        kin.max_z_velocity = snapshot['max_z_velocity']
        kin.max_z_accel = snapshot['max_z_accel']

    def _fail(self, msg, request_homing_abort=False):
        self._say(msg)
        self._error = msg
        self._state = 'error'
        self.invalidate_homing_state()
        if request_homing_abort:
            self._request_homing_abort(msg)

    def _handle_timer(self, eventtime):
        try:
            while True:
                if self._state == 'mcu_start':
                    self._start_mcu_z_align_attempt()
                    continue
                if self._state == 'mcu_wait':
                    return self._poll_mcu_z_align(eventtime)
                return self.reactor.NEVER
        except Exception:
            _klog('crashed', level=logging.exception)
            self._fail(
                'crashed; check klippy.log',
                request_homing_abort=True)
            return self.reactor.NEVER

    def is_active(self):
        return self._state in ('mcu_start', 'mcu_wait', 'prepared', 'blocking_rise')

    def needs_prep(self):
        return not self._is_toolhead_z_homed()

    def start_prepare(self):
        self._ensure_ready()
        if self._is_toolhead_z_homed():
            return False
        if self._state not in ('idle', 'error'):
            raise self.printer.command_error('Z prep is already running')
        self._learn_delta = None
        self._bottom_anchor = None
        self._reset_runtime()
        self._enable_z_align_steppers()
        self._state = 'mcu_start'
        self.reactor.update_timer(self._timer, self.reactor.NOW)
        return True

    def wait_prepare_complete(self):
        if self._is_toolhead_z_homed():
            return {
                'skipped': True,
                'z_known': self._is_toolhead_z_homed(),
            }
        self._ensure_ready()
        while True:
            if self._state == 'prepared':
                return {
                    'prepared_zmax': self._prepared_zmax,
                    'target_z': self._target_z,
                    'delta_steps': self._last_retry_delta_steps,
                    'delta_mm': self._last_retry_delta_mm,
                    'skipped': False,
                    'z_known': self._is_toolhead_z_homed(),
                }
            if self._state == 'error':
                raise self.printer.command_error(
                    self._error or 'Z prep failed')
            if self._state == 'idle':
                raise self.printer.command_error(
                    'Z prep was not started before wait_prepare_complete')
            eventtime = self.reactor.monotonic()
            self.reactor.pause(eventtime + 0.100)

    def _z_steppers(self):
        return list(self._toolhead.get_kinematics().rails[2].get_steppers())

    def validate_reference_frame(self, frame):
        try:
            values = [float(value) for value in frame]
            steppers = self._z_steppers()
            z_min, z_max = self._toolhead.get_kinematics().rails[2].get_range()
            if len(values) != len(steppers) or len(values) != 2:
                raise ValueError('expected two Z positions')
            if not all(math.isfinite(value) for value in values):
                raise ValueError('non-finite Z position')
            if min(values) < z_min or max(values) > z_max:
                raise ValueError('Z position outside configured range')
            if max(values) - min(values) > (
                    MAX_TILT_BIAS + max(s.get_step_dist() for s in steppers)):
                raise ValueError('Z differential exceeds limit')
            return values
        except Exception as err:
            raise self.printer.command_error(
                'Invalid Z reference frame: %s' % err)

    def _record_bottom_anchor(self):
        try:
            self._bottom_anchor = [
                stepper.get_mcu_position() for stepper in self._z_steppers()]
        except Exception:
            self._bottom_anchor = None
            _klog('unable to capture PLR Z anchor', level=logging.exception)

    def capture_reference_frame(self):
        if self._bottom_anchor is None or not self._logical_z_applied:
            return None
        try:
            return self.validate_reference_frame([
                stepper.mcu_to_commanded_position(anchor)
                for stepper, anchor in zip(
                    self._z_steppers(), self._bottom_anchor)
            ])
        except Exception:
            return None

    def _apply_reference_frame(self, frame):
        values = self.validate_reference_frame(frame)
        common = max(values)
        adjustments = [common - value for value in values]
        adjusted = max(adjustments) > 1.0e-9
        if adjusted:
            if self._z_tilt is None:
                raise self.printer.command_error(
                    'Z reference restore requires z_tilt')
            self._z_tilt.z_helper.adjust_steppers(
                adjustments, self.settle_drop_speed)
            self._toolhead.wait_moves()
        restored = self.capture_reference_frame()
        if restored is None or any(
                abs(actual - expected) > stepper.get_step_dist() * 1.5
                for actual, expected, stepper in zip(
                    restored, values, self._z_steppers())):
            raise self.printer.command_error('Z reference restore did not verify')
        return adjusted

    def perform_blocking_rise(
            self, monitored=True, target_z=None, rise_speed=None,
            reference_frame=None):
        if self._is_toolhead_z_homed():
            return False
        self._ensure_ready()
        if self._state != 'prepared':
            self.wait_prepare_complete()
        if self._homing_session_aborted():
            self.invalidate_homing_state()
            raise self.printer.command_error(
                'Z prep refusing blocking rise after an aborted homing session')
        if monitored and self._z_protection_active():
            self.invalidate_homing_state()
            raise self.printer.command_error(
                'Z prep refusing blocking rise while a Z protection fault is active')
        zmax = self._prepared_zmax if self._prepared_zmax is not None else self.zmax
        frame = (
            self.validate_reference_frame(reference_frame)
            if reference_frame is not None else None)
        reference_z = max(frame) if frame is not None else zmax
        default_target = self._target_z if self._target_z is not None else max(
            0.0, zmax - self.rise_distance)
        target_z = default_target if target_z is None else float(target_z)
        move_speed = self.rise_speed if rise_speed is None else float(rise_speed)
        z_min, z_limit = self._toolhead.get_kinematics().rails[2].get_range()
        margin = (
            max(frame) - min(frame) if frame is not None
            else abs(self._tilt_bias) if self._z_tilt is not None else 0.0)
        if (not math.isfinite(target_z) or not math.isfinite(move_speed)
                or move_speed <= 0.0
                or target_z < z_min
                or target_z > min(zmax, z_limit - margin)):
            raise self.printer.command_error('Invalid targeted Z rise')
        run_startup_prime = self._consume_startup_rise_prime()
        z_limit_snapshot = None
        try:
            self._state = 'blocking_rise'
            self._toolhead.get_last_move_time()
            z_limit_snapshot = self._apply_temp_z_limits()
            from .homing import HomingMove
            if monitored:
                endstops = self._get_z_home_endstops()
            else:
                kin = self._toolhead.get_kinematics()
                z_steppers = [s for s in kin.get_steppers() if 'z' in s.get_name()]
                endstops = [(ZAlignNullEndstop(z_steppers, self.reactor),
                             'z_rise')]
            pos = list(self._toolhead.get_position())
            self._logical_z_applied = True
            self._target_z = target_z
            if run_startup_prime:
                prime_dist = min(
                    STARTUP_RISE_PRIME_MAX_DIST,
                    max(0.1, self.mcu_rising_dist * 0.5))
                prime_start = list(pos)
                prime_start[2] = reference_z - prime_dist
                self._toolhead.set_position(prime_start, homing_axes="z")
                prime_target = list(prime_start)
                prime_target[2] = reference_z
                _klog(
                    'priming first %s Z move after restart dist=%.3f speed=%.3f',
                    'normal' if monitored else 'unmonitored',
                    prime_dist, STARTUP_RISE_PRIME_SPEED)
                prime_hmove = HomingMove(
                    self.printer, endstops, self._toolhead)
                prime_hmove.homing_move(
                    prime_target, STARTUP_RISE_PRIME_SPEED,
                    triggered=monitored, check_triggered=False)
            pos[2] = reference_z
            self._toolhead.set_position(pos, homing_axes="z")
            self._record_bottom_anchor()
            target = list(pos)
            target[2] = target_z
            hmove = HomingMove(self.printer, endstops, self._toolhead)
            hmove.homing_move(
                target, move_speed, triggered=monitored, check_triggered=False)
            if monitored and getattr(hmove, 'triggered_endstops', ()):
                raise self.printer.command_error(
                    'Z prep safety stop: Z endstop triggered during fast rise (%s)'
                    % (','.join(hmove.triggered_endstops),))
            if self._homing_session_aborted():
                raise self.printer.command_error(
                    self._get_homing_abort_reason()
                    or 'Z prep blocking rise aborted with the homing session')
            if monitored and self._z_protection_active():
                raise self.printer.command_error(
                    'Z prep refusing to finish the blocking rise while'
                    ' Z protection fault is active')
            if frame is None:
                adjusted = self._apply_tilt_bias()
            else:
                adjusted = self._apply_reference_frame(frame)
            if adjusted and self._homing_session_aborted():
                raise self.printer.command_error(
                    self._get_homing_abort_reason()
                    or 'Z prep blocking rise aborted with the homing session')
            if adjusted and monitored and self._z_protection_active():
                raise self.printer.command_error(
                    'Z prep refusing to finish after Z reference adjustment')
            self._state = 'idle'
            return True
        except Exception:
            self.invalidate_homing_state()
            raise
        finally:
            try:
                self._restore_z_limits(z_limit_snapshot)
            except Exception:
                _klog(
                    'failed to restore Z rise limits',
                    level=logging.exception)

    def perform_unmonitored_rise(self):
        """Rise through the null-endstop reference-frame path."""
        return self.perform_blocking_rise(monitored=False)

    def invalidate_homing_state(self):
        self._logical_z_applied = False
        self._prepared_zmax = None
        self._target_z = None
        self._learn_delta = None
        self._bottom_anchor = None

    def abort_internal(self, reason='Z prep aborted', motor_off=False,
                       restore_motor_mode=False, wait_until_safe=False):
        if self._state in ('idle', 'error'):
            return False
        self.force_stop_flag = True
        self._force_stop_mcu_z_align()
        self._fail(reason, request_homing_abort=False)
        if motor_off:
            try:
                self.printer.lookup_object('stepper_enable').motor_off()
            except Exception:
                _klog(
                    'failed to motor_off during abort',
                    level=logging.exception)
        return True

    def zdown_force_stop(self, web_request):
        self.force_stop_flag = True
        self._force_stop_mcu_z_align()
        web_request.send({"result": "success"})

    def cmd_ZDOWN_FORCE_STOP(self, _gcmd):
        self.force_stop_flag = True
        self._force_stop_mcu_z_align()

    def cmd_ZDOWN(self, _gcmd):
        self.force_stop_flag = False
        try:
            started = self.start_prepare()
            if started:
                self.wait_prepare_complete()
                self.perform_blocking_rise()
        except self.printer.command_error as err:
            msg = str(err)
            if 'timed out waiting for MCU z_align' in msg:
                return MOTOR_ZDOWN_TIMEOUT
            return MOTOR_PROTECT_ERROR
        return 0

    def get_status(self, _eventtime):
        return {
            'endstop_pin_status': list(self.endstop_pin_status),
            'state': self._state,
            'target_z': self._target_z,
            'prepared_zmax': self._prepared_zmax,
            'z_known': self._is_toolhead_z_homed(),
            'logical_z_applied': self._logical_z_applied,
            'last_retry_delta_steps': self._last_retry_delta_steps,
            'last_retry_delta_mm': self._last_retry_delta_mm,
            'error': self._error or '',
        }


def load_config(config):
    return ZAlign(config)
