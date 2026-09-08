# The STWIN sensor board

A STEVAL-STWINKT1B records alongside the cameras. It runs as its own process
and shares nothing with them -- no threads, no locks, no USB bus reservation.

## Identifying the firmware, and why it matters

The board enumerates as:

```
0483:5744 STMicroelectronics STWINKT1B_Multi_Sensor_Streaming
```

That is **FP-SNS-DATALOG2** (confirmed from the board itself:
`fw_name: FP-SNS-DATALOG2_Datalog2, fw_version: 2.0.0`), not the FP-SNS-DATALOG1
firmware most STWIN guides describe. DATALOG1 uses product id `0x5743` and the
name `STWIN Multi-Sensor Streaming`.

This is not a cosmetic difference. ST ships two host libraries, and
`libhs_datalog_v1.so` **hardcodes `0x5743`** -- disassembling it shows a single
immediate, and against this board it silently reports zero devices. Only
`libhs_datalog_v2.so`, which hardcodes `0x5744`, talks to it. Following the
common DATALOG1 instructions here leads to "no device found" with no hint why.

The library comes from `STMicroelectronics/fp-sns-datalog1` (it ships both
versions); `setup/install.sh` copies it into `vendor/`.

## The stream format

`hs_datalog_get_data` returns bytes with two layers, neither documented for this
firmware. Both were established by inspecting live data.

**Layer 1 -- transport blocks.** A run of `[uint32 counter][payload]`, where the
counter is the running total of payload bytes delivered so far. Payload size is
constant per component and is learned from the first block:

| component | payload | | component | payload |
|---|---|---|---|---|
| iis3dwb_acc | 7000 | | iis2dh_acc | 403 |
| imp23absu_mic | 7000 | | iis2mdc_mag | 30 |
| imp34dt05_mic | 4800 | | lps22hh_press/temp | 40 |
| ism330dhcx_acc/gyro | 2000 | | stts751_temp | 12 |

Because the counter is cumulative it doubles as a loss detector: a jump larger
than one payload means the host did not collect fast enough. That number is
reported per component in `stwin_recording.json` as `lost_bytes`, and it is what
caught the microphone problem below.

**Layer 2 -- sensor frames**, once the headers are stripped: `samples_per_ts`
samples of `dim` channels, then one `double` holding the device time at the end
of that frame. Times within a frame are linearly interpolated. Frames are not
aligned to blocks, so the two layers buffer independently.

No sample rate is needed to reconstruct time -- the stream carries its own.

## Why the microphones are WAV and everything else is CSV

The analog microphone runs at 192 kHz. Written as CSV that is ~39 MB/s of text
formatting, and it measurably does not keep up: a 15 second recording produced a
584 MB file and **lost 539 kB of samples**. As WAV the same recording is 5.3 MB
with zero loss.

So microphones are written as WAV plus a `_timestamps.csv` carrying one row per
frame, which is all that is needed to place any audio sample in time. Everything
else is CSV, one file per sensor. `./run_sensors.py --mics csv` forces CSV if
you want it anyway, at the cost above.

One honest caveat: at 192 kHz this firmware does not emit usable per-frame
timestamps -- most arrive zeroed or NaN. Those are replaced by continuing at the
last believable rate (what ST's own tooling does for corrupt stamps) and counted
in `stwin_recording.json` as `bad_timestamps`. For audio at a fixed sample rate
this is sound; the count is reported rather than hidden.

## Measured, all ten sensors at once

15 second recording, running beside all four cameras, **zero lost bytes on every
component** and every timestamp table monotonic:

| component | rate | output | size |
|---|---|---|---|
| iis3dwb_acc | 26 726 Hz | CSV | 24.0 MB |
| imp23absu_mic | 192 000 Hz | WAV | 5.4 MB |
| imp34dt05_mic | 48 000 Hz | WAV | 1.4 MB |
| ism330dhcx_acc | 6 810 Hz | CSV | 6.0 MB |
| ism330dhcx_gyro | 6 810 Hz | CSV | 6.8 MB |
| iis2dh_acc | 1 367 Hz | CSV | 1.2 MB |
| lps22hh_press / temp | 179 Hz | CSV | 0.2 MB |
| iis2mdc_mag | 100 Hz | CSV | 0.1 MB |
| stts751_temp | 8 Hz | CSV | tiny |

About 45 MB per 15 s, roughly 10 GB/hour, on top of the video.

Values were checked against reality, not just parsed: the accelerometers read
1 g at rest, pressure 1001 hPa, temperature 32.4 °C.

## The board wedges if a process is killed mid-log

If the daemon dies while the board is streaming, the board's USB stack stops
responding and neither a port power cycle nor a USB reset revives it -- only the
physical RESET button does. Every exit path therefore stops the log, and
`pupilrec-sensors.service` uses SIGTERM with a 30 s stop timeout for the same
reason. Do not `kill -9` this daemon.

`./run_sensors.py --probe` clears a log left running by a previous crash, so it
is the right first thing to try if the board seems unresponsive.

## Alignment with the video

Sensor logging starts about 0.9 s after the cameras: the daemon learns of the
recording by polling, then has to start the board's log. This does not need
correcting for -- every CSV carries `unix_time` on the same host clock as the
camera tables, so rows line up directly. `device_time` is seconds since the
board's log began, and `stwin_board.json` records the host time of that instant.
