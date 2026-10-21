# MagicMaker print optimization skill

Created 2026-10-20 for the MagicMaker SLuban / JOKER V2 Marlin printer and customized OrcaSlicer app.

## Use it

- Codex: `$magicmaker-print-optimize`
- Claude Code: `/magicmaker-print-optimize`
- Example: “Use the MagicMaker print optimization skill to compare this bracket for strength, print quality and filament cost. Keep the original profile.”

The canonical skill lives at `/Users/nadeeshan/.codex/skills/magicmaker-print-optimize`. The personal Claude Code skill directory `/Users/nadeeshan/.claude/skills/magicmaker-print-optimize` links to that same folder. Editing the canonical Markdown updates both local installations. A running assistant may need a new session to discover it.

For Claude's skill-upload interface, use [magicmaker-print-optimize.zip](../../dist/magicmaker-print-optimize.zip). This is a dated portable snapshot; rebuild it after changing the canonical files. It has not been uploaded to a Claude account. See [Claude's custom skill instructions](https://support.claude.com/en/articles/12512198-how-to-create-custom-skills) and [Claude Code skill locations and invocation](https://code.claude.com/docs/en/skills).

## Markdown knowledge files

- [Skill workflow](/Users/nadeeshan/.codex/skills/magicmaker-print-optimize/SKILL.md): inspect the part, resolve effective settings, compare variants, and distinguish proposed changes from physical results.
- [Printer baseline](/Users/nadeeshan/.codex/skills/magicmaker-print-optimize/references/printer-baseline.md): hardware, firmware limits, dated profile values, Wi-Fi setup, backups and exact local evidence paths.
- [Optimization and calibration](/Users/nadeeshan/.codex/skills/magicmaker-print-optimize/references/optimization.md): appearance, fit, strength, orientation, shells, supports, material/time tradeoffs, flow and retraction, and symptom-led troubleshooting.
- [Experiment record](/Users/nadeeshan/.codex/skills/magicmaker-print-optimize/assets/experiment.md): baseline, changed variables, actual slicing estimates, physical observations, acceptance and rollback.

The ZIP contains all these files plus optional Codex display metadata. On another computer, its dated printer snapshot remains useful even when the original Mac paths are unavailable; provide current profiles and model/project files before claiming current settings.

## Scope and evidence

This skill supplies an optimization workflow, not a universally optimal preset. It does not certify a part's load capacity. It distinguishes the configured 50 mm³/s PLA flow limit from measured hotend capacity and skips unsupported linear advance and input shaping on the current firmware.

Creating the skill did not change active printer or slicer settings, start a print, or calibrate the machine. Validation records are in `../../.codex/runs/2026-10-20-print-optimization-skill/`.
