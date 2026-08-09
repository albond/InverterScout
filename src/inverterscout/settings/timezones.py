"""IANA time zone choices shared by setup and runtime settings."""

from zoneinfo import available_timezones

# IANA identifiers are language-neutral, English-based names such as
# ``Europe/Berlin``. Keep them sorted so autocomplete results are predictable.
IANA_TIMEZONES: tuple[str, ...] = tuple(sorted(available_timezones() | {"UTC"}))
