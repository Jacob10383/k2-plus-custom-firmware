# Error explanation

## CFS

Errors reported by the CFS.

<!-- generated: cfs-errors -->
| Error | What it means | Code |
| --- | --- | --- |
| Communication errors | The box is rejecting corrupted messages. If this keeps happening, check its cable. | `BAD_CRC` (0x02) |
| Filament didn't reach the hub | Filament never reached the hub's sensor within 7 s. Check that it's seated in the slot gears and not tangled. | `STAGE0_SENSOR_TIMEOUT` (0x05) |
| Slot is empty | There's no filament at this slot's inlet. | `SLOT_EMPTY` (0x08) |
| Filament isn't moving | The hub's measuring wheel didn't turn for 2 s. Check for a tangle, or that the hub is gripping the filament. | `STAGE0_ODOMETER_TIMEOUT` (0x09) |
| Feed timed out | The hub fed for 25 s without filament reaching the printhead. | `FEED_TIMEOUT` (0x0A) |
| Filament didn't arrive | The hub fed 3 m, but filament never reached the printhead. Check for a disconnected PTFE tube. | `OVERTRAVEL` (0x0B) |
| Filament stopped moving | The hub's measuring wheel stopped while feeding. Check for a jam or resistance in the path. | `ODOMETER_STALLED` (0x0C) |
| Buffer didn't fill | The buffer didn't report full within 3 s of feeding. Check the PTFE into the buffer. | `BUFFER_NOT_FULL` (0x0D) |
| Filament couldn't pull back | The hub tried to pull back, but the buffer never released within 5 s, so the filament is likely jammed. Check for a jam between the hub and the printhead. | `UNLOAD_BUFFER_TIMEOUT` (0x13) |
| Filament stuck in the hub | The retract waited for the hub path to clear, and it never did. Check for filament stuck in the hub. | `UNLOAD_HUB_PE_TIMEOUT` (0x14) |
| Nothing to unload | This slot shows no filament at its inlet. | `UNLOAD_NO_FILAMENT` (0x16) |
| Slot inlet already empty | The slot's inlet already showed no filament when the retract started. | `UNLOAD_INLET_CLEAR` (0x17) |
| Hub motor stalled | The hub motor stalled while pulling filament back. Check for a jam. | `UNLOAD_MOTOR_BLOCKED` (0x19) |
| Retract timed out | The hub pulled back for 40 s without the path clearing. | `UNLOAD_ODOMETER_TIMEOUT` (0x1A) |
| Spool ran out | The spool ran out. | `RUNOUT` (0x50) |
| Buffer isn't refilling | The hub is feeding, but the buffer stays empty. Check the PTFE from the hub into the buffer. | `BUFFER_REFILL_STALLED` (0x51) |
| Buffer refill didn't move | The hub tried to refill the buffer, but the filament moved less than 5 mm. Check for a jam or tangle. | `BUFFER_REFILL_NO_MOTION` (0x52) |
| Unknown status | The box reported an unknown status. | other |
<!-- end generated: cfs-errors -->

## Motor control

Protection bits from the motor boards. Several can be set at once. X/Y/Z/Z1 shut the printer down; the extruder pauses and can be cleared.

| Error | Meaning |
| --- | --- |
| `encoder mutation` | The encoder reading jumped farther than the protect threshold between samples. |
| `encoder read error` | The motor MCU failed to read the encoder. |
| `software peak overcurrent` | Phase current spiked over the peak limit. |
| `software continuous overcurrent` | Phase current stayed over the continuous limit long enough to trip. |
| `speed feedback continuously over limit` | Measured speed stayed above the protect limit. |
| `position feedback over limit` | Encoder position went past the software position limit. |
| `position command mutation` | The motion command jumped farther/faster than the protect rate. |
| `excessive position tracking error` | Commanded position and encoder position stayed too far apart. |
| `motor check value abnormal` | Startup phase check measured a nonsense value. |
| `motor check unstable` | Startup phase check readings would not settle. |
| `motor phase not connected` | Phase check saw an open phase. |
| `motor phase resistance mismatch` | Phase resistances disagree by more than the tolerance. |
| `motor check other error` | Startup phase check failed for some other reason. |
| `motor power supply too low` | Motor-board supply voltage is below the minimum. |
| `MCU overheating` | The motor MCU temperature is above the protect limit. |
