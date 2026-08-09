#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_NAME="inverterscout"
DEFAULT_WEB_PORT=8080
DEFAULT_INVERTER_PORT=8000
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMP_FILES=()

cleanup() {
  local path
  for path in "${TEMP_FILES[@]:-}"; do
    if [[ -n "$path" && -f "$path" ]]; then
      rm -f -- "$path"
    fi
  done
}
trap cleanup EXIT

say() {
  printf '%s\n' "$*"
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

show_help() {
  cat <<'EOF'
InverterScout Quick Start

Usage: ./start.sh

The interactive launcher can deploy InverterScout:
  1. On Docker running on this computer
  2. On a home NAS or Docker-capable Arduino Linux board over SSH
  3. On a remote Linux Docker host over SSH

It validates Docker Compose, selects a free Web UI port, optionally checks
inverter TCP reachability from the application container, and starts the
first-run setup wizard.
EOF
}

prompt_value() {
  local prompt="$1"
  local default_value="${2:-}"
  local answer

  if [[ -n "$default_value" ]]; then
    printf '%s [%s]: ' "$prompt" "$default_value" >&2
  else
    printf '%s: ' "$prompt" >&2
  fi
  if ! IFS= read -r answer; then
    fail "Input was closed before setup completed."
  fi
  if [[ -z "$answer" ]]; then
    answer="$default_value"
  fi
  printf '%s' "$answer"
}

prompt_yes_no() {
  local prompt="$1"
  local default_answer="${2:-yes}"
  local suffix="[Y/n]"
  local answer

  if [[ "$default_answer" == "no" ]]; then
    suffix="[y/N]"
  fi
  while true; do
    printf '%s %s: ' "$prompt" "$suffix" >&2
    if ! IFS= read -r answer; then
      fail "Input was closed before setup completed."
    fi
    if [[ -z "$answer" ]]; then
      answer="$default_answer"
    fi
    case "$answer" in
      y | Y | yes | YES | Yes) return 0 ;;
      n | N | no | NO | No) return 1 ;;
      *) say "Please enter y or n." >&2 ;;
    esac
  done
}

is_valid_port() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535))
}

is_valid_host() {
  local host="$1"
  [[ "$host" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]
}

is_valid_username() {
  local username="$1"
  [[ "$username" =~ ^[A-Za-z_][A-Za-z0-9._-]*$ ]]
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

is_private_ipv4() {
  local address="$1"
  local first second third fourth
  is_valid_ipv4 "$address" || return 1
  IFS=. read -r first second third fourth <<<"$address"
  ((10#$first == 10)) && return 0
  ((10#$first == 192 && 10#$second == 168)) && return 0
  ((10#$first == 172 && 10#$second >= 16 && 10#$second <= 31)) && return 0
  return 1
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
  if command -v hostname >/dev/null 2>&1; then
    hostname -I 2>/dev/null | tr ' ' '\n' | grep -Fxq "$address"
    return
  fi
  return 1
}

detect_local_lan_ipv4() {
  local address interface_name

  if [[ "$(uname -s)" == "Darwin" ]] \
    && command -v route >/dev/null 2>&1 \
    && command -v ipconfig >/dev/null 2>&1; then
    interface_name="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
    if [[ -n "$interface_name" ]]; then
      address="$(ipconfig getifaddr "$interface_name" 2>/dev/null || true)"
      if is_private_ipv4 "$address" && address_belongs_to_host "$address"; then
        printf '%s' "$address"
        return 0
      fi
    fi
  fi

  if command -v ip >/dev/null 2>&1; then
    address="$(ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')"
    if is_private_ipv4 "$address" && address_belongs_to_host "$address"; then
      printf '%s' "$address"
      return 0
    fi
  fi

  if command -v hostname >/dev/null 2>&1; then
    while IFS= read -r address; do
      if is_private_ipv4 "$address" && address_belongs_to_host "$address"; then
        printf '%s' "$address"
        return 0
      fi
    done < <(hostname -I 2>/dev/null | tr ' ' '\n')
  fi

  return 1
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
  return 1
}

next_free_port() {
  local candidate=$((10#$1 + 1))
  local limit=$((candidate + 99))
  ((limit > 65535)) && limit=65535
  while ((candidate <= limit)); do
    if ! port_is_in_use "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
    candidate=$((candidate + 1))
  done
  return 1
}

select_web_port() {
  local requested suggested
  while true; do
    requested="$(prompt_value "Web UI port" "$DEFAULT_WEB_PORT")"
    if ! is_valid_port "$requested"; then
      say "Enter a number from 1 to 65535."
      continue
    fi
    if ! port_is_in_use "$requested" || port_is_owned_by_project "$requested"; then
      SELECTED_WEB_PORT="$requested"
      return
    fi
    suggested="$(next_free_port "$requested" || true)"
    if [[ -z "$suggested" ]]; then
      say "Port $requested is occupied and no nearby free port was found."
      continue
    fi
    say "Port $requested is occupied. Suggested free port: $suggested."
    if prompt_yes_no "Use port $suggested?" yes; then
      SELECTED_WEB_PORT="$suggested"
      return
    fi
  done
}

request_web_port() {
  local requested
  while true; do
    requested="$(prompt_value "Preferred remote Web UI port" "$DEFAULT_WEB_PORT")"
    if is_valid_port "$requested"; then
      SELECTED_WEB_PORT="$requested"
      return
    fi
    say "Enter a number from 1 to 65535."
  done
}

select_bind_address() {
  local deployment_kind="$1"
  local choice address suggested_address="" default_choice="1"

  if [[ "$deployment_kind" == "home_remote" ]]; then
    if is_private_ipv4 "${SSH_HOST:-}"; then
      suggested_address="$SSH_HOST"
    fi
    default_choice="2"
    say "Who should be able to open the Web UI?"
    say "  1. This computer through an SSH tunnel only"
    say "  2. Other devices on the trusted home LAN (recommended for a NAS or Arduino Linux board)"
  elif [[ "$deployment_kind" == "remote" ]]; then
    say "Who should be able to open the Web UI?"
    say "  1. This computer through an SSH tunnel only (recommended)"
    say "  2. Devices on the remote host's trusted private LAN or VPN"
  else
    suggested_address="$(detect_local_lan_ipv4 || true)"
    say "Who should be able to open the Web UI?"
    say "  1. Only this computer at localhost (recommended)"
    say "  2. Other devices on this trusted home LAN"
  fi

  while true; do
    choice="$(prompt_value "Choose Web UI access" "$default_choice")"
    case "$choice" in
      1)
        SELECTED_BIND_ADDRESS="127.0.0.1"
        return
        ;;
      2)
        say "LAN access publishes the selected Docker port on one private host address."
        say "It does not and must not create public router port forwarding."
        address="$(prompt_value "Docker host private LAN IPv4 address" "$suggested_address")"
        if ! is_private_ipv4 "$address"; then
          say "Enter a private IPv4 address such as 192.168.x.x, 10.x.x.x, or 172.16-31.x.x."
          continue
        fi
        if [[ "$deployment_kind" == "local" ]] && ! address_belongs_to_host "$address"; then
          say "Address $address is not assigned to this computer."
          continue
        fi
        SELECTED_BIND_ADDRESS="$address"
        return
        ;;
      *) say "Enter 1 or 2." ;;
    esac
  done
}

show_lan_access_notes() {
  local address="$1"
  local port="$2"
  say "Open from this computer or another device on the trusted LAN: http://$address:$port"
  say "If another device cannot connect, allow inbound TCP port $port from the local subnet in the host firewall."
  say "Never forward this Web UI port on the internet router; use Telegram or a private VPN away from home."
}

collect_inverter_target() {
  local host port
  while true; do
    host="$(prompt_value "Inverter hostname or IPv4 address")"
    if is_valid_host "$host"; then
      INVERTER_HOST="$host"
      break
    fi
    say "Use a hostname or IPv4 address without a URL or path."
  done
  while true; do
    port="$(prompt_value "Inverter TCP port" "$DEFAULT_INVERTER_PORT")"
    if is_valid_port "$port"; then
      INVERTER_PORT="$port"
      break
    fi
    say "Enter a number from 1 to 65535."
  done
}

wait_for_local_docker() {
  local attempt
  for attempt in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

ensure_local_docker() {
  command -v docker >/dev/null 2>&1 || fail \
    "Docker was not found. Install Docker Desktop or Docker Engine with the Compose plugin."
  docker compose version >/dev/null 2>&1 || fail \
    "The Docker Compose plugin is missing. Install Docker Compose v2."

  if docker info >/dev/null 2>&1; then
    return
  fi

  if [[ "$(uname -s)" == "Darwin" ]] && [[ -d "/Applications/Docker.app" ]]; then
    if prompt_yes_no "Docker Desktop is not running. Start it now?" yes; then
      open -a Docker
      say "Waiting for Docker Desktop..."
      wait_for_local_docker || fail "Docker Desktop did not become ready within two minutes."
      return
    fi
  fi

  fail "The Docker daemon is not available. Start Docker Desktop or the Docker service, then run this launcher again."
}

write_local_env() {
  local env_file="$ROOT_DIR/.env"
  (
    umask 077
    {
      printf 'INVERTERSCOUT_BIND_ADDRESS=%s\n' "$SELECTED_BIND_ADDRESS"
      printf 'INVERTERSCOUT_WEB_PORT=%s\n' "$SELECTED_WEB_PORT"
    } >"$env_file"
  )
}

probe_inverter_with_compose() {
  (
    cd "$ROOT_DIR"
    docker compose -p "$PROJECT_NAME" run --rm --no-deps --entrypoint python inverterscout \
      -c 'import socket,sys; connection=socket.create_connection((sys.argv[1], int(sys.argv[2])), 5); connection.close()' \
      "$INVERTER_HOST" "$INVERTER_PORT"
  )
}

wait_for_service() {
  local container_id status attempt
  container_id="$(cd "$ROOT_DIR" && docker compose -p "$PROJECT_NAME" ps -a -q inverterscout)"
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

run_local_deployment() {
  ensure_local_docker
  select_bind_address local
  select_web_port
  collect_inverter_target
  write_local_env

  say
  say "Validating and building InverterScout..."
  (
    cd "$ROOT_DIR"
    docker compose -p "$PROJECT_NAME" config --quiet
    docker compose -p "$PROJECT_NAME" build inverterscout
  )

  if prompt_yes_no "Test inverter reachability from the container now?" yes; then
    if probe_inverter_with_compose; then
      say "Inverter TCP connection succeeded."
    else
      say "The container cannot reach $INVERTER_HOST:$INVERTER_PORT."
      say "Check the address, VLAN/firewall rules, and routing from the Docker host."
      if ! prompt_yes_no "Start the Web UI anyway?" no; then
        fail "Deployment stopped before the application was started."
      fi
    fi
  else
    say "Inverter connectivity test skipped. Verify it in the first-run wizard."
  fi

  (cd "$ROOT_DIR" && docker compose -p "$PROJECT_NAME" up -d --no-build)
  if ! wait_for_service; then
    (cd "$ROOT_DIR" && docker compose -p "$PROJECT_NAME" logs --tail 80 inverterscout) >&2 || true
    fail "InverterScout did not become healthy. Review the logs above."
  fi

  say
  say "InverterScout is running."
  if [[ "$SELECTED_BIND_ADDRESS" == "127.0.0.1" ]]; then
    say "Open: http://localhost:$SELECTED_WEB_PORT"
  else
    show_lan_access_notes "$SELECTED_BIND_ADDRESS" "$SELECTED_WEB_PORT"
  fi
  say "Complete the browser wizard. Re-enter the inverter address there so it is stored in the encrypted database."
}

ensure_remote_tools() {
  command -v ssh >/dev/null 2>&1 || fail "OpenSSH client (ssh) was not found."
  command -v scp >/dev/null 2>&1 || fail "OpenSSH secure copy (scp) was not found."
  command -v tar >/dev/null 2>&1 || fail "tar was not found."
}

collect_remote_access() {
  local value auth_mode key_path
  while true; do
    value="$(prompt_value "Remote Docker host (IPv4 address or DNS name)")"
    if is_valid_host "$value"; then
      SSH_HOST="$value"
      break
    fi
    say "Use an IPv4 address or DNS name without a URL or path."
  done
  while true; do
    value="$(prompt_value "SSH port" "22")"
    if is_valid_port "$value"; then
      SSH_PORT="$value"
      break
    fi
    say "Enter a number from 1 to 65535."
  done
  while true; do
    value="$(prompt_value "SSH username")"
    if is_valid_username "$value"; then
      SSH_USER="$value"
      break
    fi
    say "Use a normal Linux username without spaces or shell characters."
  done

  say "SSH authentication:"
  say "  1. Password (entered securely by OpenSSH and never stored)"
  say "  2. Private key"
  while true; do
    auth_mode="$(prompt_value "Choose authentication" "1")"
    case "$auth_mode" in
      1)
        SSH_KEY_PATH=""
        return
        ;;
      2)
        key_path="$(prompt_value "Path to the private key")"
        case "$key_path" in
          "~/"*) key_path="$HOME/${key_path#~/}" ;;
        esac
        if [[ ! -f "$key_path" ]]; then
          say "Private key not found: $key_path"
          continue
        fi
        SSH_KEY_PATH="$key_path"
        return
        ;;
      *) say "Enter 1 or 2." ;;
    esac
  done
}

create_remote_bundle() {
  local bundle="$1"
  COPYFILE_DISABLE=1 tar --no-xattrs -czf "$bundle" -C "$ROOT_DIR" \
    .dockerignore Dockerfile LICENSE README.md docker-compose.yml pyproject.toml src
}

show_tunnel_command() {
  local local_port="$1"
  local remote_port="$2"
  say "Keep this command running while using the Web UI:"
  if [[ -n "$SSH_KEY_PATH" ]]; then
    printf 'ssh -N -L %s:127.0.0.1:%s -p %s -i %q %q\n' \
      "$local_port" "$remote_port" "$SSH_PORT" "$SSH_KEY_PATH" "$SSH_USER@$SSH_HOST"
  else
    printf 'ssh -N -L %s:127.0.0.1:%s -p %s %q\n' \
      "$local_port" "$remote_port" "$SSH_PORT" "$SSH_USER@$SSH_HOST"
  fi
  say "Then open: http://localhost:$local_port"
}

run_remote_deployment() {
  local deployment_kind="${1:-remote}"
  local release_id bundle remote_archive output_file probe_mode status selected_port local_tunnel_port
  local destination
  local -a ssh_options scp_options

  ensure_remote_tools
  collect_remote_access
  select_bind_address "$deployment_kind"
  request_web_port
  collect_inverter_target

  probe_mode="skip"
  if prompt_yes_no "Require a successful inverter connection from the remote container?" yes; then
    probe_mode="required"
  fi

  release_id="$(date -u +%Y%m%d%H%M%S)-$RANDOM"
  bundle="$(mktemp "${TMPDIR:-/tmp}/inverterscout-bundle.XXXXXX")"
  output_file="$(mktemp "${TMPDIR:-/tmp}/inverterscout-remote.XXXXXX")"
  TEMP_FILES+=("$bundle" "$output_file")
  create_remote_bundle "$bundle"

  remote_archive="/tmp/inverterscout-$release_id.tar.gz"
  destination="$SSH_USER@$SSH_HOST"
  ssh_options=(-p "$SSH_PORT")
  scp_options=(-P "$SSH_PORT")
  if [[ -n "$SSH_KEY_PATH" ]]; then
    ssh_options+=(-i "$SSH_KEY_PATH" -o IdentitiesOnly=yes)
    scp_options+=(-i "$SSH_KEY_PATH" -o IdentitiesOnly=yes)
  fi

  say
  say "Uploading a runtime-only source bundle over SSH..."
  say "OpenSSH may ask for the host-key confirmation, account password, or key passphrase."
  scp "${scp_options[@]}" "$bundle" "$destination:$remote_archive"

  say "Checking remote Docker, selecting ports, building, and starting InverterScout..."
  set +e
  ssh "${ssh_options[@]}" "$destination" \
    "bash -s -- '$remote_archive' '$SELECTED_BIND_ADDRESS' '$SELECTED_WEB_PORT' '$INVERTER_HOST' '$INVERTER_PORT' '$probe_mode' '$release_id'" \
    <"$ROOT_DIR/scripts/deployment/remote-install.sh" 2>&1 | tee "$output_file"
  status=${PIPESTATUS[0]}
  set -e
  ((status == 0)) || fail "Remote deployment failed. No SSH password or private key was saved."

  selected_port="$(sed -n 's/^INVERTERSCOUT_WEB_PORT=//p' "$output_file" | tail -n 1)"
  is_valid_port "$selected_port" || fail "The remote installer did not return a valid Web UI port."

  say
  if [[ "$SELECTED_BIND_ADDRESS" == "127.0.0.1" ]]; then
    local_tunnel_port="$selected_port"
    if port_is_in_use "$local_tunnel_port"; then
      local_tunnel_port="$(next_free_port "$local_tunnel_port" || true)"
      [[ -n "$local_tunnel_port" ]] || fail "No free local port was found for the SSH tunnel."
    fi
    show_tunnel_command "$local_tunnel_port" "$selected_port"
  else
    show_lan_access_notes "$SELECTED_BIND_ADDRESS" "$selected_port"
  fi
  say "Complete the browser wizard. Re-enter the inverter address there so it is stored in the encrypted database."
}

main() {
  local deployment_mode
  [[ -f "$ROOT_DIR/docker-compose.yml" ]] || fail "Run this launcher from the InverterScout source package."

  if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    show_help
    return
  fi
  [[ $# -eq 0 ]] || fail "Unknown option: $1"

  say "InverterScout Quick Start"
  say "=========================="
  say "  1. Docker on this computer"
  say "  2. Home NAS or Docker-capable Arduino Linux board over SSH"
  say "  3. Remote Linux server over SSH"
  say "     Ordinary Arduino boards such as Uno, Nano, and Mega cannot run Docker."
  while true; do
    deployment_mode="$(prompt_value "Choose deployment target" "1")"
    case "$deployment_mode" in
      1) run_local_deployment; return ;;
      2) run_remote_deployment home_remote; return ;;
      3) run_remote_deployment remote; return ;;
      *) say "Enter 1, 2, or 3." ;;
    esac
  done
}

if [[ "${INVERTERSCOUT_LAUNCHER_LIBRARY:-0}" != "1" ]]; then
  main "$@"
fi
