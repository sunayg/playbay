"""Seekable on-demand HLS for finished files, in the style of Plex and Jellyfin.

A session lists every segment of the file up front (so the player sees the whole timeline and can
seek anywhere), but encodes only what is asked for. A request for a segment that is not there yet
starts an encode at that point; the encoder then runs a few segments ahead of the viewer, is paused
when it gets too far ahead, and segments well behind the viewer are deleted. Disk use stays at a few
minutes of video instead of the whole ladder.
"""

import contextlib
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from subprocess import DEVNULL, PIPE, Popen
from typing import Any, Callable, Dict, List

import fmp4
import hls_diagnostics
import hls_ladder
import log
import settings
import video_conversion

# Segments the encoder may run past the farthest one asked for before a request restarts it elsewhere.
RESTART_GAP = 4
# Pause the encoder once it is this many segments ahead of the viewer; resume when it is within RESUME_AT.
PAUSE_AHEAD = 30
RESUME_AT = 20
# Keep this many segments behind the viewer, so short rewinds do not trigger a re-encode.
KEEP_BEHIND = 20
# A session nobody has requested anything from for this long is closed.
IDLE_SECONDS = 600
# How long a request waits for an encode to produce its segment.
SEGMENT_WAIT_SECONDS = 45
POLL_SECONDS = 0.1
PIPE_CHUNK = 256 * 1024
# An encoder that dies on its own is restarted this many times in a row before the session is given up on.
MAX_ENCODER_FAILURES = 2
# An encoder that is running, has work to do and has produced nothing new for this long is stuck: restart it.
STALL_SECONDS = 45
# The encoder's speed is read from this many of the latest once-a-second samples.
RATE_WINDOW_SAMPLES = 30

_SEGMENT_RE = re.compile(r"^seg_v(\d+)_(\d{4,})\.m4s$")
_INIT_RE = re.compile(r"^init_v(\d+)\.mp4$")


@dataclass
class GrowingSource:
    """A file that is still downloading. The encoder is fed the part that has arrived, in order."""

    total_size: int
    available: Callable[[], int]  # contiguous bytes from the start of the file
    # Asks the torrent to fetch the bytes from this offset on first, so they arrive in time.
    request_window: Callable[[int], None] | None = None
    # Where the finished file will be (a link to the download): the download itself is removed
    # once the whole torrent is done, but the data lives on under this name.
    final_path: str | None = None

    def complete(self) -> bool:
        # The link exists only once the whole file has downloaded, and outlives the torrent: after the
        # torrent is removed, `available` reads 0 and says nothing about the file.
        if self.final_path and os.path.exists(self.final_path):
            return True
        return self.available() >= self.total_size


class SessionError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Session:
    id: str
    key: str
    source: str
    directory: str
    duration: float
    segment_seconds: int
    ladder: List[hls_ladder.Rendition]
    src_height: int
    has_audio: bool
    audio_copy: bool  # the audio can go into the stream as it is
    encoder: str
    hdr: str = "sdr"  # see hls_ladder.probe_dynamic_range
    # Who the session is for ("" when not named). Each viewer of a file has an encoder of their own: one encoder
    # can only be at one place in the file, and two viewers would keep moving it away from each other.
    viewer: str = ""
    audio_tracks: List[hls_ladder.AudioTrack] = field(default_factory=list)
    growing: GrowingSource | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    proc: "Popen[bytes] | None" = None
    start_segment: int = 0
    runs: int = 0
    paused: bool = False
    requested: int = 0
    # Segments below this were deleted behind the viewer; the run is counted from here, not from its first segment.
    floor: int = 0
    # The segment of the newest request. Only requests near it may move the encoder, so requests
    # left over from an earlier playback position cannot pull it back.
    latest: int = 0
    last_request: float = field(default_factory=time.monotonic)
    # Consecutive times the encoder died on its own; past MAX_ENCODER_FAILURES the session is given up on.
    failures: int = 0
    dead: bool = False
    closed: bool = False
    # Track id -> timescale for each variant, read from its init file.
    timescales: Dict[int, Dict[int, int]] = field(default_factory=dict)
    # How far the current run had got when last looked at, and when that last changed (the stall watchdog).
    progress_mark: int = -1
    progress_at: float = field(default_factory=time.monotonic)
    # (time, segments through) of the running encoder while it was not paused, for its speed.
    rate_samples: List["tuple[float, int]"] = field(default_factory=list)

    @property
    def separate_audio(self) -> bool:
        """With several audio tracks each is its own variant, after the video ones."""
        return self.has_audio and len(self.audio_tracks) > 1

    @property
    def variant_count(self) -> int:
        return len(self.ladder) + (len(self.audio_tracks) if self.separate_audio else 0)

    @property
    def segment_count(self) -> int:
        return max(1, math.ceil(self.duration / self.segment_seconds))


def segment_name(level: int, index: int) -> str:
    return f"seg_v{level}_{index:04d}.m4s"


def variant_playlist(session: Session, level: int) -> str:
    """The whole file as a finished playlist: the player can seek anywhere in it."""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{session.segment_seconds}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f'#EXT-X-MAP:URI="init_v{level}.mp4"',
    ]
    remaining = session.duration
    for index in range(session.segment_count):
        length = min(float(session.segment_seconds), remaining)
        remaining -= length
        lines += [f"#EXTINF:{length:.6f},", segment_name(level, index)]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def parse_segment_request(name: str) -> tuple[str, int, int] | None:
    """("init"|"seg", level, index) for a file name the session serves, else None."""
    match = _INIT_RE.match(name)
    if match:
        return ("init", int(match.group(1)), 0)
    match = _SEGMENT_RE.match(name)
    if match:
        return ("seg", int(match.group(1)), int(match.group(2)))
    return None


def produced_indexes(directory: str, level: int = 0) -> List[int]:
    """Indexes of the finished segments of one rendition currently on disk."""
    found: List[int] = []
    with contextlib.suppress(OSError):
        for name in os.listdir(directory):
            match = _SEGMENT_RE.match(name)
            if match and int(match.group(1)) == level:
                found.append(int(match.group(2)))
    return sorted(found)


def _adopt_later_init(directory: str, level: int, path: str) -> bool:
    """Copy a restarted run's init file over the first run's, which was cut short and stayed empty."""
    pattern = re.compile(rf"^rinit\d+_v{level}\.mp4$")
    with contextlib.suppress(OSError):
        for name in sorted(os.listdir(directory)):
            source = os.path.join(directory, name)
            if pattern.match(name) and os.path.getsize(source) > 0:
                shutil.copyfile(source, path + ".tmp")
                os.replace(path + ".tmp", path)
                return True
    return False


def _read_timescales(directory: str, level: int) -> Dict[int, int]:
    """A variant's track timescales, from any run's init file: every run writes the same one."""
    pattern = re.compile(rf"^(r?init\d*)_v{level}\.mp4$")
    with contextlib.suppress(OSError):
        for name in sorted(os.listdir(directory)):
            if pattern.match(name):
                with contextlib.suppress(OSError, struct.error, IndexError), open(os.path.join(directory, name), "rb") as f:
                    timescales = fmp4.track_timescales(f.read())
                    if timescales:
                        return timescales
    return {}


def encoded_through(indexes: List[int], start: int) -> int:
    """Last segment of the unbroken run that begins at `start` (start - 1 when none is there yet).

    Segments left by an earlier run, elsewhere in the file, do not count as this run's progress.
    """
    present = set(indexes)
    last = start - 1
    while last + 1 in present:
        last += 1
    return last


def probe_duration(source: str) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", source],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout.strip()
        value = float(out)
    except Exception:
        return None
    return value if value > 0 else None


class SessionManager:
    def __init__(self, root: str) -> None:
        self.root = root
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        self._create_lock = threading.Lock()
        self._watcher: threading.Thread | None = None
        self._closing = False

    # -- lifecycle ---------------------------------------------------------------------------

    def create(
        self, key: str, source: str, start_seconds: float = 0.0, growing: GrowingSource | None = None, viewer: str = ""
    ) -> Session:
        """The session of a viewer for a file, made on first use; its encode starts at `start_seconds`, where
        the viewer is about to resume. Raises SessionError(reason) when it cannot be made."""
        # One creation at a time: the probes below take a moment, and two requests for the same file (a double
        # click, a second device) must not each open a session, nor several files together pass the limit.
        with self._create_lock:
            return self._create(key, source, start_seconds, growing, viewer)

    def _create(self, key: str, source: str, start_seconds: float, growing: GrowingSource | None, viewer: str) -> Session:
        with self._lock:
            existing = next((s for s in self._sessions.values() if s.key == key and s.viewer == viewer), None)
            if existing and not existing.dead:
                existing.last_request = time.monotonic()
                return existing
        if existing:
            self.close(existing.id)  # its encoder gave up: start afresh
        with self._lock:
            self._make_room()
        if not os.path.isfile(source):
            raise SessionError("file_not_found")
        duration = probe_duration(source)
        size = hls_ladder.probe_video_size(source)
        if duration is None or size is None:
            raise SessionError("unreadable")
        hdr = hls_ladder.probe_dynamic_range(source)
        encoder = hls_ladder.detect_encoder(settings.HLS_ENCODER)
        if settings.HLS_LIMIT_BY_SPEED:
            tonemap_on_cpu = hdr != "sdr" and not (encoder == hls_ladder.VAAPI and hls_ladder.detect_vaapi_tonemap())
            ladder = hls_diagnostics.fit_ladder(size[0], size[1], settings.HLS_LADDER_HEIGHTS, tonemap_on_cpu)
        else:
            ladder = hls_ladder.plan_ladder(size[0], size[1], settings.HLS_LADDER_HEIGHTS)
        if not ladder:
            raise SessionError("unreadable")
        tracks = hls_ladder.probe_audio_tracks(source)
        if tracks is None:  # ffprobe could not tell: fall back to MediaInfo for whether there is any audio
            has_audio = bool(video_conversion.first_audio_format(source))
            tracks = [hls_ladder.AudioTrack(0, "", "", "")] if has_audio else []
        has_audio = bool(tracks)
        audio_copy = hls_ladder.can_copy_audio(tracks, settings.AAC_CHANNELS)
        session_id = uuid.uuid4().hex
        directory = os.path.join(self.root, session_id)
        os.makedirs(directory, exist_ok=True)
        session = Session(
            id=session_id, key=key, source=source, directory=directory, duration=duration,
            segment_seconds=settings.HLS_SEGMENT_DURATION, ladder=ladder, src_height=size[1],
            has_audio=has_audio, audio_copy=audio_copy, encoder=encoder,
            audio_tracks=tracks, growing=growing, hdr=hdr, viewer=viewer,
        )
        if hdr == "dovi":
            log.event(f"HLS session {session.id[:8]}: Dolby Vision profile 5 has no HDR10 layer; its colours will look wrong")
        with self._lock:
            self._sessions[session_id] = session
        with session.lock:
            first = min(session.segment_count - 1, max(0, int(start_seconds // session.segment_seconds)))
            session.requested = first
            session.latest = first
            self._start_process(session, first)
        self._ensure_watcher()
        return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session:
            session.last_request = time.monotonic()
        return session

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        with session.lock:
            session.closed = True
            self._stop_process(session)
        shutil.rmtree(session.directory, ignore_errors=True)

    def snapshot(self) -> List[Dict[str, Any]]:
        """What each open session is doing, for the activity page."""
        with self._lock:
            sessions = list(self._sessions.values())
        now = time.monotonic()
        result: List[Dict[str, Any]] = []
        for session in sessions:
            hash_, _, filename = session.key.partition("/")
            through = self._through(session, produced_indexes(session.directory))
            samples = list(session.rate_samples)
            speed = None
            if len(samples) >= 2 and samples[-1][0] > samples[0][0] and samples[-1][1] > samples[0][1]:
                speed = (samples[-1][1] - samples[0][1]) * session.segment_seconds / (samples[-1][0] - samples[0][0])
            result.append({
                "id": session.id,
                "hash": hash_,
                "filename": filename,
                "encoder": session.encoder,
                "hdr": session.hdr,
                "levels": [r.height for r in session.ladder],
                "duration": session.duration,
                "position_seconds": session.requested * session.segment_seconds,
                "encoded_seconds": min(session.duration, (through + 1) * session.segment_seconds),
                "paused": session.paused,
                "speed": round(speed, 2) if speed is not None else None,
                "restarts": max(0, session.runs - 1),
                "idle_seconds": int(now - session.last_request),
                "failed": session.dead,
            })
        return result

    def keys(self) -> List[str]:
        """The files that have an encode session open."""
        with self._lock:
            return [s.key for s in self._sessions.values()]

    def close_key(self, key: str) -> None:
        """Close the session of one file ("hash/filename")."""
        with self._lock:
            ids = [s.id for s in self._sessions.values() if s.key == key]
        for session_id in ids:
            self.close(session_id)

    def close_torrent(self, magnet_hash: str) -> None:
        """Close the sessions of every file of a torrent."""
        with self._lock:
            ids = [s.id for s in self._sessions.values() if s.key.startswith(f"{magnet_hash}/")]
        for session_id in ids:
            self.close(session_id)

    def close_all(self) -> None:
        self._closing = True
        for session_id in list(self._sessions):
            self.close(session_id)

    def _make_room(self) -> None:
        """Caller holds self._lock. Drop the longest-idle session if the limit is reached."""
        if len(self._sessions) < max(1, settings.MAX_PARALLEL_HLS_STREAMS):
            return
        oldest = min(self._sessions.values(), key=lambda s: s.last_request)
        if time.monotonic() - oldest.last_request < 30:
            raise SessionError("capacity")
        self._sessions.pop(oldest.id, None)
        threading.Thread(target=self._discard, args=(oldest,), daemon=True).start()

    def _discard(self, session: Session) -> None:
        with session.lock:
            session.closed = True
            self._stop_process(session)
        shutil.rmtree(session.directory, ignore_errors=True)

    # -- encoder control ---------------------------------------------------------------------

    def _audio_args(self, session: Session, start_segment: int = 0) -> List[str] | None:
        if not session.has_audio:
            return None
        if session.audio_copy:
            if start_segment > 0:
                # A copied track cannot be trimmed: after a seek it starts where its container's seek point is,
                # up to a couple of seconds before the picture, and sound and picture part company. Encoded
                # again it starts where asked; the channel count stays the source's, so the init file still fits.
                return ["-c:a", "aac", "-b:a", settings.AAC_BITRATE, "-bsf:a", "aac_adtstoasc"]
            return ["-c:a", "copy", "-bsf:a", "aac_adtstoasc"]
        return ["-c:a", "aac", "-b:a", settings.AAC_BITRATE, "-ac", str(settings.AAC_CHANNELS), "-bsf:a", "aac_adtstoasc"]

    def _stop_process(self, session: Session) -> None:
        proc = session.proc
        session.proc = None
        session.paused = False
        if proc is None or proc.poll() is not None:
            return
        start = session.start_segment
        with contextlib.suppress(OSError):
            proc.send_signal(signal.SIGCONT)  # a stopped process only sees SIGTERM once resumed
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
        self._drop_cut_segments(session, start)

    @staticmethod
    def _drop_cut_segments(session: Session, start: int) -> None:
        """A stopped encoder writes out the segment it was in the middle of, cut short, under the segment's own
        name. Left there, the player gets a few frames where six seconds should be, and stalls at the gap. The
        newest segment of each variant from that run goes, so that one is encoded again when it is asked for."""
        for level in {int(m.group(1)) for n in os.listdir(session.directory) if (m := _SEGMENT_RE.match(n))}:
            indexes = produced_indexes(session.directory, level)
            last = encoded_through(indexes, start)
            if last >= start:
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(session.directory, segment_name(level, last)))

    def _start_process(self, session: Session, index: int) -> None:
        """Caller holds session.lock."""
        if session.closed:
            raise SessionError("closed")  # another request closed it meanwhile; its folder is gone
        self._stop_process(session)
        session.runs += 1
        # The first run's init segments are the ones the playlists point at; later runs write theirs aside.
        init_prefix = "init" if session.runs == 1 else f"rinit{session.runs}"
        # A file that is still downloading is read through a pipe, only as far as it has arrived;
        # once it is whole, the encoder reads it by path and can seek in it.
        piped = session.growing is not None and not session.growing.complete()
        source = session.source
        if not piped and not os.path.exists(source) and session.growing and session.growing.final_path:
            source = session.growing.final_path
        args = hls_ladder.build_session_args(
            session.ladder, session.src_height, session.encoder, settings.HLS_X264_PRESET,
            session.segment_seconds, "pipe:0" if piped else source, self._audio_args(session, index), index, init_prefix, session.audio_tracks, settings.AAC_CHANNELS,
            settings.AUDIO_DOWNMIX, settings.AUDIO_DOWNMIX_BOOST, hls_ladder.detect_hw_decode(settings.HLS_HWDECODE),
            session.hdr, session.hdr != "sdr" and session.encoder == hls_ladder.VAAPI and hls_ladder.detect_vaapi_tonemap(),
        )
        with open(os.path.join(session.directory, "ffmpeg.log"), "a") as stderr_log:
            session.proc = Popen(
                args, stdin=PIPE if piped else DEVNULL, stdout=DEVNULL, stderr=stderr_log, cwd=session.directory
            )
        if piped and session.growing is not None:
            threading.Thread(target=self._feed, args=(session.source, session.growing, session.proc), daemon=True).start()
        session.start_segment = index
        session.floor = 0
        session.progress_mark = -1
        session.progress_at = time.monotonic()
        session.rate_samples.clear()
        log.event(f"HLS session {session.id[:8]}: encoding from segment {index} ({session.encoder}, run {session.runs})")

    @staticmethod
    def _feed(path: str, growing: GrowingSource, proc: "Popen[bytes]") -> None:
        """Write the downloaded part of the file to the encoder, waiting for more as it arrives."""
        written = 0
        next_window_at = 0
        stdin = proc.stdin
        if stdin is None:
            return
        try:
            while not os.path.isfile(path):
                if growing.final_path and os.path.isfile(growing.final_path):
                    path = growing.final_path  # the download finished and was moved before the encoder got here
                    break
                if proc.poll() is not None:
                    return
                time.sleep(0.5)
            with open(path, "rb") as f:
                while proc.poll() is None:
                    if growing.request_window is not None and written >= next_window_at:
                        growing.request_window(written)
                        next_window_at = written + settings.HLS_PREFETCH_BYTES // 2
                    # The piece count can run ahead of the bytes flushed to the file.
                    if growing.final_path and os.path.exists(growing.final_path):
                        # Finished and linked: the torrent may be gone by now (its count reads 0), but the file we hold open is whole.
                        available = os.fstat(f.fileno()).st_size
                    else:
                        available = growing.available()
                        with contextlib.suppress(OSError):
                            available = min(available, os.path.getsize(path))
                    if available <= written:
                        if written >= growing.total_size:
                            break
                        time.sleep(0.5)
                        continue
                    chunk = f.read(min(PIPE_CHUNK, available - written))
                    if not chunk:
                        time.sleep(0.2)
                        continue
                    stdin.write(chunk)
                    written += len(chunk)
        except (BrokenPipeError, ValueError, OSError):
            pass  # the encoder was stopped or restarted
        finally:
            with contextlib.suppress(Exception):
                stdin.close()

    @staticmethod
    def _through(session: Session, indexes: List[int]) -> int:
        """How far the running encoder has got, counting its run from the oldest segment not yet deleted."""
        return encoded_through(indexes, max(session.start_segment, session.floor))

    def _ensure_encoding(self, session: Session, index: int) -> None:
        """Make sure an encoder is, or soon will be, producing segment `index`."""
        with session.lock:
            if abs(index - session.latest) > RESTART_GAP:
                return  # a newer request has moved on; this one is stale
            proc = session.proc
            alive = proc is not None and proc.poll() is None
            last = self._through(session, produced_indexes(session.directory))
            if alive:
                # Coming up (or next) in this run: wait for it. Already past, or too far ahead: restart there.
                if last < index <= last + 1 + RESTART_GAP:
                    if session.paused:
                        self._resume(session)
                    return
            if alive:
                log.event(f"HLS session {session.id[:8]}: asked for segment {index} while the encoder is at {last}, moving it")
            self._start_process(session, index)

    def _resume(self, session: Session) -> None:
        if session.proc and session.paused:
            with contextlib.suppress(OSError):
                session.proc.send_signal(signal.SIGCONT)
            session.progress_at = time.monotonic()  # a pause is not a stall
        session.paused = False

    # -- serving -----------------------------------------------------------------------------

    def file_for(self, session: Session, name: str, cancelled: Callable[[], bool] = lambda: False) -> str | None:
        """Path of an init or media segment, waiting for the encoder if it has not got there yet."""
        parsed = parse_segment_request(name)
        if parsed is None or parsed[1] >= session.variant_count:
            return None
        kind, level, index = parsed
        path = os.path.join(session.directory, name)
        session.last_request = time.monotonic()
        if kind == "seg":
            if index >= session.segment_count:
                return None
            session.requested = index
            session.latest = index
        try:
            found = self._wait_for(session, kind, level, index, path, cancelled)
        except SessionError:
            return None  # the session was closed under this request
        if found and kind == "seg":
            self._place(session, level, index, found)
        return found

    def _place(self, session: Session, level: int, index: int, path: str) -> None:
        """Put a segment's fragment times where the segment sits in the file (see fmp4). Done on every
        send: a segment deleted and encoded again has the same name, and one already in place is not moved."""
        with session.lock:
            timescales = session.timescales.get(level) or _read_timescales(session.directory, level)
            if not timescales:
                return
            session.timescales[level] = timescales
            with contextlib.suppress(OSError, struct.error, IndexError):
                fmp4.place_segment(path, timescales, index, session.segment_seconds)

    def _wait_for(
        self, session: Session, kind: str, level: int, index: int, path: str, cancelled: Callable[[], bool]
    ) -> str | None:
        if not os.path.isfile(path):
            if kind == "seg":
                self._ensure_encoding(session, index)
            elif session.runs == 0:
                self._ensure_encoding(session, 0)  # nothing has started yet: the init comes with the first run
        retried = False
        deadline = time.monotonic() + SEGMENT_WAIT_SECONDS
        while time.monotonic() < deadline and not cancelled():
            if self._ready(session, kind, level, path):
                session.failures = 0
                return path
            proc = session.proc
            if proc is not None and proc.poll() not in (None, 0):
                # session.proc is only ever the current encoder (a restart clears it before stopping the old
                # one), so a non-zero exit here is the encoder dying on its own, not being replaced.
                if not self._recover(session, proc, index):
                    return None
                continue
            if kind == "seg" and abs(index - session.latest) > RESTART_GAP:
                return None  # the viewer has moved on
            if kind == "seg" and (proc is None or proc.poll() is not None):
                # It finished or was replaced without producing this one: start over here, once.
                if retried:
                    return None
                retried = True
                self._ensure_encoding(session, index)
            time.sleep(POLL_SECONDS)
        return path if self._ready(session, kind, level, path) else None

    def _recover(self, session: Session, failed: "Popen[bytes]", index: int) -> bool:
        """The encoder died by itself: start it again, up to MAX_ENCODER_FAILURES times in a row. Past that,
        the session is marked dead (create() then replaces it) and the request gets nothing."""
        with session.lock:
            if session.proc is not failed:
                return True  # another request already restarted it
            session.failures += 1
            log.event(f"HLS session {session.id[:8]}: encoder stopped (exit {failed.returncode}), failure {session.failures}")
            if session.failures > MAX_ENCODER_FAILURES:
                session.dead = True
                if session.encoder == hls_ladder.VAAPI:
                    hls_ladder.fall_back_to_software()  # the replacement session then encodes in software
                log.event(f"HLS session {session.id[:8]}: encoder keeps failing (exit {failed.returncode}), giving up")
                return False
            self._start_process(session, index if abs(index - session.latest) <= RESTART_GAP else session.latest)
            return True

    @staticmethod
    def _ready(session: Session, kind: str, level: int, path: str) -> bool:
        """Whether the file can be sent. An init file exists as soon as ffmpeg opens it, but its contents are
        written later: sent early, the player gets an empty file and cannot start. Once a segment of that
        variant exists, the init file is whole. If the first run was replaced before it got that far, its init
        file stays empty for good; a later run's identical one stands in for it."""
        if kind != "init":
            return os.path.isfile(path)
        if not produced_indexes(session.directory, level):
            return False
        with contextlib.suppress(OSError):
            if os.path.getsize(path) > 0:
                return True
        return _adopt_later_init(session.directory, level, path)

    # -- housekeeping ------------------------------------------------------------------------

    def _ensure_watcher(self) -> None:
        with self._lock:
            if self._watcher and self._watcher.is_alive():
                return
            self._watcher = threading.Thread(target=self._watch, daemon=True)
            self._watcher.start()

    def _watch(self) -> None:
        while not self._closing:
            time.sleep(1.0)
            with self._lock:
                sessions = list(self._sessions.values())
            if not sessions:
                continue
            for session in sessions:
                try:
                    self._tend(session)
                except Exception as e:  # keep the watcher alive whatever one session does
                    log.debug(f"HLS session housekeeping failed: {e}")

    def _tend(self, session: Session) -> None:
        if time.monotonic() - session.last_request > IDLE_SECONDS:
            self.close(session.id)
            return
        with session.lock:
            indexes = produced_indexes(session.directory)
            stuck = self._watch_progress(session, indexes)
            if session.proc is not None and session.proc.poll() is None and indexes:
                ahead = self._through(session, indexes) - session.requested
                if ahead > PAUSE_AHEAD and not session.paused:
                    with contextlib.suppress(OSError):
                        session.proc.send_signal(signal.SIGSTOP)
                    session.paused = True
                elif ahead <= RESUME_AT and session.paused:
                    self._resume(session)
        if stuck is not None:
            self._recover(session, stuck[0], stuck[1])
        self._delete_behind(session)

    def _watch_progress(self, session: Session, indexes: List[int]) -> "tuple[Popen[bytes], int] | None":
        """Note how far the running encoder has got. Returns (process, segment to restart at) when it is stuck:
        running, not paused, with somewhere to go, and nothing new for STALL_SECONDS. Waiting for the download
        to catch up is not being stuck. Caller holds session.lock."""
        proc = session.proc
        now = time.monotonic()
        through = self._through(session, indexes)
        waiting_for_download = session.growing is not None and not session.growing.complete()
        if proc is not None and proc.poll() is None and not session.paused:
            session.rate_samples.append((now, through))
            del session.rate_samples[: max(0, len(session.rate_samples) - RATE_WINDOW_SAMPLES)]
        else:
            session.rate_samples.clear()
        if proc is None or proc.poll() is not None or session.paused or waiting_for_download or through != session.progress_mark:
            session.progress_mark = through
            session.progress_at = now
            return None
        if now - session.progress_at < STALL_SECONDS or through + 1 >= session.segment_count:
            return None
        log.event(f"HLS session {session.id[:8]}: no new segment for {int(now - session.progress_at)} s after {through}, restarting the encoder")
        session.progress_at = now
        return proc, through + 1

    def _delete_behind(self, session: Session) -> None:
        cutoff = session.requested - KEEP_BEHIND
        if cutoff <= 0:
            return
        session.floor = max(session.floor, cutoff)
        with contextlib.suppress(OSError):
            for name in os.listdir(session.directory):
                match = _SEGMENT_RE.match(name)
                if match and int(match.group(2)) < cutoff:
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(session.directory, name))
