"""`arena` command line."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from arena.calibration import CannotResolve, resolve, role_scores, role_weights
from arena.council import Citation, Opinion, Verdict
from arena.decide import decide
from arena.ledger import Ledger
from arena.llm import FakeLLM
from arena.receipt import Receipt, render_markdown
from arena.replay import replay
from arena.risk import RiskLimits
from arena.ryo_client import RecordedRyoClient, RyoClient, RyoError, record
from arena.skills import definitions as skill_definitions, invoke as skill_invoke
from arena.skills.narrative import narrative_convergence
from arena.skills.news import news_verify

load_dotenv()
app = typer.Typer(help="RYO Arena: council-of-agents decisions on RYO's read-only research tools.", no_args_is_help=True)

RECORDED_ROOT = Path("fixtures/recorded")
FIXTURE_ROOT = Path("tests/fixtures")


def _ledger() -> Ledger:
    return Ledger(os.environ.get("ARENA_DB", "arena.db"))


def _source(kind: str):
    if kind == "live":
        if not os.environ.get("RYO_MCP_KEY"):
            raise typer.BadParameter("RYO_MCP_KEY is not set; use --source recorded or set the key in .env")
        return RyoClient()
    if kind == "recorded":
        return RecordedRyoClient(RECORDED_ROOT, name="recorded")
    if kind == "fixture":
        return RecordedRyoClient(FIXTURE_ROOT, name="fixture")
    raise typer.BadParameter("source must be live, recorded or fixture")


def _llm(kind: str):
    if kind == "fake":
        # Smoke-test stand-in. Its output is labelled model=fake on every receipt.
        def op(role):
            return lambda _u: Opinion(role=role, stance="neutral", p_up_7d=0.5, confidence="low",
                                      thesis="FAKE LLM placeholder for smoke tests; not an analysis.",
                                      citations=[Citation(path="deep_analysis.data.technicals.rsi_14", value="?")], invalidation="n/a")
        return FakeLLM({"macro": op("macro"), "technician": op("technician"), "narrative": op("narrative"),
                        "judge": lambda _u: Verdict(action="no_trade", p_up_7d=0.5, rationale="FAKE LLM placeholder.")})
    if kind == "openai":
        from arena.llm import OpenAICompatLLM

        return OpenAICompatLLM()
    from arena.llm import AnthropicLLM

    return AnthropicLLM()


@app.command()
def health(source: str = "live"):
    """Check RYO MCP health (no key needed) and, when a key is set, whoami/quota."""
    client = RyoClient()
    typer.echo(json.dumps(client.health(), indent=1))
    if client.key:
        try:
            typer.echo(json.dumps(client.whoami(), indent=1))
        except RyoError as exc:
            typer.echo(f"whoami failed: {exc}")


@app.command("decide")
def decide_cmd(
    symbol: str = typer.Argument(..., help="Token symbol, e.g. SOL"),
    source: str = typer.Option("live", help="live | recorded | fixture"),
    llm: str = typer.Option(os.environ.get("ARENA_LLM", "anthropic"), help="anthropic | openai | fake (env ARENA_LLM)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the LLM output cache"),
    voices: str = typer.Option("", help="Comma list like tg:WatcherGuru,x:handle -> adds narrative_signal (env ARENA_VOICES)"),
    news: bool = typer.Option(False, help="Add news_check via news_verify (needs TAVILY_API_KEY)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Gather evidence, run the council, size a practice trade, store the receipt."""
    src = _source(source)
    extras = {}
    voice_list = [v for v in (voices or os.environ.get("ARENA_VOICES", "")).split(",") if v.strip()]
    if voice_list:
        extras["narrative_signal"] = lambda sym: narrative_convergence(voice_list, tokens=[sym], hours=24)
    if news:
        extras["news_check"] = lambda sym: news_verify(f"{sym} crypto news this week", symbol=sym, ryo=src)
    receipt = decide(symbol, src, _llm(llm), _ledger(), RiskLimits(), use_cache=not no_cache, extras=extras or None)
    typer.echo(receipt.model_dump_json(indent=1) if as_json else render_markdown(receipt))



@app.command("replay")
def replay_cmd(decision_id: str, fresh: bool = typer.Option(False, help="Call the model again instead of using cached outputs"),
               llm: str = typer.Option(os.environ.get("ARENA_LLM", "anthropic"), help="anthropic | openai | fake (env ARENA_LLM)")):
    """Rebuild a receipt from stored evidence and report whether it is identical."""
    res = replay(decision_id, _ledger(), _llm(llm), RiskLimits(), fresh=fresh)
    typer.echo(f"identical: {res.identical}  (fresh={res.fresh})")
    for d in res.diff:
        typer.echo(f"  {d}")



@app.command("resolve")
def resolve_cmd(decision_id: str = typer.Argument(None), all_: bool = typer.Option(False, "--all"), source: str = "live"):
    """Score decisions against a fresh analyze_token price read."""
    led = _ledger()
    ids = led.unresolved() if all_ else ([decision_id] if decision_id else [])
    if not ids:
        typer.echo("nothing to resolve")
        raise typer.Exit()
    src = _source(source)
    for i in ids:
        try:
            out = resolve(i, led, src)
            typer.echo(f"{i} {out.symbol}: {out.return_pct:+.2f}% went_up={out.went_up} horizon_reached={out.horizon_reached} brier={out.brier}")
        except CannotResolve as exc:
            typer.echo(f"{i}: cannot resolve ({exc})")



@app.command()
def scores():
    """Per-agent Brier scores and the weights the judge currently uses."""
    led = _ledger()
    s = role_scores(led)
    typer.echo(json.dumps({"scores": s, "weights": role_weights(s)}, indent=1))


@app.command()
def show(decision_id: str, as_json: bool = typer.Option(False, "--json")):
    """Print a stored receipt."""
    raw = _ledger().get_decision(decision_id)
    if raw is None:
        raise typer.BadParameter(f"no decision {decision_id}")
    typer.echo(raw if as_json else render_markdown(Receipt.model_validate_json(raw)))


@app.command("list")
def list_cmd(limit: int = 20):
    for d in _ledger().list_decisions(limit):
        typer.echo(f"{d['id']}  {d['created_at']}  {d['symbol']:6}  {d['model']}")


@app.command("record")
def record_cmd(symbol: str):
    """Capture live RYO responses for SYMBOL into fixtures/recorded (needs RYO_MCP_KEY)."""
    client = _source("live")
    for tool, args in [("market_overview", {}), ("monitor_market_sentiment_shift", {}),
                       ("deep_analysis", {"symbol": symbol.upper(), "include_perp": True}), ("analyze_token", {"symbol": symbol.upper()})]:
        try:
            typer.echo(f"recorded {record(client, tool, args, RECORDED_ROOT)}")
        except RyoError as exc:
            typer.echo(f"{tool}: {exc}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000):
    """Serve the read-only API and the diff-first dashboard (Track 2)."""
    import uvicorn

    uvicorn.run("arena.api:app", host=host, port=port)


skill_app = typer.Typer(help="Track 3 skills: RYO-shaped research tools RYO does not have yet.", no_args_is_help=True)
app.add_typer(skill_app, name="skill")


@skill_app.command("spec")
def skill_spec():
    """Print the skill definitions (RYO SkillDefinition shape)."""
    typer.echo(json.dumps([d.model_dump() for d in skill_definitions()], indent=1))


@skill_app.command("run")
def skill_run(name: str, args_json: str = typer.Argument("{}", help="JSON object of arguments"), source: str = typer.Option("live", help="RYO source for market context")):
    """Invoke a skill and print its envelope."""
    args = json.loads(args_json)
    deps = {}
    if name == "news_verify" and args.get("symbol"):
        try:
            deps["ryo"] = _source(source)
        except typer.BadParameter:
            pass  # skill reports the missing market context itself
    typer.echo(skill_invoke(name, args, **deps).model_dump_json(indent=1))


if __name__ == "__main__":
    app()
