#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_NAME="inverterscout"
ARCHIVE_PATH="${1:-}"
BIND_ADDRESS="${2:-}"
REQUESTED_WEB_PORT="${3:-}"
INVERTER_HOST="${4:-}"
INVERTER_PORT="${5:-}"
PROBE_MODE="${6:-}"
RELEASE_ID="${7:-}"
BASE_DIR="$HOME/.local/share/inverterscout"
RELEASES_DIR="$BASE_DIR/releases"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
CURRENT_LINK="$BASE_DIR/current"

cleanup() {
  if [[ "$ARCHIVE_PATH" == /tmp/inverterscout-*.tar.gz && -f "$ARCHIVE_PATH" ]]; then
    rm -f -- "$ARCHIVE_PATH"
  fi
}
trap cleanup EXIT

fail() {
  printf 'Remote deployment error: %s\n' "$*" >&2
  exit 1
}

is_valid_port() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535))
}

is_valid_host() {
  local host="$1"
  [[ "$host" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]
}

is_valid_ipv4() {
  local address="$1"
  local first second third fourth extra octet
  IFS=. read -r first second third fourth extra <<<"$address"
  [[ -z "${extra:-}" && -n "${fourth:-}" ]] || return 1
  for octet in "$first" "$second" "$third" "$fourth"; do
    [[ "$octet" =~ ^[0-9]+$ ]] || return 1
    ((10#$octet >= 0 && 10#$octet <= 255)) || return 1
  done
}

address_belongs_to_host() {
  local address="$1"
  [[ "$address" == "127.0.0.1" ]] && return 0
  if command -v ip >/dev/null 2>&1; then
    ip -o address show 2>/dev/null | grep -Fq " $address/"
    return
  fi
  if command -v ifconfig >/dev/null 2>&1; then
    ifconfig 2>/dev/null | grep -Eq "inet[[:space:]]+$address([[:space:]]|$)"
    return
  fi
  return 0
}

port_is_owned_by_project() {
  local port="$1"
  docker ps \
    --filter "label=com.docker.compose.project=$PROJECT_NAME" \
    --filter "label=com.docker.compose.service=inverterscout" \
    --format '{{.Ports}}' 2>/dev/null | grep -Eq ":${port}->8080/tcp"
}

port_is_in_use() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    return
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .
    return
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -an 2>/dev/null | grep -Eq "[.:]${port}[[:space:]].*LISTEN"
    return
  fi
  if command -v timeout >/dev/null 2>&1; then
    timeout 1 bash -c "</dev/tcp/127.0.0.1/$port" >/dev/null 2>&1
    return
  fi
  return 1
}

select_free_port() {
  local candidate="$1"
  local limit=$((10#$candidate + 100))
  ((limit > 65535)) && limit=65535
  while ((candidate <= limit)); do
    if ! port_is_in_use "$candidate" || port_is_owned_by_project "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
    candidate=$((candidate + 1))
  done
  return 1
}

validate_archive() {
  local archive_listing archive_details required
  [[ -f "$ARCHIVE_PATH" ]] || fail "Upload archive not found."
  archive_listing="$(tar -tzf "$ARCHIVE_PATH")" || fail "The upload archive is unreadable."
  if grep -Eq '(^/|(^|/)\.\.(/|$))' <<<"$archive_listing"; then
    fail "The upload archive contains an unsafe path."
  fi
  for required in Dockerfile docker-compose.yml pyproject.toml src/inverterscout/__main__.py .dockerignore; do
    grep -Fxq "$required" <<<"$archive_listing" || fail "Archive is missing $required."
  done
  archive_details="$(tar -tvzf "$ARCHIVE_PATH")" || fail "The upload archive metadata is unreadable."
  if awk '
    substr($1, 1, 1) != "-" && substr($1, 1, 1) != "d" { unsafe = 1 }
    END { exit unsafe ? 0 : 1 }
  ' <<<"$archive_details"; then
    fail "The upload archive contains a link or special file."
  fi
}

probe_inverter() {
  docker compose -p "$PROJECT_NAME" run --rm --no-deps --entrypoint python inverterscout \
    -c 'import socket,sys; connection=socket.create_connection((sys.argv[1], int(sys.argv[2])), 5); connection.close()' \
    "$INVERTER_HOST" "$INVERTER_PORT"
}

wait_for_service() {
  local container_id status attempt
  container_id="$(docker compose -p "$PROJECT_NAME" ps -a -q inverterscout)"
  [[ -n "$container_id" ]] || return 1
  for attempt in $(seq 1 45); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
    case "$status" in
      healthy | running) return 0 ;;
      unhealthy | exited | dead) return 1 ;;
    esac
    sleep 2
  done
  return 1
}

main() {
  local selected_web_port

  [[ -n "${HOME:-}" && "$HOME" == /* ]] || fail "The remote account has no usable home directory."
  [[ "$ARCHIVE_PATH" == /tmp/inverterscout-*.tar.gz ]] || fail "Unexpected upload archive path."
  [[ "$RELEASE_ID" =~ ^[0-9]{14}-[0-9]+$ ]] || fail "Invalid release identifier."
  is_valid_ipv4 "$BIND_ADDRESS" || fail "Invalid Web UI bind address."
  is_valid_port "$REQUESTED_WEB_PORT" || fail "Invalid Web UI port."
  is_valid_host "$INVERTER_HOST" || fail "Invalid inverter host."
  is_valid_port "$INVERTER_PORT" || fail "Invalid inverter port."
  [[ "$PROBE_MODE" == "required" || "$PROBE_MODE" == "skip" ]] || fail "Invalid connectivity-check mode."
  command -v docker >/dev/null 2>&1 || fail "Docker is not installed on the remote host."
  docker info >/dev/null 2>&1 || fail "Docker is installed but unavailable to this SSH account."
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is not installed on the remote host."
  command -v tar >/dev/null 2>&1 || fail "tar is not installed on the remote host."
  address_belongs_to_host "$BIND_ADDRESS" || fail \
    "Bind address $BIND_ADDRESS is not assigned to the remote Docker host."
  validate_archive

  selected_web_port="$(select_free_port "$REQUESTED_WEB_PORT" || true)"
  [[ -n "$selected_web_port" ]] || fail "No free Web UI port was found near $REQUESTED_WEB_PORT."
  if [[ "$selected_web_port" != "$REQUESTED_WEB_PORT" ]]; then
    printf 'Port %s is occupied; using %s instead.\n' "$REQUESTED_WEB_PORT" "$selected_web_port"
  fi

  mkdir -p "$RELEASES_DIR"
  [[ ! -e "$RELEASE_DIR" ]] || fail "Release directory already exists: $RELEASE_DIR"
  [[ ! -e "$CURRENT_LINK" || -L "$CURRENT_LINK" ]] || fail \
    "The managed current path exists but is not a symbolic link: $CURRENT_LINK"
  mkdir -m 0700 "$RELEASE_DIR"
  tar -xzf "$ARCHIVE_PATH" -C "$RELEASE_DIR"
  (
    umask 077
    {
      printf 'INVERTERSCOUT_BIND_ADDRESS=%s\n' "$BIND_ADDRESS"
      printf 'INVERTERSCOUT_WEB_PORT=%s\n' "$selected_web_port"
    } >"$RELEASE_DIR/.env"
  )

  cd "$RELEASE_DIR"
  docker compose -p "$PROJECT_NAME" config --quiet
  docker compose -p "$PROJECT_NAME" build inverterscout

  if [[ "$PROBE_MODE" == "required" ]]; then
    if probe_inverter; then
      printf 'Inverter TCP connection succeeded from the remote container.\n'
    else
      fail "The remote container cannot reach $INVERTER_HOST:$INVERTER_PORT. Check LAN, VLAN, VPN, and firewall routing."
    fi
  else
    printf 'Inverter connectivity test skipped.\n'
  fi

  docker compose -p "$PROJECT_NAME" up -d --no-build
  if ! wait_for_service; then
    docker compose -p "$PROJECT_NAME" logs --tail 80 inverterscout >&2 || true
    fail "InverterScout did not become healthy."
  fi
  ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"

  printf 'Remote release: %s\n' "$RELEASE_DIR"
  printf 'Persistent Docker volume: %s_inverterscout_data\n' "$PROJECT_NAME"
  printf 'INVERTERSCOUT_BIND_ADDRESS=%s\n' "$BIND_ADDRESS"
  printf 'INVERTERSCOUT_WEB_PORT=%s\n' "$selected_web_port"
}

main
