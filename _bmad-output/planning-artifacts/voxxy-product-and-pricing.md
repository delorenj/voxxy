# Voxxy as a product: editions, pricing, keys and store listing

Status: **decided 2026-11-23** (section 10). Jarad delegated N1 to N16 ("answer the rest on my
behalf. I trust your judgement", relayed draft); the agent decided them after three independent
reviews (economics, risk, operator time). Where a decision below differs from the body of this
doc, the decision wins. Still nothing created in Stripe, nothing published, no spending: the
go-live ticket per product (N14) is the one thing Jarad signs.

Level (pillar `belsky-doctrine`): the ask names Voxxy. This plan is the AutomaticAI
store's selling pattern for **any second-and-later product**: one product page, a
hosted edition and a self-hosted edition, one go-live gate per thing sold, a
per-product entry in the store's key issuer, and a consent rule for anything that
touches a person's likeness. Voxxy is the first product to use it. Earshot, the
first product, stays as it is.

Source of the decisions: Jarad, dictated 2026-11-23. "Voxy"/"boxy" are Voxxy.
"Gravado" is `~/code/gruvato` (checked, see section 9).

## 1. Recommendation at a glance

| | Hosted | Self-hosted |
|---|---|---|
| Who runs it | AutomaticAI, on Jarad's GPU box | The buyer, on their own NVIDIA GPU |
| Price | Solo $9/mo, Studio $19/mo, Team $59/mo (annual = 10 months) | $49 once, 12 months of updates, keep running it forever |
| Cloning | Your own voice only, verified. Plus "describe a voice" | The buyer's responsibility (section 5) |
| Keys | Per-customer API keys, many per account | One signed license key, checked offline |
| Sold at | `store.automaticai.io/voxxy/`, Stripe payment links | Same page, separate payment link |

Order of work (section 8): self-hosted first, hosted as a capped beta second. The
reason is in section 3.3.

## 2. Product definition and who buys

Voxxy is a speech service: you send text, you get audio in a chosen voice. It has an
HTTP API (including an OpenAI-compatible `/v1/audio/speech`), an MCP endpoint for
agents, a `voxxy` command line tool, and a "sink" relay that plays audio on the
machine you are sitting at. Two GPU engines can sit behind it, with ElevenLabs as an
optional paid fallback (`README.md`, `app/main.py`).

What is sold at launch is **VoxCPM2 only**. It is Apache-2.0 and speaks 30 languages
(`engines/voxcpm/README.md`, README "voice-design" notes). VibeVoice is research-only
licensed and watermarks its output (`house-voices/README.md`, `app/engines.py`
docstring), and it is not deployed today (`/healthz` lists voxcpm and elevenlabs).
So "multi-engine" is an architecture, not a launch feature. See Needs Jarad N9.

| Edition | Who buys | Why this one |
|---|---|---|
| Hosted | Developers, creators and small teams without an NVIDIA GPU. Podcasters and video makers who want their own voice reading scripts. Agent builders who want a speech endpoint | They want audio, not a GPU project. They want their own voice, legitimately cloned |
| Self-hosted | Developers and tinkerers with a spare NVIDIA GPU. Small studios who want flat cost and no per-character meter. People who keep their text off other people's servers | They want to own it. $49 buys the packaged build, the install path and a year of updates, not the model (the model is open and free) |

Not for sale to anyone: the fleet voices in `voices/` (celebrity and character
clones such as `arnold`, `joe-pesci`, `carlin`, `marge`, `bluey`). They stay on
Jarad's own service. The only catalog voices that ship are the four synthetic
`house-*` voices, plus new designed ones (section 4.2).

## 3. Tiers, prices and the cost they sit on

### 3.1 What costs money (from the real architecture)

| Cost | Reality today | Effect on pricing |
|---|---|---|
| GPU time | One RTX 3090 (24 GB). VoxCPM2 holds about 5 GB; VibeVoice would hold about 7.5 GB more. The same box serves Jarad's agent fleet through `vox.delo.sh` | Marginal cost per character is near zero. **Capacity** is the limit: customers share the GPU with the fleet. Cap the number of hosted customers |
| ElevenLabs fallback | Pay per character. `app/engines.py` appends it last whenever `ELEVENLABS_API_KEY` is set. It cannot clone a per-request clip | **Off for every customer key.** Only Jarad's root key may fall back. A customer whose GPU engine is down gets a 503 with a retry hint, never a silent paid call. ElevenLabs' own terms on output reuse also vary by plan; not checked, another reason to keep it out |
| Stripe fees | Standard published US card rate, about 2.9% + 30 cents (check the account) | About $1.72 on $49, about $0.56 on $9 |
| Storage, bandwidth | Voice clips of 30 seconds or less, an audio cache with a 1 hour TTL, consent clips | Negligible |
| Jarad's time | Manual key emails inside 24 hours (Earshot's promise), support, reviewing borderline voice verifications | The real cost. It is why the hosted tier is capped and the self-hosted tier is not hand-held |
| Home uptime | Hosted runs on big-chungus. The Earshot brief says paying customers should not depend on it | Sell hosted as best effort, no SLA, with a status line. Revisit when subscribers justify a rented always-on GPU (my unchecked estimate: a few hundred dollars a month) |

Capacity is the number to measure before opening hosted. The README says "~1-2 s RTF
on a 3090" but nothing has been measured under mixed load. Assumption for sizing:
one worker produces several minutes of audio per wall-clock minute, so a 3090
handles dozens of typical subscribers (most use a fraction of their quota) before it
hurts the fleet. Treat the cap of 50 as a guess to be replaced by a measurement
(gap G3).

Quotas are in characters because that is how the market quotes them. About 900
characters is one minute of speech (ElevenLabs' own page equates 30,000 credits with
about 30 minutes).

### 3.2 Hosted subscription

| | Solo | Studio | Team |
|---|---|---|---|
| Price per month | $9 | $19 | $59 |
| Price per year (two months free) | $90 | $190 | $590 |
| Characters per month | 150,000 (about 2.8 h of audio) | 600,000 (about 11 h) | 2,500,000 (about 46 h) |
| Max characters per request | 3,000 | 6,000 | 10,000 |
| Catalog voices (house + designed) | All | All | All |
| Voice design ("describe a voice") slots | 5 | 25 | 100 |
| Own-voice clone slots (verified) | 1 | 5 | 20 |
| API keys per account | 2 | 5 | 20 |
| Requests in flight | 1 | 2 | 4 |
| Queue priority | Normal | Normal | Ahead of Solo and Studio |
| Engines | VoxCPM2 | VoxCPM2 | VoxCPM2 |
| ElevenLabs fallback | Never | Never | Never |
| Output rights | Customer owns downloaded audio | same | same |
| At the limit | Hard stop with HTTP 429 and the reset date. No overage billing at launch | same | same |

Why these numbers:
- Against ElevenLabs self-serve (pricing page read 2026-11-23): Starter $6 for 30,000
  credits, Creator $22 for 121,000, Pro $99 for 600,000. Solo gives 5x Starter's
  characters for $3 more. Studio gives about 5x Creator's characters for less money.
  Team gives about 4x Pro's characters for 60% of the price.
- Three tiers is enough. A fourth ("Enterprise", the April doc's $500 to $2,000 on-prem
  deal) is dropped: a single home GPU cannot honor it. Anyone that large buys the
  self-hosted edition and asks for help.
- Clone slots justify Studio and Team. All clones in an account must be the same
  person (section 4), so extra slots mean the same voice in different styles or
  recordings, not other people.
- No free tier at launch. The April doc's "Free, Rick only" is out: Rick is a
  celebrity clone, and a free tier on a shared home GPU invites abuse. Testers get
  Stripe promotion codes (the store already allows them for Earshot). A short
  recorded demo covers "try before paying". A trial can come later once usage is known.
- Top-up packs (Gruvato sells token packs with a ledger) are a phase 2 idea, after
  there is a usage ledger to bill against.

### 3.3 Self-hosted edition: what $49 includes

| Item | Offer |
|---|---|
| Price | $49 one time. Matches "about $50"; the odd price follows Earshot's $4.99 |
| License | One signed license key per buyer, checked offline when the daemon starts. One purchase covers the buyer's own machines and own business. Not for resale, not for sharing the key |
| Updates | 12 months of new versions from purchase (Gruvato's self-hosted rule). After that the last version keeps working forever. Optional renewal, suggested $19 a year, announced only when there is something to renew |
| Engines | VoxCPM2 (Apache-2.0) with the compose files. ElevenLabs fallback is available with the buyer's own key. VibeVoice is not included and not sold (N9) |
| Voices shipped | The four `house-*` voices and a few designed ones. No celebrity or character voices |
| Cloning | `voxxy voice add` from the buyer's own clip, per-request reference audio, and the clip picker (`voice candidates`). The "find a person on YouTube and clone them" flow is not in the bundle (section 5) |
| What you also get | The `voxxy` CLI, MCP endpoint, OpenAI-compatible endpoint, sink relay, HeyMa adapter, docs |
| Hardware | Linux with an NVIDIA GPU, 8 GB VRAM minimum for VoxCPM2 alone (it uses about 5 GB; 12 GB or more is comfortable), Docker with the NVIDIA container toolkit, about 20 GB of disk for the image and weights. No CPU-only or Mac build at launch |
| Delivery | A bundle (compose files, CLI wheel, install script, docs) downloaded after purchase. The engine image is built on the buyer's machine and weights come from Hugging Face, so AutomaticAI hosts no 16 GB image |
| Support | Email to support@automaticai.io, install and licensing help, best effort within 3 business days. No custom work. 30 days of install help is a reasonable promise. Jarad chooses the real window (N12) |
| Refunds | 14 days on request, as a policy line on the page (Jarad decides, N12) |

Why self-hosted goes first:
- Nothing runs on Jarad's GPU for customers, so there is no capacity cap, no
  consent-verification system and no multi-tenant voice store to build first.
- It reuses what Earshot already proved: a Stripe payment link, a manually issued
  Ed25519 key, a delivery promise on the page.
- It is the "optimize" lever Jarad asked about: it carries the best margin (about
  $47 net, no running cost), and it is the honest answer for people who want flat
  cost. The hosted tiers keep a floor under it: five months of Solo cost more than
  $49, which sends heavy users to self-hosted, which is fine.

## 4. Cloning on the hosted service: the strict rule

Rule (to state on the page and in the terms): **On the hosted service you can clone
only your own voice, and we check. You can also describe a voice and have one made.
You cannot clone another person's voice, a character, or a celebrity.**

### 4.1 How "your own voice only" is enforced

| Step | What happens |
|---|---|
| 1. Start | In the dashboard or `voxxy voice consent`, the customer asks to add a voice. The server issues a random single-use challenge sentence with a short code, valid 10 minutes |
| 2. Consent recording | The customer records themselves reading that sentence live, at least 8 seconds. Server checks the transcript (faster-whisper, already in the CLI `clone` extra) contains the sentence and code |
| 3. Reference clip | The customer uploads or picks the clip to clone (10 to 30 seconds). For HeyMa users, `voxxy voice candidates` and Wax's own clean-window finder pick it from their recordings |
| 4. Match | The server computes a speaker embedding of the consent clip and the reference clip and compares them. At or above the threshold, the voice is created and marked verified. Clearly below, it is refused with a plain reason. In between, it goes to a manual review queue |
| 5. Account voiceprint | The first verified clone fixes the account's voiceprint. Every later clone in that account must match it. This is how "own voice only" survives a customer trying to add a second person |
| 6. Record | Stored per voice: consent audio, transcript, challenge, embeddings, timestamps, terms version. Kept for the life of the voice plus a retention period set in the privacy page |
| 7. Report | A "report a voice" email address and a takedown action that suspends the voice and, if warranted, the account. Gruvato's `server/takedown.mjs` and `docs/legal/drafts/copyright-policy.md` are the starting point |

Also required, or the rule leaks:
- Customer keys cannot send per-request `reference_audio_b64` (today's `POST
  /synthesize` accepts it for any caller) and cannot use raw `POST /voices`. Clones
  come only through the consent flow.
- Customer keys are limited to their own voices plus the shared `house` and designed
  catalog. Today `voices` is one flat namespace and `GET /voices` shows everything
  (`app/voices.py`). The fleet's celebrity voices must be invisible to customer keys.
- `voxxy voice clone "<person>"` (web search, then YouTube) exists in the CLI. It is
  the opposite of this rule and never runs against a hosted customer account.

What the check is, honestly: a live, ASR-checked consent phrase plus a voice match.
It stops casual misuse. It does not prove consent against someone who plays a
deepfake into the microphone, and a voice match across different microphones is
imperfect. Wax's own measurements (`wax/voiceprint.py`) show same-person scores
dropping across channels (median 0.52) and phone audio pulling different people
together. So the threshold needs calibrating on real samples before launch, and the
review queue stays on while the subscriber count is small. The terms add the
customer's own warranty that the voice is theirs (wording is Jarad's, N12).

Where Wax fits: the voice registry's embeddings use TitaNet (192 numbers, cosine
scores). That is the fingerprint approach to reuse, either as the same model
(NeMo TitaNet-large, GPU, English-trained) or the TitaNet-small via sherpa-onnx that
Vinyl already runs on CPU. A short spike picks one (G5). Wax stays on the customer's
machine for HeyMa users: it finds clean windows of their own voice and can pre-check
a match before upload. The hosted service never trusts "this came from my Wax"; it
always runs its own consent check.

### 4.2 Describe a voice

No consent is needed, because no person's recording is used. VoxCPM2 "Voice Design"
is already working here: `house-voices/build.py` writes `"(" + description + ")" +
transcript`, with no reference audio, and registers the result. Two rules on top: the
description may not name a real person ("sounds like ...", refused by a simple name
check, logged), and designed voices count against the plan's design slots.

## 5. Self-hosted terms, positioning and the wording risk

### 5.1 What the terms say

Self-hosted is software the buyer runs. Jarad's decision is that they may clone what
they want at their own risk. The terms say so in neutral words:

- You are the operator. You run it on your hardware, with your data.
- You are responsible for having the rights and consent for every voice you create
  and everything you generate, and for following the law where you live.
- AutomaticAI does not receive your text, voices or audio and cannot review them.
- The license covers the software, not any voice. Using someone's voice without their
  permission may be illegal.

This is Gruvato's own shape: its cloud edition is "legal-first", its self-hosted
edition carries more freedom, and "any external module" is explicit install, visible
source label and the user's own responsibility (`PRODUCT_BRIEF.md` "Legal / Module
Boundary").

### 5.2 Positioning that implies without inviting

Recommended page copy for the self-hosted card:

> **Self-hosted.** Your hardware, your data, your responsibility. Run Voxxy on your
> own NVIDIA GPU: nothing leaves your machine, there is no meter, and you are the
> operator of every voice you make. $49 once, with 12 months of updates.

And in the comparison table, one row:

| | Hosted | Self-hosted |
|---|---|---|
| Voice cloning | Your own voice, verified | Voices you have the rights to. Your responsibility |

Never write "clone anyone", "no restrictions", "no consent checks" or "the version
without rules". The contrast between the two rows already says what Jarad wants said.

### 5.3 Wording risks for Jarad (N10, N11)

| Risk | Why it matters | Recommendation |
|---|---|---|
| A comparison row that reads as "hosted has rules, self-hosted does not" | It is the invitation. It turns the page into an advertisement for the thing the hosted rule bans, and it weakens "you are the operator" | Use the row above ("voices you have the rights to") and do not stress the hosted ban in self-hosted copy |
| The CLI ships `voxxy voice clone "Morgan Freeman"` and README examples naming real people | Selling that to a stranger is the opposite of the positioning | Leave the web-search clone flow and its celebrity examples out of the sold bundle, or make it a separate optional plugin the buyer installs knowingly (Gruvato's module boundary). Strip fleet voices and personas |
| Payment processor and card network rules on voice cloning products | A closed Stripe account would take Earshot's checkout with it (same Stripe account) | Read Stripe's current restricted-business and acceptable-use pages before the first link goes live. Not checked in this plan |
| Voiceprints are biometric data in some places | The hosted consent flow stores them | Sell hosted to the US only at launch (Gruvato sells packs to US buyers only); decide in N15 |
| Self-written legal text | Gruvato's owner decision of 2026-11-14 is no paid counsel; its terms are self-drafted with research spikes | Adapt Gruvato's drafts (`docs/legal/drafts/terms-of-service.md`, `copyright-policy.md`) and mark every clause Jarad's to approve |

None of the wording above is legal advice. All terms and policy text are Jarad's
decision.

## 6. Positioning and the store page

### 6.1 Against ElevenLabs and others

Say what is true and checkable. No "category capture", no quality parity claim until
a blind test exists (the April doc's "within noise" has no evidence behind it).

| Alternative | Honest comparison |
|---|---|
| ElevenLabs | Far more voices, languages, polish and tooling. Priced per credit; cloning starts on its Starter plan, professional cloning on Creator. Voxxy's case: a flat price to own it, an OpenAI-compatible API, your own voice from your own recordings, agents (MCP) and "play it where I am" built in, and text that never leaves your machine in the self-hosted edition |
| OpenAI TTS and similar APIs | Cheap and easy, stock voices only, no cloning. Voxxy adds cloning and design |
| Free local engines (Piper, Kokoro, XTTS) | Free, and some run without a GPU. Voxxy is the packaged, supported version of a GPU voice service with a CLI, keys, a voice store and agent hooks, not a competitor on price |
| Running VoxCPM2 yourself | Possible and free (the model is Apache-2.0). $49 buys the working stack and updates, which is an install-time and maintenance saving |

One-line positioning for the HeyMa family: "HeyMa hears you. Voxxy answers in a voice."
(Matches `voxxy-heyma-integration-scope.md` section 2.) Sell it first to people who
already talk to computers: agent builders, podcasters, developers.

### 6.2 The store page (`/voxxy/`)

The store is static (Cloudflare Pages), with one gated page per app. Follow
`automaticai-store/README.md` "Add an app":

1. Copy `site/earshot/` to `site/voxxy/`; `golive.json` with every gate off.
2. Sections: hero with two buttons ("Hosted: from $9 a month", "Self-hosted: $49 once");
   "Pick your edition" table (section 5.2 row); how it works (API, CLI, MCP, sink);
   how cloning works on hosted (the consent steps in plain words); pricing for both;
   system requirements for self-hosted; honest comparison box; FAQ; privacy link.
3. Gates: one `checkout` gate per thing sold. Use `checkoutHosted` and
   `checkoutSelfhosted`, plus `download` (the bundle), `guide`, `support` and `demo`.
   Each starts off; the unlocked state reads "Opens soon", as Earshot's does.
4. Policy: add a `voxxy` entry to `scripts/policy.mjs` in the same change that
   prepares go-live, pointing at a Voxxy owner go-live ticket (none exists yet).
5. `site/voxxy/privacy/` and an `id="voxxy"` section in `site/privacy/index.html`,
   checked against code: text sent, 1 hour audio cache, stored reference clips,
   stored consent clips and voiceprints (hosted only), self-hosted sends nothing.
6. A `/voxxy/thanks/` page per checkout, and a card on `site/index.html`.

HeyMa naming: list it as a **Voxxy** card with a "HeyMa family" tag, and a short
strip linking the other HeyMa products as they become sellable. Jarad's phrasing
("add HeyMa with Voxxy as the 2nd product") is ambiguous: see N1.

## 7. Licensing, keys and checkout

### 7.1 Hosted per-customer API keys

Today `app/auth.py` compares each request against one environment key
(`VOX_API_KEY`) in constant time, and is not enforced in production. Extend it,
do not replace it.

| Piece | Design |
|---|---|
| Root key | `VOX_API_KEY` stays as Jarad's root key. Full access, ElevenLabs fallback, fleet voices. Turn enforcement on first (V5 in the HeyMa scope), caller by caller |
| Customer keys | Format `vxk_<random>`, shown once, stored as a hash. Table: `customers` (id, email, plan, status, Stripe customer id, voiceprint) and `api_keys` (id, customer id, prefix, hash, label, created, revoked, last used) |
| Lookup | The middleware keeps its constant-time habit: find by prefix, compare hashes. Same header shapes as today (`Authorization: Bearer`, `X-API-Key`), same public paths |
| Per-key rules | `engines` forced to `["voxcpm"]`, no reference audio, no raw voice upload, voice scope, quota, concurrency, max characters per request. The request-level `allowed_engines` and `require_reference` the router already has become server-set per key, so a caller cannot widen them |
| Voices | Add `owner_id` to `voices`; name unique per owner; owner null means house or fleet, with `house` tag deciding what customers see |
| Sinks and audio | `/sink/<key>` names are prefixed with the customer id so keys cannot collide. `/audio/<id>.ogg` stays public by unguessable id (1 hour TTL) |
| Usage | Monthly character counter per customer, counted on success. 429 with the reset date. `GET /v1/usage` |
| Admin | `voxxy admin customer create/suspend/rotate-key/set-plan`, backed by an admin route only the root key can call. Used by hand first, by the webhook later |

### 7.2 Self-hosted license keys

Reuse Earshot's design, not its code path as-is. Earshot signs `ES1.<payload>.<sig>`
with Ed25519 (`docs/launch/2026-11-17-offline-license-review.md`), with the public
key embedded in the app, the private key in 1Password, and a ledger with one issuance
per Stripe Checkout Session (`scripts/licensing/issuer.py`).

- Format `VS1.<payload>.<signature>`, product `voxxy-selfhost`, entitlement
  `selfhost-12m` plus an issue date and a `updates_until` date. The daemon verifies
  offline at start and shows the license, never phones home.
- It gates the packaged bundle and its updates, not the model. Soft enforcement, as
  Earshot accepts: a determined user can bypass it, and the page says "please don't
  share your key".
- Issuance at launch is manual, within 24 hours, from a paid Checkout Session,
  checked for refund and dispute (the Earshot issuer already does this).
- Level-up work: Earshot's issuer hard-codes its payment link, key id and signing
  reference. Move those into a per-product catalog entry in the store repo so
  Voxxy is a new entry, not a copy. That is the whole generalization. It is not a
  licensing platform (Earshot's own review argued against building one at first use;
  this is the second use and the smallest honest step).

### 7.3 Checkout and fulfilment

| Thing sold | Stripe | Fulfilment |
|---|---|---|
| Self-hosted | One-time price, payment link, Stripe account of Automatic AI LLC | Manual license key plus bundle link by email within 24 hours, as Earshot does |
| Hosted | Three recurring prices, monthly and annual, one payment link each; customer portal link for cancel and card changes | Manual for the first handful of customers. A small webhook (checkout completed, subscription deleted, payment failed) calling the admin API follows before the beta grows, because a cancelled subscription must stop its key. Gruvato's `server/purchases.mjs` has the signature-checking pattern to port |

All Stripe products, prices, links and the portal are created only after Jarad
authorizes (N14). The store's leak check keeps links out of the page until a gate is
on with an authorization reference. This plan creates nothing.

## 8. What has to be built: gap list against today's code

Ordered. Estimates are planning guesses in working days, not commitments. Owner repo
in the second column.

| # | Gap (today's state) | Repo | Est. |
|---|---|---|---|
| G0 | Enforce `VOX_API_KEY` in production: confirm every first-party caller sends it (Hermes plugin, inbox-watcher, Node-RED, CLI), then switch on. `/healthz` stays public. This is HeyMa scope V5 | voxxy | 1 |
| G1 | Key store and middleware (section 7.1): customers, hashed keys, per-key rules, admin CLI. Today: a single env key | voxxy | 3 |
| G2 | Voice ownership: `owner_id` migration 004, scoped list and get, house and designed catalog visible to customers, fleet voices root-only. Today: one flat table | voxxy | 2 |
| G3 | Usage ledger, quotas, concurrency cap, 429 and `GET /v1/usage`; measure real throughput on the 3090 and set the subscriber cap. Today: none | voxxy | 3 |
| G4 | Self-hosted bundle: build script that strips fleet voices and personas, ships VoxCPM2 compose and the four house voices, excludes the web-search clone flow (or moves it to an optional plugin), license verify at daemon start, `voxxy license import`, install check, docs. Today: a repo-checkout install tied to Jarad's machine | voxxy | 5 |
| G5 | Consent flow: challenge, live consent upload, ASR check, speaker embedding match, account voiceprint, review queue, consent record. Spike first: TitaNet-large (Wax) versus TitaNet-small (sherpa-onnx), and calibrate thresholds on real recordings. Today: `voice_clone` is a CLI workflow with no identity check | voxxy, with Wax as the reference | 6 |
| G6 | Voice design endpoint: lift `house-voices/build.py` logic to `POST /voices/design`, add the real-person-name refusal and design slots. Today: offline script | voxxy | 2 |
| G7 | Terms drafts: hosted terms, acceptable use, self-hosted operator terms, takedown and report path, privacy facts. Adapt Gruvato's drafts. Jarad approves every clause | voxxy (drafts), store (published pages) | 2 |
| G8 | `.launch.yaml` in Voxxy (`mode: launch`, `live:` block when live) so it joins Holocene's Live shelf; a Voxxy go-live ticket on its board | voxxy | 0.5 |
| G9 | Store: `/voxxy/` page, two checkout gates, policy entry, privacy section, thanks pages, index card, guide. All gates off until authorized | automaticai-store | 3 |
| G10 | Store issuer: per-product catalog entry so Earshot and Voxxy share one issuer. Earshot's behavior must not change | automaticai-store (move from earshot) | 1 |
| G11 | Stripe webhook to admin API for hosted subscriptions | voxxy | 2 |
| G12 | HeyMa: the Voxxy adapter and tray row (V1), the "HeyMa family" tag in the store listing, then a local pre-check that uses Wax's registry to pick a clean own-voice window and test the match before upload | HeyMa | 2 for V1, later for the rest |

Launch sequence this implies:

1. **Self-hosted beta**: G0, G4, G8, G9 (self-hosted gate only), G10, G7 (operator
   terms). Manual key issuance. Hosted button reads "Opens soon".
2. **Hosted capped beta**: G1, G2, G3, G5, G6, G7 (hosted terms), G11, then the
   hosted gate on. Subscriber cap from G3's measurement, starting at 50.
3. **HeyMa wiring** (G12) alongside either step.

Both products' page sections ship in step 1; only the hosted gate waits.

## 9. What I verified

| Claim | Result |
|---|---|
| Gruvato has the patterns Jarad means | **Partly, and the part that matters is there.** Confirmed: a cloud edition and a self-hosted edition with different rules (`PRODUCT_BRIEF.md` "Cloud vs. Self-Hosted": cloud "legal-first", prohibits uploading audio you do not own, self-hosted "purchase once, one year of updates", user-owned modules); an accounts and bearer-token model with a token ledger (`server/accounts.mjs`, `ledger.mjs`); a Stripe Checkout and webhook fulfilment flow with pack catalog versions (`server/purchases.mjs`, `pack-catalog.mjs`); a consent record with terms versioning (`server/consent.mjs`); a takedown flow; self-drafted terms. **Not there:** subscription tiers (its "Cloud Pro" is "TBD monthly") and static per-customer API keys. Its identity is email magic links with session tokens, not keys. Its self-hosted plan was a $100 candidate, not an implemented license. So Gruvato supplies the two-edition rules, consent, takedown, ledger and webhook patterns. Per-customer API keys come from extending Voxxy's own `app/auth.py`, and self-hosted license keys from Earshot's Ed25519 issuer |
| Earshot's selling pattern | Read: static page with gated blocks, one Stripe payment link, manual key within 24 hours, `.launch.yaml` live block, offline Ed25519 license design. Earshot's gates are on (EARS-6 authorized 2026-11-20); the store README's "every gate starts off" applies to new apps |
| Voice design works today | Confirmed in `house-voices/README.md`: four synthetic voices made with VoxCPM2 Voice Design, English and Spanish |
| ElevenLabs prices | Read from elevenlabs.io/pricing on 2026-11-23; promotional discounts were showing and prices will drift. Recheck on the day the page goes up |
| Not checked | VibeVoice's exact license text, Stripe's acceptable-use rules, ElevenLabs output terms, real throughput of the GPU under load, TitaNet match thresholds for consent clips, legal status of voiceprints by region |

## 10. Decisions (2026-11-23, on Jarad's behalf)

| # | Decision | Why |
|---|---|---|
| N1 | One **Voxxy** card on store.automaticai.io with a "HeyMa family" tag. A `/heyma/` umbrella page only when a second HeyMa product is for sale | All three reviews agreed: an umbrella with one product adds a click and sells nothing |
| N2 | Hosted beta sells **Solo $9/mo (150k characters)** and **Studio $19/mo (600k)**, monthly only. Studio's clone slots are styles of the same person's voice. **Team ($59) and annual prices wait** for G1, G2, G11 and the end of the beta | One account holds one person's voiceprint, so Team's seats can't be delivered yet; prepaid annual plans on a best-effort home GPU turn an outage into refunds on the Stripe account Earshot shares |
| N3 | Self-hosted **$49 once, 12 months of updates**, the last version runs forever. No renewal price announced until there is a year of changelog to renew against | Best margin (about $47 net, no running cost); a renewal price at zero users prices an offer that does not exist |
| N4 | **Self-hosted first.** Its gate opens when N10 copy, the N11 bundle, the N12 batch 1 terms and the N14 Stripe check are done. Hosted reads "Opens soon" with a waitlist (email only) and opens after G1, G2, G3 and G11 | Self-hosted costs no GPU and no review time; the waitlist is the only demand signal before 8+ days of hosted billing work |
| N5 | Hosted cap **10**, raised to **25** after recalibrating on about 50 real consent pairs and the G11 webhook, to **50** only after G3 measures throughput | The binding cost is review minutes, not the GPU: most genuine first voices land in review |
| N6 | **No free tier.** Promotion codes for testers, a demo video, and a static page of pre-rendered house and designed voices | A free cloning tier on a check a clone can beat is the cheapest abuse path; pre-rendered samples cost no GPU |
| N7 | Hosted runs on the home 3090, **best effort, no SLA**, said on the page, with a status line. Revisit at 25 paying subscribers or about $300/mo | A rented GPU (about $150-300/mo) loses money before then; saying "no SLA" up front prevents "not as described" disputes |
| N8 | **Never** for customer keys. Root key only; a customer whose engine is down gets a 503 with a retry hint | One full Solo quota through ElevenLabs costs about $25 against $9 of revenue |
| N9 | **VoxCPM2 only** in both editions. VibeVoice stays out of anything sold | Research-only license, watermarked output |
| N10 | Self-hosted card: "**Your hardware, your data.** Run Voxxy on your own NVIDIA GPU: nothing leaves your machine and there is no meter. $49 once, with 12 months of updates." Comparison row: "Voices you have the rights to. Your responsibility." "You are the operator" lives in the operator terms and a checkout checkbox. Hosted copy says "we check", never "verified identity" or "deepfake-proof". Demos use house, designed or Jarad's own voice only. Never "clone anyone" or "no restrictions" | Implied, never advertised (Q4); keeps the page clear of Stripe's "facilitates infringement" language |
| N11 | The sold bundle ships **no fleet, celebrity or character voices, no personas, and no web-search/YouTube clone flow**, not even as an optional plugin. Before the self-hosted gate opens, move the persona files and the `voice_clone` flow out of the **public** `delorenj/voxxy` repo into a private fleet-only repo (G4) | A plugin the seller distributes is still the seller's product; the public repo would otherwise undercut the page |
| N12 | Adapt Gruvato's drafts in two batches. **Batch 1 (before self-hosted):** operator terms, 14-day refunds, email support best effort within 3 business days with 30 days of install help, terms acceptance required at checkout (`consent_collection.terms_of_service=required`). **Batch 2 (before hosted):** hosted terms, acceptable use, takedown, the customer's warranty that the voice is their own, a biometric notice and release checkbox before the first recording, a published retention schedule (embeddings deleted within 30 days of deleting the voice or account; consent audio and records purged 12 months after the voice is deleted; never sold or shared). Hosted: refund on the first charge within 14 days, renewals non-refundable, cancellation at period end | Illinois BIPA and Texas CUBI require a written release and a retention schedule for voiceprints, and the code keeps them forever today (gap: purge job, G7) |
| N13 | **Every hosted account's first voice goes to a person, whatever it scores** (built and live, voxxy `d678325`, `VOX_CONSENT_REVIEW_FIRST`). Later voices auto-accept at 0.70 or above against that reviewed voiceprint and are refused below 0.10. Reviews cleared within 24 hours, batched with key issuance. Build "second refusal goes to review" before the cap rises above 10 | A clone reads the code and scores 0.87-0.90; with nothing to compare the first voice against, auto-accepting it was the whole attack |
| N14 | Use the existing Automatic AI LLC Stripe account. Before the first live link: describe the business as AI text-to-speech software, keep cloning-of-others wording out of product names, and **ask Stripe support in writing** whether the product is supported, attaching the reply to the go-live ticket. Create the self-hosted product and link when G4 ships; hosted prices and the portal only at the hosted gate. **Each go-live ticket is signed by Jarad** (store policy) | Stripe's restricted list does not name voice cloning, but its catch-all is discretionary; a written answer costs nothing. A second Stripe account under the same LLC would not isolate a closure anyway |
| N15 | Hosted: **US buyers only** at launch, with the N12 biometric release and retention schedule. Self-hosted: anywhere Earshot sells **except the EU/EEA** until output carries a machine-readable synthetic-audio mark (EU AI Act Art. 50, in force since 2026-08-02). Payment links collect billing country; out-of-scope orders are refunded before a key is issued. Tax as Earshot (not collected until traction, HOLOC-47) | US-only does not escape BIPA, so the release is needed anyway; VoxCPM2 output carries no mark |
| N16 | Yes, as a policy now: the Wax registry may only offer clips of the **account holder's own identity**, never other speakers in it, and never replaces the hosted consent check. Build the G12 pre-check only once hosted has paying customers | The registry holds family members and call participants; a picker across all of it would clone anyone |

New gaps these decisions add: a consent-data purge job and the biometric release step (G7/G5),
the waitlist on the store page (G9), moving personas and `voice_clone` to a private repo (G4),
"second refusal goes to review" (G5), and an EU/EEA exclusion at checkout (G9).

Done 2026-11-23: the waitlist is the store's catalog sign-up on a new `/voxxy/` preview page
(store `43478f6`, form-intake `store-notify`, `product=voxxy`; read it with
`form_intake.py list --form store-notify --field product=voxxy --emails`), with a Voxxy card on
the store home and the 33GOD front shelf; "second refusal goes to review" is live (voxxy
`0b4b266`). G8 (`.launch.yaml`) is owner-edited only, so it is proposed, not written.

### The questions as originally asked

| # | Decision | Recommendation | Default |
|---|---|---|---|
| N1 | **Listing name.** "Add HeyMa with Voxxy as the 2nd product" is ambiguous: a Voxxy card tagged HeyMa family, a HeyMa umbrella listing with Voxxy inside, or both | A **Voxxy** card with a "HeyMa family" tag now; a `/heyma/` umbrella page only when a second HeyMa product is sellable. Matches Q1 in the integration scope | Voxxy card, tagged |
| N2 | Hosted tiers and prices | Solo $9, Studio $19, Team $59 per month; annual = 10 months; quotas in 3.2 | As recommended |
| N3 | Self-hosted price and updates window | $49 once, 12 months of updates, then optional $19 a year renewal | $49, 12 months, no renewal announced |
| N4 | Launch order | Self-hosted first, hosted as a capped beta; page shows both from day one with hosted reading "Opens soon" | Self-hosted first |
| N5 | Hosted subscriber cap | 50 until throughput is measured | 50 |
| N6 | Free tier | None; promotion codes for testers; demo video | None |
| N7 | Where hosted runs | Jarad's 3090, best effort, no SLA; revisit at about 25 paying subscribers | Home GPU, no SLA |
| N8 | ElevenLabs fallback for customers | Never | Never |
| N9 | VibeVoice in the paid editions | Excluded until its license is read for commercial use; VoxCPM2 only | Excluded |
| N10 | Self-hosted positioning copy (5.2) | Approve the "your hardware, your data, your responsibility" copy and the "voices you have the rights to" row; never "clone anyone" | The copy in 5.2 |
| N11 | Contents of the sold bundle | No fleet or celebrity voices; no web-search "clone a named person" flow (or optional plugin only) | Excluded |
| N12 | Legal text and policies: hosted terms, acceptable use, operator terms, takedown, refund window (14 days), support window (3 business days), consent-record retention, warranty that a cloned voice is the customer's own | Adapt Gruvato's drafts; you approve every clause | Drafts only, nothing published |
| N13 | Clone verification strictness | Auto-approve above threshold, you review the borderline queue while subscribers are few | Manual review of borderline |
| N14 | Stripe objects (products, prices, payment links, customer portal) and the go-live authorization ticket, which the store policy needs before any gate can turn on | Create after you authorize; one ticket reference per product | Nothing created |
| N15 | Where to sell and tax | US-only for hosted at launch (voiceprints, Gruvato precedent); tax as for Earshot (not collected until traction, HOLOC-47) | US-only hosted; self-hosted follows Earshot |
| N16 | Wax-registry tie-in: the registry may help pick clean clips and pre-check a match on the customer's machine, never replace the hosted consent check | Yes, as in 4.1 | As recommended |

## 11. Reconciling the April document

`docs/monetization-strategy.md` (2026-04-26) stays as history. What this plan changes:

| April | Now |
|---|---|
| Free, Hobbyist $5, Pro $15, Enterprise $500+ | Solo $9, Studio $19, Team $59; no free tier; Enterprise dropped |
| "Rick only" free voice | Celebrity voices never sold |
| Cloning on Pro for anyone | Hosted cloning is own-voice only, verified; self-hosted is the operator's responsibility |
| On-prem and HIPAA moats, "category capture" | Not claimed. No compliance promise is made here |
| Voice marketplace 60/40, reseller credits | Deferred; needs consented voice owners and a ledger first |
| ElevenLabs only as a cost to beat | ElevenLabs fallback off for customers |
| Stripe "later" | Same Stripe account and payment-link pattern as Earshot, gated |
