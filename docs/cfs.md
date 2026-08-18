# CFS and RFID

## CFS Fluidd widget

The CFS is controlled through a custom Fluidd widget called **Filament Box**.
It shows the CFS and filament-path state, lets you select a slot or unload
filament, and stores the material details for each slot.

Open the widget settings with the cog to change runout swapping, unloading
after a print, and RFID reading options. The Fluidd console is where the
firmware reports CFS status, errors, and troubleshooting information.

!!! note "Touchscreen"
    **Fluidd is the recommended interface for CFS.** HelixScreen is a
    3rd party project. It's CFS support on this firmware may be rough around the edges or entirely broken in places. If the screen and Fluidd disagree,
    trust Fluidd and its console. For screen-only problems, use HelixScreen's
    [quick debugging guide](https://github.com/prestonbrown/helixscreen/blob/main/docs/user/TROUBLESHOOTING.md#quick-debugging-guide)
    and report it upstream.

## Spoolman

You can optionally link a Spoolman spool to each CFS slot from Filament Box.
Open a slot, choose **Select spool**, then save it. Its filament details and
remaining weight are shown in Filament Box, and it becomes the active Spoolman
spool when that slot is loaded.

## RFID

If you do not plan to write your own RFID tags for your spools, you can ignore
the rest of this section.

RFID fills Filament Box slot details automatically: material, color, brand,
name, and remaining filament. The printer only reads tags. It does not write
them.

There are two readers. Both update the same Filament Box card in Fluidd.

### CFS slots

Open **Filament Box → settings** (the cog) for the two RFID options. Both are
off by default.

- **Read RFID on insertion** — when a spool is inserted into the CFS, read
  its RFID tag once to populate its saved filament details. Filament must
  be unloaded.
- **Read RFID after Klipper starts** — when Klipper starts, read present
  spools that the CFS internally has no RFID data for. Consequently, a
  printer power cycle spins every occupied slot. Saved slot details
  (name, color, material) live in Klipper. RFID remaining % comes from
  the CFS RFID record and shows on the slot after a successful read.
  The only reason to enable this is RFID remaining %. Filament must be
  unloaded.

### External reader

Tap a spool against the printer’s standalone reader. A successful scan beeps
and fills the **External** slot on the Filament Box card.

The external reader does not show remaining filament.

### Resolution

A tag identifies a spool in one of two ways. Pick one when you write it.

- **[Spoolman](#spoolman-ids-on-rfid-tags)** — put the Spoolman spool ID in `reserve`. Material,
  color, brand, and name come from Spoolman. `filamentId` and `color` on the
  tag are ignored.
- **[Catalog](#catalog)** — put a material code in `filamentId` and `#RRGGBB`
  in `color`. Leave `reserve` as `000000`. Brand, name, material, and flush
  temperature come from a mapping you saved, or else the Creality catalog.
  If the code is not in the catalog and has no mapping, the slot is left
  as-is.

If `reserve` is an integer greater than 1, the Spoolman path wins even if
`filamentId` is also set. `000000` and `000001` are not spool IDs; stock
Creality tags often have `1` in this field.

Tags are 40-character Creality records. Resolution uses `filamentId`,
`color`, and `reserve`. See
[this tag writer](https://github.com/DnG-Crafts/K2-RFID) for an example.

| date | vendor | batch | **filamentId** | **color** | length | serial | **reserve** |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 5 | 4 | 2 | 6 | 7 | 4 | 6 | 6 |
| `AB124` | `0276` | `A2` | `101001` | `#FFFFFF` | `0165` | `000001` | `000000` |

### Spoolman IDs on RFID tags

Use this if you want the slot filled from Spoolman. Write the spool ID in
`reserve`, zero-padded, greater than 1 (`000091`). The other fields are not
used.

If Spoolman’s material is not already in the Filament Box catalog, it is added
with a default flush temperature. The console tells you to set the real flush
temperature in the slot UI.

**Select spool** on the Filament Box card assigns a spool to that slot without
writing the tag. It does not change how a later RFID scan resolves.

A slot with a Spoolman spool shows remaining weight from Spoolman, not the
RFID percentage.

### Catalog

Use this if you are not using Spoolman, or you only want a material/color
profile from the tag. Write `filamentId` and `color` (`#RRGGBB`). Leave
`reserve` as `000000`.

A 6-character `filamentId` whose last five characters match a catalog code
still resolves (`101001` → `01001`).

This firmware uses the same material codes Creality uses:

??? abstract "Creality RFID material codes"

    | Code | Brand | Name | Material | Default °C |
    | --- | --- | --- | --- | --- |
    | `01001` | Creality | Hyper PLA | PLA | 215 |
    | `01002` | Creality | Hyper L-W PLA | PLA | 235 |
    | `01004` | Creality | Hyper Stardust | PLA | 215 |
    | `01601` | Creality | Soleyin Ultra PLA | PLA | 215 |
    | `02001` | Creality | Hyper PLA-CF | PLA-CF | 215 |
    | `03001` | Creality | Hyper ABS | ABS | 260 |
    | `04001` | Creality | CR-PLA | PLA | 215 |
    | `05001` | Creality | CR-Silk | PLA | 215 |
    | `06001` | Creality | CR-PETG | PETG | 245 |
    | `06002` | Creality | Hyper PETG | PETG | 245 |
    | `06003` | Creality | Hyper PETG-CF | PETG-CF | 250 |
    | `07001` | Creality | CR-ABS | ABS | 260 |
    | `07002` | Creality | Hyper PC | PC | 260 |
    | `08001` | Creality | Ender-PLA | PLA | 215 |
    | `09001` | Creality | EN-PLA+ | PLA | 215 |
    | `09002` | Creality | ENDER FAST PLA | PLA | 215 |
    | `10001` | Creality | HP-TPU | TPU | 215 |
    | `11001` | Creality | CR-Nylon | PA | 260 |
    | `12002` | Creality | Hyper PPA-CF | PA-CF | 300 |
    | `12003` | Creality | Hyper PAHT-CF | PA-CF | 300 |
    | `12004` | Creality | Hyper PA612-CF | PA612-CF | 305 |
    | `12005` | Creality | Hyper PA6-CF | PA6-CF | 305 |
    | `13001` | Creality | CR-PLA Carbon | PLA | 215 |
    | `14001` | Creality | CR-PLA Matte | PLA | 215 |
    | `15001` | Creality | CR-PLA Fluo | PLA | 215 |
    | `16001` | Creality | CR-TPU | TPU | 225 |
    | `17001` | Creality | CR-Wood | PLA | 215 |
    | `18001` | Creality | HP Ultra PLA | PLA | 215 |
    | `19001` | Creality | HP-ASA | ASA | 260 |
    | `29001` | Creality | Hyper Marble | PLA | 215 |
    | `E1001` | eSUN | PLA+ | PLA | 220 |
    | `00035` | eSUN | PLA-LW | PLA | 230 |
    | `P1001` | Polymaker | Panchroma PLA Satin | PLA | 210 |
    | `P1002` | Polymaker | PolySonic PLA Pro | PLA | 210 |
    | `P1003` | Polymaker | Panchroma PLA Matte | PLA | 210 |
    | `00001` | Generic | Generic PLA | PLA | 215 |
    | `00002` | Generic | Generic PLA-Silk | PLA | 215 |
    | `00003` | Generic | Generic PETG | PETG | 245 |
    | `00004` | Generic | Generic ABS | ABS | 260 |
    | `00005` | Generic | Generic TPU | TPU | 225 |
    | `00006` | Generic | Generic PLA-CF | PLA-CF | 215 |
    | `00007` | Generic | Generic ASA | ASA | 260 |
    | `00008` | Generic | Generic PA | PA | 250 |
    | `00009` | Generic | Generic PA-CF | PA-CF | 280 |
    | `00010` | Generic | Generic BVOH | BVOH | 210 |
    | `00011` | Generic | Generic PVA | PVA | 220 |
    | `00012` | Generic | Generic HIPS | HIPS | 235 |
    | `00013` | Generic | Generic PET-CF | PET-CF | 300 |
    | `00014` | Generic | Generic PETG-CF | PETG-CF | 250 |
    | `00015` | Generic | Generic PA6-CF | PA6-CF | 290 |
    | `00016` | Generic | Generic PAHT-CF | PAHT-CF | 310 |
    | `00017` | Generic | Generic PPS | PPS | 335 |
    | `00018` | Generic | Generic PPS-CF | PPS-CF | 325 |
    | `00019` | Generic | Generic PP | PP | 235 |
    | `00020` | Generic | Generic PET | PET | 260 |
    | `00021` | Generic | Generic PC | PC | 260 |
    | `00022` | Generic | Generic PA612-CF | PA-CF | 290 |
    | `00023` | Generic | Generic Support for PA | PA | 280 |
    | `00024` | Generic | Generic Support for PLA | PLA | 215 |
    | `00025` | Generic | Generic PA12-CF | PA-CF | 290 |
    | `00026` | Generic | Generic TPU 64D | TPU | 225 |
    | `00027` | Generic | Generic PETG-GF | PETG-GF | 260 |
    | `00031` | Generic | Generic PP-CF | PP-CF | 245 |
    | `00032` | Generic | Generic PCTG | PCTG | 260 |
    | `00033` | Generic | Generic ASA-CF | ASA-CF | 265 |
    | `00034` | Generic | Generic PA6-GF | PA-GF | 270 |

If the code is not in that table, the slot is left as-is. The console prints
something like:

```text
Unknown RFID tag in T0: CODE=ZZ9999
Map it with: _BOX_RFID_MAP_SET CODE=ZZ9999 MATERIAL=PLA BRAND="Brand" NAME="Name" TARGET_TEMP=220
```

Edit the material, brand, name, and temperature, then run it. The mapping is
saved and applied to the slot that just scanned. Later scans of that code
reuse it. Color stays whatever is on the tag.

### Remaining filament shown in box card

| Slot | What’s shown |
| --- | --- |
| CFS, RFID only | Tag remaining % |
| CFS or External, Spoolman assigned | Spoolman remaining weight |
| External, no Spoolman | No remaining value |
