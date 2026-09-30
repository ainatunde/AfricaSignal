#!/bin/sh
# Load Nigeria's boundaries (country, 37 states, 774 LGAs) and place aliases into the database.
# Usage: scripts/load_places.sh [--cache DIR] [--cities path/to/NG.txt]
# Idempotent: running it again changes nothing. Needs DATABASE_URL and network access to
# media.githubusercontent.com (or a populated cache directory, default data/places).
set -eu
cd "$(dirname "$0")/.."
exec python -m africasignal.places.load "$@"
