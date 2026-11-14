#!/usr/bin/env bash
# Day-to-day commands for the mail server and campaigns.
#
#   ./manage.sh status            what is running, and whether mail is moving
#   ./manage.sh logs [service]    follow the logs (all, or one: front, smtp, campaigns, campaigns-worker...)
#   ./manage.sh restart           restart everything
#   ./manage.sh stop | start      stop or start everything
#   ./manage.sh update            get the newest version and restart (keeps your settings and data)
#   ./manage.sh backup            save data and settings to backups/<date>.tar.gz
#   ./manage.sh restore <file>    put a backup back (stops everything while it does)
#   ./manage.sh password <email>  set a new password for a mailbox
#   ./manage.sh add-mailbox <email>   create a mailbox (prints its password)
set -euo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }
rand() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

case "${1:-help}" in help|-h|--help) sed -n '2,13p' "$SELF"; exit 0 ;; esac
[ -f homing.env ] || die "Not installed yet. Run ./install.sh first."
command -v docker >/dev/null 2>&1 || die "Docker is not installed."
compose() { docker compose "$@"; }

split_email() {
  case "$1" in *@*.*) LOCAL=${1%@*}; DOMAIN_PART=${1#*@} ;; *) die "\"$1\" is not an email address." ;; esac
}

case "${1:-help}" in
  status)
    compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
    echo
    say "Mail waiting in the server's outgoing queue"
    compose exec -T smtp postqueue -p 2>/dev/null | tail -n 1 || echo "  (could not read the queue)"
    say "Campaign service"
    if compose exec -T campaigns python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/campaigns/health', timeout=3)" 2>/dev/null; then
      echo "  web interface: up"
    else
      echo "  web interface: NOT responding (./manage.sh logs campaigns)"
    fi
    ;;
  logs)
    shift
    compose logs -f --tail=200 "$@"
    ;;
  restart) compose restart ;;
  stop)    compose stop ;;
  start)   compose up -d ;;
  update)
    say "Getting the newest version"
    git -C .. pull --ff-only || die "Could not update the code (local changes?). Run 'git -C .. status'."
    say "Pulling images and rebuilding"
    compose pull --ignore-buildable 2>/dev/null || compose pull || true
    ./install.sh --yes
    ;;
  backup)
    mkdir -p backups
    file="backups/$(date +%Y-%m-%d-%H%M).tar.gz"
    say "Backing up to $file"
    # The campaign database is copied consistently while it runs.
    compose exec -T campaigns python -c "
import sqlite3; src = sqlite3.connect('/data/campaigns.db'); dst = sqlite3.connect('/data/backup.db')
src.backup(dst); dst.close(); src.close()" 2>/dev/null || echo "  (campaign service not running: copying files as they are)"
    tar -czf "$file" homing.env .env overrides data --exclude='data/redis' --exclude='data/campaigns/campaigns.db-wal' \
        --exclude='data/campaigns/campaigns.db-shm'
    rm -f data/campaigns/backup.db
    chmod 600 "$file"
    say "Done: $file ($(du -h "$file" | cut -f1)). Copy it somewhere off this server."
    ;;
  restore)
    [ -n "${2:-}" ] && [ -f "$2" ] || die "Usage: ./manage.sh restore backups/<file>.tar.gz"
    printf 'This replaces the current data and settings with the backup. Type YES to continue: '
    read -r answer
    [ "$answer" = "YES" ] || die "Cancelled."
    compose down
    tar -xzf "$2"
    if [ -f data/campaigns/backup.db ]; then mv data/campaigns/backup.db data/campaigns/campaigns.db; rm -f data/campaigns/campaigns.db-wal data/campaigns/campaigns.db-shm; fi
    compose up -d
    say "Restored from $2"
    ;;
  password)
    [ -n "${2:-}" ] || die "Usage: ./manage.sh password you@yourdomain.com"
    split_email "$2"
    new=$(rand 9)
    compose exec -T admin flask mailu password "$LOCAL" "$DOMAIN_PART" "$new" >/dev/null || die "No mailbox $2."
    say "New password for $2: $new"
    ;;
  add-mailbox)
    [ -n "${2:-}" ] || die "Usage: ./manage.sh add-mailbox sales@yourdomain.com"
    split_email "$2"
    new=$(rand 9)
    compose exec -T admin flask mailu user "$LOCAL" "$DOMAIN_PART" "$new" >/dev/null || die "Could not create $2 (does it exist already, is $DOMAIN_PART added in Mail admin?)."
    say "Created $2 with password: $new"
    ;;
  help|-h|--help) sed -n '2,13p' "$SELF" ;;
  *) die "Unknown command \"$1\". Run ./manage.sh help" ;;
esac
