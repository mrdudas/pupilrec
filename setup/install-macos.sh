#!/bin/zsh
# Install the recorder as a macOS service.  The counterpart of setup/install.sh.
#
#   sudo ./setup/install-macos.sh
#
# What is different from Linux, and why:
#  - the service runs as root, because libusb can only take a UVC camera away
#    from the macOS driver as root (without it every open is "Access denied")
#  - there is no udev, and none is needed: with root there is nothing to grant
#  - there is no systemd watchdog, so a healthcheck job takes its place
set -e

PROJECT="/Users/zsolt/aimotive"
OWNER="zsolt"
GROUP="staff"

if [ "$(id -u)" != "0" ]; then
  echo "run this with sudo" >&2
  exit 1
fi

echo "==> dependencies"
# libusb is what pyuvc talks to; pkg-config is how its build finds libusb.
# ffmpeg muxes the cameras' own JPEGs into Matroska without re-encoding them.
for tool in ffmpeg; do
  command -v $tool >/dev/null || { echo "missing: $tool (brew install $tool)" >&2; exit 1; }
done
[ -f /usr/local/opt/libusb/lib/libusb-1.0.dylib ] || {
  echo "missing libusb (brew install libusb pkg-config)" >&2; exit 1; }
[ -x "$PROJECT/.venv/bin/python" ] || {
  echo "missing venv at $PROJECT/.venv -- see README" >&2; exit 1; }

echo "==> directories"
# setgid plus the group write bit from Umask=2 in the plist: files are written
# by root, and the logged-in user can still delete and copy their recordings.
install -d -o "$OWNER" -g "$GROUP" -m 2775 "$PROJECT/recordings" "$PROJECT/var"

echo "==> recovery helper"
install -o root -g wheel -m 755 \
  "$PROJECT/setup/pupilrec-recover-macos" /usr/local/sbin/pupilrec-recover
install -o root -g wheel -m 755 \
  "$PROJECT/setup/pupilrec-healthcheck" /usr/local/sbin/pupilrec-healthcheck

echo "==> launchd jobs"
install -o root -g wheel -m 644 \
  "$PROJECT/setup/com.pupilrec.plist" /Library/LaunchDaemons/
install -o root -g wheel -m 644 \
  "$PROJECT/setup/com.pupilrec.healthcheck.plist" /Library/LaunchDaemons/

# bootout first so re-running this script replaces a job instead of failing.
launchctl bootout system/com.pupilrec 2>/dev/null || true
launchctl bootout system/com.pupilrec.healthcheck 2>/dev/null || true
launchctl bootstrap system /Library/LaunchDaemons/com.pupilrec.plist
launchctl bootstrap system /Library/LaunchDaemons/com.pupilrec.healthcheck.plist

echo
echo "installed.  the recorder is on http://$(ipconfig getifaddr en0 2>/dev/null || echo 127.0.0.1):8080/"
echo
echo "  launchctl print system/com.pupilrec | head        # state"
echo "  tail -f $PROJECT/var/pupilrec.log                 # log"
echo "  launchctl kickstart -k system/com.pupilrec        # restart"
echo "  launchctl bootout system/com.pupilrec             # stop"
echo
echo "if this machine is a dedicated recorder, stop it sleeping as well --"
echo "USB devices re-enumerate on wake and the recorder loses every camera:"
echo "  sudo pmset -a disablesleep 1"
