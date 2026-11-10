# Masakhane Speech Quality Supervision Dashboard

A web-based dashboard for monitoring speech collection, reviewing voice recordings, and managing quality control for the Masakhane project.

## Features

- **Contributor management:** View contributors and their recording activity.
- **Supervisor access:** Assign contributors to supervisors; administrators can oversee all contributors.
- **Audio review:** Securely play recordings and submit approval or rejection decisions when production QC write-back is enabled.
- **Prompt categories:** Track reading sentences, pictures, free speech, and conversation starters.
- **Dashboard and reports:** Monitor recording counts, durations, QC outcomes, and progress towards the 500-hour accepted-audio goal.
- **Synchronization:** Refresh authorized recording metadata from the production collector.
- **Audit trail:** Track review decisions and administrative activity.

## Architecture

- **Web dashboard:** PHP application hosted on Hostinger.
- **Production collector:** DigitalOcean-hosted speech collection service with PostgreSQL.
- **Integration:** Authenticated HTTPS gateway for authorized recording metadata, audio playback, and QC updates.

Original recordings remain on the production server; access and review permissions are enforced by the application.

## Getting started

1. Deploy the PHP dashboard on a PHP-compatible web server.
2. Set up its database and required tables according to the deployment documentation.
3. Configure the production API URL and credentials in the protected server configuration.
4. Verify the HTTPS connection, audio playback, and authorized QC write-back before using the review workflow.
5. Sign in as an administrator to create supervisor accounts and assign contributors.

**Security:** Never commit passwords, API tokens, `.env` files, or private configuration files to GitHub. Use the documented server-side configuration and limit access by role.

## Project status

Under active development and deployment.
