#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename $SCRIPT_DIR)

rm -f /service/$SERVICE_NAME
pid=$(pgrep -f "supervise $SERVICE_NAME")
if [ -n "$pid" ]; then
    kill $pid
fi
chmod a-x $SCRIPT_DIR/service/run
$SCRIPT_DIR/restart.sh
