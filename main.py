#!/usr/bin/env python3
"""Werewolf agent for make.fun werewolf-agent-01.

A deterministic, model-free heuristic player (stdlib only, no network beyond
the env). It plays one seat in 30 eight-player games against seven house
players, as werewolf (15 games), seer (5), doctor (5) or villager (5).

How it plays
------------
Every decision is a pure function of the game's view as served by the env,
the trial seed, and per-game memory derived only from views already seen
(so a replay of the trial reproduces every action byte for byte: no clock,
no unseeded randomness, no cross-game state).

* Suspicion scoring. Each living seat gets a score from the public record:
  revealed roles, the seer's own inspections, who claimed to be the seer
  and whether their claims matched revealed roles, day-vote history (voting
  to exile a revealed villager is suspicious, voting out a revealed wolf
  clears you, voting alongside a revealed wolf is suspicious), and who
  accused whom in debate.
* Seer: inspects the most suspicious uninspected seat each night and
  announces a found wolf to the table at its next chance to speak, naming
  seat and night; counter-claims a false seer only when its own inspection
  proves the accused innocent.
* Doctor: protects a living seer claimant first, otherwise a proven
  villager (someone who voted for or accused a revealed wolf), falling
  back to the most trusted living seat; never repeats the previous night.
* Villager: votes the most suspicious living seat when the evidence clears
  a threshold, otherwise follows a clear plurality or abstains rather than
  exile on no evidence; speaks accusations backed by the cited evidence.
* Werewolf: matches its partner's night vote (the kill only lands when both
  wolves name the same seat), otherwise targets a seer claimant first, then
  whoever accused the wolves most. In debate it accuses the villager who
  looks worst to the village, defends itself when accused, and makes at
  most one fake seer claim per game (day 2+, only while nobody has claimed
  seer yet), naming the same target consistently.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ENV_URL = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
SEED = os.environ.get("MAKEFUN_TRIAL_SEED") or "0"

# Per-game memory, derived only from views already served in this run.
# Replay-safe: the same view sequence rebuilds the same memory.
MEMORY = {}


def mem(game):
    return MEMORY.setdefault(game, {"announced": set(), "fake_done": False,
                                    "fake_target": None})


def fold(text):
    """Fold a statement the env's way: runs of tabs/newlines become one space,
    other control characters are dropped, ends trimmed."""
    text = re.sub(r"[\t\n\v\f\r\x85\u2028\u2029]+", " ", text)
    text = "".join(ch for ch in text if not (ord(ch) < 32 or ord(ch) == 127))
    return text.strip()


def cut(text, max_chars):
    folded = fold(text)
    chars = list(folded)
    if len(chars) > max_chars:
        folded = "".join(chars[:max_chars]).strip()
    return folded


def tiebreak(game, day, seat):
    """Deterministic tie-break from the trial seed (stable across replay)."""
    h = hashlib.sha256(f"{SEED}:{game}:{day}:{seat}".encode()).hexdigest()
    return int(h[:12], 16)


# --------------------------------------------------------------------------
# View analysis
# --------------------------------------------------------------------------

def name_of(view, seat):
    for p in view.get("players", []):
        if p.get("seat") == seat:
            return p.get("name") or f"Seat {seat}"
    return f"Seat {seat}"


def seat_of_name(view, name):
    for p in view.get("players", []):
        if p.get("name", "").lower() == name.lower():
            return p.get("seat")
    return None


def mentioned_seats(view, text):
    """Seats a statement refers to, by 'seat N' or by player name."""
    out = set()
    for m in re.finditer(r"seat\s*(\d+)", text, re.I):
        out.add(int(m.group(1)))
    low = text.lower()
    for p in view.get("players", []):
        nm = (p.get("name") or "").lower()
        if nm and re.search(r"\b" + re.escape(nm) + r"\b", low):
            out.add(p["seat"])
    return out


def analyze(view):
    """Build the suspicion model for one game's view.

    Returns a dict with: living, revealed, known_wolf, known_innocent,
    suspicion {seat: score}, seer_claims {seat: {wolf:set, innocent:set}},
    accusers {seat: set}, votes_today {seat: [voters]}, accused_me (bool),
    night_dead (seat, role) of the latest dawn, and per-day vote records.
    """
    me = view["you"]["seat"]
    role = view["you"].get("role")
    players = {p["seat"]: p for p in view.get("players", [])}
    living = [s for s, p in players.items() if p.get("alive")]
    revealed = {s: p.get("role") for s, p in players.items()
                if p.get("role")}
    known_wolf = {s for s, r in revealed.items() if r == "werewolf"}
    known_innocent = {s for s, r in revealed.items()
                      if r and r != "werewolf"}
    partner = None
    if role == "werewolf":
        part = view["you"].get("partner")
        if part:
            partner = part["seat"]
            known_wolf.add(me)
            known_wolf.add(partner)
    if view.get("seerResults"):
        for r in view["seerResults"]:
            if r.get("werewolf"):
                known_wolf.add(r["seat"])
            else:
                known_innocent.add(r["seat"])

    log = view.get("log", [])
    suspicion = {s: 0 for s in living if s != me}
    seer_claims = {}
    accusers = {s: set() for s in living}
    votes_by_day = {}
    votes_today = {}
    accused_me = False
    night_dead = None
    false_claim = {}
    cleared = {s: 0 for s in living if s != me}

    for e in log:
        kind = e.get("kind")
        if kind == "dawn":
            night_dead = (e.get("died"), e.get("role"))
        elif kind == "speech":
            seat, text = e.get("seat"), (e.get("text") or "")
            low = text.lower()
            if seat == me:
                continue
            targets = mentioned_seats(view, text)
            targets.discard(seat)
            # Seer claims.
            if "seer" in low and re.search(
                    r"\b(i am|i'm|im)\b[^.]{0,20}\bseer\b", low):
                seer_claims.setdefault(seat, {"wolf": set(),
                                              "innocent": set()})
            if seat in seer_claims and ("check" in low or "inspect" in low
                                        or "saw" in low):
                for t in targets:
                    if "wolf" in low or "werewolf" in low:
                        seer_claims[seat]["wolf"].add(t)
                    elif ("not" in low and ("wolf" in low or "innocent" in low)) \
                            or "innocent" in low or "villager" in low:
                        seer_claims[seat]["innocent"].add(t)
            # Direct "seat N is a wolf" claims also count as seer-style claims
            # only when the speaker already claimed seer; otherwise plain
            # accusations.
            if ("wolf" in low or "werewolf" in low or "sus" in low
                    or "vote" in low):
                for t in targets:
                    if t == me:
                        accused_me = True
                    accusers.setdefault(t, set()).add(seat)
            if me in targets and ("not" in low and "wolf" in low):
                pass  # someone defended me; ignore
        elif kind == "vote":
            day = e.get("day")
            votes_by_day.setdefault(day, []).append(e)
            if day == view.get("day"):
                votes_today.setdefault(e.get("target"), []).append(e.get("seat"))

    # Claim credibility from revealed roles.
    for claimant, claims in seer_claims.items():
        cred = 0
        for t in claims["wolf"]:
            if t in known_wolf:
                cred += 1
            elif t in known_innocent:
                cred -= 1
        for t in claims["innocent"]:
            if t in known_innocent:
                cred += 1
            elif t in known_wolf:
                cred -= 1
        w = 50 if cred > 0 else (25 if cred == 0 else 0)
        for t in claims["wolf"]:
            if t in suspicion and t not in known_wolf \
                    and t not in known_innocent:
                suspicion[t] += w
        for t in claims["innocent"]:
            if t in suspicion and t not in known_wolf \
                    and t not in known_innocent:
                suspicion[t] -= w
        if cred < 0:
            suspicion[claimant] = suspicion.get(claimant, 0) + 30
            false_claim[claimant] = True

    # Two seer claimants: at least one lies.
    if len(seer_claims) >= 2:
        for c in seer_claims:
            if c in suspicion:
                suspicion[c] += 10

    # Vote history.
    for day, votes in votes_by_day.items():
        for v in votes:
            voter, target = v.get("seat"), v.get("target")
            if voter is None or target is None or voter == me:
                continue
            if target in known_wolf:
                suspicion[voter] = suspicion.get(voter, 0) - 25
                cleared[voter] = cleared.get(voter, 0) + 1
            elif target in known_innocent:
                suspicion[voter] = suspicion.get(voter, 0) + 20
        # Voters who agreed with a revealed wolf's vote that day.
        wolf_votes = {v.get("target") for v in votes
                      if v.get("seat") in known_wolf and v.get("target")}
        for v in votes:
            if v.get("target") in wolf_votes and v.get("seat") not in known_wolf:
                suspicion[v.get("seat")] = suspicion.get(v.get("seat"), 0) + 8

    # Accusations: being accused by several distinct seats is suspicious;
    # accusing a confirmed innocent is suspicious; accusing a revealed
    # wolf clears you (used by the doctor to pick a proven villager).
    for t, acc in accusers.items():
        if t in suspicion:
            suspicion[t] += min(18, 6 * len(acc))
        if t in known_wolf:
            for s in acc:
                if s in cleared:
                    cleared[s] += 1
    for e in log:
        if e.get("kind") == "speech":
            seat = e.get("seat")
            if seat == me or seat not in suspicion:
                continue
            low = (e.get("text") or "").lower()
            if "wolf" in low or "sus" in low:
                for t in mentioned_seats(view, e.get("text") or ""):
                    if t in known_innocent:
                        suspicion[seat] += 10

    # Confirmed knowledge overrides the heuristic.
    for s in known_wolf:
        if s in suspicion:
            suspicion[s] = 1000
    for s in known_innocent:
        if s in suspicion:
            suspicion[s] = -1000
    if partner is not None and partner in suspicion:
        suspicion.pop(partner, None)

    # Quiet players: no speech at all by day 2+ look slightly wolfy.
    if view.get("day", 1) >= 2:
        spoke = {e.get("seat") for e in log if e.get("kind") == "speech"}
        for s in list(suspicion):
            if s not in spoke and suspicion[s] < 1000:
                suspicion[s] += 5

    return {
        "me": me, "role": role, "living": living, "revealed": revealed,
        "known_wolf": known_wolf, "known_innocent": known_innocent,
        "partner": partner, "suspicion": suspicion,
        "seer_claims": seer_claims, "accusers": accusers,
        "cleared": cleared,
        "votes_today": votes_today, "accused_me": accused_me,
        "night_dead": night_dead, "false_claim": false_claim,
    }


def best(seats, score, game, day, reverse=True):
    """Highest-scored seat; ties broken by the seeded hash (deterministic)."""
    if not seats:
        return None
    return sorted(seats,
                  key=lambda s: ((-score.get(s, 0)) if reverse else score.get(s, 0),
                                 -tiebreak(game, day, s)))[0]


def speeches_today(view):
    day = view.get("day")
    return [e for e in view.get("log", [])
            if e.get("kind") == "speech" and e.get("day") == day]


def my_speeches_today(view):
    me = view["you"]["seat"]
    return [e for e in speeches_today(view) if e.get("seat") == me]


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

def decide_kill(game, view, options, a):
    me = a["me"]
    day = view.get("day", 1)
    # Match the partner's vote: the kill lands only on agreement.
    for v in view.get("wolfVotes") or []:
        if v.get("night") == day and v.get("seat") == a["partner"] \
                and v.get("target") in options:
            return v["target"]
    # Target a seer claimant first, then whoever accused us most.
    threat = {s: 0 for s in options}
    for c in a["seer_claims"]:
        if c in threat:
            threat[c] += 50
    for t, acc in a["accusers"].items():
        if t in (me, a["partner"]) and acc:
            for s in acc:
                if s in threat:
                    threat[s] += 10
    # Prefer talkative accusers over quiet villagers.
    spoke = {e.get("seat") for e in view.get("log", [])
             if e.get("kind") == "speech"}
    for s in threat:
        if s in spoke:
            threat[s] += 3
    return best(options, threat, game, day)


def decide_protect(game, view, options, a):
    day = view.get("day", 1)
    # Protect a living seer claimant first.
    for c in a["seer_claims"]:
        if c in options:
            return c
    # Otherwise a proven villager: someone who voted for, or accused, a
    # revealed wolf. A merely quiet seat may be a wolf lying low, so
    # "lowest suspicion" is only the fallback.
    cleared = {s: a["cleared"].get(s, 0) for s in options}
    if cleared and max(cleared.values()) > 0:
        return best(options, cleared, game, day)
    return best(options, a["suspicion"], game, day, reverse=False)


def decide_inspect(game, view, options, a):
    day = view.get("day", 1)
    done = {r["seat"] for r in (view.get("seerResults") or [])}
    fresh = [s for s in options if s not in done]
    pool = fresh if fresh else options
    # Most suspicious first; a claimant contradicting us is worth checking.
    score = dict(a["suspicion"])
    for c in a["seer_claims"]:
        if c in score:
            score[c] += 20
    return best(pool, score, game, day)


def decide_vote(game, view, options, a):
    day = view.get("day", 1)
    role = a["role"]
    sus = a["suspicion"]
    if role == "werewolf":
        # Exile the villager who looks worst to the village; join a
        # plurality already forming against a villager.
        pool = [s for s in options if s not in a["known_wolf"]]
        tally = {t: len(v) for t, v in a["votes_today"].items() if t}
        for t, n in tally.items():
            if n >= 2 and t in pool:
                return t
        return best(pool, sus, game, day)
    # Village side.
    wolves = [s for s in options if s in a["known_wolf"]]
    if wolves:
        return best(wolves, sus, game, day)
    top = best(options, sus, game, day)
    if top is not None and sus.get(top, 0) >= 15:
        return top
    # Follow a clear plurality against someone not confirmed innocent.
    tally = {t: len(v) for t, v in a["votes_today"].items()
             if t and t not in a["known_innocent"]}
    if tally:
        lead, n = max(tally.items(), key=lambda kv: (kv[1], -kv[0]))
        if n >= 2:
            return lead
    return None  # abstain rather than exile on no evidence


def decide_speak(game, view, options_max, a):
    """Return a statement string, or None to pass."""
    me, role, day = a["me"], a["role"], view.get("day", 1)
    m = mem(game)
    sus = a["suspicion"]
    living = [s for s in a["living"] if s != me]
    my_turns = len(my_speeches_today(view))

    def nm(s):
        return f"Seat {s} ({name_of(view, s)})"

    # --- Seer: announce a found wolf not yet announced. ---
    if role == "seer":
        for r in view.get("seerResults") or []:
            if r.get("werewolf") and r["seat"] not in m["announced"]:
                m["announced"].add(r["seat"])
                return (f"I am the seer. I checked {nm(r['seat'])} on night "
                        f"{r.get('night', day)} and they are a werewolf. "
                        f"Vote {nm(r['seat'])} out today.")
        # Counter a false seer claim against someone I cleared.
        for c, claims in a["seer_claims"].items():
            for t in claims["wolf"]:
                if t in a["known_innocent"] and c != me:
                    return (f"I am the real seer. I checked {nm(t)} and "
                            f"they are not a wolf, so {nm(c)} is lying. "
                            f"Vote {nm(c)}.")
    # --- Accused: defend, then counter-accuse. ---
    if a["accused_me"] and my_turns == 0:
        pool = [s for s in living if s not in a["known_innocent"]]
        top = best(pool, sus, game, day) if pool else None
        tail = (f" Look at {nm(top)} instead: their votes keep landing "
                f"on villagers.") if top is not None else ""
        return f"I am not a wolf. I have voted with the village all game.{tail}"

    # --- Werewolf: one consistent fake seer claim, day 2+, if none yet. ---
    if role == "werewolf" and day >= 2 and not m["fake_done"] \
            and not a["seer_claims"] and my_turns == 0:
        pool = [s for s in living
                if s not in a["known_wolf"] and s not in a["known_innocent"]]
        top = best(pool, sus, game, day) if pool else None
        if top is not None:
            m["fake_done"] = True
            m["fake_target"] = top
            return (f"I checked {nm(top)} last night. They are a "
                    f"werewolf. We should vote them out today.")
    if role == "werewolf" and m["fake_done"] and m["fake_target"] in living \
            and my_turns == 0:
        return (f"As I said, {nm(m['fake_target'])} is a wolf. "
                f"Vote them out before they eat another villager.")

    # --- Accuse the top suspect, with the reason the record gives. ---
    pool = [s for s in living if s not in a["known_innocent"]]
    if role == "werewolf":
        pool = [s for s in pool if s not in a["known_wolf"]]
    top = best(pool, sus, game, day) if pool else None
    if top is None:
        return None
    # Second turn of the day with nothing new: pass.
    if my_turns >= 1 and not a["accused_me"]:
        return None
    reason = reason_for(view, a, top)
    if day == 1 and sus.get(top, 0) < 15 and not a["known_wolf"]:
        dead = a["night_dead"]
        if dead and dead[0]:
            return (f"Night 1 took {nm(dead[0])}, a {dead[1] or 'villager'}. "
                    f"No hard evidence yet, so I will watch the votes "
                    f"before naming anyone.")
        return ("No one died last night, so we have no evidence yet. "
                "I will watch who pushes a quick exile today.")
    return f"{nm(top)} looks like a wolf to me. {reason}"


def reason_for(view, a, seat):
    """One evidence-backed reason string for suspecting a seat."""
    # Voted to exile a revealed villager.
    for e in view.get("log", []):
        if e.get("kind") == "vote" and e.get("seat") == seat:
            t = e.get("target")
            if t in a["known_innocent"]:
                return (f"They voted to exile {name_of(view, t)}, a "
                        f"revealed {a['revealed'].get(t, 'villager')}.")
    # Accused by several seats.
    acc = a["accusers"].get(seat, set())
    if len(acc) >= 2:
        names = ", ".join(name_of(view, s) for s in sorted(acc)[:3])
        return f"{names} have all pointed at them."
    # Claimed seer, contradicted.
    if seat in a["false_claim"]:
        return "Their seer claim was proven false."
    if seat in a["seer_claims"]:
        for t in a["seer_claims"][seat]["wolf"]:
            if t in a["known_innocent"]:
                return (f"They claimed to be the seer and accused "
                        f"{name_of(view, t)}, who is innocent.")
    # Voted alongside a revealed wolf.
    for e in view.get("log", []):
        if e.get("kind") == "vote" and e.get("seat") == seat:
            t = e.get("target")
            if t and any(v.get("seat") in a["known_wolf"]
                         and v.get("target") == t
                         for v in view.get("log", [])
                         if v.get("kind") == "vote"):
                return "They voted in lockstep with a revealed wolf."
    if view.get("day", 1) >= 2:
        spoke = {e.get("seat") for e in view.get("log", [])
                 if e.get("kind") == "speech"}
        if seat not in spoke:
            return "They have said nothing all game while villagers die."
    return "Their story keeps changing and their votes do not add up."


def decide(game, pending):
    kind = pending["decision"]
    view = pending["view"]
    a = analyze(view)
    if not view["you"].get("alive", True):
        # Dead seats are never asked, but stay safe if they are.
        return {"game": game, "decision": kind,
                **({"pass": True} if kind == "speak" else {"target": None})}
    if kind == "speak":
        text = decide_speak(game, view, pending.get("max_chars", 500), a)
        if text is None:
            return {"game": game, "decision": "speak", "pass": True}
        return {"game": game, "decision": "speak",
                "text": cut(text, pending.get("max_chars", 500))}
    options = pending.get("options") or []
    if kind == "kill":
        t = decide_kill(game, view, options, a)
    elif kind == "protect":
        t = decide_protect(game, view, options, a)
    elif kind == "inspect":
        t = decide_inspect(game, view, options, a)
    elif kind == "vote":
        t = decide_vote(game, view, options, a)
    else:
        t = None
    if t is not None and t not in options:
        t = None
    return {"game": game, "decision": kind, "target": t}


# --------------------------------------------------------------------------
# Env loop
# --------------------------------------------------------------------------

def post(path, body, timeout=65):
    req = urllib.request.Request(
        ENV_URL + path, data=json.dumps(body).encode(),
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def fallback(pending):
    if pending["decision"] == "speak":
        return {"game": pending["game"], "decision": "speak", "pass": True}
    return {"game": pending["game"], "decision": pending["decision"],
            "target": None}


def main():
    if not ENV_URL:
        print("MAKEFUN_ENV_URL is needed", file=sys.stderr)
        return 1
    decisions = 0
    while True:
        status, obs = post("/observe", {})
        if status != 200 or not isinstance(obs, dict):
            time.sleep(1)
            continue
        if obs.get("done"):
            break
        pending = obs.get("pending") or []
        if not pending:
            continue
        for p in pending:
            decisions += 1
            try:
                act = decide(p["game"], p)
            except Exception:
                act = fallback(p)
            status, _ = post("/act", act)
            if status == 400:
                status, _ = post("/act", fallback(p))
            elif status == 409:
                pass  # already answered / moved on
            elif status == 0:
                time.sleep(1)
    print(f"werewolf-agent: {decisions} decisions, all games finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
