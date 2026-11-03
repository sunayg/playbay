"""Describes every editable setting: its type, limits and how it is validated.

Values cross the API in a "wire" form (plain JSON); `coerce` turns a wire
value into the Python type `settings.py` uses and `to_wire` goes back.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlparse

from hls_ladder import DOWNMIX_CHOICES

Kind = Literal[
    "bool", "int", "str", "url", "bitrate", "str_list", "int_list", "host_port_list", "regex_list", "choice"
]


MAX_LIST_ITEMS = 50
MAX_ITEM_LENGTH = 200


class ValidationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Field:
    key: str
    category: str
    kind: Kind
    label: str
    help: str = ""
    secret: bool = False
    restart_needed: bool = False
    nullable: bool = False
    min: int | None = None
    max: int | None = None
    non_empty: bool = False
    choices: tuple[tuple[str, str], ...] = ()  # (value, label) for a "choice"


CATEGORIES: list[tuple[str, str]] = [
    ("search", "Search"),
    ("torrents", "Torrents"),
    ("streaming", "Streaming"),
    ("conversion", "Encoding"),
    ("subtitles", "Subtitles and metadata"),
    ("trending", "Trending"),
    ("general", "General"),
]


def _f(key: str, category: str, kind: Kind, label: str, **kw: Any) -> tuple[str, Field]:
    return key, Field(key, category, kind, label, **kw)


FIELDS: dict[str, Field] = dict(
    [
        _f("JACKETT_HOST", "search", "url", "Jackett host", help="Changing it clears the Jackett API key unless you set a new one."),
        _f("JACKETT_API_KEY", "search", "str", "Jackett API key", secret=True),
        _f("PROWLARR_HOST", "search", "url", "Prowlarr host", help="Changing it clears the Prowlarr API key unless you set a new one."),
        _f("PROWLARR_API_KEY", "search", "str", "Prowlarr API key", secret=True),
        _f("MOVE_RESULTS_TO_BOTTOM_CONTAINING_STRINGS", "search", "str_list", "Move results containing to bottom", help="One per line."),
        _f("TORRENT_LISTENING_PORT", "torrents", "int", "Listening port", restart_needed=True, nullable=True, min=1, max=65535),
        _f("DHT_ROUTERS", "torrents", "host_port_list", "DHT routers", restart_needed=True, help="One host:port per line."),
        _f("MAX_TORRENT_AGE_HOURS", "torrents", "int", "Max torrent age (hours)", min=1),
        _f("TORRENT_DOWNLOAD_LIMIT", "torrents", "int", "Download limit (bytes/s, -1 = none)", help="Applies to torrents added after the change.", min=-1),
        _f("TORRENT_UPLOAD_LIMIT", "torrents", "int", "Upload limit (bytes/s, -1 = none)", help="Applies to torrents added after the change.", min=-1),
        _f("SUPPORTED_EXTENSIONS", "torrents", "str_list", "Supported extensions", help="One per line."),
        _f("MAX_OUTPUT_FILE_AGE", "torrents", "int", "Max output file age (hours)", min=1),
        _f("HLS_LADDER_HEIGHTS", "streaming", "int_list", "Quality levels (heights)", help="One height per line, highest first, for example 1080. The player switches between them as the connection allows. Never above the source.", min=144, max=4320, non_empty=True),
        _f("HLS_LIMIT_BY_SPEED", "streaming", "bool", "Limit quality levels to what this machine can encode", help="A short speed test runs at startup. If the machine could not encode every quality level faster than real time, the highest ones are left out so playback does not stall."),
        _f("HLS_SEGMENT_DURATION", "streaming", "int", "Segment duration (seconds)", min=1),
        _f("HLS_START_SECONDS", "streaming", "int", "Start buffer (seconds of video)", help="Playback of a file that is still downloading begins once this much of the video has arrived. It is worked out from the file's own length and size.", min=1, max=600),
        _f("CAST_ORIGIN", "streaming", "url", "Cast address", help="The address a Chromecast can reach RapidBay at, for example http://192.168.1.20:5050. Leave empty to use the address you opened the page at, which a TV outside your Tailscale network cannot reach."),
        _f("MAX_PARALLEL_HLS_STREAMS", "streaming", "int", "Max parallel HLS streams", min=1),
        _f("HLS_PREFETCH_BYTES", "streaming", "int", "Prefetch window (bytes)", min=1),
        _f("AAC_BITRATE", "conversion", "bitrate", "AAC bitrate", help="For example 256k."),
        _f("AAC_CHANNELS", "conversion", "int", "AAC channels", min=1, max=8, help="2 mixes surround tracks down to stereo (see Downmix); 6 keeps 5.1."),
        _f("AUDIO_DOWNMIX", "conversion", "choice", "Downmix", help="How a surround track is mixed down to stereo. The default mix leaves dialogue quiet; the others keep it up front.", choices=DOWNMIX_CHOICES),
        _f("AUDIO_DOWNMIX_BOOST", "conversion", "int", "Downmix volume (%)", help="Raise or lower a downmixed track. Different mixes come out at different volumes.", min=50, max=300),
        _f("VIDEO_EXTENSIONS", "conversion", "str_list", "Video extensions", help="One per line."),
        _f("SUBTITLE_LANGUAGES", "subtitles", "str_list", "Subtitle languages", help="One per line, for example en."),
        _f("OPENSUBTITLES_USERNAME", "subtitles", "str", "OpenSubtitles username"),
        _f("OPENSUBTITLES_PASSWORD", "subtitles", "str", "OpenSubtitles password", secret=True),
        _f("TMDB_API_KEY", "subtitles", "str", "TMDB API key", secret=True),
        _f("EXCLUDE_TRACKERS_FROM_TRENDING", "trending", "str_list", "Exclude trackers", help="One per line."),
        _f("EXCLUDE_CATEGORIES_FROM_TRENDING", "trending", "int_list", "Exclude categories", help="One number per line."),
        _f("TRENDING_TITLE_BLOCKLIST", "trending", "regex_list", "Title blocklist (regex)", help="One regular expression per line."),
        _f("TRENDING_BLOCK_NON_LATIN_TITLES", "trending", "bool", "Block non-Latin titles"),
        _f("AUTO_PLAY_NEXT_FILE", "general", "bool", "Auto-play next file"),
        _f("PASSWORD", "general", "str", "Password", secret=True, help="Leave empty for open access."),
    ]
)

_BITRATE_RE = re.compile(r"^\d+k?$")
_PASSWORD_RE = re.compile(r"^[!#-+\--:<-\[\]-~]{1,128}$")  # printable ASCII except space " , ; \


def _clean_list(field: Field, wire: Any) -> list[str]:
    if not isinstance(wire, list):
        raise ValidationError(f"{field.label}: expected a list")
    out: list[str] = []
    for item in cast(list[Any], wire):
        if not isinstance(item, str):
            raise ValidationError(f"{field.label}: items must be text")
        item = item.strip()
        if item and item not in out:
            out.append(item)
    if len(out) > MAX_LIST_ITEMS:
        raise ValidationError(f"{field.label}: at most {MAX_LIST_ITEMS} entries")
    for item in out:
        if len(item) > MAX_ITEM_LENGTH:
            raise ValidationError(f"{field.label}: entries are limited to {MAX_ITEM_LENGTH} characters")
    return out


def _int(field: Field, wire: Any) -> int | None:
    if wire is None and field.nullable:
        return None
    if isinstance(wire, bool) or not isinstance(wire, int):
        if isinstance(wire, str) and re.fullmatch(r"-?\d+", wire.strip()):
            wire = int(wire)
        else:
            raise ValidationError(f"{field.label}: expected a whole number")
    if field.min is not None and wire < field.min:
        raise ValidationError(f"{field.label}: must be at least {field.min}")
    if field.max is not None and wire > field.max:
        raise ValidationError(f"{field.label}: must be at most {field.max}")
    return wire


def coerce(field: Field, wire: Any) -> Any:
    kind = field.kind
    if kind == "bool":
        if not isinstance(wire, bool):
            raise ValidationError(f"{field.label}: expected on or off")
        return wire
    if kind == "int":
        return _int(field, wire)
    if kind == "choice":
        if wire not in {value for value, _ in field.choices}:
            raise ValidationError(f"{field.label}: choose one of the listed options")
        return wire
    if kind in ("str", "url", "bitrate"):
        if not isinstance(wire, str):
            raise ValidationError(f"{field.label}: expected text")
        value = wire.strip()
        if field.key == "PASSWORD":
            if value and not _PASSWORD_RE.match(wire):
                raise ValidationError("Password must be 1-128 printable ASCII characters without spaces, quotes, commas, semicolons or backslashes")
            return wire
        if kind == "url":
            if not value:
                return ""
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValidationError(f"{field.label}: must be an http or https URL")
            return value.rstrip("/")
        if kind == "bitrate" and not _BITRATE_RE.match(value):
            raise ValidationError(f"{field.label}: use digits with an optional k, for example 256k")
        return value
    items = _clean_list(field, wire)
    if kind == "str_list":
        return items
    if kind == "int_list":
        out: list[int] = []
        for item in items:
            if not re.fullmatch(r"-?\d+", item):
                raise ValidationError(f"{field.label}: '{item}' is not a whole number")
            out.append(int(item))
        for number in out:
            if field.min is not None and number < field.min:
                raise ValidationError(f"{field.label}: {number} is below {field.min}")
            if field.max is not None and number > field.max:
                raise ValidationError(f"{field.label}: {number} is above {field.max}")
        if field.non_empty and not out:
            raise ValidationError(f"{field.label}: at least one entry is required")
        return out
    if kind == "host_port_list":
        routers: list[tuple[str, int]] = []
        for item in items:
            host, _, port = item.rpartition(":")
            if not host or not port.isdigit() or not 1 <= int(port) <= 65535 or re.search(r"\s", host):
                raise ValidationError(f"{field.label}: '{item}' is not host:port")
            routers.append((host, int(port)))
        return routers
    for item in items:  # regex_list
        try:
            re.compile(item)
        except re.error as e:
            raise ValidationError(f"{field.label}: '{item}' is not a valid pattern ({e})") from e
    return items


def to_wire(field: Field, value: Any) -> Any:
    kind = field.kind
    if kind == "host_port_list":
        return [f"{h}:{p}" for h, p in value]
    if kind == "int_list":
        return [str(v) for v in value]
    if kind == "str_list" or kind == "regex_list":
        return list(value)
    return value
