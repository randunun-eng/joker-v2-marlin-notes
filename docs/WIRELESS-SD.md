# Wireless transfer to onboard SD

The original embedded ESP3D web server has a native direct SD upload route. This client uses that route over Wi-Fi; it does not open USB, start a print, or issue printer motion, heater or reset commands. It does not use `/upload_serial`. The local Orca ESP3D backend uses `/upload_serial`, which does not match this direct endpoint. Earlier failures with USB serial M28 do not establish whether direct SD upload works.

The protocol was checked against the cached ESP3DLib 1.0.12 source for v2.1.3; the exact library used in the printer's original v2.0.8 firmware is not established. **No transfer has been tested on the printer.** Before any live attempt, identify its current IP and confirm on the printer that it is idle and the card is inserted. The client asks `[ESP800]` for Direct SD capability, `[ESP200]` for card availability, and checks the direct root listing. A busy, absent, unknown or malformed response stops the transfer. These checks cannot guarantee that the printer remains idle or that another client does not create a conflicting file between the check and upload; avoid concurrent access. The firmware can overwrite an existing filename, so the client refuses names found in either `name` or `shortname` but cannot make that check atomic.

With the correct printer address, inspect first:

```sh
python3 tools/wifi_sd.py --host 192.168.1.20 probe
python3 tools/wifi_sd.py --host 192.168.1.20 list
```

Replace the address with the printer's actual IP, optionally followed by `:port`. The first live upload should be a unique harmless comments-only file, and must not be started as a print:

```sh
python3 tools/wifi_sd.py --host 192.168.1.20 upload harmless.gcode --name TEST01.GCO
```

Names must be uppercase, with 1–8 ASCII letters, digits or underscores before `.GCO`. If omitted, a name based on the local filename and path hash is generated. Collisions are rejected rather than renamed or overwritten. The client sends the file in chunks using the original multipart fields and reads `/SD/NAME.GCO` back to compare exact byte count and SHA256. A reported success requires matching readback, not just HTTP 200. The transfer has no automatic retry. On timeout, interrupted upload, or verification failure, a partial or complete file may remain on the card; inspect it before trying again and do not assume success.

Files must be between 1 byte and 128 MiB. Command, listing and upload response bodies are limited to 1 MiB; uploads and readback are streamed in 64 KiB chunks. Connections use a 15-second socket timeout, rather than a total operation deadline. HTTP redirects and configured proxy settings are not followed. The client uses plain HTTP without authentication support, so use only a trusted network. It does not install software, change firmware, or configure the network.
