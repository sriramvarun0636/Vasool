<div align="center">

<h1>⚖️ Vasool</h1>

<p>
  <strong>A revenue-recovery agent for Razorpay that recovers less money than the baseline — on purpose, and provably.</strong><br/>
  <em>An untrusted LLM proposes. A deterministic state machine disposes. Every rupee that moves leaves a hash-chained receipt.</em>
</p>

<p>
  <a href="#the-result"><img src="https://img.shields.io/badge/Recovery-49.1%25_(baseline_65.4%25)-c1443c?style=for-the-badge" alt="Recovery"></a>
  <a href="#what-the-gap-bought"><img src="https://img.shields.io/badge/Safety_predicate-1%2C000_%2F_1%2C000_seeds-0ca30c?style=for-the-badge" alt="Safety"></a>
  <a href="#f1f7--the-criteria-that-could-have-killed-this"><img src="https://img.shields.io/badge/Falsification-7_criteria,_registered_first-2D68E6?style=for-the-badge" alt="Falsification"></a>
  <a href="https://razorpay.com/buildathon/"><img src="https://img.shields.io/badge/Track_03-AI_Revenue_Recovery-005571?style=for-the-badge" alt="Track 03"></a>
</p>

<br/>

> 📊 **[Live dashboard →](https://sriramvarun0636.github.io/Vasool)** · every figure below is rendered from `out/development/evaluation.json`, which you can regenerate yourself.

<a href="#the-result">
  <img src="docs/assets/dashboard.png" width="100%" alt="Vasool dashboard" onerror="this.style.display='none'">
</a>

</div>

---

## The result

**Vasool loses.** Over 1,000 seeded universes, the realistic incumbent recovers 65.4% of failed payments and Vasool recovers 49.1% — a paired difference of **−16.35 percentage points**, with a 95% bootstrap interval of [−16.54, −16.17] that does not touch zero.

That is not a caveat buried in an appendix. It is the headline, and it was registered as falsification criterion **F1** in [`docs/EVALUATION.md`](docs/EVALUATION.md) before the first run — along with the rule that a criterion which fires goes in the write-up and gets said out loud.

Here is what the incumbent does to earn those extra 16 points:

| | `retry_plus_contact` (incumbent) | ⚖️ **Vasool** |
| :--- | ---: | ---: |
| Recovery rate | **65.42%** | 49.07% |
| Seeds where the §2a safety predicate held | **0 / 1,000** | **1,000 / 1,000** |
| Automated actions on risk-declined payments | 20,988 | **0** |
| Retries burned on a dead instrument | 292,256 | 64,321 |
| Retries on a class the taxonomy prices at zero attempts | 66,040 | **0** |

The incumbent is not a worse agent that happens to score higher. It is an agent that **cannot legally be deployed**, scoring higher *because* of the actions that make it undeployable. Every one of those columns is a ledger scan, reproducible from a seed — not a self-report.

**The honest one-line summary:** the taxonomy did not buy recovery. It bought a deployable system, and the 16 points are what that cost in this simulator.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/forest-dark.svg">
  <img src="docs/assets/forest-light.svg" width="100%" alt="Paired difference in recovery rate against Vasool across eight comparison arms, with 95% bootstrap intervals. Vasool trails retry_plus_contact by 16.35 percentage points and vasool_ungated by 4.74, and leads the remaining six.">
</picture>

Every arm runs the **same seeded universe** — same customers, same arrivals, same outcome draws — so the comparison is the per-seed difference, bootstrapped over 1,000 seeds. At this sample size every interval is narrower than its own marker (the widest spans 0.37pp), so the dots *are* the intervals. Regenerate the plot with `python3 tools/make_forest_svg.py`; it reads the same manifest the dashboard does, so the two cannot disagree.

### What the gap bought

- **1,000 / 1,000 seeds** satisfy the [§2a safety predicate](docs/EVALUATION.md) — eight ledger-scanned claims covering contact windows, DLT templates, risk blocks, consent withdrawal, dead-instrument retries, contact caps, hash-chain integrity and receipt uniqueness.
- **pass^k = 1.0** at every registered k ∈ {1, 5, 10, 25, 50, 100}. A system safe in 99 of 100 worlds is not safe; `pass^k` is what makes an intermittent violation visible where a mean would bury it.
- **4.7 percentage points** is the measured price of the guard chain — `vasool_ungated` (identical taxonomy, no guards) recovers 53.8%. F5 was registered at a 20-point threshold. It did not fire.
- **Byte-identical ledgers** on re-run. Same seed → same SHA-256 chain, asserted in CI.

---

## Verify it yourself

Nothing here asks for trust. The whole artifact regenerates from source:

```bash
git clone https://github.com/sriramvarun0636/Vasool && cd Vasool
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then set VASOOL_ID_PEPPER to any string

pytest                        # 1,381 tests
make demo                     # one episode, narrated, no network
make redteam                  # 22 adversarial attacks -> out/adversary/redteam.json
make eval                     # 9 arms x 1,000 seeds  (~20 min)
make report                   # builds out/report.html from the manifest
make replay                   # asserts the ledger hash is deterministic
```

`make sweeps` runs the full §7 sensitivity grid — 83 configurations × 9 arms × 200 seeds. It takes about nine hours and resumes if interrupted.

### Every claim, and where it comes from

No figure in this README is typed by hand. Each one is a key in `out/development/evaluation.json`, the manifest `make sweeps` writes:

| Claim in this README | Manifest key | Value |
| :--- | :--- | ---: |
| Vasool recovers 49.07% | `per_arm.vasool.recovery_rate_mean` | `0.4906981214797104` |
| Incumbent recovers 65.42% | `per_arm.retry_plus_contact.recovery_rate_mean` | `0.6542272536430769` |
| Ungated recovers 53.81% | `per_arm.vasool_ungated.recovery_rate_mean` | `0.5381228185488008` |
| −16.35pp, interval excludes zero | `paired_vs_vasool.retry_plus_contact.recovery_rate` | `point: -0.163529…` |
| Safety predicate on 1,000/1,000 | `per_arm.vasool.safety_holds_on` | `1000` |
| pass^100 = 1.0 | `pass_k.100` | `1.0` |
| 20,988 actions on risk-declined | `per_arm.retry_plus_contact.risk_block_actions_world` | `20988` |
| 66,040 retries on a zero-budget class | `per_arm.retry_plus_contact.customer_action_retries_world` | `66040` |
| F5 gap 4.74pp of a 20pp threshold | `falsification.F5_compliance_unaffordable.gap_pp` | `4.742469…` |
| Ledgers byte-identical on re-run | `determinism.identical` | `true` |
| 18 of 22 attacks survive | `out/adversary/redteam.json` → `survived` | `18` |

The dashboard makes this checkable without leaving the page: **click _trace every number_ and each of its 50 figures displays the exact manifest key it was read from.** A value the manifest does not carry renders as a dash and raises a warning banner — never as a plausible number.

That rule is enforced, not merely stated. [`tests/test_report.py`](tests/test_report.py) fails the build if a `|| <number>` fallback is reintroduced on any expression reading from the manifest. It exists because one was found in this repository, rendering a hardcoded constant as a measurement; the incident is recorded in [`docs/EVALUATION.md` §10](docs/EVALUATION.md).

### Check the cryptography without trusting us

The manifest ships twelve real receipts from seed 0, each with the exact byte string its hash was computed over:

```bash
python3 - <<'EOF'
import json, hashlib
d = json.load(open("out/development/evaluation.json"))
rs = d["determinism"]["sample_receipts"]
print("hash == sha256(payload):", all(
    hashlib.sha256(r["canonical_payload"].encode()).hexdigest() == r["hash"] for r in rs))
print("chain links:", all(b["prev_hash"] == a["hash"] for a, b in zip(rs, rs[1:])))
EOF
# hash == sha256(payload): True
# chain links: True
```

Exhibit E on the dashboard does the same computation in your browser with the Web Crypto API.

---

## What the agent actually does

Real output from `make demo`, pinned byte-for-byte by [`tests/test_demo.py`](tests/test_demo.py) against [`data/golden/`](data/golden/). An expired card fails at 19:30 IST — inside the RBI Fair Practices Code's prohibited contact window:

```text
[4] classified
    failure_class: INSTRUMENT_DEAD
    rationale    : Zero percent chance of succeeding — not low, zero. There is
                   no state of the world in which the same expired card
                   authorises on the third attempt. A retry has exactly zero
                   expected value while consuming one of the four attempts the
                   re-auth link needed.

[6] guard chain -- cycle 1 (2026-08-21 19:30 IST)
    IdempotencyGuard    ALLOW
    RiskBlockGuard      NOT_APPLICABLE
    ConsentGuard        ALLOW              DPDP Act 2023 s.6 + DPDP Rules 2025
    RetryCapGuard       NOT_APPLICABLE
    PromiseToPayGuard   ALLOW              RBI FPC (fair dealing)
    DNDGuard            NOT_APPLICABLE
    FrequencyCapGuard   ALLOW              RBI FPC (anti-harassment)
    ContactWindowGuard  DEFER -> 2026-08-22 08:09 IST
                                           RBI FPC ¶55 — 19:30 IST is outside
                                           the 08:00-19:00 contact window
    ...
[7] decision -- cycle 1
    resolved     : DEFER -> 2026-08-22 08:09 IST
```

Three things are load-bearing here and none of them are the LLM:

1. **All thirteen guards run, then resolve by severity.** Not short-circuit. A cheapest-first chain would have stopped at the first refusal and the receipt would cite one clause instead of every violated one.
2. **Gating happens at execute time, not propose time.** The proposal was built at 19:30 and gated again when it woke at 08:09 — because consent can be withdrawn, and the payment can settle, in between.
3. **08:09, not 08:00.** The deferral target carries a per-customer offset derived from `sha256(customer_id)` — deterministic, so the ledger still replays byte-identically, but enough to stop a merchant's whole overnight backlog firing at 08:00:00.000. A burst of simultaneous messages reads to a recipient exactly like the automated dunning ¶55 exists to prevent.

---

## The air gap

The LLM has no tools. It cannot reach the Razorpay SDK, and there is no code path that converts what it emits into something executable — the diagnosis plane returns an `LLMVerdict`, and `LLMVerdict` is deliberately **not** a `Proposal`. There is no adapter. Invariant 1 is a property of the type graph, and [`tests/test_shadow_boundary.py`](tests/test_shadow_boundary.py) walks the import graph in both directions to prove it.

```mermaid
flowchart TD
    classDef plane fill:#1e1e1e,stroke:#333,stroke-width:2px,color:#fff
    classDef quarantine fill:#2d1b1b,stroke:#ff4444,stroke-width:2px,color:#fff
    classDef policy fill:#1b2d1b,stroke:#44ff44,stroke-width:2px,color:#fff
    classDef ledger fill:#1b1b2d,stroke:#4444ff,stroke-width:2px,color:#fff

    A["<b>1. EVENT INGRESS</b><br/>payment.failed · HMAC verified · deduped on event_id"]:::plane

    B["<b>2. THE QUARANTINE</b><br/>• LLM reads the failure, emits an LLMVerdict<br/>• Inert data. Not a Proposal. No adapter exists.<br/>⚠️ ZERO network access, ZERO SDK execution"]:::quarantine

    C["<b>3. THE POLICY MACHINE (13 guards)</b><br/>[G03] DPDP Act s.6 · [G07] anti-harassment cap<br/>[G08] RBI FPC ¶55 contact window<br/>[G09] RBI e-mandate pre-debit notice<br/>all evaluated, resolved by severity"]:::policy

    D["<b>4. EXECUTION PLANE</b><br/>The only code that may call Razorpay"]:::plane
    E["<b>5. DEFERRED QUEUE</b><br/>Re-gated on wake, never replayed blind"]:::plane

    F["<b>6. HASH-CHAINED LEDGER</b><br/>EXECUTED · BLOCKED · ESCALATED · RECOVERED<br/>Block_N = SHA256(Block_N-1 + canonical payload)"]:::ledger

    A --> B
    B -->|Inert verdict| C
    C -->|ALLOW| D
    C -->|BLOCK| F
    C -->|ESCALATE| F
    C -->|DEFER| E
    D --> F
    E -->|Wakes up, re-gates| A
    A -.->|Out of band| F
```

**Restraint is recorded as loudly as action.** A `BLOCKED` receipt is a first-class entry in the same chain as an `EXECUTED` one, carrying every clause that refused it. An agent that quietly does nothing and an agent that correctly declines are indistinguishable unless the ledger says which happened.

### Four of the thirteen guards

| Guard | Citation | Trigger | Response |
| :--- | :--- | :--- | :--- |
| **G03** `ConsentGuard` | DPDP Act 2023 s.6 | consent absent or withdrawn | `BLOCK`, and purge queued work for that customer |
| **G07** `FrequencyCapGuard` | RBI FPC (anti-harassment) | >2 contacts/episode, or >3 per customer per rolling 7d | `BLOCK` |
| **G08** `ContactWindowGuard` | RBI FPC ¶55 | dispatch time outside 08:00–19:00 IST | `DEFER` to the next open window |
| **G09** `PreDebitNoticeGuard` | RBI e-mandate framework | mandate debit with no notice served | `DEFER`, and emit the obligation to send one |

---

## F1–F7 — the criteria that could have killed this

Registered in [`docs/EVALUATION.md` §9](docs/EVALUATION.md) before any run, with thresholds, because a criterion invented after seeing the numbers is not a criterion.

| | Criterion | Threshold | Result |
| :--- | :--- | :--- | :--- |
| **F1** | The taxonomy adds nothing | interval vs `retry_plus_contact` includes zero | did not fire — but **excludes zero on the wrong side**, −16.35pp. Read as *worse* than F1 firing. |
| **F2** | The flagship `card_expired` claim is inert | A3 inert on recovery **and** attempts | did not fire — attempts-per-recovery −0.151 |
| **F3** | Salary-aware timing is noise | A2 interval includes zero | did not fire — +4.98pp |
| **F4** | The guards are unreliable | pass^100 < 1.0 | did not fire — pass^100 = 1.0 |
| **F5** | Compliance is unaffordable | ungated beats gated by >20pp | did not fire — 4.74pp |
| **F6** | The conclusions are model artifacts | ≥5 of 8 comparisons flip across the 83-config grid | did not fire |
| **F7** | Determinism fails | two runs of one seed differ | did not fire — ledgers identical |

**F1's `fired: false` is not good news and the artifact says so in its own `detail` field.** F1 as registered fires when the interval *includes* zero. Ours excludes zero — in the baseline's favour. The criterion is silent on that case, which is exactly why the manifest carries a `direction` field beside it.

---

## What broke

We wrote a survival criterion, registered it, and only then wrote 22 attacks against it. [`windtunnel/adversary/criterion.py`](windtunnel/adversary/criterion.py)'s `judge()` is the only thing that can return a verdict, and it scans the ledger the way §2a scans — never "a guard returned BLOCKED".

**18 of 22 survive.** Four remain open, and they are the complete set:

| | Attack | Why it still wins |
| :--- | :--- | :--- |
| **A01** | Out-of-band payment mid-ladder | A customer who pays through another channel carries no join key. We keep chasing money the merchant already has — a double-collection hazard, not a lost-revenue one. |
| **A07** | One human, two customer IDs | Per-human contact caps key on a derived id; two ids for one person defeat the cap. Worst case seen: 4 contacts in 7 days against a cap of 3. |
| **A08** | Contact window in the wrong timezone | The window is enforced in merchant-local IST, not the customer's. One contact landed at 22:30 customer-local. |
| **A09** | Message to a DND-listed customer | `DNDGuard` scopes to promotional traffic; the classification gap lets one through. |

Four attacks — A15, A16, A18, A19 — **were** open and are now closed. A queued proposal used to outlive the diagnosis that built it, so a retry minted from a benign row could fire on a payment that had since been risk-declined. `PolicyMachine.observe()` now retires superseded work. The full account, including the four demonstrations, is [`docs/taxonomy.md` §9.12](docs/taxonomy.md).

`make redteam` reproduces all of it.

---

## What this evaluation will not claim

The single most important section, and it is [in the protocol](docs/EVALUATION.md) rather than here:

- **Not** that Vasool would recover 49% of *your* failed payments. It measures a model, and the model is mine.
- **Eight of the nine outcome parameters are `[guess]`** — my judgement, tagged as such in the simulator's own source, where a parameter with no provenance tag fails a test. Nobody publishes conditional retry-success probabilities at this granularity, and inventing a citation would have been the first dishonest sentence in the repository.
- **Nine of ten error reasons are `_SIMULATED`.** Razorpay test mode reproduces exactly one failure reason — `payment_failed` — regardless of which documented "error scenario" card you use. That finding, and everything else learned live, is in [`docs/VERIFIED.md`](docs/VERIFIED.md).
- **Subscriptions were unavailable pre-activation**, so the failed-mandate loop is stub-only.
- **The `[guess]` fraction is itself a headline result** and appears on the dashboard as prominently as the recovery rate.

Every amendment to the protocol after registration — twenty-plus of them — is logged in §10 with a date, a reason, and a **POST-HOC** flag stating whether it was made with the relevant output already visible. Two rows were re-marked `No → Yes` when the standard was tightened retroactively, including one that had been disclosing honestly before there was a rule requiring it to.

---

## Repository map

| Path | What lives there |
| :--- | :--- |
| [`vasool/diagnosis/`](vasool/diagnosis/) | The failure taxonomy, the deterministic classifier, and the LLM shadow (which never touches a ledger) |
| [`vasool/policy/`](vasool/policy/) | Thirteen pure-function guards, the state machine, the transition log |
| [`vasool/actions/`](vasool/actions/) | The only code permitted to call Razorpay |
| [`vasool/ledger/`](vasool/ledger/) | Hash-chained receipts and `verify_chain` |
| [`windtunnel/`](windtunnel/) | The simulator, the outcome model, the evaluator, and the adversary |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | The pre-registered protocol. Append-only. |
| [`docs/taxonomy.md`](docs/taxonomy.md) | Why each failure class gets the intervention it gets, and §9's known limits |
| [`docs/VERIFIED.md`](docs/VERIFIED.md) | Everything learned from the live account, including what did not work |

---

<div align="center">
<sub>Built for the Razorpay AI Buildathon, Track 03.<br/>
A figure not derivable from the protocol is not a result — including ours.</sub>
</div>
