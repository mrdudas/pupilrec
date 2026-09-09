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

## More than two headsets

Headsets and their cameras are optional and discovered at start-up. Two get the
labels `left` and `right`; further ones are numbered (`unit3`), and any label
can be changed in `config.json` -- the label is what names the files. A headset
carrying more than one eye camera gets `left_eye`, `left_eye2`, numbered in USB
port order, so the names existing recordings use never move.

`tests/test_camera_naming.py` covers three headsets, two eye cameras on one
headset and a single headset, all against synthetic device lists:

```sh
./.venv/bin/python -m unittest discover -s tests
```

**A camera plugged in while the system runs needs a restart to be used.** The UI
says so when it detects one, and the restart button applies it. This is not
laziness: pyuvc enumerates devices inside both `uvc.device_list()` and the
`Capture` constructor, and those calls can block indefinitely while other
cameras stream -- with the GIL held, which stops every thread in the process.
That was measured, twice, before the design changed to detect-and-report.

Camera discovery therefore reads sysfs rather than asking the capture library,
which also makes it safe from a background thread.

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

## Camera settings

The **⚙** next to any camera opens a panel for that one camera, showing every
image control it exposes -- exposure and its mode, gain, brightness, contrast,
gamma, sharpness, white balance, and whatever else that particular model has.
The live preview of the camera being adjusted sits at the top of the panel, so
a change is visible as it is made; the other previews pause while the panel is
open, so one camera has the USB bus and the browser's connections to itself.

Resolution and frame rate are deliberately **not** here. They are chosen per
role in `config.json` (see `DEFAULT_MODES`) and changing one means restarting
the stream, so they are not a knob to turn while recording. Two controls can
still cost frame rate, and the panel says so where it can:

* **Expozíció elsőbbsége** lets the camera drop below its nominal rate to
  expose properly.
* An **expozíciós idő** longer than one frame interval (16.7 ms at 60 fps)
  cannot be delivered at that rate. The panel shows the time in milliseconds
  and warns when it crosses the frame interval.

The values live in the camera -- and only until it loses power, which happens
on every replug and every reboot. So each change is also written to
`camera_controls` in `config.json`, keyed by camera id, and written back into
the camera every time it is opened. Pull a headset out and put it back and it
comes up configured. **Alaphelyzet** puts one camera back to the defaults it
reports and forgets its stored values.

A control the camera refuses is reported and skipped, never retried into a
failed open: losing one setting must not cost the stream. Controls the camera
marks read-only, or that an automatic mode currently owns, are labelled as
such rather than hidden.

### Why the panel is careful

Control transfers on these cameras are slow, and pyuvc performs them with the
GIL held, so one of them stops **every** capture thread in the process, not
just its own. Measured on a Pupil Cam2: ~50 ms to read a control and ~100 ms
to write one; a Pupil Cam1 is roughly ten times quicker. Three consequences
shape the code:

* **Opening the panel costs nothing.** Re-reading a camera's twenty-odd
  controls measured **1.2 s**, during which every camera fell to a few frames
  per second. So the panel serves the values pyuvc read when the camera was
  opened, updated by our own writes; only the control a write just touched is
  read back, to learn what the camera clamped it to. **Frissítés** pays the
  full 1.2 s deliberately and re-reads everything -- the way to see a value an
  automatic mode has moved since the camera opened, or to correct one the
  camera reported wrongly at open. (That happens: a Pupil Cam2 reported
  `Backlight Compensation` as 121 on a control whose range is 0-3, and a
  re-read gave 0.) During a recording the button asks first.
* **Control transfers hold the same lock as opening a camera.** Without that,
  restoring one stored setting at start-up was enough to wedge the recorder:
  the write overlapped another camera's `uvc.Capture()`, and the process sat
  with the GIL held, unresponsive, unkillable by SIGTERM, with three of four
  cameras streaming. Reproducible, and gone once the two cannot overlap.
* **Changing a setting mid-recording drops frames** -- about 0.15 s of them on
  every camera. It is allowed, and the panel says so while a recording runs.

Every transfer is carried out by that camera's own capture thread, between two
frames. The HTTP thread queues the request and waits: touching the capture
handle from another thread would race the reopen path, which closes and
replaces it.

## Managing recordings

The table under the previews lists what has been recorded, with its size, and
each row can be renamed or deleted.

**Renaming changes the label, not the timestamp.** A directory is
`<date>_<time>` plus an optional label, and the stamp is what orders this table
and what ties the directory to the times written inside it -- so the editor
shows it as fixed context and only the label is typed. The same rule governs
what a label may contain whether it is chosen at the start of a recording or
years later: letters (accented ones included), digits, `-` and `_`, up to 40
characters, spaces turned into underscores. `recording.json` carries its own
copy of the name, which is rewritten to match; if that fails the rename still
stands and says so, because moving the directory is the real change.

**Deleting is irreversible and asks first**, naming the recording and its size,
and reminding that the sensor CSVs and the GPS track inside go with it. It is
logged. Neither operation will touch a recording that is still running.

The names come from a browser, so they are checked rather than trusted: a name
must be a single path component naming a directory that really sits in the
recordings directory, resolved through `realpath` so a symlink planted there is
not a way out either.

The frames column shows the total and, in red, any camera that recorded
**nothing** -- a recording quietly missing one camera is the failure worth
catching, and it does not survive being one number among four.

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
* **A camera that cannot answer says so.** Asking a wedged or unplugged camera
  for its settings returns an error to the panel within a few seconds instead
  of hanging the request.
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

## GNSS logging

A u-blox receiver on USB is logged **continuously**, whether or not anything is
being recorded, to `gps/<date>_gps.log.csv` -- one file per day, appended across
unplugs. While a recording runs, the same rows are also written into the
recording's directory, so a recording carries its own copy of the track.

```sh
./run_gps.py --probe    # what the receiver is doing right now
sudo cp setup/pupilrec-gps.service /etc/systemd/system/
sudo systemctl enable --now pupilrec-gps
```

Rate is **10 Hz with GPS, GLONASS, Galileo and BeiDou**. The receiver can reach
18 Hz, but only with GPS alone -- `--gps-only` selects that if the extra rate is
worth losing three constellations, which for fix quality it usually is not. It
clamps anything below a 55 ms period to 100 ms.

**The daily log runs at 10 Hz only while something is being recorded.** The
rest of the time it keeps one row every ten seconds. Around the clock at full
rate the log reached 535,000 rows and 68 MB in a single day, nearly all of it
`fix=none` from a receiver sitting indoors -- a rate that earns its keep inside
a recording and almost nowhere else. `--idle-period` changes the interval, and
`--idle-period 0` logs every row as before.

What is *not* slowed down is the receiver. Reconfiguring it would save a little
USB traffic, but a recording would then begin on a stale position and wait for
the receiver to speed up again; at a constant 10 Hz the first row of a recording
is as fresh as every other one, and the map and the health panel stay live while
idle. The recording's own copy of the track is never thinned -- only the
always-on log is, and the health chip says so (`napló 0.1 Hz`) rather than
reporting a rate that is not being written.

A thinned log still records a change of fix the moment it happens, rather than
up to ten seconds later: when a fix appears or is lost is the one thing in an
idle log worth having to the second.

Full rate begins about half a second into a recording, which is how long the
logger takes to notice one started (`STATUS_POLL_S`). The recording's own copy
of the track has always begun at that same moment; what is new is that the
half second before it is now in the daily log at idle resolution rather than at
10 Hz. Measured on the rig: last idle row at 20:42:09.0, recording started at
20:42:12, full rate from 20:42:12.6.

Rows are written even with no fix: indoors the receiver reports `fix=none` with
no position, and logging that documents the gap rather than leaving a silent
hole. Position columns fill in only once the receiver reports a valid fix.

### Host time

`unix_time` is what lines a GNSS row up against a camera frame or a sensor
sample, so it is worth more than the convenient reading. Two things were wrong
with it, both visible by comparing it against `itow_s`, the receiver's own
clock, over one recording:

```
receiver:   every message 0.100 s apart, 250 of them
host time:  0.000 s x99  alternating with  0.200 s x99
```

The read was `read(4096)` against a 0.2 s timeout, so it returned on the
timeout rather than when a message arrived, and every message in it was
stamped with that one moment. Half the rows were therefore up to 100 ms wrong
and consecutive rows shared a timestamp.

Now the read blocks for the first byte of a chunk and takes the time right
there, and where a read still carries more than one message they are spread
apart using the receiver's iTOW -- it is the better clock, so it is the one
asked how far apart its own messages were. A row whose iTOW cannot be trusted,
across a GPS week rollover for instance, keeps the read's own time rather than
a fabricated one.

The same measurement afterwards: gaps clustered on 0.100 s (±2 ms), no two
rows sharing a timestamp, and host time tracking the receiver to within
**1 ms** across the whole recording.

**The receiver is optional.** With none attached the logger waits quietly and
nothing else is affected; unplugging it mid-run is not an error and it is picked
back up automatically, typically within five seconds. Recordings made without it
simply contain no GPS file.

## Map

`/map` (linked from the header) draws the day's GNSS track and the live
position, updating once a second. A date selector reaches earlier days, and
`?date=YYYY-MM-DD` opens one directly. "Követés" keeps the map centred on the
device; panning by hand turns it off.

**Map tiles are cached by this server**, so the tablet needs no internet of its
own -- it always talks to the recorder, which fetches a tile from OpenStreetMap
the first time anyone looks at it and serves it from `tiles/` forever after.
Area already viewed therefore works fully offline. Tiles are fetched **only on
demand**, never by pre-loading an area, which is what the OSM tile usage policy
requires; when a tile is missing and there is no connection, a flat grey square
stands in rather than a broken image.

A day at 10 Hz is close to a million rows, so the track is thinned for display:
a point is kept once the track has moved 2 m or 5 s have passed. A stationary
period costs one point every five seconds instead of fifty. Leaflet is served
from `pupilrec/static/vendor/`, not a CDN.

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
| a headset's USB hub re-enumerates | the cameras report "nincs jel"; a restart picks them back up |
| the recorder freezes inside libuvc | the systemd watchdog restarts it within 20 s -- automatic |
| the GNSS receiver is unplugged | logged as absent, picked up again on its own |

The board's power cycle brought it back every time it was tried here, but it is
not guaranteed: if the board still does not answer, its physical RESET button is
the only remaining option. The panel says so rather than pretending otherwise.

The panel is deliberate about one state: if a recording is running while sensors
are **not** being captured, it says so in red. A green light over a recording
that is quietly missing half its data is the failure worth designing against.

**Never `kill -9` the sensor daemon**: a process killed while the board streams
wedges the board until someone presses its RESET button.

### A camera that vanished is never closed

`close()` on a libuvc handle whose device is gone does not fail -- it blocks,
with the GIL held, which stops every thread in the process including the HTTP
server. It was seen in the field: a headset's USB hub dropped off the bus and
came back six seconds later with new device addresses, the operator pressed
**Kameraszerver újraindítása**, and the shutdown stuck on the one camera whose
handle was stale. The process ignored SIGTERM (a signal handler cannot run
while C code holds the GIL) and only systemd's 90 s stop timeout ended it, so
a six second dropout became a ninety second outage.

So before closing, the worker asks sysfs whether the device is still there
under the same uid -- a camera keeps its USB port across a replug but is given
a new address. If it is gone, the handle is abandoned rather than closed. That
leaks a file descriptor until the process exits, which is a bargain against
freezing the recorder, and the port is opened from scratch next time anyway.

`uvc.Capture()` can block exactly the same way while the recorder is
*starting*, and there nothing inside the process can help: no Python code runs
to notice it. That one is answered from outside, by the watchdog.

## The systemd watchdog

`run.py` pings systemd roughly three times per `WatchdogSec` for as long as it
can still run Python. When the pings stop, systemd kills the service and
restarts it.

That the ping comes from an ordinary Python thread is the whole design. The
failure being guarded against is a C call holding the GIL, which stops every
thread in the process -- so a ping is a direct test of the thing that breaks,
and a frozen recorder cannot fake one. It also covers freezes nobody has seen
yet, because it does not care *where* the process got stuck.

```
WatchdogSec=20        # four missed pings
NotifyAccess=main     # without this systemd drops them: it defaults to
                      # none for a Type=simple service
```

Twenty seconds is a deliberate floor, not a guess. A healthy camera open can
hold the GIL for up to `FIRST_FRAME_TIMEOUT` (4 s) and the opens are
serialised, so four slow-but-recovering cameras must not be mistaken for a
freeze. Measured against the real thing: a start-up where one camera wedged
inside `uvc.Capture()` stopped the pings dead and never sent another.

The watchdog starts before the cameras are touched, so a freeze during
start-up is covered too -- which is the case that hurts, since there is no
recording to lose and no operator input to wait for. A deliberate shutdown
sends `STOPPING=1` first, so a slow but healthy stop is not mistaken for a
freeze, and each ping carries a status line for `systemctl status`:

```
Status: "4/4 cameras streaming, recording 2026-09-09_18-22-31_ut2"
```

None of this requires systemd. Run `run.py` from a terminal and
`NOTIFY_SOCKET` is unset, every call is a no-op, and nothing changes. The
variables are removed from the environment once read, so that ffmpeg and
`systemctl is-active` -- both spawned by the recorder -- cannot answer in its
place. (They try: `systemctl` reports its own `EXIT_STATUS` down an inherited
socket.)

**Installing it takes a `daemon-reload`**, because the watchdog lives in the
unit file, not in the code:

```
sudo cp setup/pupilrec.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart pupilrec
```

Until that is done the code is inert -- systemd sets no `WATCHDOG_USEC`, the
thread returns immediately, and the recorder behaves exactly as before.

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
├── stwin_recording.json  per-sensor row counts, lost bytes, timing
└── <date>_gps.log.csv    the GNSS track for this recording, if a receiver was attached
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
pupilrec/systemd.py    liveness pings, so a frozen recorder gets restarted
pupilrec/static/       the iPad UI
setup/                 udev rules, installer, systemd unit
```
