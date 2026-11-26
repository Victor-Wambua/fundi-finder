# Ranking

How Fundi Finder orders fundis in search. The maths is in `src/fundifinder/ranking.py`
(pure functions, no database). Every number below is produced by that code and checked in
`tests/test_ranking.py`.

## The score

```
score = 100 × ( 0.55 · quality
              + 0.15 · volume
              + 0.12 · response
              + 0.10 · punctuality
              + 0.08 · verification )
```

| Term | Definition | Why |
|---|---|---|
| quality | `(bayes_mean − 1) / 4`, see below | The evidence about work quality, shrunk toward the platform mean |
| volume | `1 − exp(−completed_jobs / 30)` | Confirmed jobs are the hardest thing to fake. Saturates, so nobody needs 500 |
| response | `(answered_in_24h + 0.75·3) / (answerable + 3)` | Unanswered requests cost customers days. Smoothed so one miss is not fatal |
| punctuality | `(on_time + 0.80·3) / (eligible + 3)` | On time = started within 15 minutes of the slot. Fundi cancellations and no-shows count against |
| verification | `badges / 4` | Phone, ID, References, Trade skills. Each is computed from records |

### Quality: a Bayesian mean with recency and trust weights

```
w_i     = max(0.25, 0.5 ^ (age_days_i / 180)) × trust_i
bayes   = (C · m  +  Σ w_i · rating_i)  /  (C + Σ w_i)
```

* `m` is the platform mean rating, **clamped to [3.5, 4.5]** so the prior cannot be inflated
  or dragged around.
* `C = 8` pseudo-reviews at the platform mean. A fundi has to out-weigh the prior before their
  own average dominates.
* Recency: a review's weight halves every 180 days, with a floor of 0.25. Old honest history
  still counts, but recent form counts more.
* `trust_i` is 1 normally, **0.25** while the review is under an open abuse flag, and a removed
  review is excluded entirely.

`bayes` is a weighted average of the prior and the reviews, so it always lies between 1 and 5,
and adding a review of rating `r` moves it toward `r`. That one fact gives the monotonicity
property below.

### Wilson lower bound (shown on profiles, not used for ranking)

Profiles say "N% of reviews were 4 or 5 stars; we are 95% sure the true figure is at least
M%". M is the Wilson score interval lower bound. 3 happy customers out of 3 gives 44%, 58 of
60 gives 89%. It is a display aid so small samples are not read as certainty.

## Worked examples

All reviews fresh, platform mean 4.2, identical response/punctuality history, no badges.

| Fundi | Reviews | Completed | Bayes mean | Score |
|---|---|---|---|---|
| A | 1 × 5★ | 1 | (8·4.2 + 5) / 9 = **4.289** | **62.7** |
| B | 48 × 5★ + 12 × 4★ (avg 4.8) | 60 | (8·4.2 + 240 + 48) / 68 = **4.729** | **81.2** |
| C | 11 × 5★ + 1 × 4★ | 12 | **4.630** | 71.9 |
| D | none | 0 | 4.200 (the prior) | 61.0 |

A has a perfect average and still ranks 18 points below B. B's 60 reviews two years old
(weight floor 0.25) still score 79.4: history decays slowly.

Effect of a review ring (4 honest 4★ reviews + 6 fake 5★ reviews, 10 completed jobs):

| State | Bayes mean | Score |
|---|---|---|
| Before the scan | 4.422 | 68.3 |
| Ring flagged (trust 0.25) | 4.230 | 65.7 |
| Ring confirmed and removed | 4.133 | 62.0 |

The fake reviews are worth six review-points while unflagged, one and a half while flagged,
and nothing once removed.

## New fundis: reserved slots, not a score bonus

A score bonus for newcomers would break the guarantee that one fake 5★ cannot outrank an
established fundi, and would be the first thing an attacker optimises for. So scores stay
clean. Instead, in `order_results`:

* A fundi is **new** with fewer than 5 confirmed jobs.
* Every 4th result slot is reserved for a new fundi **whose phone is verified** (unverified
  accounts cannot farm exposure).
* New fundis are ordered by score but **rotated daily** so exposure is shared.
* Established fundis keep strict score order.
* The UI labels these slots "New on Fundi Finder", so nobody is misled about why they are there.

## Properties that are tested

Using Hypothesis (`tests/test_ranking.py`):

1. **Monotonicity.** For any stats and any platform mean, adding a fresh 5★ review (and one
   more completed job) never lowers the score.
2. **One 5★ never beats sixty 4.8★.** For platform means from 3.0 to 4.7 and any shared
   response/punctuality/badge history.
3. The score is always within 0 to 100, and the Bayesian mean within 1 to 5.
4. A review moves the mean toward its own rating, never away.
5. Quarantined reviews (trust 0) have no effect at all.
6. Older reviews of the same rating never weigh more than newer ones.
7. `order_results` returns a permutation, keeps established fundis in score order, and puts
   newcomers exactly in every 4th slot while any remain.

## Failure modes (honest list)

* **Platform mean drift.** If most reviews become fake the mean moves, but the clamp keeps the
  prior between 3.5 and 4.5, so it cannot swing far.
* **Collusion between a fundi and real customers.** The completion code proves a customer
  and fundi met in the app. It cannot prove the job was honest. The ring heuristics only catch
  throwaway accounts (see `abuse.py`); a patient attacker with aged accounts that review other
  fundis passes.
* **Volume rewards the busy.** The `volume` term favours big operations over a careful solo
  fundi. It is capped at 15 points and saturates near 90 jobs.
* **Response/punctuality are measured only inside the app.** A fundi who answers on WhatsApp
  but ignores the inbox is penalised.
* **Weights are judgement, not fitted.** There is no real conversion or complaint data to tune
  them against. They are in `RankParams` so they can be changed in one place.
* **Cold start for customers is unsolved.** In an area with five fundis, every ordering is
  mostly noise.
