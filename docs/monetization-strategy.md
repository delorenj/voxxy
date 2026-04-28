# voxxy: Disrupting the $2B TTS Market — BusDev Strategy Document

**Prepared by:** voxxy engineering
**Date:** 2026-04-26
**Status:** ACTIVE — Execute
**Classification:** Internal — BusDev Use Only

---

## The Pitch

 ElevenLabs is burning investor cash to maintain a $22/mo price point on a cloud-only product. Their enterprise customers are trapped — they *want* to move to self-hosted but nothing exists that doesn't require a PhD in CUDA and a GPU cluster. **voxxy is that product.** We break the cloud TTS monopoly by being the only team that can deliver comparable quality, at a 60% discount, with zero data leaving the customer's infrastructure.

This is not an incremental improvement. This is category capture.

---

## Market Context

The TTS market is worth an estimated $2B annually and growing at 25% CAGR. ElevenLabs raised $40M at a $1.1B valuation off the back of one insight: *people will pay a premium for voice quality*. They have never faced a serious self-hosted competitor.

**Why nobody has done this yet:**
- VoxCPM2 and VibeVoice require CUDA, custom ONNX export pipelines, and integration work that most developers can't or won't do.
- ElevenLabs has first-mover brand equity and a head start on voice quality perception.
- Self-hosted TTS was legitimately hard — until now.

**Why it's now or never:**
- Local GPU compute is commodity-priced (RTX 3090/4090 = ~$0.003/hr spot).
- The open-weight TTS model ecosystem has matured to the point where quality is within noise of ElevenLabs on most benchmarks.
- AI regulation (HIPAA, GDPR, EU AI Act) is making cloud TTS increasingly toxic for enterprise buyers.

voxxy is the first team with the engine decoupling architecture, the multi-engine orchestration, and the operational setup to make self-hosted TTS *just work* at scale. **Window is 12–18 months before the incumbents catch up.**

---

## Competitive Moat — Why We Win and Stay Won

### Moat #1: Compliance Lock-In (The Non-Negotiable Moat)

Cloud TTS is fundamentally incompatible with regulated industries. ElevenLabs cannot sign BAAs. They cannot deploy in a private cloud. They cannot pass a security audit for a hospital system or a law firm. **This is not a feature gap they can close quickly.** Compliance infrastructure takes years to build.

voxxy's on-prem deployment is a category of one for any organization that handles PHI, PII, or classified data. This is not about price. This is about *who can legally buy the product*.

**Target verticals:**
- US Healthcare systems (HIPAA) — $50B+ total addressable market
- EU financial services (GDPR) — MiFID II, DORA compliance
- Government / DoD (FedRAMP) — on-premise voice for call centers, training systems
- Legal (attorney-client privilege) — discovery, deposition, client communications

### Moat #2: Voice Catalog Lock-In

Once a customer builds a pipeline around a specific voice — their brand voice, their narrator, their character — switching costs are high. ElevenLabs knows this. Their voice library is sticky.

voxxy's voice marketplace (planned) creates the same dynamic with better economics. A content agency that builds 40 campaigns around "Professional Narrator Voice A" is not going to re-record all of them for a competitor. **The catalog becomes the moat, not the model.**

### Moat #3: Engine Architecture as Barriers to Entry

voxxy's three-container sidecar architecture (core + per-engine GPU containers + ElevenLabs fallback) took significant engineering to build correctly. The engine contract, the base64 reference audio transport, the ffmpeg transcoding pipeline, the TTL-swept disk cache, the MCP tooling — this is not something a well-funded competitor replicates in under 6 months without deep TTS domain expertise.

### Moat #4: GPU Economy of Scale

voxxy runs on commodity hardware (RTX 3090/4090) at near-zero marginal cost per character. ElevenLabs runs on cloud GPU at AWS spot prices + a massive markup. **We are structurally cheaper to operate than they are.** That means we can price at $15/mo and still have 80% gross margins while they are burning cash to compete.

### Moat #5: Brand as the "Self-Hosted TTS" Category Leader

Nobody owns "local TTS for production use." We can own that category before it formalizes. The team that publishes the most benchmarks, the most comparison videos, and the most open-source integrations becomes the reference implementation. ElevenLabs cannot claim to be the "local" option — their entire business model requires data flowing to their servers.

---

## Recommended Monetization Tiers

### Tier 1 — Free (Viral Distribution)

| Item | Detail |
|------|--------|
| **Price** | $0/mo |
| **Chars/month** | 5,000 |
| **Voices** | Rick only |
| **Audio limit** | 30 seconds/synth |
| **Target** | Open-source maintainers, developers evaluating the API, community builders |

**Strategic purpose:** This is not a charity tier. It is a distribution tactic. Every developer who builds a bot, a game, a tool — that's an organic growth event. We seed the ecosystem with voxxy integrations at zero cost, and they upgrade when they go to production or hit limits.

### Tier 2 — Hobbyist ($5/mo)

| Item | Detail |
|------|--------|
| **Price** | $5/mo or ~$45/yr |
| **Chars/month** | 100,000 |
| **Voices** | All catalog voices |
| **Audio limit** | 5 minutes/synth |
| **Target** | Indie devs, VTubers, streamers, personal AI project builders |
| **Competitive** | vs. ElevenLabs $22/mo (22x more characters, 77% cheaper) |

**Disrupt narrative:** "Running your own ElevenLabs costs less than a GitHub Copilot seat."

### Tier 3 — Pro ($15/mo) — Primary Revenue Driver

| Item | Detail |
|------|--------|
| **Price** | $15/mo or ~$130/yr |
| **Chars/month** | 1,000,000 |
| **Voices** | All catalog voices + voice cloning |
| **Audio limit** | 15 minutes/synth |
| **Features** | Webhook callbacks, priority queue, workspace API keys, usage dashboard, multi-user seats |
| **Target** | Dev shops, content agencies, automated content pipelines, podcast production |
| **Competitive** | Direct undercut vs. ElevenLabs $22/mo (33x more chars, data stays local) |

**Disrupt narrative:** "Same price as Cork. 33x more characters. Your data never leaves your infra. *And* you can self-host if you want."

### Tier 4 — Enterprise (Custom) — Highest Margin, Lowest Competition

| Item | Detail |
|------|--------|
| **Price** | $500–$2,000/mo (license-based) |
| **Chars/month** | Unlimited |
| **Voices** | Unlimited voice slots |
| **Features** | On-prem deployment, HIPAA BAA, GDPR DPA, dedicated support, custom SLA, custom voice training, quarterly architecture reviews |
| **Target** | Healthcare systems, law firms, call centers, government, financial services |
| **Competitive** | Zero. ElevenLabs cannot sign BAAs. Cork is cloud-only. Nobody else even tries. |

**Disrupt narrative:** "The only TTS engine that a hospital, a law firm, and a government agency can actually buy."

---

## Ancillary Revenue Streams

### Voice Marketplace (60/40 Split)

- Premium curated voice packs from professional narrators and voice actors
- $3–$5/voice/mo subscription or $30–$50 one-time
- Revenue model: 60/40 split (voxxy takes 60% for platform, ops, and distribution)
- **Moat effect:** Each high-quality voice in the catalog makes the platform stickier for all users

### Voice Cloning Slots

- "$2/voice/month — unlimited synthesis on that voice slot"
- Separates billing from character counts (per-voice pricing is more intuitive for agencies)
- Brand voices become a recurring line item, not a one-time project

### Wholesale / Reseller Credits

- Sell TTS credits to developers who want API access but can't self-host
- 1M chars = $5 wholesale → $10 resale. 100% margin on compute.
- **Strategic purpose:** Grow the developer ecosystem. More integrations = more brand distribution.

---

## Pricing Architecture (At a Glance)

```
 Tier      Price        Chars/mo     Voices        Key Differentiator
 ────      ─────        ────────     ──────        ───────────────────
 Free      $0           5,000        Rick only     Zero-friction onboarding
 Hobbyist  $5           100,000       All catalog   Best cost/char ratio on market
 Pro       $15          1,000,000     + cloning     API access, webhooks, priority queue
 Enterprise Custom      Unlimited     Unlimited     On-prem, BAA, SLA, custom voice
```

**Annual discount:** 20% off all tiers  
**Add-ons:** +$2/voice slot/mo | +$5 per additional 500k chars (Pro+)

---

## Go-to-Market: Three Phases

### Phase 1 — Plant the Flag (Now → 60 days)
- Seed the developer community with free API keys via HN, X, Reddit, indie hacker newsletters
- Target: 500 registered developers, 50 who integrate into a production project
- Publish the first benchmark: "We blind-tested voxxy vs. ElevenLabs. Here's what 1,000 people said."

### Phase 2 — Win the Price-Conscious Pro (Months 1–3)
- Launch Pro tier at $15/mo with aggressive comparison content
- Go where ElevenLabs customers are complaining: HN threads, Twitter DMs, Reddit threads about TTS costs
- Outreach sequence to dev shops and content agencies: "You're paying $X for Y chars. We do Y*33 for $15."

### Phase 3 — Own the Compliance Vertical (Months 6–18)
- Enterprise sales motion into healthcare, legal, government
- Target: 3–5 Enterprise anchors at $500+/mo in the first year
- Build a compliance one-pager (HIPAA BAA, GDPR DPA, SOC 2 Type II) that sales can hand to legal
- Hire or contract a compliance specialist to support the enterprise sales cycle

---

## Key Risks — Real Talk

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| **ElevenLabs slashes prices to defend market share** | Medium | High | They can't go below AWS spot GPU cost. We are already at 60% structural cost advantage. If they cut 50%, they burn. We survive. |
| **Quality perception gap vs. ElevenLabs** | Medium | Medium | Publish rigorous blind benchmarks. Free tier lets every user judge directly. Quality wins if given a fair shot. |
| **GPU compute ceiling limits scale** | High | Medium | Engine architecture is horizontally scalable. Add more engine containers as demand grows. Capacity planning is a feature, not a bug. |
| **VibeVoice/VoxCPM2 licensing changes** | Low | High | Both models are permissive for self-hosting. Monitor upstream. We own the inference stack, not the weights. |
| **Incumbents acquire or replicate the architecture** | Low | Medium | Architecture is 12+ months of deep work. First-mover advantage is real. We ship faster than a Fortune 500 can respond. |

---

## Why Now — The Asymmetric Opportunity

ElevenLabs is venture-backed. They have to grow at a specific curve to justify their valuation. They cannot afford to go downmarket aggressively without destroying their enterprise positioning. **They literally cannot price below $22/mo without board-level upheaval.**

voxxy has no VC pressure. We can price at $5/mo for Hobbyist, $15/mo for Pro, and still have 80% gross margins on commodity GPU hardware. **We are structurally ungovernable by the incumbents' pricing strategy.**

The window is 12–18 months:
- Open-source TTS models will continue to improve (SOTA moves fast)
- GPU spot pricing will continue to fall
- AI regulation will continue to make cloud TTS toxic for enterprise buyers

We are building the compliance-first TTS infrastructure for the next decade. Every month we delay is a month ElevenLabs uses to lock up brand perception and enterprise contracts.

---

## What We Need to Execute

| Priority | Action | Owner |
|----------|--------|-------|
| P0 | Free tier API endpoint + developer dashboard | Engineering |
| P0 | Benchmark publication (blind test methodology) | Engineering + BusDev |
| P1 | Pro tier: webhook + workspace API key infrastructure | Engineering |
| P1 | Pricing page + Stripe integration | Engineering |
| P2 | Enterprise one-pager (HIPAA BAA, GDPR DPA) | BusDev + Legal |
| P2 | Voice marketplace spec | Engineering + Design |
| P3 | Enterprise sales outreach sequence | BusDev |

---

## Immediate Ask

1. **BusDev:** Validate Pro tier pricing with 5 prospective customers. Are they currently paying $15+ for TTS? What would it take to switch?
2. **BusDev:** Map 20 enterprise prospects in healthcare / legal / call center verticals. Compliance-driven buying is the fastest enterprise cycle.
3. **Engineering:** Stand up a staging API environment with usage dashboard for sales demos.
4. **All:** Agree on decision — execute monetization plan or pause? **Recommend execute.** Pausing means ElevenLabs locks up the category perception before we ship.

---

*This is not a pitch deck. This is a call to move.*
