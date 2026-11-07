#!/usr/bin/env bash
# =============================================================================
# add_readonly_user.sh — give a team member read-only access to the droplet
# =============================================================================
# Creates a login (SSH key only, no password, no sudo, no docker), installs the
# `tiy` helper, allows that one command in sudoers, makes sure the app's home
# folder and .env are private, and creates a read-only database role that can
# only read tables without names or phone numbers.
#
# Run on the droplet, from the folder holding this script and `tiy`:
#     sudo bash add_readonly_user.sh <username> <public-key-file>
# Example:
#     sudo bash add_readonly_user.sh stewart stewart.pub
# Safe to run again (e.g. to replace the key or re-apply the grants).
set -euo pipefail

# =============================================================================
# Settings
# =============================================================================
APP_USER="dun"
APP_DIR="/home/dun/WhatsApp-speech-collector"
DB_ROLE="sc_readonly"
READABLE_TABLES="recordings prompts sessions assignments qc_results alembic_version"
PUBLIC_PARTICIPANT_COLUMNS="participant_id status is_test batch enrolled_on created_at primary_device_type primary_device_brand primary_device_model"

# =============================================================================
# Checks
# =============================================================================
NEW_USER="${1:-}"
PUBKEY="${2:-}"
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
[[ "$NEW_USER" =~ ^[a-z][a-z0-9_-]{1,30}$ ]] || { echo "Usage: sudo bash $0 <username> <public-key-file>"; exit 1; }
[[ "$NEW_USER" != "$APP_USER" && "$NEW_USER" != "root" ]] || { echo "Choose a new username."; exit 1; }
[[ -f "$PUBKEY" ]] || { echo "Public key file not found: $PUBKEY"; exit 1; }
grep -qE '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+) ' "$PUBKEY" \
  || { echo "$PUBKEY does not look like an SSH public key (it should start with ssh-ed25519)."; exit 1; }
grep -q "PRIVATE KEY" "$PUBKEY" && { echo "That is a PRIVATE key. Ask for the .pub file instead."; exit 1; }
[[ -f ./tiy ]] || { echo "Put the 'tiy' file in this folder first."; exit 1; }
[[ -d "$APP_DIR" ]] || { echo "App folder not found: $APP_DIR"; exit 1; }

# =============================================================================
# 1. Login account (SSH key only)
# =============================================================================
if id "$NEW_USER" >/dev/null 2>&1; then
  echo "User $NEW_USER exists; updating key and access."
else
  adduser --disabled-password --gecos "" "$NEW_USER"
fi
install -d -m 700 -o "$NEW_USER" -g "$NEW_USER" "/home/$NEW_USER/.ssh"
install -m 600 -o "$NEW_USER" -g "$NEW_USER" "$PUBKEY" "/home/$NEW_USER/.ssh/authorized_keys"

# Never in groups that give admin or Docker rights
for g in sudo docker adm lxd; do
  if id -nG "$NEW_USER" | tr ' ' '\n' | grep -qx "$g"; then
    gpasswd -d "$NEW_USER" "$g"
  fi
done

# =============================================================================
# 2. Keep the app folder and secrets private
# =============================================================================
chmod 750 "/home/$APP_USER"
[[ -f "$APP_DIR/.env" ]] && chmod 600 "$APP_DIR/.env"

# =============================================================================
# 3. The tiy helper and its sudoers rule
# =============================================================================
install -m 755 -o root -g root ./tiy /usr/local/bin/tiy
rule_file="/etc/sudoers.d/tiy-$NEW_USER"
tmp_rule="$(mktemp)"
echo "$NEW_USER ALL=($APP_USER) NOPASSWD: /usr/local/bin/tiy" > "$tmp_rule"
visudo -cqf "$tmp_rule" || { echo "sudoers rule failed validation"; rm -f "$tmp_rule"; exit 1; }
install -m 440 -o root -g root "$tmp_rule" "$rule_file"
rm -f "$tmp_rule"

# =============================================================================
# 4. Read-only database role
# =============================================================================
sudo -u "$APP_USER" -H bash -c "cd '$APP_DIR' && docker compose exec -T postgres \
  sh -c 'exec psql -X -q -v ON_ERROR_STOP=1 -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\"'" <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DB_ROLE') THEN
    CREATE ROLE $DB_ROLE LOGIN;
  END IF;
END \$\$;
ALTER ROLE $DB_ROLE SET default_transaction_read_only = on;
ALTER ROLE $DB_ROLE SET statement_timeout = '30s';
SELECT format('GRANT CONNECT ON DATABASE %I TO $DB_ROLE', current_database()) \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO $DB_ROLE;

-- Tables without names or phone numbers (inbound_messages, outbound_messages
-- and participants are deliberately left out)
DO \$\$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY string_to_array('$READABLE_TABLES', ' ') LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON public.%I TO $DB_ROLE', t);
      RAISE NOTICE 'readable: %', t;
    END IF;
  END LOOP;
END \$\$;

-- Participants without name and phone number
DO \$\$ DECLARE cols text; BEGIN
  SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) INTO cols
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = 'participants'
    AND column_name = ANY (string_to_array('$PUBLIC_PARTICIPANT_COLUMNS', ' '));
  EXECUTE 'DROP VIEW IF EXISTS public.participants_public';
  EXECUTE 'CREATE VIEW public.participants_public AS SELECT ' || cols || ' FROM public.participants';
  RAISE NOTICE 'participants_public columns: %', cols;
END \$\$;
GRANT SELECT ON public.participants_public TO $DB_ROLE;
SQL

# =============================================================================
# 5. Test as the new user
# =============================================================================
echo
echo "== Test: tiy tables (as $NEW_USER)"
sudo -u "$NEW_USER" /usr/local/bin/tiy tables
echo
echo "== Test: tiy status (as $NEW_USER)"
sudo -u "$NEW_USER" /usr/local/bin/tiy status
echo
echo "== Check: $NEW_USER cannot read the app folder"
if sudo -u "$NEW_USER" ls "$APP_DIR" >/dev/null 2>&1; then
  echo "WARNING: $NEW_USER can read $APP_DIR"
else
  echo "OK: no access to $APP_DIR"
fi
if grep -qiE '^\s*AllowUsers' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null; then
  echo "NOTE: sshd has an AllowUsers line; add $NEW_USER there or the login will be refused."
fi
echo
echo "Done. $NEW_USER can now log in with: ssh $NEW_USER@<droplet-ip>"
