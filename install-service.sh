#!/bin/bash
# Run on the Pi: bash ~/bustracker/install-service.sh
set -euo pipefail
cd /home/pi/bustracker
VENV=/home/pi/bustracker/.venv

# Drop packages that broke the env (niquests needs urllib3-future, which conflicts with requests)
"$VENV/bin/pip" uninstall -y openmeteo-requests openmeteo-sdk niquests urllib3-future datetime 2>/dev/null || true
"$VENV/bin/pip" install -r requirements.txt

# Smoke-test imports
"$VENV/bin/python" -c "import sys; sys.path.append('/home/pi/rpi-rgb-led-matrix/bindings/python'); import requests, dotenv, PIL, rgbmatrix; from google.transit import gtfs_realtime_pb2; print('imports OK')"

sudo cp /home/pi/bustracker/bustracker.service /etc/systemd/system/bustracker.service
sudo systemctl daemon-reload
sudo systemctl enable bustracker.service
sudo systemctl restart bustracker.service
sleep 5
sudo systemctl status --no-pager bustracker.service || true
sudo journalctl -u bustracker.service -n 30 --no-pager
