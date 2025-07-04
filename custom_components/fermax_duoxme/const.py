"""Constants for the FERMAX DuoxMe integration."""
from typing import Final

# --- Core Integration Constants ---
DOMAIN: Final = "fermax_duoxme"
INTEGRATION_NAME: Final = "FERMAX DuoxMe"
PLATFORMS: Final = ["binary_sensor", "camera", "lock"]
STORAGE_VERSION: Final = 1
STORAGE_KEY_FCM: Final = f"{DOMAIN}_fcm_credentials"
STORAGE_KEY_TOKEN: Final = f"{DOMAIN}_oauth_token"
STORAGE_KEY_PERSISTENT_IDS: Final = f"{DOMAIN}_persistent_ids"

# --- Configuration Keys ---
# Credentials
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_CLIENT_ID: Final = "client_id"
CONF_CLIENT_SECRET: Final = "client_secret"

# FCM Configuration
CONF_FCM_API_KEY: Final = "fcm_api_key"
CONF_FCM_PROJECT_ID: Final = "fcm_project_id"
CONF_FCM_GCM_SENDER_ID: Final = "fcm_gcm_sender_id"
CONF_FCM_GMS_APP_ID: Final = "fcm_gms_app_id"
CONF_FCM_ANDROID_PACKAGE_NAME: Final = "fcm_android_package_name"

# Options
CONF_ENABLE_PUSH_NOTIFICATIONS: Final = "enable_push_notifications"

# Internal Token Storage
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_EXPIRES_AT: Final = "expires_at"

# --- API Endpoints ---
FERMAX_BASE_URL: Final = "https://pro-duoxme.fermax.io"
OAUTH_BASE_URL: Final = "https://oauth-pro-duoxme.fermax.io"
OAUTH_TOKEN_URL: Final = f"{OAUTH_BASE_URL}/oauth/token"
API_PHOTO_URL: Final = f"{FERMAX_BASE_URL}/callManager/api/v1/photocall"
API_LIST_URL: Final = f"{FERMAX_BASE_URL}/callManager/api/v1/callregistry/participant"
API_REGISTER_FCM_URL: Final = f"{FERMAX_BASE_URL}/notification/api/v1/apptoken"
API_PAIRINGS_URL: Final = f"{FERMAX_BASE_URL}/pairing/api/v3/pairings/me"
API_OPEN_DOOR_URL: Final = f"{FERMAX_BASE_URL}/deviceaction/api/v1/device/{{device_id}}/directed-opendoor"
API_ACK_URL: Final = f"{FERMAX_BASE_URL}/callmanager/api/v1/message/ack"


# --- Signals for Dispatcher ---
SIGNAL_NOTIFICATION_RECEIVED: Final = f"{DOMAIN}_notification_received"
SIGNAL_LISTENER_READY: Final = f"{DOMAIN}_listener_ready"
SIGNAL_CALL_INITIATED_WITH_IMAGE: Final = f"{DOMAIN}_call_initiated_with_image"

# --- Notification Constants ---
NOTIFICATION_TYPE_KEY: Final = "FermaxNotificationType"
NOTIFICATION_TYPE_CALL: Final = "Call"
NOTIFICATION_PHOTO_ID_KEY: Final = "photoId"
NOTIFICATION_ROOM_ID_KEY: Final = "RoomId"
NOTIFICATION_SOCKET_URL_KEY: Final = "SocketUrl"
