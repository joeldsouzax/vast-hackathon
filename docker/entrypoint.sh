#!/bin/sh
set -eu
umask 077
if [ "$#" -eq 0 ]; then
    set -- serve
fi
case "$1" in
    -*) set -- serve "$@" ;;
esac
exec python3 /opt/breadcast/app/studio.py "$@"
