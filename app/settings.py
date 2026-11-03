import contextlib
import copy as _copy
import os
from typing import List, Tuple

# RAPIDBAY
PASSWORD: str | None = None
AUTO_PLAY_NEXT_FILE: bool = True
OPENAPI_HOST: str | None = None

# PATHS - temp values, final constants computed below
_data_dir: str = "./data"
_cache_dir: str = ""
_logfile: str = ""
_download_dir: str = ""
_filelist_dir: str = ""
_torrents_dir: str = ""
_output_dir: str = ""
_frontend_dir: str = "./frontend/dist"
_kodi_addon_dir: str = "./app/kodi.addon"

# JACKETT
JACKETT_HOST: str | None = None
JACKETT_API_KEY: str = ""

# PROWLARR
PROWLARR_HOST: str | None = None
PROWLARR_API_KEY: str = ""

# TORRENT
TORRENT_LISTENING_PORT: int | None = None
DHT_ROUTERS: List[Tuple[str, int]] = [
    ("router.utorrent.com", 6881),
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.bitcomet.com", 6881),
    ("dht.aelitis.com", 6881),
]
MAX_TORRENT_AGE_HOURS: int = 10
VIDEO_EXTENSIONS: List[str] = ["mp4", "mkv", "avi", "mpg", "mpeg"]
SUPPORTED_EXTENSIONS: List[str] = ["mp4", "mkv", "avi", "mpg", "mpeg", "mp3", "flac"]
TORRENT_DOWNLOAD_LIMIT: int = -1
TORRENT_UPLOAD_LIMIT: int = -1
EXCLUDE_TRACKERS_FROM_TRENDING: List[str] = []
EXCLUDE_CATEGORIES_FROM_TRENDING: List[int] = []
TRENDING_TITLE_BLOCKLIST: List[str] = []
TRENDING_BLOCK_NON_LATIN_TITLES: bool = False

MOVE_RESULTS_TO_BOTTOM_CONTAINING_STRINGS: List[str] = ["XviD"]

# SUBTITLES
SUBTITLE_LANGUAGES: List[str] = ["en"]  # list of languages, e.g. ["en", "de", "es"]

# CONVERSION
AAC_BITRATE: str = "256k"
AAC_CHANNELS: int = 2
AUDIO_DOWNMIX: str = "dialogue"  # how surround audio is mixed down to stereo: dialogue, default, dave750 or nightmode
AUDIO_DOWNMIX_BOOST: int = 100  # percent: volume of a downmixed track
MAX_OUTPUT_FILE_AGE: int = 10  # hours

OPENSUBTITLES_USERNAME: str | None = None
OPENSUBTITLES_PASSWORD: str | None = None
TMDB_API_KEY: str | None = None

# HLS STREAMING
# Enable early HLS playback while downloading. The contiguous head a stream
# needs is formed with libtorrent piece deadlines, scoped to the file being
# watched: the daemon deadline-rushes the head window while a viewer sits on
# the file's download screen, and the streamer keeps a rolling prefetch window
# rushed ahead of the feeder (HLS_PREFETCH_BYTES). Torrent-wide
# sequential_download is deliberately NOT used — unwatched downloads keep
# rarest-first piece picking.
HLS_LADDER_HEIGHTS: List[int] = [1080, 720, 480, 360]  # quality levels to encode, never above the source
HLS_ENCODER: str = "auto"  # auto, software or vaapi
HLS_HWDECODE: str = "auto"  # auto (decode on the GPU when it can) or off
HLS_LIMIT_BY_SPEED: bool = True  # encode fewer quality levels at once when this machine is too slow for all of them
HLS_X264_PRESET: str = "veryfast"  # software encoder speed/quality trade-off
HLS_SEGMENT_DURATION: int = 6  # seconds per HLS segment
HLS_START_SECONDS: int = 20  # seconds of video that must have downloaded, from the start, before playback begins
CAST_ORIGIN: str = ""  # address a Chromecast can reach RapidBay at, e.g. http://192.168.1.20:5050; empty = the address the page was opened at
MAX_PARALLEL_HLS_STREAMS: int = 2  # cap on concurrent encode sessions
HLS_PREFETCH_BYTES: int = 32 * 1024 * 1024  # rolling deadline-rushed window the streamer keeps ahead of the pipe feeder

# Built-in defaults and the values that came from the environment, kept so the
# settings page can tell where a value comes from and reset it.
_DEFAULTS = {_k: _copy.deepcopy(_v) for _k, _v in list(globals().items()) if _k.isupper() and not _k.startswith("_")}
_ENV_VALUES: dict[str, object] = {}

# Load environment variables
for _variable in [item for item in list(globals().keys()) if not item.startswith("_")]:
    _NULL = "NULL"
    _env_var = os.getenv(_variable, _NULL)
    if _env_var is not _NULL:
        with contextlib.suppress(Exception):
            _env_var = eval(_env_var)
        globals()[_variable] = _env_var
        _ENV_VALUES[_variable] = _env_var

# eval() above turns PASSWORD=1234 into an int that never matches the cookie.
if PASSWORD is not None:
    PASSWORD = str(PASSWORD)
    if "PASSWORD" in _ENV_VALUES:
        _ENV_VALUES["PASSWORD"] = PASSWORD

# Load path overrides from environment
_data_dir = os.getenv("DATA_DIR", _data_dir)
_cache_dir = os.getenv("CACHE_DIR", _cache_dir)
_logfile = os.getenv("LOGFILE", _logfile)
_download_dir = os.getenv("DOWNLOAD_DIR", _download_dir)
_filelist_dir = os.getenv("FILELIST_DIR", _filelist_dir)
_torrents_dir = os.getenv("TORRENTS_DIR", _torrents_dir)
_output_dir = os.getenv("OUTPUT_DIR", _output_dir)
_frontend_dir = os.getenv("FRONTEND_DIR", _frontend_dir)
_kodi_addon_dir = os.getenv("KODI_ADDON_DIR", _kodi_addon_dir)

# Compute final path constants (assigned once)
DATA_DIR: str = _data_dir
# Watch history is not disposable download data, so it can live outside DATA_DIR.
LIBRARY_PATH: str = os.getenv("LIBRARY_PATH", os.path.join(DATA_DIR, "library.json"))
CACHE_DIR: str = _cache_dir if _cache_dir else os.path.join(_data_dir, "cache")
LOGFILE: str = _logfile if _logfile else os.path.join(_data_dir, "rapidbay_errors.log")
DOWNLOAD_DIR: str = _download_dir if _download_dir else os.path.join(_data_dir, "downloads") + "/"
FILELIST_DIR: str = _filelist_dir if _filelist_dir else os.path.join(_data_dir, "filelists") + "/"
TORRENTS_DIR: str = _torrents_dir if _torrents_dir else os.path.join(_data_dir, "torrents") + "/"
OUTPUT_DIR: str = _output_dir if _output_dir else os.path.join(_data_dir, "output") + "/"
FRONTEND_DIR: str = _frontend_dir
KODI_ADDON_DIR: str = _kodi_addon_dir

# Saved values from the settings page win over the environment.
import sys as _sys  # noqa: E402

import settings_store as _settings_store  # noqa: E402

_settings_store.apply_saved(_sys.modules[__name__])
