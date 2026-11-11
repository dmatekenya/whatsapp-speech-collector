# Deploying and running the speech collector

This guide covers the DigitalOcean droplet that runs the WhatsApp bot. It explains how to set it
up, update it and back it up, connect WhatsApp, start a collection run, look after testers and
participants, and run the ASR batch. Commands to run **on your Mac** and **on the droplet** are
labelled. The design of the collection itself (task types, targets, clip limits, QC) is in
`docs/methodology.md`.

**Values specific to this droplet.** If you build a new server, replace them everywhere below.

| What | Value |
|---|---|
| Domain | `bot.zazoai.com` (an A record at Hostinger pointing at the droplet) |
| Server user | `dun` (`id -u` is `1001`, so `APP_UID=1001` in `.env`) |
| App folder on the droplet | `/home/dun/WhatsApp-speech-collector` (capital W and S) |
| Participant number | +1 618-602-0282, on the **Meta WhatsApp Cloud API** |
| Fallback number | the +1 941 number on **Twilio**, used only if Meta is unavailable |

What runs on the droplet (all in Docker, defined in `docker-compose.yml`):

| Service | What it does | Reachable from the internet? |
|---|---|---|
| `caddy` | HTTPS for the domain (automatic certificates); forwards requests to the API | Yes: ports 80 and 443 |
| `api` | Receives WhatsApp webhooks (Meta at `/webhooks/meta`; Twilio fallback at `/whatsapp/webhook`) and serves prompt images | No (only through Caddy) |
| `worker` | Stores voice notes, runs the conversation, sends replies | No |
| `migrate` | Updates the database schema, then exits | No |
| `postgres` | The database (data kept in a Docker volume) | No |
| `redis` | The job queue | No |

Files (voice notes, prompt CSVs, images, participant lists) live in `data/` in the app folder, which
the containers share. Settings live in `.env` in the app folder.

Database commands below use `-U speech -d speech_collector`, the `POSTGRES_USER` and `POSTGRES_DB`
values from `.env.production.example`. If the droplet's `.env` uses other names, change them.

## Contents

1. [First-time setup](#1-first-time-setup)
2. [Routine operations](#2-routine-operations): update, roll back, back up, restore
3. [WhatsApp](#3-whatsapp): Meta webhook, Twilio fallback
4. [Starting a new collection run](#4-starting-a-new-collection-run)
5. [Testers and participants](#5-testers-and-participants): add, override targets, reset, tester
   to contributor, returning contributors
6. [ASR batch (Modal)](#6-asr-batch-modal): transcription and tier-2 scoring
7. [Troubleshooting](#7-troubleshooting)
8. [Local development](#8-local-development)

---

## 1. First-time setup

Done once per server. The current droplet is already set up; keep this section for a rebuild.

### 1.1 Create the droplet

In the DigitalOcean control panel:

1. **Create → Droplets.** Choose Ubuntu 24.04 (LTS) x64, Basic, **4 GB / 2 CPUs**. The image is
   built on the droplet, which needs the memory.
2. **Authentication:** your Mac's SSH key. If it isn't listed, click **New SSH Key** and paste the
   output of `cat ~/.ssh/id_ed25519.pub` (**on your Mac**). No key yet? Run
   `ssh-keygen -t ed25519` first.
3. Optional: **Networking → Reserved IPs → Assign** gives an address that survives rebuilds. Use it
   in the DNS record below.

### 1.2 Point the domain at it (DNS)

1. At Hostinger (the DNS provider for `zazoai.com`), add an **A** record: name `bot`, value the
   droplet's IP, TTL 300.
2. Remove any other A, AAAA, ALIAS or CNAME record for `bot`.
3. Check **on your Mac** (it can take a few minutes):

   ```bash
   dig +short bot.zazoai.com @8.8.8.8     # must print only the droplet's IP
   ```

Don't start the app until this is right: Caddy needs it to get the HTTPS certificate.

### 1.3 Server setup

1. **On your Mac:** `ssh root@<droplet IP>` (type `yes` to accept the fingerprint).
2. **On the droplet**, as root:

   ```bash
   apt update && apt upgrade -y
   adduser dun                                   # skip if the user already exists
   usermod -aG sudo dun
   rsync --archive --chown=dun:dun ~/.ssh /home/dun

   # Firewall: only SSH, HTTP and HTTPS
   ufw allow OpenSSH
   ufw allow 80/tcp
   ufw allow 443/tcp
   ufw allow 443/udp
   ufw enable                                    # answer y

   # Docker and the Compose plugin
   curl -fsSL https://get.docker.com | sh
   usermod -aG docker dun

   # 2 GB of swap, for image builds on a 4 GB droplet
   fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
   echo '/swapfile none swap sw 0 0' >> /etc/fstab
   ```

3. Open a **second** terminal on your Mac and check that `ssh dun@bot.zazoai.com` works before
   logging out as root.
4. As `dun`, **on the droplet**:

   ```bash
   docker run --rm hello-world     # prints "Hello from Docker!"
   docker compose version
   id -u                           # 1001 on this droplet: that's APP_UID
   ```

> Docker publishes container ports past `ufw`. That's fine here: only Caddy's 80 and 443 are
> published. Postgres and Redis never are.

### 1.4 Get the code

1. **On your Mac**, commit and push the version you want to run. The droplet clones from GitHub,
   so anything not pushed won't be there.
2. The repository is private, so the droplet needs a read-only **deploy key**. **On the droplet:**

   ```bash
   ssh-keygen -t ed25519 -C "speech droplet deploy key" -f ~/.ssh/github_deploy -N ""
   cat ~/.ssh/github_deploy.pub
   ```

3. On GitHub, open **dmatekenya/whatsapp-speech-collector → Settings → Deploy keys → Add deploy
   key**. Paste the key, leave **Allow write access** unticked, and click **Add key**.
4. **On the droplet**, use that key for GitHub, then clone into the app folder:

   ```bash
   cat >> ~/.ssh/config <<'EOF'
   Host github.com
       IdentityFile ~/.ssh/github_deploy
       IdentitiesOnly yes
   EOF
   chmod 600 ~/.ssh/config

   ssh -T git@github.com     # "... You've successfully authenticated ..."
   git clone git@github.com:dmatekenya/whatsapp-speech-collector.git /home/dun/WhatsApp-speech-collector
   cd /home/dun/WhatsApp-speech-collector
   mkdir -p data/participants data/prompts data/audio data/asr_batches backups
   ```

### 1.5 Settings (`.env` on the droplet)

1. **On your Mac**, in the project folder, build `.env.production` from
   `.env.production.example`. The script copies only the Twilio settings from your laptop's
   settings, generates a new `MEDIA_SIGNING_SECRET` and `POSTGRES_PASSWORD`, and prints key names
   only, never values:

   ```bash
   python3 scripts/make_env_production.py --dry-run
   python3 scripts/make_env_production.py
   ```

2. Open `.env.production` and fill in by hand:
   - `SUPPORT_CONTACT`;
   - `APP_UID=1001`;
   - the Meta settings and `WHATSAPP_PROVIDER=meta` (section 3.1);
   - `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, if the ASR batch will run (section 6).

   `DOMAIN` and `PUBLIC_BASE_URL` already say `bot.zazoai.com`.
3. Copy it to the droplet as `.env`, readable only by you:

   ```bash
   rsync -av .env.production dun@bot.zazoai.com:/home/dun/WhatsApp-speech-collector/.env
   ssh dun@bot.zazoai.com chmod 600 /home/dun/WhatsApp-speech-collector/.env
   ```

**Only do this once.** After the first start, edit `.env` **on the droplet** (`nano .env`). The
database password is set when Postgres first starts, so a regenerated file would lock the app out
of its own database.

### 1.6 Start and check

**On the droplet:**

```bash
cd /home/dun/WhatsApp-speech-collector
./scripts/deploy.sh
```

The first run builds the image (a few minutes), creates the database, applies the migrations and
starts everything. Then check:

```bash
docker compose ps                     # api, worker, postgres, redis, caddy running; migrate exited (0)
docker compose logs --tail 50 caddy   # look for "certificate obtained successfully"
curl -s https://bot.zazoai.com/health # {"status":"ok"}
```

If HTTPS fails, DNS usually isn't pointing at the droplet yet (1.2), or ports 80/443 are blocked.
Fix that, then run `docker compose restart caddy`.

Next: connect WhatsApp (section 3), set up nightly backups (2.3), then import participants and
prompts (section 4).

---

## 2. Routine operations

### 2.1 Update the code

1. **On your Mac:** run the tests, check that nothing private is staged, then commit and push:

   ```bash
   uv run pytest -q
   git status          # no .env, .env.production, data/ or backups/ files
   git add -A && git commit -m "..." && git push
   ```

2. **On the droplet:** take a backup (migrations may run), then pull:

   ```bash
   cd /home/dun/WhatsApp-speech-collector
   ./scripts/backup_db.sh
   ls -lh backups/ | tail -2              # a new, non-empty .dump file
   git pull --ff-only
   ```

3. If the update adds settings, add them to `.env` (`nano .env`) **before** deploying. This lists
   settings that are in the template but missing from `.env` (names only):

   ```bash
   grep -o '^[A-Z_]*=' .env.production.example | sort > /tmp/a
   grep -o '^[A-Z_]*=' .env | sort > /tmp/b
   comm -23 /tmp/a /tmp/b
   ```

4. Deploy. `deploy.sh` pulls again (nothing new by now), rebuilds, applies new migrations and
   restarts what changed. Data and settings are kept.

   ```bash
   ./scripts/deploy.sh
   ```

5. Check:

   ```bash
   docker compose ps
   docker compose logs migrate --tail 10  # "Running upgrade ..." lines, or nothing to do; no errors
   curl -s https://bot.zazoai.com/health  # {"status":"ok"}
   ```

Settings are read only when the containers start. After changing `.env` or a file in `config/`
(other than `config/asr_batch.yaml`, see section 6), run
`docker compose up -d --force-recreate api worker`. A config change pushed through git is picked
up by `./scripts/deploy.sh`.

### 2.2 Roll back a bad update

`deploy.sh` runs `git pull`, which stops on an older, checked-out commit. To run older code, build
and start it directly instead. **On the droplet:**

1. Find the last good commit, and check whether the bad update added migrations:

   ```bash
   cd /home/dun/WhatsApp-speech-collector
   git log --oneline -5
   git diff --stat <good-commit> HEAD -- migrations/versions    # empty: no new migrations
   ```

2. **Only if it added migrations:** step the database back while the new code is still in place
   (the old code doesn't know the newer schema, and its `migrate` would fail):

   ```bash
   ./scripts/backup_db.sh
   docker compose stop api worker
   docker compose run --rm migrate alembic downgrade -1    # once per new migration file
   ```

   A downgrade removes the new columns and whatever they held. If a migration damaged data,
   restore the backup taken before the update instead (2.4). That also loses anything received
   since.
3. Check out the good commit, then build and start it:

   ```bash
   git checkout <good-commit>
   docker compose build
   docker compose up -d --remove-orphans
   docker compose ps
   ```

4. Later, return to the latest code with `git checkout main`, then `./scripts/deploy.sh`.

### 2.3 Backups

`scripts/backup_db.sh` saves the database to `backups/speech_collector_YYYY-MM-DD_HHMMSS.dump`
and keeps the newest 14. Run it once by hand, then schedule it nightly. **On the droplet:**

```bash
cd /home/dun/WhatsApp-speech-collector
./scripts/backup_db.sh
crontab -e     # choose nano, then add this line:
```

```text
30 2 * * * cd /home/dun/WhatsApp-speech-collector && ./scripts/backup_db.sh >> backups/backup.log 2>&1
```

The dump doesn't include the audio. Keep copies of both off the server. **On your Mac:**

```bash
rsync -av dun@bot.zazoai.com:/home/dun/WhatsApp-speech-collector/backups/ ~/speech-collector-backups/
rsync -av dun@bot.zazoai.com:/home/dun/WhatsApp-speech-collector/data/audio/ ~/speech-collector-audio/
```

Both contain personal data: keep them on an encrypted disk.

### 2.4 Restore a backup

This replaces the current database. **On the droplet:**

```bash
cd /home/dun/WhatsApp-speech-collector
ls -lh backups/                        # pick the file
docker compose stop api worker
docker compose exec -T postgres sh -c 'pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < backups/speech_collector_2026-11-05_023000.dump
docker compose start api worker
```

A backup made by older code has an older schema. If the current code is newer, run
`./scripts/deploy.sh` afterwards so `migrate` brings it up to date. If you're rolling back, check
out the matching code first (2.2).

---

## 3. WhatsApp

Production runs on the **Meta WhatsApp Cloud API**: participants write to +1 618-602-0282, and Meta
calls `https://bot.zazoai.com/webhooks/meta`. Twilio (the +1 941 number) is a fallback only.
Replies always go out on the number and channel the participant wrote to, so both can be
connected at the same time.

### 3.1 Meta settings and webhook

These settings go in the droplet's `.env`. Get the values from Meta, and never paste them into
chats or screenshots.

| Setting | Where it comes from |
|---|---|
| `META_VERIFY_TOKEN` | Any random string you choose. Type the same value into Meta's webhook setup. |
| `META_APP_SECRET` | Meta App Dashboard → App settings → Basic → App secret |
| `META_ACCESS_TOKEN` | The permanent token of the Admin System User (Business settings). The dashboard's temporary token expires within a day. |
| `META_PHONE_NUMBER_ID` | WhatsApp → API Setup: the phone number **ID**, not the phone number |
| `META_WABA_ID` | WhatsApp → API Setup: the WhatsApp Business Account ID |
| `META_GRAPH_VERSION` | The Graph API version (the template has `v23.0`) |
| `WHATSAPP_PROVIDER` | `meta`. This is the channel for messages that answer nothing (e.g. `scripts/send_test_image.py`); replies always use the participant's channel. |

After changing any of them, run `docker compose up -d --force-recreate api worker`.

Connect the webhook **after** deploying, because Meta checks the address immediately:

1. Check that the endpoint is up. **From anywhere**, this should print **403** (wrong token on
   purpose):

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' \
     "https://bot.zazoai.com/webhooks/meta?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1"
   ```

2. In the Meta App Dashboard (use Chrome), go to **Use cases → Customize → Configure Webhooks**:
   - callback URL `https://bot.zazoai.com/webhooks/meta`;
   - verify token = the value of `META_VERIFY_TOKEN`;
   - **Verify and save**;
   - subscribe to the **messages** field (optionally also `account_update` and
     `phone_number_quality_update`).
3. Make sure **Subscribe webhooks** is on for the WhatsApp account that holds the number (in the
   same use case, under registering the WhatsApp phone number).
4. **On the droplet**, `docker compose logs api --tail 10` should show `Meta webhook verified`.
5. Send *Hi* from a registered phone to +1 618-602-0282. `docker compose logs -f worker` shows the
   message being processed and the reply being sent.

To check the token without printing it, run this **on the droplet**:

```bash
cd /home/dun/WhatsApp-speech-collector
set -a; source .env; set +a
curl -s "https://graph.facebook.com/$META_GRAPH_VERSION/$META_PHONE_NUMBER_ID?fields=display_phone_number,verified_name,quality_rating,status" \
  -H "Authorization: Bearer $META_ACCESS_TOKEN"
```

Lessons from the setup:

- The System User that owns the token needs the **Admin** role and full control of both the app
  and the WhatsApp account. Otherwise sending fails with "(#10) Viewer cannot access the provided
  Paid Message Account". Generate a new token after assigning new assets.
- A WhatsApp account created through Twilio can't be used by our app for sending. Our number lives
  in our own account ("Zazoai").
- The WhatsApp account needs a payment method, even though replies within 24 hours are free.
- Meta allows **one webhook URL per app**. Never point the production app anywhere else while
  participants are recording (see section 8).

### 3.2 Twilio (fallback only)

Use this only if the Meta number can't be used. The Twilio settings (`TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER`) are already in `.env` from step 1.5.

1. In the Twilio Console, go to **Messaging → Senders → WhatsApp senders** → the +1 941 number.
   Set **When a message comes in** to `https://bot.zazoai.com/whatsapp/webhook`, method **POST**.
   Delivery status callbacks are requested with each message the bot sends, so nothing else is
   needed.
2. If participants are being moved to the Twilio number, also set `WHATSAPP_PROVIDER=twilio` in
   `.env` and run `docker compose up -d --force-recreate api worker`.
3. Tell participants which number to write to. Their replies come from whichever number they
   wrote to.

---

## 4. Starting a new collection run

Use this for each new group of participants (e.g. a cohort or a weekly run).

### 4.1 Prepare the files (on your Mac)

| Content | Location | Notes |
|---|---|---|
| Participants | `data/participants/<run>.csv` (e.g. `sr-c01.csv`) | Same columns as `data/participants.example.csv`. Phone numbers like `+265 888 72 15 91`. Saved as **CSV UTF-8**. `is_test` false for real participants. Optional `primary_device_type`, `primary_device_brand`, `primary_device_model`. Their `batch` is the file name without `.csv` (e.g. `sr-c01`), unless the import gives `--batch` (e.g. `--batch pilot`) or the CSV has a `batch` column. |
| Sentences | `data/prompts/text/<batch>.csv` | Curated, with no sensitive items. They must pass the import's checks in `config/prompts.yaml`. Keep them short enough to read in well under 45 seconds, the sentence limit in `clip_limits`. |
| Conversation starters | `data/prompts/conversation-starters/<batch>.csv` | Chichewa; columns as in `config/prompts.yaml` |
| Images | `data/prompts/images/<batch>/<theme>/...` | One folder per theme; JPG or PNG. The theme is the folder name, so check its spelling in the dry run |

Participant CSVs contain names and phone numbers. They are never committed to git, emailed or sent
on WhatsApp; delete local copies you don't need.

- **Name participant files after the round**: `pilot.csv`, `round-1.csv`, `round-2.csv`, …, or
  import with `--batch pilot` / `--batch round-1`. `batch` is the only record of the round a
  participant joined in.
- **New prompts go in a new file or folder per batch** (e.g. `sentences-2027-02.csv`,
  `images/2026-12-batch-2/`), or, if you keep one file, give the new rows a different `source`
  value. Either way a batch can be told apart and retired later. Don't append rows to an old file
  without changing `source`.
- Prompts carry over from round to round: a new round doesn't need new prompt files. All active
  prompts of a type form one pool, whatever file they came from.

### 4.2 Plan the run (on your Mac)

1. In `config/assignment_settings.yaml`, review:
   - `targets_minutes.per_participant`: recorded minutes per person (20 for the pilot). A multiple
     of 20 makes every type come out in whole or half minutes. It is a **lifetime total**, not per
     round: counted minutes include everything a participant has ever recorded. A returning
     contributor only records what is left, e.g. with 60 for round 1, a pilot contributor with 20
     minutes records about 40 more (the cap of 60 per person holds automatically).
   - `targets_minutes.split`: normally unchanged (30/25/25/20, so 70% spontaneous).
   - `task_types.<type>.speakers_per_prompt`: raise it if a type would run out of prompts.
   - `clip_limits` and `clip_checks`: per-task length limits and the speech check, normally
     unchanged.
2. The simulation uses **your laptop's** prompt bank, so import the new prompts locally first,
   checking each with `--dry-run`:

   ```bash
   uv run python -m scripts.import_prompts sentences data/prompts/text/<batch>.csv --dry-run
   uv run python -m scripts.import_prompts sentences data/prompts/text/<batch>.csv
   # likewise conversation_starters and images (see 4.5)
   ```

3. Run the simulation with the planned number of participants. It runs on the throwaway
   `speech_collector_test` database and rolls everything back. Check that no type runs out ("no
   prompts for ..."):

   ```bash
   uv run python -m scripts.simulate_task_bank --participants 30 --per-participant 20
   ```

4. Commit and push any config change.

### 4.3 Copy the files to the droplet (on your Mac)

```bash
rsync -av \
  --exclude '.DS_Store' --exclude '_prepared/' \
  --exclude '*_imported.csv' --exclude '*_rejected.csv' \
  data/participants data/prompts \
  dun@bot.zazoai.com:/home/dun/WhatsApp-speech-collector/data/
```

rsync sends the files encrypted over SSH. The prepared image copies and the import reports aren't
copied: the droplet makes its own.

### 4.4 Back up and deploy (on the droplet)

```bash
cd /home/dun/WhatsApp-speech-collector
./scripts/backup_db.sh
./scripts/deploy.sh        # picks up any config change from 4.2
```

### 4.5 Retire old prompts, then import (on the droplet)

1. **Retire content you no longer want assigned.** Prompts are never deleted (history and IDs are
   kept); they're made inactive. First see what's active:

   ```bash
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "select prompt_type, source_file, count(*) from prompts where active group by 1, 2 order by 1, 2;"
   ```

   Then retire by `source_file`, using the values the query shows. For sentences and conversation
   starters it's the CSV's file name. For images it's `<batch>/<file>`, so match the folder with
   `like`:

   ```bash
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "update prompts set active = false where source_file in ('test-dhs.csv', 'conversation_starters_test_en.csv');"
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "update prompts set active = false where prompt_type = 'image' and source_file like 'test/%';"
   ```

   Other ways to retire, when needed:

   ```bash
   # a topic (e.g. hold back political conversation starters)
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "update prompts set active = false where prompt_type = 'conversation_starter' and domain = 'ndale';"
   # single prompts
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "update prompts set active = false where prompt_id in ('S00123', 'C0045');"
   ```

   Retiring only stops new assignments. Recordings, IDs and history are kept, and prompts already
   sent can still be answered. Prompts carry over between rounds, so retire only test sets and
   unsuitable prompts.

2. **Import.** Always run `--dry-run` first, then for real. Run imports in the same order on every
   environment so prompt IDs match.

   ```bash
   # Participants
   docker compose exec api python -m scripts.import_participants data/participants/<run>.csv --dry-run
   docker compose exec api python -m scripts.import_participants data/participants/<run>.csv --write-ids

   # Sentences
   docker compose exec api python -m scripts.import_prompts sentences data/prompts/text/<batch>.csv --dry-run
   docker compose exec api python -m scripts.import_prompts sentences data/prompts/text/<batch>.csv --write-ids

   # Conversation starters
   docker compose exec api python -m scripts.import_prompts conversation_starters \
     data/prompts/conversation-starters/<batch>.csv --dry-run
   docker compose exec api python -m scripts.import_prompts conversation_starters \
     data/prompts/conversation-starters/<batch>.csv --write-ids

   # Images (the prepared WhatsApp copies are made here, on the droplet)
   docker compose exec api python -m scripts.import_prompts images data/prompts/images/<batch> \
     --source ai-generated --dry-run
   docker compose exec api python -m scripts.import_prompts images data/prompts/images/<batch> \
     --source ai-generated --write-ids
   ```

   Each import ends with `RESULT: imported` and reports any rejected rows. Rejected rows are
   written to `<name>_rejected.csv`; add `--strict` to import nothing unless every row is valid.
   Re-importing the same file is safe.

   - **Prompts:** rows that match an existing prompt (normalised text, or image content) are
     skipped and keep their IDs; new ones get the next IDs. Length warnings (`length=short`,
     `length=long`, `all_caps`) are flags, not errors.
   - **Image `--source`** records where the pictures came from (`ai-generated`, `own-photos`,
     `licensed-<provider>`, …). `TBD` is accepted when unknown; fill it in later with
     `update prompts set source = '<source>' where prompt_type = 'image' and source = 'TBD';`.
     Settle it before any dataset release.
   - **Participants:** a phone number that is already registered is reported as a duplicate and
     not added; that person keeps their ID and their recorded minutes (see 5.4 and 5.5).
   - **Welcome:** add `--send-welcome` to the real (not dry-run) participant import to send the
     registration welcome to the participants it creates, or send it separately afterwards (4.7).
3. **Check what's active now:**

   ```bash
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "select prompt_type, count(*) filter (where active) as active, count(*) as total from prompts group by 1;"
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "select participant_id, is_test, status, batch from participants order by participant_id;"
   ```

### 4.6 Close the previous run (optional)

Participants from an earlier run who should stop recording can be set to `completed`. The bot then
answers them with the "completed" message and gives no more tasks. `batch` is the CSV's file name
without `.csv`, or the name given with `--batch` at import:

```bash
docker compose exec postgres psql -U speech -d speech_collector -c \
  "update participants set status = 'completed' where batch = '<previous-run>';"
```

### 4.7 Smoke test, welcome and launch

New participants haven't written to the bot yet, so the bot can only reach them with an approved
WhatsApp **template**: `registration_welcome` ("Hello {first name}, you are registered ... Your
target is {N} minutes ... reply Hi to start"). It goes out on the Meta number. Its name, language
and whether imports send it automatically are in `config/welcome.yaml`.

1. **Check the template is Active** in WhatsApp Manager (Account tools → Message templates). Until
   then every send fails with a Meta error (e.g. 132001) and nothing is retried.
2. **Try it on a tester first** (testers are skipped unless you add `--include-test`; `--force`
   sends even if they have already written to the bot or had it before). **On the droplet:**

   ```bash
   docker compose exec api python -m scripts.send_welcome --include-test --participants <tester ID> --force
   ```

3. From the tester's phone, reply **Hi** to +1 618-602-0282. The summary should show the run's
   target (e.g. 20 minutes, in four lines). Record one clip.
4. Check **on the droplet**:

   ```bash
   docker compose logs -f api worker
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "select id, channel, status from outbound_messages order by id desc limit 3;"   # meta, delivered or read
   ```

5. **Welcome the run's participants**, looking first:

   ```bash
   docker compose exec api python -m scripts.send_welcome --dry-run --batch <run>    # IDs, first names, targets
   docker compose exec api python -m scripts.send_welcome --batch <run>
   ```

   It sends to active, non-test participants who have never been sent the welcome and have never
   written to the bot, and prints each one as sent, failed (with Meta's error) or skipped (and
   why). It exits with an error if any send failed. Run it again to retry the failed ones: those
   already sent are skipped. `--participants P0008,P0009` limits it to some participants;
   `--force` resends to them.
6. Brief participants: save **+1 618-602-0282**, reply **Hi** to the welcome (or send Hi) to start,
   and who to contact with problems.

Each welcome is an `outbound_messages` row with purpose `template:registration_welcome`; the webhook
updates it to delivered or read:

```bash
docker compose exec postgres psql -U speech -d speech_collector -c \
  "select participant_id, status, error_code, sent_at from outbound_messages where purpose = 'template:registration_welcome' order by id;"
```

### 4.8 Daily checks during the run

**On the droplet:**

```bash
docker compose logs -f api worker                        # live activity
docker compose exec postgres psql -U speech -d speech_collector -c \
  "select channel, status, count(*) from outbound_messages where created_at > now() - interval '1 day' group by 1, 2;"
docker compose exec postgres psql -U speech -d speech_collector -c \
  "select qc_status, qc_reason, length_flag, count(*) from recordings where created_at > now() - interval '1 day' group by 1, 2, 3;"
```

These are worth a closer look:

- `failed` or `skipped_window` outbound messages;
- many `too_short`, `too_long`, `no_speech` or `not_recorded` recordings;
- many `long_sentence` flags (sentences that take too long to read).

---

## 5. Testers and participants

### 5.1 Add a tester

Testers are ordinary participants with `is_test` true; they're left out of exports and, by default,
of the ASR batch.

1. Put them in a small CSV, e.g. `data/participants/testers.csv`.
2. Copy it to the droplet (4.3) and import it (4.5, with `--dry-run` first).

`is_test` belongs to the **person, not to individual recordings**: it covers everything they have
recorded and will record. It does not stop recordings counting towards targets; it only keeps
their data out of exports and the default ASR batch. To mark a whole test batch:

```bash
docker compose exec postgres psql -U speech -d speech_collector -c \
  "update participants set is_test = true where batch = '<test-batch>';"
```

### 5.2 Override a participant's targets

A short target lets a tester reach every task quickly. There are two kinds of override, both stored
in `participants.attributes.targets_minutes`.

- **A different total, split as configured.** For example, 4 minutes in total:

  ```bash
  docker compose exec postgres psql -U speech -d speech_collector -c \
    "update participants set attributes = jsonb_set(coalesce(attributes, '{}'::jsonb), '{targets_minutes}', '{\"per_participant\": 4}') where participant_id = 'P00XX';"
  ```

- **Explicit minutes per type.** `reset_participant` sets these and also resets the conversation
  (5.3). Types left out keep their configured target.

  ```bash
  docker compose exec api python -m scripts.reset_participant P00XX \
    --targets sentence=1,image=1,conversation_starter=1,free_speech=1
  ```

To remove either override and go back to the configured split:

```bash
docker compose exec api python -m scripts.reset_participant P00XX --clear-targets
```

### 5.3 Reset a stuck participant

`reset_participant` does three things:

- closes their active session (abandoned);
- expires their unanswered prompts, which go back to the pool;
- resets their conversation, so their next message gets the first-time welcome.

Recordings and progress are kept. It asks for confirmation unless the participant is a tester or
you add `--yes`. **On the droplet:**

```bash
docker compose exec api python -m scripts.reset_participant P00XX --dry-run
docker compose exec api python -m scripts.reset_participant P00XX
```

### 5.4 A tester becomes a real contributor

One participant record can't be partly test and partly real, and phone numbers are unique across
**all** participants, active, withdrawn or test. So a tester who joins a round as a real contributor
gets a **new ID**:

1. Retire the test record and free the phone number (the `-test` suffix means it can never match an
   incoming WhatsApp number again):

   ```bash
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "update participants set status = 'withdrawn', is_test = true, phone_number = phone_number || '-test' where participant_id = 'P00XX';"
   ```

2. Import the round's file as usual (4.5). They get a new P-number, start from zero minutes, and
   their test recordings stay under the old, test-only ID.

Don't do this for testers who keep testing (e.g. the project team's phones): the bot would no longer
recognise their number.

### 5.5 Returning contributors

A contributor who takes part in another round keeps their ID and phone number. Leave them out of the
new round's file, or let the import report them as duplicates. Targets are lifetime totals (4.2), so
they record only what is left up to `per_participant`. `batch` keeps the round they first joined in;
work out minutes per round (e.g. for payments) from recording dates.

---

## 6. ASR batch (Modal)

Twice a day, the droplet sends new recordings to a Chichewa Whisper model on a Modal GPU and stores
the transcripts in the database:

- `transcripts`: one row per recording and model;
- `asr_batches`: one row per run.

The GPU code runs only on Modal (`scripts/modal_asr_app.py`, deployed from your Mac); the droplet
calls it with the WAV files. Settings are in `config/asr_batch.yaml`, which you can edit on the
droplet without a deploy (see "Change the schedule" below).

### Give the droplet a Modal token

1. In the Modal dashboard, go to **Settings → API Tokens → New Token**. Name it e.g.
   `speech droplet`. A separate token can be revoked without affecting your Mac.
2. **On the droplet**, add both values to `.env` (`nano .env`):

   ```bash
   MODAL_TOKEN_ID=ak-...
   MODAL_TOKEN_SECRET=as-...
   ```

3. Recreate the containers so they read it, and tell the batch wrapper to run inside the api
   container (once):

   ```bash
   cd /home/dun/WhatsApp-speech-collector
   docker compose up -d
   echo 'export ASR_BATCH_RUNNER=docker' >> ~/.bashrc && source ~/.bashrc
   mkdir -p data/asr_batches
   ```

### Deploy the Modal app (on your Mac)

```bash
uv run modal deploy scripts/modal_asr_app.py
```

- **Login:** this uses your Mac's own Modal login (`modal setup`).
- **First call:** downloads the model weights (about 3 GB) into the `hf-model-cache` volume. Later
  calls reuse them.
- **Cost:** idle GPU containers stop after a minute, so the app costs nothing between batches.
- **When to redeploy:** after changing anything in `scripts/modal_asr_app.py` (models or their
  revisions, GPU types, batch size, timeouts, package pins). Changes to `config/asr_batch.yaml` or
  the droplet's code don't need a redeploy.

**Clips over 30 s** (allowed since 2026-11-07, see `clip_limits` in
`config/assignment_settings.yaml`) go through Whisper's long-form decoding. Their transcripts
have `long_form` set and no average log-probability.

- **Redeploy the Modal app before updating the droplet.** An app deployed before 2026-11-08 has no
  long-form decoding and cuts these clips off at 30 s.
- If the droplet runs a batch against such an app, the long clips' transcripts are not stored.
  They count as failed, the log says "Redeploy scripts/modal_asr_app.py", and the next batch
  after the redeploy picks them up.

To compare models on a few long clips without a deploy (`modal run` starts a temporary app), on
your Mac:

```bash
uv run modal run scripts/modal_asr_compare.py --wav <a WAV over 30 s> --models whisper_b,whisper_v1
```

### Run a batch by hand

**On the droplet**, look first, then run:

```bash
cd /home/dun/WhatsApp-speech-collector
./scripts/asr_batch.sh --dry-run          # what would be sent: counts and audio minutes, no Modal call
./scripts/asr_batch.sh                    # transcribe everything not yet transcribed by the default model
```

Options:

- `--since 2026-11-01` / `--until 2026-11-03`: local dates, inclusive.
- `--participant P0012`: repeatable.
- `--recordings 120,121`, `--limit 50`, `--model whisper_v1`.
- `--include-test`: include testers.
- `--force`: transcribe again even if this model already has.

Only one batch runs at a time; a second one exits with a message.

The batch ends with a summary: clips selected, transcribed and failed, audio minutes, wall time, GPU
and model revision. Failed clips get no transcript and are retried by the next batch. The raw output
is in `data/asr_batches/<batch id>/results.jsonl`. It contains transcripts of participants' speech,
so treat it like the audio.

### Schedule it

Cron runs the batch every hour, but it only starts at the slots in `config/asr_batch.yaml`
(`runs_per_day: 2` from `first_run: "02:00"` Malawi time, so 02:00 and 14:00), once per slot.
**On the droplet**, `crontab -e` and add:

```text
5 * * * * cd /home/dun/WhatsApp-speech-collector && ASR_BATCH_RUNNER=docker ./scripts/asr_batch.sh --scheduled >> data/asr_batches/cron.log 2>&1
```

After a slot, check it with `tail data/asr_batches/cron.log`, or in the database:

```bash
docker compose exec postgres psql -U speech -d speech_collector -c \
  "select id, trigger, status, clips_selected, clips_transcribed, clips_failed, started_at from asr_batches order by id desc limit 5;"
```

### Change the schedule, pause, or use another model

- **Change or pause:** edit `config/asr_batch.yaml` **on the droplet** with `nano` and set
  `runs_per_day` (`0` pauses; otherwise 1, 2, 3, 4, 6, 8, 12 or 24).
  - The file is mounted read-only into the containers, so the next hourly check uses it. No deploy
    is needed, and the crontab stays as it is. Check what the container sees with
    `docker compose exec api cat config/asr_batch.yaml`.
  - Use `nano`, which saves the file in place. An editor that writes a new file instead (vim can)
    leaves the containers on the old version until
    `docker compose up -d --force-recreate api worker`.
  - The edit is a local change to the droplet's git checkout. Copy it into the repo on your Mac
    too, or `./scripts/deploy.sh` may stop at `git pull` when the file has also changed on GitHub.
    In that case, run `git checkout config/asr_batch.yaml` on the droplet and deploy again.
- **Another model:** `./scripts/asr_batch.sh --model whisper_v1` transcribes the recordings that
  model hasn't done yet; the earlier transcripts are kept. To make it the default, change
  `modal.model` in the config.
- **Redo with the same model** (e.g. after changing its revision and redeploying):
  `./scripts/asr_batch.sh --force`. New rows are added; the latest per model counts.

### Tier-2 scoring (step 2)

After each ASR batch, the droplet scores the clips it transcribed: a few metrics per clip (SNR,
words per second, model confidence, repeated phrases; for sentences, CER against the prompt). Since
2026-11-11 only sentences get a level from 1 to 4, from CER alone; other task types get the
metrics but no level ("not applicable"). The level is stored in `recordings.qc_tier2`, and the
details in `qc_results`.
It runs on the droplet's CPU. It never rejects a clip, never changes `qc_status`, and sends
nothing to participants. A scoring error doesn't fail the ASR batch: those clips are scored next
time. The design and the levels are in `docs/methodology.md` (section 8). The cut-offs are in
`config/qc_tier2.yaml`.

**First time** (the update that adds it), **on the droplet**:

1. Take a backup, then update. `deploy.sh` rebuilds the image (new packages: numpy, jiwer) and
   applies the migration (`b4e81c6d2a57`):

   ```bash
   cd /home/dun/WhatsApp-speech-collector
   ./scripts/backup_db.sh
   ./scripts/deploy.sh
   docker compose logs migrate --tail 5      # "Running upgrade f3a7c2d9e816 -> b4e81c6d2a57"
   ```

2. Score the clips transcribed so far, looking first:

   ```bash
   docker compose exec api python -m scripts.score_tier2 --dry-run   # computes and prints; writes nothing
   docker compose exec api python -m scripts.score_tier2
   ```

   It prints a report: clips and minutes per task type and level, the most common reasons,
   metric distributions, the sentences with the highest CER, and levels per participant (IDs
   only).

**Any time:**

```bash
docker compose exec api python -m scripts.score_tier2 --report-only                # report from stored results
docker compose exec api python -m scripts.score_tier2 --report-only --sample 10    # + a listening list
docker compose exec api python -m scripts.score_tier2 --participants P0012 --since 2026-11-10
```

`--sample 10` writes `data/qc/tier2-sample-<thresholds_version>.csv`. It lists up to 10 clips per
task type and level, chosen at random with a fixed seed, with their IDs, level, reason, metrics
and WAV path (relative to `data/audio`). Copy it and the clips to your Mac to listen (**on your
Mac**):

```bash
rsync -av dun@bot.zazoai.com:/home/dun/WhatsApp-speech-collector/data/qc/ ~/speech-collector-qc/
```

Test participants are left out unless you add `--include-test`. Only one scoring run at a time:
a second one says so and exits. Scoring after an ASR batch skips while another run is going, and
the next run picks those clips up.

**Change the cut-offs** (e.g. after listening to the sample):

1. Edit `config/qc_tier2.yaml` **on the droplet** with `nano`. It is mounted into the containers
   like `config/asr_batch.yaml`, so no rebuild is needed. Set a new `thresholds_version`, e.g.
   `2026-11-20`.
2. Re-score everything with the new cut-offs. New rows are added and the old ones are kept, so
   the versions can be compared. `qc_tier2` shows the latest.

   ```bash
   docker compose exec api python -m scripts.score_tier2 --rescore --dry-run
   docker compose exec api python -m scripts.score_tier2 --rescore
   ```

3. Copy the edited file into the repo on your Mac and commit it, as for `config/asr_batch.yaml`.
   Otherwise `./scripts/deploy.sh` may stop at `git pull`.

### Tier 3 and the final label

Supervisors' decisions (tier 3) go into `qc_human_reviews`, inserted by the supervision gateway.
Database triggers copy them to `recordings.qc_tier3` and keep `recordings.qc_final` (Bronze,
Silver, Gold, Rejected) up to date (`docs/methodology.md` 8.4–8.5;
`docs/architecture/database.md`, "Triggers and functions"). Nothing in the bot or the scoring
writes `qc_final`.

**Deploying it** (migrations `c7d3a9e5f218`, `d9f4b2c8e631` and `e2a6c4f8b913`), **on the droplet**.
This changes the live bot's arrival code (tier 1 now sets `accepted`, and rejects duplicates), so
deploy between rounds, not mid-round:

1. Take a backup, then update. `deploy.sh` applies the migrations. They backfill `qc_final` for
   every recording, grant the gateway's roles their rights (writer: insert and read
   `qc_human_reviews` only), and move clips the bot has finished from `qc_status` `pending` to
   `accepted` (duplicates to `rejected`, `duplicate`).

   ```bash
   cd /home/dun/WhatsApp-speech-collector
   ./scripts/backup_db.sh
   ./scripts/deploy.sh
   docker compose logs migrate --tail 5      # ... -> d9f4b2c8e631, -> e2a6c4f8b913
   ```

   Afterwards no clip should stay `pending` for more than a few seconds; one that does had its
   turn fail (check the worker log for its message):

   ```bash
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "select id, created_at from recordings where status = 'stored' and qc_status = 'pending'
        and created_at < now() - interval '5 minutes';"
   ```

2. Re-score tier 2 with the CER-only cut-offs (`2026-11-11-cer-only`). Spontaneous clips lose
   their level, and sentences keep a CER level:

   ```bash
   docker compose exec api python -m scripts.score_tier2 --rescore --dry-run
   docker compose exec api python -m scripts.score_tier2 --rescore
   ```

3. Check the labels per task type:

   ```bash
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "select coalesce(a.prompt_type, 'unassigned') as task_type, r.qc_final, count(*), round(sum(r.duration_s)::numeric / 60, 1) as minutes
      from recordings r left join assignments a on a.id = r.assignment_id
      where r.status = 'stored' group by 1, 2 order by 1, 2;"
   ```

4. Check the gateway's roles (the writer must have no SELECT, INSERT, UPDATE or DELETE on
   `recordings`, or the gateway refuses to write):

   ```bash
   docker compose exec postgres psql -U speech -d speech_collector -c \
     "select r.rolname, r.rolcanlogin,
             has_table_privilege(r.rolname, 'qc_human_reviews', 'INSERT') as reviews_insert,
             has_any_column_privilege(r.rolname, 'recordings', 'SELECT') as recordings_select,
             has_any_column_privilege(r.rolname, 'recordings', 'UPDATE') as recordings_update
      from pg_roles r where r.rolname in ('masakhane_qc_writer', 'masakhane_supervision_ro', 'sc_readonly');"
   ```

   The writer stays NOLOGIN, and the gateway's writes stay off, until they have been tested
   together on a copy of the data.

**Correcting a decision:** rows in `qc_human_reviews` can't be changed or deleted (error
`QC405`). Insert a new decision for the recording; the latest one wins. **On the droplet:**

```bash
docker compose exec postgres psql -U speech -d speech_collector -c \
  "insert into qc_human_reviews (recording_id, decision, reason_code, note, reviewer_id, reviewer_name, operation_key)
   values (<recording id>, 'accept', null, 'Corrected by admin', 'admin', 'Dunstan', gen_random_uuid());"
```

**Changing the final-label rules later:** the rules live only in the database function
`qc_final_label()`. In a new Alembic migration:

1. Replace the function: `CREATE OR REPLACE FUNCTION qc_final_label(...)` with the same
   signature.
2. Recompute every row once:

   ```sql
   UPDATE recordings r
   SET qc_final = qc_final_label(r.qc_status, r.qc_tier2, r.qc_tier3,
                                 (SELECT a.prompt_type FROM assignments a WHERE a.id = r.assignment_id)),
       updated_at = now()
   WHERE qc_final IS DISTINCT FROM qc_final_label(r.qc_status, r.qc_tier2, r.qc_tier3,
                                 (SELECT a.prompt_type FROM assignments a WHERE a.id = r.assignment_id));
   ```

   Setting `updated_at` on the changed rows lets the dashboard's incremental sync pick them up.
3. Update `docs/methodology.md` 8.5 and its decision log, and take a backup before deploying.

---

## 7. Troubleshooting

| Symptom | Likely cause | Check or fix |
|---|---|---|
| WhatsApp shows *delivered*, but nothing in the API log | Meta isn't sending to the droplet | In Meta: the webhook URL, the **messages** subscription, and **Subscribe webhooks** on for the WhatsApp account (3.1) |
| API log: "Meta message for a different phone number ID ignored" | A message to another number on the same Meta app | Expected for other numbers; otherwise check `META_PHONE_NUMBER_ID` and recreate api and worker |
| Meta webhook POSTs get 403; log says "invalid or missing signature" | App secret mismatch | `META_APP_SECRET` = Meta App settings → Basic; recreate api and worker |
| Meta webhook POSTs get 503; log says "META_APP_SECRET is not set" | Setting missing | Add it to `.env`; recreate api and worker |
| Webhook verification fails in Meta | Verify token differs, or the app isn't deployed yet | `META_VERIFY_TOKEN` matches what you typed in Meta; run the 403 check in 3.1 |
| Replies `failed`, error `(#10)` | The token's System User lacks rights | Admin role, full control of the app and WhatsApp account, then a new token |
| Replies `skipped_window` | The participant's last message is over 23.5 h old | Expected: they need to message first |
| Welcome `failed`, error 132001 | Template not found under that name and language | Active in WhatsApp Manager? Name and `language` in `config/welcome.yaml` match it exactly (`en` vs `en_US`) |
| Welcome `failed`, error 132000 | Wrong number of values | The approved body must have exactly {{1}} (first name) and {{2}} (minutes) |
| Welcome `failed`, error 132015 or 132016 | Template paused or disabled by Meta (quality) | Check it in WhatsApp Manager; edit or resubmit |
| Worker errors "column ... does not exist" | Migration not applied | `./scripts/deploy.sh` (droplet) or `uv run alembic upgrade head` (laptop) |
| `migrate` exits with an error after a rollback ("Can't locate revision") | The database is newer than the code | Downgrade with the newer code first, or restore an older backup (2.2) |
| Old settings still in effect | Containers weren't recreated | `docker compose up -d --force-recreate api worker` |
| HTTPS fails | DNS or ports | `dig +short bot.zazoai.com @8.8.8.8`; then `docker compose restart caddy` |
| Images don't arrive | WhatsApp can't fetch the image URL | `PUBLIC_BASE_URL=https://bot.zazoai.com`; `docker compose logs caddy`; check with `send_test_image` (below) |
| Meta dashboard won't load or blocks actions | Browser or new-device checks | Use Chrome; wait out "new device" timers instead of retrying |
| `asr_batch.sh`: "uv not found", or "connection refused" on port 5432 | Runner not set, so it ran on the host instead of in the container | `ASR_BATCH_RUNNER=docker` in `~/.bashrc` (then `source ~/.bashrc`) and in the crontab line (section 6); delete any `.venv` it created in the app folder |
| Participant import: phone number already registered | The number belongs to an existing (possibly test) participant | Returning contributor: nothing to do (5.5). Tester becoming a contributor: 5.4 |
| A user can't log in: "password must be changed" / "Authentication token manipulation error" | Forced password change at first login | As an admin: `sudo passwd <user>`, then `sudo chage -d "$(date +%F)" <user>`; check with `sudo chage -l <user>` |

**Check image sending.** **On the droplet**, this prints a signed image URL and sends nothing.
Open the URL in a browser; it should show the picture:

```bash
docker compose exec api python -m scripts.send_test_image --prompt-id I0001 --to +1XXXXXXXXXX --dry-run
```

Without `--dry-run`, it sends the image to that number on `WHATSAPP_PROVIDER`'s channel, or on
`--channel meta` / `--channel twilio`.

**Logs:**

```bash
docker compose logs -f api          # webhook requests (Ctrl+C to stop following)
docker compose logs -f worker       # message processing and replies
docker compose logs migrate         # "Running upgrade ..." lines, or nothing to do
docker compose logs postgres redis
```

---

## 8. Local development

The droplet answers the production number, and your laptop's database is separate from the
droplet's.

**Run the app on your Mac.** Postgres 17 and Redis run as Homebrew services. In separate terminals:

```bash
uv run alembic upgrade head
uv run uvicorn src.api.main:app --port 8000
uv run rq worker inbound outbound --with-scheduler --worker-class rq.worker.SimpleWorker
ngrok http 8000            # or with your reserved ngrok domain: ngrok http --url=<your-domain> 8000
```

**Connect a WhatsApp number to the laptop.** Meta allows **one webhook URL per app**, so the
production app must keep pointing at the droplet while contributors are active. Use one of these:

- **A separate Meta dev app (recommended).** Create a second Meta app (e.g. "Tiyankhuleni dev")
  with Meta's free test number. Point its webhook at `https://<your-ngrok-url>/webhooks/meta`, and
  put its values in your laptop's settings.
- **The production app, only temporarily.** Do this only when no contributors are active (never
  during a run). Point its webhook at ngrok, test, then set it back to
  `https://bot.zazoai.com/webhooks/meta` and check for `Meta webhook verified` in the droplet's
  API log.
- **The Twilio WhatsApp sandbox.** Use the sandbox number (e.g. `+14155238886`) for
  `TWILIO_WHATSAPP_NUMBER` and your ngrok URL for `PUBLIC_BASE_URL` in your laptop's settings.
  Set the **sandbox's** "When a message comes in" URL to `https://<your-ngrok-url>/whatsapp/webhook`.
  Your test phone joins by sending the sandbox's `join <code>` message. Never point the production
  Twilio sender at ngrok.

### Testing the containers on your Mac (optional)

This needs Docker (e.g. Colima), which isn't installed on every development Mac. It runs the same
containers without Caddy, with the API on `localhost`:

```bash
INSTALL_DEV=true docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.local.yml ps
curl http://localhost:8000/health
docker compose -f docker-compose.yml -f docker-compose.local.yml exec api pytest -q
docker compose -f docker-compose.yml -f docker-compose.local.yml down     # stop (data is kept)
```

If your dev server already uses port 8000, put `LOCAL_API_PORT=8001` in front of the commands and
use `localhost:8001`.
