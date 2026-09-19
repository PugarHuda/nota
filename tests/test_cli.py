"""End-to-end CLI checks on recorded RYO answers with the council stubbed (no network, no spend)."""

import json

from typer.testing import CliRunner

from nota import cli
from tests.test_decide_replay import FIXTURES, make_llm

runner = CliRunner()


def _env(tmp_path, monkeypatch, action="long"):
    monkeypatch.setenv("NOTA_DB", str(tmp_path / "cli.db"))
    monkeypatch.delenv("RYO_MCP_KEY", raising=False)
    monkeypatch.setattr(cli, "_llm", lambda kind: make_llm(action=action))
    monkeypatch.setattr(cli, "RECORDED_ROOT", FIXTURES)  # the recording the tests pin, not the one `nota record` refreshes


def test_decide_replay_show_list_scores(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(cli.app, ["decide", "SOL", "--source", "recorded", "--no-price-check", "--json"])
    assert res.exit_code == 0, res.output
    receipt = json.loads(res.output)
    assert receipt["symbol"] == "SOL" and receipt["trade"]["kind"] == "trade" and "compare" in receipt["availability"]
    assert "identical: True" in runner.invoke(cli.app, ["replay", receipt["id"]]).output
    assert "Decision receipt" in runner.invoke(cli.app, ["show", receipt["id"]]).output
    assert receipt["id"] in runner.invoke(cli.app, ["list"]).output
    assert '"weights"' in runner.invoke(cli.app, ["scores"]).output
    assert "nothing to resolve" in runner.invoke(cli.app, ["resolve", "--all", "--source", "recorded"]).output  # not due yet


def test_live_source_without_key_fails_clearly(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(cli.app, ["decide", "SOL", "--no-price-check"])
    assert res.exit_code != 0 and "RYO_MCP_KEY is not set" in res.output


def test_unknown_llm_kind_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTA_DB", str(tmp_path / "cli.db"))
    res = runner.invoke(cli.app, ["decide", "SOL", "--source", "recorded", "--llm", "fake", "--no-price-check"])
    assert res.exit_code != 0 and "anthropic or openai" in res.output


def test_scan_funnel_on_fixture(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(cli.app, ["scan", "--source", "recorded", "--top-n", "2", "--decide-top", "1"])
    assert res.exit_code == 0, res.output
    from nota.evidence import candidate_symbols

    scan = json.loads((FIXTURES / "scan_market" / "default.json").read_text(encoding="utf-8"))
    first, second = candidate_symbols(scan["data"])[:2]  # RYO's real shortlist; neither has a recorded analysis
    assert f"scan_market: {scan['status']}" in res.output
    assert f"{first:8} error        NO_FIXTURE" in res.output and f"{second:8} error        NO_FIXTURE" in res.output
    assert f"decided {first}: {first}: no trade (primary evidence (deep_analysis) unavailable" in res.output


def test_watch_one_cycle_survives_failures(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(cli.app, ["watch", "SOL,NOPE", "--source", "recorded", "--cycles", "1", "--every", "0", "--no-price-check", "--notify"])
    assert res.exit_code == 0, res.output
    assert "SOL: SOL: LONG practice trade" in res.output and "notify telegram: not_configured" in res.output
    assert "NOPE:" in res.output and ("no trade" in res.output or "failed" in res.output)


def test_skill_spec_lists_the_read_only_skills():
    res = runner.invoke(cli.app, ["skill", "spec"])
    names = {d["name"] for d in json.loads(res.output)}
    assert names == {"narrative_convergence", "news_verify", "price_crosscheck", "technicals_crosscheck", "positioning_check", "move_base_rate", "verdict_track_record"}


def test_decide_without_an_llm_key_says_so_instead_of_raising_from_the_sdk(monkeypatch):
    """A judge's first command must not be a stack trace: name the missing key and what still works."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    res = runner.invoke(cli.app, ["decide", "SOL", "--source", "recorded", "--llm", "anthropic"])
    assert res.exit_code != 0
    assert "ANTHROPIC_API_KEY is not set" in res.output and "no key at all" in res.output


def test_once_a_day_skips_a_symbol_already_decided_today(monkeypatch, tmp_path):
    from nota.ledger import Ledger

    db = tmp_path / "l.db"
    Ledger(str(db)).save_decision("abc", "h", "SOL", "m", "{}")
    monkeypatch.setenv("NOTA_DB", str(db))
    res = runner.invoke(cli.app, ["decide", "sol", "--once-a-day", "--source", "recorded"])
    assert res.exit_code == 0 and "already decided today; skipped" in res.output


def test_bad_input_is_a_usage_error_not_a_traceback(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    for argv in (["replay", "nope"], ["resolve", "nope", "--source", "recorded"], ["skill", "run", "nope"],
                 ["skill", "run", "price_crosscheck", "{bad"], ["skill", "run", "price_crosscheck", "[1]"],
                 ["skill", "run", "price_crosscheck", '{"symbol": "../x"}'], ["serve", "--port", "99999"],
                 ["decide", "", "--source", "recorded"], ["decide", "BTC?x", "--source", "recorded"], ["watch", "SOL,../x"]):
        res = runner.invoke(cli.app, argv)
        assert res.exit_code == 2 and "Traceback" not in res.output, (argv, res.output)


def test_record_writes_nothing_unless_every_tool_answered(tmp_path, monkeypatch):
    from nota.ryo_client import RecordedRyoClient, RyoError
    from tests.test_decide_replay import FIXTURES

    monkeypatch.chdir(tmp_path)  # RECORDED_ROOT is relative: the repo's committed fixtures are never touched
    assert runner.invoke(cli.app, ["record", ""]).exit_code == 2 and not (tmp_path / "fixtures").exists()

    class Flaky(RecordedRyoClient):
        def call(self, tool, args=None):
            if tool == "scan_market":
                raise RyoError(503, "UNAVAILABLE", "down")
            return super().call(tool, args)

    monkeypatch.setattr(cli, "_source", lambda kind: Flaky(FIXTURES))
    res = runner.invoke(cli.app, ["record", "sol"])
    assert res.exit_code == 1 and "left unchanged" in res.output and not (tmp_path / "fixtures").exists()
    monkeypatch.setattr(cli, "_source", lambda kind: RecordedRyoClient(FIXTURES))
    res = runner.invoke(cli.app, ["record", "sol"])
    assert res.exit_code == 0, res.output
    assert len(list((tmp_path / "fixtures" / "recorded").rglob("*.json"))) == 6


def test_health_strict_checks_expiry_and_every_argument_against_the_live_catalog(monkeypatch):
    from datetime import datetime, timedelta, timezone

    catalog = json.loads((FIXTURES / "ryo_catalog.json").read_text(encoding="utf-8"))
    far = (datetime.now(timezone.utc) + timedelta(days=85)).isoformat()
    who = {"key_id": "k", "expires_at": far}
    monkeypatch.setenv("RYO_MCP_KEY", "ryo_mcp_test")
    monkeypatch.setattr(cli.RyoClient, "health", lambda self: {"status": "ok"})
    monkeypatch.setattr(cli.RyoClient, "whoami", lambda self: who)
    monkeypatch.setattr(cli.RyoClient, "initialize", lambda self: {"serverInfo": {"name": "ryo-chan"}})
    monkeypatch.setattr(cli.RyoClient, "tools_mcp", lambda self: catalog)
    monkeypatch.setattr(cli.RyoClient, "tools", lambda self: catalog)
    res = runner.invoke(cli.app, ["health", "--strict"])
    assert res.exit_code == 0, res.output
    assert "published but unused by Nota: monitor_market_sentiment_shift(time_window)" in res.output
    assert "scan_market(" not in res.output  # filter_direction is sent now (`scan --direction`)

    who["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    res = runner.invoke(cli.app, ["health", "--strict"])
    assert res.exit_code == 1 and "problem: key expires in" in res.output
    assert runner.invoke(cli.app, ["health"]).exit_code == 0  # without --strict it only reports

    who["expires_at"] = far
    monkeypatch.setattr(cli, "ryo_args", lambda s: {"compare": {"symbols": "SOL, BTC", "intent": "weekly"}})
    res = runner.invoke(cli.app, ["health", "--strict"])
    assert res.exit_code == 1 and "intent='weekly' is not one of" in res.output

    def refused(self):
        raise cli.RyoError(401, "UNAUTHENTICATED", "Invalid or expired credential.")
    monkeypatch.setattr(cli.RyoClient, "whoami", refused)
    assert runner.invoke(cli.app, ["health", "--strict"]).exit_code == 1


def test_fixture_is_no_longer_a_source():
    res = runner.invoke(cli.app, ["decide", "SOL", "--source", "fixture"])
    assert res.exit_code == 2 and "live or recorded" in res.output
