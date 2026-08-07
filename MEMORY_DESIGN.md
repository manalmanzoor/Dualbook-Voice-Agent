# Memory Design

DualBook's differentiator is not that it books car washes — it's that the second
booking is shorter than the first. This document explains the three decisions
that make that work, and what each one costs.

The implementation is `dualbook/memory.py`; every section below maps to a
labelled block in that file.

---

## The problem being solved

A booking agent without memory asks a returning customer the same five questions
every time. That's not just annoying — it's the difference between a form with a
chat interface and something that behaves like a service that knows you.

But "add memory to the agent" is under-specified. Three separate decisions hide
inside it, and each has a wrong answer that looks reasonable:

| Decision | The tempting default | What we did |
|---|---|---|
| **What to write** | Feed the transcript to an LLM, ask it to extract anything notable | A fixed, typed schema of six fields |
| **How to retrieve** | Embed memories, semantic-search each turn | Load the row once, at session start |
| **Where processing happens** | Update memory continuously as facts appear | Write once, after the booking is confirmed |

The tempting defaults are what you'd build for an open-ended assistant. This is a
narrow, transactional agent, and the constraints point the other way.

---

## Decision 1 — What to write: a structured schema, not generic extraction

### The choice

`customers` stores exactly six typed fields:

```
phone                  TEXT PRIMARY KEY   -- identity, shared across both channels
customer_name          TEXT
vehicle_type           TEXT
preferred_time_of_day  TEXT               -- 'morning' | 'afternoon' | 'evening'
usual_service          TEXT
last_booking_date      TEXT
booking_count          INTEGER
```

Not stored: transcripts, greetings, small talk, sentiment, or free-text
impressions of the customer.

### Why

**The test each field has to pass is "does knowing this remove a question I'd
otherwise have to ask?"** All six do. `vehicle_type` turns "What are you
driving?" into "Same SUV as last time?". `booking_count` is what separates a
first-timer from a regular. Anything that fails that test is storage cost and
prompt noise with no conversational payoff — "customer said thanks" will never
shorten a booking.

Three concrete advantages over generic LLM extraction:

1. **Deterministic.** A schema write either fills a column or doesn't. Generic
   extraction is a model call, which means it varies run to run, and a memory
   layer that's *sometimes* right is worse than one that's predictably narrow —
   you can't build a greeting on it.
2. **No extra inference.** Generic extraction adds an LLM round-trip and its cost
   to every completed booking. We derive the same value from arguments the model
   already produced.
3. **Bounded prompt cost.** Six fields render to well under 100 tokens. Free-form
   memory grows without limit, and an agent that reads a growing blob of past
   chatter gets *worse* at slot filling, not better — the relevant facts get
   buried.

The general principle: **for a domain-specific agent, you already know what's
worth remembering. Encoding that as a schema beats asking a model to rediscover
it on every conversation.** Generic extraction earns its cost when you genuinely
can't enumerate the useful facts in advance. A car wash booking is not that
situation.

### The one place we do transform, not just copy

`preferred_time_of_day` stores a bucket, not the raw time. `derive_time_of_day()`
maps "9:30 AM" to `morning`.

This is deliberate. *"Booked 9:30am on 12 July"* is a fact about **one booking**;
*"this customer books mornings"* is a fact about **the customer**. Only the
second one is still true next month, and only the second one is useful for
proposing a slot. Memory should hold habits; the `bookings` table already holds
the exact history.

### Tradeoff accepted

The schema is rigid. If the business starts caring about (say) loyalty tier or
preferred branch, that's a migration, not a free-form note. For six stable
fields in a fixed domain, that rigidity is the feature — it's what makes the
memory trustworthy enough to build a greeting on.

---

## Decision 2 — How to retrieve: pre-loaded context, not per-turn search

### The choice

One indexed SQLite read at session start (`memory.load_profile`), rendered into
the system prompt (`memory.format_profile_for_prompt`), and then held there for
the whole conversation. No retrieval on subsequent turns.

```
Session start:  SELECT * FROM customers WHERE phone = ?   ← once
                 -> formatted into the system prompt
Every turn:      (nothing)
```

### Why

**Latency, and the fact that the corpus is one row.**

Per-turn semantic search adds a retrieval hop to every single reply. On WhatsApp
that's tolerable. On the **voice** channel it is not: Uplift targets roughly
**one second** end-to-end, and that budget is already spent on
speech-to-text → LLM → text-to-speech. A retrieval round-trip inserted into that
loop lands directly in the caller's perceived pause — the silence where a human
would have already started talking.

And there is nothing to search. Semantic search earns its latency when the
candidate set is too large to fit in context and you need to pick the relevant
slice. Our candidate set is **a single row of six fields**. Ranking one row is
strictly worse than just reading it: same result, extra infrastructure, extra
delay.

The conversation is also short — typically 4–8 turns. Pre-loading amortises one
sub-millisecond indexed lookup across the entire session. Per-turn retrieval
would pay a cost 4–8 times to produce the same six fields every time.

**Pre-loaded context is the correct low-latency default whenever the memory fits
in the prompt.** Retrieval is what you reach for when it doesn't.

### Identity is the load-bearing part

Retrieval only works if both channels agree on the key. `normalize_phone()`
collapses `923001234567@s.whatsapp.net` (WhatsApp's JID) and `+92 300 123 4567`
(a voice caller) to the same `+923001234567`. Get this wrong and memory silently
splits into two half-profiles that each look plausible — the worst failure mode,
because nothing errors. It's also what lets a customer book on WhatsApp and be
recognised when they phone.

### The voice channel: why adhoc sessions, not a persisted assistant

This is the one place where the memory design forced an architectural choice,
so it's worth spelling out.

Uplift AI offers two ways to start a realtime session:

| | Endpoint | Instructions come from |
|---|---|---|
| **Persisted assistant** | `POST /v1/realtime-assistants/{id}/createSession` | The assistant object, **fixed at provisioning time** |
| **Adhoc session** | `POST /v1/realtime-assistants/adhoc/createSession` | The request body, **per session** |

A persisted assistant holds **one instruction string shared by every caller.**
It is written once, when you provision the assistant, and every session reuses
it verbatim. The session-creation request carries only `participantName` and an
optional `roomName` — there is nowhere to put caller-specific context.

That is a structural blocker, not an inconvenience: the string cannot contain
"Ali", "SUV" or "Premium Wash", because at provisioning time we don't know who
will call. **A persisted assistant is therefore incapable of greeting a
returning caller by name**, no matter how the prompt is phrased.

So `run_voice.py` defaults to the **adhoc** endpoint, which accepts the full
config inline per session. That is where the pre-loaded profile goes:

```
inbound call, phone known
      │
      ├─► memory.load_profile(phone)          one indexed SQLite read
      │
      ├─► booking_core.build_system_prompt()  profile rendered into instructions
      │
      └─► POST /adhoc/createSession {config: {agent: {instructions: "...Ali...SUV..."}}}
                │
                └─► agent speaks first: "Welcome back, Ali! Same Premium Wash for the SUV?"
```

**This is the same Decision-2 pattern as WhatsApp, not an exception to it.** One
lookup, folded into the system prompt, zero per-turn retrieval — just moved to
the moment we learn who is calling. The two channels differ only in *where* the
prompt is assembled (in-process for WhatsApp, in the session-creation payload
for voice); the memory read is identical.

`--persisted` is still available for the fixed-assistant path, and logs a
warning that the session will not personalise. It exists to make the tradeoff
visible, not because it's a reasonable default here.

**One consequence worth noting:** because tools are declared in the same config
block, the adhoc payload also carries the `save_booking` schema — which
`booking_core` generates from the same slot definitions the WhatsApp tool uses.
Voice personalisation and voice slot filling therefore stay in lockstep with
WhatsApp automatically.

### Tradeoff accepted

Memory loaded at session start is a snapshot; a mid-conversation change to the
profile wouldn't be visible until the next session. For a booking flow that's
correct behaviour, not a limitation — the only thing that updates the profile is
the booking currently being made, and that update is meant to affect the *next*
conversation.

---

## Decision 3 — Where processing happens: post-processing write + pre-load read

### The choice

The profile is written **after** the booking completes, from the confirmed
values only — never incrementally during the conversation.

```
  read  ──►  conversation  ──►  booking confirmed  ──►  write
  (once,      (no memory I/O)     (tool call)          (once, off the
  at start)                                             response path)
```

`booking_core.complete_booking()` persists the booking, then calls
`memory.update_profile_from_booking()`. Both channels go through that one
function.

### Why

**Correctness.** Mid-conversation values are provisional. A customer who says
*"the SUV… actually, I've got the sedan today"* would, under incremental
writes, leave `vehicle_type = SUV` in permanent memory — and that wrong fact
would be confidently offered back at the start of the next conversation.
Writing only from the final confirmed booking means **memory records only things
the customer actually agreed to.** Correction-during-conversation is normal
speech, especially on voice where mishearing is routine.

**Latency.** Nothing in a booking session needs to read back what was written
earlier in that same session — the conversation history already holds it. So the
write has no reader waiting on it and can happen entirely off the response path,
after the caller has hung up or the WhatsApp reply has been sent.

Put precisely: **these sessions need cross-session memory, not mid-session
recall.** Mid-session recall is what forces you into incremental writes; we don't
need it, so we don't pay for it.

### How updates merge

`store.upsert_customer` uses `COALESCE` on every field, so a booking that didn't
mention the service type preserves the one we already knew rather than nulling
it. Memory only gets richer unless the customer explicitly changes something.
`booking_count` is the exception — it's read-then-increment, so it counts.

New customers get a fresh row with `booking_count = 1`; returning customers get
their counter bumped and any changed field overwritten.

### Tradeoff accepted

An abandoned conversation writes nothing. A customer who gives their name and
vehicle and then hangs up is still a stranger next time.

That's the right call: a half-finished booking is weak evidence about what
someone actually wants, and acting on it ("Welcome back, Ali — the SUV again?"
to someone who never completed a booking) is worse than asking. **We'd rather
under-remember than remember something wrong.** Confirmed bookings are a clean,
high-signal write trigger.

---

## What the customer experiences

**New number:**

> **Agent:** Hi! Happy to book your car wash. Can I take your name, and what are
> you driving?

Five questions to get through.

**Known number** (`booking_count: 3`, SUV, mornings, Premium Wash):

> **Agent:** Welcome back, Ali! Same Premium Wash for the SUV? Just need a day
> and time.

Two fields left, one of which is usually answered in the same breath. Same agent,
same code path — the only difference is the profile block the system prompt was
built with.

Note the phrasing the prompt enforces: remembered values are offered as
**suggestions to confirm**, never asserted as fact. Someone who drove an SUV last
year may be in a sedan today, and an agent that assumes is more irritating than
one that asks.

---

## Summary

| | Decision | Primary reason | Cost accepted |
|---|---|---|---|
| **What** | Typed 6-field schema | Deterministic, cheap, bounded prompt | Schema changes need a migration |
| **How** | Pre-load once into the prompt | Voice has a ~1s budget; corpus is one row | Snapshot, not live |
| **Where** | Post-processing write | Only records confirmed facts | Abandoned chats write nothing |

The through-line: **this is a narrow, short, transactional agent, and the memory
layer is sized to match.** The generic answers — LLM extraction, vector search,
continuous updates — are all built for open-ended assistants with large,
unpredictable memory. Applying them here would add latency and non-determinism to
buy capability the use case doesn't need.
