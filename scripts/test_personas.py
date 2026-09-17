"""Offline tests for the persona layer -- no API calls.

Stubs the three LLM-touching functions so the collision-negotiation logic and
DB persistence can be verified deterministically. Run:  python -m scripts.test_personas

(The *live* real-API path is exercised separately by scripts/gen_personas.py.)
"""
import sys, os, random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, personas, llm


def _persona(team, gm, **kw):
    base = dict(team_name=team, gm_name=gm, personality=f"{gm} runs it hard.",
                risk_tolerance="balanced", valuation_bias="loves rookies",
                chattiness="moderate", catchphrase="Let's ride.", _raw={})
    base.update(kw)
    return base


def test_extract_json():
    # Trailing commentary after the object is stripped; braces in strings are safe.
    raw = '{"a": 1, "note": "has } brace"} then junk'
    assert llm._extract_first_json_object(raw) == '{"a": 1, "note": "has } brace"}'
    assert llm._extract_first_json_object("no object here") is None
    print("ok: _extract_first_json_object")


def test_norm_and_enum():
    assert personas._norm("The  Gridiron-Gurus!") == "the gridiron gurus"
    assert personas._coerce_enum("Boom Bust", personas.RISK_TOLERANCES, "balanced") == "boom-bust"
    assert personas._coerce_enum("whatever", personas.CHATTINESS, "moderate") == "moderate"
    print("ok: _norm / _coerce_enum")


def test_collision_resolution():
    """Two collisions: one resolved by a voluntary cede, one by coin flip."""
    random.seed(7)  # deterministic coin flip
    people = [
        _persona("Gridiron Gurus", "Sam Rivers"),   # 0  team collides w/ 1
        _persona("gridiron  gurus", "Dana Vance"),   # 1
        _persona("Iron Eagles", "Casey Stone"),      # 2  gm collides w/ 3
        _persona("Steel Hawks", "casey  stone"),     # 3
    ]

    # Stub negotiation: for the team-name group {0,1} nobody cedes -> coin flip.
    # For the gm-name group {2,3} agent 3 cedes -> agent 2 keeps it.
    def fake_msg(persons, idx, group, field, contested, rnd):
        cede = (field == "gm_name" and idx == 3)
        return {"message": f"round {rnd} I want {contested}", "cede": cede}

    # Stub revision: hand back a unique, obviously-new name.
    counter = {"n": 0}
    def fake_revise(persons, idx, field, contested, taken):
        counter["n"] += 1
        return {"new_value": f"New {field} {counter['n']}",
                "message": "so be it."}

    personas._collision_message = fake_msg
    personas._revise_name = fake_revise

    transcript = personas.resolve_collisions(people)

    teams = [personas._norm(p["team_name"]) for p in people]
    gms = [personas._norm(p["gm_name"]) for p in people]
    assert len(set(teams)) == 4, f"team names not unique: {teams}"
    assert len(set(gms)) == 4, f"gm names not unique: {gms}"
    # The team-name group had no ceder -> a coin-flip system message exists.
    assert any(e["event"] == "system" for e in transcript), "expected a coin flip"
    # A cede happened for the gm group -> no coin flip needed there.
    system_msgs = [e for e in transcript if e["event"] == "system"]
    assert len(system_msgs) == 1, f"expected exactly one coin flip, got {len(system_msgs)}"
    print(f"ok: resolve_collisions ({len(transcript)} transcript entries, "
          f"1 coin flip, 1 voluntary cede)")
    return people, transcript


def test_persistence(people, transcript):
    conn = db.init_db(":memory:")
    personas.ensure_league(conn)
    assert conn.execute("SELECT COUNT(*) FROM league").fetchone()[0] == 1
    slots = [1, 2, 3, 4]
    ids = personas.persist_teams(conn, people, slots)
    n = personas.persist_transcript(conn, transcript, ids)

    assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 4
    assert n == len(transcript)
    assert conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0] == len(transcript)
    # Attribution: agent-authored lines carry a real team_id; system lines don't.
    named = conn.execute(
        "SELECT COUNT(*) FROM chat_log WHERE team_id IS NOT NULL").fetchone()[0]
    system = conn.execute(
        "SELECT COUNT(*) FROM chat_log WHERE team_id IS NULL").fetchone()[0]
    assert named + system == len(transcript)
    assert system == sum(1 for e in transcript if e["agent"] is None)
    # persona_json round-trips.
    row = conn.execute("SELECT persona_json FROM teams LIMIT 1").fetchone()
    assert row["persona_json"] is not None
    print(f"ok: persistence (4 teams, {n} chat_log rows, "
          f"{named} attributed / {system} system)")


def _fake_cast(n=8):
    """A mocked single-call response: Dale first, then n-1 varied friends."""
    chatty = ["trash-talker", "quiet", "moderate", "trash-talker", "quiet",
              "moderate", "balanced-bad-value", "loud"]
    risk = ["boom-bust", "safe-floor", "balanced", "Boom Bust", "safe floor",
            "balanced", "whatever", "boom-bust"]
    gms = [{"gm_name": "Dale Ferraro", "team_name": "Back Home Bandits",
            "personality": "Talks nonstop, blows FAAB on impulse.",
            "bio": "Dale is a talker who references back home constantly...",
            "risk_tolerance": risk[0], "valuation_bias": "chases upside",
            "chattiness": chatty[0], "catchphrase": "back home we'd never"}]
    for i in range(1, n):
        gms.append({"gm_name": f"Real Name{i}", "team_name": f"Funny Team {i}",
                    "personality": f"distinct GM {i}", "bio": f"baggage {i}",
                    "risk_tolerance": risk[i % len(risk)],
                    "valuation_bias": "quirk", "chattiness": chatty[i % len(chatty)],
                    "catchphrase": "line"})
    return {"gms": gms}


def test_generate_cast():
    personas.llm.chat_json = lambda *a, **k: _fake_cast(8)
    cast = personas.generate_cast(8)
    assert len(cast) == 8, len(cast)
    # Dale is first and keeps his seed identity.
    assert cast[0]["gm_name"] == "Dale Ferraro" and cast[0]["bio"]
    # Free-text enums are coerced onto the allowed sets ("Boom Bust" -> boom-bust,
    # "whatever"/"loud" -> defaults).
    assert all(p["risk_tolerance"] in personas.RISK_TOLERANCES for p in cast)
    assert all(p["chattiness"] in personas.CHATTINESS for p in cast)
    assert cast[3]["risk_tolerance"] == "boom-bust"      # "Boom Bust" coerced
    # Every persona carries the full structured field set + raw.
    for p in cast:
        assert {"gm_name", "team_name", "personality", "bio", "catchphrase",
                "_raw"} <= set(p)
    # The prompt actually embeds the seed and the naming rules we depend on.
    assert "back home" in personas._CAST_SYSTEM.lower()
    assert "DALE FIRST" in personas._CAST_SYSTEM
    print("ok: generate_cast (single call -> 8 normalized GMs, Dale first)")


def test_generate_cast_short_response_raises():
    personas.llm.chat_json = lambda *a, **k: _fake_cast(5)   # only 5 GMs
    try:
        personas.generate_cast(8)
    except RuntimeError as e:
        assert "expected 8" in str(e), e
        print("ok: generate_cast raises when the model returns too few GMs")
        return
    raise AssertionError("expected RuntimeError on a short cast")


def main():
    test_extract_json()
    test_norm_and_enum()
    test_generate_cast()
    test_generate_cast_short_response_raises()
    people, transcript = test_collision_resolution()
    test_persistence(people, transcript)
    print("\nALL OFFLINE PERSONA TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
