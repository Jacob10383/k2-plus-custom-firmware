# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Static product metadata for CFS RFID material codes.

Material codes are normalized into stable lookup keys and resolved to the
known product name, brand, material, and default printing temperature.
"""


_MATERIALS = {
    "01001": ("Creality", "Hyper PLA", "PLA", 215),
    "02001": ("Creality", "Hyper PLA-CF", "PLA-CF", 215),
    "06002": ("Creality", "Hyper PETG", "PETG", 245),
    "03001": ("Creality", "Hyper ABS", "ABS", 260),
    "09002": ("Creality", "ENDER FAST PLA", "PLA", 215),
    "04001": ("Creality", "CR-PLA", "PLA", 215),
    "05001": ("Creality", "CR-Silk", "PLA", 215),
    "06001": ("Creality", "CR-PETG", "PETG", 245),
    "07001": ("Creality", "CR-ABS", "ABS", 260),
    "00001": ("Generic", "Generic PLA", "PLA", 215),
    "00002": ("Generic", "Generic PLA-Silk", "PLA", 215),
    "00003": ("Generic", "Generic PETG", "PETG", 245),
    "00004": ("Generic", "Generic ABS", "ABS", 260),
    "00005": ("Generic", "Generic TPU", "TPU", 225),
    "00006": ("Generic", "Generic PLA-CF", "PLA-CF", 215),
    "00007": ("Generic", "Generic ASA", "ASA", 260),
    "08001": ("Creality", "Ender-PLA", "PLA", 215),
    "09001": ("Creality", "EN-PLA+", "PLA", 215),
    "10001": ("Creality", "HP-TPU", "TPU", 215),
    "11001": ("Creality", "CR-Nylon", "PA", 260),
    "13001": ("Creality", "CR-PLA Carbon", "PLA", 215),
    "14001": ("Creality", "CR-PLA Matte", "PLA", 215),
    "15001": ("Creality", "CR-PLA Fluo", "PLA", 215),
    "16001": ("Creality", "CR-TPU", "TPU", 225),
    "17001": ("Creality", "CR-Wood", "PLA", 215),
    "18001": ("Creality", "HP Ultra PLA", "PLA", 215),
    "19001": ("Creality", "HP-ASA", "ASA", 260),
    "00008": ("Generic", "Generic PA", "PA", 250),
    "00009": ("Generic", "Generic PA-CF", "PA-CF", 280),
    "00010": ("Generic", "Generic BVOH", "BVOH", 210),
    "00011": ("Generic", "Generic PVA", "PVA", 220),
    "00012": ("Generic", "Generic HIPS", "HIPS", 235),
    "00013": ("Generic", "Generic PET-CF", "PET-CF", 300),
    "00014": ("Generic", "Generic PETG-CF", "PETG-CF", 250),
    "00015": ("Generic", "Generic PA6-CF", "PA6-CF", 290),
    "00016": ("Generic", "Generic PAHT-CF", "PAHT-CF", 310),
    "00017": ("Generic", "Generic PPS", "PPS", 335),
    "00018": ("Generic", "Generic PPS-CF", "PPS-CF", 325),
    "00019": ("Generic", "Generic PP", "PP", 235),
    "00020": ("Generic", "Generic PET", "PET", 260),
    "00021": ("Generic", "Generic PC", "PC", 260),
    "00025": ("Generic", "Generic PA12-CF", "PA-CF", 290),
    "00022": ("Generic", "Generic PA612-CF", "PA-CF", 290),
    "12003": ("Creality", "Hyper PAHT-CF", "PA-CF", 300),
    "12002": ("Creality", "Hyper PPA-CF", "PA-CF", 300),
    "00023": ("Generic", "Generic Support for PA", "PA", 280),
    "00024": ("Generic", "Generic Support for PLA", "PLA", 215),
    "00026": ("Generic", "Generic TPU 64D", "TPU", 225),
    "07002": ("Creality", "Hyper PC", "PC", 260),
    "01601": ("Creality", "Soleyin Ultra PLA", "PLA", 215),
    "00033": ("Generic", "Generic ASA-CF", "ASA-CF", 265),
    "00034": ("Generic", "Generic PA6-GF", "PA-GF", 270),
    "00035": ("eSUN", "PLA-LW", "PLA", 230),
    "00027": ("Generic", "Generic PETG-GF", "PETG-GF", 260),
    "00031": ("Generic", "Generic PP-CF", "PP-CF", 245),
    "00032": ("Generic", "Generic PCTG", "PCTG", 260),
    "06003": ("Creality", "Hyper PETG-CF", "PETG-CF", 250),
    "01004": ("Creality", "Hyper Stardust", "PLA", 215),
    "01002": ("Creality", "Hyper L-W PLA", "PLA", 235),
    "29001": ("Creality", "Hyper Marble", "PLA", 215),
    "12004": ("Creality", "Hyper PA612-CF", "PA612-CF", 305),
    "12005": ("Creality", "Hyper PA6-CF", "PA6-CF", 305),
    "E1001": ("eSUN", "PLA+", "PLA", 220),
    "P1001": ("Polymaker", "Panchroma PLA Satin", "PLA", 210),
    "P1002": ("Polymaker", "PolySonic PLA Pro", "PLA", 210),
    "P1003": ("Polymaker", "Panchroma PLA Matte", "PLA", 210),
}


def normalize_material_code(value):
    """Normalize an RFID material code without discarding unknown values."""
    code = str(value or "").strip().upper()
    if len(code) == 6 and code[1:] in _MATERIALS:
        return code[1:]
    return code


def resolve_material(value):
    """Return ``(normalized_code, detached_product)`` for an RFID value."""
    code = normalize_material_code(value)
    metadata = _MATERIALS.get(code)
    if metadata is None:
        return code, None
    return code, dict(zip(
        ("brand", "name", "material", "default_temp"), metadata))
