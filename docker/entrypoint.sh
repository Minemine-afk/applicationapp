#!/bin/sh
# Start a virtual screen, a password-protected web view of it, then mcf-apply.
set -e

if [ -z "$VNC_PASSWORD" ]; then
  echo "Set VNC_PASSWORD in .env: it protects the browser view on port 6080." >&2
  exit 1
fi

Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/dev/null 2>&1 &
sleep 1
fluxbox >/dev/null 2>&1 &

mkdir -p /root/.vnc
x11vnc -storepasswd "$VNC_PASSWORD" /root/.vnc/passwd >/dev/null 2>&1
# VNC itself only listens inside the container; the web view (6080) is the way in.
x11vnc -display :99 -rfbauth /root/.vnc/passwd -forever -shared -localhost \
       -rfbport 5900 -quiet >/dev/null 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

for f in config.yml answers.yml; do
  if [ ! -f "/app/$f" ]; then
    echo "/app/$f is missing: create it next to docker-compose.yml (see README → Home server)." >&2
    exit 1
  fi
done

exec "$@"
