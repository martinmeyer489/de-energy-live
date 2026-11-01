#!/bin/sh
# Energy stack — all services in one container.
#   :8000 proxy.py      public entry (published as 127.0.0.1:8090 on the host)
#   :8001 main.py web   de-energy backend (MaStR API)
#   :8004 price/        static price map
set -eu
cd /app

python3 main.py web --port 8001 &
(cd price && exec python3 -m http.server 8004) &

sh map/build/refresh_loop.sh &
sh price/refresh_loop.sh &

exec python3 proxy.py
