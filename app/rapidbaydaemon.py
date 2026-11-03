import contextlib
import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import time
from threading import Event, Lock, Thread
from typing import Any, Callable, Dict, List, Set, Tuple

import hls_ladder
import hls_sessions
import http_cache
import log
import settings
import subtitles
import torrent
import video_conversion
import web_seed_proxy
from common import threaded
from subtitles import get_subtitle_language


def get_filepaths(magnet_hash: str) -> List[str] | None:
    filename = os.path.join(settings.FILELIST_DIR, magnet_hash)
    if os.path.exists(filename):
        with open(filename) as f:
            data = f.read().replace("\n", "")
            return json.loads(data)
    return None


def _subtitle_filenames(h: Any, filename: str) -> List[str]:
    files = torrent.get_torrent_info(h).files()
    filename_without_extension = os.path.splitext(filename)[0]
    subtitle_filenames: List[str] = []
    for f in files:
        basename = os.path.basename(f.path)
        basename_lower = basename.lower()
        if basename_lower.endswith(".srt") and basename_lower.startswith(
            filename_without_extension.lower()
        ):
            subtitle_filenames.append(basename)
    return subtitle_filenames


def _subtitle_indexes(h: Any, filename: str) -> List[int]:
    subtitle_filenames = _subtitle_filenames(h, filename)
    files = torrent.get_torrent_info(h).files()
    subtitle_set: List[str] = []
    subtitle_indexes: List[int] = []
    for i, f in enumerate(files):
        basename = os.path.basename(f.path).lower()
        if basename in subtitle_filenames and basename not in subtitle_set:
            subtitle_set.append(basename)
            subtitle_indexes.append(i)
    return subtitle_indexes


def _get_output_dir(magnet_hash: str) -> str:
    return os.path.join(settings.OUTPUT_DIR, magnet_hash)


def _get_output_filepath(magnet_hash: str, filepath: str) -> str:
    # The output is the download itself (linked, not converted), so it keeps its extension.
    output_extension = os.path.splitext(filepath)[1][1:]
    return (
        os.path.splitext(
            os.path.join(settings.OUTPUT_DIR, magnet_hash, os.path.basename(filepath))
        )[0]
        + f".{output_extension}"
    )


def _describe_audio(track: hls_ladder.AudioTrack) -> Tuple[str, str]:
    """(BCP-47 language, display name) of an audio track, from its tags."""
    code, name = video_conversion.resolve_language(track.language)
    label = track.title or name
    if label == "Unknown":
        label = f"Track {track.index + 1}"
    return code, label.replace('"', "")


def _link_or_copy(source: str, destination: str) -> None:
    """Put the downloaded file in the output folder without using a second copy of the disk space.

    A hard link makes the file reachable under both names and keeps its data when the download
    folder is cleaned up. Only across filesystems does it fall back to a real copy.
    """
    tmp = f"{destination}.{os.getpid()}.part"
    try:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        try:
            os.link(source, tmp)
        except OSError:
            shutil.copyfile(source, tmp)
        os.replace(tmp, destination)
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _remove_old_files_and_directories(dirname: str, max_age: int) -> None:
    try:
        subpaths = os.listdir(dirname)
    except FileNotFoundError:
        return
    for subpath in subpaths:
        path = os.path.join(dirname, subpath)
        try:
            modified = datetime.datetime.strptime(
                time.ctime(os.path.getmtime(path)),
                "%a %b %d %H:%M:%S %Y",
            )
        except FileNotFoundError:
            continue
        diff = datetime.datetime.now() - modified
        days, seconds = diff.days, diff.seconds
        hours = days * 24 + seconds // 3600
        if hours > max_age:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path):
                    os.remove(path)
            except FileNotFoundError:
                pass


def _torrent_is_stale(h: Any) -> bool:
    return (time.time() - h.status().added_time) > 3600 * settings.MAX_TORRENT_AGE_HOURS


# BitTorrent infohash: 40 hex (sha-1, v1), 32 base32 (v1), or 64 hex (sha-256, v2).
# Anchors to lowercase only — torrent.get_hash() lower-cases its return value, and
# this makes `..` / path separators / other shell-relevant chars structurally
# unrepresentable here, which is the property we rely on for path safety.
_VALID_MAGNET_HASH = re.compile(r"^[a-z0-9]{32}$|^[a-z0-9]{40}$|^[a-z0-9]{64}$")


def _is_valid_magnet_hash(s: str) -> bool:
    return bool(_VALID_MAGNET_HASH.fullmatch(s))


def _is_safe_subpath(parent: str, child: str) -> bool:
    """True iff `child` resolves to a path inside `parent` (no traversal)."""
    try:
        parent_abs = os.path.realpath(parent)
        child_abs = os.path.realpath(child)
    except (OSError, ValueError):
        return False
    return os.path.commonpath([parent_abs, child_abs]) == parent_abs


# Longest the embedded subtitles of one file are given to come out before it is made ready without the rest.
_SUBTITLE_EXTRACTION_SECONDS = 600

# The start of a file holds the container headers (tracks, codecs, duration): this much is read first.
_HEADER_BYTES = 4 * 1024 * 1024
# What to wait for when the file's length is not known yet or cannot be read.
_FALLBACK_START_BYTES = 50 * 1024 * 1024


def _start_threshold_bytes(file_size: int, duration: float | None, seconds: float) -> int:
    """Contiguous bytes from the start of a file that must be on disk before it can start playing.

    `seconds` of video, at this file's own average rate (size over duration). Never less than the
    container headers, and never more than a quarter of the file, so a small file is not locked out.
    """
    cap = max(file_size // 4, 1024 * 1024)
    head = min(_HEADER_BYTES, cap)
    wanted = int(file_size / duration * seconds) if duration and duration > 0 else _FALLBACK_START_BYTES
    return min(max(wanted, head), cap)


def _get_vtt_subtitles(output_dir: str, video_filename: str) -> List[str]:
    if not os.path.isdir(output_dir):
        return []
    # VTTs land in a per-magnet output_dir that's shared across every file in
    # the torrent, so a season pack accumulates subs for every episode. Anchor
    # on the video's stem + a separator so episode 1's master playlist doesn't
    # surface episode 2/3/...'s tracks. Both producers prefix with the stem:
    # ffmpeg-extracted "{stem}.{idx}_{lang}.vtt" and downloaded "{stem}_{lang}.vtt".
    stem = os.path.splitext(os.path.basename(video_filename))[0]
    return sorted(
        [
            f for f in os.listdir(output_dir)
            if f.endswith(".vtt")
            and "_" in os.path.splitext(f)[0]
            and (f.startswith(stem + ".") or f.startswith(stem + "_"))
        ],
        key=lambda fn: fn.split("_")[-1],
    )


class FileStatus:
    READY: str = "ready"
    WAITING_FOR_TORRENT: str = "waiting_for_torrent"
    WAITING_FOR_METADATA: str = "waiting_for_metadata"
    FILE_NOT_FOUND: str = "file_not_found"
    DOWNLOADING_SUBTITLES_FROM_TORRENT: str = "downloading_subtitles_from_torrent"
    DOWNLOADING_SUBTITLES: str = "downloading_subtitles"
    DOWNLOAD_FINISHED: str = "download_finished"
    DOWNLOADING: str = "downloading"
    READY_TO_COPY: str = "ready_to_copy"


class SubtitleDownloadStatus:
    DOWNLOADING: str = "downloading"
    FINISHED: str = "finished"


class RapidBayDaemon:
    def __init__(self) -> None:
        self.subtitle_downloads: Dict[str, str] = {}
        self.torrent_client: torrent.TorrentClient = torrent.TorrentClient(
            listening_port=settings.TORRENT_LISTENING_PORT,
            dht_routers=settings.DHT_ROUTERS,
            filelist_dir=settings.FILELIST_DIR,
            download_dir=settings.DOWNLOAD_DIR,
            torrents_dir=settings.TORRENTS_DIR,
        )
        self._playback_modes: Dict[str, str] = {}
        # Length of each file, read from its headers once enough of it has downloaded; and when the last try failed.
        self._durations: Dict[Tuple[str, str], float] = {}
        self._duration_tries: Dict[Tuple[str, str], float] = {}
        sessions_dir = os.path.join(settings.DATA_DIR, "hls_sessions")
        shutil.rmtree(sessions_dir, ignore_errors=True)  # whatever a previous run left behind
        self.sessions = hls_sessions.SessionManager(sessions_dir)
        self._subtitle_gate: Lock = Lock()
        self._early_subtitles: Set[Tuple[str, str]] = set()  # (hash, filename) looked up by name while downloading
        # Last deadline-rush of a file's head window, keyed by (magnet_hash, filename);
        # throttles _rush_hls_head.
        self._head_rushes: Dict[Tuple[str, str], float] = {}
        # Files being finished (subtitles extracted, then linked into the output folder) on their own threads.
        self._finishing: Set[Tuple[str, str]] = set()
        # Files handed to libtorrent as web seeds (BEP19) from a debrid cache
        # (Real-Debrid / TorBox). libtorrent stays the sole writer and fetches
        # the file from the web seed and/or peers transparently; tracked only so
        # get_file_status can report the source as HTTP.
        self._web_seeded: Dict[str, Set[str]] = {}
        self._stop_event: Event = Event()
        self.thread: Thread = Thread(target=self._loop_wrapper, args=())
        self.thread.daemon = True
        # Optional hook fired (from daemon/worker threads) whenever a discrete
        # state transition happens — filelist written, conversion started or
        # finished, file copied to output — so SSE subscribers can re-sample
        # immediately instead of waiting for their 1s fallback tick.
        self.on_state_change: Callable[[], None] | None = None
        self.torrent_client.on_state_change = self._notify_state_change

    def _notify_state_change(self) -> None:
        callback = self.on_state_change
        if callback:
            with contextlib.suppress(Exception):
                callback()

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.thread.join(timeout=5.0)

    def downloads(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        result: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for magnet_hash, h in self.torrent_client.torrents.items():
            result[magnet_hash] = {}
            files = torrent.get_torrent_info(h).files()
            file_priorities = h.file_priorities()
            for priority, f in zip(list(file_priorities), list(files), strict=False):
                filename = os.path.basename(f.path)
                if priority == 0:
                    continue
                result[magnet_hash][filename] = self.get_file_status(magnet_hash, filename, viewer=False)
        return result

    def activity(self) -> Dict[str, Any]:
        """The open streams, and every file that is downloading or kept, for the activity page."""
        downloads: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, str]] = set()
        for magnet_hash, files in self.downloads().items():
            for filename, status in files.items():
                if os.path.splitext(filename)[1][1:] not in settings.VIDEO_EXTENSIONS:
                    continue
                seen.add((magnet_hash, filename))
                if status.get("status") == FileStatus.READY:
                    continue  # listed below, from the output folder
                downloads.append({
                    "hash": magnet_hash, "filename": filename, "status": status.get("status"),
                    "progress": status.get("progress"), "peers": status.get("peers"),
                    "download_rate": status.get("download_rate"), "size": None, "age_seconds": None,
                })
        now = time.time()
        with contextlib.suppress(OSError):
            for magnet_hash in os.listdir(settings.OUTPUT_DIR):
                directory = _get_output_dir(magnet_hash)
                if not _is_valid_magnet_hash(magnet_hash) or not os.path.isdir(directory):
                    continue
                for name in os.listdir(directory):
                    if os.path.splitext(name)[1][1:] not in settings.VIDEO_EXTENSIONS:
                        continue
                    stat = os.stat(os.path.join(directory, name))
                    downloads.append({
                        "hash": magnet_hash, "filename": name, "status": FileStatus.READY, "progress": 1.0,
                        "peers": None, "download_rate": None, "size": stat.st_size,
                        "age_seconds": int(now - stat.st_mtime),
                    })
        disk: Dict[str, int] = {}
        with contextlib.suppress(OSError):
            usage = shutil.disk_usage(settings.DOWNLOAD_DIR)
            disk = {"total": usage.total, "free": usage.free}
        return {"streams": self.sessions.snapshot(), "downloads": downloads, "disk": disk}

    def remove_file(self, magnet_hash: str, filename: str) -> bool:
        """Delete a kept file with its subtitles, and its torrent's download once no kept file of it is left.
        Returns False when the arguments do not name a file here."""
        if not _is_valid_magnet_hash(magnet_hash) or filename != os.path.basename(filename) or not filename:
            return False
        output_dir = _get_output_dir(magnet_hash)
        output_path = _get_output_filepath(magnet_hash, filename)
        if not _is_safe_subpath(settings.OUTPUT_DIR, output_path) or not os.path.isfile(output_path):
            return False
        self.sessions.close_key(f"{magnet_hash}/{filename}")
        stem = os.path.splitext(filename)[0]
        for name in os.listdir(output_dir):
            if name == filename or name.startswith(stem + "."):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(output_dir, name))
        kept_videos = [n for n in os.listdir(output_dir) if os.path.splitext(n)[1][1:] in settings.VIDEO_EXTENSIONS]
        if not kept_videos:
            shutil.rmtree(output_dir, ignore_errors=True)
            with self.torrent_client.locks.lock(magnet_hash):
                h = self.torrent_client.torrents.get(magnet_hash)
                if h:
                    self._discard_torrent(magnet_hash, h)
                else:
                    shutil.rmtree(os.path.join(settings.DOWNLOAD_DIR, magnet_hash), ignore_errors=True)
        self._notify_state_change()
        return True

    def session_torrents(self) -> List[str]:
        return [h.name() for h in self.torrent_client.session.get_torrents()]

    @threaded
    @log.catch_and_log_exceptions
    def fetch_filelist_from_link(self, magnet_link: str) -> None:
        assert self.thread.is_alive()
        print(f"fetch_filelist_from_link: {magnet_link}", flush=True)
        self.torrent_client.fetch_filelist_from_link(magnet_link)

    @log.catch_and_log_exceptions
    def save_torrent_file(self, filepath: str) -> None:
        self.torrent_client.save_torrent_file(filepath)

    @threaded
    @log.catch_and_log_exceptions
    def download_file(self, magnet_link: str, filename: str, download_subtitles: bool = True) -> None:
        assert self.thread.is_alive()
        magnet_hash = torrent.get_hash(magnet_link)
        if self.get_file_status(magnet_hash, filename)["status"] == FileStatus.READY:
            return

        # Add to libtorrent first so the on-disk filelist is rewritten with
        # libtorrent's view of file paths before we resolve the debrid file
        # below: an RD-supplied filelist can list a file as `inner.mkv` while
        # libtorrent stores it at `<torrent_name>/inner.mkv` for multi-file
        # torrents, and the web-seed registration keys off libtorrent's path.
        self.torrent_client.download_file(magnet_link, filename)

        h = self.torrent_client.torrents.get(magnet_hash)
        h.set_download_limit(settings.TORRENT_DOWNLOAD_LIMIT)  # type: ignore
        h.set_upload_limit(settings.TORRENT_UPLOAD_LIMIT)  # type: ignore

        # Hand the debrid URL to libtorrent as a web seed (BEP19) instead of
        # running a parallel HTTP download. libtorrent stays the sole writer,
        # hash-verifies every piece, and falls back to peers on its own — so
        # there's no urlretrieve/libtorrent write race, no priority-0 dance, and
        # no manual failover. When more than one debrid provider has the file,
        # each is attached as its own web seed and libtorrent fans piece requests
        # out across them in parallel, recombining into the one verified file.
        # Both single- and multi-file torrents go through the
        # local reverse-proxy shim: it maps each in-torrent file path back to its
        # debrid URL and streams the bytes via tinyproxy (so the download takes
        # the same egress/IP as the debrid API calls). The shim's directory-form
        # base URL works for single-file torrents too — libtorrent just appends
        # the lone file name.
        if h is not None and filename not in self._web_seeded.get(magnet_hash, set()):
            cached_urls = http_cache.get_cached_urls(magnet_hash, filename)
            for provider, cached_url in cached_urls:
                web_seed_proxy.register(provider, magnet_hash, filename, cached_url)
                self.torrent_client.add_web_seed(
                    magnet_hash, web_seed_proxy.base_url(provider, magnet_hash)
                )
            if cached_urls:
                self._web_seeded.setdefault(magnet_hash, set()).add(filename)

        if download_subtitles:
            for subtitle_filename in _subtitle_filenames(
                self.torrent_client.torrents.get(magnet_hash), filename
            ):
                self.torrent_client.download_file(magnet_link, subtitle_filename)

        self._notify_state_change()

    def _discard_torrent(self, magnet_hash: str, h: Any) -> None:
        """Drop a torrent and everything downloaded for it."""
        # End any encode session first; otherwise its ffmpeg and feeder outlive
        # remove_files=True and wait forever for bytes that will never arrive.
        self.sessions.close_torrent(magnet_hash)
        for f in torrent.get_torrent_info(h).files():
            filepath = os.path.join(settings.DOWNLOAD_DIR, magnet_hash, f.path)
            self.subtitle_downloads.pop(filepath, None)
            self._head_rushes.pop((magnet_hash, os.path.basename(f.path)), None)
        self._web_seeded.pop(magnet_hash, None)
        web_seed_proxy.unregister(magnet_hash)
        self.torrent_client.remove_torrent(magnet_hash, remove_files=True)

    @threaded
    @log.catch_and_log_exceptions
    def cancel_download(self, magnet_hash: str, filename: str) -> None:
        """Stop downloading a file and free what it fetched.

        Runs on its own thread: while a magnet is still resolving metadata,
        download_file holds the torrent lock for up to 30s and a cancel has to
        wait for it. A file that has reached conversion or is already playable
        is left alone. Other files of the same torrent keep downloading; the
        torrent itself goes once none remain.
        """
        if not _is_valid_magnet_hash(magnet_hash):
            return
        with self.torrent_client.locks.lock(magnet_hash):
            h = self.torrent_client.torrents.get(magnet_hash)
            if not h:
                return
            if self.get_file_status(magnet_hash, filename, viewer=False)["status"] == FileStatus.READY:
                return
            index, _ = torrent.get_index_and_file_from_files(h, filename)
            if index is None:
                return
            priorities = list(h.file_priorities())
            priorities[index] = 0
            for subtitle_index in _subtitle_indexes(h, filename):
                priorities[subtitle_index] = 0
            torrent.prioritize_files(h, priorities)
            self.sessions.close_key(f"{magnet_hash}/{filename}")
            self._head_rushes.pop((magnet_hash, filename), None)
            self._web_seeded.get(magnet_hash, set()).discard(filename)
            if not any(priorities):
                self._discard_torrent(magnet_hash, h)
        self._notify_state_change()

    def get_file_status(self, magnet_hash: str, filename: str, viewer: bool = True) -> Dict[str, Any]:
        """Status of one file. `viewer` is False for the admin listing and internal checks, which
        must not start head rushes or offer playback."""
        assert self.thread.is_alive()
        filename_extension = os.path.splitext(filename)[1]
        is_video = filename_extension[1:] in settings.VIDEO_EXTENSIONS
        output_filepath = _get_output_filepath(magnet_hash, filename)

        # While the file downloads, offer playback once enough of it is on disk in a
        # container the encoder can read from a pipe (an MP4 only when faststart).
        extra: Dict[str, Any] = {}
        if is_video and viewer:
            can_stream, start_at = self._stream_readiness(magnet_hash, filename)
            if start_at is not None:
                extra['start_at'] = start_at  # where playback can begin, as a share of the file
            if can_stream:
                extra['can_stream'] = True
            else:
                # A viewer is looking at this file (only their status polls reach this
                # branch) but the playable prefix hasn't formed yet: deadline-rush the
                # head window so it does. File-scoped, instead of a torrent-wide
                # sequential download that would slow every file.
                self._rush_hls_head(magnet_hash, filename)

        # Check if MP4 output file exists (READY - primary output)
        if os.path.isfile(output_filepath):
            base_filename = os.path.basename(output_filepath)
            return {
                'status': FileStatus.READY,
                'filename': base_filename,
                # Anchor on stem + separator so that ffmpeg-emitted phantom
                # subtitle segments (named "{stem}{N}.vtt" without separator)
                # don't leak into the picker.
                'subtitles': _get_vtt_subtitles(os.path.dirname(output_filepath), base_filename),
                'supported': any(filename.endswith(ext) for ext in settings.SUPPORTED_EXTENSIONS),
                'playback': self._playback_mode(output_filepath),
                **extra,
            }

        # Torrent-based states
        h = self.torrent_client.torrents.get(magnet_hash)
        if not h:
            return {'status': FileStatus.WAITING_FOR_TORRENT, **extra}
        if not h.has_metadata():
            return {'status': FileStatus.WAITING_FOR_METADATA, **extra}
        files = list(h.get_torrent_info().files())
        i, f = torrent.get_index_and_file_from_files(h, filename)
        if f is None or i is None:
            return {'status': FileStatus.FILE_NOT_FOUND, **extra}
        download_progress = h.file_progress()[i] / f.size

        if download_progress == 1:
            if filename_extension[1:] in settings.VIDEO_EXTENSIONS:
                all_torrent_subtitles_downloaded = all(
                    h.file_progress()[i] == files[i].size
                    for i in _subtitle_indexes(h, filename)
                )
                if not all_torrent_subtitles_downloaded:
                    return {'status': FileStatus.DOWNLOADING_SUBTITLES_FROM_TORRENT, **extra}
                filepath = os.path.join(settings.DOWNLOAD_DIR, magnet_hash, f.path)
                subtitle_download_status = self.subtitle_downloads.get(filepath)
                if subtitle_download_status == SubtitleDownloadStatus.DOWNLOADING:
                    return {'status': FileStatus.DOWNLOADING_SUBTITLES, **extra}
                if subtitle_download_status == SubtitleDownloadStatus.FINISHED:
                    return {'status': FileStatus.READY_TO_COPY, **extra}
            else:
                return {'status': FileStatus.READY_TO_COPY, **extra}
            return {'status': FileStatus.DOWNLOAD_FINISHED, **extra}
        torrent_status = h.status()
        download_rate = torrent_status.download_rate
        is_http = filename in self._web_seeded.get(magnet_hash, set())
        return {
            'status': FileStatus.DOWNLOADING,
            'progress': download_progress,
            'peers': None if is_http else torrent_status.num_peers,
            'source': 'http' if is_http else 'torrent',
            'download_rate': download_rate,
            **extra,
        }

    def _start_threshold(self, magnet_hash: str, filename: str, filepath: str, file_size: int, available: int) -> int:
        """Bytes needed before playback can start, for this file: HLS_START_SECONDS of its own video."""
        key = (magnet_hash, filename)
        duration = self._durations.get(key)
        if duration is None and available >= min(_HEADER_BYTES, max(file_size // 4, 1024 * 1024)):
            now = time.monotonic()
            if now - self._duration_tries.get(key, -10.0) >= 5.0:
                self._duration_tries[key] = now
                duration = hls_sessions.probe_duration(filepath)
                if duration is not None:
                    self._durations[key] = duration
        return _start_threshold_bytes(file_size, duration, settings.HLS_START_SECONDS)

    def _stream_readiness(self, magnet_hash: str, filename: str) -> tuple[bool, float | None]:
        """For a file that is still downloading: whether playback can start now, and how far into the
        file (0 to 1) it has to have downloaded before it can. The container must be readable in order
        (an MP4 only when its index comes first)."""
        h = self.torrent_client.torrents.get(magnet_hash)
        if not h or not h.has_metadata():
            return False, None
        i, f = torrent.get_index_and_file_from_files(h, filename)
        if i is None or f is None or f.size <= 0:
            return False, None
        available = self._get_available_bytes(magnet_hash, filename)
        filepath = os.path.join(settings.DOWNLOAD_DIR, magnet_hash, f.path)
        if _is_safe_subpath(settings.DOWNLOAD_DIR, filepath):
            self._start_early_subtitles(magnet_hash, filename, filepath)
        needed = self._start_threshold(magnet_hash, filename, filepath, f.size, available)
        fraction = round(needed / f.size, 4)
        if available < needed:
            return False, fraction
        return video_conversion.is_pipe_streamable(filepath, available), fraction

    def create_session(self, magnet_hash: str, filename: str, start_seconds: float = 0.0, viewer: str = "") -> Dict[str, Any]:
        """Open (or rejoin) the viewer's seekable encode session for a file.

        A finished file is read from the output folder. One that is still downloading is read
        as far as it has arrived, so the viewer can seek anywhere in what is already there.
        Returns {"session": id} or {"session": None, "reason": ...}.
        """
        if not _is_valid_magnet_hash(magnet_hash):
            return {"session": None, "reason": "invalid_path"}
        output_filepath = _get_output_filepath(magnet_hash, filename)
        if not _is_safe_subpath(settings.OUTPUT_DIR, output_filepath):
            return {"session": None, "reason": "invalid_path"}
        growing: hls_sessions.GrowingSource | None = None
        if os.path.isfile(output_filepath):
            source = output_filepath
        else:
            found = self._growing_source(magnet_hash, filename, output_filepath)
            if isinstance(found, str):
                return {"session": None, "reason": found}
            source, growing = found
        try:
            session = self.sessions.create(f"{magnet_hash}/{filename}", source, start_seconds, growing, viewer)
        except hls_sessions.SessionError as e:
            return {"session": None, "reason": e.reason}
        return {"session": session.id}

    def _growing_source(
        self, magnet_hash: str, filename: str, output_filepath: str
    ) -> tuple[str, hls_sessions.GrowingSource] | str:
        """The download as an encode source, or the reason it cannot be one yet."""
        h = self.torrent_client.torrents.get(magnet_hash)
        if not h or not h.has_metadata():
            return "no_torrent"
        i, f = torrent.get_index_and_file_from_files(h, filename)
        if i is None or f is None:
            return "file_not_found"
        filepath = os.path.join(settings.DOWNLOAD_DIR, magnet_hash, f.path)
        if not _is_safe_subpath(settings.DOWNLOAD_DIR, filepath):
            return "invalid_path"
        available = self._get_available_bytes(magnet_hash, filename)
        if available < self._start_threshold(magnet_hash, filename, filepath, f.size, available):
            return "not_ready"
        if not video_conversion.is_pipe_streamable(filepath, available):
            return "unsupported_format"
        client = self.torrent_client

        def request_window(offset: int) -> None:
            client.prioritize_byte_range(magnet_hash, filename, offset, offset + settings.HLS_PREFETCH_BYTES)

        growing = hls_sessions.GrowingSource(
            total_size=f.size,
            available=lambda: self._get_available_bytes(magnet_hash, filename),
            request_window=request_window,
            final_path=output_filepath,
        )
        return filepath, growing

    def session_master_playlist(
        self, session_id: str, magnet_hash: str, filename: str, subtitles: bool = True, for_cast: bool = False
    ) -> str | None:
        """Master playlist of a session: every quality level, plus the file's subtitles. For a Chromecast, only
        the levels it can decode."""
        session = self.sessions.get(session_id)
        if session is None:
            return None
        output_dir = _get_output_dir(magnet_hash)
        vtts = _get_vtt_subtitles(output_dir, os.path.basename(_get_output_filepath(magnet_hash, filename))) if subtitles else []
        lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
        lines += video_conversion.subtitle_media_lines(output_dir, vtts, uri_base=f"/play/{magnet_hash}/")
        if session.separate_audio:
            lines += hls_ladder.audio_media_lines(
                session.audio_tracks, len(session.ladder), "playlist", _describe_audio
            )
        lines += hls_ladder.stream_inf_lines(
            session.ladder,
            "playlist",
            session.has_audio,
            bool(vtts),
            audio_group=session.separate_audio,
            max_width=hls_ladder.CHROMECAST_MAX_WIDTH if for_cast else None,
        )
        return "\n".join(lines) + "\n"

    def _finish_file(self, magnet_hash: str, filename: str, filepath: str, output_filepath: str) -> None:
        """Extract a downloaded file's embedded subtitles and link it into the output folder (which makes it
        ready), on a thread of its own: extraction reads the whole file, which can take minutes, and the
        heartbeat must keep looking after every other torrent meanwhile."""
        key = (magnet_hash, filename)
        if key in self._finishing:
            return
        self._finishing.add(key)
        threading.Thread(
            target=self._extract_and_link, args=(key, filepath, output_filepath), daemon=True
        ).start()

    @log.catch_and_log_exceptions
    def _extract_and_link(self, key: Tuple[str, str], filepath: str, output_filepath: str) -> None:
        try:
            output_dir = os.path.dirname(output_filepath)
            os.makedirs(output_dir, exist_ok=True)
            # Embedded text subtitles become VTT files before the file counts as ready.
            try:
                extraction = video_conversion.extract_subtitles_as_vtt(filepath, output_dir)
                try:
                    extraction.wait(timeout=_SUBTITLE_EXTRACTION_SECONDS)
                except subprocess.TimeoutExpired:
                    extraction.kill()  # not left running on, writing subtitles after the file is playable
                video_conversion.name_untagged_subtitles(
                    output_dir, os.path.splitext(os.path.basename(filepath))[0]
                )
            except Exception as e:
                log.debug(f"Subtitle extraction failed for {filepath}: {e}")
            _link_or_copy(filepath, output_filepath)
            self._notify_state_change()
        finally:
            self._finishing.discard(key)

    def _playback_mode(self, output_filepath: str) -> str:
        """"direct" when the browser can play the file as it is, "session" when it needs an encode session."""
        mode = self._playback_modes.get(output_filepath)
        if mode is None:
            mode = "direct" if video_conversion.is_direct_playable(output_filepath) else "session"
            self._playback_modes[output_filepath] = mode
        return mode

    def _get_available_bytes(self, magnet_hash: str, filename: str) -> int:
        """Contiguous bytes available from the start of the file for pipe
        feeding — the count of verified contiguous pieces from file start."""
        return self.torrent_client.get_sequential_bytes(magnet_hash, filename)

    def _rush_hls_head(self, magnet_hash: str, filename: str) -> None:
        """Deadline-rush the head window of a file a viewer is waiting on so a
        contiguous, streamable prefix forms under rarest-first piece picking.
        Deadlines persist on the handle; the 30s re-rush is a cheap safety net
        for deadlines libtorrent dropped (e.g. after piece timeouts)."""
        key = (magnet_hash, filename)
        now = time.monotonic()
        if now - self._head_rushes.get(key, 0.0) < 30:
            return
        h = self.torrent_client.torrents.get(magnet_hash)
        if not h or not h.has_metadata():
            return
        i, f = torrent.get_index_and_file_from_files(h, filename)
        if i is None or f is None:
            return
        self._head_rushes[key] = now
        self.torrent_client.prioritize_byte_range(
            magnet_hash, filename, 0, _start_threshold_bytes(f.size, self._durations.get(key), settings.HLS_START_SECONDS)
        )

    def _download_external_subtitles(self, filepath: str, output_dir: str, skip: List[str] | None = None) -> None:
        # Test-and-set the dedup marker synchronously so racing callers (heartbeats
        # spawning while a prior download is in flight) can't both pass the check
        # and trigger duplicate OpenSubtitles fetches.
        with self._subtitle_gate:
            if self.subtitle_downloads.get(filepath):
                return
            self.subtitle_downloads[filepath] = SubtitleDownloadStatus.DOWNLOADING
        self._run_subtitle_download(filepath, output_dir, skip or [])

    def _start_early_subtitles(self, magnet_hash: str, filename: str, filepath: str) -> None:
        """Someone is opening a file that is still downloading: look its subtitles up by name now, so they are
        there when playback starts instead of only after the download."""
        key = (magnet_hash, filename)
        with self._subtitle_gate:
            if key in self._early_subtitles or not settings.SUBTITLE_LANGUAGES:
                return
            self._early_subtitles.add(key)
        self._run_early_subtitles(filepath, _get_output_dir(magnet_hash))

    @threaded
    @log.catch_and_log_exceptions
    def _run_early_subtitles(self, filepath: str, output_dir: str) -> None:
        os.makedirs(output_dir, exist_ok=True)
        subtitles.download_all_subtitles(filepath, early=True)
        self._emit_vtts_for_hls(filepath, output_dir)

    @threaded
    @log.catch_and_log_exceptions
    def _run_subtitle_download(self, filepath: str, output_dir: str, skip: List[str]) -> None:
        try:
            subtitles.download_all_subtitles(filepath, skip=skip)
            self._emit_vtts_for_hls(filepath, output_dir)
        finally:
            # Always mark FINISHED, even when the fetch or SRT→VTT step threw:
            # nothing retries a wedged DOWNLOADING marker, so the file would
            # otherwise sit in DOWNLOADING_SUBTITLES until the torrent ages out.
            self.subtitle_downloads[filepath] = SubtitleDownloadStatus.FINISHED
            self._notify_state_change()

    @staticmethod
    def _emit_vtts_for_hls(filepath: str, output_dir: str) -> None:
        # Copy downloaded .srt files to output dir as .vtt for HLS playback
        dirname = os.path.dirname(filepath)
        basename = os.path.basename(filepath)
        basename_without_ext = os.path.splitext(basename)[0]
        for srt_file in os.listdir(dirname):
            if srt_file.endswith(".srt") and srt_file.startswith(basename_without_ext):
                srt_path = os.path.join(dirname, srt_file)
                srt_stem = os.path.splitext(srt_file)[0]
                # Strip both . and _ separators so "Movie_en.srt" doesn't end up
                # as "Movie__en.vtt" (subtitles.download_all_subtitles uses both
                # styles depending on provider).
                lang = srt_stem[len(basename_without_ext):].lstrip("._")
                # Always emit a "_<lang>" suffix — the frontend's label parser
                # takes everything after the last "_" as the language; without
                # one, scene releases that ship a bare "{stem}.srt" end up
                # labeled with the full filename in the subtitle picker.
                # Default to "en" because most scene releases that ship a single
                # language-less SRT are English.
                vtt_name = f"{basename_without_ext}_{lang or 'en'}.vtt"
                vtt_path = os.path.join(output_dir, vtt_name)
                # Made again when the SRT is newer: the hash-matched, timed subtitles of a finished download
                # replace the name-matched ones fetched while it was still downloading.
                if not os.path.isfile(vtt_path) or os.path.getmtime(srt_path) > os.path.getmtime(vtt_path):
                    # Bound runtime so a malformed SRT can't wedge the
                    # subtitle-download thread indefinitely.
                    try:
                        subprocess.run(
                            ["ffmpeg", "-nostdin", "-y", "-v", "quiet", "-i", srt_path, vtt_path],
                            check=False,
                            timeout=60,
                        )
                    # OSError covers a missing/broken ffmpeg binary
                    # (FileNotFoundError), which TimeoutExpired alone lets
                    # escape and kill the remaining conversions.
                    except (subprocess.TimeoutExpired, OSError):
                        log.debug(f"ffmpeg SRT→VTT failed for {srt_path}")
                        with contextlib.suppress(OSError):
                            os.remove(vtt_path)

    def _handle_torrent(self, magnet_hash: str) -> None:
        h = self.torrent_client.torrents.get(magnet_hash)
        if not h:
            return
        file_priorities = h.file_priorities()
        files = [
            f
            for priority, f in zip(file_priorities, torrent.get_torrent_info(h).files(), strict=False)
            if priority != 0
        ]
        filenames = [os.path.basename(f.path) for f in files]
        video_filenames = [
            fn
            for fn in filenames
            if any(fn.endswith(ext) for ext in settings.SUPPORTED_EXTENSIONS)
        ]

        def is_state(filename: str, state: str) -> bool:
            return self.get_file_status(magnet_hash, filename)["status"] == state

        active_filenames = video_filenames if video_filenames else filenames

        if all(is_state(filename, FileStatus.READY) for filename in active_filenames):
            # The finished files live on in the output folder (hard links), so the download and
            # its torrent can go. Encode sessions read the output files once their source is gone.
            self.torrent_client.remove_torrent(magnet_hash, remove_files=True)
            self._web_seeded.pop(magnet_hash, None)
            web_seed_proxy.unregister(magnet_hash)
            for f in files:
                filepath = os.path.join(settings.DOWNLOAD_DIR, magnet_hash, f.path)
                self.subtitle_downloads.pop(filepath, None)
                self._head_rushes.pop((magnet_hash, os.path.basename(f.path)), None)
            return

        output_dir = _get_output_dir(magnet_hash)

        for f in files:
            filename = os.path.basename(f.path)
            filepath = os.path.join(settings.DOWNLOAD_DIR, magnet_hash, f.path)
            output_filepath = _get_output_filepath(magnet_hash, filepath)

            # MP4 conversion pipeline
            if is_state(filename, FileStatus.DOWNLOAD_FINISHED):
                if not os.path.isfile(filepath):
                    log.debug(f"File not found, skipping: {filepath}")
                    continue
                subtitle_filenames = _subtitle_filenames(h, filepath)
                available_subtitle_languages = [lang for lang in [get_subtitle_language(fn) for fn in subtitle_filenames] if lang]
                embedded_subtitle_languages = [
                    lang for (_, lang) in video_conversion.get_sub_tracks(filepath) if lang != video_conversion.UNTAGGED
                ]
                self._download_external_subtitles(filepath, output_dir, skip=available_subtitle_languages + embedded_subtitle_languages)
            elif is_state(filename, FileStatus.READY_TO_COPY):
                if not os.path.isfile(filepath):
                    log.debug(f"File not found for copy, skipping: {filepath}")
                    continue
                self._finish_file(magnet_hash, filename, filepath, output_filepath)

    @log.catch_and_log_exceptions
    def _heartbeat(self) -> None:
        # Process libtorrent session alerts for better monitoring
        self.torrent_client.process_alerts()

        for magnet_hash in list(self.torrent_client.torrents.keys()):
            h = self.torrent_client.torrents.get(magnet_hash)
            if not h:
                continue
            try:
                if _torrent_is_stale(h):
                    self._discard_torrent(magnet_hash, h)
                elif h.has_metadata():
                    with self.torrent_client.locks.lock(magnet_hash):
                        self._handle_torrent(magnet_hash)
            except Exception as e:
                raise e
        _remove_old_files_and_directories(
            settings.OUTPUT_DIR, settings.MAX_OUTPUT_FILE_AGE
        )
        _remove_old_files_and_directories(
            settings.FILELIST_DIR, settings.MAX_OUTPUT_FILE_AGE
        )
        _remove_old_files_and_directories(
            settings.DOWNLOAD_DIR, settings.MAX_OUTPUT_FILE_AGE
        )
        _remove_old_files_and_directories(
            settings.TORRENTS_DIR, settings.MAX_OUTPUT_FILE_AGE
        )

    def _loop_wrapper(self) -> None:
        try:
            self._loop()
        except Exception as e:
            import traceback
            print(f"FATAL: Daemon thread crashed with exception: {e}", flush=True)
            print("Stack trace:", flush=True)
            traceback.print_exc()
            os._exit(1)
        if not self._stop_event.is_set():
            print("FATAL: Daemon thread exited unexpectedly", flush=True)
            os._exit(1)

    def _loop(self) -> None:
        while not self._stop_event.wait(timeout=1):
            self._heartbeat()
