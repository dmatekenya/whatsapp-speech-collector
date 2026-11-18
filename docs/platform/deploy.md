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

## QC calibration verification

The checked-in study flag is `studies.qc_calibration.enabled: false` in
`config/studies/qc_calibration.yaml`. The default deployment keeps the study
closed. Keep the fixed study prompts `S12577` and `C0482` inactive in the main
prompt pool.

Set `STUDY_QC_CALIBRATION_ENABLED=true` in production `.env` or in staging
`.env.staging` to enable the study in that stack. Set it to `false` to switch
the study off, or remove the setting to use the YAML default. The API and
worker read the environment supplied at container startup. After editing the
file, recreate both services so they use the new values:

```bash
# Use this in the production checkout for an approved production toggle.
docker compose up -d --no-deps --force-recreate api worker
# Use this in the staging checkout for a staging toggle.
staging up -d --wait --no-deps --force-recreate api worker
```

For staging verification, first check available RAM and free root disk space.
Restore the latest droplet-side backup into staging as above. Run the main
fixture with the override set to `false`, then set it to `true` in staging
only and recreate the staging API and worker. Keep
`WHATSAPP_SENDS_DISABLED=true`, verify the Compose project uses
its own services and has no published ports, and enroll the synthetic
participant (look up its participant ID by the `staging-smoke` batch):

```bash
read -r -p 'Fixture participant ID: ' fixture_participant_id
staging exec -T api python -m scripts.enroll enroll "$fixture_participant_id" qc_calibration stage1
```

Post a signed text greeting as above. To simulate a button or list selection,
use the actual ID from the latest fixture reply in
`outbound_messages.interactive`. Construct a fixture with `text(sender, "fixture")`,
remove its `text` key, and set `type="interactive"` and
`interactive={"type": kind, kind: {"id": reply_id, "title": "Fixture"}}`, with
`kind` equal to `list_reply` for a menu row or `button_reply` for an action.
Sign and post it to the same loopback endpoint.

Select C3, tap Ready, post audio, then tap Re-record and post audio again. Tap
Next and post the free-speech clip. Confirm the prompt and reply sequence,
attempts 1 and 2, stored audio, and the absence of a sentence slot or assignment
rows. Compare prompt coverage and main progress with a baseline taken before
the fixture turns. Verify replies use stub IDs. Run `staging down -v` and
confirm the staging Postgres volume is gone.

For a separately approved live check, first enable the study in the deployed
configuration. Verify `P0023` is still the registered test handset with
`is_test = true`. Record its active main enrollment and the pre-check progress
and coverage. Enroll it in `qc_calibration` for the test round and repeat the
C3 sequence from the handset on the production WhatsApp number. Verify the
results in the database, then resume its previous main enrollment if it is
still in the study.

### Roll back by reverting the merge

Pause study traffic and resume the main enrollment for participants in the
study before reverting. Create a rollback branch from current `origin/main`.
Revert the Part B merge and retain its migration file in the revert commit.

```bash
set -e
git fetch origin main
git switch -c revert/qc-calibration origin/main
read -r -p 'Part B merge commit: ' merge_commit
git revert -m 1 --no-commit "$merge_commit"
migration=migrations/versions/e81c4d29b702_qc_calibration.py
git restore --source=HEAD --staged --worktree -- "$migration"
git diff --cached --check
git commit -m "Revert QC calibration app and retain its Alembic revision"
```

Review and merge the rollback PR to `main`, then run `./scripts/deploy.sh`
with no arguments from the production `main` checkout. Confirm the checkout
and API container are on the reverted commit, the migrate service exited
successfully, and the API is healthy. The reverted app may report its legacy
version in `/health`; check the checkout SHA and container's
`APP_GIT_COMMIT` as well.

This works because the Part B migration is additive and the previous app can
use the upgraded schema. A migration that changes or removes existing schema
or data needs its own rollback plan.
