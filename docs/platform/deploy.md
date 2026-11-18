# Release verification and rollback

## Staging on the droplet

Use a separate checkout of the reviewed candidate and a fresh staging database.
Copy `.env.staging.example` to `.env.staging` and set a dedicated database
password and a random local signing secret. Keep the production dump accessible
only to the operator. The Compose stack uses its own Postgres, Redis and audio
volume on an internal network. Run the checks inside the API container on
loopback. Leave the production webhook configuration in place.

In the staging checkout, run the following with Bash. Set `dump` to the absolute
path of the production dump on the droplet.

```bash
set -e
staging() { docker compose --env-file .env.staging -f docker-compose.staging.yml "$@"; }
dump=/absolute/path/to/production.dump
export APP_GIT_COMMIT="$(git rev-parse HEAD)"

staging up -d --wait postgres redis
staging exec -T postgres sh -ec '
  pg_restore --single-transaction --no-owner --no-privileges \
    -U "$POSTGRES_USER" -d speech_collector_staging
' < "$dump"
staging build migrate
staging up -d --wait api worker
staging ps -a
staging logs migrate
staging exec -T api python -c 'from urllib.request import urlopen; print(urlopen("http://127.0.0.1:8000/health").read().decode())'
```

Confirm the migration exited successfully and health reports the candidate
commit. Register a fixture participant in staging and generate synthetic audio
for the worker's media download stub.

```bash
staging exec -T api sh -ec '
  printf "name,phone_number,is_test\nStaging Fixture,+12025550123,true\n" > /tmp/staging-fixture.csv
  python scripts/import_participants.py /tmp/staging-fixture.csv --batch staging-smoke --no-send-welcome
  ffmpeg -nostdin -loglevel error -y -f lavfi -i sine=frequency=440:duration=8 \
    -af volume=3 -c:a libopus "$STAGING_META_AUDIO_FIXTURE"
'
```

Use the test suite's signed payloads. Post `hi`, wait until the worker
processes it, then post `ready` if prompted. Once the simulated prompt is
stored as sent, post the voice note. The function prints each inbound message
ID for the database checks.

```bash
post_fixture() {
  staging exec -T api python - "$@" <<'PY'
import os
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4

from tests.meta_payloads import event, signed, text, voice_note

sender = "12025550123"
message = voice_note(sender) if sys.argv[1] == "audio" else text(sender, sys.argv[1])
message.update(id=message["id"] + "." + uuid4().hex, timestamp=str(int(time.time())))
body, headers = signed(event(messages=[message]), secret=os.environ["META_APP_SECRET"])
request = Request("http://127.0.0.1:8000/webhooks/meta", data=body, headers=headers)
with urlopen(request, timeout=10) as response:
    print(response.status, message["id"])
PY
}

post_fixture hi
```

Once the greeting is processed, run `post_fixture ready` if the bot asks.
Wait for the prompt to be recorded as sent before running `post_fixture audio`.
Open the staging database with:

```bash
staging exec postgres sh -ec 'exec psql -X -U "$POSTGRES_USER" -d speech_collector_staging'
```

Use the printed IDs to check `inbound_messages.message_sid`
and confirm each row is `processed`. Join `outbound_messages` by
`inbound_message_id` and verify the reply is `sent` with a `stub-` provider
ID. Join `recordings` by the audio message's inbound ID and verify `status =
'stored'`, a populated `wav_path` and an `enrollment_id` belonging to the
fixture participant. Inspect the worker logs if a job is still pending or failed.

## Production post-deploy checklist

1. Confirm the release commit in `/health` and successful migration in
   `docker compose logs migrate`. Verify the registered handset maps to the
   intended `participants.participant_id` with `is_test = true` and an active
   enrollment. Capture baseline accepted and pending minutes, round progress by
   task, and prompt coverage for non-test participants.
2. From that handset, send **Hi** to the production WhatsApp number. Follow the
   bot's start instructions, send **Ready** when asked, and reply to the
   sentence prompt with a clear voice note of at least five seconds. Confirm
   the prompt and recording acknowledgement arrive on the handset.
3. For the handset's participant ID and test window, check `outbound_messages`
   for the prompt and acknowledgement with status `sent`, `delivered`, or
   `read`, plus a populated `sent_at` and `provider_message_id`. Verify that
   the matching `recordings` row is `stored`, has a `wav_path`, and joins
   through `enrollment_id` to that participant's active enrollment.
4. Compare non-test participant progress with the baseline using the same
   cutoff for prompt coverage. Check that the handset's messages caused no
   changes to those participants' progress.

### Redeploy the previous production tag

For this additive migration, keep the upgraded database. Pause experiment
traffic before switching to the previous code. Run from the production checkout
with Bash and enter the known-good production tag when prompted.

```bash
set -e
read -r -p 'Previous production tag: ' previous_tag
git rev-parse --verify "refs/tags/${previous_tag}^{commit}"
docker compose stop api worker
git switch --detach "refs/tags/${previous_tag}"
APP_GIT_COMMIT="$(git rev-parse HEAD)" docker compose build migrate
docker compose up -d --no-deps --force-recreate api worker
docker compose ps api worker
```

Check production `/health` for the previous commit. The `--no-deps` flag
keeps the previous Alembic runner from starting against the upgraded schema.
