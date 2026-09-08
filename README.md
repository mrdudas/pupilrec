# Pupil Core recording front-end

Live view and synchronised recording of two Pupil Core headsets (four cameras)
from a browser -- built for driving it from an iPad.

* **World cameras** — 1920x1080 MJPEG @ 30 fps (the sensor's maximum)
* **Eye cameras** — 400x400 MJPEG @ 120 fps (the sensor's maximum)
* Frames are **never decoded or re-encoded**: the JPEGs the cameras emit are the
  JPEGs stored in the video file and the JPEGs sent to the browser.
* Every recording ships a per-camera CSV mapping frame number to wall clock time.

## Which headset is which

A headset is identified by the **front panel USB port it is plugged into**, not
by device numbering (which changes on every replug). The port is read from
sysfs: each headset's internal hub sits on one root port, so all its cameras
share e.g. `3-1`, and the other headset's share `3-7`.

The first run assigns the lower-numbered port to `left` and stores it in
`config.json`. If the labels come out the wrong way round, swap them once:

```sh
./run.py --swap-sides
```

## Install

```sh
./setup/install.sh
```

It installs the system packages, builds Pupil Labs' `libuvc` fork, installs
`pyuvc` **from git**, and drops in the udev rules. Both details matter --
see [docs/USB-BANDWIDTH.md](docs/USB-BANDWIDTH.md) for why the kernel's V4L2
driver cannot run these four cameras at all, and which two traps the stack has.

## Run

```sh
./run.py                  # serve on 0.0.0.0:8080
./run.py --list           # show which camera is on which port
./run.py --port 9000
```

It prints the URL to open on the iPad, e.g. `http://192.168.7.50:8080/`.

To have it come back after a reboot, see `setup/pupilrec.service`.

## Using it

1. Open the URL on the iPad — all four cameras appear live.
2. Optionally type a name, press **Felvétel indítása**.
3. Press **Felvétel leállítása**. The files are closed and listed underneath.

The preview is throttled (10 fps by default, selectable in the header) so that
Wi-Fi never limits what gets recorded. **Recording always stores every captured
frame** regardless of what the preview shows.

## What a recording contains

```
recordings/2026-09-08_19-55-29_teszt/
├── left_world.mkv    1920x1080 MJPEG, stream-copied
├── left_world.csv    one row per stored frame
├── left_eye.mkv      400x400 MJPEG
├── left_eye.csv
├── right_world.mkv
├── right_world.csv
├── right_eye.mkv
├── right_eye.csv
└── recording.json    modes, camera identities, start/stop, per-camera results
```

Each CSV row is one frame of the corresponding video, in order:

| column | meaning |
|---|---|
| `frame_index` | 0-based frame number **in the video file** |
| `unix_time` | host clock when the frame arrived |
| `iso_time` | the same instant, human readable |
| `monotonic_time` | host monotonic clock, unaffected by clock adjustments |
| `device_time` | the camera's own clock |
| `uvc_index` | the device's frame counter — gaps mean the camera dropped a frame |
| `jpeg_bytes` | size of that frame |

So frame *n* of `left_world.mkv` happened at `unix_time` of row *n*. The videos
carry a constant nominal frame rate; the CSV is where the exact timing lives,
and it is what aligns the four cameras with each other.

`recording.json` also stores `clock_offset_unix_minus_monotonic`, so the
monotonic column can be mapped to wall clock even if the system clock is
adjusted mid-recording.

## Disk use

Storing JPEG untouched costs disk instead of CPU, which is the intended trade:

| stream | rate |
|---|---|
| world camera (1080p30) | ~7 MB/s each |
| eye camera (400x400@120) | ~2 MB/s each |
| **all four together** | **~18 MB/s, ~65 GB/hour** |

Measured on a 20 s recording: 4805 eye frames and 1196 world frames, **zero
dropped**, all four cameras started within 12 ms of each other.

If the disk stalls, frames are dropped rather than blocking capture (that would
corrupt timing on every camera sharing the bus); the count shows up per camera
in the UI and in `recording.json` as `dropped_queue`.

## Layout

```
pupilrec/usbmap.py     cameras -> physical USB port -> headset
pupilrec/capture.py    one thread per camera; fans frames out to preview + recording
pupilrec/recording.py  ffmpeg stream-copy muxing and the timestamp tables
pupilrec/server.py     HTTP API and MJPEG preview streams (standard library only)
pupilrec/static/       the iPad UI
setup/                 udev rules, installer, systemd unit
```
