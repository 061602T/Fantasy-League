"""The player market: trades and FAAB waivers -- the league's real decisions.

Both use the two model tiers: a cheap Haiku "do you want to act?" gate decides
whether a GM engages at all, and Sonnet makes the actual call (what to offer,
accept/reject/counter, what to bid). Everything a GM references is chosen from
*numbered menus* so the model never has to echo raw player ids.

Trades
  The proposer sets an even N-for-N player package (1-3 each way); roster sizes
  stay at 15. Over up to MAX_TRADE_ROUNDS the decider (alternating each round)
  accepts, walks away, or counters by haggling the FAAB sweetener (keeping the
  players fixed keeps counters unambiguous). A deal executes only if both
  rosters stay legal (a fieldable lineup) and the FAAB is affordable.

Waivers
  One blind FAAB claim (add a free agent, drop a roster player) per team per
  run. Resolved highest-bid-first, tie-broken by remaining FAAB then draft slot.
  FAAB is spent only on a winning claim. Adds/drops must keep the roster legal.

Persistence: trades and claims are written to `transactions` (status tracks the
lifecycle) and `rosters` (dropped_week on the losing side, a new row on the
gaining side); trade talk and waiver results are written to `chat_log`.
"""
from __future__ import annotations

import json
import random
import sqlite3

from . import config, llm, rosters

MAX_TRADE_ROUNDS = config.MAX_TRADE_ROUNDS


# --- Menus -----------------------------------------------------------------

def _menu(rows, letter: str, proj_map: dict) -> tuple[str, dict]:
    """Number a roster: returns (text, {number: player_id})."""
    lines, idx = [], {}
    for i, r in enumerate(rows, start=1):
        tag = f"{letter}{i}"
        idx[tag] = r["player_id"]
        proj = proj_map.get(r["player_id"], 0.0)
        lines.append(f"  {tag}. {r['name']} ({r['position']}, proj {proj:.1f})")
    return "\n".join(lines), idx


def _names(conn, ids) -> dict:
    return {r["player_id"]: r["name"] for r in [
        conn.execute("SELECT player_id, name FROM players WHERE player_id=?",
                     (pid,)).fetchone() for pid in ids] if r}


# --- Trade validation & execution ------------------------------------------

def validate_offer(conn: sqlite3.Connection, offer: dict) -> bool:
    """Structural + legality + affordability check for a trade offer."""
    a, b = offer["a"], offer["b"]
    ag, bg = offer["a_gives"], offer["b_gives"]
    if not ag or not bg or len(ag) != len(bg) or len(ag) > 3:
        return False
    if len(set(ag)) != len(ag) or len(set(bg)) != len(bg):
        return False
    # Ownership: a_gives all owned by a, b_gives all owned by b.
    if any(rosters.owner_of(conn, p) != a for p in ag):
        return False
    if any(rosters.owner_of(conn, p) != b for p in bg):
        return False
    af, bf = int(offer.get("a_faab", 0)), int(offer.get("b_faab", 0))
    if af < 0 or bf < 0:
        return False
    ta = conn.execute("SELECT faab_remaining FROM teams WHERE team_id=?", (a,)).fetchone()
    tb = conn.execute("SELECT faab_remaining FROM teams WHERE team_id=?", (b,)).fetchone()
    if af > ta["faab_remaining"] or bf > tb["faab_remaining"]:
        return False
    # Both rosters must remain full and startable.
    if not rosters.legal_after(conn, a, add_ids=bg, drop_ids=ag):
        return False
    if not rosters.legal_after(conn, b, add_ids=ag, drop_ids=bg):
        return False
    return True


def execute_trade(conn: sqlite3.Connection, offer: dict) -> bool:
    """Apply a validated trade atomically. Returns False (no-op) if invalid."""
    if not validate_offer(conn, offer):
        return False
    a, b = offer["a"], offer["b"]
    week = rosters.current_week(conn)
    af, bf = int(offer.get("a_faab", 0)), int(offer.get("b_faab", 0))

    def move(players, frm, to):
        for pid in players:
            conn.execute("UPDATE rosters SET dropped_week=? WHERE player_id=? "
                         "AND team_id=? AND dropped_week IS NULL", (week, pid, frm))
            conn.execute("""INSERT INTO rosters(team_id, player_id, acquired_via,
                              acquired_week) VALUES(?,?, 'trade', ?)""",
                         (to, pid, week))

    move(offer["a_gives"], a, b)
    move(offer["b_gives"], b, a)
    if af:
        conn.execute("UPDATE teams SET faab_remaining=faab_remaining-? WHERE team_id=?", (af, a))
        conn.execute("UPDATE teams SET faab_remaining=faab_remaining+? WHERE team_id=?", (af, b))
    if bf:
        conn.execute("UPDATE teams SET faab_remaining=faab_remaining-? WHERE team_id=?", (bf, b))
        conn.execute("UPDATE teams SET faab_remaining=faab_remaining+? WHERE team_id=?", (bf, a))
    conn.commit()
    return True


def describe_offer(conn, offer: dict, viewer: int) -> str:
    """Plain-language offer from `viewer`'s side."""
    a, b = offer["a"], offer["b"]
    names = _names(conn, offer["a_gives"] + offer["b_gives"])
    a_side = ", ".join(names[p] for p in offer["a_gives"])
    b_side = ", ".join(names[p] for p in offer["b_gives"])
    af, bf = int(offer.get("a_faab", 0)), int(offer.get("b_faab", 0))
    if viewer == a:
        give, get = f"{a_side}" + (f" + ${af} FAAB" if af else ""), \
                    f"{b_side}" + (f" + ${bf} FAAB" if bf else "")
    else:
        give, get = f"{b_side}" + (f" + ${bf} FAAB" if bf else ""), \
                    f"{a_side}" + (f" + ${af} FAAB" if af else "")
    return f"YOU GIVE: {give}\nYOU RECEIVE: {get}"


# --- Trade decisions (LLM) --------------------------------------------------

def _team(conn, tid):
    return conn.execute("SELECT * FROM teams WHERE team_id=?", (tid,)).fetchone()


def wants_to_trade(conn, team_id: int, proj_map: dict) -> bool:
    """Haiku gate: is this GM interested in shopping for a trade right now?"""
    t = _team(conn, team_id)
    roster_txt, _ = _menu(rosters.active_roster(conn, team_id), "R", proj_map)
    system = (f"You are {t['gm_name']}, a fantasy football GM. Persona: "
              f"{t['personality']} Answer only with JSON.")
    user = (f"Your roster:\n{roster_txt}\n\nWould you like to explore a trade to "
            'improve this team right now? Return {"act": true or false}.')
    return llm.gate(system, user)


def _offer_from_nums(a_id, b_id, amap, bmap, spec) -> dict | None:
    """Translate a {a_gives, b_gives, faab} spec (numbers, "A7"/"7" both ok)."""
    def pick(items, m):
        out = []
        for x in items or []:
            digits = "".join(ch for ch in str(x) if ch.isdigit())
            if digits in m:
                out.append(m[digits])
        return out
    a_gives = pick(spec.get("a_gives"), amap)
    b_gives = pick(spec.get("b_gives"), bmap)
    if not a_gives or not b_gives:
        return None
    return {"a": a_id, "b": b_id, "a_gives": a_gives, "b_gives": b_gives,
            "a_faab": int(spec.get("a_faab", 0) or 0),
            "b_faab": int(spec.get("b_faab", 0) or 0)}


def _ab_menu(conn, a_id, b_id, proj_map):
    at, amap = _menu(rosters.active_roster(conn, a_id), "A", proj_map)
    bt, bmap = _menu(rosters.active_roster(conn, b_id), "B", proj_map)
    # amap/bmap keyed "A1"/"B1"; also index by bare number for either side.
    a_by_num = {k[1:]: v for k, v in amap.items()}
    b_by_num = {k[1:]: v for k, v in bmap.items()}
    return at, bt, a_by_num, b_by_num


def propose_offer(conn, a_id, b_id, proj_map) -> dict | None:
    """Proposer (team a) constructs an initial even-swap offer via Sonnet."""
    ta, tb = _team(conn, a_id), _team(conn, b_id)
    at, bt, amap, bmap = _ab_menu(conn, a_id, b_id, proj_map)
    system = (f"You are {ta['gm_name']}, GM of \"{ta['team_name']}\". Persona: "
              f"{ta['personality']} Valuation quirk: {ta['valuation_bias']}. "
              f"You have ${ta['faab_remaining']} FAAB. Propose trades in character "
              f"but only ones that genuinely help your team."
              + llm.VOICE)
    user = (
        f"You want to propose a trade to {tb['gm_name']} ({tb['team_name']}).\n"
        f"YOUR players (A#):\n{at}\n\nTHEIR players (B#):\n{bt}\n\n"
        "Propose an even swap. IMPORTANT: a_gives and b_gives MUST be the same "
        "length (1-3 players each), optionally sweetened with FAAB. Return JSON: "
        '{"a_gives": [<A#>...], "b_gives": [<B#>...], "a_faab": <int>, '
        '"b_faab": <int>, "message": "<in-character pitch>"}.')
    try:
        spec = llm.chat_json(system, user, max_tokens=1200)
    except (ValueError, json.JSONDecodeError):
        return None
    offer = _offer_from_nums(a_id, b_id, amap, bmap, spec)
    if offer is None:
        return None
    offer["_message"] = str(spec.get("message", "")).strip()
    return offer if validate_offer(conn, offer) else None


def _faab_counter(conn, offer, decider_id, faab_to_me) -> dict | None:
    """Same players, but the OTHER side sends `faab_to_me` FAAB to the decider."""
    new = dict(offer)
    if decider_id == offer["b"]:
        new["a_faab"], new["b_faab"] = faab_to_me, 0
    else:
        new["b_faab"], new["a_faab"] = faab_to_me, 0
    return new if validate_offer(conn, new) else None


def evaluate_offer(conn, decider_id, offer, rnd, proj_map) -> dict:
    """Decider responds to a fixed-player offer: accept / reject / counter.

    The player package is set by the proposer; the decider haggles over FAAB
    only (unambiguous and robust). A counter keeps the same players and asks the
    other GM to send FAAB.
    """
    t = _team(conn, decider_id)
    other = offer["a"] if decider_id == offer["b"] else offer["b"]
    other_faab = conn.execute(
        "SELECT faab_remaining FROM teams WHERE team_id=?", (other,)).fetchone()[0]
    system = (f"You are {t['gm_name']}, GM of \"{t['team_name']}\". Persona: "
              f"{t['personality']} Risk: {t['risk_tolerance']}. Decide in "
              f"character but protect your team's value."
              + llm.VOICE)
    user = (
        f"Trade round {rnd} of {MAX_TRADE_ROUNDS}. Offer on the table (the "
        f"players are FIXED -- you may only haggle over FAAB):\n"
        f"{describe_offer(conn, offer, decider_id)}\n\n"
        f"The other GM has ${other_faab} FAAB available. Decide one of:\n"
        "  - accept: take the deal as it stands.\n"
        "  - counter: keep the SAME players but ask the other GM to send you "
        "FAAB to sweeten it; give the total FAAB you want sent TO YOU.\n"
        "  - reject: walk away -- this ENDS the talks. Only reject if no FAAB "
        "sweetener would make this work.\n"
        'Return JSON: {"decision": "accept"|"reject"|"counter", '
        '"message": "<in-character line>", "faab_to_me": <int>}.')
    try:
        data = llm.chat_json(system, user, max_tokens=800)
    except (ValueError, json.JSONDecodeError):
        return {"decision": "reject", "message": ""}
    decision = str(data.get("decision", "reject")).lower()
    out = {"decision": decision, "message": str(data.get("message", "")).strip()}

    faab_to_me = int(data.get("faab_to_me", 0) or 0)
    counter = _faab_counter(conn, offer, decider_id, faab_to_me) if faab_to_me > 0 else None

    if decision == "accept":
        out["decision"] = "accept"
    elif counter is not None:
        # A concrete, affordable FAAB ask means "keep dealing" even if labelled
        # reject -- honour the intent so the negotiation rounds are used.
        out["decision"] = "counter"
        out["counter"] = counter
    else:
        out["decision"] = "reject"
    return out


# --- Trade orchestration ---------------------------------------------------

def _log(conn, team_id, event, message, txn_id=None):
    conn.execute("INSERT INTO chat_log(team_id, event_type, message, txn_id) "
                 "VALUES(?,?,?,?)", (team_id, event, message, txn_id))


def negotiate(conn, a_id, b_id, proj_map, initial=None) -> dict:
    """Run a full trade negotiation between team a (proposer) and team b.

    Returns {status, offer, rounds}. status in accepted/rejected/failed/no_offer.
    """
    offer = initial or propose_offer(conn, a_id, b_id, proj_map)
    if offer is None:
        return {"status": "no_offer", "offer": None, "rounds": 0}

    cur = conn.execute(
        """INSERT INTO transactions(type, status, from_team_id, to_team_id,
             round, details_json) VALUES('trade','proposed',?,?,0,?)""",
        (a_id, b_id, json.dumps(_clean(offer))))
    txn_id = cur.lastrowid
    conn.commit()
    ta, tb = _team(conn, a_id), _team(conn, b_id)
    _log(conn, a_id, "trade_talk",
         f"{ta['gm_name']} offers {tb['gm_name']} a trade. "
         f"{offer.get('_message', '')}".strip(), txn_id)

    decider, current = b_id, offer
    for rnd in range(1, MAX_TRADE_ROUNDS + 1):
        resp = evaluate_offer(conn, decider, current, rnd, proj_map)
        who = _team(conn, decider)
        _log(conn, decider, "trade_talk",
             f"[Round {rnd}] {who['gm_name']}: {resp['message']}", txn_id)
        conn.execute("UPDATE transactions SET round=? WHERE txn_id=?", (rnd, txn_id))

        if resp["decision"] == "accept":
            ok = execute_trade(conn, current)
            status = "processed" if ok else "failed"
            _finish_txn(conn, txn_id, status, current)
            _log(conn, None, "trade_talk",
                 f"Trade {'completed' if ok else 'fell through (illegal/unaffordable)'}.",
                 txn_id)
            return {"status": "accepted" if ok else "failed",
                    "offer": _clean(current), "rounds": rnd}
        if resp["decision"] == "counter" and "counter" in resp:
            current = resp["counter"]
            conn.execute("UPDATE transactions SET status='countered', details_json=? "
                         "WHERE txn_id=?", (json.dumps(_clean(current)), txn_id))
            conn.commit()
            decider = a_id if decider == b_id else b_id
            continue
        # reject (or invalid)
        _finish_txn(conn, txn_id, "rejected", current)
        return {"status": "rejected", "offer": _clean(current), "rounds": rnd}

    _finish_txn(conn, txn_id, "rejected", current)  # ran out of rounds
    return {"status": "rejected", "offer": _clean(current), "rounds": MAX_TRADE_ROUNDS}


def attempt_one_trade(conn, proj_map, rng=random, use_gate: bool = True) -> dict | None:
    """Pick an interested GM and a random partner, and run one negotiation.

    For mid-week 'life' between scored weeks. Tries a few GMs through the Haiku
    gate; returns the negotiation result, or None if nobody wanted to deal.
    """
    teams = [r["team_id"] for r in conn.execute("SELECT team_id FROM teams")]
    rng.shuffle(teams)
    initiator = next((t for t in teams[:3]
                      if not use_gate or wants_to_trade(conn, t, proj_map)), None)
    if initiator is None:
        return None
    partners = [t for t in teams if t != initiator]
    rng.shuffle(partners)
    return negotiate(conn, initiator, partners[0], proj_map)


def _clean(offer):
    return {k: v for k, v in offer.items() if not k.startswith("_")}


def _finish_txn(conn, txn_id, status, offer):
    conn.execute("""UPDATE transactions SET status=?, details_json=?,
                     resolved_at=datetime('now') WHERE txn_id=?""",
                 (status, json.dumps(_clean(offer)), txn_id))
    conn.commit()


# --- Waivers ----------------------------------------------------------------

def free_agents(conn, proj_map, top: int = 25) -> list[dict]:
    """Top unrostered players by projection."""
    rows = conn.execute(
        """SELECT p.player_id, p.name, p.position FROM players p
            WHERE NOT EXISTS (SELECT 1 FROM rosters r
                               WHERE r.player_id = p.player_id
                                 AND r.dropped_week IS NULL)""").fetchall()
    fas = [{"player_id": r["player_id"], "name": r["name"],
            "position": r["position"], "proj": proj_map.get(r["player_id"], 0.0)}
           for r in rows]
    fas.sort(key=lambda x: x["proj"], reverse=True)
    return fas[:top]


def wants_waiver(conn, team_id, fa_list, proj_map) -> bool:
    """Haiku gate: does this GM want to make a waiver claim this week?"""
    t = _team(conn, team_id)
    top = "\n".join(f"  - {f['name']} ({f['position']}, proj {f['proj']:.1f})"
                    for f in fa_list[:10])
    system = (f"You are {t['gm_name']}, a fantasy GM. Persona: {t['personality']} "
              "Answer only with JSON.")
    user = (f"Top available free agents:\n{top}\n\nYou have "
            f"${t['faab_remaining']} FAAB left. Do you want to make a waiver "
            'claim this week? Return {"act": true or false}.')
    return llm.gate(system, user)


def decide_waiver(conn, team_id, fa_list, proj_map) -> dict | None:
    """Sonnet: pick one FA to add, one player to drop, and a FAAB bid. Or pass."""
    t = _team(conn, team_id)
    fa_lines, fmap = [], {}
    for i, f in enumerate(fa_list, start=1):
        fmap[str(i)] = f["player_id"]
        fa_lines.append(f"  F{i}. {f['name']} ({f['position']}, proj {f['proj']:.1f})")
    roster_txt, rmap = _menu(rosters.active_roster(conn, team_id), "D", proj_map)
    dmap = {k[1:]: v for k, v in rmap.items()}
    system = (f"You are {t['gm_name']}, GM of \"{t['team_name']}\". Persona: "
              f"{t['personality']} Valuation quirk: {t['valuation_bias']}. "
              f"You have ${t['faab_remaining']} FAAB (blind bidding).")
    user = (
        f"Free agents (F#):\n" + "\n".join(fa_lines) + "\n\n"
        f"Your roster (D#):\n{roster_txt}\n\n"
        "Optionally claim ONE free agent, dropping ONE of your players to make "
        "room, with a blind FAAB bid (0 to your remaining budget). Keep a legal "
        "lineup. Return JSON: "
        '{"pass": false, "add": <F#>, "drop": <D#>, "faab": <int>, '
        '"message": "<why>"} or {"pass": true}.')
    try:
        data = llm.chat_json(system, user, max_tokens=900)
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("pass"):
        return None
    add = fmap.get(str(data.get("add")))
    drop = dmap.get(str(data.get("drop")))
    if not add or not drop:
        return None
    faab = max(0, min(int(data.get("faab", 0) or 0), t["faab_remaining"]))
    if not rosters.legal_after(conn, team_id, add_ids=[add], drop_ids=[drop]):
        return None
    return {"team_id": team_id, "add": add, "drop": drop, "faab": faab,
            "message": str(data.get("message", "")).strip()}


def run_waivers(conn, week: int = None, team_ids=None, use_gate: bool = True,
                proj_map: dict = None) -> list[dict]:
    """Collect one claim per interested team and resolve them by FAAB priority."""
    if proj_map is None:
        from . import projections
        proj_map = {r.entity_id: r.proj_ppg
                    for r in projections.build_projections().itertuples(index=False)}
    week = week or rosters.current_week(conn)
    fa_list = free_agents(conn, proj_map)
    if team_ids is None:
        team_ids = [r["team_id"] for r in conn.execute("SELECT team_id FROM teams")]

    claims = []
    for tid in team_ids:
        if use_gate and not wants_waiver(conn, tid, fa_list, proj_map):
            continue
        claim = decide_waiver(conn, tid, fa_list, proj_map)
        if claim:
            claims.append(claim)

    # Priority: highest bid, then most FAAB remaining, then earliest draft slot.
    meta = {r["team_id"]: r for r in conn.execute("SELECT * FROM teams")}
    claims.sort(key=lambda c: (-c["faab"], -meta[c["team_id"]]["faab_remaining"],
                               meta[c["team_id"]]["draft_slot"]))

    taken, results = set(), []
    for c in claims:
        tid, add, drop, faab = c["team_id"], c["add"], c["drop"], c["faab"]
        team = _team(conn, tid)
        ok = (add not in taken and rosters.owner_of(conn, add) is None
              and rosters.owner_of(conn, drop) == tid
              and faab <= team["faab_remaining"]
              and rosters.legal_after(conn, tid, add_ids=[add], drop_ids=[drop]))
        status = "processed" if ok else "failed"
        if ok:
            conn.execute("UPDATE rosters SET dropped_week=? WHERE player_id=? "
                         "AND team_id=? AND dropped_week IS NULL", (week, drop, tid))
            conn.execute("""INSERT INTO rosters(team_id, player_id, acquired_via,
                              acquired_week) VALUES(?,?, 'waiver', ?)""",
                         (tid, add, week))
            conn.execute("UPDATE teams SET faab_remaining=faab_remaining-? "
                         "WHERE team_id=?", (faab, tid))
            taken.add(add)
        conn.execute(
            """INSERT INTO transactions(type, status, to_team_id, faab_bid,
                 details_json, resolved_at)
               VALUES('waiver_claim', ?, ?, ?, ?, datetime('now'))""",
            (status, tid, faab, json.dumps({"add": add, "drop": drop})))
        nm = _names(conn, [add, drop])
        _log(conn, tid, "waiver",
             f"{team['gm_name']} "
             + (f"wins {nm.get(add, add)} for ${faab} (drops {nm.get(drop, drop)})"
                if ok else f"misses on {nm.get(add, add)} (${faab})"))
        results.append({**c, "status": status})
    conn.commit()
    return results
