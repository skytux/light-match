# Changelog

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
