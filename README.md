# PlayBay

PlayBay is a self-hosted video service and torrent client that makes playing videos from torrents as easy as:

1. Opening the web app on a phone, laptop, tablet or smart TV.
2. Searching for content.
3. Selecting the video file you want.
4. Pressing play. It starts while the file is still downloading and converting.
5. Watching on the device, or casting to AppleTV/Chromecast.

PlayBay is a fork of [RapidBay by hauxir](https://github.com/hauxir/rapidbay), renamed because it has diverged far from the original (internal names such as the `rapidbay` Docker image, the `RAPIDBAY_PORT` variable and the `rapidbay` service in the compose files are unchanged for now). It keeps the Python backend and the MIT license, and changes how the app looks, streams and is configured. See [What is different in this fork](#what-is-different-in-this-fork).

## Features

-   Uses [Jackett](https://github.com/Jackett/Jackett) and/or [Prowlarr](https://github.com/Prowlarr/Prowlarr) as a search backend.
-   Pick the individual video file you want to play and the system takes care of the rest to make it streamable.
-   Plex-style streaming: the video is encoded on the fly, a little ahead of where you are, into several quality levels, and the player switches between them to match your connection speed. Seek anywhere; nothing is converted in advance.
-   Automatic download of subtitles, converted for all browsers, Chromecast and AppleTV.
-   Converts audio that browsers don't support to AAC.
-   Settings page: change almost every option from the browser, no restart needed for most.
-   Home screen with this week's worldwide trending series and movies, your watch history and favorites.
-   Works with a TV remote, including Android TV and Google TV.
-   Registers as a handler for any magnet link.
-   Cleans up disk space automatically.
-   Kodi support.

## What is different in this fork

-   **New web UI.** The whole frontend is rewritten in Vue 3, TypeScript and Vite (`frontend/`), with a new dark design. Keyboard and TV-remote navigation, resume, favorites, search history and Chromecast are kept. It has unit tests (Vitest) and browser tests (Playwright).
-   **Plex-style adaptive streaming.** Downloads are kept as they are and encoded on demand, a few minutes ahead of the playhead, into 1080p, 720p, 480p and 360p (never above the source). The player picks a level from the measured connection speed and you can seek anywhere. See [Adaptive streaming](#adaptive-streaming).
-   **Settings page** at `/settings`: 34 options in seven categories, saved to `settings.json`, applied live where possible. See [Settings page](#settings-page).
-   **Trending from TMDB.** The home rails show this week's worldwide trending titles when a TMDB key is set. The empty-search feed is now called "Latest releases".
-   **Rich search is the only mode,** and a download can be cancelled from its screen.
-   **A local stack with a VPN.** `docker-compose.local.yml` runs PlayBay and Prowlarr behind an ExpressVPN tunnel (via [gluetun](https://github.com/qdm12/gluetun)), so searches and torrent traffic both leave through the VPN. See [Running the local stack](#running-the-local-stack).
-   **Hardware encoding and diagnostics.** On Linux with an Intel or AMD GPU mapped into the container, video is encoded and decoded on the GPU (VAAPI), after a test confirms it can honour the bitrates; otherwise it falls back to software. A diagnostics view under Settings > Streaming shows what was detected and why, a startup speed test drops quality levels the machine cannot keep up with, a stuck encoder is restarted, and an end-to-end playback test (real ffmpeg, seeks and restarts) runs in CI. See [Adaptive streaming](#adaptive-streaming) and [What works on which hardware](#what-works-on-which-hardware).

## Setting up a search backend

PlayBay requires a torrent indexer for searching. You can use [Jackett](https://github.com/Jackett/Jackett), [Prowlarr](https://github.com/Prowlarr/Prowlarr), or both at the same time (results are merged and deduplicated).

### Jackett

See [how to set Jackett up with Docker](https://github.com/Jackett/Jackett#installation-using-docker). The [Jackett compose example](docker-compose.example.with.jackett.yml) shows how to connect it to PlayBay.

### Prowlarr

See the [Prowlarr docs](https://wiki.servarr.com/prowlarr/installation). The [Prowlarr compose example](docker-compose.example.with.prowlarr.yml) shows how to connect it to PlayBay.

## Running

There is no published image for this fork, so build it from this repository.

### Running the local stack

`docker-compose.local.yml` starts PlayBay and Prowlarr behind an ExpressVPN tunnel.

1. Copy `.env.example` to `.env` and `.env.vpn.example` to `.env.vpn`.
2. In `.env.vpn`, set `OPENVPN_USER` and `OPENVPN_PASSWORD` to your ExpressVPN **manual-configuration** credentials (not your login email and password), and optionally `SERVER_COUNTRIES`.
3. In `.env`, set `TMDB_API_KEY` if you want posters and the trending rails.
4. Start it:

```bash
docker compose --env-file .env --env-file .env.vpn -f docker-compose.local.yml up -d --build
```

PlayBay is at <http://localhost:5050> (change it with `RAPIDBAY_PORT`) and Prowlarr at <http://localhost:9696>. After Prowlarr's first start, copy its API key (Settings > General) into `PROWLARR_API_KEY` in `.env.vpn` and run the command again. The `.env` files stay out of git.

Without a VPN, `docker-compose.yml` runs PlayBay alone on port 5000.

### With Docker

```bash
docker build -t rapidbay .

# Using Jackett
docker run -p 5000:5000 -e JACKETT_HOST="http://your.jackett.host" -e JACKETT_API_KEY="YourAPIKey" rapidbay

# Using Prowlarr
docker run -p 5000:5000 -e PROWLARR_HOST="http://your.prowlarr.host" -e PROWLARR_API_KEY="YourAPIKey" rapidbay
```

### Without Docker

**System dependencies:**

```bash
# Ubuntu/Debian
sudo apt install python3 python3-venv ffmpeg mediainfo nginx nodejs npm

# macOS
brew install python ffmpeg mediainfo nginx node
```

**Optional:** install [alass](https://github.com/kaegi/alass/releases) for subtitle synchronization.

**Install and build:**

```bash
# Python dependencies (uv is recommended)
uv sync
# or: python3 -m venv .venv && source .venv/bin/activate && pip install .

# The web UI
cd frontend && npm ci && npm run build && cd ..
```

**Run the app:**

```bash
export JACKETT_HOST="http://your.jackett.host"
export JACKETT_API_KEY="YourAPIKey"
# export PROWLARR_HOST="http://your.prowlarr.host"
# export PROWLARR_API_KEY="YourAPIKey"

cd app
uvicorn app:app --host 0.0.0.0 --port 5000 --workers 1 --timeout-keep-alive 900
```

The app is at http://localhost:5000. It serves the built UI from `./frontend/dist`; point `FRONTEND_DIR` elsewhere to change that.

## Settings page

Open `/settings` (or the Settings button in the home screen's header) to change settings from the browser. Changes are saved to `settings.json` next to the library file (`LIBRARY_PATH`), so mount that directory as a volume to keep them across container rebuilds.

A saved value beats the environment variable, which beats the built-in default. "Reset" removes the saved value; "Clear" saves an empty value that overrides the environment. Listening port and DHT routers need a restart; download and upload limits apply to torrents added afterwards. Storage paths stay environment-only.

If `PASSWORD` is set, the page uses it. If it is not, anyone who can reach the server can open the page, and can set a password. To recover from a lockout, delete `settings.json`. A numeric `PASSWORD` from the environment (such as `1234`) now works; before it could never match.

## Adaptive streaming

PlayBay streams like Plex or Jellyfin: the download is kept as it is, and video is encoded only while someone watches it, only a little ahead of the playhead.

- **Finished files:** opening a file starts an *encode session*. The player gets the whole timeline at once, so you can seek anywhere. A seek starts the encoder at that point (a few seconds), it runs a few minutes ahead of you and pauses, and segments well behind you are deleted. One ffmpeg process encodes every quality level (1080p, 720p, 480p and 360p by default, never above the source size) and the player switches between them to match your connection speed. A quality menu pins one. The session is removed a few minutes after you stop watching.
- **Audio and subtitles:** every audio track of the file is offered as a language in the player's menu (English, French, Italian, and so on), and each text subtitle track and downloaded subtitle as a subtitle. Picture-based subtitles (PGS, VobSub) are not supported. A session opened while a file is still downloading lists the subtitles that exist at that moment.
- **Surround audio:** a 5.1 or 7.1 track is mixed down to stereo (`AAC channels` = 2) with the dialogue kept up front, because ffmpeg's own mix leaves speech several dB quiet. Pick the mix under Settings > Encoding: Dialogue (the default), Default (ffmpeg's), Dave750 or Night mode (the recipes Jellyfin offers), and a volume percentage. Set `AAC channels` to 6 to keep 5.1 for a surround system. HE-AAC and other non-LC audio is re-encoded to AAC-LC.
- **Direct play:** an MP4 with H.264 video and AAC audio is served as it is, with no encoding.
- **While downloading:** a file that is still downloading plays the same way, from the part that has arrived. Playback starts by itself once the first 20 seconds of video (the "Start buffer" setting under Streaming) have downloaded. The server reads the file's length from its headers, so this adapts to the file: a low-bitrate rip needs only a few MB, a 4K remux more. You can seek anywhere in what has been downloaded; seeking past it waits for the download to catch up. The encoder is fed from the torrent in order, and the torrent is asked for the bytes it is about to need. When the download finishes, the same session carries on. Only an MP4 whose index is at the end of the file has to wait for the whole download.
- **Disk use:** the original download is hard-linked into the output folder rather than copied or converted, so a finished file costs its own size once. A playing session adds a few minutes of video per quality level (62 MB in a 5-minute, 1080p test, against a source of 528 MB), and is deleted when you stop watching. Nothing else is stored.

- **Quality levels:** edit them in Settings, under Streaming.
- **Hardware encoding:** `HLS_ENCODER=auto` (the default) uses the GPU only if a bitrate-capped test encode on it succeeds, and falls back to software x264 otherwise. `software` or `vaapi` force one. Only VAAPI (Intel and AMD on Linux) is supported as a hardware path; NVIDIA and Intel QSV are not used yet. Decoding the source on the GPU (`HLS_HWDECODE=auto`) is independent of the encoder.
- **Speed:** each session must encode faster than real time, and every quality level adds work. At startup a short test encode measures this machine, and new sessions leave out the highest quality levels while it could not encode them all faster than 1.5x real time (Settings > Streaming, "Limit quality levels to what this machine can encode"). The test covers encoding only, not decoding a heavy source. With software encoding you can also set `HLS_X264_PRESET` to `superfast` or `ultrafast`. `MAX_PARALLEL_HLS_STREAMS` limits concurrent sessions.
- **Seeing what happened:** Settings > Streaming > "Video encoding on this machine" (or `/api/hls_diagnostics`) shows the encoder and decoder chosen, each hardware test that was tried and why it failed, and the measured speed. Include it in a bug report. Encoder starts, restarts and failures also go to the container's log (`docker logs`). An encoder that is running but produces nothing for 45 seconds is restarted.
- **Kodi and casting:** both get the original file, not an encode, so they need a device that plays its format. Chromecast works for H.264 MP4 files.

### What works on which hardware

| Setup | Encoding |
| --- | --- |
| Linux, Intel or AMD GPU, `/dev/dri` mapped into the container | Hardware (VAAPI), tested on Intel |
| Linux, no GPU or none mapped | Software (x264), limited to what the CPU can keep up with |
| Windows or macOS with Docker Desktop | Software, same limit. There is no `/dev/dri` in these containers |
| NVIDIA (any system) | Software for now: NVENC is not supported yet |
| Intel QSV | Not used |
| Windows without Docker | Not supported |

Hardware encoding for NVIDIA and for Windows is wanted but has not been written, because nobody has been able to test it. If you have such a machine and want to help, open a "Hardware report" issue with your diagnostics report (Settings > Streaming > "Video encoding on this machine").

### Hardware encoding on Linux (Intel and AMD, VAAPI)

Map the GPU into the container and give it access to the render group. In `docker-compose.yml`:

```yaml
services:
  rapidbay:
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "992"   # the group that owns /dev/dri/renderD128 on the host: `stat -c %g /dev/dri/renderD128`
```

Then open the diagnostics view. If the encoder says `libx264`, the test list says why:

- `/dev/dri/renderD128 does not exist`: the device is not mapped, or the machine has no GPU.
- A permission error: the container user is not in the render group (`group_add` above).
- Intel Kaby Lake, Coffee Lake and similar chips can encode H.264 at a target bitrate only with the non-free Intel media driver, which the image installs. Newer Intel chips and AMD work with the free drivers.
- Intel chips that only support constant quality cannot honour the quality levels' bitrates and use software encoding.

Without a GPU the encoder runs on the CPU. A recent 4-core CPU encodes one or two 720p levels comfortably; the speed test removes the levels it cannot keep up with.

## Subtitles

You'll need a VIP account at OpenSubtitles.org for it to work:

```
-e OPENSUBTITLES_USERNAME=someusername -e OPENSUBTITLES_PASSWORD=yourpassword
```

### Configuring which subtitles to download

The default setting downloads English subtitles. Add the env variable `SUBTITLE_LANGUAGES` to get more languages:

```
-e SUBTITLE_LANGUAGES="['en', 'de', 'es']"
```

## Require a password

Add the env variable `PASSWORD` to prompt for a password when opening PlayBay (or set it on the Settings page):

```
-e PASSWORD=YOURPASSWORD
```

## Registering as a handler for any magnet link

-   Go to `https://<RAPIDBAY_HOST>/registerHandler` and it should prompt you to register your running PlayBay instance as the default handler for torrent links on any torrent site.
-   You can also paste a magnet link directly into the search bar to open it.

## Using debrid caching

-   You can speed up downloads by using a torrent cache from Real Debrid and/or TorBox.
-   For Real Debrid, set the env variable `RD_TOKEN` to the one on https://real-debrid.com/apitoken.
-   For TorBox, set the env variable `TB_TOKEN` to your API key from https://torbox.app/settings.
-   If both are set, Real Debrid is tried first and TorBox is used as a fallback.

## Using Kodi as a frontend

The Kodi addon is served at `/app.kodi.py`. Setup steps are in the [upstream wiki](https://github.com/hauxir/rapidbay/wiki/Setting-up-Rapidbay-with-Kodi). They predate this fork, so some details may differ. The addon is not tested here.

## Running PlayBay on a VPS

The [upstream wiki](https://github.com/hauxir/rapidbay/wiki/Setting-RapidBay-up-on-a-VPS-and-tunnel-torrent-traffic-through-NordVPN) describes tunnelling torrent traffic through NordVPN. The local stack above does the same with ExpressVPN and gluetun.

## Developing

```bash
# Backend: type check, lint and tests
./ci.sh

# Frontend (in frontend/): dev server, unit tests, build, browser tests
npm run dev
npm test
npm run build
npm run e2e        # needs the Docker stack running on port 5050
```

See [`frontend/README.md`](frontend/README.md) for how the browser tests work. The Dockerfile builds the frontend into `/frontend`, which the backend serves at `/`. The image must be rebuilt after any change under `frontend/src`.

Design notes and plans are in [`docs/superpowers`](docs/superpowers).

## Responsible use

PlayBay is a general-purpose, self-hosted torrent client and media player. It ships with no content and no list of sources. You are responsible for what you search for, download and stream, and for following the copyright laws and other rules that apply where you live. Only use it for content you have the right to access, such as public-domain, openly licensed or your own media.

## License and credits

MIT, see [LICENSE](LICENSE). RapidBay was created by [Haukur Rosinkranz (hauxir)](https://github.com/hauxir/rapidbay); this fork builds on that work.
