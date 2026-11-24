# QC calibration labelling with Label Studio

Label Studio runs on the droplet as its own Compose project, behind the bot's Caddy at
`https://label.zazoai.com`. Raters log in and label blind clips: random file names, no participant
IDs, no conditions. Everything that links a clip back to a recording stays in a private key file
that is never uploaded to Label Studio.

| File | Goes to (on the droplet, repo folder `~/WhatsApp-speech-collector`) |
|---|---|
| `docker-compose.labelling.yml` | repo root |
| `build_labelling_bundle.py` | `scripts/labelling/` |
| `pass1-label-config.xml`, `pass2-label-config.xml` | `config/labelling/` |

Commit these through a branch and PR like other changes, together with the Caddyfile block below.

## 1. DNS

In Hostinger (where zazoai.com's DNS is managed), add an **A record**: `label` → `165.227.73.241`.
Check with `dig +short label.zazoai.com` until it returns the droplet's IP.

## 2. Caddy

Add a second site block to the droplet's `Caddyfile` (keep the existing block and the supervision
route as they are):

```
label.zazoai.com {
	encode gzip
	reverse_proxy label-studio:8080
	request_body {
		max_size 50MB
	}
}
```

Reload Caddy without restarting the bot: `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`.
Until Label Studio is running, that address returns an error; that's expected.

## 3. Start Label Studio

```bash
cd ~/WhatsApp-speech-collector
mkdir -p data/labelling/bundles
cat > .env.labelling <<'EOF'
LABEL_DOMAIN=label.zazoai.com
LABEL_STUDIO_ADMIN_EMAIL=you@example.com
LABEL_STUDIO_ADMIN_PASSWORD=change-me-to-a-long-password
EOF
chmod 600 .env.labelling
free -h
docker compose -p labelling -f docker-compose.labelling.yml --env-file .env.labelling up -d
docker compose -p labelling -f docker-compose.labelling.yml logs -f --tail=50
```

Wait for "Starting development server" or similar, then open `https://label.zazoai.com` and log in
with the admin email and password. `.env.labelling` must not be committed (add it to `.gitignore`).
Label Studio is limited to 1 GB of memory; `free -h` should show at least that much available.

## 4. Pick the pilot sample

Stratified by task type and tier-2 level, from real contributors only (not test accounts, not
study clips), excluding arrival rejections. About 150 sentences and 50 of each other task type.

```bash
docker compose exec -T postgres psql -U speech -d speech_collector -c "\copy (
  select participant_id, recording_id, wav_path, task, tier2 from (
    select r.participant_id, r.id as recording_id, r.wav_path,
           coalesce(a.prompt_type, 'free_speech') as task,
           coalesce(r.qc_tier2::text, 'none') as tier2,
           row_number() over (partition by coalesce(a.prompt_type, 'free_speech'),
                                           coalesce(r.qc_tier2::text, 'none')
                              order by random()) as n
    from recordings r
    join participants p on p.participant_id = r.participant_id and not p.is_test
    join enrollments e on e.id = r.enrollment_id and e.study = 'main'
    left join assignments a on a.id = r.assignment_id
    where r.qc_status in ('accepted', 'needs_review') and r.audio_origin = 'recorded'
      and r.duration_s between 5 and 60
  ) s
  where n <= case when task = 'sentence' then 30 else 10 end
) to stdout with csv header" > data/exports/pilot-sample.csv
tail -n +2 data/exports/pilot-sample.csv | cut -d, -f4,5 | sort | uniq -c
```

The last line shows how many clips per task and tier-2 level. Levels with few clips simply give
fewer; that's fine.

## 5. Build the blind bundle

Export the study clips (if not already done today), then build:

```bash
mkdir -p data/exports
docker compose exec api python -m scripts.export_calibration_clips /app/data/exports/qc-calibration-$(date +%F)
python3 scripts/labelling/build_labelling_bundle.py \
  --calibration data/exports/qc-calibration-$(date +%F)/clips.csv \
  --pilot data/exports/pilot-sample.csv \
  --name qc-2026-11 --exclude P0020,P0023
```

It prints the number of clips and writes `data/labelling/tasks/qc-2026-11-pass1.json`,
`...-pass2.json` and the private `data/labelling/keys/qc-2026-11-key.csv`. Copy the two task
files to your Mac to upload them (step 6):

```bash
scp dun@165.227.73.241:'~/WhatsApp-speech-collector/data/labelling/tasks/qc-2026-11-pass*.json' ~/Downloads/
```

## 6. Set up the projects in Label Studio

**Pass 1: audio labels (all clips)**
1. Create project "QC calibration – pass 1".
2. Labeling setup → Custom template → Code: paste `pass1-label-config.xml` → Save.
3. Settings → Cloud Storage → Add Source Storage → **Local files**:
   - absolute local path: `/label-studio/files/qc-2026-11`
   - leave "Treat every bucket object as a source file" **off**
   - Add storage. **Don't** press Sync (the tasks come from the JSON file).
4. Import → upload `qc-2026-11-pass1.json`.
5. Settings → Annotation: task sampling **Random**, so each rater gets a different order.
6. Settings → General: turn off "Show annotations to others" if that option is present, so raters
   don't see each other's answers.

**Pass 2: transcription (pilot sentence clips)**
Same steps with `pass2-label-config.xml`, project name "QC calibration – pass 2", same local
storage path, and `qc-2026-11-pass2.json`.

Play one task in each project before inviting raters, to confirm the audio loads.

## 7. Raters

Organization → Invite people → copy the invite link and send it to each rater (sign-up only works
through that link). Each rater labels **every** task in pass 1, so with two raters every clip gets
exactly two labels. A third rater can be added later for clips where the two disagree.

Rater instructions: the four questions with their definitions and one example clip per issue tag
(from the labelling manual). Ask raters to work in sessions of about 45 minutes.

## 8. Export and link back

Project → Export → **JSON** (includes rater and answers). On the droplet, match the `clip` field
with `data/labelling/keys/qc-2026-11-key.csv` to get the recording, speaker, condition and tier-2
level. Do the analysis from there; the key never goes into Label Studio.

## Stopping and removing

```bash
docker compose -p labelling -f docker-compose.labelling.yml down        # stop, keep labels
docker compose -p labelling -f docker-compose.labelling.yml down -v     # remove labels too
```

Back up labels before removing: Project → Export, or copy the `labelling_label_studio_data` volume.
When the labelling is finished, delete `data/labelling/bundles/qc-2026-11/` (audio copies).
