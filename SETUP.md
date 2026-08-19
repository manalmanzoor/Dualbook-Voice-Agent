# Setup Guide — API keys and where your data goes

Two things people always ask first. Both answered here.

---

## Read this first: Vapi and WhatsApp

**Vapi has no WhatsApp channel.** Checked against Vapi's own documentation, its
channels are:

| Channel | Supported |
|---|---|
| Voice calls (phone + web) | Yes |
| Chat API (text) | Yes |
| SMS | Yes — **US numbers only** |
| **WhatsApp** | **No** |

Every "Vapi + WhatsApp" product you'll find (Make, Pabbly, n8n, Zapier) is a
third-party bridge that shuttles messages between the **Meta WhatsApp Cloud
API** and Vapi. It is not a Vapi feature.

**Vapi is also not free:** $0.05/min voice, $0.005/msg chat. Model, STT and TTS
are billed "at cost, **$0 if you bring your own API key**" — so pointing Vapi at
your free Groq key removes the model cost, but Vapi's own per-minute fee stays.

So, given "free options only", here's what this project does:

```
  WhatsApp  ──►  Meta WhatsApp Cloud API   (FREE tier)
                        │
                        └──► brain: Groq (FREE)     ← default
                             or    Vapi Chat API    ← optional, paid

  Voice     ──►  Vapi          (paid, $0.05/min — real phone calls)
                 or Uplift AI  (the original integration)
                 or `simulate`  (FREE, no keys, full logic in your terminal)
```

**Nothing is lost by staying free.** `run_whatsapp.py simulate` and
`run_voice.py simulate` run the *identical* `ConversationEngine`, slot filling,
booking write and memory layer — only the audio/transport is missing. You can
demo the entire product with one free Groq key.

If your supervisor wants Vapi specifically, it's implemented and tested —
`run_vapi.py`. Just budget for the per-minute cost.

---

## Part 1 — Which keys do I actually need?

**Short answer: one free LLM key.** Everything else is optional and only needed
for the live channels.

Copy the template, then fill in what you need:

```bash
cp .env.example .env
```

### Tier 1 — Required to run anything

| Key | Needed for | Cost |
|---|---|---|
| `GROQ_API_KEY` | The conversation brain (both agents, both simulators) | **Free** |

Get it at **https://console.groq.com/keys** — sign up, click "Create API Key",
copy it. No card required.

```env
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_your_key_here
```

That's enough to run:

```bash
python demo_data.py
python run_dashboard.py                        # dashboard, no keys needed at all
python run_whatsapp.py simulate +923001234567  # full WhatsApp agent in terminal
python run_voice.py    simulate +923001234567  # full voice agent in terminal
python run_voice.py    simulate +923001234567 --speak   # ...and hear it aloud
```

**Want to actually hear the voice agent?** `--speak` uses your OS's built-in
synthesiser (Windows System.Speech, macOS `say`, Linux espeak) — free, offline,
no install. You still type the caller's side; a paid provider is what adds
speech-to-text and a phone line.

**The dashboard needs no keys whatsoever** — it only reads the database.

### Tier 2 — Live WhatsApp (free via Meta)

Meta's WhatsApp Cloud API has a genuinely free tier: a **test number** that can
message up to 5 verified recipients, plus **1,000 free service conversations per
month** once you attach a real number.

**Step by step:**

1. Go to **https://developers.facebook.com/apps** → **Create app** → choose
   **Business**.
2. In the app, click **Add product** → **WhatsApp** → **Set up**.
3. You land on **API Setup**. Three things are on that page:

| On the page | Goes in `.env` as |
|---|---|
| **Temporary access token** (top) | `META_WA_TOKEN` |
| **Phone number ID** (under "From") | `META_WA_PHONE_NUMBER_ID` |
| **"To" → Manage phone number list** | add your own number so you can test |

> **You never scan a QR code.** Meta gives *you* a business test number — that
> number is the agent. Your own number goes in the "To" list as one of the (up
> to 5) recipients it's allowed to message. QR-scanning is how the *unofficial*
> providers link your personal WhatsApp account; this isn't that.

4. Invent any random string for `META_WA_VERIFY_TOKEN` — you'll paste the same
   one into Meta in step 6.
5. Go to **App settings → Basic → App secret → Show** and copy it into
   `META_WA_APP_SECRET`. This is what proves an incoming webhook actually came
   from Meta (see the warning after step 7).

```env
WHATSAPP_PROVIDER=meta
META_WA_TOKEN=EAAG...
META_WA_PHONE_NUMBER_ID=123456789012345
META_WA_VERIFY_TOKEN=whatever-string-you-choose
META_WA_APP_SECRET=32-hex-characters-from-app-settings
```

6. Start the server and expose it:

```bash
python run_whatsapp.py serve --port 8000
ngrok http 8000                       # separate terminal
```

7. Back in Meta: **WhatsApp → Configuration → Webhook → Edit**
   - **Callback URL:** `https://<your-id>.ngrok.io/webhook`
   - **Verify token:** the same string you chose in step 4
   - Click **Verify and save**, then **Manage** → subscribe to **messages**

Meta sends a GET request to confirm the URL. Your server echoes the challenge
back automatically — you should see `Meta webhook verified` in the logs.

> ⚠️ **The verify token is not a password.** It's checked exactly once, when
> Meta first saves the URL, and never again. Every *message* is authenticated by
> the `X-Hub-Signature-256` header, which needs `META_WA_APP_SECRET`. Without it
> the endpoint accepts any POST that reaches it — an ngrok URL is not a secret.
> The server tells you which mode it's in at startup:
>
> ```
> Webhook authentication: ON
> Webhook authentication: OFF — META_WA_APP_SECRET is not set, ...
> ```
>
> and reports `webhook_authenticated` on `GET /health`. `python run_vapi.py
> doctor` checks it too.

> ⚠️ The temporary token **expires in 24 hours**. For anything lasting, create a
> System User token: Business Settings → System Users → Add → Generate token
> with `whatsapp_business_messaging` + `whatsapp_business_management`.

### Tier 2b — Message templates (required before voice bookings get confirmed)

**TODO before going live.** WhatsApp lets you send free text to someone only
within **24 hours of their last message to you**. Replying inside a WhatsApp
chat is fine. But a confirmation for a booking taken **by phone** goes to a
number that never messaged your WhatsApp — that's an *opening* message, and only
a pre-approved **template** may open a conversation. Meta refuses free text with
error `131047`.

The code already handles the decision (`notify.choose_mode`): inside the window
it sends free text, outside it it sends the declared template. What it cannot do
for you is get a template approved. That part is manual:

- [ ] **1. Register the template.** Meta dashboard → **WhatsApp → Manage
      templates → Create template**. Category **Utility** (not Marketing —
      Utility is cheaper and approves faster for booking confirmations).
- [ ] **2. Write the body with numbered placeholders**, and keep it identical to
      `body_text` in `dualbook/templates/whatsapp.json`:
      ```
      Hi {{1}}! Your {{2}} at {{3}} is confirmed for {{4}} at {{5}}.
      Booking #{{6}}. Reply here if you need to change anything.
      ```
- [ ] **3. Submit and wait for approval.** Usually minutes, up to 24 hours. The
      status must read **Approved** — *Pending* templates fail at send time.
- [ ] **4. Copy the exact name and language code** into
      `dualbook/templates/whatsapp.json`. The name is lowercase/digits/
      underscores only, and the language code (`en_US`, not `en`) must match the
      registration character for character. A mismatch fails with `132001`,
      which reads misleadingly like the template doesn't exist.
- [ ] **5. Set the credentials** in `.env` — `META_WA_TOKEN` and
      `META_WA_PHONE_NUMBER_ID` (this project's names for what Meta's docs call
      the access token and phone number ID), plus `META_WA_APP_SECRET`.
- [ ] **6. Verify** with `python run_vapi.py doctor`, which now reports declared
      templates and flags a missing one.

Until all six are done, out-of-window confirmations are recorded in the outbox
with status **`blocked`** and the reason, rather than being attempted and
failing. Nothing else changes: in-window replies, simulated mode and the
booking itself are unaffected.

<details>
<summary>Alternative: Whapi.Cloud (paid)</summary>

```env
WHATSAPP_PROVIDER=whapi
WHAPI_TOKEN=your_whapi_channel_token
WHAPI_WEBHOOK_SECRET=some-long-random-string-you-make-up
```

Token from https://panel.whapi.cloud → create a channel. Point its webhook at
`https://<id>.ngrok.io/webhook`. `WHAPI_WEBHOOK_SECRET` is optional but
recommended — set the same value as an `X-Webhook-Secret` header on the Whapi
side, or anyone who finds your URL can POST fake bookings.
</details>

### Tier 3 — Live voice calls

Pick **one** provider. Both are implemented; neither is free.

<details open>
<summary><b>Option A — Vapi</b> ($0.05/min)</summary>

1. **https://dashboard.vapi.ai** → **Organization → API Keys**. There are two:
   - **Private key** → `VAPI_API_KEY` (server-side only, never ship it)
   - **Public key** → `VAPI_PUBLIC_KEY` (the only one safe in a browser)
2. **Phone Numbers** → buy or import one → copy its id → `VAPI_PHONE_NUMBER_ID`

```env
VAPI_API_KEY=your_private_key
VAPI_PUBLIC_KEY=your_public_key
VAPI_SERVER_SECRET=another-random-string-you-choose
VAPI_LLM_PROVIDER=groq            # your own key -> model tokens cost $0
VAPI_LLM_MODEL=openai/gpt-oss-120b
```

3. **This is the step people miss.** Vapi executes `save_booking` by POSTing to
   *your* server. Without a reachable URL the conversation sounds perfect and
   **nothing is ever saved**:

```bash
python run_vapi.py serve --port 8002
ngrok http 8002                   # separate terminal
```

```env
VAPI_SERVER_URL=https://<your-id>.ngrok.io/vapi/webhook
```

4. Create the assistant (re-run this any time you change the URL or prompt):

```bash
python run_vapi.py provision      # prints VAPI_ASSISTANT_ID -> put it in .env
```

5. Check everything, then use it:

```bash
python run_vapi.py doctor                  # what's set, what it unlocks
python run_vapi.py preflight               # RUN THIS BEFORE SPENDING CREDIT
python run_vapi.py chat +923001234567      # text conversation, no phone needed
python run_vapi.py call +923001234567      # real outbound call
```

`VAPI_SERVER_SECRET` is echoed back by Vapi as an `x-vapi-secret` header; the
webhook rejects anything that doesn't match, so nobody can forge bookings.
</details>

### Testing on a tiny budget (~$0.50)

At $0.05/min, **$0.50 is about 10 minutes of talking.** That's plenty for a
demo, but only if you don't waste it. Two facts make this cheap:

- **You don't need to buy a phone number.** Vapi's dashboard has a **Talk**
  button that starts a browser call with your mic. Free Vapi numbers also
  exist (US area codes, up to 5 per account) but you don't need one to test.
- **The audio is the only paid part.** Slot filling, memory and booking logic
  are identical in `simulate`, which is free. Debug there, spend credit only on
  the final audio run.

**The one mistake that wastes the whole $0.50:** provisioning with a webhook
URL Vapi can't reach. The call sounds perfect, the assistant says "booked!",
and nothing is saved — and you've paid for the minutes. `ngrok` also issues a
**new URL every restart**, so a URL that worked yesterday is dead today.

Order of operations:

```bash
# 1. FREE — get the conversation right first
python run_whatsapp.py simulate +923001234567
python run_voice.py    simulate +923001234567

# 2. FREE — start the webhook and expose it
python run_vapi.py serve --port 8002
ngrok http 8002                    # copy the https URL

# 3. FREE — set VAPI_SERVER_URL in .env, then provision
python run_vapi.py provision

# 4. FREE — prove the loop works end to end
python run_vapi.py preflight
#    [OK] Webhook reachable, secret accepted, handler executing.
#    [OK] Nothing was written to the database.

# 5. NOW spend credit: dashboard.vapi.ai -> Assistants -> Talk
#    Speak a booking. Then check it landed:
python view_bookings.py
```

`preflight` sends a deliberately incomplete tool call to your public URL. A
healthy setup replies *"Booking NOT saved — still missing: ..."*, which proves
the URL is reachable, the secret matches, and your handler is running — while
writing nothing to the database. It catches a dead ngrok tunnel, a stale URL, a
secret mismatch, and a missing/non-https URL.

If your credit runs out, nothing is lost: both simulators keep working on the
free Groq key, with the same booking and memory behaviour.

<details>
<summary><b>Option B — Uplift AI</b> (the original integration)</summary>

```env
UPLIFT_API_KEY=your_uplift_key
UPLIFT_LLM_PROVIDER=groq
UPLIFT_LLM_MODEL=openai/gpt-oss-120b
```

Key from https://upliftai.org → dashboard → API keys. You do **not** need
`UPLIFT_ASSISTANT_ID`: sessions are created adhoc so each caller's memory can be
injected (see [MEMORY_DESIGN.md](MEMORY_DESIGN.md)). It's only for `--persisted`.
</details>

### Swapping the LLM provider

Change `LLM_PROVIDER` and set the matching key. Nothing else changes.

| `LLM_PROVIDER` | Key to set | Free? | Get it |
|---|---|---|---|
| `groq` *(default)* | `GROQ_API_KEY` | Yes | https://console.groq.com/keys |
| `gemini` | `GEMINI_API_KEY` | Yes | https://aistudio.google.com/apikey |
| `openrouter` | `OPENROUTER_API_KEY` | Yes (`:free` models) | https://openrouter.ai/keys |
| `ollama` | *none* | Yes, fully local | https://ollama.com → `ollama pull llama3.1` |
| `openai` | `OPENAI_API_KEY` | No | https://platform.openai.com/api-keys |
| `anthropic` | `ANTHROPIC_API_KEY` | No | https://platform.claude.com |

For `anthropic` you also need `pip install anthropic`. Every other provider runs
on plain `requests`, already in `requirements.txt`.

> The model must support **function calling** — that's how the agent emits
> `save_booking` with structured fields. All the defaults above do.

### Which command needs which keys

| Command | Keys needed |
|---|---|
| `python run_dashboard.py` | **none** |
| `python demo_data.py` | **none** |
| `python view_bookings.py` | **none** |
| `python run_whatsapp.py simulate` | `GROQ_API_KEY` |
| `python run_voice.py simulate` | `GROQ_API_KEY` |
| `python run_whatsapp.py serve` | `GROQ_API_KEY` + `META_WA_*` |
| `python run_vapi.py chat` / `call` | `VAPI_API_KEY` (+ `VAPI_SERVER_URL` to save) |
| `python run_voice.py provision` | `UPLIFT_API_KEY` |

### Checking your setup

```bash
python run_vapi.py doctor              # Vapi + WhatsApp key report
python run_whatsapp.py simulate +923001234567
```

A missing key fails immediately with the exact fix:

```
Configuration error: LLM_PROVIDER is 'groq' but GROQ_API_KEY is not set.
  Get a key: https://console.groq.com/keys
  Then put GROQ_API_KEY=... in your .env
```

### Security notes

- `.env` is in `.gitignore` — never commit it.
- `.env.example` holds **placeholders only**, no real keys.
- The Uplift API key must never reach a browser client. `run_voice.py serve`
  exists precisely so the backend mints short-lived session tokens instead.

---

## Part 2 — Where does the data get saved?

Everything lives under **`dualbook/data/`**, created automatically on first run.

```
dualbook/data/
├── dualbook.db      ← SQLite: the source of truth
└── bookings.csv     ← generated on demand by the export
```

Override the location with `DB_PATH` in `.env` if you want it elsewhere.

### The database — 3 tables

**`bookings`** — every completed booking, append-only. One row per booking.

```
id, created_at, channel, customer_name, phone, contact_details,
vehicle_type, service_type, preferred_date, preferred_time, notes
```

**`customers`** — the memory layer. One row per phone number, updated after each
booking. This is what makes a returning customer get greeted by name.

```
phone (PK), customer_name, vehicle_type, preferred_time_of_day,
usual_service, last_booking_date, booking_count, updated_at
```

**`conversations`** — in-flight sessions, so the dashboard can show live
activity from a separate process. **Transient**: rows are deleted when a booking
completes, and pruned after 60 minutes of inactivity so a crashed agent doesn't
show as permanently "active".

```
phone (PK), channel, status, last_message, turn_count,
slots_json, is_returning, customer_name, started_at, updated_at
```

### Getting the data out

**1. CSV export** — writes `dualbook/data/bookings.csv`:

```bash
python view_bookings.py --csv          # console table + CSV
```

Or click **Export CSV** in the dashboard (either the Quick Action tile or the
link on the Recent Bookings card). It writes the same file and confirms the row
count and path.

The CSV opens directly in Excel / Google Sheets:

```csv
id,created_at,channel,customer_name,phone,contact_details,vehicle_type,service_type,preferred_date,preferred_time,notes
36,2026-08-03T23:50:01+00:00,whatsapp,Maryam Raza,+923078889900,+923078889900,Hatchback,Express Wash,2026-08-07,6:30 PM,
```

**2. Console** — every completed booking prints as it happens:

```
================= BOOKING CONFIRMED #1 =================
  Customer   Ali Khan
  Phone      +923001234567
  Vehicle    SUV
  Date       2026-08-12
  Time       10:00 AM
  Service    Premium Wash
  Channel    whatsapp
========================================================
```

**3. Dashboard** — `python run_dashboard.py` → http://localhost:8080

**4. JSON API** — for anything else:

| Endpoint | Returns |
|---|---|
| `GET /api/overview` | Everything the dashboard renders, one payload |
| `GET /api/bookings` | All bookings |
| `GET /api/customers` | All memory profiles |
| `POST /api/export` | Writes the CSV, returns row count + path |

**5. Straight SQL** — it's an ordinary SQLite file:

```bash
sqlite3 dualbook/data/dualbook.db "SELECT customer_name, booking_count FROM customers ORDER BY booking_count DESC;"
```

### Resetting

```bash
python demo_data.py --reset     # wipe all three tables, then re-seed
```

Or just delete `dualbook/data/dualbook.db` — it's recreated on next run.

> **Note:** seeded live conversations are pruned after 60 minutes. If the Live
> Conversations panel looks empty after a while, re-run `python demo_data.py
> --reset`. Bookings and customer profiles are permanent and unaffected.
