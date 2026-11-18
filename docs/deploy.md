# Deploying a release

Use [the platform deployment guide](platform/deployment.md) for the existing
server setup, backups and Meta webhook configuration.

1. Check out the reviewed release candidate on staging. The staging stack uses
   its own Compose project, Postgres database, Redis instance and audio volume.
   Use the example settings to create `.env.staging` with dedicated database
   credentials, Meta test-number credentials and a staging callback URL.
2. Before applying Alembic on an existing staging or production database,
   create a `pg_dump` and verify it can be restored. Start staging with
   `APP_GIT_COMMIT="$(git rev-parse HEAD)" docker compose --env-file .env.staging -f docker-compose.staging.yml up -d --build`.
   The migrate service upgrades the schema before the API and worker start.
3. Check `/health` for the expected version and commit. Send a text reply and
   voice note through the Meta test number. Confirm prompt delivery, recording
   storage, enrollment attribution and production progress.
4. After staging passes, publish the release tag. On the production host,
   take the backup and run `./scripts/deploy.sh v0.1.0`.
5. To roll back application code, check out and build the previous tag, then
   start the previous API and worker with
   `docker compose up -d --no-deps api worker`. Keep the migrated database
   in place. The previous release's migration runner cannot resolve a newer
   Alembic revision. Use a database downgrade only on a disposable test copy.
