# DualBook

Two conversational car wash booking agents — **WhatsApp** and **Voice** — sharing
one booking store and one customer memory layer, plus the two web pages that put
them in front of real people: a **customer booking page** and an **owner
console**.

A returning customer is recognised by phone number and greeted with their prior
preferences, whichever channel they use:

```
New number      >  "Hi! Can I take your name, and what are you driving?"
Known number    >  "Welcome back, Ali! Same Premium Wash for the SUV?
                    Just need a day and time."
```

The memory layer is the interesting part — the design writeup is in
**[MEMORY_DESIGN.md](MEMORY_DESIGN.md)**.

### Why this is more than a chatbot

Anyone can wire an LLM to a booking form. Four things here are deliberately
harder, and they are what a business owner is actually buying:

1. **It remembers, and the saving is measured.** Returning customers book in
   **2.9 turns against 7.4** for a first-timer in the seeded history — a 61%
   shorter conversation. That number is computed from `bookings.turns` on the
   Analytics page, not asserted on a slide, and bookings made before turn
   tracking existed are reported as untracked rather than estimated.

2. **Recognition is earned, never assumed.** The customer page starts with no
   identity at all. The agent asks for a name, then a number, and only *then*
   looks anyone up. There is no admin field to prefill a name and fake a warm
   greeting.

3. **The business's rules actually bind.** Opening hours, the priced service
   menu and the booking window live in the owner's Settings and are enforced
   inside the `save_booking` tool — so the agent physically cannot confirm 8pm
   on a Sunday. A prompt asks; `validate.py` refuses, and hands back a reason
   the agent can use in conversation.

4. **Nothing invented reaches a customer.** A phone number that the customer
   never said is rejected as fabricated (`resolve_contact`), every number is
   normalised and sanity-checked before any message is sent, and every
   confirmation — sent or merely simulated — is recorded in an outbox the owner
   can read.

> **TODO before live WhatsApp delivery:** confirmations for bookings taken by
> *phone* go to a number that never messaged us, which WhatsApp only permits as
> a pre-approved **template**. The send path already picks the right shape; the
> template still has to be registered and approved in Meta's dashboard. Six-step
> checklist: [SETUP.md → Message templates](SETUP.md). Until then those sends
> are recorded as `blocked` with the reason, and everything else is unaffected.

**New here?** [SETUP.md](SETUP.md) covers exactly which API keys you need (one
free one is enough) and where your data is saved.

### Providers — and one correction worth knowing

| Role | Options |
|---|---|
| **Brain** | Groq *(free, default)* · Gemini · OpenRouter · Ollama · OpenAI · Anthropic |
| **WhatsApp** | **Meta WhatsApp Cloud API** *(free tier)* · Whapi.Cloud |
| **Voice** | Vapi · Uplift AI · `simulate` *(free, no keys)* |

**Vapi does not have a WhatsApp channel.** Its channels are voice, web chat and
SMS (US only) — verified against Vapi's docs. Every "Vapi + WhatsApp" product is
a third-party bridge over the Meta Cloud API. Vapi is also not free ($0.05/min
voice, $0.005/msg chat), though model tokens are $0 if you bring your own key.

So WhatsApp runs on Meta's free tier, and Vapi is wired up for voice
(`run_vapi.py`) if you want it. Both simulators run the full booking + memory
logic with no paid keys at all.

---

## Architecture

```
   WhatsApp                                              Phone call
   (Whapi.Cloud)                                         (Uplift AI Realtime)
        │                                                       │
        │ webhook POST                                  WebRTC / LiveKit room
        ▼                                                       ▼
 ┌──────────────────┐                                 ┌──────────────────────┐
 │ run_whatsapp.py  │                                 │    run_voice.py      │
 │ FastAPI webhook  │                                 │ session broker + RPC │
 └────────┬─────────┘                                 └──────────┬───────────┘
          │                                                      │
          │ whatsapp_client.py                    uplift_client.py│
          │ (provider isolated)                  (provider isolated)
          │                                                      │
          └──────────────────────┬───────────────────────────────┘
                                 ▼
                    ┌─────────────────────────┐
                    │     booking_core.py     │   ◄── ALL business rules
                    │  • slot definitions     │       live here, once
                    │  • save_booking tool    │
                    │  • system prompt        │
                    │  • complete_booking()   │
                    └───────┬────────┬────────┘
                            │        │
                   llm.py ◄─┘        └─► memory.py ──► store.py
              (provider-swappable)      (profiles)     (SQLite + CSV)
              groq / gemini /                          ├── bookings
              openrouter / ollama                      ├── customers
                                                       └── conversations
                                                              ▲
                                                              │ reads only
                                              ┌───────────────┴────────────┐
                                              │      run_dashboard.py      │
                                              │  admin UI on :8080         │
                                              │  (its own process)         │
                                              └────────────────────────────┘
```

**Why it's split this way:** the two agents differ only in *transport*.
Everything that is a business rule — which fields make a booking, the tool
contract, the prompt, what "a booking happened" means — lives in
`booking_core.py` and is imported by both. Adding a slot changes both channels
at once. The vendor-specific HTTP lives in exactly two files
(`whatsapp_client.py`, `uplift_client.py`), so either provider can be swapped
without touching anything else.

### Booking flow

```
message/speech ──► LLM with the save_booking tool
                        │
                   still missing fields? ──► ask for 1-2 of them, loop
                        │
                   all fields collected ──► read back, customer confirms
                        │
                   save_booking(...) ──► complete_booking()
                                            ├─► INSERT INTO bookings
                                            ├─► upsert customer profile   ← memory write
                                            └─► print to console
```

---

## Files

| File | Purpose |
|---|---|
| `run_whatsapp.py` | **Entry point** — WhatsApp webhook server + terminal simulator |
| `run_voice.py` | **Entry point** — Uplift assistant provisioning, sessions, RPC host, simulator |
| `run_dashboard.py` | **Entry point** — serves both pages, the admin API, and the agent (`/api/agent/*`) |
| `dualbook/static/dashboard.html` | Owner console: overview, bookings, customers, analytics, settings (self-contained, no CDN) |
| `dualbook/static/book.html` | Customer booking page — anonymous start, speech via the browser's Web Speech API |
| `dualbook/settings_store.py` | Business configuration the owner edits and the agent obeys |
| `dualbook/validate.py` | Phone numbers (E.164) and booking slots vs opening hours — the checks that actually hold |
| `dualbook/notify.py` | Outbound WhatsApp confirmations, with the outbox that records what was (or would be) sent |
| `dualbook/wa_templates.py` | Loads and validates the approved message templates used outside the 24-hour window |
| `dualbook/templates/whatsapp.json` | Where you declare template name, language code and variables — edit this, not the code |
| `dualbook/analytics.py` | Read-only aggregates that feed the dashboard |
| `dualbook/booking_core.py` | Shared slot filling, tool contract, prompt, `complete_booking()` |
| `dualbook/memory.py` | Customer memory: schema, pre-load read, post-processing write |
| `dualbook/store.py` | SQLite (`bookings`, `customers`) + CSV export |
| `dualbook/llm.py` | Provider-swappable LLM client (Groq / Gemini / OpenRouter / Ollama / OpenAI / Anthropic) |
| `dualbook/whatsapp_client.py` | Meta Cloud API + Whapi transports (**only file that knows the provider**) |
| `dualbook/uplift_client.py` | Uplift AI assistants + sessions (**only file that knows the provider**) |
| `dualbook/vapi_client.py` | Vapi assistants, Chat API, calls, tool webhook |
| `run_vapi.py` | **Entry point** — Vapi provision / serve / chat / call / doctor |
| `dualbook/config.py` | Environment configuration |
| `demo_data.py` | Seeds one returning customer for the demo |
| `view_bookings.py` | Business-owner view + CSV export |

---

## Setup

```bash
git clone <repo> && cd Voice-Agent-Task
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
cp .env.example .env            # then edit .env
```

### Minimum config to run the demo

You only need **one free LLM key**. WhatsApp and Uplift credentials are needed
only for the live channels, not for the simulators.

```env
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...            # free: https://console.groq.com/keys
```

**Other free options** — set `LLM_PROVIDER` and the matching key:

| Provider | Key | Get it | Notes |
|---|---|---|---|
| `groq` | `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) | **Default.** Free tier, fast, good function calling |
| `gemini` | `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Free tier |
| `openrouter` | `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) | Free models (`:free` suffix) |
| `ollama` | *(none)* | [ollama.com](https://ollama.com) → `ollama pull llama3.1` | Fully local, no account |
| `anthropic` | `ANTHROPIC_API_KEY` | [platform.claude.com](https://platform.claude.com) | Paid; also `pip install anthropic` |

The agent needs a model with reliable **function calling** — that's how
`save_booking` gets emitted with structured arguments. All defaults above
support it.

---

## Running it

### Quickest path — one command, two pages

```bash
python demo_data.py          # seeds a business with history (optional)
python run_dashboard.py      # -> http://localhost:8080
```

One server, **two pages, for two different people**:

| URL | Who it's for | What it does |
|---|---|---|
| `/` | The car wash owner | Bookings, customers, analytics, settings, live monitoring |
| `/book` | The customer | Talks or types to the agent and makes a booking |

**Start at `/book`** — that's the product. Press **Talk to book**, allow the
microphone, and just speak; the browser transcribes you (Web Speech API), the
agent answers out loud, and the mic re-opens after every reply. **Type instead**
runs the same agent over text, exactly as a WhatsApp customer would experience
it. Use Chrome or Edge to speak — Firefox and Safari have no `SpeechRecognition`,
and the page says so rather than failing silently.

Then open `/` and watch the booking you just made land in the stat cards, the
bookings table, the customer's profile and the analytics.

#### The bit worth demoing: the recognition moment

The customer page starts **anonymous**. There is no phone field to prefill, so
the agent cannot greet anyone by a name it was handed:

1. It introduces itself and asks *your name*.
2. Then it asks for your mobile number, and says why — that's where the
   confirmation goes.
3. The moment you give a number, the server does one indexed lookup
   (`ConversationEngine.identify`). If it matches a profile, the agent's very
   next sentence is *"Welcome back, Ali — same Premium Wash for the SUV?"*, and
   the page shows a "we found your details" banner.

Book once as a new number, then book again with the same number, and the second
conversation is visibly shorter. That difference is measured, not asserted — see
**Analytics → What the memory layer is worth**.

Nothing beyond `GROQ_API_KEY` is required for any of this: the browser supplies
the microphone and the speaker, so no telephony account is involved. The sections
below are the *other* transports — real WhatsApp messages and real phone calls —
which reach the identical booking logic.

> **Free-tier note.** Groq meters quota per model. If the default
> (`openai/gpt-oss-120b`) hits its daily cap mid-demo, the client
> automatically drops to `openai/gpt-oss-20b` and carries on, logging the
> switch. Pin a model with `LLM_MODEL=` in `.env` to disable that.

### 1. Seed the demo customer

```bash
python demo_data.py
```

Creates `Ali` — `+923001234567`, SUV, mornings, Premium Wash, 3 prior bookings.

### 2. WhatsApp agent

**Terminal simulator** (no WhatsApp credentials needed):

```bash
python run_whatsapp.py simulate +923001234567    # returning -> greeted by name
python run_whatsapp.py simulate +923339998877    # new       -> asked everything
```

**Live webhook server:**

```bash
python run_whatsapp.py serve --port 8000
# expose it:  ngrok http 8000
# then point your Whapi.Cloud channel webhook at  https://<id>.ngrok.io/webhook
```

Set `WHAPI_TOKEN` in `.env`. Optionally set `WHAPI_WEBHOOK_SECRET` and add the
same value as an `X-Webhook-Secret` header on the Whapi webhook so the endpoint
can't be spammed with fabricated bookings.

### 3. Voice agent

**Terminal simulator** (no Uplift credentials or microphone needed) — runs the
voice prompt and writes bookings tagged `channel='voice'`:

```bash
python run_voice.py simulate +923001234567
python run_voice.py simulate +923001234567 --speak   # hear it out loud
```

`--speak` synthesises the agent's replies through your operating system's own
voice — Windows System.Speech, macOS `say`, or espeak on Linux. Free, offline,
nothing to install. You still type the caller's side: local speech-to-text is a
separate dependency, and recognising the caller is the half a real voice
provider actually sells.

The synthesiser starts warming up in a background thread as soon as the
simulator launches, because loading the Windows speech assembly takes ~20s and
doing it lazily would leave dead air after the agent had already answered.

**Live, against Uplift AI** (set `UPLIFT_API_KEY` in `.env`):

```bash
python run_voice.py provision                      # create the persisted assistant (optional)
python run_voice.py session +923001234567          # mint an adhoc session (memory pre-loaded)
python run_voice.py session +923001234567 --join   # ...and host the save_booking RPC handler
python run_voice.py serve --port 8001              # backend token endpoint for a client

# Tool host alongside a separate speaking client, in the same room:
python run_voice.py session +923001234567 --room dualbook-r1 --join
```

### 4. View what was collected

**Dashboard** — needs no API keys at all, it only reads the database:

```bash
python run_dashboard.py           # http://localhost:8080
```

**Console / CSV:**

```bash
python view_bookings.py           # bookings + memory profiles
python view_bookings.py --csv     # also export to dualbook/data/bookings.csv
```

Completed bookings also print to the console as they happen.

Full detail on storage locations, table schemas and export options is in
[SETUP.md](SETUP.md#part-2--where-does-the-data-get-saved).

---

## The dashboard

```bash
python run_dashboard.py --port 8080
```

Five sections, each answering a different question. Every figure is derived from
real rows — there are no placeholder numbers anywhere in it.

| Section | Answers | Source |
|---|---|---|
| **Overview** | What's happening right now? | stats, live `conversations`, today's rota, trend, memory insights, confirmations |
| **Bookings** | What's the job list? | `query_bookings()` — filter by search / channel / status / service / date range, sort, page, and change a booking's state |
| **Customers** | Who are these people? | `customers` + their history; click any row for the profile the agent recalls |
| **Analytics** | Is this working, and what's it worth? | turns-to-book impact, daily volume by channel, service mix + revenue, time-of-day, status |
| **Settings** | What is the business? | `settings` table — name, priced menu, opening hours, confirmation message |

**Bookings are operational, not decorative.** *Done* / *Cancel* / *Restore*
write a real `status` to the row, and the analytics, today's rota and the CSV
export all respect it. Export honours the filters you can see, because handing
someone the whole book when they filtered to "cancelled, this week" is a quiet
way to give them the wrong spreadsheet.

### Settings actually drive the agent

This is the difference between a settings screen and configuration. What you
save is read at the start of the *next* conversation — no restart:

- **Service menu with prices** → the agent quotes them, and `save_booking`
  rejects a service that isn't on the list.
- **Opening hours** → `validate.booking_slot()` refuses a slot outside them and
  hands the reason back to the model, so the agent says *"we're closed Sundays —
  Saturday at 10?"* instead of confirming something impossible.
- **Booking window** → nothing further out than N days.
- **Confirmation message** → the WhatsApp text your customer receives, with
  placeholder validation before it can be saved.

`.env` still holds developer configuration (API keys, provider, database path)
and still needs a restart. The Settings page shows those as read-only facts.

### The API

| Endpoint | Does |
|---|---|
| `POST /api/agent/start` | Opens a session — `{channel}` alone for an anonymous web visitor, `{channel, phone}` when a transport already knows the number |
| `POST /api/agent/say` | `{session, text}` — one customer turn in, one agent turn out |
| `POST /api/agent/end` | Drops the transcript and the live row |
| `GET /api/agent/intro` | Business name, priced menu and hours for the customer page |
| `GET /api/bookings` | Filtered/sorted/paged; `POST /api/bookings/{id}/status` to change one |
| `GET /api/customers`, `GET /api/customers/{phone}` | Profiles, and one customer's full history |
| `GET /api/analytics?days=` | Everything the Analytics section draws |
| `GET/PUT /api/settings` | Business configuration, validated on write |
| `POST /api/whatsapp/validate`, `/api/whatsapp/test` | Check a number, then send a real test message |

Sessions are keyed by an opaque session id, **not** by phone number — because a
web visitor doesn't have one yet. That single decision is what makes the
anonymous → identified → recognised flow possible. Each session is retired the
moment a booking saves, so the next conversation re-reads the profile that
booking just updated.

**Two deliberate choices worth knowing about:**

**There is no "Satisfaction Rate" card.** We never ask customers to rate
anything, so any number there would be invented. It's replaced by **Memory Hit
Rate** — the share of bookings made by a customer we already had a profile for,
which is the metric this project exists to move.

**Percentage changes are suppressed below a baseline of 3.** Going from 1
booking to 17 is "+1600%", which is arithmetically true and completely useless —
it describes the tiny denominator, not the business. Those cards show a neutral
dash instead.

### How live monitoring works across processes

The agents hold the transcript in memory, but publish a small summary — channel,
last message, turn count, which slots are filled — to the `conversations` table
after each turn. The dashboard reads that. The write is best-effort and wrapped
in a `try`, because monitoring must never break a customer conversation.

The slot dots are the memory layer made visible: a **returning** customer starts
at 4/6 before saying anything, because their name, vehicle, service and number
are already known. A new customer starts at 0/6. Hover any dot to see which
field it is.

Rows are deleted when a booking completes and pruned after 60 minutes of
inactivity, so a crashed agent doesn't sit there looking "active" forever.

---

## How the voice agent captures the booking

Worth knowing before reading `run_voice.py`, because it's the one genuinely
non-obvious part of the design.

**Uplift executes assistant tools as RPC calls against a participant in the
LiveKit room — there is no server-side webhook tool type.** So something has to
join the room and answer that RPC. `run_voice.py session --join` does exactly
that: it connects as a Python participant, registers `save_booking`, and the
handler calls straight into `booking_core.complete_booking()` — the same
function the WhatsApp tool handler calls. The booking write stays in Python,
next to the store and the memory layer, and no business rule is duplicated.

If you'd rather drive the call from a browser or mobile LiveKit client, use
`run_voice.py serve` for the token (Uplift's docs are explicit that the API key
must never reach the client) — but that client then has to register the
`save_booking` RPC handler itself.

The handler is deliberately tolerant about the invocation payload shape: the
LiveKit SDK hands over `payload` as a JSON string, while Uplift's own examples
show arguments nested at `payload.arguments.raw_arguments`. It accepts both
rather than betting on one and failing silently mid-call.

### Participant identity — pick one of two topologies

A session token identifies **exactly one participant**, and LiveKit rejects two
participants sharing an identity. So:

| Topology | Setup |
|---|---|
| **Headless demo** | This process is the only participant and hosts the tool: `session <phone> --join` |
| **Real call** | A browser/mobile client speaks to the caller; a **second** token joins the same room as the tool host: `session <phone> --room <name> --join` |

If your speaking client registers `save_booking` itself, skip the tool host —
but that handler must still call `booking_core.complete_booking()`, or voice
forks away from the shared booking path.

### Per-caller memory uses adhoc sessions

A persisted Uplift assistant carries **one fixed instruction string for every
caller**, written at provisioning time. The session-creation request for a
persisted assistant carries only `participantName` and `roomName` — there is
nowhere to put caller context. It therefore *structurally* cannot say "Welcome
back, Ali".

`run_voice.py session` defaults to the **adhoc** endpoint, which takes the
config inline per session, and injects the caller's profile into the
instructions at creation time — still one lookup, still zero per-turn
retrieval. `--persisted` uses the fixed assistant and logs a warning that the
call will not personalise. Full reasoning in
[MEMORY_DESIGN.md](MEMORY_DESIGN.md#the-voice-channel-why-adhoc-sessions-not-a-persisted-assistant).

---

## Data model

**`bookings`** — append-only log: `phone`, `customer_name`, `vehicle_type`,
`preferred_date`, `preferred_time`, `service_type`, `contact_details`, `channel`,
`notes`, `created_at`.

**`customers`** — the memory profile, keyed on phone: `customer_name`,
`vehicle_type`, `preferred_time_of_day`, `usual_service`, `last_booking_date`,
`booking_count`.

**`conversations`** — transient live-session state for the dashboard: `channel`,
`status`, `last_message`, `turn_count`, `slots_json`, `is_returning`. Deleted on
completion, pruned after 60 minutes idle.

Phone numbers are normalised (`memory.normalize_phone`) so WhatsApp's
`923001234567@s.whatsapp.net` and a voice caller's `+92 300 123 4567` resolve to
the same profile. That's what lets a customer book on WhatsApp and be recognised
when they phone.

---

## Verified behaviour

Checked end to end against a stub LLM server (no paid API involved):

- Slot-filling loop → tool call → booking persisted → profile upserted
- Tool schema is emitted in OpenAI *and* Anthropic wire shapes from one definition
- Second session pre-loads the profile with **zero** LLM or retrieval round-trips
- Cross-channel memory: booking on WhatsApp, recognised on voice, one profile
- `COALESCE` merge preserves prior facts a later booking didn't mention
- Newer values overwrite older ones; `booking_count` increments correctly
- RPC payload parsing across 5 shape variants, plus bytes and malformed input
- Webhook parsing ignores `from_me` echoes and non-text messages
- Dashboard stat cards reconcile exactly with `/api/bookings` and `/api/customers`
- Dashboard page renders with **zero console errors** and **zero external
  requests** (verified in a real browser — it works offline)
- No horizontal overflow at any width from **390px to 1920px**, and the bookings
  table never clips its Status column

Checked against the live free-tier model, in a real browser:

- Anonymous customer session: agent asks for a name, then a number, and only
  says *"Welcome back, Ali"* on the turn the number matched a profile
- A closed day is refused in conversation — asked for Sunday, the agent offered
  Saturday and saved the booking against the day the business is open
- Booking made from `/book` appears in the owner's stat cards, bookings table,
  customer profile and outbox
- Bookings filters (search / channel / status / service / dates), sorting,
  paging and status changes all round-trip through SQLite
- Settings save → validation errors surface inline → saved values reach the
  agent's next system prompt
- Both pages render with **zero console errors** and **zero external requests**

---

## Notes and limitations

- **The Analytics impact figure depends on seeded history.** `demo_data.py`
  writes plausible turn counts for its own rows (a cold start takes 6-9 turns, a
  remembered customer 2-4) so a fresh install has a chart worth looking at.
  Real bookings record their real turn count, and `demo_data.py --backfill`
  touches seeded rows only — a real conversation is never given an invented
  number.

- **Session state is in-process.** Each phone number's live transcript is held in
  a dict, so it doesn't survive a restart or span multiple workers. The durable
  half — the customer profile — is in SQLite; losing a transcript just means the
  agent re-asks the current question. Redis would be the swap for production.
- **No availability checking.** The agent accepts any date/time; it doesn't book
  against a real calendar. Out of scope per the PRD (listed as a stretch goal).
- **No payment processing.** Also out of scope.
- **`derive_time_of_day` handles digits, not spelled-out numbers.** "4:30 PM"
  buckets correctly; "half past four" returns `None` — which is safe, since
  `COALESCE` then preserves whatever was already known.
- The default LLMs are free-tier models, which follow instructions less tightly
  than a frontier model. `booking_core` re-validates required slots server-side
  before writing, so a premature tool call is rejected with a message the model
  can recover from rather than persisting a half-filled booking.
