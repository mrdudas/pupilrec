#!/usr/bin/env bash
# Set up everything the recorder needs on a fresh Ubuntu machine.
#
# Run from the project root:   ./setup/install.sh
# Needs sudo for the packages, the libuvc install and the udev rules.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/.build"
cd "$ROOT"

echo "==> System packages"
sudo apt-get update -qq
sudo apt-get install -y \
    git cmake pkg-config build-essential \
    libusb-1.0-0-dev libturbojpeg0-dev \
    ffmpeg python3-venv python3-pip usbutils v4l-utils uhubctl

# libuvc: Pupil Labs' fork.  Unlike the kernel's uvcvideo it lets the caller ask
# for a smaller isochronous payload, which is the only way to run both cameras
# of one headset at once (docs/USB-BANDWIDTH.md).
echo "==> libuvc (Pupil Labs fork)"
mkdir -p "$BUILD"
if [ ! -d "${BUILD}/libuvc" ]; then
    git clone --depth 1 https://github.com/pupil-labs/libuvc "${BUILD}/libuvc"
fi
cmake -S "${BUILD}/libuvc" -B "${BUILD}/libuvc/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "${BUILD}/libuvc/build" -j"$(nproc)"
sudo cmake --install "${BUILD}/libuvc/build"

# The fork ships no pkg-config file, which pyuvc's build needs.
echo "==> pkg-config entry for libuvc"
sudo install -d /usr/local/lib/pkgconfig
sudo tee /usr/local/lib/pkgconfig/libuvc.pc > /dev/null <<'EOF'
prefix=/usr/local
exec_prefix=${prefix}
libdir=${exec_prefix}/lib
includedir=${prefix}/include

Name: libuvc
Description: USB Video Class library (Pupil Labs fork)
Version: 0.0.6
Requires.private: libusb-1.0
Libs: -L${libdir} -luvc
Libs.private: -lpthread
Cflags: -I${includedir}
EOF
sudo ldconfig

echo "==> Python environment"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip

# pyuvc must come from git, not PyPI: the published 1.0.0b7 wheel calls the old
# three-argument uvc_open(), which leaves libuvc's `subdevice` field
# uninitialised and makes every open fail with "Device is not UVC-compliant".
echo "==> pyuvc (from git, to match libuvc)"
if [ ! -d "${BUILD}/pyuvc" ]; then
    git clone --depth 1 https://github.com/pupil-labs/pyuvc "${BUILD}/pyuvc"
fi
PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./.venv/bin/pip install "${BUILD}/pyuvc"

# The STWIN board is driven through ST's prebuilt library.  Only the v2 one
# works: v1 hardcodes USB product id 0x5743 and reports zero devices for a
# board that enumerates as 0x5744 (docs/STWIN-SENSORS.md).
echo "==> STWIN support library"
if [ ! -d "${BUILD}/datalog1" ]; then
    git clone --depth 1 https://github.com/STMicroelectronics/fp-sns-datalog1 "${BUILD}/datalog1"
fi
mkdir -p "${ROOT}/vendor"
cp "${BUILD}/datalog1/Utilities/HSDPython_SDK/st_hsdatalog/st_hsdatalog/HSD_link/communication/libhs_datalog/linux/libhs_datalog_v2.so" \
   "${ROOT}/vendor/"

# Lets the web UI restart the services and power-cycle the board.  The sudoers
# rule grants password-less root for exactly the actions this helper names,
# nothing else.
echo "==> recovery helper"
sudo install -m 0755 "${ROOT}/setup/pupilrec-recover" /usr/local/sbin/
sudo install -m 0440 "${ROOT}/setup/pupilrec-sudoers" /etc/sudoers.d/pupilrec
sudo visudo -c -f /etc/sudoers.d/pupilrec

echo "==> udev rules"
sudo install -m 0644 "${ROOT}/setup/70-pupil-cams.rules" /etc/udev/rules.d/
sudo install -m 0644 "${ROOT}/setup/71-stwin-datalog.rules" /etc/udev/rules.d/
sudo install -m 0644 "${ROOT}/setup/72-ublox-gps.rules" /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --action=add
sleep 2

echo
echo "==> Cameras found:"
./.venv/bin/python run.py --list || {
    echo "No cameras detected. Plug the headsets in and re-run: ./run.py --list" >&2
    exit 1
}
echo
echo "Done.  Start the server with:  ./run.py"
