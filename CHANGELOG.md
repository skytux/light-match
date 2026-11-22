# Changelog

## 0.7.4 - 2026-11-22

- Export and Share downloads now work in the companion app and phone browsers: the panel signs an authenticated download link (`/api/light_match/export/<entry>` and `/share/<entry>`, admins only) and follows it, as Home Assistant does for backups; the blob download stays as a fallback.
- The lamp model for an export is an inline field instead of a prompt.
- Copy JSON copies the exported profile to the clipboard, or shows it to copy by hand.
- Paste JSON imports a pasted profile.
- Copy calibration from another twin (websocket `light_match/profile/copy`), merging or replacing, with the same mode conversion as an import.

## 0.7.3 - 2026-11-22

- Whites with the colour LEDs, so they can go dimmer: a new per-lamp option
  (auto, default; always; never) for lamps with colour temperature and
  RGB/RGBW/RGBWW channels. Auto shows white cells below the brightness
  floor with the colour channels (floor brightness, channels scaled down:
  the W channel with red and green for warmth, the mix that matches the
  colour temperature), the lamp's own white above; levels blend and switch
  where they cross the floor. Always uses the colour LEDs for every white;
  never keeps colour temperature (whites outside the lamp's range still
  use the colour LEDs, as in 0.7.0).
- Calibration panel: a "Calibrate in colour mode" switch on white cells
  turns the sliders into channel sliders and saves the cell in that mode;
  it starts as the lamp option sets it, and whites shown with the colour
  LEDs carry a colour bar in the grid. A saved white keeps its mode.
- Inverse mapping recognises colour-LED whites at any brightness; export,
  import, model profiles and the profile schema record the mode of each
  such cell (`modes.cells`), and a lamp without colour LEDs converts them
  to its colour temperature.
- scripts/test.sh builds the test image only when it is missing or with
  REBUILD=1.

## 0.7.2 - 2026-11-22

- Calibration panel: a big circle shows the target colour and brightness
  of the current cell (CIE xy and relative output converted for an sRGB
  screen, screen white = full output); tap it for a full-screen patch to
  hold next to the lamp. Marked as a rough guide: screens are not
  calibrated, the reference lamp comes first.

## 0.7.1 - 2026-11-22

- CONTRIBUTING.md: how to calibrate against lm-ref-1, share, the profile
  checklist, nicknames, the CC BY 4.0 agreement, imported profiles, ratings.
- GitHub files (not run yet): workflows for the tests, hassfest + HACS
  validation, profile checks on pull requests, and a nightly profiles
  release (assets per revision + community stats committed to the index);
  the "New lamp profile" issue form with the fields the Share link fills,
  checked by a test.
- README: profile database and ranking, optional online update, privacy
  (nothing is sent unless the online update is on), ratings, Share profile,
  and the licences of the profile data.

## 0.7.0 - 2026-11-22

- Effects pass through: the twin shows the source's effect list and running
  effect; starting an effect sends the effect with a calibrated brightness
  only, and "off"/"none" or a colour command stops it. Unknown effects are
  ignored.
- Narrow colour-temperature ranges: on a lamp with colour LEDs, white rows
  more than 100 K outside its own ct range (for example 2200 K on a
  2700-6500 K bulb) are shown with the colour LEDs instead of being clamped,
  and are calibrated in the colour mode. Targets between such a row and a ct
  row use the lamp's white inside its range and a converted blend outside
  it. The inverse mapping recognises these whites, export/import keeps
  them, and an old clamped ct cell in such a row is ignored. The reference
  lamp and lamps without colour LEDs still clamp.

## 0.6.0 - 2026-11-22

- Imported lamp profiles: about 600 models in `profiles/imported/`, generated
  by `tools/import/` (all in containers) from the Powercalc profile library
  (MIT), zigbee-herdsman-converters (MIT), deCONZ device files
  (BSD-3-Clause), diyHue light types (Apache-2.0), pywizlight fixtures (MIT)
  and python-yeelight model specs (BSD-2-Clause). They are partly
  calibrated, imported and unverified: model ids and aliases, colour modes,
  kelvin range, lumens, Hue gamut (deCONZ wins over diyHue on
  disagreement), and from Powercalc's power tables an estimated output curve
  (standby subtracted, normalised, quality "power-proxy") and a brightness
  floor where the power stays flat at the bottom (quality "estimated"). No
  cells, no author emails. Each source's licence and copyright ship in
  `profiles/imported/licenses/` with a NOTICE; each profile lists its
  sources in `provenance.imports`.
- New method "imported" (ranked between spec and by eye), the
  `provenance.imports` list and `capabilities.output_curve`, in the runtime
  schema and `profile.schema.json`.
- Measured or verified community profiles now rank first, then the closest
  match; capabilities are merged across all matching profiles, best first,
  so an imported profile combines with a hand-written one (Hue gamut C
  subfloor, WiZ floor).
- What the lamp reports (kelvin range, colour modes, a gamut attribute)
  wins over spec and imported data; only measured or verified profiles
  override it.
- Default brightness follows the output curve when one is known (smart
  behaviour only); the panel names the import sources and licences, and the
  config flow lists only the imported profiles that fit the lamp.

## 0.5.0 - 2026-11-22

- Optional online update of the profile database, off by default: a
  checkbox in the panel (or `light_match.update_profiles`) fetches the
  profile index from GitHub with an ETag, downloads only profiles newer than
  the bundled ones from the "profiles" release assets, checks each against
  the index's sha256 and the schema, caches them, and checks once a day.
  Online profiles only fill the model layer; your cells are never touched.
  Turning it off drops the online layer and its cache. Nothing else is sent.
- Community stats: the index carries each profile's rating (thumbs up/down
  on its GitHub Discussion), downloads (release asset counts), verified
  status, contributor nicknames and last update, with
  `profiles/index.schema.json` and a runtime schema. The panel shows them
  next to the matched profile with a "Rate / report" link.
- Ranking among equally good matches: verified, then measured, then rating,
  then revision.
- `scripts/profile_stats.py`: release assets per revision and the stats
  refresh (Discussions are created per profile when missing), for a
  scheduled workflow.

## 0.4.0 - 2026-11-22

- Smart inference, so fewer cells need calibrating: an uncalibrated cell takes
  its colour from the same anchor's nearest calibrated level, and its
  brightness from the base (model profile or default) scaled by the
  lightness ratio your calibrated cells show at that level, interpolated
  between levels and held beyond the ends. Colour cells borrow the white
  correction when none of them is calibrated. Such cells are marked
  "inferred" (~ in the grid).
- Quick calibration: a 5-cell walk (white 2700 K at 1, 20 and 100 %, plus red
  and blue at 20 %; the cold and warm ends for colour-temperature lamps; the
  5 levels for dimmers), remembered in the browser.
- Copy from model profile: one cell or every model cell you have not
  calibrated (`light_match/cell/copy_model`).

## 0.3.0 - 2026-11-22

- Shared reference space `lm-ref-1` (`reference.py`, `docs/reference-space.md`):
  colour cells are the saturated sRGB colour of their hue as CIE xy, white
  cells the Planckian locus, and the brightness percentage is perceived
  lightness of the lamp's own maximum. Every target lies inside Hue gamut C,
  so a Hue gamut C colour lamp is the stand-in reference when calibrating by
  eye; such a reference lamp is sent `xy_color` targets. The panel shows each
  cell's target xy and relative output.
- Profiles matched against any other lamp are marked `house`: the personal
  "match my reference lamp" layer, which works at home but is not portable.
- Share profile in the panel: builds the profile in the database format with
  your own cells, the lamp's make, model and integration as the device
  registry reports them, what Home Assistant knows of its capabilities, the
  method (by eye, unverified), the CC BY 4.0 licence and, if you give one, your
  nickname (remembered in the browser, marked as public). It downloads the
  file and opens a prefilled "new lamp profile" GitHub issue, with the JSON in
  the link when it fits. No entity ids, names or addresses go into the file.
- Websocket commands `light_match/profile/share` and `light_match/reference`.

## 0.2.0 - 2026-11-22

- Lamp profile database by manufacturer and model:
  `profiles/<manufacturer>/<model>.json`, bundled with the integration, with
  a JSON schema (`profile.schema.json`), an index with checksums and
  `scripts/profiles.py` to check the schema, the rules, duplicate models and
  the index.
- Each profile names the device as Home Assistant's device registry reports
  it (manufacturer and aliases, model names or ids, integrations, firmware
  range), its capabilities, its provenance (spec, by eye, lux meter or
  colorimeter; sources; verified), contributors by nickname with a role and
  date, and the data licence CC BY 4.0 (`profiles/LICENSE`).
- Data-driven adapters from the profile: brightness floor, sub-floor method
  (channel scaling or none), kelvin range, colour modes, xy gamut clamping
  (Hue gamuts A, B, C or custom corners), channel gains and transition
  support. The twin only offers transitions when the lamp takes them.
- A new twin is matched to a profile by the source lamp's device
  (manufacturer, model, model id, integration, firmware); the config flow
  shows the lamp and the match and lets you pick another profile or none,
  also later in the options. Equal matches prefer verified, then measured
  profiles.
- Layering: built-in defaults < model profile cells < your own cells. The
  panel shows the matched profile and each cell's source.
- Seed profiles from public specs only, marked spec-based and unverified
  with their sources: WiZ (10 % floor) and Philips Hue gamuts A, B and C.
- Profile files are format version 2; version 1 exports still import.

## 0.1.1 - 2026-11-22

- README: install, set-up, calibration walkthrough, how it works, profile
  format, services, limits, and related projects / how this differs.

## 0.1.0 - 2026-11-22

- First version: calibrated twin lights with a colour x brightness calibration
  grid per lamp, perceptual interpolation, the sub-floor trick for RGB/RGBW
  lamps with a brightness floor, inverse mapping of the source state, config
  and options flow (behaviour, reference lamp, brightness floor, hiding the
  source), profile storage, export and import, services, a calibration panel
  in the sidebar and a Lightener-style brightness curve import.
