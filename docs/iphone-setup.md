# iPhone setup

## Part 1 — Reminders on your phone (milestone 4)

Jarvis sends important reminders to the **ntfy** app on your iPhone. The reminder
text stays on your PC: ntfy.sh only relays a content-free "check now" signal through
Apple, and the app then fetches the text from your PC over **Tailscale**.

### 1. Turn on HTTPS for your Tailscale network (once, in a browser)

1. Open <https://login.tailscale.com/admin/dns> and sign in with the account you used on the PC.
2. Check that **MagicDNS** is enabled.
3. Under **HTTPS Certificates**, click **Enable HTTPS**.

Then tell Claude, which runs `tailscale serve` so the PC answers at its Tailscale
HTTPS address: the `NTFY_PUBLIC_URL` value in `C:\Users\LAP\JARVIS\.env`
(like `https://your-pc.your-tailnet.ts.net`).

### 2. Tailscale on the iPhone

1. Install **Tailscale** from the App Store.
2. Sign in with the **same account** as the PC, and allow the VPN configuration.
3. Leave it connected. It works on Wi-Fi and mobile data and uses very little battery.

### 3. ntfy on the iPhone

1. Install **ntfy** from the App Store and allow notifications when asked.
2. Tap **+** (subscribe to topic).
3. **Topic name:** the `NTFY_TOPIC` value from `C:\Users\LAP\JARVIS\.env` (starts with `jarvis-`).
4. Turn on **Use another server** and enter the `NTFY_PUBLIC_URL` value from `.env`.
5. Tap **Subscribe**. When it asks you to log in:
   - **Username:** `phone`
   - **Password:** the `NTFY_PHONE_PASSWORD` value from `.env`
6. The test message Claude sent may already show up.

### 4. Keep the PC side running

While these are running, reminders go out on time:

```
.venv\Scripts\python -m jarvis ntfy-server
.venv\Scripts\python -m jarvis serve      (or: .venv\Scripts\python -m jarvis voice)
```

Milestone 8 starts them automatically at Windows login.

### Checks

- `python -m jarvis notify-test` sends a test notification right away.
- `python -m jarvis remind-test` adds an important event 15 minutes out. Its 10-minute reminder arrives 5 minutes later.
- If the phone shows only **"New message"**: the alert arrived, but the phone couldn't reach the PC to fetch the text. Check that Tailscale is connected on the phone and that the PC is awake.
