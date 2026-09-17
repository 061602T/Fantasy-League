"""Agent personas: generation, name-collision negotiation, and persistence.

Each of the league's teams is run by an AI general manager (GM) with a distinct
persona. The whole cast is generated in ONE Sonnet call (``generate_cast``): a
fixed seed character, Dale, embodies himself and then invents the other seven
GMs as his friends -- seeing his own finished persona as he writes, so he can
deliberately contrast tone, voice, and behavior across all eight and avoid the
convergence the old eight-isolated-calls approach produced.

Two invented team names can still collide by chance. The league resolves that
the way a real one would: the colliding GMs argue it out in-character (at most
MAX_COLLISION_ROUNDS rounds), and if nobody backs down it goes to a coin flip --
the loser revises. Every message of that exchange is recorded for ``chat_log``.

The LLM is reached through ``ffl.llm``; tests replace the functions that call it
(``generate_cast``, ``_collision_message``, ``_revise_name``) to exercise the
logic deterministically without touching the API.
"""
from __future__ import annotations

import json
import random
import re
import sqlite3

from . import config, llm

# Allowed values for the structured persona fields (mirrors the teams schema
# comments in db.py). Free-generated values are normalised onto these.
RISK_TOLERANCES = ("boom-bust", "balanced", "safe-floor")
CHATTINESS = ("quiet", "moderate", "trash-talker")

# The fixed seed character. One agent embodies Dale, then invents the rest of
# the league as his friends in a single call, seeing the whole cast as it writes
# so it can deliberately contrast them (fixing the convergence the old
# eight-isolated-calls approach produced). Dale picks his own last name.
_SEED_BIO = (
    "Dale is a talker -- genuinely can't help it, fills silence with stories, "
    "and not all of them are true; he'll casually mention running into someone "
    "on the street who he definitely didn't, just because a conversation needs "
    "texture. He references \"back home\" constantly -- comparing league drama, "
    "waiver rules, anything, back to wherever he's from, even when it makes no "
    "sense to. He's quietly excellent with money -- the one thing about himself "
    "he'd never bring up -- but that discipline snaps the second real money or "
    "FAAB shows up unexpectedly: he'll blow a full season's saved-up budget on "
    "one ridiculous, overpriced waiver claim just because he can. He hates "
    "losing at things built to be competitive (fantasy football, Uno) but isn't "
    "the guy trying to turn a casual hangout into a battle. He doesn't like "
    "baseball -- cold childhood practices killed it permanently. His humor mixes "
    "self-deprecation, absurdism, dry one-liners, and real cruelty aimed at "
    "other people, sometimes in the same message. He's liberal, cares about "
    "real politics, and holds none of that back from his jokes. First "
    "impressions peg him as mean; he's not, just fast and unfiltered. Good moods "
    "bring out his wittiest lines; losing or anger makes him loud and scattered, "
    "losing his own thread mid-argument. He ends conflicts unpredictably -- "
    "sometimes needs the last word, sometimes goes dark and stops responding. "
    "Bites his nails when thinking or bored, carries a coin in his wallet for no "
    "defendable reason, won't get in small elevators, has a genuinely irrational "
    "fear of bees. Measures himself against his easygoing, happy-go-lucky "
    "grandma -- the one thing that gets sincerity out of him."
)

_CAST_SYSTEM = """\
You are Dale, a real person and one of eight friends about to play a season of \
fantasy football together. You're writing up the whole cast of this league -- \
yourself first, then the seven friends you rope in to play.

WHO YOU (DALE) ARE -- this is exactly your character. Pick your own last name: a \
normal, realistic human surname, NOT a pun or joke name.
%s

Then INVENT THE OTHER SEVEN GMs -- the friends you play this league with. You \
can see your own finished character as you write them, so make each friend \
DELIBERATELY DIFFERENT from you and from each other. Contrast their tone, voice, \
temperament, humor, and how they manage a team. Vary chattiness (some quiet, \
some moderate, some loud), risk tolerance, and personality -- do NOT let them \
converge into the same loud-trash-talker mold. They are their own distinct \
people, not clones of Dale.

NAMES -- two different rules:
- Every GM NAME (all 8, Dale included) is a normal, regular-sounding human name, \
  first and last. NOT a pun, NOT a joke, NOT a football reference.
- Every TEAM NAME (all 8) should be funny/creative -- puns, absurd names, mild \
  trash-talk-flavored names are all welcome. This is the ONE place humor belongs \
  in the naming.

CONTENT: a raunchy roast league. Characters trash-talk and can be cocky, crude, \
and foul-mouthed (mild profanity fine); each bio should carry real flaws and \
baggage rivals can roast. Fiction played for comedy: no real or identifiable \
people, nothing sexually explicit, and never build a character around real \
protected traits (race, religion, sex, gender, orientation, disability).

Return ONE JSON object: {"gms": [ ...exactly 8 objects, DALE FIRST... ]}. Each \
object has exactly these fields:
  "gm_name":        normal human full name (see NAMES).
  "team_name":      funny/creative franchise name, 1-4 words, no year/number.
  "personality":    1-2 vivid sentences on how they run their team and act in chat.
  "bio":            3-6 sentences of personal lore, flaws, and baggage. Dale's is
                    the character above, retold in your words with his last name.
  "risk_tolerance": one of "boom-bust", "balanced", "safe-floor".
  "valuation_bias": a short quirk in how they value players.
  "chattiness":     one of "quiet", "moderate", "trash-talker".
  "catchphrase":    one short in-character line they'd post in league chat.
""" % _SEED_BIO


def _norm(name: str) -> str:
    """Comparison key for names: lowercased, punctuation-insensitive, collapsed."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _coerce_enum(value, allowed, default):
    """Map a free-text value onto an allowed enum, else fall back to default.

    Comparison ignores separator style, so "Boom Bust" matches "boom-bust".
    """
    v = _norm(value)  # lowercased, punctuation -> spaces
    for a in allowed:
        if _norm(a) == v:
            return a
    for a in allowed:
        na = _norm(a)
        if na in v or v in na:
            return a
    return default


# --- Generation ------------------------------------------------------------

def generate_cast(n: int = config.NUM_TEAMS) -> list[dict]:
    """Generate all `n` GMs in a SINGLE call: Dale (the seed) plus n-1 friends.

    One agent embodies Dale and invents the rest of the league while seeing its
    own finished character, so it can deliberately contrast every GM instead of
    the old eight-isolated-calls approach that converged on the same voice.
    Returns a list of normalized persona dicts, Dale first. Raises if the model
    doesn't return at least `n` GMs (a live run should retry).
    """
    user = (f"Write all {n} GMs now -- Dale first, then {n - 1} contrasting "
            "friends -- as the single JSON object described.")
    data = llm.chat_json(_CAST_SYSTEM, user, model=llm.MODEL_DECISION,
                         max_tokens=8000)
    gms = data.get("gms") if isinstance(data, dict) else data
    if not isinstance(gms, list) or len(gms) < n:
        got = len(gms) if isinstance(gms, list) else 0
        raise RuntimeError(f"cast generation returned {got} GMs, expected {n}")
    return [_normalize_persona(g) for g in gms[:n]]


def _normalize_persona(data: dict) -> dict:
    """Validate/normalise a raw persona dict; keep the original under _raw."""
    team = str(data.get("team_name") or "Unnamed Team").strip()
    gm = str(data.get("gm_name") or "Anonymous GM").strip()
    return {
        "team_name": team,
        "gm_name": gm,
        "personality": (data.get("personality") or "").strip(),
        "bio": (data.get("bio") or "").strip(),
        "risk_tolerance": _coerce_enum(data.get("risk_tolerance"),
                                       RISK_TOLERANCES, "balanced"),
        "valuation_bias": (data.get("valuation_bias") or "").strip(),
        "chattiness": _coerce_enum(data.get("chattiness"), CHATTINESS, "moderate"),
        "catchphrase": (data.get("catchphrase") or "").strip(),
        "_raw": data,
    }


# --- Collision detection & negotiation -------------------------------------

def find_collisions(personas: list[dict], field: str) -> list[list[int]]:
    """Groups of persona indices that share a normalised value for `field`."""
    groups: dict[str, list[int]] = {}
    for i, p in enumerate(personas):
        groups.setdefault(_norm(p[field]), []).append(i)
    return [idxs for idxs in groups.values() if len(idxs) > 1]


def resolve_collisions(personas: list[dict]) -> list[dict]:
    """Resolve every duplicate team_name and gm_name in place.

    Returns a transcript: an ordered list of {"agent": idx|None, "event": str,
    "text": str}. Revisions can create fresh collisions, so each field is
    re-scanned until clean (bounded for safety).
    """
    transcript: list[dict] = []
    for field in ("team_name", "gm_name"):
        for _ in range(50):  # safety bound; each pass fixes >=1 group
            groups = find_collisions(personas, field)
            if not groups:
                break
            for group in groups:
                _resolve_one(personas, group, field, transcript)
    return transcript


def _resolve_one(personas, group, field, transcript):
    """Negotiate a single collision group for one field; revise the losers."""
    contested = personas[group[0]][field]
    kind = "team name" if field == "team_name" else "GM name"
    winner = None

    for rnd in range(1, config.MAX_COLLISION_ROUNDS + 1):
        non_ceders = []
        for idx in group:
            msg = _collision_message(personas, idx, group, field, contested, rnd)
            text = msg.get("message", "").strip()
            transcript.append({
                "agent": idx, "event": "collision",
                "text": f"[Round {rnd}] {personas[idx]['gm_name']}: {text}",
            })
            if not msg.get("cede"):
                non_ceders.append(idx)
        if len(non_ceders) == 1:            # one holdout keeps it, rest revise
            winner = non_ceders[0]
            break
        if not non_ceders:                  # everyone offered to cede
            break

    if winner is None:                      # unresolved after 3 rounds -> coin flip
        winner = random.choice(group)
        transcript.append({
            "agent": None, "event": "system",
            "text": (f'Coin flip: the {kind} "{contested}" is awarded to '
                     f"{personas[winner]['gm_name']}. The other(s) must revise."),
        })

    taken = {_norm(p[field]) for p in personas}
    for loser in [i for i in group if i != winner]:
        result = _revise_name(personas, loser, field, contested, taken)
        new_val = result["new_value"]
        personas[loser][field] = new_val
        taken.add(_norm(new_val))
        note = result.get("message", "").strip()
        transcript.append({
            "agent": loser, "event": "collision",
            "text": (f"{personas[loser]['gm_name']} rebrands their {kind} to "
                     f'"{new_val}".' + (f" {note}" if note else "")),
        })


def _collision_message(personas, idx, group, field, contested, rnd) -> dict:
    """One in-character negotiation message. Returns {"message", "cede": bool}."""
    me = personas[idx]
    rivals = [personas[j]["gm_name"] for j in group if j != idx]
    kind = "team name" if field == "team_name" else "GM name"
    system = (
        f"You are {me['gm_name']}, GM of \"{me['team_name']}\". "
        f"Persona: {me['personality']} Chattiness: {me['chattiness']}.\n"
        f"Speak in character, in the first person, in 1-2 sentences."
        + llm.VOICE
    )
    user = (
        f'You and {", ".join(rivals)} independently picked the same {kind}: '
        f'"{contested}". Only one GM can keep it. This is negotiation round '
        f"{rnd} of {config.MAX_COLLISION_ROUNDS}; if nobody yields after the "
        f"final round it goes to a coin flip.\n"
        f'Argue your case to keep "{contested}", then decide: will you yield it?\n'
        'Return JSON: {"message": "<what you say out loud>", '
        '"cede": true or false}.'
    )
    data = llm.chat_json(system, user, model=llm.MODEL_DECISION, max_tokens=1200)
    return {"message": str(data.get("message", "")), "cede": bool(data.get("cede"))}


def _revise_name(personas, idx, field, contested, taken) -> dict:
    """Ask the losing GM for a fresh, unused name. Returns {"new_value","message"}."""
    me = personas[idx]
    kind = "team name" if field == "team_name" else "GM name"
    system = (
        f"You are {me['gm_name']}, GM of \"{me['team_name']}\". "
        f"Persona: {me['personality']}\nStay in character."
        + llm.VOICE
    )
    used = ", ".join(sorted(_denorm_sample(personas, field)))
    user = (
        f'You lost the fight for the {kind} "{contested}", so you need a new '
        f"one that fits your persona. It must be different from all of these "
        f"(case/punctuation-insensitive): {used}.\n"
        'Return JSON: {"new_value": "<your new ' + kind + '>", '
        '"message": "<a short in-character reaction, 1 sentence>"}.'
    )
    for _ in range(4):
        data = llm.chat_json(system, user, model=llm.MODEL_DECISION, max_tokens=1200)
        new_val = str(data.get("new_value", "")).strip()
        if new_val and _norm(new_val) not in taken:
            return {"new_value": new_val, "message": str(data.get("message", ""))}
    # Deterministic fallback so we always converge.
    base = contested
    n = 2
    while _norm(f"{base} {_roman(n)}") in taken:
        n += 1
    return {"new_value": f"{base} {_roman(n)}",
            "message": data.get("message", "") if isinstance(data, dict) else ""}


def _denorm_sample(personas, field):
    return {p[field] for p in personas}


def _roman(n: int) -> str:
    return {2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII"}.get(n, str(n))


# --- Persistence -----------------------------------------------------------

def persist_teams(conn: sqlite3.Connection, personas: list[dict],
                  draft_slots: list[int]) -> list[int]:
    """Insert the teams and return their new team_ids, in persona order."""
    ids = []
    for p, slot in zip(personas, draft_slots):
        cur = conn.execute(
            """INSERT INTO teams(team_name, gm_name, personality, bio,
                 risk_tolerance, valuation_bias, chattiness, draft_slot,
                 faab_budget, faab_remaining, persona_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (p["team_name"], p["gm_name"], p["personality"], p.get("bio", ""),
             p["risk_tolerance"], p["valuation_bias"], p["chattiness"], slot,
             config.FAAB_BUDGET, config.FAAB_BUDGET, json.dumps(p.get("_raw", p))),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def persist_transcript(conn: sqlite3.Connection, transcript: list[dict],
                       team_ids: list[int]) -> int:
    """Write the collision negotiation transcript to chat_log."""
    rows = [
        (team_ids[e["agent"]] if e["agent"] is not None else None,
         e["event"], e["text"])
        for e in transcript
    ]
    conn.executemany(
        "INSERT INTO chat_log(team_id, event_type, message) VALUES(?,?,?)", rows)
    conn.commit()
    return len(rows)


def ensure_league(conn: sqlite3.Connection) -> None:
    """Insert the single league row if it isn't there yet (status 'setup')."""
    exists = conn.execute("SELECT 1 FROM league WHERE id = 1").fetchone()
    if exists:
        return
    settings = {
        "starters": config.STARTERS, "bench": config.BENCH_SPOTS,
        "roster_size": config.ROSTER_SIZE, "draft_rounds": config.DRAFT_ROUNDS,
    }
    conn.execute(
        """INSERT INTO league(id, season, as_of_week, current_week, num_teams,
             status, settings_json)
           VALUES(1,?,?,?,?,?,?)""",
        (config.SEASON, config.AS_OF_WEEK, 0, config.NUM_TEAMS, "setup",
         json.dumps(settings)),
    )
    conn.commit()


def build_league(conn: sqlite3.Connection, n: int = config.NUM_TEAMS) -> dict:
    """Full step-3 flow: generate personas, resolve collisions, persist all.

    Returns a summary dict {team_ids, personas, transcript}.
    """
    ensure_league(conn)
    personas = generate_cast(n)
    transcript = resolve_collisions(personas)
    slots = list(range(1, n + 1))
    random.shuffle(slots)
    team_ids = persist_teams(conn, personas, slots)
    persist_transcript(conn, transcript, team_ids)
    return {"team_ids": team_ids, "personas": personas,
            "draft_slots": slots, "transcript": transcript}
