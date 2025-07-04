# FERMAX DuoxMe Integration for Home Assistant

This is a custom integration for Home Assistant that connects to the FERMAX DuoxMe video doorbell system. It uses a push-based notification system to provide real-time updates from your doorbell, including incoming calls, and allows you to open your door directly from Home Assistant.

## Features

* **Camera:** A feed that displays a "call" image when someone is ringing and updates to the latest snapshot otherwise.
* **Ring Sensor:** A binary sensor that turns on when someone is ringing the doorbell.
* **Door Lock:** A lock entity that allows you to open your door with a single tap.
* **Push-Based:** Uses real-time push notifications from FERMAX servers for instant updates, avoiding inefficient polling.

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
2.  Copy the `fermax_duoxme` directory into your `custom_components` directory in your Home Assistant configuration folder.
3.  Restart Home Assistant.

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

### Integration Options

After setting up the integration, you can change its behavior by going to the integration's card on the **Devices & Services** page and clicking **Configure**.

* **Enable Doorbell Notifications**:
    * **Enabled (Default):** This is the full-featured mode. The integration will listen for real-time events from FERMAX's servers, providing instant ring notifications, live snapshots via the camera, and the door lock.
    * **Disabled:** This is a "lock-only" mode. The integration will not listen for push notifications. The camera and ring sensor entities will not be created. This is useful if you only want the ability to open the door and prefer a simpler setup without the push notification dependencies.

## Usage

Once configured, the integration will create the following entities:

* **`camera.fermax_duoxme_doorbell`**: Displays the video feed. When a call is active, it shows a placeholder image. Otherwise, it shows the last snapshot taken. This camera supports streaming and works with HomeKit.
* **`binary_sensor.fermax_duoxme_ring`**: Turns `on` when a call is initiated and `off` otherwise.
* **`lock.fermax_duoxme_door`**: Represents your door lock. To open the door, call the `unlock` service on this entity. It will show as "Unlocked" for 2 seconds and then automatically revert to "Locked".

### Example Automation

```yaml
automation:
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

### Exporting to HomeKit

You can easily export the camera to HomeKit and get alerts when someone is at the door.

Here's a sample configuration to get you started:

```yaml
homekit:
  - name: "FERMAX Doorbell"
  id: fermax_doorbell
  mode: accessory
  filter:
      include_entities:
      - camera.fermax_duoxme_doorbell
  entity_config:
      camera.fermax_duoxme_doorbell:
      linked_doorbell_sensor: binary_sensor.fermax_duoxme_ring
      support_audio: false
```

## Troubleshooting

* **PERMISSION_DENIED Error During Setup**: This error means your FCM API Key is incorrect or lacks the necessary permissions. In your Google Cloud project, ensure the key has the firebaseinstallations.installations.create permission enabled.
* **No Notifications**: If the integration sets up but you don't receive any updates, double-check all your FCM credentials. Also, check the Home Assistant logs for any errors from the custom_components.fermax_duoxme.push component.
* **Camera Feed is Blank**: This can happen if the integration fails to fetch the initial image. Try restarting Home Assistant. If the problem persists, check the logs for errors related to the FERMAX API.

## Contributing

Contributions are welcome! If you have an idea for an improvement or have found a bug, please open an issue or submit a pull request.

## License

This project is licensed under the Apache License, Version 2.0. See the LICENSE file for details.