#!/bin/sh
# =============
# TEST DATABASE
# =============
# Local testing only (mounted by docker-compose.local.yml): the database pytest
# uses. Runs once, when the postgres volume is first initialised.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -c "CREATE DATABASE speech_collector_test OWNER \"$POSTGRES_USER\";"
