# Device Connection Guide

InverterScout reads the LuxPower inverter through its Wi-Fi dongle and controls supported Tapo or Tuya devices on the trusted local network. Complete each manufacturer's normal pairing flow before entering anything in InverterScout.

Never forward the inverter, smart-device, or Web UI ports through the internet router. Reserve local addresses in the router where possible so a DHCP renewal does not break monitoring.

## LuxPower Wi-Fi dongle

1. Install the dongle on the inverter and complete its network setup in the LuxPower app. LuxPowerTek describes the dongle as an RS485 communication module that joins the network through its app and web platform; see the [official Wi-Fi dongle page](https://luxpowertek.com/product-data-logger-wifi-dongle/) and [official SNA 3–6K manual](https://luxpowertek.com/wp-content/uploads/2025/12/SNA-3-6K-User-manual-English-OffGrid-Single-Phase-LuxpowerTek.pdf).
2. Open the home router's connected-client or DHCP list, identify the dongle, and reserve its IPv4 address. Enter that address as the inverter host. Do not enter the Docker host's address.
3. Start with TCP port `8000`. This is the default verified by InverterScout for the tested logger, but LuxPowerTek does not document it as a universal setting for every dongle and firmware version.
4. Enter the exact 10-character Wi-Fi dongle and inverter serial numbers printed on their labels. Do not paste either serial into an issue, screenshot, or public diagnostic archive.
5. Keep the Docker host and dongle on the same trusted LAN, an allowed VLAN route, or a private VPN. The connection is outbound from InverterScout; no inverter port mapping belongs in Docker Compose.

The inverter integration sends only Modbus function `0x04` read-input-register requests. A model listed as expected or experimental in the README still requires independent hardware validation.

## Tapo switches and bulbs

1. Pair the device in the official Tapo app and verify that it can be controlled there.
2. Reserve the device's IPv4 address in the router.
3. Open **Settings > Tapo** in InverterScout. Enter the email address and password of the TP-Link ID that can control the device. InverterScout never renders the saved values back into the page.
4. Open **Devices > Add device**, choose **Tapo**, enter the reserved local address and a display name, then add the device.

The bundled [Tapo package](https://pypi.org/project/tapo/) is an unofficial local API client. Compatibility depends on the exact model and firmware even when the official Tapo app works. InverterScout currently provides on/off control through the generic device API and power readings for P110 and P115 plugs.

For better account separation, a dedicated TP-Link ID can be created and the official Tapo app's [Device Sharing](https://www.tp-link.com/us/support/faq/5121/) feature can grant it access. Shared-account support varies by model, firmware, and permission tier and is not guaranteed by the local API library. Confirm control with the dedicated account before saving it in InverterScout.

## Tuya switches and bulbs

Tuya Smart or Smart Life credentials cannot be entered directly into InverterScout. Tuya setup requires developer access and an authorized cloud project so InverterScout can retrieve the Device ID metadata and secret Local Key.

### 1. Pair the devices

Pair every device in the **Tuya Smart** or **Smart Life** app using a normal account, not a guest account. Confirm that each device works in the app.

### 2. Request and configure Tuya Developer Cloud access

Follow Tuya's current [Request Tuya Cloud API Key](https://developer.tuya.com/en/docs/developer/apply-cloud-api-key?id=Kff30z8sv62ah) guide:

1. Sign in to [Tuya Developer Cloud](https://platform.tuya.com/cloud/).
2. Open **Cloud > Cloud Project > Project Management**, activate or request the required IoT Core plan, and create a cloud project.
3. Set **Development Method** to **Smart Home**. A different project type will not provide the expected app-account device list.
4. Confirm the required cloud services are subscribed and authorized: **Country and City Info**, **Smart Home Basic Service**, **IoT Core**, and the currently listed Smart Home scene service.
5. Open **Devices > Link App Account > Add App Account**, choose **Tuya App Account Authorization**, and scan the QR code with the same Tuya Smart or Smart Life account used for pairing.
6. Confirm that the devices appear under **All Devices**. Record each required **Device ID**.
7. Copy **Client ID** and **Client Secret** from the project's **Authorization Key** section. InverterScout labels these fields **Access ID** and **Access secret**.

Tuya can change the portal and subscription rules. Treat the linked Tuya guide as authoritative if a label or required service changes. IoT Core access can expire and may require a renewal request.

### 3. Configure InverterScout

1. Open **Settings > Tuya** and enter the project's Client ID, Client Secret, and exact data-center region. Choose the region used by the cloud project, not the region that merely looks geographically closest. Supported codes are `eu`, `eu-w`, `us`, `us-e`, `cn`, `sg`, and `in`.
2. Reserve the Tuya device's IPv4 address in the router.
3. Determine the device's actual local protocol version. The [TinyTuya package documentation](https://pypi.org/project/tinytuya/) supports versions 3.1, 3.2, 3.3, 3.4, and 3.5 and documents `python -m tinytuya scan` for detecting the address, Device ID, and version from a computer on the same LAN.
4. Open **Devices > Add device**, choose **Tuya**, then enter the reserved address, Device ID, and detected protocol version.

When the device is added, InverterScout contacts Tuya Cloud to find that Device ID and retrieve its Local Key. Normal on/off commands then go directly to the device over the LAN. Resetting or re-pairing a Tuya device changes its Local Key; remove and add that device again afterward. The Tuya app can also occupy a device's local connection temporarily, so close it while diagnosing LAN-control failures.

## Credential storage

TP-Link passwords, Tuya Client Secrets, Tuya Local Keys, device settings, and other runtime state are encrypted before being written to the local SQLite database. They are not part of the source tree, container image, screenshots, or sample configuration.

The generated database key is stored separately as `/app/data/.master.key`. Anyone who obtains both the database and that key can decrypt the saved values. Protect Docker volume backups, or inject `INVERTERSCOUT_MASTER_KEY` from a platform secret manager and keep that key backup separate from the database.
