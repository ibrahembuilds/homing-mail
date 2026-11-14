#!/usr/bin/env bash
# Installs the mail server and the campaign service on a Linux VPS.
#
#   cd deploy && sudo ./install.sh
#
# Answers can also be given up front, for unattended installs:
#   DOMAIN=example.com MAIL_HOST=mail.example.com BRAND_NAME="Acme Mail" ./install.sh --yes
#
#   --config-only   write the configuration files and stop (nothing is started)
#   --yes           do not ask questions; use the environment and defaults
#
# Running it again on an existing installation keeps every secret and
# setting in homing.env, rebuilds the campaign service and restarts.
set -euo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")"

CONFIG_ONLY=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --config-only) CONFIG_ONLY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,14p' "$SELF"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }
rand() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

ask() {  # ask VAR "Question" "default"
  local var=$1 prompt=$2 default=${3-} reply
  if [ -n "${!var-}" ]; then return; fi
  if [ "$ASSUME_YES" = 1 ]; then printf -v "$var" '%s' "$default"; return; fi
  read -r -p "  $prompt${default:+ [$default]}: " reply
  printf -v "$var" '%s' "${reply:-$default}"
}

setting() { grep -E "^$1=" homing.env | head -n1 | cut -d= -f2-; }

# --------------------------------------------------------------- preconditions
if [ "$CONFIG_ONLY" = 0 ]; then
  command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install it first:
    curl -fsSL https://get.docker.com | sh"
  docker compose version >/dev/null 2>&1 || die "The Docker Compose plugin is missing (need 'docker compose')."
  docker info >/dev/null 2>&1 || die "Cannot talk to Docker. Run this script with sudo."
fi

# ------------------------------------------------------------- configuration
if [ -f homing.env ]; then
  say "Existing installation found: keeping homing.env"
  DOMAIN=$(setting DOMAIN)
  MAIL_HOST=$(setting HOSTNAMES | cut -d, -f1)
  BRAND_NAME=$(setting BRAND_NAME)
  BRAND_COLOR=$(setting BRAND_COLOR)
  PROXY_SECRET=$(setting CAMPAIGNS_PROXY_SECRET)
  BOUNCE_PASSWORD=$(setting CAMPAIGNS_IMAP_PASSWORD)
  HELLO_PASSWORD=$(setting CAMPAIGNS_REPLY_PASSWORD)
  if ! grep -q '^PUBLIC_IP=' homing.env; then   # added in a later version
    printf '\n# Public address of this server (shown on the domain setup page)\nPUBLIC_IP=%s\n' \
      "$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -n1)" >> homing.env
  fi
  ADMIN_PASSWORD=""
  FRESH=0
else
  say "A few questions"
  ask DOMAIN      "Your domain (addresses will be name@this)" ""
  [ -n "$DOMAIN" ] || die "A domain is required."
  ask MAIL_HOST   "Hostname of this server" "mail.$DOMAIN"
  ask BRAND_NAME  "Product name shown in the interface" "Homing"
  ask BRAND_COLOR "Brand colour (hex)" "#5b3be8"
  ask WEBSITE     "Your website" "https://$DOMAIN"
  ask TZ          "Timezone" "$(cat /etc/timezone 2>/dev/null || echo Etc/UTC)"
  ask ADMIN_PASSWORD "Password for admin@$DOMAIN (empty = generate one)" ""

  echo "$DOMAIN"      | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' || die "\"$DOMAIN\" is not a valid domain name (lower case, no https://)."
  echo "$MAIL_HOST"   | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' || die "\"$MAIL_HOST\" is not a valid hostname."
  echo "$BRAND_COLOR" | grep -Eq '^#[0-9a-fA-F]{6}$' || die "The colour must look like #5b3be8."
  echo "$BRAND_NAME"  | grep -Eq '^[A-Za-z0-9 ._-]{1,40}$' || die "Use only letters, digits, spaces, dots and dashes in the product name."
  echo "$WEBSITE"     | grep -Eq '^https?://[A-Za-z0-9./_-]+$' || die "The website must start with https://"
  echo "$TZ"          | grep -Eq '^[A-Za-z0-9/_+-]+$' || die "Unknown timezone format."
  if [ -z "$ADMIN_PASSWORD" ]; then ADMIN_PASSWORD=$(rand 9); fi
  echo "$ADMIN_PASSWORD" | grep -Eq '^[A-Za-z0-9._@%+=:,-]{8,}$' || die "Password: at least 8 characters from letters, digits and . _ @ % + = : , -"

  PROXY_SECRET=$(rand 24)
  BOUNCE_PASSWORD=$(rand 16)
  HELLO_PASSWORD=$(rand 12)
  PUBLIC_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -n1 || true)
  PROJECT=$(echo "$BRAND_NAME" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9')
  [ -n "$PROJECT" ] || PROJECT=mail

  umask 077
  sed -e "s|__BRAND_NAME__|$BRAND_NAME|g" \
      -e "s|__BRAND_COLOR__|$BRAND_COLOR|g" \
      -e "s|__WEBSITE__|$WEBSITE|g" \
      -e "s|__DOMAIN__|$DOMAIN|g" \
      -e "s|__HOSTNAME__|$MAIL_HOST|g" \
      -e "s|__TZ__|$TZ|g" \
      -e "s|__TLS_FLAVOR__|${TLS_FLAVOR:-letsencrypt}|g" \
      -e "s|__PROJECT__|$PROJECT|g" \
      -e "s|__SECRET_KEY__|$(rand 16)|g" \
      -e "s|__CAMPAIGNS_SECRET__|$(rand 32)|g" \
      -e "s|__PROXY_SECRET__|$PROXY_SECRET|g" \
      -e "s|__BOUNCE_PASSWORD__|$BOUNCE_PASSWORD|g" \
      -e "s|__HELLO_PASSWORD__|$HELLO_PASSWORD|g" \
      -e "s|__PUBLIC_IP__|$PUBLIC_IP|g" \
      -e "s|__ADMIN_PASSWORD__|$ADMIN_PASSWORD|g" \
      homing.env.template > homing.env
  printf 'COMPOSE_PROJECT_NAME=%s\n' "$PROJECT" > .env
  umask 022
  FRESH=1
  note "Wrote homing.env (readable by root only)."
fi

# Proxy routes and webmail name are regenerated on every run.
mkdir -p overrides/nginx overrides/roundcube overrides/postfix overrides/dovecot overrides/rspamd
( umask 077; sed -e "s|__PROXY_SECRET__|$PROXY_SECRET|g" nginx-campaigns.conf.template > overrides/nginx/campaigns.conf )
chmod 644 overrides/nginx/campaigns.conf   # the proxy container reads it as an unprivileged user
printf "<?php\n\$config['product_name'] = '%s Webmail';\n" "$BRAND_NAME" > overrides/roundcube/branding.inc
mkdir -p data/campaigns
chown 1000:1000 data/campaigns 2>/dev/null || warn "Could not hand data/campaigns to user 1000; run this script with sudo."

# Logo in the brand colour. The PNG favicons need Pillow; without it the
# default ones are kept and can be replaced by hand in branding/static/.
if command -v python3 >/dev/null 2>&1 && python3 -c 'import PIL' >/dev/null 2>&1; then
  python3 ../branding/make_icons.py --name "$BRAND_NAME" --color "$BRAND_COLOR" >/dev/null && note "Logo and favicons generated."
else
  sed -i -E "s|(<stop offset=\"0\" stop-color=\")#[0-9a-fA-F]{6}|\1$BRAND_COLOR|" ../branding/static/logo.svg ../branding/static/logo-wordmark*.svg
  note "Logo recoloured (favicons keep the default colour; see branding/make_icons.py)."
fi

if [ "$CONFIG_ONLY" = 1 ]; then
  say "Configuration written. Nothing was started (--config-only)."
  exit 0
fi

# ------------------------------------------------------------------- checks
say "Checking this server"
if command -v getent >/dev/null 2>&1; then
  RESOLVED=$(getent ahostsv4 "$MAIL_HOST" 2>/dev/null | awk 'NR==1{print $1}' || true)
  LOCAL_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -n1 || true)
  if [ -z "$RESOLVED" ]; then
    warn "$MAIL_HOST does not resolve yet. Add an A record for it pointing at this server;"
    warn "the TLS certificate cannot be issued until it does."
  elif [ -n "$LOCAL_IP" ] && [ "$RESOLVED" != "$LOCAL_IP" ]; then
    note "$MAIL_HOST resolves to $RESOLVED (this machine's address is $LOCAL_IP; fine if that is your public IP)."
  else
    note "$MAIL_HOST resolves to $RESOLVED."
  fi
fi
if timeout 8 bash -c 'exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
  note "Outgoing port 25 is open."
else
  warn "Outgoing port 25 looks blocked. Many VPS providers block it by default: ask their"
  warn "support to open it, otherwise no email can be delivered to other providers."
fi
for port in 25 80 443; do
  if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$port\$"; then
    docker compose ps -q front >/dev/null 2>&1 && [ -n "$(docker compose ps -q front 2>/dev/null)" ] \
      || warn "Port $port is already in use by another program. Stop it (for example a web server or postfix) first."
  fi
done

# -------------------------------------------------------------------- start
say "Building and starting (the first run downloads about 1.5 GB)"
docker compose up -d --build

say "Waiting for the mail admin to come up"
ready=0
for _ in $(seq 1 90); do
  if docker compose exec -T admin curl -sf -o /dev/null http://localhost:8080/ping 2>/dev/null; then ready=1; break; fi
  sleep 4
done
[ "$ready" = 1 ] || die "The admin service did not start. Look at: docker compose logs admin resolver"

# Mailbox for bounces and complaints.
# Mailbox you send from and leads reply to; the campaign service reads its replies.
if [ -n "$HELLO_PASSWORD" ] && docker compose exec -T admin flask mailu user hello "$DOMAIN" "$HELLO_PASSWORD" >/dev/null 2>&1; then
  note "Created the mailbox hello@$DOMAIN (your sending address; read it in webmail)."
fi
if docker compose exec -T admin flask mailu user bounces "$DOMAIN" "$BOUNCE_PASSWORD" >/dev/null 2>&1; then
  note "Created the mailbox bounces@$DOMAIN."
else
  note "Mailbox bounces@$DOMAIN already exists."
fi

# Signing key for the domain.
if [ ! -s "data/dkim/$DOMAIN.dkim.key" ]; then
  if printf 'domain:\n  - name: %s\n    dkim_key: -generate-\n' "$DOMAIN" \
       | docker compose exec -T admin flask mailu config-import -u -q >/dev/null 2>&1; then
    note "Generated the DKIM signing key."
  else
    warn "Could not generate the DKIM key automatically. Do it in Mail admin > Mail domains > $DOMAIN > Regenerate keys."
  fi
fi
DKIM_RECORD=$(docker compose exec -T admin flask mailu config-export --dns domain.dns_dkim 2>/dev/null \
              | grep -o 'dkim._domainkey.*' | head -n1 || true)

# --------------------------------------------------------------------- done
say "Installed"
note "Mail admin:  https://$MAIL_HOST/admin      sign in as admin@$DOMAIN"
if [ "$FRESH" = 1 ]; then note "Password:    $ADMIN_PASSWORD    (change it after signing in)"; fi
note "Campaigns:   https://$MAIL_HOST/campaigns/"
if [ "$FRESH" = 1 ] && [ -n "$HELLO_PASSWORD" ]; then
  note "Sending address: hello@$DOMAIN  (webmail password: $HELLO_PASSWORD)"
fi
note "Webmail:     https://$MAIL_HOST/webmail"

say "DNS records to add at your domain registrar (or let Campaigns > Your domain add them on Cloudflare)"
note "$MAIL_HOST.           A      <this server's public IP>"
note "$DOMAIN.              MX     10 $MAIL_HOST."
note "$DOMAIN.              TXT    \"v=spf1 mx a:$MAIL_HOST ~all\""
note "_dmarc.$DOMAIN.       TXT    \"v=DMARC1; p=none; rua=mailto:postmaster@$DOMAIN\""
if [ -n "$DKIM_RECORD" ]; then
  note "$DKIM_RECORD"
else
  note "dkim._domainkey.$DOMAIN.  TXT  <copy from Mail admin > Mail domains > $DOMAIN>"
fi
note "And at your VPS provider: set the reverse DNS (PTR) of the server's IP to $MAIL_HOST"

say "Then"
note "1. Open Campaigns > Domain check and run it until every row is OK."
note "2. Campaigns > Settings: set the sender address and postal address, add your AI key."
note "3. Turn on warm-up in Settings before your first large campaign."
note "Day to day: ./manage.sh status | logs | update | backup  (./manage.sh help)"
