#!/bin/zsh
set -eu

cd -- "$(dirname -- "$0")"
game_port="${KOL_TOWN_PORT:-4173}"
game_url="http://127.0.0.1:${game_port}/"

(sleep 1; open "$game_url") &
python3 server.py --port "$game_port" --host 127.0.0.1
