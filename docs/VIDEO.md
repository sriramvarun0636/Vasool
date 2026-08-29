# Pitch video — 5:00, shot by shot

The form asks for a **5-minute pitch video**. This script runs **831 spoken
words ≈ 5:45 at 145 wpm**, and the section timings below are measured rather
than aspirational. An earlier draft ran 9:15 — not merely long: a judge watching
fifty of these stops at minute six, and missing the one stated spec is its own
signal.

**If you want a hard 5:00**, cut in this order and stop when you get there:
drop §7's *trace every number* narration and keep only the receipt verify
(&minus;16s); drop the second paragraph of §4, keeping the verbatim line and the
type-graph point (&minus;20s); drop the "I found it writing an attack" sentence
in §5 (&minus;12s). That lands at 4:57 with every rubric beat intact. **Do not
cut §5 further, and never cut §1's loss.**

Time your own rehearsal before trusting any of this — 145 wpm is an estimate,
and a rehearsed delivery usually runs faster than a cold read.

Two rules the whole thing is built on. **The money goes first and the loss goes
second** — the track's bar asks for measured money recovered, so meet it in
sentence one, then spend the loss as proof that the restraint is real rather
than claimed. And **every beat is aimed at one of the four things they say they
grade**, because a video that entertains without answering the rubric is a
wasted five minutes.

| Their criterion | Where it lands |
|---|---|
| **Problem taste** | §2 the safety contrast · §3 the risk-decline rule |
| **Build quality** | §3 live demo and receipt · §6 falsification · §7 provenance |
| **AI judgment** | §4 the air gap, and where the model *lost* |
| **Failure recovery** | §5 the bug · §9 eighteen of twenty-two, four still open |

Track 03's own bar names four things — measured money, compliant escalation,
stopping rules, an audit trail. All four are on screen: money in §1, escalation
and stopping rules in §3, the audit trail in §7.

Two lines are marked **VERBATIM**. Say them exactly.

---

## Shot plan

Three OBS scenes, two hotkeys. Face on the beats where you are being honest
about your own weaknesses; screen where the dashboard is the evidence.

| Scene | What it is | Used in |
|---|---|---|
| **FACE** | Webcam full frame, no screen | §1 open, §9 close |
| **SCREEN** | Display capture, small webcam corner inset | §2, §3, §5, §6, §7 |
| **BIG** | Screen with the inset enlarged | §4, §8 — the verbatim lines |

---

## §1 · 0:00–0:30 · FACE · The money, then the catch

**On screen:** you. No slides, no dashboard. Look at the lens, not at your own
picture.

> "I built revenue recovery for Razorpay. Across a thousand seeded universes of
> five hundred customers it recovered **₹116 crore** of failed payments with
> **zero compliance violations**.
>
> Now the part I'd rather you heard from me. A dumber agent recovers more. Retry
> everything, then send a link: **65.4%** to my **49.1%**. I lose by sixteen
> points, and I registered that as a falsification criterion before I ran
> anything."

**Beat.** One second of silence. You have met the bar and volunteered your worst
number inside thirty seconds.

---

## §2 · 0:30–1:05 · SCREEN · What the gap bought

**On screen:** dashboard hero, then Exhibit A's three cards. Cursor down the
fine print on the incumbent's card.

> "Here's what the incumbent does to earn those points. Twenty thousand nine
> hundred and eighty-eight automated actions on **risk-declined** payments.
> Sixty-six thousand retries on a class my taxonomy prices at zero attempts.
>
> The safety predicate holds for it on **zero** of a thousand seeds. For me, a
> thousand out of a thousand — on the development set and on a sealed holdout.
>
> It isn't a better agent scoring higher. It's an agent that can't be deployed,
> scoring higher *because of* what makes it undeployable."

---

## §3 · 1:05–1:50 · SCREEN · The one rule worth forty seconds

**On screen:** terminal.

```
make demo SCENARIO=payment_risk_check_failed
```

> ⚠️ **Type it exactly like that.** `make demo --scenario …` **fails on camera** —
> `make` reads `--scenario` as one of its own options and prints
> `unrecognized option`. Variables are `NAME=value`, never flags.

It finishes in **under a second** and prints 82 lines, so most of it scrolls.
Run it, then scroll back to `[4] classified` and walk down to the last line:
`SUMMARY: HUMAN_QUEUE for pay_… -- ESCALATED by Card network norms`.

> "A risk-declined payment gets nothing. No retry, no link, no SMS — straight to
> a human.
>
> That's not a technical limit. Retrying it would work sometimes. It's that if a
> fraud engine declined this, the person on the other end may be a victim — and an
> automated *your payment failed, click here* to a compromised customer is exactly
> the phishing pattern.
>
> Hard stop, human queue, zero outbound. And the receipt records the refusal,
> hash-chained, with the clause — because an agent that correctly does nothing and
> an agent that's broken look identical unless the ledger says which."

---

## §4 · 1:50–2:36 · BIG · The air gap · **VERBATIM**

**On screen:** the architecture diagram, then `vasool/diagnosis/llm.py`.

> **"You did not use an LLM to decide whether money moves. You used a state
> machine, because money movement must be replayable, and a stochastic planner
> is not."** ← *VERBATIM*

> "The model has no tools. It emits an `LLMVerdict`; the policy plane consumes a
> `Proposal`. Different types, and **no function in the repository converts one
> to the other** — so for the model to move money, someone would have to write
> code that doesn't exist.
>
> And I measured what that's worth. On the most common failure on the platform it
> gave the same wrong answer fifteen times out of fifteen — perfectly consistent,
> perfectly wrong. That's the argument for where it sits."

---

## §5 · 2:36–3:42 · SCREEN · The bug · *the one they read first*

**On screen:** `POSTMORTEM.md`, then the before/after table.

*The form's twelfth question is "what broke, and how you got out", and they say
it is the one they read first. This is that answer, out loud.*

> "Every test passed. Thirteen hundred and fifty-three of them. Safety clean on
> a thousand seeds. And a third of my population was doing nothing at all.
>
> The pre-debit notice guard holds a mandate debit until a notice is sent, and
> returns an obligation to send one. Obligations were only honoured on the
> *execute* path — and a deferred proposal never executes. **The one thing that
> could satisfy the guard was an execution the guard was blocking.**
>
> I found it writing an attack that turned out to be inert: it couldn't fail,
> because the thing it attacked never happened. Zero of seven hundred and seven
> retries landed on the two hundred and seventy-five mandate episodes.
>
> Fixing it moved recovery from **0.344 to 0.491** — so three quarters of what I
> had been calling *the price of the guards* was this bug.
>
> Every test I'd written asked whether the agent did something wrong. Not one
> asked whether it did anything at all."

---

## §6 · 3:42–4:18 · SCREEN · What could have killed it

**On screen:** Exhibit D, the F1–F7 board. Then Exhibit C's sweep grid.

> "Seven criteria, thresholds fixed before any run. None fired — but read F1. It
> didn't fire because F1 fires when the interval *includes* zero, and mine
> excludes it. **On the wrong side.** The artifact says so in its own detail
> field, because `fired: false` there is worse news than firing.
>
> Eight of my nine outcome parameters are guesses, labelled as such in the source
> where an untagged parameter fails a test. So every one gets swept ±50% —
> eighty-three configurations, every comparison re-tested in all of them."

---

## §7 · 4:18–4:49 · SCREEN · The audit trail, live

**On screen:** click **trace every number**, let the paths appear at once. Then
Exhibit H — paste a receipt id, hit verify, show the SHA-256 match.

> "Every figure on this page shows the manifest key it came from. A value the
> manifest doesn't carry renders as a dash, never as a plausible number — and a
> test fails the build if anyone reintroduces a fallback, because I found a
> hardcoded constant in here rendering as a measurement.
>
> And this recomputes the receipt hash in your browser, from the exact bytes it
> was signed over. You don't have to trust the page."

---

## §8 · 4:49–5:20 · BIG · Honest limitations · **VERBATIM**

**On screen:** `docs/EVALUATION.md` §11.

> **"These outcome probabilities are calibrated to published benchmarks, not
> observed on live traffic. No student has live merchant data. So the number I'm
> reporting is not '₹X was recovered.' It is: under a stated, sensitivity-tested
> outcome model, this policy beats the control by X, and the direction of that
> result is robust to ±50% error in every parameter. The methodology is the
> deliverable. Plug in real traffic and the same harness gives you a real
> number."** ← *VERBATIM*

---

## §9 · 5:20–5:46 · FACE · Close

**On screen:** you again. Optionally `make redteam` finishing behind you — it
runs in under a second and prints 34 lines, widest line **123 characters**, so
size the terminal for that or the verdict column wraps.

> "Twenty-two attacks. Nineteen survive. Three are open and named. I could have
> written twenty-two attacks it passes; these are the ones it doesn't.
>
> One came off that list two days ago. My contact window was enforced in the
> merchant's timezone, so an attack put a message at half ten at night where the
> customer actually lives. I fixed it — and my own harness turned red twice:
> once because a *fixed* attack breaks the build exactly like a broken one, and
> again because the fix made the attack pass with zero receipts, which is a
> vacuous pass, and a test I'd written for that caught it.
>
> The claim isn't that this agent is correct. It's that **the apparatus is built
> so that being wrong is discoverable** — and the evidence is how long that list
> is, and how much of it the apparatus found instead of me."

---

## Production setup

**Physical.** One light at 45° in front of you, never behind — a bright window
behind your head makes a silhouette. Camera at eye level; put the laptop on
books. Look at the **lens**, not at your own picture. Plain wall, or enough
distance that the background falls out of focus.

**Audio is the highest-leverage variable, above video quality.** Earbuds with a
mic beat any laptop mic. Soft room. Record ten seconds, listen back on
headphones, and fix it *before* committing to a take.

**Screen.** Capture 1920×1080, then **zoom the browser to 125–150%** — the
dashboard's body text is 13–14px and is unreadable once a 1920 capture is
downscaled into a player. Terminal at 18–20pt, sized to **123 columns**.

**Record in segments, one file per section.** A single unbroken take means one
stumble at 4:30 costs everything. Nobody can tell in the cut.

**Pre-flight:** Do Not Disturb on · every other tab closed · scrollback cleared ·
both live commands dry-run once · dashboard already open at the hero · phone
face-down in another room.

**Export** 1080p, upload unlisted, then **watch it once on your phone with the
sound low** — closer to how it will actually be judged than your monitor is.

---

## Rehearsal notes

- **Time every run.** The budget is 730 words. If a take lands over 5:15, cut
  from §6 and §7 first — they are the most compressible. **Never cut §5.**
- **The loss is not an apology.** Say it flat, then move to what it bought. If a
  take has you softening it, do the take again.
- **Over-rehearse the two VERBATIM blocks** until they are automatic. Everything
  else should sound like you are thinking, because you will be.
- **Run the terminal commands live.** Both finish in under a second. Pre-recorded
  terminal reads as fake, and this submission is about that difference.
- **Numbers to say precisely:** ₹116 crore · 65.4 to 49.1 · sixteen points ·
  1,000 of 1,000 against 0 of 1,000 · 20,988 · 0.344 to 0.491 · nineteen of
  twenty-two. Everything else can be rounded out loud.
- **Eight passes.** The target is not memorisation — it is that you stop reading.
