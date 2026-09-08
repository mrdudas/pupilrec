# Pupil Core recording front-end

Live view and synchronised recording of two Pupil Core headsets (four cameras)
from a browser -- built for driving it from an iPad.

* **World cameras** — 1280x720 MJPEG @ 60 fps
* **Eye cameras** — 400x400 MJPEG @ 120 fps (the sensor's maximum)

The world cameras can do either 1920x1080@30 or 1280x720@60; the higher frame
rate is the default. Change it in `config.json` (`modes.world`) if the extra
resolution is worth more than the extra frames on a given day.
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

To have it come back after a reboot:

```sh
sudo cp setup/pupilrec.service /etc/systemd/system/
sudo systemctl enable --now pupilrec
```

The unit runs as `zsolt` with the `plugdev` group, which is what grants raw USB
access when nobody is logged in graphically -- the `uaccess` tag in the udev
rules only covers a local desktop session.

## Using it

1. Open the URL on the iPad — all four cameras appear live.
2. Optionally type a name, press **Felvétel indítása**.
3. Press **Felvétel leállítása**. The files are closed and listed underneath.

The preview is throttled (10 fps by default, selectable in the header) so that
Wi-Fi never limits what gets recorded. **Recording always stores every captured
frame** regardless of what the preview shows.

## Several clients, unreliable connections

All state lives on the server. A client renders whatever `/api/status` reports
and never trusts its own view, which is what makes the following work:

* **Any number of clients can watch and control the same session.** When one
  starts or stops a recording, the others see it within a second and say which
  device did it. Every client can stop a recording, not only the one that
  started it.
* **A client going away changes nothing.** Close the tab, lock the iPad, walk
  out of Wi-Fi range -- the recording keeps running on the server and keeps
  every frame. Reconnecting rejoins the session in progress, with the correct
  elapsed time.
* **Two clients pressing start at once is not an error.** The one that loses the
  race is told a recording is already running and who owns it, then simply
  displays it. The same applies to stopping something already stopped.
* **A dead preview is never shown as live.** MJPEG in an `<img>` freezes
  silently when its connection dies, so the page watches for it: losing the
  status poll greys out every tile and raises a banner, and streams are rebuilt
  on reconnect. Individual frozen streams are also detected and reattached,
  where the browser gives a per-frame signal to detect them with.
* Stalled viewers are dropped server-side after 20 s rather than holding a
  thread until TCP gives up.

## Sensor board

A STEVAL-STWINKT1B records its ten sensors into the same recording directory,
driven by `run_sensors.py` as a **separate process** -- it shares no thread,
lock or USB bandwidth with the cameras, and follows the recorder's HTTP status
to know when to start and stop.

```sh
./run_sensors.py --probe     # list what the board offers
./run_sensors.py             # follow the recorder and log alongside it
sudo cp setup/pupilrec-sensors.service /etc/systemd/system/
sudo systemctl enable --now pupilrec-sensors
```

Vibration and motion sensors, magnetometer, pressure and temperature go to CSV,
one file each. The two microphones go to WAV plus a timestamp table, because CSV
at 192 kHz demonstrably loses samples. Details, the stream format and the
measured rates are in [docs/STWIN-SENSORS.md](docs/STWIN-SENSORS.md).

## When something wedges

The UI has a **Rendszer állapota** panel showing whether the cameras stream, the
sensor daemon runs, and the board actually answers -- plus three recovery
buttons. It refuses to restart anything mid-recording without a confirmation.

| what wedges | what happens |
|---|---|
| sensor daemon hangs | its watchdog aborts it, systemd restarts it -- automatic |
| a camera drops its stream | the worker reopens it -- automatic |
| the board stops responding | **Board tápciklizálása** power-cycles its USB port and restarts the daemon with it |
| the recorder itself | **Kameraszerver újraindítása** |

The board's power cycle brought it back every time it was tried here, but it is
not guaranteed: if the board still does not answer, its physical RESET button is
the only remaining option. The panel says so rather than pretending otherwise.

The panel is deliberate about one state: if a recording is running while sensors
are **not** being captured, it says so in red. A green light over a recording
that is quietly missing half its data is the failure worth designing against.

**Never `kill -9` the sensor daemon**: a process killed while the board streams
wedges the board until someone presses its RESET button.

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
├── recording.json    modes, camera identities, start/stop, per-camera results
├── stwin_iis3dwb_acc.csv        vibration, 26.7 kHz
├── stwin_ism330dhcx_acc.csv     IMU accelerometer
├── stwin_ism330dhcx_gyro.csv    IMU gyroscope
├── stwin_iis2dh_acc.csv         accelerometer
├── stwin_iis2mdc_mag.csv        magnetometer
├── stwin_lps22hh_press.csv      pressure
├── stwin_lps22hh_temp.csv       temperature
├── stwin_stts751_temp.csv       temperature
├── stwin_imp23absu_mic.wav      analog microphone, 192 kHz
├── stwin_imp23absu_mic_timestamps.csv
├── stwin_imp34dt05_mic.wav      digital microphone, 48 kHz
├── stwin_imp34dt05_mic_timestamps.csv
├── stwin_board.json  every sensor's configuration as the board reported it
└── stwin_recording.json  per-sensor row counts, lost bytes, timing
```

Every sensor CSV carries `sample_index, unix_time, device_time` and one column
per channel. `unix_time` is the same host clock as the camera tables, so sensor
rows and video frames line up directly.

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
| world camera (720p60) | ~6 MB/s each |
| eye camera (400x400@120) | ~2 MB/s each |
| **all four together** | **~16 MB/s, ~57 GB/hour** |

Measured on a 15 s recording: 3604 eye frames and 1793 world frames, **zero
dropped** on any camera and no gaps in any device frame counter. 1080p30 costs
slightly more (16.6 MB/s) for half the world frame rate.

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
