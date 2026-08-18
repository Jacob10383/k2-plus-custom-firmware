# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
# prtouch - K2 nozzle load-cell Z probe (CS1237 + cross-MCU swap endstop)
import logging
import random
from statistics import median
from types import SimpleNamespace

from . import probe

_POST_HOME_LIFT = 3.0


def _sample_range(values):
    return max(values) - min(values)


def _decode_prtouch_frame(payload, signed_first):
    """Decode the nozzle firmware's delta-packed integer array."""
    payload = bytes(payload)
    if not payload:
        return []
    count = payload[0]
    descriptor_len = (count + 3) // 4
    data_pos = 1 + descriptor_len
    if len(payload) < data_pos:
        raise ValueError("truncated descriptor")
    descriptors = payload[1:data_pos]
    values = []
    for index in range(count):
        descriptor = descriptors[-1 - index // 4]
        width = ((descriptor >> (2 * (index % 4))) & 3) + 1
        if data_pos + width > len(payload):
            raise ValueError("truncated data")
        raw = payload[data_pos:data_pos + width]
        data_pos += width
        value = int.from_bytes(
            raw, 'little', signed=signed_first if index == 0 else True)
        if index:
            value += values[-1]
        values.append(value)
    return values


def _best_subset(values, size):
    if len(values) < size:
        return None
    values = sorted(values)
    start = min(
        range(len(values) - size + 1),
        key=lambda i: values[i + size - 1] - values[i])
    return values[start:start + size]


def _thermal_z_comp(rate_mm_c, print_temp, touch_temp):
    """Z shift so a touch at touch_temp is correct when printing at print_temp.

    Hotter nozzle grows toward the bed. Label contact lower by rate*(T_print -
    T_touch) so that after heating, tip-bed contact lands at z_offset.
    """
    return rate_mm_c * (print_temp - touch_temp)


class _PRTouchPrinterProbe(probe.PrinterProbe):
    """PrinterProbe without axis-twist correction for a nozzle probe."""

    def _probe(self, speed, gcmd):
        epos, is_good = self.probing_move(speed, gcmd)
        self.gcode.respond_info(
            "probe at %.3f,%.3f is z=%.6f"
            % (epos[0], epos[1], epos[2]))
        return epos[:3], is_good


class PRTouchEndstopWrapper:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.ppins = self.printer.lookup_object('pins')

        self.position_endstop = config.getfloat('z_offset')
        # When true: claim Klipper probe object + probe:z_virtual_endstop.
        # When false: PRTOUCH_HOME still works; endstop chip is prtouch:.
        self.register_as_probe = config.getboolean('register_as_probe', True)
        self.speed = config.getfloat('speed', 5., above=0.)
        self.lift_speed = config.getfloat('lift_speed', self.speed, above=0.)
        self.sample_retract_dist = config.getfloat(
            'sample_retract_dist', 2., above=0.)
        # Always consume PrinterProbe knobs so they stay valid when
        # register_as_probe is false (otherwise Klipper errors unused options).
        self.sample_count = config.getint('samples', 1, minval=1)
        self.samples_result = config.getchoice(
            'samples_result', ['median', 'average'], 'average')
        self.samples_tolerance = config.getfloat(
            'samples_tolerance', 0.100, minval=0.)
        self.samples_retries = config.getint(
            'samples_tolerance_retries', 0, minval=0)
        self.no_trigger_retries = config.getint(
            'no_trigger_retries', 1, minval=0, maxval=3)
        self.no_trigger_retry_max_distance = config.getfloat(
            'no_trigger_retry_max_distance', 10., above=0.)
        self.step_swap_pin = config.get('step_swap_pin', '!PC7')
        self.pres_swap_pin = config.get('pres_swap_pin', 'nozzle_mcu:PA15')
        self.pres_cfg_regs = config.getint(
            'pres_cfg_regs', 60, minval=0, maxval=255)
        self.pres_tri_hold = config.getintlist(
            'pres_tri_hold', [4000, 10000, 500], count=3)
        # Match CS1237 rate from pres_cfg_regs (60 -> 1280 SPS -> 0.78125ms).
        self.pres_acq_tkms = config.getfloat(
            'pres_acq_tkms', 0.78125, minval=0.1, maxval=1000.)
        self.pres_tri_fter = config.getfloatlist(
            'pres_tri_fter', [5., 1., 0.8], count=3)
        self.pres_ded_tkms = config.getfloat(
            'pres_ded_tkms', 128., minval=0., maxval=2000.)
        # K2: inverted !PC7 is open when nozzle MCU drives idle=1.
        self.pres_idle_swap_state = config.getint(
            'pres_idle_swap_state', 1, minval=0, maxval=1)
        self.pres_release_timeout = config.getfloat(
            'pres_release_timeout', 0.250, above=0.)
        self.pres_ack_timeout = config.getfloat(
            'pres_ack_timeout', 0.250, above=0.)
        self.pres_rearm_delay = config.getfloat(
            'pres_rearm_delay', 0.010, minval=0.)

        # PRTOUCH_HOME: multi-sample Z home (same probe / z_offset as G28 Z)
        self.home_xy = config.getfloatlist('home_xy', (175., 175.), count=2)
        self.home_samples = config.getint('home_samples', 3, minval=1)
        self.home_max_samples = config.getint(
            'home_max_samples', max(10, self.home_samples),
            minval=self.home_samples)
        self.home_max_noisy = config.getint('home_max_noisy', 3, minval=0)
        self.home_sample_range = config.getfloat(
            'home_sample_range', 0.010, minval=0.)
        self.home_travel_speed = config.getfloat(
            'home_travel_speed', 200., above=0.)
        self.home_z_hop = config.getfloat('home_z_hop', 2., above=0.)
        # Nozzle/stack growth (mm/°C). Used only when PRTOUCH_HOME PRINT_TEMP= is set.
        self.thermal_expansion = config.getfloat(
            'thermal_expansion', 0., minval=0.)

        # PRTOUCH_SCRUB: detect the flexible rear tab, then scrub its surface.
        self.scrub_x_start = config.getfloat('scrub_x_start', 173.)
        self.scrub_x_end = config.getfloat('scrub_x_end', 223.)
        self.scrub_y_min = config.getfloat('scrub_y_min', 353.)
        self.scrub_y_max = config.getfloat('scrub_y_max', 356.)
        if self.scrub_y_min > self.scrub_y_max:
            raise config.error(
                "prtouch: scrub_y_min must not exceed scrub_y_max")
        self.scrub_detect_hold_ratio = config.getfloat(
            'scrub_detect_hold_ratio', 2.5, above=1.)
        self.scrub_detect_deflection = config.getfloat(
            'scrub_detect_deflection', .035, above=0.)
        self.scrub_tab_depth = config.getfloat(
            'scrub_tab_depth', .15, minval=0., maxval=.20)
        self.scrub_no_tab_depth = config.getfloat(
            'scrub_no_tab_depth', .05, minval=0., maxval=.20)
        if self.scrub_no_tab_depth > self.scrub_tab_depth:
            raise config.error(
                "prtouch: scrub_no_tab_depth must not exceed scrub_tab_depth")
        self.scrub_speed = config.getfloat(
            'scrub_speed', 10., above=0.)

        if config.has_section('stepper_z'):
            zconfig = config.getsection('stepper_z')
            self.z_position = zconfig.getfloat(
                'position_min', 0., note_valid=False)
        else:
            pconfig = config.getsection('printer')
            self.z_position = pconfig.getfloat(
                'minimum_z_position', 0., note_valid=False)

        # Set by load_config when register_as_probe is true.
        self._printer_probe = None

        self.pres_cs_pins = []
        for i in range(8):
            default_pin = (
                'nozzle_mcu:PB13, nozzle_mcu:PB14' if i == 0 else None)
            pin_val = config.get('pres_cs%d_pin' % i, default_pin)
            if pin_val:
                self.pres_cs_pins.append(pin_val)
        if not self.pres_cs_pins:
            raise config.error("prtouch: at least one pres_csN_pin required")

        self.step_mcu = self.ppins.parse_pin(
            self.step_swap_pin, True, True)['chip']
        self.pres_mcu = self.ppins.parse_pin(
            self.pres_swap_pin, True, True)['chip']
        self.step_oid = self.step_mcu.create_oid()
        self.pres_oid = self.pres_mcu.create_oid()

        pin_params = self.ppins.lookup_pin(
            self.step_swap_pin, can_invert=True, can_pullup=True)
        self.mcu_endstop = self.step_mcu.setup_pin('endstop', pin_params)
        self.get_mcu = self.mcu_endstop.get_mcu
        self.add_stepper = self.mcu_endstop.add_stepper
        self.get_steppers = self.mcu_endstop.get_steppers
        self.query_endstop = self.mcu_endstop.query_endstop
        self._mcu_home_wait = self.mcu_endstop.home_wait

        # _armed means both MCUs acked start with err=0 — not "we sent start".
        self._armed = False
        self._last_probe_no_trigger = False
        self._last_probe_cleanup_ok = False
        self._last_home_wait_result = None
        self._probing_move_active = False
        self._step_acq_tick = None
        self._pres_acq_tick = None
        self._ack_by_source_oid = {}
        self._last_ack = None
        self.start_step_cmd = None
        self.stop_step_cmd = None
        self.start_pres_cmd = None
        self.stop_pres_cmd = None
        self.read_pres_cmd = None
        self._pres_clock_freq = None
        self._pres_hold_ratio = 1.

        self.step_mcu.register_config_callback(self._build_step_config)
        self.pres_mcu.register_config_callback(self._build_pres_config)
        self.pres_mcu.register_response(
            lambda params: self._handle_ack('pres', params),
            'ack_prtouch', self.pres_oid)
        self.step_mcu.register_response(
            lambda params: self._handle_ack('step', params),
            'ack_prtouch', self.step_oid)

        self.printer.register_event_handler(
            'klippy:mcu_identify', self._handle_mcu_identify)
        self.printer.register_event_handler(
            'klippy:shutdown', self._force_disarm)
        self.printer.register_event_handler(
            'stepper_enable:motor_off', self._force_disarm)

        self.gcode.register_command(
            'PRTOUCH_HOME', self.cmd_PRTOUCH_HOME,
            desc=self.cmd_PRTOUCH_HOME_help)
        self.gcode.register_command(
            'PRTOUCH_SCAN_CALIBRATE', self.cmd_PRTOUCH_SCAN_CALIBRATE,
            desc=self.cmd_PRTOUCH_SCAN_CALIBRATE_help)
        self.gcode.register_command(
            'PRTOUCH_AXIS_TWIST_COMPENSATION',
            self.cmd_PRTOUCH_AXIS_TWIST_COMPENSATION,
            desc=self.cmd_PRTOUCH_AXIS_TWIST_COMPENSATION_help)
        self.gcode.register_command(
            'PRTOUCH_SCRUB', self.cmd_PRTOUCH_SCRUB,
            desc=self.cmd_PRTOUCH_SCRUB_help)

        if not self.register_as_probe:
            # Alternate chip so carto (or another probe) can own "probe".
            self.ppins.register_chip('prtouch', self)
            # PrinterProbe normally arms via these; wire them ourselves.
            self.printer.register_event_handler(
                'homing:homing_move_begin', self._handle_homing_move_begin)
            self.printer.register_event_handler(
                'homing:homing_move_end', self._handle_homing_move_end)
            self.printer.register_event_handler(
                'gcode:command_error', self._handle_command_error)

    def _handle_mcu_identify(self):
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis('z'):
                self.add_stepper(stepper)

    def _handle_ack(self, source, params):
        oid = params.get('oid')
        self._last_ack = params
        if oid is not None:
            self._ack_by_source_oid[(source, oid)] = params
        err = params.get('err', 0)
        if err:
            logging.warning(
                "prtouch: %s ack err=%s expar0=%s expar1=%s oid=%s",
                source, err, params.get('expar0'), params.get('expar1'), oid)

    def _await_ack_ok(self, source, oid, label, expected_expar0):
        """Block until ack_prtouch for oid arrives with err=0."""
        key = (source, oid)
        deadline = self.reactor.monotonic() + self.pres_ack_timeout
        while True:
            ack = self._ack_by_source_oid.pop(key, None)
            if ack is not None:
                if (
                        ack.get('expar0', 0) != expected_expar0
                        or ack.get('expar1', 0) != 0):
                    continue
                err = ack.get('err', 0)
                if err:
                    raise self.printer.command_error(
                        "prtouch: %s failed (ack err=%s expar0=%s expar1=%s)"
                        % (label, err, ack.get('expar0'), ack.get('expar1')))
                return
            now = self.reactor.monotonic()
            if now >= deadline:
                raise self.printer.command_error(
                    "prtouch: %s ack timeout" % (label,))
            self.reactor.pause(min(deadline, now + .005))

    def _run_checked(
            self, source, cmd, params, oid, label, expected_expar0):
        """Send a start command and require a clean MCU ack before continuing."""
        self._ack_by_source_oid.pop((source, oid), None)
        cmd.send(params)
        self._await_ack_ok(source, oid, label, expected_expar0)

    def _build_step_config(self):
        toolhead = self.printer.lookup_object('toolhead')
        kin_name = str(self.config.getsection('printer').get('kinematics'))
        is_corexz = kin_name == 'corexz'

        oid_zstp = oid_xstp = oid_ystp = 0
        for stepper in toolhead.get_kinematics().get_steppers():
            name = stepper.get_name()
            if name == 'stepper_z':
                oid_zstp = stepper.get_oid()
            elif name == 'stepper_x' and is_corexz:
                oid_xstp = stepper.get_oid()
            elif name == 'stepper_z1' and not is_corexz:
                # Firmware packs secondary Z into x/y OID slots.
                oid_xstp = stepper.get_oid()
            elif name == 'stepper_z2' and not is_corexz:
                oid_ystp = stepper.get_oid()

        if not oid_zstp:
            logging.warning("prtouch: stepper_z OID not found at config")

        step_swap_pin_num = self.ppins.parse_pin(
            self.step_swap_pin, True, True)['pin']
        self.step_mcu.add_config_cmd(
            'config_prtouch_step oid=%d oid_xstp=%d oid_ystp=%d '
            'oid_zstp=%d swp_pin=%s'
            % (self.step_oid, oid_xstp, oid_ystp, oid_zstp, step_swap_pin_num))
        self.start_step_cmd = self.step_mcu.lookup_command(
            'start_prtouch_step oid=%c aqc_tick=%u', cq=None)
        self.stop_step_cmd = self.step_mcu.lookup_command(
            'stop_prtouch_step oid=%c', cq=None)
        step_freq = self.step_mcu.get_constant_float('CLOCK_FREQ')
        self._step_acq_tick = max(
            1, int(round(self.pres_acq_tkms * .001 * step_freq)))

    def _build_pres_config(self):
        pres_swap_pin_num = self.ppins.parse_pin(
            self.pres_swap_pin, True, True)['pin']
        for idx, pin_str in enumerate(self.pres_cs_pins):
            pins = [p.strip() for p in pin_str.split(',') if p.strip()]
            if len(pins) != 2:
                raise self.printer.config_error(
                    "prtouch: pres_cs%d_pin must be 'clk_pin, sdo_pin'" % idx)
            clk_pin = self.ppins.parse_pin(pins[0], True, True)['pin']
            sdo_pin = self.ppins.parse_pin(pins[1], True, True)['pin']
            self.pres_mcu.add_config_cmd(
                'config_prtouch_pres oid=%d idx=%d swp_pin=%s '
                'clk_pin=%s sdo_pin=%s'
                % (self.pres_oid, idx, pres_swap_pin_num, clk_pin, sdo_pin))

        self.start_pres_cmd = self.pres_mcu.lookup_command(
            'start_prtouch_pres oid=%c cfg_regs=%c acq_tick=%u '
            'ned_tftr=%c ned_hftr=%c ned_lftr=%u min_hold=%i max_hold=%i '
            'add_hold=%i lmt_dead=%u',
            cq=None)
        self.stop_pres_cmd = self.pres_mcu.lookup_command(
            'stop_prtouch_pres oid=%c sta_swap=%c', cq=None)
        self.read_pres_cmd = self.pres_mcu.lookup_query_command(
            'read_prtouch_pres oid=%c is_src=%c ch=%c idx=%c len=%c',
            'resault_prtouch_pres oid=%c tri_chxs=%c buf_len=%c '
            'ch=%c idx=%c len=%c ticks=%.*s datas=%.*s',
            oid=self.pres_oid, cq=None)
        pres_freq = self.pres_mcu.get_constant_float('CLOCK_FREQ')
        self._pres_clock_freq = pres_freq
        self._pres_acq_tick = max(
            1, int(round(self.pres_acq_tkms * .001 * pres_freq)))

    def get_position_endstop(self):
        return self.position_endstop

    def setup_pin(self, pin_type, pin_params):
        if pin_type != 'endstop' or pin_params['pin'] != 'z_virtual_endstop':
            raise self.config.error(
                "prtouch virtual endstop only useful as endstop pin")
        if pin_params['invert'] or pin_params['pullup']:
            raise self.config.error(
                "Can not pullup/invert prtouch virtual endstop")
        return self

    def get_status(self, eventtime=None):
        last = self._last_ack
        return {
            'armed': self._armed,
            'last_ack_err': None if last is None else last.get('err', 0),
            'last_ack_oid': None if last is None else last.get('oid'),
        }

    def _query_swap_triggered(self):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        return bool(self.mcu_endstop.query_endstop(print_time))

    def _wait_for_swap_release(self):
        deadline = self.reactor.monotonic() + self.pres_release_timeout
        while True:
            if not self._query_swap_triggered():
                return
            eventtime = self.reactor.monotonic()
            if eventtime >= deadline:
                raise self.printer.command_error(
                    "prtouch: swap endstop did not release after probe")
            self.reactor.pause(min(deadline, eventtime + .005))

    def _force_disarm(self, print_time=None):
        self._disarm(swallow=True)

    def _disarm(self, verify_release=False, swallow=False):
        # Stop pressure (idle swap) before step capture so !PC7 doesn't latch.
        try:
            if self.stop_pres_cmd is not None:
                self.stop_pres_cmd.send(
                    [self.pres_oid, self.pres_idle_swap_state])
        except Exception as exc:
            if not swallow:
                raise
            logging.warning(
                "prtouch: stop_pres during force disarm: %s", exc)
        finally:
            try:
                if self.stop_step_cmd is not None:
                    self.stop_step_cmd.send([self.step_oid])
            except Exception as exc:
                if not swallow:
                    raise
                logging.warning(
                    "prtouch: stop_step during force disarm: %s", exc)
            finally:
                self._armed = False
        if verify_release:
            self._wait_for_swap_release()

    def _arm(self):
        """Fail-closed arm: both MCUs must ack start ok, swap must stay idle.

        Probe motion is only allowed after this returns. A send that merely
        enqueues is not enough — that was the bed-crash footgun.
        """
        self._disarm(verify_release=True)
        if self.pres_rearm_delay:
            self.reactor.pause(
                self.reactor.monotonic() + self.pres_rearm_delay)

        ned_tftr = max(0, min(255, int(round(self.pres_tri_fter[0]))))
        ned_hftr = max(0, min(255, int(round(self.pres_tri_fter[1]))))
        ned_lftr = max(
            0, min(0xffffffff, int(round(self.pres_tri_fter[2] * 1000.))))
        lmt_dead = max(
            0, int(round(self.pres_ded_tkms / self.pres_acq_tkms)))
        min_hold, max_hold, add_hold = self.pres_tri_hold
        min_hold = int(round(min_hold * self._pres_hold_ratio))
        max_hold = int(round(max_hold * self._pres_hold_ratio))

        try:
            # Step capture first so a swap trigger can't beat the timestamp stream.
            # Firmware START markers: step=(0, 0), pressure=(0x40, 0).
            self._run_checked(
                'step',
                self.start_step_cmd,
                [self.step_oid, self._step_acq_tick],
                self.step_oid, 'start_prtouch_step', 0)
            self._run_checked(
                'pres',
                self.start_pres_cmd,
                [
                    self.pres_oid, self.pres_cfg_regs, self._pres_acq_tick,
                    ned_tftr, ned_hftr, ned_lftr,
                    min_hold, max_hold, add_hold, lmt_dead,
                ],
                self.pres_oid, 'start_prtouch_pres', 0x40)
            if self._query_swap_triggered():
                raise self.printer.command_error(
                    "prtouch: swap endstop asserted after arm — refusing probe")
        except Exception:
            self._disarm(swallow=True)
            raise
        self._armed = True

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        self._last_home_wait_result = None
        try:
            return self.mcu_endstop.home_start(
                print_time, sample_time, sample_count, rest_time, triggered)
        except Exception:
            if self._armed:
                self._disarm()
            raise

    def home_wait(self, home_end_time):
        result = self._mcu_home_wait(home_end_time)
        self._last_home_wait_result = result
        return result

    def probe_prepare(self, hmove):
        self._arm()

    def _report_probe_diagnostic(self):
        armed = self._armed
        ack = dict(self._last_ack or {})
        swap_triggered = None
        notes = []
        try:
            swap_triggered = self._query_swap_triggered()
        except Exception as exc:
            notes.append('swap query failed: %s' % exc)
        frozen = False
        try:
            self.stop_pres_cmd.send(
                [self.pres_oid, self.pres_idle_swap_state])
            frozen = True
        except Exception as exc:
            notes.append('PRES freeze failed: %s' % exc)

        params = {}
        ticks = []
        pressures = []
        index = 0
        buf_len = 64
        pages = 0
        while index < buf_len and pages < 64:
            try:
                page = self.read_pres_cmd.send(
                    [self.pres_oid, 1, 0, index, buf_len - index],
                    retry=False)
            except Exception as exc:
                notes.append('PRES read failed at idx=%d: %s' % (index, exc))
                break
            pages += 1
            if not params:
                params = page
                try:
                    buf_len = max(0, min(64, int(page.get('buf_len', 64))))
                except Exception:
                    notes.append('invalid buf_len=%r' % page.get('buf_len'))
            try:
                page_ticks = _decode_prtouch_frame(
                    page.get('ticks', b''), False)
            except Exception as exc:
                page_ticks = []
                notes.append('tick decode failed at idx=%d: %s' % (index, exc))
            try:
                page_pressures = _decode_prtouch_frame(
                    page.get('datas', b''), True)
            except Exception as exc:
                page_pressures = []
                notes.append(
                    'pressure decode failed at idx=%d: %s' % (index, exc))
            decoded_len = max(len(page_ticks), len(page_pressures))
            if not decoded_len:
                notes.append('empty PRES page at idx=%d' % index)
                break
            try:
                page_index = int(page.get('idx', index))
                page_len = int(page.get('len', decoded_len))
                if page_index != index:
                    notes.append(
                        'firmware idx=%d requested=%d' % (page_index, index))
                if page_len != decoded_len:
                    notes.append(
                        'firmware len=%d decoded=%d at idx=%d'
                        % (page_len, decoded_len, index))
            except Exception:
                notes.append('invalid page metadata at idx=%d' % index)
            remaining = max(0, buf_len - index)
            ticks.extend(
                value & 0xffffffff for value in page_ticks[:remaining])
            pressures.extend(page_pressures[:remaining])
            index += min(decoded_len, remaining)

        age_ms = span_ms = None
        try:
            if ticks and self._pres_clock_freq:
                now_clock = self.pres_mcu.print_time_to_clock(
                    self.pres_mcu.estimated_print_time(
                        self.reactor.monotonic()))
                age_ms = ((now_clock - ticks[-1]) & 0xffffffff)
                age_ms *= 1000. / self._pres_clock_freq
                span_ms = ((ticks[-1] - ticks[0]) & 0xffffffff)
                span_ms *= 1000. / self._pres_clock_freq
        except Exception as exc:
            notes.append('sample timing failed: %s' % exc)
        summary = 'pressure_samples=0'
        if pressures:
            summary = (
                'pressure_samples=%d first=%d last=%d min=%d max=%d '
                'range=%d delta=%d'
                % (len(pressures), pressures[0], pressures[-1],
                   min(pressures), max(pressures),
                   max(pressures) - min(pressures),
                   pressures[-1] - pressures[0]))
        tri_chxs = params.get('tri_chxs')
        tri_text = 'n/a' if tri_chxs is None else '0x%02x' % tri_chxs
        self.gcode.respond_info(
            'PRTouch probe diagnostic:\n'
            'armed=%s arm_ack=(err=%s expar0=%s expar1=%s) '
            'swap_triggered=%s buffer_frozen=%s tri_chxs=%s '
            'buf_len=%s ch=%s pages=%d ticks=%d pressure=%d target=%d\n'
            'last_sample_age_ms=%s sample_span_ms=%s %s\n'
            'notes=%s\nticks=%s\npressure=%s'
            % (armed, ack.get('err'), ack.get('expar0'), ack.get('expar1'),
               swap_triggered, frozen, tri_text, params.get('buf_len'),
               params.get('ch'), pages, len(ticks), len(pressures), buf_len,
               'n/a' if age_ms is None else '%.3f' % age_ms,
               'n/a' if span_ms is None else '%.3f' % span_ms,
               summary, '; '.join(notes) if notes else 'none',
               ticks, pressures))

    def probe_finish(self, hmove):
        self._last_probe_no_trigger = (
            self._probing_move_active
            and self._last_home_wait_result == 0.
            and not getattr(hmove, 'triggered_endstops', ())
            and not getattr(hmove, 'force_stop_requested', False))
        self._last_probe_cleanup_ok = False
        if self._armed:
            try:
                if self._last_probe_no_trigger:
                    self._report_probe_diagnostic()
            finally:
                self._disarm(verify_release=True)
                self._last_probe_cleanup_ok = True

    def multi_probe_begin(self, always_restore_toolhead=False):
        pass

    def multi_probe_end(self):
        if self._armed:
            self._disarm()

    def _handle_homing_move_begin(self, hmove):
        if self in hmove.get_mcu_endstops():
            self.probe_prepare(hmove)

    def _handle_homing_move_end(self, hmove):
        if self in hmove.get_mcu_endstops():
            self.probe_finish(hmove)

    def _handle_command_error(self):
        try:
            self.multi_probe_end()
        except Exception:
            logging.exception("prtouch: multi_probe_end after command error")

    def probing_move(self, pos, speed, gcmd):
        homing = self.printer.lookup_object('homing')
        toolhead = self.printer.lookup_object('toolhead')
        start_z = toolhead.get_position()[2]
        probe_distance = abs(start_z - pos[2])
        self._probing_move_active = True
        try:
            for retry in range(self.no_trigger_retries + 1):
                self._last_probe_no_trigger = False
                self._last_probe_cleanup_ok = False
                try:
                    return homing.probing_move(self, pos, speed)
                except self.printer.command_error as error:
                    if (
                            str(error)
                            != "No trigger on probe after full movement"
                            or self.printer.is_shutdown()
                            or not self._last_probe_no_trigger
                            or not self._last_probe_cleanup_ok
                            or probe_distance
                            > self.no_trigger_retry_max_distance
                            or retry >= self.no_trigger_retries):
                        raise
                    homed = toolhead.get_status(
                        self.reactor.monotonic())['homed_axes']
                    if 'z' not in homed:
                        raise
                    self.gcode.respond_info(
                        "prtouch: no trigger; restoring Z=%.3f and retrying "
                        "(%d/%d)"
                        % (start_z, retry + 1, self.no_trigger_retries))
                    toolhead.manual_move(
                        [None, None, start_z], self.lift_speed)
                    toolhead.wait_moves()
        finally:
            self._probing_move_active = False

    def _probe_one(self, gcmd):
        """Single touch sample."""
        if self._printer_probe is not None:
            return self._printer_probe.run_probe(gcmd)
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.reactor.monotonic()
        if 'z' not in toolhead.get_status(curtime)['homed_axes']:
            raise gcmd.error("prtouch: must home Z before probe")
        speed = gcmd.get_float('PROBE_SPEED', self.speed, above=0.)
        pos = toolhead.get_position()
        pos[2] = self.z_position
        epos = self.probing_move(pos, speed, gcmd)
        self.gcode.respond_info(
            "probe at %.3f,%.3f is z=%.6f" % (epos[0], epos[1], epos[2]))
        return epos[:3]

    def _multi_sample_probe_z(self, gcmd=None):
        samples = self.home_samples
        max_samples = max(self.home_max_samples, samples)
        max_noisy = self.home_max_noisy
        sample_range = self.home_sample_range
        sample_params = {
            'SAMPLES': '1',
            'SAMPLES_RESULT': 'median',
            'SAMPLES_TOLERANCE': '999',
            'SAMPLES_TOLERANCE_RETRIES': '0',
        }
        if gcmd is not None:
            samples = gcmd.get_int(
                'SAMPLES', samples, minval=1, maxval=50)
            max_samples = gcmd.get_int(
                'MAX_SAMPLES', max(max_samples, samples),
                minval=samples, maxval=100)
            max_noisy = gcmd.get_int(
                'MAX_NOISY', max_noisy, minval=0, maxval=100)
            sample_range = gcmd.get_float(
                'SAMPLE_RANGE', sample_range, minval=0., maxval=1.)
            for key in ('PROBE_SPEED', 'LIFT_SPEED', 'SAMPLE_RETRACT_DIST'):
                val = gcmd.get(key, None)
                if val is not None:
                    sample_params[key] = val
        window_size = samples + max_noisy
        sample_gcmd = self.gcode.create_gcode_command(
            'PRTOUCH_SAMPLE', 'PRTOUCH_SAMPLE', sample_params)
        collected = []
        best = None
        for attempt in range(1, max_samples + 1):
            epos = self._probe_one(sample_gcmd)
            collected.append(epos[2])
            window = collected[-window_size:]
            if len(window) >= samples:
                best = _best_subset(window, samples)
                if best is not None and _sample_range(best) <= sample_range:
                    break
            if attempt < max_samples:
                self._retract_home_sample(sample_gcmd)
        if best is None or _sample_range(best) > sample_range:
            raise self.printer.command_error(
                "prtouch: unable to find %d samples within %.4fmm "
                "after %d touches"
                % (samples, sample_range, max_samples))
        return float(median(best)), collected, best

    def _seed_z_homed_if_needed(self, toolhead):
        curtime = self.reactor.monotonic()
        if 'z' in toolhead.get_status(curtime)['homed_axes']:
            return False
        zconfig = self.config.getsection('stepper_z')
        z_max = zconfig.getfloat('position_max')
        pos = toolhead.get_position()
        pos[2] = z_max - 10.
        toolhead.set_position(pos, homing_axes='z')
        return True

    def _lookup_carto_macro(self, class_name, *need_attrs):
        carto = self.printer.lookup_object('cartographer', None)
        if carto is None:
            return None
        for reg in getattr(carto, 'macros', []):
            macro = getattr(reg, 'macro', None)
            if (
                    macro is not None
                    and type(macro).__name__ == class_name
                    and all(hasattr(macro, name) for name in need_attrs)):
                return macro
        return None

    def _establish_nozzle_z_zero(self, gcmd=None):
        toolhead = self.printer.lookup_object('toolhead')
        seeded = self._seed_z_homed_if_needed(toolhead)
        try:
            pos = toolhead.get_position()
            if pos[2] < self.sample_retract_dist:
                toolhead.manual_move(
                    [None, None, self.sample_retract_dist], self.lift_speed)
                toolhead.wait_moves()
            trigger_z, _collected, best = self._multi_sample_probe_z(gcmd)
            toolhead.get_last_move_time()  # sync print_time before rewriting Z
            pos = toolhead.get_position()
            pos[2] = pos[2] - trigger_z + self.position_endstop
        finally:
            if seeded:
                toolhead.get_kinematics().clear_homing_state('z')
        toolhead.set_position(pos, homing_axes='z')
        return trigger_z, best

    cmd_PRTOUCH_HOME_help = (
        "Multi-sample Z home with the nozzle probe "
        "(more accurate than a single G28 Z touch). "
        "Optional PRINT_TEMP applies thermal_expansion compensation.")

    cmd_PRTOUCH_SCAN_CALIBRATE_help = (
        "Calibrate Cartographer scan model using prtouch for nozzle Z=0. "
        "Requires cartographer. Optional MODEL=default.")

    cmd_PRTOUCH_AXIS_TWIST_COMPENSATION_help = (
        "Calibrate axis twist using Cartographer scans and prtouch contacts.")

    def _get_extruder_temp(self):
        pheaters = self.printer.lookup_object('heaters')
        heater = pheaters.lookup_heater('extruder')
        temp, _target = heater.get_temp(self.reactor.monotonic())
        return float(temp)

    def cmd_PRTOUCH_SCAN_CALIBRATE(self, gcmd):
        macro = self._lookup_carto_macro('ScanCalibrateMacro', '_calibrate')
        if macro is None:
            raise gcmd.error(
                "prtouch: cartographer ScanCalibrateMacro not loaded")
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.reactor.monotonic()
        homed = toolhead.get_status(curtime)['homed_axes']
        if 'x' not in homed or 'y' not in homed:
            raise gcmd.error(
                "prtouch: must home X/Y before PRTOUCH_SCAN_CALIBRATE")
        model = gcmd.get('MODEL', 'default').strip().lower()
        zrp = macro._config.bed_mesh.zero_reference_position
        travel = macro._config.general.travel_speed
        toolhead.manual_move([zrp[0], zrp[1], None], travel)
        toolhead.wait_moves()
        trigger_z, best = self._establish_nozzle_z_zero(gcmd)
        gcmd.respond_info(
            "prtouch: nozzle Z=%.4f (median=%.4f range=%.4f); "
            "running carto scan calibrate"
            % (self.position_endstop, trigger_z, _sample_range(best)))
        macro._calibrate(model)
        self.gcode.run_script_from_command('SAVE_CONFIG RESTART=0')

    def cmd_PRTOUCH_AXIS_TWIST_COMPENSATION(self, gcmd):
        macro = self._lookup_carto_macro(
            'AxisTwistCompensationMacro', 'run', 'probe')
        if macro is None:
            raise gcmd.error(
                "prtouch: cartographer AxisTwistCompensationMacro not loaded")
        original_probe = macro.probe

        def prtouch_contact():
            trigger_z, _collected, _best = self._multi_sample_probe_z(gcmd)
            self._retract_home_sample(gcmd)
            return trigger_z - self.position_endstop

        macro.probe = SimpleNamespace(
            touch=original_probe.touch,
            perform_scan=original_probe.perform_scan,
            perform_touch=prtouch_contact)
        try:
            macro.run(gcmd)
        except (RuntimeError, ValueError) as error:
            raise gcmd.error(str(error)) from error
        finally:
            macro.probe = original_probe

    def cmd_PRTOUCH_HOME(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.reactor.monotonic()
        homed = toolhead.get_status(curtime)['homed_axes']
        if 'x' not in homed or 'y' not in homed:
            raise gcmd.error("prtouch: must home X/Y before PRTOUCH_HOME")

        travel_speed = gcmd.get_float(
            'TRAVEL_SPEED', self.home_travel_speed, above=0.)
        z_hop = gcmd.get_float('Z_HOP', self.home_z_hop, above=0.)
        print_temp = gcmd.get_float('PRINT_TEMP', None, minval=0.)

        thermal_comp = 0.
        touch_temp = None
        if print_temp is not None:
            if self.thermal_expansion <= 0.:
                raise gcmd.error(
                    "prtouch: PRINT_TEMP requires thermal_expansion "
                    "in [prtouch] config")
            touch_temp = self._get_extruder_temp()
            thermal_comp = _thermal_z_comp(
                self.thermal_expansion, print_temp, touch_temp)

        z_was_unhomed = 'z' not in homed
        try:
            if z_was_unhomed:
                self._seed_z_homed_if_needed(toolhead)

            pos = toolhead.get_position()
            toolhead.manual_move(
                [None, None, pos[2] + z_hop], travel_speed)
            toolhead.manual_move(
                [self.home_xy[0], self.home_xy[1], None], travel_speed)
            toolhead.wait_moves()

            trigger_z, collected, best = self._multi_sample_probe_z(gcmd)
            toolhead.get_last_move_time()  # sync print_time before rewriting Z
            pos = toolhead.get_position()
            # Contact becomes z_offset - thermal_comp so hotter print tip
            # lands at z_offset after nozzle growth.
            pos[2] = (
                pos[2] - trigger_z + self.position_endstop - thermal_comp)
        finally:
            if z_was_unhomed:
                toolhead.get_kinematics().clear_homing_state('z')

        toolhead.set_position(pos, homing_axes='z')
        pos = toolhead.get_position()
        pos[2] = _POST_HOME_LIFT
        toolhead.manual_move([None, None, pos[2]], self.lift_speed)
        toolhead.wait_moves()
        gcode_move = self.printer.lookup_object('gcode_move', None)
        if gcode_move is not None:
            gcode_move.reset_last_position()
        msg = (
            "prtouch: Z home at (%.3f, %.3f) z=%.4f "
            "(median=%.4f range=%.4f attempts=%d)"
            % (pos[0], pos[1], self.position_endstop - thermal_comp,
               trigger_z, _sample_range(best), len(collected)))
        if print_temp is not None:
            msg += (
                " thermal=%.4fmm (touch=%.1fC print=%.1fC rate=%.6fmm/C)"
                % (thermal_comp, touch_temp, print_temp,
                   self.thermal_expansion))
        gcmd.respond_info(msg)

    def _retract_home_sample(self, gcmd):
        retract = gcmd.get_float(
            'SAMPLE_RETRACT_DIST', self.sample_retract_dist, above=0.)
        lift_speed = gcmd.get_float('LIFT_SPEED', self.lift_speed, above=0.)
        toolhead = self.printer.lookup_object('toolhead')
        pos = toolhead.get_position()
        toolhead.manual_move([None, None, pos[2] + retract], lift_speed)

    def _zone_manual_move(self, coord, speed):
        self.printer.lookup_object(
            'extended_zone_transform').manual_move(coord, speed)

    def _scrub_probe(self, probe_gcmd, hold_ratio=1.):
        previous = self._pres_hold_ratio
        self._pres_hold_ratio = hold_ratio
        try:
            return self._probe_one(probe_gcmd)[2]
        finally:
            self._pres_hold_ratio = previous

    def _scrub_probe_line(self, toolhead, probe_gcmd, start, end):
        values = []
        for point in (start, end):
            self._zone_manual_move(
                [point[0], point[1], None], self.home_travel_speed)
            toolhead.wait_moves()
            values.append(self._scrub_probe(probe_gcmd))
            self._retract_home_sample(probe_gcmd)
        if abs(values[1] - values[0]) > .5:
            raise self.printer.command_error(
                "prtouch: scrub endpoints differ by more than 0.500mm")
        return values

    def _scrub_round_trip(
            self, toolhead, start, end, z_start, z_center, z_end, depth):
        center = [
            .5 * (start[0] + end[0]),
            .5 * (start[1] + end[1]),
        ]
        clear_z = max(z_start, z_center, z_end) + self.sample_retract_dist
        self._zone_manual_move([None, None, clear_z], self.lift_speed)
        self._zone_manual_move(
            [start[0], start[1], None], self.home_travel_speed)
        self._zone_manual_move(
            [None, None, z_start - depth], self.lift_speed)
        toolhead.wait_moves()
        self._zone_manual_move(
            [center[0], center[1], z_center - depth], self.scrub_speed)
        self._zone_manual_move(
            [end[0], end[1], z_end - depth], self.scrub_speed)
        self._zone_manual_move(
            [center[0], center[1], z_center - depth], self.scrub_speed)
        self._zone_manual_move(
            [start[0], start[1], z_start - depth], self.scrub_speed)
        toolhead.wait_moves()
        self._zone_manual_move([None, None, clear_z], self.lift_speed)
        toolhead.wait_moves()

    def _scrub_lift(self, toolhead):
        pos = toolhead.get_position()
        z_max = self.config.getsection('stepper_z').getfloat('position_max')
        lift_z = min(pos[2] + self.sample_retract_dist, z_max)
        if lift_z > pos[2]:
            self._zone_manual_move([None, None, lift_z], self.lift_speed)
            toolhead.wait_moves()

    cmd_PRTOUCH_SCRUB_help = (
        "Detect the flexible rear bed tab and scrub the nozzle against it.")

    def cmd_PRTOUCH_SCRUB(self, gcmd):
        self.gcode.run_script_from_command("HOME_IF_NEEDED AXIS=XYZ")
        toolhead = self.printer.lookup_object('toolhead')
        probe_gcmd = self.gcode.create_gcode_command(
            'PRTOUCH_SCRUB_PROBE', 'PRTOUCH_SCRUB_PROBE', {
                'SAMPLES': '1',
                'SAMPLES_RESULT': 'median',
                'SAMPLES_TOLERANCE': '999',
                'SAMPLES_TOLERANCE_RETRIES': '0',
            })
        try:
            y = random.uniform(self.scrub_y_min, self.scrub_y_max)
            start = (self.scrub_x_start, y)
            end = (self.scrub_x_end, y)
            self._scrub_lift(toolhead)

            center = [
                .5 * (start[0] + end[0]),
                .5 * (start[1] + end[1]),
            ]
            self._zone_manual_move(
                [center[0], center[1], None], self.home_travel_speed)
            toolhead.wait_moves()
            deflections = []
            normal_zs = []
            for _sample in range(3):
                normal_z = self._scrub_probe(probe_gcmd)
                normal_zs.append(normal_z)
                self._retract_home_sample(probe_gcmd)
                high_z = self._scrub_probe(
                    probe_gcmd, self.scrub_detect_hold_ratio)
                self._retract_home_sample(probe_gcmd)
                deflections.append(abs(normal_z - high_z))
            deflection = round(float(median(deflections)), 6)
            tab_detected = deflection > self.scrub_detect_deflection
            depth = (
                self.scrub_tab_depth if tab_detected
                else self.scrub_no_tab_depth)
            gcmd.respond_info(
                "prtouch: %s: deflection %.4fmm %s %.4fmm; "
                "wiping %.3fmm deep at %.1fmm/s"
                % ("tab detected" if tab_detected else "no tab",
                   deflection, ">" if tab_detected else "<=",
                   self.scrub_detect_deflection, depth, self.scrub_speed))

            z_start, z_end = self._scrub_probe_line(
                toolhead, probe_gcmd, start, end)
            z_center = float(median(normal_zs))
            center_offset = z_center - .5 * (z_start + z_end)
            passes = 0
            shortened = 0.
            for passes in range(1, 4):
                self._scrub_round_trip(
                    toolhead, start, end, z_start, z_center, z_end, depth)
                next_start, next_end = self._scrub_probe_line(
                    toolhead, probe_gcmd, start, end)
                shortened = (
                    .5 * (z_start + z_end)
                    - .5 * (next_start + next_end))
                z_start, z_end = next_start, next_end
                z_center = .5 * (z_start + z_end) + center_offset
                if shortened < .01:
                    break
            gcmd.respond_info(
                "prtouch: scrub tab=%s deflection=%.4fmm depth=%.3fmm "
                "passes=%d shorten=%.4fmm"
                % (tab_detected, deflection, depth, passes, shortened))
        except self.printer.command_error as error:
            if self._printer_probe is not None:
                try:
                    self._printer_probe.multi_probe_end()
                finally:
                    self._printer_probe.retry_session.end()
            else:
                self.multi_probe_end()
            homed = toolhead.get_status(
                self.reactor.monotonic())['homed_axes']
            if self.printer.is_shutdown() or not all(
                    axis in homed for axis in 'xyz'):
                raise
            gcmd.respond_info("prtouch: scrub stopped: %s" % (error,))
        finally:
            try:
                self._scrub_lift(toolhead)
            except Exception:
                logging.exception("prtouch: scrub cleanup lift")


def load_config(config):
    prtouch = PRTouchEndstopWrapper(config)
    if prtouch.register_as_probe:
        pprobe = _PRTouchPrinterProbe(config, prtouch)
        config.get_printer().add_object('probe', pprobe)
        prtouch._printer_probe = pprobe
    return prtouch
