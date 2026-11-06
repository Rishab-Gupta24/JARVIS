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

## Part 2 — Two Shortcuts (milestone 5)

You'll build two small shortcuts:

- **Jarvis**: you speak, Jarvis answers out loud. (3 blocks)
- **Send to Jarvis**: share a WhatsApp message or a photo to Jarvis. (2 blocks)

### Before you start: the two values you need

On the PC, open `C:\Users\LAP\JARVIS\.env` and find these two lines:

- `JARVIS_PUBLIC_URL=...` → the part after `=` is your **ADDRESS**
  (it looks like `https://something.ts.net:8443`)
- `JARVIS_PHONE_TOKEN=...` → the part after `=` is your **TOKEN** (20 letters and numbers)

Keep that file open on the PC while you work on the phone. Also check that the
**Tailscale** app on the iPhone says **Connected**.

---

### Shortcut 1: "Jarvis"

**Make a new shortcut**

1. Open the **Shortcuts** app.
2. At the bottom, tap **Shortcuts** (so you see your list).
3. Tap **+** in the top-right corner. An empty shortcut opens.
4. Tap the name at the top ("New Shortcut") → **Rename** → type `Jarvis` → **Done**.

**Block 1: listen to you**

5. Tap the search bar at the bottom (**Search Actions**).
6. Type `dictate`.
7. Tap **Dictate Text**. It appears as the first block.

**Block 2: send your words to Jarvis**

8. Tap the search bar again and type `get contents`.
9. Tap **Get Contents of URL**. A second block appears that says *Get contents of URL*.
10. Tap the blue word **URL** in that block and type your **ADDRESS** followed by `/ask`.
    Example: `https://something.ts.net:8443/ask`. Tap **Done**.
11. Tap the small **arrow (›)** on the right of that block to open its options.
12. Tap **Method** and choose **POST**.
13. Tap **Headers**, then **Add new header**:
    - In **Key**, type `Authorization`
    - In **Text**, type `Bearer ` (the word Bearer, then **one space**), then your **TOKEN**.
      Example: `Bearer abcd2345efgh6789jkmn`
14. Tap **Request Body** and choose **JSON**.
15. Tap **Add new field** → **Text**.
    - In **Key**, type `text`
    - Tap the right-hand box (**Text**) and choose the variable **Dictated Text**
      from the bar above the keyboard.

    (Don't use **File** here: Shortcuts sends nothing when spoken text is put in a File
    body, and Jarvis replies "I didn't hear anything.")

**Block 3: say the answer**

16. Tap the search bar, type `speak`, and tap **Speak Text**.
17. The new block should already read *Speak Contents of URL*. If it shows something
    else, tap that blue word and choose **Contents of URL**.
    Tap its **›** arrow: turn on **Wait Until Finished**, and set **Voice** to a voice with a
    real name (Rishi or Isha for English (India), or Samantha for English (US)).
    A voice called "Voice 1" may not be installed, and then nothing is spoken.

**Done**

18. Tap **Done** (top-right).

**Try it:** tap the **Jarvis** tile, wait for the listening sound, and say
*"What's on tomorrow?"*. You should hear the answer.

**Use it hands-free:**
- Say **"Hey Siri, Jarvis"**, then speak your request.
- **Action Button** (iPhone 15 Pro and newer): Settings → Action Button → swipe to **Shortcut** → choose **Jarvis**.
- **Back Tap** (any iPhone): Settings → Accessibility → Touch → Back Tap → Double Tap → **Jarvis**.

If Jarvis asks you something back ("Which one, Friday or Saturday?"), just run the
shortcut again within 10 minutes and answer.

---

### Shortcut 2: "Send to Jarvis"

**Make a new shortcut**

1. In the Shortcuts list, tap **+**.
2. Tap the name at the top → **Rename** → type `Send to Jarvis` → **Done**.

**Let it appear in the Share menu**

3. Tap the name at the top again → **Details** (or tap the **ⓘ** icon at the bottom).
4. Turn on **Show in Share Sheet**. Tap **Done**.
5. A block appears at the top: *Receive **Any** input from **Share Sheet***. Leave it as it is.

**Block 1: send whatever you shared to Jarvis**

6. Tap the search bar, type `get contents`, and tap **Get Contents of URL**.
7. Tap the blue word **URL** and type your **ADDRESS** followed by `/send`.
   Example: `https://something.ts.net:8443/send`. Tap **Done**.
8. Tap the **arrow (›)** on the block to open its options.
9. **Method** → **POST**.
10. **Headers** → **Add new header**: Key `Authorization`, Text `Bearer ` + your **TOKEN**
    (the same as in Shortcut 1).
11. **Request Body** → **Form**.
12. Tap **Add new field** → **Text**: Key `text`, value the variable **Shortcut Input**.
13. Tap **Add new field** again → **File**: Key `file`, value **Shortcut Input** again.

    (Both fields get the same thing. A shared message arrives through the text field and a
    photo or PDF through the file field, and Jarvis uses whichever has something in it.)

**Block 2: show Jarvis's reply**

14. Tap the search bar, type `notification`, and tap **Show Notification**.
15. Tap the blue text in that block (it may say *Hello World!*), delete it, and pick the
    variable **Contents of URL** from the bar above the keyboard.

**Done**

16. Tap **Done**.

**Try it with a WhatsApp message (the milestone test):**
1. In WhatsApp, open a chat with a message about an event.
2. Long-press the message → **Forward** → tap the **share icon** (bottom-right) → **Send to Jarvis**.
3. A notification says something like *"Added DBMS lab viva, Thursday 12 November at 2 pm."*

**Send the weekly mess menu as a PDF:**
1. Wherever the PDF is (WhatsApp, Mail, Files), tap **Share** → **Send to Jarvis**.
2. A notification says something like *"Got the mess menu for 2 to 8 November, 28 meals."*
   If the menu doesn't show its dates, Jarvis asks which week it covers; run **Jarvis** and answer,
   for example *"It's for this week"*.

Excel and CSV menus work the same way. **Menus from photos aren't saved:** on a real
notice-board photo the local vision model got only about 72% of dishes exactly right, so
Jarvis asks for the PDF instead.

**Photos of notices and posters** do work: share one and any events it mentions are added.
The reply arrives as an **ntfy** notification after 15 seconds to 2 minutes.

---

### If something goes wrong

| What you see | What to do |
| --- | --- |
| "Could not connect to the server" | Open the Tailscale app and make sure it says **Connected**. Check that the PC is on, with `jarvis serve` running. |
| "Missing or wrong token" | Open the **Get Contents of URL** block → Headers. It must be exactly `Bearer` + one space + TOKEN, with no extra spaces. |
| Nothing is spoken, or the notification is empty | In the **Speak** or **Show Notification** block, check the blue variable is **Contents of URL**. |
| "Send to Jarvis" isn't in the Share menu | Open the shortcut → **ⓘ** → turn on **Show in Share Sheet**. In the share menu, scroll right in the bottom row, or tap **Edit Actions**, to find it. |

Offline use (no PC reachable, sync later) comes in milestone 7.
