#!/bin/sh
set -e

# Create or migrate the SQLite schema (invoices + void_events) before the API
# starts serving traffic. Uses DATABASE_PATH from the environment.
python -m app.init_db

exec "$@"
