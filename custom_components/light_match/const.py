"""Constants for Light Match."""

from __future__ import annotations

DOMAIN = "light_match"
STORAGE_VERSION = 1
PROFILE_FORMAT = "light_match_profile"
PROFILE_VERSION = 2

CONF_SOURCE = "source"
CONF_NAME = "name"
CONF_REFERENCE = "reference_entity"
CONF_BEHAVIOUR = "behaviour"
CONF_AUTO_FLOOR = "auto_floor"
CONF_FLOOR = "brightness_floor"
CONF_HIDE_SOURCE = "hide_source"
CONF_WHITE_COLOR = "white_color"

# Whether white cells of a lamp with colour temperature and colour LEDs are
# commanded with the colour LEDs: only below the lamp's brightness floor
# (where the colour LEDs can dim further), always, or never (only whites
# outside the lamp's colour temperature range).
WHITE_COLOR_AUTO = "auto"
WHITE_COLOR_ALWAYS = "always"
WHITE_COLOR_NEVER = "never"
WHITE_COLORS = [WHITE_COLOR_AUTO, WHITE_COLOR_ALWAYS, WHITE_COLOR_NEVER]

# How a twin translates: calibrated cells with smart defaults (sub-floor
# trick), calibrated cells with plain defaults, or "this lamp is the
# reference" (commands pass straight through, calibration is ignored).
BEHAVIOUR_SMART = "smart"
BEHAVIOUR_PLAIN = "plain"
BEHAVIOUR_REFERENCE = "reference"
BEHAVIOURS = [BEHAVIOUR_SMART, BEHAVIOUR_PLAIN, BEHAVIOUR_REFERENCE]

DEFAULT_OPTIONS = {
    "behaviour": BEHAVIOUR_SMART,
    "auto_floor": True,
    "brightness_floor": 0,
    "hide_source": False,
    "profile": "auto",
    "white_color": WHITE_COLOR_AUTO,
}

# The calibration grid. Hues are the anchor points on the colour circle that
# a full-saturation colour is calibrated at; whites are calibrated at these
# colour temperatures; every colour and white at these brightness levels (%).
COLOR_ANCHORS: dict[str, float] = {
    "red": 0.0,
    "orange": 30.0,
    "yellow": 60.0,
    "green": 120.0,
    "cyan": 180.0,
    "blue": 240.0,
    "purple": 275.0,
    "pink": 320.0,
}
WHITE_KELVINS: list[int] = [2200, 2700, 4000, 6500]
BRIGHTNESS_LEVELS: list[float] = [1.0, 5.0, 20.0, 50.0, 100.0]

# The calibrated (uniform) colour temperature range every twin exposes.
TWIN_MIN_KELVIN = WHITE_KELVINS[0]
TWIN_MAX_KELVIN = WHITE_KELVINS[-1]

CONF_PROFILE = "profile"
PROFILE_AUTO = "auto"
PROFILE_NONE = "none"

PANEL_URL = "light-match"
PANEL_TITLE = "Light Match"
PANEL_ICON = "mdi:lightbulb-group-outline"
PANEL_ELEMENT = "light-match-panel"
STATIC_URL = "/light_match_static"
