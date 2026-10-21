# MagicMaker JOKER V2.0 — Marlin Build Notes & Config

Community documentation and a working Marlin **2.1.2.9** configuration for the
**MagicMaker JOKER V2.0** ESP32 mainboard (Marlin board ID `BOARD_MM_JOKER`, a
derivative of [MRR ESPE](https://github.com/maplerainresearch/MRR_ESPE)), as shipped
on **MagicMaker SLuban (小鲁班)** printers.

Everything here was verified against real hardware — a SLuban with a 125 × 125 mm bed,
TMC2209 standalone drivers (no UART wiring on this board) and a 12 V 50 W heated bed. Claims are backed by serial
output, binary inspection, or a file-and-line citation in the Marlin source.

> **Scope:** this is a *documentation + configuration* repository. It does not
> redistribute the vendor's Marlin tree or any compiled binary. You build from the
> vendor's source with the `config.ini` here.

---

## Why this exists

The vendor's own repository ([magicmaker3/magicmaker](https://github.com/magicmaker3/magicmaker))
ships firmware, schematics and tutorials, but almost entirely in Chinese and with no
build documentation. At the time of writing, **all five forks of it were zero commits
ahead** — nobody had contributed anything back. Several non-obvious traps below will
waste a day each if you meet them cold.

---

## The five things that will bite you

### 1. Editing `Configuration.h` does nothing — and the build still succeeds

`Marlin/config.ini` contains `ini_use_config = all`. Marlin's
`buildroot/share/PlatformIO/scripts/configuration.py` **regenerates `Configuration.h`
and `Configuration_adv.h` from that INI on every build**, discarding your edits.

The build reports `SUCCESS`. The binary is valid. It simply contains vendor defaults.

**→ Edit `Marlin/config.ini`, never the `.h` files.**
**→ Always verify the resulting binary (see [Verifying a build](#verifying-a-build)).**

### 2. The build fails from any path containing a space

`ini/features.ini` injects `-DSRCHOME=${platformio.src_dir}/src`. An unquoted path with
a space truncates at the space:

```
fatal error: D:/Claude/general/magicmaker/inc/MarlinConfigPre.h: No such file or directory
```

**→ Build from a path with no spaces** (e.g. `D:\mm-build\joker`).

### 3. `EMERGENCY_PARSER` cannot be enabled — ever

`Marlin/src/HAL/ESP32/inc/SanityCheck.h:29`

```
#error "EMERGENCY_PARSER is not yet implemented for ESP32. Disable EMERGENCY_PARSER to continue."
```

This affects **every** ESP32 Marlin board, not just this one.

**Consequence:** `M112` is queued behind the planner buffer and is *not* an emergency
stop. **The physical power switch is your only emergency stop.**

### 4. `LIN_ADVANCE` cannot be enabled on this board

`Marlin/src/pins/esp32/pins_MM_JOKER.h:96`

```
#error "I2S stream is currently incompatible with LIN_ADVANCE."
```

The I2S stepper stream drives the pin expander and is not optional here.

**→ Skip pressure-advance calibration in OrcaSlicer/PrusaSlicer permanently.**

### 5. Symbols get renamed between Marlin versions — silently, with a clean build

**The one that cost the most time.** The vendor's `config.ini` and `pins_MM_JOKER.h`
are written for **2.1.3-beta3**. Several symbols were renamed between 2.1.2.x and
2.1.3, so on any other version those keys match nothing. Marlin's config generator
**appends them to `Configuration.h` as dead `#define`s and leaves the real setting at
its stock value.** No error. No warning. A valid binary that behaves wrongly.

Four real instances, found on 2.1.2.9:

| Name in the b3 files | What 2.1.2.9 actually reads | Symptom |
|---|---|---|
| `*_endstop_hit_state = LOW` (×7) | `*_ENDSTOP_INVERTING = true` | **every endstop read backwards** — the axis drives into its limit, grinds, then `Homing Failed` → `kill()` |
| `default_bedkp/ki/kd` | `DEFAULT_BED_KP/KI/KD` | bed silently runs stock PID |
| `z_clearance_for_homing` | `Z_HOMING_HEIGHT` | **no Z lift before XY homing** — nozzle can drag the bed |
| `SD_SS_PIN` (pins file) | `SDSS` (`cardreader.cpp`) | `pins_postprocess.h` falls back to `SDSS -1`; card reader initialises on CS −1, `M21` says "No SD card", LCD shows "Attach Media" and clicking does nothing |

The renames run **in both directions** relative to any given version. `FAN_PIN` →
`FAN0_PIN` landed *before* 2.1.2.9, so the b3 pins file is correct there; `SDSS` →
`SD_SS_PIN` landed *after*, so it is wrong. A newer-looking name is not automatically
the right one — check which symbol the **consuming version** reads.

**→ Detection, for config keys:**

```bash
grep -n "Added by config.ini" <marlin>/Marlin/Configuration.h
```

Every line it prints is a key that matched nothing and is therefore inert. Triage all
of them. LCD cosmetics are harmless; anything touching endstops, heaters, motion or
storage is not.

**→ Detection, for pin symbols:** diff the symbols your board file defines against
those the target version's source actually consumes. `SDSS` must be a **literal** —
`#define SDSS SD_SS_PIN` does not compile, because `SD_SS_PIN` is gone by the point of
use.

**→ And always diff the *generated* headers against a known-good firmware**, never
against `config.ini`, which shows intent rather than effect. Reset
`Configuration.h`/`Configuration_adv.h` to stock before each rebuild or the dead
appends accumulate.

---

## Porting to another Marlin version

The board-specific surface is **three files**, so moving to a different Marlin is a few
hours, not a rewrite:

| File | Change |
|---|---|
| `Marlin/src/pins/esp32/pins_MM_JOKER.h` | copy verbatim (then check for renamed symbols — see trap 5) |
| `Marlin/src/core/boards.h` | add `#define BOARD_MM_JOKER 7011` |
| `Marlin/src/pins/pins.h` | add the `#elif MB(MM_JOKER)` include |

Plus `Marlin/config.ini`, and `Marlin/_Bootscreen.h` if you want the vendor splash
(stock Marlin ships none and `SHOW_CUSTOM_BOOTSCREEN` expects one).

### Which version to build

Upstream **never released 2.1.3** — the only tags are `2.1.3-b1/b2/b3`. The vendor zip
is **b3 (tagged 2026-01-22)**; the newest actual release is **2.1.2.9 (2026-10-12)**.

**Vendor b3 has a hard `G28 Y` fault on this board**: homing hangs with continuous
`echo:busy: processing` and never returns `ok`. Reproduced repeatedly; brownout, input
shaping, feedrate, endstop wiring and polarity, homing direction, position limits and
Z clearance were all eliminated by test. Cause still unidentified.

**2.1.2.9 homes correctly** once the trap-5 renames are fixed: `G28 Y` in 1.82 s, full
`G28` in 8.81 s.

### Build environment fixes

Three more, none of which change machine behaviour:

- `ini/esp32.ini`: `lib_ignore = NativeEthernet, AsyncTCP_RP2040W` — PlatformIO
  otherwise resolves a Raspberry Pi Pico W library.
- `ini/features.ini`: pin `esp32async/AsyncTCP@3.3.3` and
  `mathieucarbou/ESP Async WebServer@3.0.6`. Unpinned resolves to AsyncTCP 3.5.0,
  which needs an `esp_idf_version.h` that `espressif32@2.1.0` lacks.
- Build from a space-free path (trap 2).

---

## Firmware variant naming (vendor bins)

| Marker in filename | Meaning |
|---|---|
| `SLB` | **English** LCD |
| `小鲁班` | Chinese LCD |
| `出厂版` | "factory" build — reduced feature set |
| `125口` / `150口` / `220口` | bed/travel variant |
| `2208` / `4988` | stepper driver variant |

### The `4988` vs `2208` bins **do** differ in motor direction

A byte-level diff of the vendor's `SLB 125口 2208-出厂版.bin` and `SLB 125口 4988-出厂版.bin`
(both 1,121,840 bytes) shows **159 differing bytes**. Static disassembly confirms that
the differing code includes direction writes as well as driver timing.

Driver type only sets `MINIMUM_STEPPER_PULSE` and `MAXIMUM_STEPPER_RATE`
(`Marlin/src/inc/Conditionals_adv.h`):

| | A4988 | TMC2208 / 2209 |
|---|---|---|
| X/Y/Z/E direction tuple | `false/true/false/true` | `true/false/true/false` |
| `MINIMUM_STEPPER_PULSE` | 1 µs | 0 |
| `MAXIMUM_STEPPER_RATE` | 500 kHz | 5 MHz |

Direction comes from `INVERT_*_DIR`; the vendor source comment explicitly labels these
opposite tuples. Picking the wrong variant can therefore reverse motors as well as use
the wrong step timing.

### Published source ≠ shipped factory binaries

The vendor's 2.0.8.1 source zip has `THERMAL_PROTECTION_HOTENDS`,
`THERMAL_PROTECTION_BED` and `PIDTEMPBED` all **enabled**, yet a factory (`出厂版`)
binary reports them **off** (`M115` → `Cap:THERMAL_PROTECTION:0`, no `M304` in `M503`).
The released bins were built from a differently configured tree.

**→ Do not infer your running configuration from the published source. Ask the board
with `M115` and `M503`.**

---

## Building

```bash
pip install platformio
# clone upstream Marlin 2.1.2.9 to a SPACE-FREE path -- NOT the vendor's b3 zip:
#   git clone --depth 1 --branch 2.1.2.9 https://github.com/MarlinFirmware/Marlin.git
# then graft the board (3 files, see "Porting to another Marlin version")
cp config/config.ini <marlin>/Marlin/config.ini      # review it first
cd <marlin>
python -m platformio run -e esp32
# output: .pio/build/esp32/firmware.bin
```

Reference build: **1,150 KB, 87.8 % of the 1280 K app partition.**

## Flashing

The partition table read from a real board:

| Partition | Offset | Size |
|---|---|---|
| `nvs` (holds Marlin EEPROM) | `0x9000` | 20 K |
| `otadata` | `0xe000` | 8 K |
| **`app0`** | **`0x10000`** | **1280 K** |
| `app1` | `0x150000` | 1280 K |
| `spiffs` (ESP3D web UI) | `0x290000` | 1472 K |

**Back up first** — this is your only route home:

```bash
python -m esptool --port <PORT> --baud 460800 read-flash 0 0x400000 backup-4MB.bin
```

Flash the application only, leaving `nvs` and `spiffs` intact:

```bash
python -m esptool --port <PORT> --baud 460800 write-flash \
  --flash-mode dio --flash-freq 40m --flash-size 4MB 0x10000 firmware.bin
```

Flashing `app0` **preserves your EEPROM settings** across same-version updates.
A Marlin **major version change invalidates EEPROM** — capture `M503` beforehand and
replay it afterwards, then `M500`.

Full restore: `write-flash 0x0 backup-4MB.bin`.

### Serial

**250000 baud.** Opening the port with DTR **and** RTS asserted **resets the board**
(esptool auto-reset circuit) — set both false to monitor without rebooting. The ESP32
ROM boot log is suppressed on boards with the OLED fitted (GPIO15 strapping); Marlin's
own banner at 250000 carries the reset reason and IP instead.

---

## Verifying a build

Because a misconfigured build still reports `SUCCESS`, check the binary itself:

| Check | Expected |
|---|---|
| First byte | `0xE9` (ESP32 image magic) |
| Size | `< 1310720` (app0 partition) |
| CJK string runs | **~0** for an English build (a Chinese build has ~230) |
| Feature strings | `Babystep`, `Shaping`, `Probe Wizard`, `Bed Tramming`, `Thermal Runaway` |
| Regenerated `Configuration.h` | re-grep it *after* the build to confirm your INI values landed |

A quick CJK counter:

```python
import re
d = open('firmware.bin','rb').read()
print(len(re.findall(rb'(?:[\xe4-\xe9][\x80-\xbf]{2}){2,}', d)))
```

---

## Hardware reference

### Endstop / probe connector block

Six 3-pin `S G V` connectors below the drivers, in two rows:

```
top row:     X      Y      Z
bottom row:  E      C      P
```

| Port | GPIO | Function |
|---|---|---|
| X | 15 | X endstop |
| Y | 16 | Y endstop |
| Z | 17 | Z endstop |
| E | 13 | filament runout |
| **C** | **14** | **Z probe** (`Z_MIN_PROBE_PIN 14`), V pin jumper-selectable 3.3/5 V |
| P | 35 | spare, 3.3 V only |

Other pins: `TEMP_0 36`, `TEMP_BED 39`, `HEATER_0 141`, `HEATER_BED 140`, `FAN 143`,
`E0_AUTO_FAN 142`. Steppers run over I2S (`WS 26`, `BCK 25`, `DATA 27`, expander pins 128+).

### Inductive probe (SN04-N) wiring

Vendor routes it to the **C port**: brown → `V`, blue → `G`, black → `S`.
Keep the Z endstop plugged into the Z port — probe and Z endstop are independent inputs.

> **A stock SN04-N is a 6–36 V sensor.** The C port supplies at most 5 V, so a standard
> unit will not power up. If you run it from 12 V, verify the output is true
> open-collector before connecting it: power the sensor, leave it untriggered, and
> measure signal↔ground. Floating or ~0 V is safe. **~12 V means an internal pull-up
> and will destroy GPIO14 (3.3 V logic).**

Verify with `M119` — `z_probe` should toggle when metal approaches.

### WiFi / ESP3D

The board runs ESP3D. Upload `index.html.gz` **as-is, without renaming or extracting**,
via the *Flash Filesystem* box at `192.168.0.1` (AP `MARLIN-ESP`, password `12345678`) —
not the *Firmware Update* box below it.

**There is no OctoPrint API.** `/api/version`, `/api/printer` and `/api/files/local` all
return ESP3D's "Unknown page". In OrcaSlicer 2.4.2, use the native **ESP3D** host type
instead of OctoPrint: raw Hostname/IP `192.168.8.109` and Device UI
`http://192.168.8.109`. Orca uploads through `/upload_serial` and applies the 8.3
filename limit. The lower-level ESP3D endpoints that also work are:

- `GET /command?commandText=<gcode>` → returns `Ok` (responses go to the websocket, not this response)
- `GET /files?action=list&path=/` → JSON listing of the flash filesystem

The vendor documents AP mode as a cause of random reboots; **station (client) mode,
2.4 GHz only, is their fix**.

---

## Configuration

| File | For | Status |
|---|---|---|
| [`config/config-2.1.2.9.ini`](config/config-2.1.2.9.ini) | upstream **2.1.2.9** | **verified on hardware** — homes, prints, SD works |
| [`config/pins_MM_JOKER-2.1.2.9.h`](config/pins_MM_JOKER-2.1.2.9.h) | upstream **2.1.2.9** | board file with the `SDSS` alias (trap 5) |
| [`config/config.ini`](config/config.ini) | vendor **2.1.3-b3** | compiles, but b3 hangs on `G28 Y` — see "Which version to build" |
| [`config/config.ini.diff`](config/config.ini.diff) | vendor 2.1.3-b3 | changes from vendor stock |

The 2.1.2.9 config deliberately matches the known-good 2.0.8.1 machine behaviour
rather than the vendor's 2.1.3 defaults: endstop polarity `true`, bed thermal
protection **on**, `Z_HOMING_HEIGHT 5`, hotend PID `18.00/1.47/55.30`, bed PID on at
`74.10/14.22/257.80`, directions `true/false/true/false`.

The table below describes the **b3** config and is kept for reference.

| Change | Rationale |
|---|---|
| `lcd_language` `zh_CN` → `en` | The "SLB" (English) source ships set to Chinese |
| `z_max_pos` `161` → `145` | Machine-specific overhead clearance — **set yours** |
| `*_driver_type` — **leave at `A4988`** | The vendor's own comment says UART-less Trinamics should stay `A4988`. The type only sets step-pulse timing, and with `I2S_STEPPER_STREAM` pulses land on the 4 µs DMA grid anyway, so it changes nothing real. Direction is set by `INVERT_*_DIR`, not by this. |
| `babystepping` + `babystep_zprobe_offset` → `on` | Live first-layer adjustment, writes back to `M851 Z` |
| `input_shaping_x/y` + `shaping_menu` | **Leave off.** `M593` exists from 2.1.x, but input shaping on an I2S stepper stream depends on [PR #28710](https://github.com/MarlinFirmware/Marlin/pull/28710), still an open pull request reported as tested on a TinyBee *with no motors attached*. Unproven on this board. |
| `g38_probe_target` + `g38_probe_away` → `on` | Probe-toward/away, enables bed edge-finding |
| `thermal_protection_bed` → `on`, hysteresis `2`→`8` °C, period `120`→`180` s | See the warning below |
| `lin_advance` | forced `off` — impossible on this board |

### ⚠️ Bed thermal protection on 12 V machines

The vendor's config comments read `小鲁班12V不开off，24V开on` — *"SLuban 12 V: leave
off; 24 V: turn on"* — for both `thermal_protection_bed` and `pidtempbed`. A 50 W 12 V
bed heats slowly enough to trip Marlin's default checks spuriously.

This config departs from that for **`thermal_protection_bed` only**, because runaway
protection is a fire-safety feature, and compensates by widening the trip thresholds to
8 °C / 180 s so a merely *slow* bed is not mistaken for a runaway one.

`pidtempbed` is left **off** per vendor guidance — bang-bang suits a marginal heater
better, and it is a comfort feature rather than a safety one.

**If you get spurious "Thermal Runaway" or "Heating Failed" on the bed, widen these
further or follow the vendor and disable the feature.** Your call, your machine.

### Input shaping is not usable here yet

`M593` is present from Marlin 2.1.x, but working input shaping on an I2S stepper
stream needs the shared-scheduler work in
[PR #28710](https://github.com/MarlinFirmware/Marlin/pull/28710) — still an open pull
request, reported as tested on a TinyBee **without motors attached**. Nothing
demonstrates it working on this board.

An accelerometer measures resonance; it cannot resolve a scheduler incompatibility.
Resolve the firmware path before buying ADXL345 hardware for this printer.

---

## Known-good machine parameters (SLuban, 125 × 125 bed)

```
M92  X100.00 Y100.00 Z1600.00 E87.00     ; MK8 extruder
M203 X300.00 Y300.00 Z5.00 E40.00
M201 X1000.00 Y1000.00 Z100.00 E10000.00
M204 P3000.00 R3000.00 T3000.00
M205 B20000.00 S0.00 T0.00 J0.01
M301 P18.00 I1.47 D55.30                 ; hotend PID, retune at your print temp
M851 X12.00 Y-30.00 Z0.00                ; MK8 probe offset; Z must be calibrated
```

Vendor extruder presets: **MK8** `E87`, probe `12, -30` · **Titan** `E387`, probe `-24, -24`
· **BMG** `E387`, probe `-22, -24`.

X homes to **maximum** (switch on the right) — `X_HOME_DIR 1`. After `G28`, `M114`
reads `X:125`, not `X:0`. This is correct, not a fault. Since the reference point is the
far end, `X_MAX_POS` is a calibration value: if it is wrong, every print shifts sideways.
Check by homing, jogging to `X0`, and looking at where the nozzle sits. Correct with
`M206 X<offset>` + `M500`.

Hotend max is **250 °C** with a PTFE-lined throat. Do not exceed it without an
all-metal hotend.

---

## Not verified here

- CoreXY variants, the SPARROW board, and 150/220 mm machines — config keys exist but were not tested
- Whether input shaping degrades step timing on the I2S stream under load
- Laser module support

## License & attribution

Documentation: **CC BY 4.0**. `config.ini` is derived from the Marlin configuration
distributed by MagicMaker and remains under **GPL-3.0**, as does Marlin itself.

Marlin: <https://github.com/MarlinFirmware/Marlin> ·
Vendor: <https://github.com/magicmaker3/magicmaker> ·
Board lineage: <https://github.com/maplerainresearch/MRR_ESPE>

Not affiliated with or endorsed by MagicMaker.

**No warranty.** Flashing firmware can brick your board, and a printer with modified
thermal settings can start a fire. Back up your flash, verify your build, and never
print unattended.
