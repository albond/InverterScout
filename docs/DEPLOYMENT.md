# Deployment Guide

This guide expands on the interactive Quick Start in the project README. InverterScout is designed for a trusted home network; it has no public-internet Web UI mode.

## Choose a deployment location

| Location | Recommended Web UI access | Inverter requirement |
| --- | --- | --- |
| Home computer | Host-only or trusted LAN | Computer can route to the inverter LAN |
| Home NAS | Trusted LAN | NAS firewall and VLAN permit inverter TCP traffic |
| Remote Linux server | SSH tunnel | Private routed VPN to the inverter LAN |
| Docker-capable Arduino Linux/ARM64 board | Trusted LAN by default | Board can route to the inverter LAN |

A cloud or off-site server without a private route to the home network cannot monitor the inverter. Public router forwarding for the inverter or Web UI is not an acceptable substitute.

## Local prerequisites

For Linux or macOS:

- Bash
- Docker Engine with Docker Compose v2, or Docker Desktop
- `lsof`, `ss`, or `netstat` for the preflight port check when available

For Windows:

- Docker Desktop
- Windows PowerShell
- Command Prompt or PowerShell to run `start.bat`

The launcher does not install Docker or change system-level Docker permissions. On Linux, the current account must be allowed to access the Docker daemon.

## Remote prerequisites

The client computer needs `ssh`, `scp`, and `tar`. Windows users can enable **OpenSSH Client** under Optional Features when `ssh` or `scp` is missing.

The remote Linux server or NAS needs:

- an SSH account with a usable home directory;
- Docker Engine and Docker Compose v2;
- permission for that account to access Docker without an interactive `sudo` prompt;
- `bash` and `tar`;
- a LAN, VLAN, or private-VPN route to the inverter.

The launcher accepts a non-default SSH port. Passwords are handled by OpenSSH, not by the launcher. A private key is read only by the local SSH client and is not included in the uploaded archive.

## Deployment flow

The launchers perform these stages:

1. Validate input so hostnames, usernames, addresses, ports, and remote paths cannot become shell commands.
2. Check Docker and Docker Compose on the selected host.
3. Ask whether the Web UI is host-only or available to the trusted LAN, detect a local private IPv4 address when possible, and check the requested port.
4. Build the production image from the repository's restricted Docker context.
5. Optionally open a TCP connection to the inverter from a one-off application container.
6. Start the service and wait for the container healthcheck.
7. Print either the local/LAN URL or an exact SSH tunnel command.

Only the Web UI is published on the Docker host. The inverter connection is outbound from the container and therefore has no Compose port mapping.

The **Home NAS or Docker-capable Arduino Linux board over SSH** target uses the same guarded remote installer but defaults Web UI access to the trusted LAN. When the SSH destination is a private IPv4 address, the launcher suggests that address as the Docker bind address. Ordinary Arduino microcontrollers cannot use this target because they do not provide Linux, Docker, persistent container storage, or SSH.

## Remote release layout

Remote deployments are stored under:

```text
~/.local/share/inverterscout/
├── current -> releases/<release-id>/
└── releases/
    └── <release-id>/
```

The `current` symlink is updated only after the new container becomes healthy. Older source releases are retained for diagnosis and manual rollback. Runtime data is not stored in these directories; it remains in the `inverterscout_inverterscout_data` Docker volume.

The uploaded archive contains only the runtime build files selected by the launcher. The remote installer rejects absolute paths, parent-directory traversal, an unexpected upload path, or a missing required file before extraction.

## First-run setup

The deployment launcher asks for the inverter host and port only to test network reachability. It deliberately does not save them in `.env`. Enter them again in the browser wizard so the application can store them in its encrypted database.

If Telegram is enabled, create a private bot with `@BotFather`, enter the token, and provide the administrator chat ID. The wizard does not send a test message. Every additional Telegram user remains pending until manually approved.

## Common failures

### Docker command not found

Install Docker Desktop or Docker Engine with the Compose v2 plugin. Restart the terminal after installation so the updated command path is available.

### Docker is installed but unavailable

Start Docker Desktop on macOS or Windows. On Linux, start the Docker service and ensure the current account has Docker access. On a remote host, verify this without `sudo`:

```sh
docker info
docker compose version
```

### Requested Web UI port is occupied

Accept the nearby free port suggested by the launcher or enter another port. InverterScout currently publishes only one host port.

### LAN bind address is rejected

Enter a private IPv4 address assigned to the computer, NAS, or remote Docker host. Do not enter the inverter address, a public address, or `0.0.0.0`. In local mode the launcher suggests the detected private address when possible.

### LAN URL works on the Docker host but not another device

Confirm both devices are on the same trusted LAN and that guest-Wi-Fi or client isolation is disabled. Allow the selected inbound TCP port only from the local subnet in the Docker host firewall.

On Windows, rerun `start.bat`, choose LAN access, and accept the optional scoped Windows Firewall rule. The rule applies only to the selected local address, `LocalSubnet`, and the `Private` network profile. If rule creation reports an authorization error, run the launcher as Administrator. Do not solve this by forwarding the port on the internet router.

### Container cannot reach the inverter

Confirm the inverter address and TCP port, then check:

- the Docker host can reach the inverter network;
- the inverter and Docker host are not separated by guest-Wi-Fi isolation;
- VLAN and NAS firewall rules permit the connection;
- a remote host has a private routed VPN to the home network;
- the inverter TCP service is enabled and listening.

Skipping the test starts the Web UI but does not fix the network route.

### Remote Docker permission denied

The SSH account cannot access the Docker daemon. Configure Docker access according to the remote operating system's security policy. The launcher does not send a password to `sudo` or modify group membership.

## Service management

Run local commands from the InverterScout directory:

```sh
docker compose -p inverterscout ps
docker compose -p inverterscout logs --tail 100 inverterscout
docker compose -p inverterscout restart inverterscout
docker compose -p inverterscout down
```

For a remote deployment, first connect with SSH and enter the current release:

```sh
cd ~/.local/share/inverterscout/current
docker compose -p inverterscout ps
docker compose -p inverterscout logs --tail 100 inverterscout
```

`docker compose down` removes the container and network but preserves the named data volume unless `--volumes` is explicitly added. Do not add `--volumes` unless permanent data deletion is intended and a verified backup exists.
