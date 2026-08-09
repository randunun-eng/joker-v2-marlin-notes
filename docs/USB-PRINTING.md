# Printing over USB (the `Joker V2 wired` profile)

## Why this exists

**OrcaSlicer cannot print over USB.** This is not a configuration gap — the
feature does not exist. Orca's print-host factory
(`src/slic3r/Utils/PrintHost.cpp`, `PrintHost::get_print_host`) only ever
constructs *network* hosts:

    OctoPrint, Duet, FlashAir, AstroBox, Repetier, PrusaLink, PrusaConnect,
    MKS, ESP3D, CrealityPrint, Obico, Flashforge, SimplyPrint, ElegooLink,
    3DPrinterOS, Moonraker

`src/slic3r/Utils/Serial.cpp` exists but is port enumeration for firmware
flashing, not a print backend. USB streaming is a Pronterface/Cura feature.

So Orca's role is **slice → Export G-code**, and `tools/usb_print.py` streams
the result over the wire.

The other reason this exists: the ESP3D wireless path on this board is not
dependable. The ESP32's IP stack wedges after a small number of HTTP requests —
measured from a cold boot, free heap fell 167 KB → 128 KB across **5 requests in
60 seconds**, after which the board stopped answering ICMP and TCP entirely
while still replying to ARP. Only a power-cycle recovers it. USB and SD do not
have that failure mode.

## The two printer profiles

Both inherit `MM slb 0.4 nozzle`, so bed volume and every motion/extrusion
parameter are identical by construction — **125 × 125 × 145 mm**, 0.4 mm nozzle.
They differ only in how the job reaches the machine.

| Profile | Transport | Host fields |
|---|---|---|
| `Joker V2 Wireless` | ESP3D over Wi-Fi | `print_host` = `192.168.8.109` |
| `Joker V2 wired` | USB serial / SD | host fields blank |

Installed copies live in
`~/Library/Application Support/OrcaSlicer/user/default/machine/`.
Each needs its `.json` **and** a matching `.info` sidecar; the `base_id` in the
`.info` is the parent's `setting_id` (`A0I3dDAbwNC059c6`). Orca only reads new
user presets at startup — restart it after adding one.

## Usage

    tools/usb_print.py FILE.gcode --dry-run    # parse + validate, never opens the port
    tools/usb_print.py FILE.gcode              # stream to the printer
    tools/usb_print.py --list-ports

Port defaults to `/dev/cu.usbserial-2120` at **250000** baud
(`Marlin/Configuration.h`). Requires `pyserial`.

The script uses Marlin line numbering + checksums, so a corrupted line is resent
rather than silently mangling the print.

## Board-specific behaviour the script accounts for

* **Opening the port resets the ESP32.** DTR/RTS drive the esptool-style
  auto-reset circuit. That is desirable at print start (clean state), so the
  script does it and waits for Marlin's banner. `--no-reset` attaches without
  rebooting.
* **`echo:busy: processing` is liveness, not an error.** Marlin floods it during
  heating and long moves. An earlier debugging session concluded a working `G28`
  had "crashed" purely because a script filtered `busy` out and used a short
  read window. Never conclude anything from silence.
* **`M112` is not immediate.** `EMERGENCY_PARSER` is impossible on ESP32 (Marlin
  HAL limitation, `src/HAL/ESP32/inc/SanityCheck.h`), so any abort queues behind
  buffered moves. **The physical power switch is the only real emergency stop.**

## Before the first print

* The **filament runout sensor works** — its indicator is visible on the unit and
  a vendor file has printed with it in place. HANDOVER §6.3 still claims the
  sensor reads TRIGGERED and needs `M412 S0`; that note is stale, and no `M412`
  workaround is needed.
* Do **not** enable `M420 S1` while `M851 Z` is `0.00` — the nozzle will crash or
  print in air. The script's `--dry-run` warns if it sees `M420 S1`.
* USB streaming dies if the host sleeps or the cable is disturbed. For long or
  unattended prints prefer SD: FAT32, under 16 GB, Class 4+, quick-format OFF,
  and **8.3 filenames** (`TEST1.GCO`).

---

# printerd: persistent print service

`tools/usb_print.py` ties the print to a terminal session. `tools/printerd.py`
is the daemon version: it owns the serial port for its whole lifetime (a
reconnect would reset the ESP32) and exposes the printer over HTTP, so a
browser tab, the CLI and the MCP server all see one consistent state.

    tools/printerd.py                 # then open http://127.0.0.1:8765
    tools/printerd.py --no-connect    # serve the UI without opening the port

**SD upload is the default path**, not USB streaming:

    M28 NAME.GCO -> stream -> M29 -> M23 NAME.GCO -> M24

After `M24` the printer runs off its own card, so the Mac becomes irrelevant --
sleep it, unplug USB, the print continues. Progress then comes from polling
`M27`, which needs no open stream. Verified present in this firmware:
`SDSUPPORT` on (`Configuration.h:1939`), `M28` compiled (`gcode.cpp:474`).

Cost: `BINARY_FILE_TRANSFER` is disabled, so upload is ASCII and takes roughly
25-45 min for a 20 MB file. That is expected.

SD upload buys **robustness, not speed.** Measured mid-print over USB, the link
ran at 360 B/s against ~25,000 B/s capacity (1.4% utilisation) with ~80 ms per
command -- far longer than serial round-trip latency. The printer paces the job,
not the cable.

## 8.3 filenames are mandatory

`LONG_FILENAME_HOST_SUPPORT` is disabled, so uploads must be 8.3. Naive
truncation collides -- `eiffel_final_PLA_4h11m` and `eiffel_final_PLA_9h50m`
both reduce to `EIFFELFI`, which would silently overwrite the wrong file on the
card, possibly one being printed. `sd_name()` therefore uses 4 characters of the
stem plus 4 hex characters of a path hash: stable per file, unique across files.

## MCP

`tools/mcp_printer.py` is a thin client over printerd's HTTP API -- it holds no
serial handle, because the port is exclusive and printerd owns it.

    claude mcp add joker -- /path/to/tools/mcp_printer.py

Tools: `printer_status`, `list_gcode`, `start_print`, `abort_print`.
There is deliberately **no raw-G-code tool**; motion and heaters are not
scriptable from a prompt, and `start_print` cannot bypass the bounds check.
`abort_print` is not an emergency stop -- EMERGENCY_PARSER is impossible on
ESP32, so aborts queue behind buffered moves.
