# Custom Config Reference

This lists the configuration sections and options added by this firmware.
Standard Klipper and Cartographer configuration remains in their own
documentation. Values shown are the shipped baseline or the source default
when the shipped config leaves an option unset.

## Serial RS-485 Bus

Shared serial bus used by CFS/box, motor control, and belt tension modules.

```ini
[serial_485 serial485]
serial: /dev/ttyS5          # RS-485 serial device
baud: 230400                # Bus speed
default_timeout: 1.0        # Normal command response timeout, seconds
read_timeout: 0.05          # Low-level serial read timeout, seconds
connect_settle: 0.05        # Delay after opening the serial device, seconds
write_timeout: 1.0          # Low-level serial write timeout, seconds
read_chunk: 4096            # Max bytes read at once
max_queued_frames: 64       # Max received frames buffered before dropping old ones
```

## CFS Box

Main CFS subsystem: address assignment, load/unload and tool changes, runout,
slot metadata, RFID, cutter motion, and nozzle cleaning.

```ini
[box]
box_count: 4                # Discovery target, 1-4; lower values shorten startup
state_path: /mnt/UDISK/printer_data/filament_box.json # Persistent box data
purge_speed: 20             # Purge volumetric flow rate, mm3/s
hotend_feed_speed: 20       # Feed volumetric flow rate, mm3/s
hotend_feed_length: 63      # Printhead gears to hotend, mm
print_prime_speed: 10       # Print-start prime flow rate, mm3/s
print_prime_length: 20      # Print-start prime filament, mm
fallback_purge_length: 100  # Purge when no slicer matrix is available, mm
default_temp: 220           # Fallback nozzle temp when print data is unavailable
retract_length: 30          # Retract before cutting, mm
retract_velocity: 3000.0    # Retraction/snap/cut-assist feedrate, mm/min
external_feed_velocity: 600.0 # External/manual feedrate, mm/min
toolchange_z_hop: 2.0       # Z clearance for toolchange travel, mm
wastebin_pos_x: 133.0       # Wastebin X coordinate
wastebin_pos_y: 378.0       # Wastebin Y coordinate
travel_velocity: 18000.0    # XY service-move feedrate, mm/min
z_velocity: 600.0           # Z service-move feedrate, mm/min
pre_cut_pos_x: 10.0         # X position used before/after the cut stroke
# cut_pos_x: <unset>        # Cutter X position; normally saved by calibration
cut_pos_y: 200.0            # Y position used for the cut sequence
cut_velocity: 30000.0       # Cut move feedrate, mm/min
pre_cut_cal_pos_x: -5.0     # Starting X position for cut-position calibration
check_cut_pos_x_max: -5.5   # Highest accepted X hit during cutter check/calibration
check_cut_pos_x_min: -9.5   # Lowest accepted X hit during cutter check/calibration
clean_pad_left_x: 154.0     # Silicone pad left X edge
clean_pad_right_x: 166.0    # Silicone pad right X edge
clean_pad_front_y: 367.0    # Silicone pad front Y edge
clean_pad_back_y: 378.0     # Silicone pad back Y edge
clean_pad_passes: 1         # Complete silicone scrub passes
clean_velocity: 12000.0     # Nozzle-clean feedrate, mm/min
snap_fan_speed: 1.0         # Part-fan speed during flush-clean-snap, 0-1
snap_fan_dwell_ms: 3000     # Part-fan dwell before snap retract, ms
```

## External RFID Reader

```ini
[external_rfid_reader]
beep_enabled: true          # Enable the scan-success buzzer
```

## PR Touch

K2 load-cell probe and multi-sample Z-home settings.

```ini
[prtouch]
z_offset: 0                 # Probe offset; required
register_as_probe: False    # Keep PRTouch as an alternate prtouch: chip
speed: 5                    # Probe speed, mm/s
lift_speed: 40              # Probe lift speed, mm/s
samples: 1                  # Standard probe samples
sample_retract_dist: 2.0    # Retract between samples, mm
samples_result: median      # Sample aggregation
samples_tolerance: 0.02     # Allowed sample spread, mm
samples_tolerance_retries: 3
home_xy: 175, 175            # XY point used by PRTOUCH_HOME
home_samples: 3
home_max_samples: 10
home_max_noisy: 3
home_sample_range: 0.010    # Maximum accepted home spread, mm
home_travel_speed: 200      # XY travel speed, mm/s
home_z_hop: 2               # Lift between home samples, mm
thermal_expansion: 0.00083  # Nozzle/stack growth, mm/°C
scrub_x_start: 173          # Rear-tab wipe start X
scrub_x_end: 223            # Rear-tab wipe end X
scrub_y_min: 353            # Random wipe Y range, minimum
scrub_y_max: 356            # Random wipe Y range, maximum
scrub_detect_hold_ratio: 2.5
scrub_detect_deflection: 0.035
scrub_tab_depth: 0.15
scrub_no_tab_depth: 0.05
scrub_speed: 10
step_swap_pin: !PC7         # Stepper-MCU synchronization pin
pres_swap_pin: nozzle_mcu:PA15 # Nozzle-MCU pressure synchronization pin
pres_cfg_regs: 60           # CS1237 configuration byte
pres_acq_tkms: 0.78125      # Pressure acquisition interval, ms
pres_tri_hold: 4000, 10000, 500
pres_tri_fter: 5.0, 1.0, 0.8
pres_ded_tkms: 128
pres_idle_swap_state: 1
pres_release_timeout: 0.250 # Release acknowledgement timeout, seconds
pres_ack_timeout: 0.250     # Pressure acknowledgement timeout, seconds
pres_rearm_delay: 0.010     # Delay before rearming pressure detection, seconds
pres_cs0_pin: nozzle_mcu:PB13, nozzle_mcu:PB14
```

## Motor Control

Host-side motor controller for startup, pin setup, stall handling, firmware
parameter overrides, calibration, and motor fault reporting.

```ini
[motor_control]
cut_pos_offset: 0.4         # Added to measured cutter hit when saving cut_pos_x

x_param_stall_pos_err_rad: 0.007
y_param_stall_pos_err_rad: 0.007
x_controller_cur_filter_param_en: 1
y_controller_cur_filter_param_en: 1
z_controller_cur_filter_param_en: 1
z1_controller_cur_filter_param_en: 1
```

Motor pin options and their defaults:

```ini
motor_x_dir: PB9
motor_x_step: PB10
motor_x_stall: PB11
motor_y_dir: !PB7
motor_y_step: PB8
motor_y_stall: PB12
motor_z_dir: PB5
motor_z_step: PB6
motor_z_stall: PB13
motor_z1_dir: PA1
motor_z1_step: PB15
motor_z1_stall: PA10
motor_e_stall: nozzle_mcu:PB12
```

Motor firmware overrides use `{axis}_{parameter}`, where axis is `e`, `x`,
`y`, `z`, or `z1`. If an override is omitted, the motor board's stored value
is used. All five axes accept these parameter names:

??? abstract "Motor firmware parameter names"

    ```text
    cmd_int_param_char_cmd_support
    controller_cur_filter_param_en
    controller_cur_filter_param_fc
    controller_cur_loop_pid_fal_param_a
    controller_cur_loop_pid_fal_param_en
    controller_cur_loop_pid_fal_param_zoom
    controller_cur_loop_pid_param_kc
    controller_cur_loop_pid_param_ki
    controller_cur_loop_pid_param_kp
    controller_fwc_param_I_max
    controller_fwc_param_en
    controller_fwc_param_wm_base
    controller_leso_param_b0k
    controller_leso_param_wd
    controller_leso_param_wp
    controller_leso_param_ws
    controller_leso_param_z3k
    controller_param_en_feedforward_idiq
    controller_param_en_feedforward_uduq
    controller_pos_loop_pid_param_kp
    controller_spd_loop_pid_fal_param_a
    controller_spd_loop_pid_fal_param_en
    controller_spd_loop_pid_fal_param_zoom
    controller_spd_loop_pid_param_kc
    controller_spd_loop_pid_param_ki
    controller_spd_loop_pid_param_kp
    controller_td_fhan_param_f0
    controller_td_param_d_gain
    controller_td_param_dd_gain
    controller_td_param_en
    controller_td_param_h0_gain_h
    motor_param_Ke
    motor_param_L
    motor_param_R
    param_elec_offset
    param_elec_offset_err_deg
    param_encoder_calibtate_official_ud_cal_set
    param_motion_dir
    param_phase_order_invert
    param_stall_cur_A
    param_stall_mode
    param_stall_pos_err_rad
    param_ud_cal_set
    protection_param_mcu_temp_max
    protection_param_power_voltage_min
    protection_param_protect_en
    protection_param_prt_continuous_cur_A
    protection_param_prt_continuous_time_s
    protection_param_prt_over_speed_rad_s
    protection_param_prt_over_speed_time_s
    protection_param_prt_peak_cur_A
    protection_param_prt_track_err_time
    protection_param_prt_track_max_err
    stall_pos_err_rad_for_slicer
    step_controller_param_subdivision
    zazen_param_zazen_en
    zazen_param_zazen_gain_cur_ki
    zazen_param_zazen_gain_cur_kp
    zazen_param_zazen_gain_spd_ki
    zazen_param_zazen_gain_spd_kp
    zazen_param_zazen_trigger_time_s
    ```

## Z Align

MCU-assisted dual-Z bottom-switch alignment before final host-side Z homing.

```ini
[z_align]
endstop_pin_z: PA15, PA8   # Bottom switch pins, one per Z motor
zd_up: 0                   # Direction level that moves Z away from switches
zes_untrig: 1              # Logic level reported by an untriggered switch
quick_speed: 30.0          # Fast MCU-side drop speed, mm/s
slow_speed: 10.0           # Slow settle speed, mm/s
rising_dist: 5             # Lift between fast and slow MCU passes, mm
filter_cnt: 10             # Consecutive switch samples required for trigger
retries: 5                 # MCU retry attempts before aborting
retry_tolerance: 10        # Allowed left/right mismatch, steps
rise_distance: 340         # Post-align rise distance, mm
rise_speed: 50             # Post-align rise speed, mm/s
temp_rise_max_z_accel: 200 # Temporary accel cap for the post-align rise
safe_dist: 40              # Max extra one-sided drop after the other switch hits
timeout: 30                # Seconds allowed for one MCU alignment attempt
zmax: 350                  # Logical Z after bottom-switch alignment
```

## Belt Tension Modules

Controls the X/Y automatic belt tensioner modules over the shared RS-485 bus.

```ini
[belt_mdl x]
target_tension: 140
tension_tolerance: 0.02
calibration_low_tension: 140
calibration_high_tension: 160
tighten_direction: 1
control_gain: 1.0
max_adjust_step: 12
max_adjustments: 200
settle_rounds: 2
settle_distance: 50        # compact symmetric sweep around (175,175)
settle_speed: 60
settle_margin: 5.0
relax_time: 0.25
move_seconds_per_unit: 0.007
post_move_settle: 0.15
adc_samples: 3
adc_sample_interval: 0.12
serial_timeout: 1.0

[belt_mdl y]
target_tension: 140
tension_tolerance: 0.02
calibration_low_tension: 140
calibration_high_tension: 160
tighten_direction: 1
control_gain: 1.0
max_adjust_step: 12
max_adjustments: 200
settle_rounds: 2
settle_distance: 50        # compact symmetric sweep around (175,175)
settle_speed: 60
settle_margin: 5.0
relax_time: 0.25
move_seconds_per_unit: 0.007
post_move_settle: 0.15
adc_samples: 3
adc_sample_interval: 0.12
serial_timeout: 1.0
```

## Extended Zone Transform

Routes moves safely into the K2 extended Y area before bed-mesh compensation.

```ini
[extended_zone_transform]
standard_y_max: 352.0      # Normal Y envelope
extended_y_max: 380.0      # Extended Y envelope
safe_x_min: 122.0          # Minimum X allowed while entering extended Y
safe_x_max: 230.0          # Maximum X allowed while entering extended Y
```

## Power-Loss Recovery

Maintains durable print checkpoints for manual recovery after a power loss.

```ini
[power_loss_recovery]
state_path: /mnt/UDISK/printer_data/power_loss_recovery # A/B journal directory
candidate_interval: 0.5     # Minimum seconds between in-memory candidates
checkpoint_interval: 10.0   # Minimum seconds between durable submissions
recovery_lift: 5.0          # Clearance above saved print Z, mm
maximum_recovery_z: 350.0   # Maximum aligned K2 recovery clearance, mm
recovery_travel_speed: 100.0 # XY recovery travel, mm/s
recovery_z_speed: 15.0      # Z recovery move speed, mm/s
nozzle_standby: 140.0       # Nozzle target during reference recovery
```

## Motion Limits

Adds commands to save and restore velocity, accel, cruise ratio, square-corner
velocity, and optional G-code state.

```ini
[motion_limits]
# config options: none
```

## Chamber Heater Circulation Fan

Runs the chamber circulation fan when the chamber heater is applying power, with
separate preheat and printing speeds.

```ini
[chamber_heater_circulation_fan chamber_heater_fan]
pin: !PB14                 # Fan output pin
enable_pin: PB2            # Fan enable pin
heater: chamber_heater     # Heater to watch
fluidd_alias: heater_fan chamber_heater_fan # Optional UI alias object
preheat_speed: 1.0         # Fan speed while heating before print starts, 0-1
printing_speed: 0.30       # Fan speed while printing, 0-1
```

## Fan Feedback

Monitors fan tach feedback and warns, pauses, or shuts down after confirmed fan
stall.

```ini
[fan_feedback]
fans: PC6, nozzle_mcu:PA12, nozzle_mcu:PC13 # Tach pins to monitor
shutdown_fans: PC6       # Fans that shut down the printer on confirmed stall
pause_fans: nozzle_mcu:PA12, nozzle_mcu:PC13 # Fans that pause/warn on stall
warn_fans:               # Fans that only warn on stall
fan_drivers: heater_fan chamber_heater_fan, heater_fan heatbreak_fan, fan # Matching fan objects
fan_names: chamber heater fan, heatbreak fan, part cooling fan # Matching display names
poll_interval: 1.0       # Tach poll interval, seconds
confirm_seconds: 20.0    # Stall duration before action, seconds
repeat_warn_seconds: 1800.0 # Repeat warning interval, seconds
```

## Temperature Fan Manual Floor

Adds a manual speed floor to an existing `temperature_fan` while keeping the
temperature fan as the owner of the physical pin.

```ini
[temperature_fan_manual_floor chamber_exhaust_fans]
temperature_fan: chamber_exhaust_fans # Wrapped temperature_fan object
status_key: manual_speed              # Extra status key exposed for manual speed
min_update_delta: 0.05                # Ignore smaller speed changes during delay
```

## LED Idle Manager

Keeps LEDs on during printing or after user activity, then turns them off after
an idle timeout.

```ini
[led_idle_manager]
pin: LED                  # Output pin object controlled by SET_PIN
timeout: 300              # Idle timeout before LEDs turn off, seconds
on_value: 1.0             # LED-on value, 0-1
off_value: 0.0            # LED-off value, 0-1
ignore_commands: M105,STATUS,GET_POSITION,M114,QUERY_ENDSTOPS # Commands ignored as activity
```

## PTC Power Limiter

Caps chamber-heater PWM against bed-heater load so the combined load stays under
the configured budget.

```ini
[ptc_power_limiter]
bed: heater_bed           # Bed heater object to read load from
chamber: heater_generic chamber_heater # Chamber heater object to limit
enabled: True             # Enable power limiting
bed_full_load: 1.0        # Load used when bed PWM is 100%
chamber_full_load: 0.35   # Load used when chamber PWM is 100%
max_combined_load: 1.1    # Total normalized load allowed
minimum_useful_chamber_power: 0.20 # Smaller chamber cap is rounded to zero
debug: False              # Log limiter decisions
```

## Force Stop Homing

Adds the UI/webhook path that can abort active homing or a drip move.

```ini
[force_stop_homing]
# config options: none
```
