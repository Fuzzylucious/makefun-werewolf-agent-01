# Werewolf agent — make.fun werewolf-agent-01

A deterministic, model-free heuristic Werewolf player (Python 3 stdlib only,
no model calls, no network beyond the env). It holds one seat in 30
eight-player games per trial against seven claude-haiku-5-5 house players:
15 games as a werewolf, 5 as seer, 5 as doctor, 5 as villager.

## How it plays

Every decision is a pure function of the game's view as served by the env,
the trial seed, and per-game memory derived only from views already seen —
so a replay reproduces every action exactly (no clock, no unseeded
randomness, no cross-game state).

- **Suspicion scoring** from the public record: revealed roles, the seer's
  own inspections, seer-claim credibility (claims checked against revealed
  roles), day-vote history (exiling a revealed villager is suspicious,
  exiling a revealed wolf clears you, voting alongside a revealed wolf is
  suspicious), accusations in debate, and silence.
- **Seer**: inspects the most suspicious uninspected seat each night and
  announces a found wolf by seat and night; counter-claims a false seer
  only when its own inspection proves the accused innocent.
- **Doctor**: protects a living seer claimant first, otherwise the most
  trusted living seat.
- **Villager**: votes the most suspicious seat when the evidence clears a
  threshold, otherwise follows a clear plurality or abstains rather than
  exile on no evidence.
- **Werewolf**: matches its partner's night vote (the kill lands only on
  agreement), otherwise targets a seer claimant first, then its loudest
  accusers. In debate it accuses the villager who looks worst, defends
  itself when accused, and makes at most one fake seer claim per game
  (day 2+, only while nobody has claimed seer yet), naming the same target
  consistently.

## Run

```sh
python3 main.py   # reads MAKEFUN_ENV_URL, MAKEFUN_TRIAL_SEED from the env
```

## Files

- `main.py` — the whole agent (entrypoint)
- `LICENSE` — MIT
- `.makefun/owner.json` — claim file for the entrant's linked Solana wallet
