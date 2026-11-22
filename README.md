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
   The circle shows the target colour and brightness as a screen can show
   it. A lit lamp never looks black, even at 1 %, so full output is screen
   white and lower levels follow perceived lightness (CIE L*) down to a
   **screen floor**, half brightness by default. Change the floor with the
   slider under the hint; the panel remembers it in this browser. Tap the
   circle for a full-screen patch; Back (Android or browser), Esc or a tap
   closes it without leaving the panel.

   **Match the screen first.** The swatch is only fair when the screen is set
   up for it: good colour rendering (AMOLED is best), colour mode Natural or
   sRGB (not Vivid), night mode and blue-light filter off, enhanced contrast,
   adaptive colour, True Tone and eye comfort off, HDR boost off. Then press
   **Match screen to lamp** in *Screen and calibration details*: the lamp
   (or the reference lamp) shows its 100 % neutral white and the phone shows
   a full-screen white patch. Set the phone brightness until the two match,
   turn auto-brightness off and keep it there for the whole session. The
   floor above is relative to this matched screen.
5. **Save** stores the cell and moves on; **Skip** leaves it at its default.
   **Previous** goes back; **Reset this cell** returns a cell to its default.
   Click any square in the grid to jump to that cell. The bar at the top shows
   how many cells are calibrated.

**Calibration details** (same section, all optional, saved with the lamp and
included in exports and shares): the box *I calibrated under these
conditions* (shown as a **good practice** badge, worth a small ranking boost
in the database), the screen used (prefilled from the browser, editable,
never sent anywhere by itself), the instrument, the method (by eye, lux meter,
colorimeter) and a note of up to 500 characters. *Match screen to lamp* also
records `screen_matched`.

### Camera calibration (rough)

*Camera calibration (rough)* in the calibrate card lets the phone's rear
camera do the matching: it measures the lamps and adjusts the lamp under test
step by step (8 to 12 small damped steps, stopping when the colour and
brightness are close). It is rough: a phone camera is not a colour meter, so
check every result by eye. Nothing is saved automatically: **Auto-match this
cell** puts the result in the sliders and you press **Save**; **Auto-run all
cells** goes through the cells with a short pause and a **Stop** button, then
offers **Save the N matched cells**. A lamp calibrated this way records the
method *camera (rough)*.

**Use Chrome on an Android phone for camera calibration.** Only Chrome on
Android lets a web page switch off the camera's automatic white balance and
automatic exposure. With those on, the camera keeps "correcting" the picture:
a warm lamp gets shifted toward white and a dim lamp gets brightened. That
hides exactly the differences being measured, and the lamp gets adjusted to
the wrong colour. On iPhone every browser uses Safari's engine, which has no
such controls; Firefox does not offer them either. The panel locks white
balance (about 5000 K), exposure and focus where it can and lists what it
managed; where it could not, it shows this recommendation first and offers
*Continue anyway (very rough)*. The Home Assistant companion app often does
not give the camera to the page at all: open Home Assistant in Chrome
instead. The page must be served over HTTPS for the browser to offer the
camera.

Two modes:

- **Against the reference lamp** (better): both lamps, or the white paper or
  wall each one lights, in the same picture. Tap the reference, then the lamp
  under test. The reference shows each cell's target and the camera compares
  the two in the same frame, so the camera's own colour errors mostly cancel.
- **Without a reference lamp**: the lamp is matched to the cell's lm-ref-1
  colour and relative output directly. First **Measure 100 % anchor**: the
  lamp shows its 100 % neutral white with the exposure locked, which defines
  output 1.0; when the panel changes the exposure (to keep a patch from
  clipping or going too dark) it rescales by the exposure ratio. This mode
  depends on the camera's colours, so the optional **camera correction**
  helps: open the panel on a second screen (a computer or tablet, set up like
  the phone screen above), press **Show patches** there and measure the red,
  green, blue and white patches with the phone (its rear camera cannot see
  its own screen). The fitted correction is kept in that browser. Profiles
  made without a reference rank below those made with one.

The details go into the profile's `calibration`: `camera_locked: {wb,
exposure}`, `camera_reference` (`lamp` or `absolute`) and
`camera_corrected`.

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

**Rating from the panel** (optional): when the release has a GitHub OAuth
app set up, the online profiles card offers **Sign in to GitHub**. It uses
GitHub's device flow: the panel shows a code, you enter it on github.com,
and the token is kept in this Home Assistant (`.storage/light_match.github`),
never in the browser. It asks for the `public_repo` scope because GitHub
needs it to add a reaction on a public repository's discussion; Light Match
only adds or removes your thumbs up/down on the profile's discussion.
👍 / 👎 links then appear next to profiles that have a discussion. **Sign
out of GitHub** forgets the token (you can also revoke it in your GitHub
settings). Without the OAuth app the feature is hidden; the Discussions link
always works.

**Format versions.** Profile files carry `format_version` (a file without
it is format 1). Files are written in the lowest format that holds them:
format 2, or format 3 when they carry camera calibration details, so Light
Match 0.8.1 still applies profiles calibrated by eye and shows camera ones
read-only. They also carry `app_version`, the Light Match release that wrote
it. Older formats are converted on import and when loaded from the database;
saved ones show **Convert to the current format** in *My calibrations*. A
file of a newer format than your Light Match knows is shown, read-only, with
"update Light Match to use this profile", and never applied.

See [CONTRIBUTING.md](CONTRIBUTING.md) to calibrate and submit a profile.

## Sharing profiles

The panel's **Export profile** downloads the lamp's calibration as a JSON
file. Make and model are filled in from the device registry (or the matched
model profile) and can be changed. The profile and the file are named
`<make>_<model>_<YYYY-MM-DD>_<random>.light-match.json`, and the file carries
the name, date, make and model. The name can be edited. The download goes
through a short-lived signed link, so it also works in the companion app and
in phone browsers. **Copy JSON** puts the same JSON on the clipboard (or shows
it to copy by hand where there is no clipboard).

**Import profile file**, **Paste JSON**, **Copy calibration from** and a
profile's **Apply to chosen lamps** in the profile list all apply to the lamps
ticked under **Apply to**: single lamps or whole groups. Each lamp gets the
profile converted for its own capabilities. **Merge** keeps the lamps' other
cells; without it their cells are replaced, after one confirmation. The panel
then lists the result per lamp. A profile without calibrated cells is refused
before any lamp is touched, and when replacing, a lamp none of whose cells fit
is left as it was. A profile made on a lamp that is
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

## My calibrations and groups

**Save to my library** keeps the current lamp's calibration in a local
library on your Home Assistant (`.storage/light_match.library`), under the
name in the export field. Imported files can be saved there too (tick "also
save imported profiles to my library"). Saving is on demand only; nothing is
saved automatically.

The **Profiles** card lists three separate sections, each filtered by make or
model:

- **My calibrations**: your library. Entries can be applied to lamps,
  exported, copied as JSON, renamed and deleted.
- **Built-in**: the calibrated profiles shipped with Light Match.
- **Community**: imported public lamp data (`profiles/imported/`) and profiles
  downloaded from the online database.

**Lamp groups** pick several lamps at once. Lamps of the same make and model
are grouped automatically from the device registry. These groups are worked
out live, so they follow new and removed lamps without upkeep. Groups of your
own are created from the ticked lamps, can be set to the ticked lamps again,
renamed or deleted, and are stored with the library.

## Services

| Service | What it does |
| --- | --- |
| `light_match.calibrate_cell` | Store a source command for a cell (`entity_id` of the twin, `cell`, `brightness` plus `rgb_color`/`rgbw_color`/`rgbww_color`/`hs_color`/`color_temp_kelvin`). |
| `light_match.reset_cell` | Forget a calibrated cell, or every cell with `cell: "*"`. |
| `light_match.export_profile` | Return the profile as a response (optional `model`). |
| `light_match.import_profile` | Load a profile (object or JSON text); `merge: true` keeps cells it does not have. |
| `light_match.import_brightness_curve` | Fill the uncalibrated cells from a brightness curve of target % to lamp %, for example a Lightener brightness map: `curve: {"10": 30, "50": 70}`. Colours keep their defaults. |

## Limits

- Calibration is by eye (or a rough phone camera) and only as good as the
  match you make. Camera locking works only in Chrome on Android, and is
  tested against simulated lamps, not a range of phones. Viewing
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
`pytest-homeassistant-custom-component`), runs ruff and the tests, checks
the panel's JavaScript with `node --check` and runs the panel tests in
`tests/js` with `node --test`.

## Licence

GPL-2.0-or-later. Copyright (C) 2026 Lou H. See [LICENSE](LICENSE).

The lamp profile data is CC BY 4.0; submitting a profile means you agree to
license it under CC BY 4.0 (see
[profiles/LICENSE](custom_components/light_match/profiles/LICENSE)).
Imported profiles also keep the MIT, BSD-2-Clause, BSD-3-Clause and
Apache-2.0 notices of the databases they come from, in
[profiles/imported/](custom_components/light_match/profiles/imported/NOTICE.md).
