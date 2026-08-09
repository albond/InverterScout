# Security policy

## Keep the Web UI private

InverterScout has no public-internet Web UI mode. The interface is intended for a trusted home network and does not replace an internet-facing identity provider or hardened reverse proxy.

Do not expose its port through router forwarding, UPnP, a public tunnel, or a public reverse proxy. Bind to `127.0.0.1` for host-only access or to one trusted LAN interface when household devices need the dashboard. Use Telegram or a private authenticated VPN outside the home.

## Credentials

Telegram tokens, Tapo account credentials, Tuya developer credentials, device local keys, serial numbers, and user chat IDs are private.

- Enter them only in the local setup or Settings page.
- Never add them to Compose files, `.env.example`, screenshots, issues, or logs.
- InverterScout does not render a saved secret back into the browser.
- Replace credentials immediately if they appear in a public file or Git history.
- Use a dedicated Tapo account with access only to the devices InverterScout needs when the vendor account model permits it.
- Use a dedicated Tuya cloud project with the minimum required APIs and devices.

## Encrypted storage

The SQLite database contains Fernet ciphertext records. Record names are hashed, and each decrypted envelope authenticates its logical record name. Tampered ciphertext fails closed.

By default, a random key is created at `data/.master.key` with restrictive file permissions. This prevents an isolated database backup from revealing its values, but an attacker who can read the complete data directory can obtain both database and key. For stronger separation, set `INVERTERSCOUT_MASTER_KEY` through the host's secret manager and do not persist the key beside the database.

Losing the key makes the database unrecoverable. Back it up securely and separately.

## Telegram access

Selecting Telegram mode requires both a bot token and an administrator chat ID. A user who opens the bot is added to the encrypted pending list. Commands and notifications remain unavailable until that exact chat ID is approved manually. Blocking removes the chat ID from approved users and adds it to the blocked set.

No verification procedure should send a message to pending or blocked accounts.

## Inverter safety

The inverter transport is passive by design. Only Modbus function `0x04` Read Input Registers is allowed. Changes that introduce `0x06`, `0x10`, or another write operation are outside the project safety model and must not be merged.

## Reporting a vulnerability

Do not include live credentials or an unredacted database in a report. Use `albond.dev@proton.me` for a private security report. Include the affected version, reproduction steps with synthetic data, and the expected impact.
