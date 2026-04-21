# Parental Control — Device Enrollment Guide

## Prerequisites

- Nova admin panel: `https://nova.nova-home.co.uk/admin/`
- MDM console: `https://mdm.nova-home.co.uk` (admin / linkstar)
- Headwind MDM launcher APK: `https://nova.nova-home.co.uk/admin/parental/apk`

---

## Option A — Basic Enrollment (app visible, can be uninstalled)

Use this for a quick test or if factory reset is not possible.

1. On the child's Android phone, open Chrome and navigate to:
   `https://nova.nova-home.co.uk/admin/parental/apk`
2. When prompted, allow installation from unknown sources.
3. Install and open the **Headwind MDM** app.
4. The app will ask for a server URL — tap **Scan QR Code**.
5. In Nova admin → **Parental** tab → select **Common – Minimal** → tap **Generate QR Code**.
6. Scan the QR code shown on screen.
7. The device will appear in the **Enrolled Devices** list within a few seconds.

---

## Option B — Device Owner Enrollment (stealth, cannot be uninstalled) ✓ Recommended

Device Owner mode gives full control: the MDM app is invisible, cannot be uninstalled, and survives factory resets. Requires a factory reset.

### Step-by-step

1. **Back up** the child's device (photos, contacts, etc.).
2. **Factory reset** the device:
   - Settings → General Management → Reset → Factory Data Reset.
3. Power on. At the **"Welcome / Start"** screen (before entering Wi-Fi), **tap the screen 6–7 times rapidly** in the same spot.
   - On some Samsung devices: tap the Welcome text 6 times.
   - A QR code scanner or provisioning screen will appear.
4. If prompted to download a QR reader, follow the instructions.
5. In Nova admin → **Parental** tab → select **Common – Minimal** → tap **Generate QR Code**.
6. **Scan the QR code** with the provisioning scanner on the child's device.
7. The device will automatically:
   - Download the Headwind MDM launcher (`hmdm-6.14-os.apk`) from `https://mdm.nova-home.co.uk`
   - Install it as **Device Owner**
   - Complete setup with the MDM profile applied
8. The device will appear in the **Enrolled Devices** list once setup is complete.

> **Note:** If the 6-tap method does not trigger provisioning, some devices require tapping on the Wi-Fi icon or a specific area of the welcome screen. Samsung devices may need NFC provisioning instead.

---

## After Enrollment

### Block / Unblock Apps

1. In Nova admin → **Parental** tab → click the device name in **Enrolled Devices**.
2. Enter a package name (e.g. `com.instagram.android`) or use a **Quick Block** shortcut.
3. Tap **Block** — the app will be hidden/disabled within seconds.

### Common Social Media Packages

| App | Package |
|-----|---------|
| Instagram | `com.instagram.android` |
| TikTok | `com.zhiliaoapp.musically` |
| WhatsApp | `com.whatsapp` |
| Snapchat | `com.snapchat.android` |
| X / Twitter | `com.twitter.android` |
| Facebook | `com.facebook.katana` |
| YouTube | `com.google.android.youtube` |

### Send an Alert

Select the device → enter a message in **Send Alert to Device** → tap **Send**.
The message appears as a full-screen notification on the child's phone.

### View Location

Select the device → **Last Known Location** shows coordinates with a Google Maps link.
Location is reported by the MDM app when the device is online.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| QR code not scanning | Ensure screen brightness is high; try reducing distance |
| Device not appearing after enrollment | Refresh the Enrolled Devices list; check device has internet |
| 6-tap provisioning not working | Try tapping the Welcome/Start text specifically; some devices need 7 taps |
| APK install blocked | Enable "Install unknown apps" for Chrome in device settings |
| MDM console not loading | Clear browser cache; open in new tab |
