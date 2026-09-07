"""`nota` command line."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from dotenv import load_dotenv

import time

from nota.calibration import CannotResolve, close_position, due, resolve, role_scores, role_weights
from nota.decide import decide
from nota import paths
from nota.evidence import SECTIONS, candidate_symbols, first_present, ryo_args
from nota.ledger import Ledger, now_iso
from nota.notify import notify_receipt
from nota.receipt import Receipt, render_markdown
from nota.replay import replay
from nota.risk import RiskLimits, atr_usd as risk_atr_usd
from nota.ryo_client import RecordedRyoClient, RyoClient, RyoError, record
from nota.skills import definitions as skill_definitions, invoke as skill_invoke
from nota.skills.narrative import narrative_convergence
from nota.skills.news import news_verify
from nota.skills.price_check import price_crosscheck
from nota.skills.technicals import technicals_crosscheck

load_dotenv()
app = typer.Typer(help="Nota: council-of-agents decisions on RYO's read-only research tools.", no_args_is_help=True)

RECORDED_ROOT = Path("fixtures/recorded")
FIXTURE_ROOT = Path("tests/fixtures")


def _ledger() -> Ledger:
    return Ledger(os.environ.get("NOTA_DB", "nota.db"))


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


KEYLESS = ("Without one you can still run `nota health`, the four skills (`nota skill run ...`), "
           "`nota serve`, `nota positions`, and `nota replay <id>` - a cached replay reads the "
           "ledger only, so verifying a receipt needs no key at all.")


def _llm(kind: str):
    """Build the configured LLM, saying which key is missing instead of letting the SDK raise."""
    if kind == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise typer.BadParameter(f"OPENAI_API_KEY is not set, so the council cannot run. {KEYLESS}")
        from nota.llm import OpenAICompatLLM

        return OpenAICompatLLM()
    if kind == "anthropic":
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise typer.BadParameter(
                f"ANTHROPIC_API_KEY is not set, so the council cannot run. Set it, or point "
                f"NOTA_LLM=openai at any OpenAI-compatible provider. {KEYLESS}")
        from nota.llm import AnthropicLLM

        return AnthropicLLM()
    raise typer.BadParameter("llm must be anthropic or openai (env NOTA_LLM)")


LLM_HELP = "anthropic | openai (env NOTA_LLM)"


@app.command()
def health():
    """Check RYO MCP health (no key needed) and, when a key is set, whoami/quota."""
    client = RyoClient()
    typer.echo(json.dumps(client.health(), indent=1))
    typer.echo(f"transport: {client.transport} (env RYO_TRANSPORT)")
    typer.echo(f"key_set: {bool(client.key)} (env RYO_MCP_KEY)")
    if not client.key:
        typer.echo("no builder key: live RYO evidence is unavailable; --source fixture|recorded, "
                   "the skills and the dashboard still run")
    if client.key:
        try:
            typer.echo(json.dumps(client.whoami(), indent=1))
            tools = client.tools_mcp()
            typer.echo("MCP tools/list: " + ", ".join(t.get("name", "?") for t in tools))
            if client.last_rate_limit:
                typer.echo(f"rate limit headers: {client.last_rate_limit}")
        except RyoError as exc:
            typer.echo(f"authenticated probe failed: {exc}")


@app.command("decide")
def decide_cmd(
    symbol: str = typer.Argument(..., help="Token symbol, e.g. SOL"),
    source: str = typer.Option("live", help="live | recorded | fixture"),
    llm: str = typer.Option(os.environ.get("NOTA_LLM", "anthropic"), help=LLM_HELP),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the LLM output cache"),
    voices: str = typer.Option("", help="Comma list like tg:WatcherGuru,x:handle -> adds narrative_signal (env NOTA_VOICES)"),
    news: bool = typer.Option(False, help="Add news_check via news_verify (Tavily or Venice web search)"),
    notify: bool = typer.Option(False, help="Post the receipt to configured Telegram/Discord channels"),
    price_check: bool = typer.Option(True, help="Add price_check: keyless CoinGecko/Coinbase/Kraken spot prices vs RYO's price"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Gather evidence, run the council, size a practice trade, store the receipt."""
    src = _source(source)
    receipt = decide(symbol, src, _llm(llm), _ledger(), RiskLimits(), use_cache=not no_cache, extras=_extras(src, voices, news, price_check))
    typer.echo(receipt.model_dump_json(indent=1) if as_json else render_markdown(receipt))
    if notify:
        for out in notify_receipt(receipt):
            typer.echo(f"notify {out['channel']}: {out['status']}" + (f" ({out['error']})" if out.get("error") else ""))


def _extras(src, voices: str, news: bool, price_check: bool = True):
    extras = {}
    voice_list = [v for v in (voices or os.environ.get("NOTA_VOICES", "")).split(",") if v.strip()]
    if voice_list:
        extras["narrative_signal"] = lambda sym, _pack: narrative_convergence(voice_list, tokens=[sym], hours=24)
    if news:
        extras["news_check"] = lambda sym, _pack: news_verify(f"{sym} crypto news this week", symbol=sym, ryo=src)
    if price_check:
        def _check(sym, pack):
            path, price = first_present(pack, paths.PRICE_USD)
            fg = pack.get("market_overview.data.fear_greed.value")
            return price_crosscheck(sym, reference_price=price, reference_path=path,
                                    reference_fear_greed=float(fg) if isinstance(fg, (int, float)) and not isinstance(fg, bool) else None)
        extras["price_check"] = _check

        def _tech(sym, pack):
            _, ref_price = first_present(pack, paths.PRICE_USD)
            _, rsi_ref = first_present(pack, paths.RSI_14)
            _, atr_ref = risk_atr_usd(pack, ref_price) if ref_price else (None, None)
            return technicals_crosscheck(sym, reference_rsi_14=rsi_ref, reference_atr_14=atr_ref)
        extras["technicals_check"] = _tech
    return extras or None


@app.command()
def scan(
    top_n: int = typer.Option(5, help="Candidates to take from scan_market"),
    chain: str = typer.Option("", help="Optional chain filter, e.g. bsc"),
    theme: str = typer.Option("", help="Optional theme context, e.g. news"),
    source: str = typer.Option("live", help="live | recorded | fixture"),
    decide_top: int = typer.Option(0, help="Run the council on the first N candidates"),
    llm: str = typer.Option(os.environ.get("NOTA_LLM", "anthropic"), help=LLM_HELP),
):
    """RYO's research funnel: scan_market -> analyze_token on each candidate -> optional council decisions."""
    src = _source(source)
    args = {k: v for k, v in {"chain": chain, "theme": theme, "top_n": top_n}.items() if v}
    env = src.call("scan_market", args)
    typer.echo(f"scan_market: {env.status}, data_mode {env.data_mode}, as_of {env.as_of} | {env.summary.headline}")
    for w in env.warnings:
        typer.echo(f"  warning: {w}")
    candidates = candidate_symbols(env.data)[:top_n]
    if not candidates:
        typer.echo("no candidate symbols found in scan_market data")
        raise typer.Exit(1)
    for sym in candidates:
        try:
            a = src.call("analyze_token", {"symbol": sym})
            typer.echo(f"{sym:8} {a.status:12} {a.summary.headline}")
        except RyoError as exc:
            typer.echo(f"{sym:8} {'error':12} {exc.code}: {exc.message}")
    for sym in candidates[:decide_top]:
        r = decide(sym, src, _llm(llm), _ledger(), RiskLimits())
        typer.echo(f"decided {sym}: {r.headline} (receipt {r.id})")


@app.command()
def watch(
    symbols: str = typer.Argument("", help="Comma list, e.g. SOL,BTC (may be empty with --scan-top)"),
    scan_top: int = typer.Option(0, help="Each cycle, also take the top N candidates from scan_market"),
    every: int = typer.Option(3600, help="Seconds between cycles"),
    cycles: int = typer.Option(0, help="Stop after N cycles (0 = run until interrupted)"),
    source: str = typer.Option("live", help="live | recorded | fixture"),
    llm: str = typer.Option(os.environ.get("NOTA_LLM", "anthropic"), help=LLM_HELP),
    voices: str = typer.Option("", help="Telegram/X voices for narrative_signal"),
    news: bool = typer.Option(False, help="Add news_check"),
    notify: bool = typer.Option(False, help="Post each new receipt to Telegram/Discord"),
    price_check: bool = typer.Option(True, help="Add price_check section"),
    close_on_stop: bool = typer.Option(False, help="Exit practice positions whose stop or target the latest independent price has crossed"),
):
    """Autonomous loop: decide every symbol each cycle, exit practice positions at stop/target, score matured decisions, publish receipts."""
    fixed = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not fixed and not scan_top:
        raise typer.BadParameter("give symbols, --scan-top N, or both")
    n = 0
    while True:
        n += 1
        src, led = _source(source), _ledger()
        syms = list(fixed)
        if scan_top:
            try:
                env = src.call("scan_market", {"top_n": scan_top})
                picked = [s for s in candidate_symbols(env.data)[:scan_top] if s not in syms]
                typer.echo(f"[{now_iso()}] scan_market {env.status}: {', '.join(picked) or 'no new candidates'}")
                syms += picked
            except RyoError as exc:
                typer.echo(f"[{now_iso()}] scan_market failed {exc.code}: {exc.message}; using fixed symbols only")
        for sym in syms:
            try:
                r = decide(sym, src, _llm(llm), led, RiskLimits(), extras=_extras(src, voices, news, price_check))
                typer.echo(f"[{now_iso()}] {sym}: {r.headline} (receipt {r.id}, cache hits {r.cache_hits})")
                if notify and r.cache_hits < 4:  # a fully cached receipt is a repeat, not news
                    for out in notify_receipt(r):
                        typer.echo(f"  notify {out['channel']}: {out['status']}")
            except Exception as exc:  # one symbol failing must not stop the loop
                typer.echo(f"[{now_iso()}] {sym}: failed {type(exc).__name__}: {exc}")
        if close_on_stop:
            from nota.api import positions as open_positions

            for p in open_positions():
                if p["status"] in ("stopped", "target") and p["latest_price"] is not None:
                    out = close_position(p["decision_id"], led, float(p["latest_price"]), str(p["latest_as_of"]), p["status"])
                    typer.echo(f"[{now_iso()}] closed {p['symbol']} {p['side']} ({p['status']}) at {out.price_now:g}: {out.trade_result_usd:+.2f} USD practice result")
        for i in due(led):
            try:
                out = resolve(i, led, src)
                typer.echo(f"[{now_iso()}] resolved {i} {out.symbol}: {out.return_pct:+.2f}% brier={out.brier}")
            except CannotResolve as exc:
                typer.echo(f"[{now_iso()}] {i}: cannot resolve ({exc})")
        if cycles and n >= cycles:
            break
        time.sleep(every)



@app.command("replay")
def replay_cmd(decision_id: str, fresh: bool = typer.Option(False, help="Call the model again instead of using cached outputs"),
               llm: str = typer.Option(os.environ.get("NOTA_LLM", "anthropic"), help=LLM_HELP)):
    """Rebuild a receipt from stored evidence and report whether it is identical."""
    try:  # a cached replay reads the ledger only, so it needs no key and no LLM at all
        res = replay(decision_id, _ledger(), _llm(llm) if fresh else None, RiskLimits(), fresh=fresh)
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1)
    typer.echo(f"identical: {res.identical}  (fresh={res.fresh})")
    for d in res.diff:
        typer.echo(f"  {d}")



@app.command("resolve")
def resolve_cmd(decision_id: str = typer.Argument(None), all_: bool = typer.Option(False, "--all", help="Every decision whose 7-day horizon has passed"),
                early: bool = typer.Option(False, help="With --all: also score decisions before their horizon (final, not re-scored)"), source: str = "live"):
    """Score decisions against a fresh analyze_token price read."""
    led = _ledger()
    ids = (led.unresolved() if early else due(led)) if all_ else ([decision_id] if decision_id else [])
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


@app.command("positions")
def positions_cmd(as_json: bool = typer.Option(False, "--json")):
    """Open practice positions against the latest independent price: open, past stop, or at target."""
    from nota.api import positions as open_positions

    rows = open_positions()
    if as_json:
        typer.echo(json.dumps(rows, indent=1))
        return
    if not rows:
        typer.echo("no open practice positions")
    for p in rows:
        typer.echo(f"{p['symbol']:6} {p['side']:5} entry {p['entry']:g} stop {p['stop']:g} target {p['target']:g} | latest {p['latest_price']} "
                   f"({p['latest_as_of']}) | {p['status']} | {p['pnl_usd']} USD | receipt {p['decision_id']}")


@app.command("list")
def list_cmd(limit: int = 20):
    """Newest receipts in the ledger: id, timestamp, symbol, model."""
    for d in _ledger().list_decisions(limit):
        typer.echo(f"{d['id']}  {d['created_at']}  {d['symbol']:6}  {d['model']}")


@app.command("record")
def record_cmd(symbol: str):
    """Capture live RYO responses for SYMBOL (all six tools) into fixtures/recorded (needs RYO_MCP_KEY)."""
    client = _source("live")
    calls = [(SECTIONS[key], args) for key, args in ryo_args(symbol).items()] + [("scan_market", {})]
    for tool, args in calls:
        try:
            typer.echo(f"recorded {record(client, tool, args, RECORDED_ROOT)}")
        except RyoError as exc:
            typer.echo(f"{tool}: {exc}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000):
    """Serve the read-only API and the diff-first dashboard (Track 2)."""
    import uvicorn

    uvicorn.run("nota.api:app", host=host, port=port)


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
