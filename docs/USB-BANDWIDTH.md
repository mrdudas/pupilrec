# Why this project does not use V4L2

Two Pupil Core headsets, four cameras, everything at its maximum rate. On this
machine that is impossible through the kernel's `uvcvideo` driver, and the
reason is worth writing down because the failure looks like a bandwidth problem
that it is not.

## The symptom

Opening a second camera fails with `ENOSPC` (`errno 28`):

```
usb 3-1.4: Not enough bandwidth for new device state.
usb 3-1.4: Not enough bandwidth for altsetting 6
```

## What was measured

Each row is a separate process, so no state leaks between trials:

| Combination | Result |
|---|---|
| world 1080p30 alone | 29.8 fps |
| eye 400x400@120 alone | 120.0 fps |
| world + eye, **same** headset, any resolution down to 640x480 + 192x192 | **always fails** |
| world_A + eye_B (different front ports) | 30.2 / 120.0 fps |
| world_A + world_B | 30.1 / 29.8 fps |
| eye_A + eye_B | 120.2 / 120.1 fps |

The decisive observation: the pair that fails is not the *biggest* pair, it is
any pair **behind the same headset**. Dropping the world camera to 640x480 and
the eye camera to 192x192 does not help. So this is not about how much data the
cameras produce -- the four streams together are only ~17 MB/s on a 480 Mbit/s
bus.

## The actual cause

Both camera types advertise the same isochronous altsettings:

| altsetting | bytes per microframe |
|---|---|
| 1 | 128 |
| 2 | 256 |
| 3 | 800 |
| 4 | 1600 |
| 5 | 2400 |
| 6 | 3072 |

The cameras request a 3640 byte payload no matter which resolution or frame
rate is selected, so `uvcvideo` always lands on **altsetting 6, 3072 bytes per
microframe**. Two of those exceed what xHCI will allocate for one root port,
and each Pupil Core hangs all of its cameras off one internal hub on one port.

`uvcvideo`'s `quirks=128` (`UVC_QUIRK_FIX_BANDWIDTH`) does not help: the fixup
computes its estimate from bits-per-pixel, which is zero for a compressed format
like MJPEG, so it never lowers the request. The kernel offers no other knob.

## The fix

libuvc sets `dwMaxPayloadTransferSize` itself, so it can pick a smaller
altsetting. pyuvc exposes this as `bandwidth_factor`. With it, all four cameras
run at full rate together:

```
Pupil Cam1 ID2@3:6      29.8 fps   6.43 MB/s     (world, 1920x1080)
Pupil Cam1 ID2@3:10     30.0 fps   6.17 MB/s     (world, 1920x1080)
Pupil Cam2 ID1@3:7     120.1 fps   2.42 MB/s     (eye, 400x400)
Pupil Cam2 ID0@3:11    120.2 fps   1.59 MB/s     (eye, 400x400)
                       TOTAL      16.6 MB/s
```

`bandwidth_factor` 2.0, 1.0 and 0.6 all sustain full rate; 0.3 starves the eye
cameras (103 fps instead of 120). The default stays at pyuvc's 2.0.

This is why `setup/70-pupil-cams.rules` unbinds `uvcvideo` from these cameras:
the kernel driver and libuvc cannot both hold them, and the kernel driver cannot
do the job.

## Two traps when building the stack

1. **PyPI's `pupil-labs-uvc` (1.0.0b7) does not work with current libuvc.** It
   links the three-argument `uvc_open()`, while libuvc master reads a
   `dev->subdevice` field that only `uvc_open_subdevice()` initialises. The
   uninitialised value makes the interface scan miss a perfectly valid
   VideoControl interface, and every open fails with *"Device is not
   UVC-compliant"*. Install pyuvc from git so it matches.

2. **libuvc's device handling is not thread-safe.** Opening four cameras from
   four threads at once wedges the process; one camera reports *"Can't start
   isochronous stream"* and the rest hang. `pupilrec/capture.py` serialises open
   and close with a single lock. Frame grabbing runs in parallel unhindered.

## The bandwidth factor is not a constant

`bandwidth_factor` in `config.json` scales the isochronous bandwidth libuvc
reserves per camera. pyuvc's default is 2.0, and how many cameras fit is decided
by it and by *everything else on the same bus*, not by the cameras alone.

Measured on this machine (2026-09-14), all headsets on the one USB 2.0 root hub:

| Cameras | 2.0 | 1.2 |
|---|---|---|
| 7 (3 headsets, one with two eye cameras) | 5 | 6 |
| 4 (the original pair of headsets) | 3 | 4 |

The last row is the surprising one: four cameras ran for days at 2.0, and then
stopped fitting -- because a dock had joined the bus in between, with a
keyboard, the STWIN board and a USB Ethernet adapter on it. Periodic bandwidth
is shared with every device on the bus, so a camera that fits today can stop
fitting tomorrow without anything about the cameras changing.

1.2 measured clean here: four cameras at their full frame rates for two
minutes, no dropped or corrupt frames, and a recording whose videos matched
their timestamp tables frame for frame. Lower it further only with the same
evidence -- too small a reservation shows up as corrupt JPEGs under load, not
as a refusal.

What a camera that does not fit does *not* do is fail: it blocks inside libuvc
with the GIL held and takes the process with it. `pupilrec/quarantine.py` is
what keeps that from ending the recorder rather than the camera.

## What to try when a camera will not come up

1. `./run.py --clear-quarantine`, then restart -- a camera left out after an
   earlier freeze stays out until something changes.
2. Lower `bandwidth_factor` a step (2.0 -> 1.2) and restart.
3. `sudo uhubctl -l <hub> -p <port> -a cycle -d 5` to power-cycle one port.
   `uhubctl` with no arguments lists the hubs and what is on each port; a
   camera showing `connect []` with no vid:pid is one the kernel never managed
   to enumerate, which no setting in this repo can fix.
4. Move a headset to a port on a different controller -- but check `lsusb -t`
   first. On this NUC every USB-A socket, front and back, is the one PCH
   controller; only the rear USB-C ports reach the Thunderbolt controller, and
   only their SuperSpeed lanes do. A USB 2.0 device plugged in there still
   lands on the shared bus. Splitting the cameras across controllers needs a
   real Thunderbolt dock, whose own xHCI is tunnelled over PCIe.

## Reverting

To hand the cameras back to the kernel driver:

```sh
sudo rm /etc/udev/rules.d/70-pupil-cams.rules
sudo udevadm control --reload-rules
sudo modprobe -r uvcvideo && sudo modprobe uvcvideo
```
