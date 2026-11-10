

def test_remote_state_refusal_requires_a_demo_only_deploy():
    from loadtests.lib import remote_state_refusal as why
    ok = {"mode": "demo", "arb_mode": "demo", "live_allowed": False, "arming": {"maker": False, "arbitrage": False}}
    assert why(ok) is None
    assert why({**ok, "mode": "live"}) and "demo mode" in why({**ok, "mode": "live"})
    assert "arbitrage" in why({**ok, "arb_mode": "live"})
    assert "arbitrage" in why({"mode": "demo", "arb": {"arb_mode": "live"}})
    assert "armed" in why({**ok, "arming": {"maker": False, "arbitrage": True}})
    assert "live-capable" in why({**ok, "live_allowed": True})
    assert why({"mode": "demo"}) is None            # an older status without the extra fields stays allowed
    assert why(None)
