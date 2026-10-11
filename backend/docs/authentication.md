# How the Gmail connection works

This document explains how our app connects to Gmail. It is written so that
**anyone** can follow it — no prior knowledge needed — while still being
technically accurate for developers.

All of the behavior described here lives in one file:
[`app/gmail/client.py`](../app/gmail/client.py).

---

## 1. The problem: how do you let an app read your email safely?

Normally you sign into Gmail with your **username and password**. That works for
*you*, in a browser. But it would be dangerous to hand your password to a
program, because:

- The program would then know your *actual* password.
- A password unlocks **everything** — email, Drive, Photos, settings, the works.
- You could not easily take the access back without changing your password.

So Google does **not** let apps use your password. Instead it uses a system
called **OAuth 2.0**. The whole point of OAuth is simple:

> Let an app do a *limited* thing on your behalf, **without ever seeing your
> password**, and let you take that permission back at any time.

### A real-world analogy: the hotel keycard 🏨

Think of your Gmail password as the **master key** to your house.

- You would never mail your master key to a cleaning service.
- Instead, you give them a **keycard** that only opens the front door, only this
  week, and which you can **deactivate** anytime.

OAuth works exactly like that keycard system. Our app never gets your master key
(password). It gets a limited, revocable **keycard** instead. The rest of this
document explains where that keycard comes from and how it is stored.

---

## 2. The two secret files, explained simply

There are two important files. People mix them up constantly, so here is the
plain-English difference:

### `credentials.json` — "Who is this app?"

This file identifies **our application** to Google. Think of it as the app's
**company ID badge**. You download it once from the Google Cloud Console when you
register the app.

It answers the question: *"Which app is asking for access?"*

It does **not** contain your email, your password, or your personal data. It is
still a secret, because it proves "I am this specific app."

### `token.json` — "What did the user agree to?"

This file is created **by the app**, *after* you sign in and click "Allow." It is
your **keycard** — the proof that *you* granted this app limited access to *your*
Gmail.

It answers the question: *"Has this user given permission, and is it still
valid?"*

| | `credentials.json` | `token.json` |
| --- | --- | --- |
| **Answers** | "Which app is asking?" | "Did the user say yes?" |
| **Who creates it?** | You download it from Google | The app writes it after sign-in |
| **Analogy** | The app's company badge | Your hotel keycard |
| **Is it your password?** | ❌ No | ❌ No |
| **Keep it secret?** | ✅ Yes | ✅ Yes |

---

## 3. What is inside `token.json`? (access token vs refresh token)

`token.json` holds **two** different keys. This is the part that confuses people
the most, so let's slow down.

### The access token — a keycard that expires fast ⏱️

The **access token** is the thing the app actually shows to Gmail on every
request, as if saying *"here's my keycard, let me read the inbox."*

But for safety, an access token **expires quickly** (typically in about an hour).
That way, if someone steals it, it becomes useless soon. So it's a keycard that
**stops working after an hour**.

### The refresh token — permission to print a new keycard 🔁

If the access token expired every hour and that was the end of it, you'd have to
sign in again every hour. Annoying.

That is what the **refresh token** solves. The refresh token is a longer-lived
key whose only job is to quietly ask Google: *"my keycard expired — please print
me a fresh one."* The app can do this **in the background**, without bothering
you to sign in again.

So:

- **Access token** = the keycard you tap to get in (expires in ~1 hour).
- **Refresh token** = the pass that lets the app print a new keycard when the old
  one expires (lasts much longer, but Google can still revoke it).

---

## 4. The "scope" — how much is this keycard allowed to do?

When the app asks for access, it specifies a **scope** — the exact permission it
wants. In our code, the scope is:

```python
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify"
]
```

`gmail.modify` means: **"This app may read Gmail AND organize it — create labels
and attach/remove them from messages — but it may NOT permanently delete
mail."**

Back to the hotel analogy: the keycard now opens the **reading room** *and* lets
you **sort mail into labelled trays**, but it still cannot open the **shredder**.
Permanent deletion needs a different, broader keycard (`https://mail.google.com/`)
that we deliberately do **not** request.

### Why not the read-only keycard anymore? 🔁

Earlier this app used `gmail.readonly`, which could only *look* at mail. v1
actually *organizes* mail (it applies category labels), and you cannot attach a
label to a message with a read-only keycard. So we upgraded to the smallest
keycard that can both read messages and label them: `gmail.modify`.

We chose `gmail.modify` on purpose rather than something broader:

| Keycard (scope) | Can read? | Can create & apply labels? | Can permanently delete? |
| --- | --- | --- | --- |
| `gmail.readonly` | ✅ | ❌ | ❌ |
| `gmail.labels` | ❌ (labels only) | creates labels, but **can't attach them to messages** | ❌ |
| **`gmail.modify`** (ours) | ✅ | ✅ | ❌ |
| `https://mail.google.com/` | ✅ | ✅ | ⚠️ ✅ (we avoid this) |

**Two consequences to know:**

1. **One-time re-consent.** Changing the scope invalidates your old keycard.
   The next run you must delete `token.json` and click "Allow" again — this time
   the consent screen will mention organizing/modifying mail, not just reading.
2. **"Sensitive" scope.** Google classifies `gmail.modify` as *sensitive*. For
   **your own** account (added as a test user on the app) it works immediately.
   If this app is later published for **other** people to use, Google requires a
   verification review first. That's a publishing step, not a code change.

Even with this keycard, the damage ceiling is limited: a stolen `token.json`
could read and re-label mail, but **could not permanently delete** it.

---

## 5. The full connection flow, step by step

Here is exactly what happens when you create a `GmailClient`. This maps directly
to the `_authenticate()` method in
[`app/gmail/client.py`](../app/gmail/client.py).

```
┌─────────────────────────────────────────────────────────────┐
│  Does token.json exist?  (Do we already have a keycard?)     │
└─────────────────────────────────────────────────────────────┘
        │ YES                                  │ NO
        ▼                                       │
┌──────────────────────────┐                   │
│ Is the keycard still      │                  │
│ valid?                    │                  │
└──────────────────────────┘                   │
    │ YES          │ NO (expired)              │
    ▼              ▼                            ▼
 Use it!   ┌────────────────────┐    ┌───────────────────────────┐
           │ Do we have a        │   │ First time ever:           │
           │ refresh token?      │   │ open the browser, you sign │
           └────────────────────┘    │ in with Google & click    │
              │ YES      │ NO         │ "Allow". Google hands back │
              ▼          └──────────► │ a fresh set of keys.       │
     Quietly get a new                └───────────────────────────┘
     access token in the                        │
     background                                  │
              │                                  │
              └──────────────┬───────────────────┘
                             ▼
              ┌───────────────────────────────┐
              │ Save the keys to token.json so │
              │ next time is automatic.        │
              └───────────────────────────────┘
                             ▼
              ┌───────────────────────────────┐
              │ Build the Gmail service and    │
              │ start making API calls.        │
              └───────────────────────────────┘
```

In words:

1. **Do we already have a keycard?** The app looks for `token.json`.
   - *Code:* `if token_path.exists(): creds = Credentials.from_authorized_user_file(...)`

2. **Is the keycard usable?** If the saved token is still valid, it's used as-is
   and you are **not** bothered.
   - *Code:* `if not creds or not creds.valid:`

3. **Expired but renewable?** If the access token expired *and* we have a refresh
   token, the app quietly gets a new access token in the background — no sign-in
   needed.
   - *Code:* `if creds and creds.expired and creds.refresh_token: creds.refresh(Request())`

4. **No keycard at all (first run)?** The app reads `credentials.json`, opens your
   browser, and you sign in with Google and click **Allow**. Google sends the
   keys back to a tiny local web server the app starts on your own machine.
   - *Code:* `flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)`
     then `creds = flow.run_local_server(port=0)`
   - `port=0` just means *"pick any free port on my computer for this one-time
     handshake."* It is not a public website.

5. **Remember it for next time.** Whatever keys we ended up with are saved to
   `token.json`, so future runs skip the browser entirely.
   - *Code:* `token_path.write_text(creds.to_json())`

6. **Open the line to Gmail.** Finally, the app builds the Gmail connection using
   those credentials.
   - *Code:* `return build("gmail", "v1", credentials=creds)`

**The nice result:** You sign in **once**. After that, the app reuses and
auto-renews your keycard, so it just works — and your password was never
involved at any step.

---

## 6. What does `userId="me"` mean?

In the API calls (`list_messages`, `get_message`, `get_profile`) you'll see
`userId="me"`. This is **not** an email address or a login. It simply means:

> "Whoever these credentials belong to."

Because the keycard already identifies the account, the app never needs to type
out an email address. `"me"` = "the owner of this keycard."

---

## 7. Where the files need to be

The code refers to the two files by name only:

```python
token_path = Path("token.json")
credentials_path = Path("credentials.json")
```

Because there is no folder path in front of the names, Python looks for them in
the **current working directory** — the folder you are standing in when you run
the program. So run the app from inside the `email_agent` folder, otherwise it
won't find `credentials.json` (and won't know where to save `token.json`).

---

## 8. Safety checklist 🔒

- **Never commit or share** `credentials.json` or `token.json`. They are already
  in [`.gitignore`](../.gitignore) for this reason.
- `.gitignore` only prevents *accidental commits* — it does **not** encrypt the
  files. Treat them like passwords.
- The `gmail.modify` scope cannot permanently delete mail, but a stolen
  `token.json` could still let someone **read and re-label** your mail until the
  access is revoked.
- **If a token leaks:** go to your Google Account → *Security* → *Third-party
  access* and remove this app's access. The stolen keycard instantly stops
  working.
- **If `credentials.json` (the app badge) leaks:** rotate/regenerate the client
  secret in the Google Cloud Console and replace the file.

---

## 9. One-paragraph summary

You never give this app your Gmail password. Instead, Google issues the app a
limited, revocable **keycard** through OAuth. `credentials.json` says *which app*
is asking; after you click "Allow," `token.json` stores *your granted keycard*.
That keycard includes a short-lived **access token** (used on every request) and
a longer-lived **refresh token** (used to silently get a new access token when
the old one expires). The app can read and **organize** mail (create and apply
labels) but **cannot permanently delete** it, you sign in just once, and you can
take the access back anytime.
