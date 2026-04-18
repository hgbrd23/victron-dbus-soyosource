#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

rm -f $SCRIPT_DIR/current.log

# Kill the running service — svscan will restart it via /service symlink.
# SIGTERM lets the service send its 0W shutdown frames first.
pid=$(pgrep -f "python3? $SCRIPT_DIR/dbus-soyosource.py")
if [ -n "$pid" ]; then
    kill $pid
fi
