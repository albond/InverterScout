# Changelog

All notable changes to InverterScout are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-09

### Added

- Mandatory multilingual first-run setup with configurable IANA time zone.
- Passive LuxPower SNA5000 monitoring through read-only input-register requests.
- Local Web UI and optional Telegram interface with manual user approval.
- Tapo and Tuya device integrations and local automation scenarios.
- Authenticated encrypted SQLite persistence and credential-safe logging.
- Hardened Docker Compose deployment for x86-64 and ARM64 Linux hosts.
- Unit, integration, security, package, and container quality gates.
- Cross-platform Quick Start launchers for local Docker and remote SSH deployment.
- Web-port conflict detection, container-level inverter reachability checks, and startup health verification.
- A fail-closed remote release installer with runtime-only uploads and persistent-volume preservation.
- Beginner-focused local, NAS, remote-server, SSH-tunnel, and inverter-network documentation.
- Test-process isolation that prevents local credentials or runtime databases from entering automated tests.
- Real application screenshots for the README in desktop light, desktop dark, setup, and mobile layouts.

### Changed

- Documented the alpha-stage hardware validation scope and compatibility warnings.
- Replaced the short manual time-zone list with full English IANA autocomplete in setup and settings.
- Made trusted-LAN Docker publication explicit in both launchers, including address detection, a home NAS/Arduino Linux target, and scoped Windows Firewall support.
- Rebuilt every Web UI page around a responsive light/dark visual system with matte layered surfaces, fluid press feedback, mobile bottom navigation, and accessible motion and contrast fallbacks.
- Replaced the generic CSS framework with local, product-specific styles, scripts, brand artwork, and a consistent SVG icon set.

[Unreleased]: https://github.com/albond/InverterScout/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/albond/InverterScout/releases/tag/v0.1.0
