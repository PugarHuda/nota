"""End-to-end CLI checks on the fixture source with the council stubbed (no network, no spend)."""

import json

from typer.testing import CliRunner

from arena import cli
from tests.test_decide_replay import make_llm

runner = CliRunner()


def _env(tmp_path, monkeypatch, action="long"):
    monkeypatch.setenv("ARENA_DB", str(tmp_path / "cli.db"))
    monkeypatch.delenv("RYO_MCP_KEY", raising=False)
    monkeypatch.setattr(cli, "_llm", lambda kind: make_llm(action=action))


def test_decide_replay_show_list_scores(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(cli.app, ["decide", "SOL", "--source", "fixture", "--no-price-check", "--json"])
    assert res.exit_code == 0, res.output
    receipt = json.loads(res.output)
    assert receipt["symbol"] == "SOL" and receipt["trade"]["kind"] == "trade" and "compare" in receipt["availability"]
    assert "identical: True" in runner.invoke(cli.app, ["replay", receipt["id"]]).output
    assert "Decision receipt" in runner.invoke(cli.app, ["show", receipt["id"]]).output
    assert receipt["id"] in runner.invoke(cli.app, ["list"]).output
    assert '"weights"' in runner.invoke(cli.app, ["scores"]).output
    assert "nothing to resolve" in runner.invoke(cli.app, ["resolve", "--all", "--source", "fixture"]).output  # not due yet


def test_live_source_without_key_fails_clearly(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(cli.app, ["decide", "SOL", "--no-price-check"])
    assert res.exit_code != 0 and "RYO_MCP_KEY is not set" in res.output


def test_unknown_llm_kind_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_DB", str(tmp_path / "cli.db"))
    res = runner.invoke(cli.app, ["decide", "SOL", "--source", "fixture", "--llm", "fake", "--no-price-check"])
    assert res.exit_code != 0 and "anthropic or openai" in res.output


def test_scan_funnel_on_fixture(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(cli.app, ["scan", "--source", "fixture", "--top-n", "2", "--decide-top", "1"])
    assert res.exit_code == 0, res.output
    assert "scan_market: ok" in res.output and "SOL      ok" in res.output and "AVAX     error" in res.output
    assert "decided SOL:" in res.output


def test_watch_one_cycle_survives_failures(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(cli.app, ["watch", "SOL,NOPE", "--source", "fixture", "--cycles", "1", "--every", "0", "--no-price-check", "--notify"])
    assert res.exit_code == 0, res.output
    assert "SOL: SOL: LONG practice trade" in res.output and "notify telegram: not_configured" in res.output
    assert "NOPE:" in res.output and ("no trade" in res.output or "failed" in res.output)


def test_skill_spec_lists_three_read_only_skills():
    res = runner.invoke(cli.app, ["skill", "spec"])
    names = {d["name"] for d in json.loads(res.output)}
    assert names == {"narrative_convergence", "news_verify", "price_crosscheck"}
