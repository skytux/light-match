# Light Match

Make lamps of different brands look the same in Home Assistant.

Bulbs from different makers react differently to the same command. "10 %"
on one bulb is a soft glow and on another a reading light; "red" is deep red
on one and orange-red on the next; some bulbs will not dim below a floor at
all. Light Match gives each real lamp a **calibrated twin**: a light entity
that translates every command into whatever that particular lamp needs to
show the same look. Point your voice assistant, scenes and dashboards at the
twins and the same command looks the same on every lamp.

You calibrate by eye, in a sidebar panel, against a reference lamp. No
colorimeter, lux meter or scripts are needed.

## What you get

- One calibrated twin per lamp. It supports brightness, hue/saturation and
  colour temperature, passes transitions through, and mirrors the real lamp's
  on/off state and availability.
- The twin's state is shown in the calibrated space: when the real lamp is
  changed some other way (its own app, a wall switch), the twin works out
  which calibrated look that is.
- A calibration grid per lamp: 8 colours (red, orange, yellow, green, cyan,
  blue, purple, pink) and 4 whites (2200, 2700, 4000, 6500 K), each at 1, 5,
  20, 50 and 100 % brightness.
- A calibration panel in the sidebar that walks you through the grid.
- Smart defaults for cells you have not calibrated, including the
  **sub-floor trick**: below the brightness floor of an RGB/RGBW bulb, the
  lamp is sent its floor brightness with weaker colour channels, so it goes
  dimmer than it otherwise would.
- Profiles are exported and imported as JSON, so a calibration can be shared
  for a bulb model.

## Install

### HACS (custom repository)

1. HACS → three-dot menu → **Custom repositories** → add this repository's
   URL with type **Integration**.
2. Install **Light Match**, then restart Home Assistant.

### Manual

Copy `custom_components/light_match` into the `custom_components` folder of
your Home Assistant configuration and restart Home Assistant.

## Set up

1. **Settings → Devices & services → Add integration → Light Match.**
2. Pick the real lamp (the *source*) and, if you like, a name. Without a name
   the twin is called "<lamp name> calibrated".
3. Repeat for every lamp you want to match.

Each twin has options (**Configure** on the entry):

| Option | What it does |
| --- | --- |
| Behaviour | **Calibrated, smart defaults** (default): calibrated cells are used, other cells get defaults with the sub-floor trick. **Calibrated, plain defaults**: the same without the sub-floor trick. **This lamp is the reference**: commands pass through unchanged; use this for the lamp whose look the others should match. |
| Reference lamp | The lamp the panel offers as the look to match when you calibrate this one. |
| Detect the brightness floor | Uses the known floor for the source's integration (currently WiZ: 26 of 255, i.e. 10 %). |
| Brightness floor | Set the floor yourself (0–255) when detection is off. 0 means the lamp has no floor. |
| Whites with the colour LEDs | For lamps with both colour temperature and RGB/RGBW/RGBWW channels. **Below the brightness floor** (default): white cells the colour temperature mode cannot reach (below the floor) are shown with the colour channels instead, at the floor brightness with the channels scaled down, so whites go as dim as colours. Above the floor the lamp's own white is used, and the switch happens where the level crosses the floor. **Always**: every white with the colour channels. **Never**: the colour temperature mode (except whites outside the lamp's range, see Limits). Each white cell can still be switched in the panel. |
| Hide the source light | Hides the real lamp from dashboards and voice assistants, so only the twin is used. Removing the twin shows it again. A lamp you hid yourself is left alone. |

## Calibrate

1. Pick your best lamp as the reference. Create its twin and set its
   behaviour to **This lamp is the reference** (or use any plain light as the
   reference).
2. Open **Light Match** in the sidebar (administrators only).
3. Choose the lamp to calibrate and the reference lamp. Put the two lamps
   next to each other, or so you can see both.
4. For each cell the reference lamp shows the target look, for example
   "orange at 5 %". The lamp under test shows its current command for that
   cell. Move the sliders until the two look the same. Every change is sent
   to the lamp live. The sliders match how the lamp is commanded:
   - brightness (1–255) always;
   - red/green/blue(/white) channels for RGB, RGBW and RGBWW lamps;
   - hue and saturation for lamps that take colours as hue/saturation or xy;
   - colour temperature for white cells of lamps that have it.

   A white cell of a lamp with colour LEDs has a **Calibrate in colour
   mode** switch. It changes that cell's sliders to channel sliders and saves
   the cell in the colour mode, so a dim white can go below the brightness
   floor. It starts as the lamp option sets it; whites shown with the colour
   LEDs have a colour bar in the grid.
5. **Save** stores the cell and moves on; **Skip** leaves it at its default.
   **Previous** goes back; **Reset this cell** returns a cell to its default.
   Click any square in the grid to jump to that cell. The bar at the top shows
   how many cells are calibrated.

You do not need to calibrate everything. Start with the cells that look most
wrong, typically the lowest brightness levels and the reds. Uncalibrated
cells use the defaults, and every target between cells is interpolated.

## How it works

**Targets and cells.** A twin receives a uniform command: a colour (hue,
saturation) or a white (kelvin) at a brightness. Each grid cell stores the
*source command* that makes this lamp show the cell's look: a brightness plus
RGB/RGBW/RGBWW channels, hue/saturation, or a colour temperature, depending on
what the lamp supports.

**Interpolation.** A target between cells is blended from its neighbours:

- brightness between levels in a perceptual space (CIE L\*), so a halfway
  step looks halfway;
- hue between the two neighbouring colour anchors along the colour circle;
- colour temperature between the neighbouring whites, in mireds;
- below 1 %, the 1 % cell scaled down in proportion.

Saturation is not calibrated: a less saturated colour is the calibrated
full-saturation colour blended towards white (or the saturation scaled, for
hue/saturation lamps).

**Defaults.** An uncalibrated cell gets the command Home Assistant would
send, translated into the lamp's own mode. With smart defaults, a target
below the lamp's brightness floor is sent at the floor with the colour
channels scaled down. For example a WiZ RGBW bulb asked for red at 1 % gets
brightness 26 with red channel 25 instead of red 255: it shows a fraction of
its floor brightness instead of staying at 10 %.

**Imported profiles.** About 600 lamp models come with an imported profile
(`profiles/imported/`), generated from public, permissively licensed
databases: the Powercalc profile library, zigbee-herdsman-converters,
deCONZ, diyHue, pywizlight and python-yeelight. They are *partly calibrated,
imported and unverified*: model ids, colour modes, kelvin range, lumens,
Hue gamut, and for Powercalc lamps an estimated brightness curve and
sometimes a brightness floor. They never hold colour cells. The curve comes
from power readings, not light (quality "power-proxy"): the lowest reading
is treated as driver overhead and the default brightness is set so the
lamp's output follows the reference space (brightness % = L\*). What the
lamp itself reports (kelvin range, colour modes, a gamut attribute) wins
over imported and spec data; a measured or verified community profile wins
over everything, and fields missing from the best profile are filled from
the next one. Each profile lists its sources, licences and the quality of
each field in `provenance.imports`. To regenerate them: `tools/import/run.sh`
(containers only).

**State.** When the source reports the command the twin just sent, the twin
shows the look it was asked for. When the source changes another way, the
twin inverse-maps the source state through the profile to find the closest
calibrated look.

## Profile database

Light Match ships a database of lamp profiles in
`custom_components/light_match/profiles/`. When you set up a twin, it matches
the source lamp's manufacturer and model (from the device registry) against
the database and uses the best profile as the model layer: its capabilities
(kelvin range, floor, gamut) and any calibrated cells. Your own cells always
sit on top and are never changed by the database. The options let you pick
another profile or none.

Ranking, best first:

1. measured or verified community profiles (by eye against lm-ref-1, lux
   meter, colorimeter), even a generic one for the brand;
2. the closest model match;
3. imported profiles (partly calibrated, unverified);
4. spec profiles from public data sheets;
5. among equals: verified, then rating, then revision.

Capabilities missing from the best profile are filled from the next ones.
What the lamp itself reports to Home Assistant wins over imported and spec
data.

**Online update (optional, off by default).** A checkbox in the panel (or
`light_match.update_profiles`) fetches the profile index from GitHub once a
day with an ETag and downloads only profiles newer than the bundled ones,
each checked against the index's checksum and the schema. Turning it off
removes the downloaded layer.

**Privacy.** Nothing is sent anywhere unless you turn the online update on.
Then Light Match only downloads public files from GitHub: no device data, no
ids, no counting by Light Match. GitHub's own download counter on the
release files is what the download stats show. Sharing a profile only
happens when you click **Share profile**, and then only through a GitHub
issue you submit yourself.

**Ratings.** Each profile has a GitHub Discussion; its thumbs up and down
are the rating shown in the panel, next to the downloads, the contributors'
nicknames and the last update. **Rate / report** opens it.

See [CONTRIBUTING.md](CONTRIBUTING.md) to calibrate and submit a profile.

## Sharing profiles

The panel's **Export profile** downloads the lamp's calibration as a JSON
file, optionally with the bulb model typed in the field above it. The download
goes through a short-lived signed link, so it also works in the companion app
and in phone browsers. **Copy JSON** puts the same JSON on the clipboard (or
shows it to copy by hand where there is no clipboard). **Import profile** loads
a file and **Paste JSON** loads pasted text, either replacing the lamp's cells
or merging them. **Copy calibration from** reuses another twin's calibrated
cells on this lamp, with the same conversion as an import. A profile made on a lamp that is
commanded differently is converted where possible (for example RGBW channels
to hue/saturation); cells that cannot be converted are skipped. Calibrations
are by eye and depend on the reference lamp, so treat a shared profile as a
starting point and fine-tune it.

Profile format:

```json
{
  "format": "light_match_profile",
  "version": 1,
  "model": "Example A60 RGBW",
  "modes": {"color": "rgbw", "white": "color_temp"},
  "floor": 26,
  "cells": {
    "color:red:1": {"brightness": 26, "rgbw_color": [12, 0, 0, 0]},
    "white:2700:20": {"brightness": 48, "color_temp_kelvin": 2650}
  }
}
```

Cell keys are `color:<name>:<level>` or `white:<kelvin>:<level>`, with levels
1, 5, 20, 50 and 100.

**Share profile** builds a database profile (format version 2: device model
ids, capabilities, provenance, an optional nickname and the CC BY 4.0
licence) and opens a prefilled "New lamp profile" issue on GitHub. Nothing is
sent until you submit that issue yourself.

## Services

| Service | What it does |
| --- | --- |
| `light_match.calibrate_cell` | Store a source command for a cell (`entity_id` of the twin, `cell`, `brightness` plus `rgb_color`/`rgbw_color`/`rgbww_color`/`hs_color`/`color_temp_kelvin`). |
| `light_match.reset_cell` | Forget a calibrated cell, or every cell with `cell: "*"`. |
| `light_match.export_profile` | Return the profile as a response (optional `model`). |
| `light_match.import_profile` | Load a profile (object or JSON text); `merge: true` keeps cells it does not have. |
| `light_match.import_brightness_curve` | Fill the uncalibrated cells from a brightness curve of target % to lamp %, for example a Lightener brightness map: `curve: {"10": 30, "50": 70}`. Colours keep their defaults. |

## Limits

- Calibration is by eye and only as good as the match you make. Viewing
  angle, diffusers and the room change how a lamp looks.
- Whether the sub-floor trick works depends on the lamp: it needs a lamp that
  really dims its channels at the floor brightness. It has been seen on WiZ
  RGBW bulbs; other brands may ignore weak channels or round them up.
- Saturation is not calibrated (see above).
- Effects are passed through unchanged: the twin lists the source's effects,
  and starting one sends only the effect and a calibrated brightness. The
  lamp's own show is not calibrated, and while it runs the twin's colour is
  only an estimate.
- The twin exposes colour temperatures from 2200 to 6500 K on every lamp.
  A colour lamp whose own white range is narrower shows the whites outside
  it (more than 100 K beyond) with its colour LEDs; those cells are
  calibrated in the colour mode. Lamps without colour LEDs are clamped to
  their range.
- Lamps that only switch on and off are mirrored but cannot be calibrated.
- The inverse mapping of an externally changed lamp is a best estimate; it
  assumes brightness grows with the target and hue goes round the circle in
  order.

## Related projects / how this differs

- [Lightener](https://github.com/fredck/lightener) and Lightener Studio:
  per-lamp brightness curves for grouped lights. Brightness only. Light Match
  can import such a curve as a starting point
  (`light_match.import_brightness_curve`).
- [Gamma Light](https://github.com/MarkGodwin/hacs_gamma_light): minimum,
  maximum and gamma correction of brightness. Brightness only.
- Lumen Calibrator: a brightness lookup table measured with a phone's light
  sensor; a separate app rather than a HACS integration, brightness only.
- Community guides with HCFR and 3D lookup tables: colour-accurate, but need
  a colorimeter and scripts.

Light Match calibrates **colour × brightness per lamp by eye** in a panel,
matches lamps against a reference lamp rather than an absolute standard, and
uses the sub-floor colour-channel trick to get below a lamp's brightness
floor.

## Development

Everything runs in containers; nothing is installed on the host:

```sh
./scripts/test.sh
```

This builds a test image (Python 3.13, Home Assistant through
`pytest-homeassistant-custom-component`), runs ruff and the tests, and checks
the panel's JavaScript with `node --check`.

## Licence

GPL-2.0-or-later. Copyright (C) 2026 Lou H. See [LICENSE](LICENSE).

The lamp profile data is CC BY 4.0; submitting a profile means you agree to
license it under CC BY 4.0 (see
[profiles/LICENSE](custom_components/light_match/profiles/LICENSE)).
Imported profiles also keep the MIT, BSD-2-Clause, BSD-3-Clause and
Apache-2.0 notices of the databases they come from, in
[profiles/imported/](custom_components/light_match/profiles/imported/NOTICE.md).
