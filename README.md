# FERMAX DuoxMe Integration for Home Assistant

This is a custom integration for Home Assistant that connects to the FERMAX DuoxMe video doorbell system. It uses a push-based notification system to provide real-time updates from your doorbell, including incoming calls, and allows you to open your door directly from Home Assistant.

> [!NOTE]
> Notifications and call media depend on FERMAX and Google FCM. The listener now reconnects automatically after errors, but this does not guarantee uninterrupted delivery. Lock-only mode remains available.

## Features

* **Camera:** Displays the latest photo retrieved from FERMAX, or an optional live WebRTC snapshot taken during an incoming call.
* **Ring Sensor:** Turns on immediately when an incoming-call notification arrives, without waiting for image capture, and off when the call is answered or ends.
* **Call Events:** Home Assistant events for call start, answer, and end, for automation triggers.
* **Last Call Sensor:** The latest call time, door/device details, and FERMAX photo ID; the image itself remains available on the camera entity.
* **Push Connection Sensor:** A binary sensor reporting the current FCM socket connection.
* **Door Lock:** A lock entity that allows you to open your door with a single tap.
* **Push-Based:** Uses real-time push notifications from FERMAX servers for instant updates, avoiding inefficient polling.

### Capabilities and Reliability

| Area | Behavior |
| --- | --- |
| Call automations | New `fermax_duoxme_call` events distinguish call start, answer, and end. |
| Last-call information | New timestamp sensor with device and photo metadata when available. |
| Push diagnostics | New connectivity sensor reports the FCM socket connection, separately from the ring sensor. |
| Unlock state | Configurable 1–30 second unlocked indication; existing installations keep 2 seconds. This does not change the physical door-release pulse. |
| Image capture | Live snapshot is optional and disabled by default. Choose go2rtc or aiortc; failed live capture falls back to the FERMAX photo API. |
| Camera access | Uses Home Assistant's camera endpoints. There is no separate unauthenticated custom MJPEG endpoint, and native live video streaming is not advertised. |
| Push recovery | Reconnects with exponential back-off from 5 seconds to 5 minutes, stops the listener on unload, and correctly unloads platforms when switching to lock-only mode. |
| Authentication | Refreshes access tokens with a 60-second margin, serializes concurrent refreshes, and supports password re-authentication and manual username/password reconfiguration. |
| Setup failures | Transient connection failures are retried. Rejected credentials and FCM registration errors are surfaced rather than leaving setup waiting indefinitely. |
| API compatibility | Uses Pairings v4 and FCM registration v2, including Firebase Installation IDs for registration/removal and Android app metadata. |
| Notification handling | Suppresses recently processed duplicate notifications using a bounded, persisted history of 100 IDs. Unrelated notifications do not clear the ring sensor. |
| Diagnostics and language | Integration logs avoid full push payloads and pairing data. Option and re-authentication translations are provided in English, Spanish, and Catalan. |

### Image Storage and Video Limitations

The camera stores only its latest successfully retrieved image in memory. A new successful image replaces it; a failed fetch leaves the previous image intact. The image is not saved by this integration to disk and is lost when the camera entity is recreated or Home Assistant restarts. On setup, the integration attempts to retrieve the latest FERMAX photo again.

WebRTC capture receives the call video only long enough to extract one JPEG frame, then disconnects. **There is currently no video recording option, MP4 file, video history, retention policy, or native live-video stream.**

## Prerequisites

This integration requires two sets of credentials:

1.  **FERMAX Account Credentials:** Your username and password for the FERMAX mobile app, plus the app's OAuth `client_id` and `client_secret`.
2.  **Google FCM Credentials:** Credentials for Firebase Cloud Messaging, which are necessary to receive the push notifications. This requires you to get them from the app too.

## Installation

### Recommended: HACS (Home Assistant Community Store)

1.  Ensure you have [HACS](https://hacs.xyz/) installed.
2.  Add this repository as a custom repository:
    * Go to HACS > Integrations > ... (top right) > Custom repositories.
    * URL: `https://github.com/edualm/ha-fermax-duoxme`
    * Category: `Integration`
    * Click "Add".
3.  Search for "FERMAX DuoxMe" in HACS and install it.
4.  Restart Home Assistant.

### Manual Installation

1.  Download the latest release from the [Releases](https://github.com/edualm/ha-fermax-duoxme/releases) page.
2.  Copy the complete `custom_components/fermax_duoxme` directory into `/config/custom_components/fermax_duoxme/`. Include all Python files, `manifest.json`, and `translations/`; exclude `__pycache__/`.
3.  Restart Home Assistant.

### Updating an Existing Installation

1. Back up the existing integration files and your Home Assistant configuration.
2. Update through HACS, or replace the integration's files with a complete, matching copy of the new version. Copying only selected Python files can cause import errors.
3. Keep the directory name `fermax_duoxme`, matching the manifest domain. Do not run a renamed copy such as `fermax_duoxme2` alongside it.
4. Restart Home Assistant, then review the integration's options.

The configuration entry version remains `1`, and the existing entity unique IDs are retained. Existing entities and automations can therefore be reused without deleting and re-adding the integration; actual entity IDs depend on your entity registry. The new sensors are added when push notifications are enabled.

Existing credentials continue to be loaded. Older FCM credentials without a Firebase Installation ID are renewed once automatically, which requires access to Google FCM and FERMAX. Do not delete Home Assistant's `.storage` files as part of a routine update.

## Configuration

After installation, you must configure the integration through the Home Assistant UI.

1.  Go to **Settings > Devices & Services**.
2.  Click **Add Integration** and search for **FERMAX DuoxMe**.
3.  The configuration flow will ask for your credentials in two steps.

### Step 1: FERMAX Connection Credentials

You will need to provide your FERMAX account details and the OAuth credentials used by the FERMAX DuoxMe mobile app.

* **Username:** Your FERMAX account email address.
* **Password:** Your FERMAX account password.
* **Client ID:** The OAuth client ID for the FERMAX app.
* **Client Secret:** The OAuth client secret for the FERMAX app.

> **Note:** The Client ID and Secret are static values from the app. How to get those is out of scope for this documentation.

### Step 2: Push Notification Credentials (FCM)

This integration requires a set of push notification credentials to listen for events from the FERMAX servers. You must get these from the FERMAX DuoxMe Android app.

* **FCM API Key:** Firebase project's API Key.
* **FCM Project ID:** Firebase Project ID.
* **FCM GCM Sender ID:** Google Cloud Messaging Sender ID.
* **FCM GMS App ID:** Google Mobile Services App ID.
* **FCM Android Package Name:** The package name for the Android app.

After entering all the credentials, the integration will be set up and your entities will be created.

### Changing the Username or Password

Go to **Settings > Devices & Services > FERMAX DuoxMe**, open the menu for the configuration entry, and select **Reconfigure**. You can enter a username and password without deleting and re-adding the integration.

The new login is validated before any saved credentials change. On success, the old session is unloaded, its cached OAuth token is removed, and the existing entry is updated and reloaded with the new login. OAuth client credentials, FCM configuration, integration options, and the entry ID are retained. The password field is not prefilled.

An account already configured in another entry cannot be selected. If you switch to a different FERMAX account, its available doors may differ, so review door entities and related automations after the reload. Invalid credentials or a failed validation leave the saved configuration unchanged.

Automatic re-authentication remains available when FERMAX rejects the stored credentials; that form only asks for the password of the saved user.

### Integration Options

After setting up the integration, you can change its behavior by going to the integration's card on the **Devices & Services** page and clicking **Configure**.

* **Enable Doorbell Notifications**:
    * **Enabled (Default):** This is the full-featured mode. The integration listens for FERMAX notifications, fires call events, and creates the camera, ring, last-call, push-connection, and door-lock entities. Live snapshots require the separate option below.
    * **Disabled:** This is a "lock-only" mode. The integration will not listen for push notifications. The camera, ring, last-call, and push-connection entities will not be created, and call events will not be emitted. This is useful if you only want the ability to open the door without running a push listener.
* **Capture live image from the call video (WebRTC)**:
    * **Enabled:** On an incoming call, the integration joins the call via WebRTC to grab a live frame for the camera entity, using the method below.
    * **Disabled (Default):** The integration skips WebRTC entirely and uses the latest photo stored by FERMAX. `aiortc` is never installed or imported. Useful if WebRTC capture fails or produces errors on your Home Assistant version.
* **Live image capture method** (only used when live capture is enabled):
    * **go2rtc (Default):** The go2rtc server bundled with Home Assistant receives the call video and produces the snapshot. No extra Python packages are installed. It requires go2rtc to be running in Home Assistant (`default_config:` or a `go2rtc:` entry in `configuration.yaml`). A temporary stream uses go2rtc's FFmpeg source to convert the received video to JPEG and is removed after capture.
    * **aiortc:** An in-process Python WebRTC peer (`aiortc`, installed on first use) receives the video and decodes the first frame.
    * If the selected method fails, the integration falls back to the latest photo stored by FERMAX.
* **Door unlock duration:** Choose how long the lock reports as unlocked before returning to locked (1–30 seconds). Existing installations retain the previous 2-second behavior. This is a Home Assistant state timer, not a command to change the physical opening duration or to lock the door again.

## Usage

Once configured, the integration will create the following entities:

The entity IDs below are examples; Home Assistant may use different IDs depending on names and existing registry entries.

* **`camera.fermax_duoxme_doorbell`**: Displays the latest successful still image. It does not expose a native live-video stream or save recordings.
* **`binary_sensor.fermax_duoxme_ring`**: Turns `on` on `Call` and `off` on `CallAttend` or `CallEnd`. Informational notifications do not change it.
* **`binary_sensor.fermax_duoxme_push_connection`**: Turns `on` while the FCM socket is connected. This does not prove that FERMAX can deliver a call or that image capture is working.
* **`sensor.fermax_duoxme_last_call`**: Timestamp of the latest call. Its attributes include `device_id`, `device_name`, and `photo_id` when available. View the actual photo on the camera entity.
* **Door locks**, for example **`lock.fermax_duoxme_door`**: One entity is created for each visible access door returned by FERMAX. Call `lock.unlock` to open it. Its state returns to "Locked" after the configured interval (2 seconds by default); this is not physical lock feedback. Sending a lock command is not supported.

### Call Events and Last-Call Metadata

The integration fires the `fermax_duoxme_call` event for these FERMAX notifications:

| FERMAX notification | Event `state` | Meaning |
| --- | --- | --- |
| `Call` | `started` | An incoming call was received. |
| `CallAttend` | `attended` | The call was attended, potentially by another participant. |
| `CallEnd` | `ended` | A call-end notification was received. |

Event data contains `state` and, when supplied, `device_id`. It intentionally omits the private call room, socket URL, and authentication tokens.

The last-call sensor updates immediately on an incoming call and refreshes metadata from the FERMAX call list after 5 seconds. Its attributes can include `device_id`, `device_name`, and `photo_id`. It is not a full call-history database. Older call-list data does not replace a newly received timestamp when it is more than 10 seconds older.

### Example Automation

```yaml
automation:
  - alias: "Handle a FERMAX call"
    trigger:
      - platform: event
        event_type: fermax_duoxme_call
        event_data:
          state: started
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "A FERMAX call has started."

  - alias: "Notify when doorbell rings"
    trigger:
      - platform: state
        entity_id: binary_sensor.fermax_duoxme_ring
        to: 'on'
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "Someone is at the door!"
```

### HomeKit Compatibility

The integration exposes a still-image camera, not a native video stream. Live HomeKit video and HomeKit recording are not provided. The ring sensor and call events can still be used in Home Assistant automations independently of image capture.

## Troubleshooting

* **PERMISSION_DENIED Error During Setup**: This error means your FCM API Key is incorrect or lacks the necessary permissions. In your Google Cloud project, ensure the key has the firebaseinstallations.installations.create permission enabled.
* **No Notifications**: If the integration sets up but you don't receive any updates, double-check all your FCM credentials. Also, check the Home Assistant logs for any errors from the custom_components.fermax_duoxme.push component.
* **Camera Feed is Blank**: This can happen if the integration fails to fetch the initial image. Try restarting Home Assistant. If the problem persists, check the logs for errors related to the FERMAX API.
* **Live WebRTC Snapshot is Unavailable**: Check that live capture is enabled and review the selected method. go2rtc requires Home Assistant's go2rtc integration; aiortc and Pillow are installed on first use only for the aiortc method. Either method can fail independently of push notifications. The camera then attempts to retrieve the latest FERMAX photo.
* **Photo API Returns HTTP 404**: That particular photo could not be retrieved at that moment. This alone does not establish whether the image is delayed, unavailable, or removed. Photo refreshes can also occur after call notifications, independently of live capture. A failed refresh does not erase an image already captured successfully.
* **go2rtc Reports `Incoming unhandled RTP ssrc(1234)`**: This message can appear even when a later log confirms successful frame capture. It is not by itself proof that image capture failed. If you choose to filter it, use a message-specific Home Assistant logger filter instead of disabling all go2rtc errors.
* **FCM Registration Returns HTTP 400 or 409**: These are reported as setup errors with selected, redacted server details when available, without an endless automatic setup retry loop. Review those details and the FCM configuration before retrying. Do not assume every HTTP 400 means a session limit.
* **OAuth Returns HTTP 400**: Check the FERMAX login and the OAuth client ID/secret. This is a different request from FCM registration; the HTTP status alone does not identify the cause.
* **Re-authentication Is Requested**: When setup detects rejected stored credentials, Home Assistant offers a re-authentication form for the FERMAX password. Network failures instead cause setup to retry.
* **`Invalid handler specified` or Missing Setup Function**: Verify the full integration directory, including `__init__.py`, `config_flow.py`, `const.py`, and `manifest.json`. Ensure its name is `fermax_duoxme`, avoid duplicate renamed installations, and restart Home Assistant.

### Safe Diagnostic Logging

Do not share raw push payloads, OAuth tokens, FCM credentials, call room IDs, or unredacted personal details. Although the integration avoids logging full notification and pairing payloads, the third-party `push_receiver` library can still print sensitive payloads at INFO or DEBUG level.

For integration debugging without those library payloads, merge the following into your existing `logger:` configuration and restart Home Assistant:

```yaml
logger:
  logs:
    custom_components.fermax_duoxme: debug
    push_receiver: warning
```

Inspect and redact logs before sharing them. WebRTC debug output may also contain local network and negotiation details.

## Contributing

Contributions are welcome! If you have an idea for an improvement or have found a bug, please open an issue or submit a pull request.

### Tests

From the repository root, install the test dependencies in a virtual environment and run:

```shell
python -m pip install -r requirements_test.txt
python -m unittest discover -s tests -v
```

The unit tests cover configuration and credential changes, HTTP requests, OAuth renewal, FCM registration and notification handling, reconnection and shutdown, entity states, snapshot fallback, and WebRTC signalling and cleanup for both backends.

Home Assistant APIs, Google/FERMAX services, sockets, timers and media peers are mocked. The tests do not require a Home Assistant installation, real credentials, go2rtc or aiortc, and do not install optional media packages. They do not replace testing inside Home Assistant or verifying real notifications and image capture with a FERMAX device.

## License

This project is licensed under the Apache License, Version 2.0. See the LICENSE file for details.
