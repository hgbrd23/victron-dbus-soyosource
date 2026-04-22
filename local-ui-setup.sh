#!/bin/bash
# local-ui-setup.sh — persistent local HDMI GUI fixes for VenusOS on Raspberry Pi.
#
# Idempotent. Called from /data/rc.local at boot, so the fixes re-apply after
# every Venus OS update (updates wipe /etc/ and /opt/ and recreate the
# /etc/venus/headless flag).
#
# What it does:
#   1. Remounts rootfs r/w so we can edit /etc and /opt. VenusOS ships
#      rootfs read-only by default; this is the same dance Victron's own
#      /opt/victronenergy/swupdate-scripts/remount-rw.sh does.
#
#   2. Removes /etc/venus/headless. Its presence tells
#      /opt/victronenergy/gui/start-gui.sh to render offscreen instead of
#      to the HDMI framebuffer. VenusOS 3.70+ drops the flag entirely —
#      until then the update recreates it on every boot.
#
#   3. Patches six battery-related QML files in /opt/victronenergy/gui/qml/.
#      In VenusOS 3.60–3.66 these still contain `import QtQuick 1.1`, but
#      the gui binary is Qt6-linked and Qt6 has no QtQuick 1.x module.
#      That one broken import cascades through PageMain → PageBattery and
#      stops the whole GUI from loading ("loading QML files failed" in
#      /var/log/gui/current). This matches the community-reported
#      "GUI v1 empty / GUI v2 black" bug (see Victron Community
#      thread 44302) — the root cause is these six files.
#
#   4. Restarts the gui service so the changes take effect. Safe even if
#      the service was already down.
#
# Install:
#   scp local-ui-setup.sh root@venus:/data/local-ui/setup.sh
#   chmod +x /data/local-ui/setup.sh
#   # then add `bash /data/local-ui/setup.sh` to /data/rc.local
#
# HDMI resolution: if the UI shows up but doesn't fill your panel, the
# Raspberry Pi firmware is auto-picking the wrong HDMI mode. Add explicit
# timings to /u-boot/config.txt (this file survives OS updates because
# it's on the FAT boot partition). Example for a 1024x600 panel:
#
#     hdmi_force_hotplug=1
#     hdmi_group=2
#     hdmi_mode=87
#     hdmi_cvt=1024 600 60 3 0 0 0
#     disable_overscan=1
#
# Victron's wiki has more examples; kwindrem/RpiDisplaySetup has an
# interactive mode-selector.

set -e

# 1. rootfs rw
mount -o remount,rw / 2>/dev/null || true

# 2. disable headless
rm -f /etc/venus/headless

# 3. patch Qt6-incompatible QtQuick 1.1 imports in the battery pages
QML_DIR=/opt/victronenergy/gui/qml
for f in PageBattery.qml PageBatteryCellVoltages.qml PageBatteryParameters.qml \
         PageBatterySettings.qml PageBatterySetup.qml PageLynxIonIo.qml; do
    if [ -f "$QML_DIR/$f" ] && grep -q '^import QtQuick 1\.1$' "$QML_DIR/$f"; then
        sed -i 's/^import QtQuick 1\.1$/import QtQuick 2/' "$QML_DIR/$f"
        echo "local-ui-setup: patched $QML_DIR/$f"
    fi
done

# 4. restart the gui service (no-op if it wasn't running)
if [ -d /service/gui ]; then
    svc -t /service/gui 2>/dev/null || true
fi

echo "local-ui-setup: done"
