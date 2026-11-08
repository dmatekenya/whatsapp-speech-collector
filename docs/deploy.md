# Deploying to a DigitalOcean droplet

This guide takes the app from your Mac to a small server ("droplet") on DigitalOcean, for a test
with colleagues. It assumes no prior server experience. Commands to run **on your Mac** and
**on the droplet** are labelled.

What runs on the droplet (all in Docker, defined in `docker-compose.yml`):

| Service | What it does | Reachable from the internet? |
|---|---|---|
| `caddy` | HTTPS for your domain (certificates are automatic), forwards requests to the API | Yes: ports 80 and 443 |
| `api` | Receives Twilio webhooks, serves prompt images | No (only through Caddy) |
| `worker` | Stores voice notes, runs the conversation, sends replies | No |
| `migrate` | Updates the database schema, then exits | No |
| `postgres` | The database (data kept in a Docker volume) | No |
| `redis` | The job queue | No |

Files (voice notes, prompt CSVs, images, participant lists) live in `data/` on the droplet, which
the containers share. Settings live in `.env` on the droplet.

Throughout, replace:

- `speech.zazoai.com` with your subdomain,
- `203.0.113.10` with your droplet's reserved IP,
- `deploy` with the user name you choose in step 3.

---

## 1. Create the droplet

In the DigitalOcean control panel:

1. **Create → Droplets.**
2. **Region:** London (LON1) or Frankfurt (FRA1).
3. **Image:** Ubuntu 24.04 (LTS) x64.
4. **Size:** Basic, Regular. **4 GB / 2 CPUs** is comfortable (the image is built on the droplet,
   which needs memory); 2 GB / 1 CPU works for a small test but builds are slower.
5. **Authentication:** SSH key. If your Mac's key isn't listed yet, click **New SSH Key** and paste
   the output of this (on your Mac):

   ```bash
   cat ~/.ssh/id_ed25519.pub        # or ~/.ssh/id_rsa.pub
   ```

   No key yet? Create one with `ssh-keygen -t ed25519`, press Enter at each question, then run the
   `cat` command above.
6. **Hostname:** e.g. `speech-collector`. Click **Create Droplet**.

Then give it a fixed address that survives rebuilds:

7. **Networking → Reserved IPs**, choose your droplet, **Assign Reserved IP**. Note the address
   (`203.0.113.10` in this guide). Use this address everywhere below, not the droplet's own IP.

## 2. Point your subdomain at it (DNS)

Wherever `zazoai.com`'s DNS is managed (your registrar, Cloudflare, or DigitalOcean's
**Networking → Domains**), add a record:

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `speech` | `203.0.113.10` | 300 (or the lowest offered) |

If you use Cloudflare, set the record to **DNS only** (grey cloud), so Caddy can get its own
certificate.

Check it from your Mac (it can take a few minutes to an hour):

```bash
dig +short speech.zazoai.com            # should print 203.0.113.10
dig +short speech.zazoai.com @1.1.1.1   # same, asking Cloudflare's resolver directly
```

Don't start the app (step 6) until this prints the right address: Caddy needs it to get the HTTPS
certificate.

## 3. First-time server setup

**On your Mac**, log in as root (the first time only):

```bash
ssh root@203.0.113.10
```

Type `yes` if asked to confirm the fingerprint. Now **on the droplet** (as root):

```bash
# Updates
apt update && apt upgrade -y

# A normal user for day-to-day work, with sudo, and your SSH key
adduser deploy                      # choose a password; other questions can be left empty
usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy

# Firewall: only SSH, HTTP and HTTPS
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw enable                          # answer y
ufw status
```

Open a **second** Terminal window on your Mac and check you can log in as the new user before
going further:

```bash
ssh deploy@203.0.113.10
```

From now on, work as `deploy`. Install Docker and the Compose plugin (official instructions for
Ubuntu, **on the droplet**):

```bash
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Let deploy use docker without sudo, then log out and back in for it to apply
sudo usermod -aG docker deploy
exit
```

Log in again (`ssh deploy@203.0.113.10`) and check:

```bash
docker run --rm hello-world     # prints "Hello from Docker!"
docker compose version
id -u                           # note this number; it's APP_UID in step 5 (usually 1000)
```

> **Note on the firewall:** Docker publishes container ports past `ufw`. That's fine here: the only
> published ports are Caddy's 80 and 443, which are meant to be public. Postgres and Redis are
> never published.

## 4. Get the code onto the server

The droplet clones the code from GitHub, so **commit and push the version you want to deploy
first** (on your Mac). Anything not pushed won't be on the server.

The repository is private, so give the droplet a read-only **deploy key**. **On the droplet:**

```bash
ssh-keygen -t ed25519 -C "speech droplet deploy key" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

On GitHub: **dmatekenya/whatsapp-speech-collector → Settings → Deploy keys → Add deploy key**.
Title: `speech droplet`; paste the key; leave **Allow write access** unticked; **Add key**.

Back **on the droplet**, tell SSH to use that key for GitHub, then clone:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/github_deploy
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config

ssh -T git@github.com     # "Hi dmatekenya/whatsapp-speech-collector! You've successfully authenticated..."
git clone git@github.com:dmatekenya/whatsapp-speech-collector.git ~/whatsapp-speech-collector
cd ~/whatsapp-speech-collector
mkdir -p data/participants data/prompts data/audio backups
```

## 5. Copy the settings and data files

### Settings (`.env` on the server)

The server needs its **own** settings: an HTTPS address instead of ngrok, a new signing secret,
and a strong database password. **On your Mac**, in the project folder:

```bash
cp .env.production.example .env.production     # ignored by git
```

Open `.env.production` and fill in every value (the comments explain each one). In particular:

- `DOMAIN=speech.zazoai.com` and `PUBLIC_BASE_URL=https://speech.zazoai.com`
- Twilio: your account SID, auth token and the WhatsApp number the droplet will use
- `MEDIA_SIGNING_SECRET` and `POSTGRES_PASSWORD`: new random values, e.g. from
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- `SUPPORT_CONTACT`, and `APP_UID` (the number `id -u` printed on the droplet)

Copy it to the server as `.env`, readable only by you:

```bash
rsync -av .env.production deploy@203.0.113.10:~/whatsapp-speech-collector/.env
ssh deploy@203.0.113.10 chmod 600 ~/whatsapp-speech-collector/.env
```

### Data files

**On your Mac**, copy the participant and prompt files (but not the image copies made locally,
which the server makes itself, nor the `_imported` / `_rejected` reports):

```bash
rsync -av \
  --exclude '.DS_Store' --exclude '_prepared/' \
  --exclude '*_imported.csv' --exclude '*_rejected.csv' \
  data/participants data/prompts \
  deploy@203.0.113.10:~/whatsapp-speech-collector/data/
```

The participant CSVs contain real names and phone numbers; rsync sends them encrypted over SSH.
Local test recordings in `data/audio` are not copied.

## 6. Start everything and import

**On the droplet:**

```bash
cd ~/whatsapp-speech-collector
./scripts/deploy.sh
```

The first run builds the image (a few minutes), creates the database, applies the migrations and
starts all services. `docker compose ps` at the end should show `api`, `worker`, `postgres`,
`redis` and `caddy` as running (`api`, `postgres` and `redis` as *healthy*), and `migrate` as
*exited (0)*.

Then import the registry and prompts **inside the API container**. Use your own file names; each
import has `--dry-run` to check a file first:

```bash
# Participants
docker compose exec api python -m scripts.import_participants data/participants/test-contributors.csv --dry-run
docker compose exec api python -m scripts.import_participants data/participants/test-contributors.csv --write-ids

# Sentences and conversation starters
docker compose exec api python -m scripts.import_prompts sentences data/prompts/text/test-dhs.csv --write-ids
docker compose exec api python -m scripts.import_prompts conversation_starters \
  data/prompts/conversation-starters/conversation_starters_test_en.csv --write-ids

# Images: the prepared copies (data/prompts/images/_prepared/) are created here, on the server
docker compose exec api python -m scripts.import_prompts images data/prompts/images/test \
  --source ai-generated --write-ids
```

Each command prints a report ending in `RESULT: imported`.

## 7. Check it works

**From anywhere:**

```bash
curl https://speech.zazoai.com/health        # {"status":"ok"}
```

**On the droplet:**

```bash
docker compose ps                   # all running; api, postgres, redis healthy
docker compose logs --tail 50 caddy # look for "certificate obtained successfully"
docker compose logs -f api          # webhook requests (Ctrl+C to stop following)
docker compose logs -f worker       # message processing and replies
docker compose logs migrate         # "Running upgrade ..." lines, or nothing to do
docker compose logs postgres redis
```

To check image sending without a participant, print a signed image URL and open it in a browser
(it should show the picture):

```bash
docker compose exec api python -m scripts.send_test_image --prompt-id I0001 --to +1XXXXXXXXXX --dry-run
```

**If HTTPS fails:** usually DNS isn't pointing at the reserved IP yet (step 2), or ports 80/443
are blocked. Fix that, then `docker compose restart caddy`.

## 8. Switch Twilio to the droplet

In the Twilio Console, open the WhatsApp sender the droplet uses (**Messaging → Senders →
WhatsApp senders**, or **Messaging → Try it out → Send a WhatsApp message → Sandbox settings** for
the sandbox) and set:

- **When a message comes in:** `https://speech.zazoai.com/whatsapp/webhook`, method **POST**.

Nothing else is needed: delivery status callbacks are requested with each message the bot sends.
Send *Hi* from a registered participant's phone; `docker compose logs -f worker` shows the
message being processed and the reply being sent.

## 9. Updating, and backups

### Updating

Push your changes to GitHub (on your Mac), then **on the droplet**:

```bash
cd ~/whatsapp-speech-collector
./scripts/deploy.sh
```

It pulls, rebuilds, applies any new migrations and restarts what changed. Data and settings are
kept.

### Backups

`scripts/backup_db.sh` saves the database to `~/whatsapp-speech-collector/backups/`
(`speech_collector_YYYY-MM-DD_HHMMSS.dump`) and keeps the newest 14. Run it once by hand, then
schedule it nightly:

```bash
./scripts/backup_db.sh
crontab -e     # choose an editor (nano is easiest), then add this line:
30 2 * * * /home/deploy/whatsapp-speech-collector/scripts/backup_db.sh >> /home/deploy/whatsapp-speech-collector/backups/backup.log 2>&1
```

Backups contain personal data. Keep a copy off the server too (**on your Mac**):

```bash
rsync -av deploy@203.0.113.10:~/whatsapp-speech-collector/backups/ ~/speech-collector-backups/
rsync -av deploy@203.0.113.10:~/whatsapp-speech-collector/data/audio/ ~/speech-collector-audio/   # the recordings
```

The database dump doesn't include the audio files; the second command copies those.

**Restoring** a backup replaces the current database. **On the droplet:**

```bash
cd ~/whatsapp-speech-collector
docker compose stop api worker
docker compose exec -T postgres sh -c 'pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < backups/speech_collector_2026-11-02_023000.dump
docker compose start api worker
```

## 10. ASR batch (Modal)

Twice a day the droplet sends new recordings to a Chichewa Whisper model on a Modal GPU and stores
the transcripts in the database (`transcripts`, one row per recording and model; `asr_batches`, one
row per run). The GPU code runs only on Modal (`scripts/modal_asr_app.py`, deployed from your Mac);
the droplet just calls it with the WAV files. Settings are in `config/asr_batch.yaml`, which you can
edit on the droplet without a deploy (see "Change the schedule" below).

### Give the droplet a Modal token

Use a separate token for the droplet, so you can revoke it without affecting your Mac. In the
Modal dashboard: **Settings → API Tokens → New Token** (name it e.g. `speech droplet`). Then
**on the droplet**, add both values to `.env` (`nano .env`):

```bash
MODAL_TOKEN_ID=ak-...
MODAL_TOKEN_SECRET=as-...
```

Containers read `.env` when they're created, so recreate them:

```bash
docker compose up -d
```

And tell the batch wrapper to run inside the api container (once):

```bash
echo 'export ASR_BATCH_RUNNER=docker' >> ~/.bashrc && source ~/.bashrc
mkdir -p data/asr_batches
```

### Deploy the Modal app (on your Mac)

```bash
uv run modal deploy scripts/modal_asr_app.py
```

It uses your Mac's own Modal login (`modal setup`). The first call downloads the model weights
(about 3 GB) into the `hf-model-cache` volume; later calls reuse them. Idle GPU containers stop
after a minute, so the app costs nothing between batches.

Redeploy after changing anything in `scripts/modal_asr_app.py`: the models or their revisions,
GPU types, batch size, timeouts or package pins. Changes to `config/asr_batch.yaml` or the
droplet's code don't need a redeploy.

**Clips over 30 s** (allowed since 2026-11-07, see `clip_limits` in
`config/assignment_settings.yaml`) are not sent yet: the batch app decodes 30 s per clip and would
cut them off. `--dry-run` counts them as "over 30 s (waiting for long-form ASR)". They stay
untranscribed and a later batch picks them up once the app has long-form decoding. That is being
tested first with the compare script, which needs no deploy (`modal run` starts a temporary app):

```bash
uv run modal run scripts/modal_asr_compare.py --wav <a WAV over 30 s> --models whisper_b,whisper_v1
```

### Run a batch by hand

**On the droplet**, look first, then run:

```bash
./scripts/asr_batch.sh --dry-run          # what would be sent: counts and audio minutes, no Modal call
./scripts/asr_batch.sh                    # transcribe everything not yet transcribed by the default model
```

Options: `--since 2026-11-01` / `--until 2026-11-03` (local dates, inclusive), `--participant
P0012` (repeatable), `--recordings 120,121`, `--include-test`, `--limit 50`, `--model whisper_v1`,
`--force` (transcribe again even if this model already has). Only one batch runs at a time; a
second one exits with a message.

It ends with a summary: clips selected, transcribed and failed, audio minutes, wall time, GPU and
model revision. Failed clips get no transcript and are retried by the next batch. The raw output is
in `data/asr_batches/<batch id>/results.jsonl`; it contains transcripts of participants' speech, so
treat it like the audio.

### Schedule it

Cron runs the batch every hour; it only starts at the slots in `config/asr_batch.yaml`
(`runs_per_day: 2` from `first_run: "02:00"` Malawi time: 02:00 and 14:00), once per slot.
**On the droplet:**

```bash
crontab -e     # add this line:
5 * * * * ASR_BATCH_RUNNER=docker /home/deploy/whatsapp-speech-collector/scripts/asr_batch.sh --scheduled >> /home/deploy/whatsapp-speech-collector/data/asr_batches/cron.log 2>&1
```

Check it with `tail data/asr_batches/cron.log` after a slot, or in the database:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT id, trigger, status, clips_selected, clips_transcribed, clips_failed, started_at FROM asr_batches ORDER BY id DESC LIMIT 5"'
```

### Change the schedule, pause, or use another model

- **Change or pause:** edit `config/asr_batch.yaml` **on the droplet** with `nano` and set
  `runs_per_day` (`0` pauses; otherwise 1, 2, 3, 4, 6, 8, 12 or 24). The file is bind-mounted
  read-only into the containers, so the next hourly check uses it: no deploy, and the crontab stays
  as it is. Check what the container sees with `docker compose exec api cat config/asr_batch.yaml`.
  - Use `nano`, which saves the file in place. An editor that writes a new file instead (vim can)
    leaves the containers on the old version until
    `docker compose up -d --force-recreate api worker`.
  - The edit is a local change to the droplet's git checkout. Copy it into the repo on your Mac
    too, or `./scripts/deploy.sh` may stop at `git pull` when the file has also changed on GitHub.
    In that case, `git checkout config/asr_batch.yaml` on the droplet and deploy again.
- **Another model:** `./scripts/asr_batch.sh --model whisper_v1` transcribes the recordings that
  model hasn't done yet; the earlier transcripts are kept. To make it the default, change
  `modal.model` in the config.
- **Redo with the same model** (e.g. after changing its revision and redeploying):
  `./scripts/asr_batch.sh --force`. New rows are added; the latest per model counts.

## 11. Local development afterwards

The droplet now answers your production WhatsApp number. Keep developing on your laptop against
the **Twilio WhatsApp sandbox** instead:

1. In your laptop's `.env`, use the sandbox number for `TWILIO_WHATSAPP_NUMBER` (e.g.
   `+14155238886`) and your ngrok URL for `PUBLIC_BASE_URL`.
2. Run the app as before (`uv run uvicorn …` and `uv run rq worker inbound outbound …`), start
   ngrok, and set the **sandbox's** "When a message comes in" URL to
   `https://<your-ngrok-url>/whatsapp/webhook`.
3. Your test phone joins the sandbox by sending the sandbox's `join <code>` message.

Only one URL can be set per sender, so never point the production sender at ngrok, or the
sandbox at the droplet if you're still testing with it locally.

### Testing the containers on your Mac

To try the same containers locally (Colima), without Caddy, with the API on `localhost`:

```bash
INSTALL_DEV=true docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.local.yml ps
curl http://localhost:8000/health
docker compose -f docker-compose.yml -f docker-compose.local.yml exec api pytest -q
docker compose -f docker-compose.yml -f docker-compose.local.yml down     # stop (data is kept)
```

If your dev server already uses port 8000, add `LOCAL_API_PORT=8001` in front of the commands and
use `localhost:8001`.
