"""`nota` command line."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv

import time
from datetime import datetime, timedelta, timezone

from nota.calibration import CannotResolve, close_position, due, fill_base_rates, repair_outcomes, resolve, role_scores, role_weights
from nota.decide import decide
from nota import paths
from nota.evidence import SECTIONS, candidate_symbols, first_present, ryo_args
from nota.ledger import Ledger, now_iso
from nota.notify import notify_receipt
from nota.receipt import Receipt, render_markdown
from nota.replay import replay
from nota.risk import RiskLimits, atr_usd as risk_atr_usd
from nota.ryo_client import RecordedRyoClient, RyoClient, RyoError, check_args, record
from nota.skills import definitions as skill_definitions, invoke as skill_invoke
from nota.skills.contract import OK, clean_symbol
from nota.skills.crowd_odds import crowd_odds
from nota.skills.liquidity import liquidity_check
from nota.skills.narrative import narrative_convergence
from nota.skills.news import news_verify
from nota.skills.positioning import positioning_check
from nota.skills.price_check import price_crosscheck
from nota.skills.technicals import technicals_crosscheck

load_dotenv()
app = typer.Typer(help="Nota: council-of-agents decisions on RYO's read-only research tools.", no_args_is_help=True)

RECORDED_ROOT = Path("fixtures/recorded")


def _sym(raw: str) -> str:
    """A symbol goes into RYO calls, exchange URLs, prompts and fixture paths: refuse a bad one as a usage error."""
    try:
        return clean_symbol(raw)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def _ledger() -> Ledger:
    return Ledger(os.environ.get("NOTA_DB", "nota.db"))


def _source(kind: str):
    if kind == "live":
        if not os.environ.get("RYO_MCP_KEY"):
            raise typer.BadParameter("RYO_MCP_KEY is not set; use --source recorded or set the key in .env")
        return RyoClient()
    if kind == "recorded":
        return RecordedRyoClient(RECORDED_ROOT, name="recorded")
    raise typer.BadParameter("source must be live or recorded")


KEYLESS = ("Without one you can still run `nota health`, the nine skills (`nota skill run ...`), "
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


def _step_summary(title: str, header: list[str], rows: list[list[Any]]) -> None:
    """Append a Markdown table to the GitHub Actions run page when running there (GITHUB_STEP_SUMMARY),
    so a day's locks, settlements and decisions read at a glance instead of from a scrolled log."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    def cell(v: Any) -> str:
        return "-" if v is None or v == "" else str(v).replace("|", "\\|").replace("\n", " ")

    lines = [f"### {title}", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows] or ["| nothing |" + " |" * (len(header) - 1)]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


KEY_WARN_DAYS = 14


@app.command()
def health(strict: bool = typer.Option(False, "--strict", help="Exit 1 when whoami fails, the key expires within "
                                       f"{KEY_WARN_DAYS} days, or an argument Nota sends is not in RYO's live catalog")):
    """Check RYO MCP health (no key needed) and, when a key is set, whoami, key expiry, the MCP handshake
    and every argument Nota sends against RYO's live tool catalog. None of these spends tool-call quota."""
    client = RyoClient()
    typer.echo(json.dumps(client.health(), indent=1))
    typer.echo(f"transport: {client.transport} (env RYO_TRANSPORT)")
    typer.echo(f"key_set: {bool(client.key)} (env RYO_MCP_KEY)")
    if not client.key:
        typer.echo("no builder key: live RYO evidence is unavailable; --source recorded, "
                   "the skills and the dashboard still run")
        if strict:
            raise typer.Exit(1)
        return
    problems: list[str] = []
    try:
        who = client.whoami()
        typer.echo(json.dumps(who, indent=1))
        expires = who.get("expires_at")
        if expires:
            left = datetime.fromisoformat(expires.replace("Z", "+00:00")) - datetime.now(timezone.utc)
            typer.echo(f"key expires in {left.days} days ({expires})")
            if left < timedelta(days=KEY_WARN_DAYS):
                problems.append(f"key expires in {left.days} days")
        init = client.initialize()
        typer.echo(f"MCP initialize: {init.get('serverInfo')} protocol {client.protocol_version}")
        typer.echo("MCP tools/list: " + ", ".join(t.get("name", "?") for t in client.tools_mcp()))
        catalog = client.tools()
        sent = {**{SECTIONS[k]: a for k, a in ryo_args("SOL").items()},
                "scan_market": {"chain": "bsc", "theme": "news", "top_n": 5, "filter_direction": "all"}}  # every key `nota scan` can send
        for tool, args in sent.items():
            problems += check_args(tool, args, catalog)
        for t in catalog:
            unused = sorted(set((t.get("inputSchema") or {}).get("properties") or {}) - set(sent.get(t.get("name"), {})))
            if unused:
                typer.echo(f"published but unused by Nota: {t.get('name')}({', '.join(unused)})")
        if client.last_rate_limit:
            typer.echo(f"rate limit headers: {client.last_rate_limit}")
    except RyoError as exc:
        problems.append(f"authenticated probe failed: {exc}")
    for p in problems:
        typer.echo(f"problem: {p}")
    if not problems:
        typer.echo("catalog check: every argument Nota sends is accepted by RYO's published schema")
    if strict and problems:
        raise typer.Exit(1)


@app.command("llm-check")
def llm_check(llm: str = typer.Option(os.environ.get("NOTA_LLM", "anthropic"), help="openai (env NOTA_LLM); "
                                      "the check is one 1-token completion")):
    """Spend one token to prove the council's LLM key, balance and model work. Exit 2 when the provider
    refuses the key or the balance (401/402/403), 1 on any other failure."""
    if llm != "openai":
        raise typer.BadParameter("llm-check speaks the OpenAI-compatible API; use --llm openai (env NOTA_LLM)")
    status, detail = _llm(llm).check()
    typer.echo(f"LLM {status or 'unreachable'}: {detail}")
    if status in (401, 402, 403):
        raise typer.Exit(2)
    if not 200 <= status < 300:
        raise typer.Exit(1)


@app.command("decide")
def decide_cmd(
    symbol: str = typer.Argument(..., help="Token symbol, e.g. SOL"),
    source: str = typer.Option("live", help="live | recorded"),
    llm: str = typer.Option(os.environ.get("NOTA_LLM", "anthropic"), help=LLM_HELP),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the LLM output cache"),
    voices: str = typer.Option("", help="Comma list like tg:WatcherGuru,x:handle -> adds narrative_signal (env NOTA_VOICES)"),
    news: bool = typer.Option(False, help="Add news_check via news_verify (Tavily or Venice web search)"),
    notify: bool = typer.Option(False, help="Post the receipt to configured Telegram/Discord channels"),
    price_check: bool = typer.Option(True, help="Add the keyless independent checks: exchange prices, technicals, positioning, DefiLlama liquidity, prediction-market odds"),
    as_json: bool = typer.Option(False, "--json"),
    once_a_day: bool = typer.Option(False, "--once-a-day", help="Skip when this symbol was decided less than 20 h ago"),
):
    """Gather evidence, run the council, size a practice trade, store the receipt."""
    symbol = _sym(symbol)
    last = _ledger().list_decisions(limit=1, symbol=symbol)
    if once_a_day and last:
        # a second call inside the same week-long window watches the same market; 20 h rather than the
        # UTC date, because two calls either side of midnight are 12 h apart, not a day
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last[0]["created_at"])
        if age < timedelta(hours=20):
            typer.echo(f"{symbol}: already decided {age.total_seconds() / 3600:.1f} h ago; skipped")
            _step_summary(f"decide {symbol}", ["status", "detail"], [["skipped", f"decided {age.total_seconds() / 3600:.1f} h ago"]])
            raise typer.Exit()
    src = _source(source)
    try:
        receipt = decide(symbol, src, _llm(llm), _ledger(), RiskLimits(), use_cache=not no_cache, extras=_extras(src, voices, news, price_check))
    except Exception as exc:
        _step_summary(f"decide {symbol}", ["status", "detail"], [["failed", f"{type(exc).__name__}: {exc}"]])
        raise
    _step_summary(f"decide {symbol}", ["status", "receipt", "headline", "degraded"],
                  [["recorded", receipt.id, receipt.headline, ", ".join(k for k, v in receipt.availability.items() if v not in OK)]])
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
            _, fg = first_present(pack, paths.FEAR_GREED)
            return price_crosscheck(sym, reference_price=price, reference_path=path, reference_fear_greed=fg)
        extras["price_check"] = _check

        def _tech(sym, pack):
            _, ref_price = first_present(pack, paths.PRICE_USD)
            _, rsi_ref = first_present(pack, paths.RSI_14)
            _, atr_ref = risk_atr_usd(pack, ref_price) if ref_price else (None, None)
            return technicals_crosscheck(sym, reference_rsi_14=rsi_ref, reference_atr_14=atr_ref)
        extras["technicals_check"] = _tech

        def _positioning(sym, pack):
            from nota.scorecard import peer_derivatives

            fund = pack.get("sentiment_shift.data.evidence.funding.latest_bps")
            return positioning_check(sym, reference_derivatives=pack.get("deep_analysis.data.derivatives"),
                                     peer_derivatives=peer_derivatives(_ledger(), now_iso()[:10]),
                                     ryo_btc_funding_bps=fund if isinstance(fund, (int, float)) and not isinstance(fund, bool) else None)
        extras["positioning_check"] = _positioning

        # macro's liquidity read; and the crowd's P(up), which no role is shown: it is the baseline the
        # judge is scored against (calibration.skill_vs_base 'vs_market'), read at the decision's own price
        extras["liquidity"] = lambda sym, _pack: liquidity_check(sym)

        def _crowd(sym, pack):
            _, price = first_present(pack, paths.PRICE_USD)
            return crowd_odds(sym, horizon_days=7, spot=price if price and price > 0 else None)
        extras["crowd_odds"] = _crowd
    return extras or None


DIRECTION_HELP = "positive (gainers) | negative (losers, for shorts) | all; sent as scan_market filter_direction"


def _direction(value: str) -> str:
    if value not in ("positive", "negative", "all"):
        raise typer.BadParameter("direction must be positive, negative or all")
    return value


def _scan_rows(env) -> list[dict]:
    """RYO's ranked candidate rows; the symbols alone when an answer carries no `candidates` list."""
    rows = env.data.get("candidates") if isinstance(env.data, dict) else None
    if not (isinstance(rows, list) and rows):
        rows = [{"symbol": s} for s in candidate_symbols(env.data)]
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:  # a scan symbol goes on into RYO calls, prompts and fixture paths, like a typed one
            out.append({**r, "symbol": clean_symbol(r.get("symbol"))})
        except ValueError:  # RYO does list tokens whose ticker is not Latin (seen live: a Chinese-named meme coin)
            typer.echo(f"  skipped candidate {r.get('name')!a}: symbol {r.get('symbol')!a} is not 1-15 letters/digits")
    return out


def _print_scan(env, rows: list[dict]) -> None:
    """The shortlist and why each token is on it, plus the two things that make a pick weaker than it looks."""
    typer.echo(f"{'rank':>4} {'symbol':8} {'24h %':>8} {'turnover':>8} {'momentum':>8} {'estab.':6} reason")
    for r in rows:
        num = lambda k: "-" if r.get(k) is None else f"{r[k]:g}" if isinstance(r[k], (int, float)) else str(r[k])
        estab = {True: "yes", False: "no"}.get(r.get("established_asset"), "-")
        typer.echo(f"{num('rank'):>4} {str(r['symbol']).upper():8} {num('change_24h_pct'):>8} {num('turnover_ratio'):>8} "
                   f"{num('momentum_score'):>8} {estab:6} {r.get('reason') or '-'}")
        if r.get("established_asset") is False:
            typer.echo(f"  warning: {str(r['symbol']).upper()} is not an established asset (RYO's flag); thin history, wider noise")
    excluded = env.data.get("excluded_candidate_count") if isinstance(env.data, dict) else None
    if excluded:
        typer.echo(f"  warning: RYO excluded {excluded} candidate(s) from this scan; the shortlist is not the whole market")


@app.command()
def scan(
    top_n: int = typer.Option(5, help="Candidates to take from scan_market"),
    chain: str = typer.Option("", help="Optional chain filter, e.g. bsc"),
    theme: str = typer.Option("", help="Optional theme context, e.g. news"),
    direction: str = typer.Option("all", help=DIRECTION_HELP),
    source: str = typer.Option("live", help="live | recorded"),
    decide_top: int = typer.Option(0, help="Run the council on the first N candidates"),
    llm: str = typer.Option(os.environ.get("NOTA_LLM", "anthropic"), help=LLM_HELP),
):
    """RYO's research funnel: scan_market -> analyze_token on each candidate -> optional council decisions.
    Each decision keeps the scan row that nominated it (the `scan` section of its evidence)."""
    direction = _direction(direction)
    src = _source(source)
    args = {k: v for k, v in {"chain": chain, "theme": theme, "top_n": top_n}.items() if v}
    env = src.call("scan_market", {**args, "filter_direction": direction})
    typer.echo(f"scan_market: {env.status}, data_mode {env.data_mode}, as_of {env.as_of} | {env.summary.headline}")
    for w in env.warnings:
        typer.echo(f"  warning: {w}")
    rows = _scan_rows(env)[:top_n]
    if not rows:
        typer.echo("no candidate symbols found in scan_market data")
        raise typer.Exit(1)
    _print_scan(env, rows)
    candidates = [str(r["symbol"]).upper() for r in rows]
    for sym in candidates:
        try:
            a = src.call("analyze_token", {"symbol": sym})
            typer.echo(f"{sym:8} {a.status:12} {a.summary.headline}")
        except RyoError as exc:
            typer.echo(f"{sym:8} {'error':12} {exc.code}: {exc.message}")
    for sym in candidates[:decide_top]:
        r = decide(sym, src, _llm(llm), _ledger(), RiskLimits(), scan_env=env)
        typer.echo(f"decided {sym}: {r.headline} (receipt {r.id})")


@app.command()
def watch(
    symbols: str = typer.Argument("", help="Comma list, e.g. SOL,BTC (may be empty with --scan-top)"),
    scan_top: int = typer.Option(0, help="Each cycle, also take the top N candidates from scan_market"),
    direction: str = typer.Option("all", help=DIRECTION_HELP),
    every: int = typer.Option(3600, help="Seconds between cycles"),
    cycles: int = typer.Option(0, help="Stop after N cycles (0 = run until interrupted)"),
    source: str = typer.Option("live", help="live | recorded"),
    llm: str = typer.Option(os.environ.get("NOTA_LLM", "anthropic"), help=LLM_HELP),
    voices: str = typer.Option("", help="Telegram/X voices for narrative_signal"),
    news: bool = typer.Option(False, help="Add news_check"),
    notify: bool = typer.Option(False, help="Post each new receipt to Telegram/Discord"),
    price_check: bool = typer.Option(True, help="Add price_check section"),
    close_on_stop: bool = typer.Option(False, help="Exit practice positions whose stop or target the latest independent price has crossed"),
):
    """Autonomous loop: decide every symbol each cycle, exit practice positions at stop/target, score matured decisions, publish receipts."""
    fixed = [_sym(s) for s in symbols.split(",") if s.strip()]
    direction = _direction(direction)
    if not fixed and not scan_top:
        raise typer.BadParameter("give symbols, --scan-top N, or both")
    n = 0
    while True:
        n += 1
        src, led = _source(source), _ledger()
        syms = list(fixed)
        scan_env = None
        if scan_top:
            try:
                scan_env = src.call("scan_market", {"top_n": scan_top, "filter_direction": direction})
                rows = _scan_rows(scan_env)[:scan_top]
                picked = [s for s in (str(r["symbol"]).upper() for r in rows) if s not in syms]
                typer.echo(f"[{now_iso()}] scan_market {scan_env.status}: {', '.join(picked) or 'no new candidates'} (direction {direction})")
                _print_scan(scan_env, [r for r in rows if str(r["symbol"]).upper() in picked])
                syms += picked
            except RyoError as exc:
                typer.echo(f"[{now_iso()}] scan_market failed {exc.code}: {exc.message}; using fixed symbols only")
        for sym in syms:
            try:
                r = decide(sym, src, _llm(llm), led, RiskLimits(), extras=_extras(src, voices, news, price_check),
                           scan_env=scan_env if sym not in fixed else None)
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
    except (KeyError, ValueError, json.JSONDecodeError) as exc:  # no such receipt, or nothing to replay it with
        raise typer.BadParameter(str(exc).strip("'\"")) from None
    except RuntimeError as exc:  # a cache miss: the receipt exists but cannot be verified from the ledger
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
    for did in repair_outcomes(led):  # outcomes stored before horizon_reached was computed from their own times
        typer.echo(f"{did}: horizon fields repaired")
    ids = (led.unresolved() if early else due(led)) if all_ else ([decision_id] if decision_id else [])
    if not ids:
        typer.echo("nothing to resolve")
    # Scoring needs a price, not RYO's price: with no builder key the exchange median stands in, which
    # is what `_price_now` already falls back to when RYO answers without one.
    src = _source(source) if source != "live" or os.environ.get("RYO_MCP_KEY") else None
    if src is None:
        typer.echo("no RYO key: scoring against the independent exchange median")
    for i in ids:
        try:
            out = resolve(i, led, src)
            typer.echo(f"{i} {out.symbol}: {out.return_pct:+.2f}% went_up={out.went_up} horizon_reached={out.horizon_reached} brier={out.brier}")
        except CannotResolve as exc:
            typer.echo(f"{i}: cannot resolve ({exc})")
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise typer.BadParameter(str(exc).strip("'\"")) from None
    for did, p in fill_base_rates(led):  # keyless, and also catches up outcomes scored before base rates existed
        typer.echo(f"{did}: base rate {p if p is not None else 'unavailable'}")


@app.command("lock")
def lock_cmd(symbols: str = typer.Option("", help="Comma-separated; default is the scorecard universe")):
    """Scorecard: lock today's deep_analysis verdict and trade plan for each symbol (one RYO call each,
    paced for the builder fan-out limit). A failure is stored as one row per symbol-day, and a dead key
    stops the run after the first symbol with one 'aborted' row (exit 1)."""
    from nota.scorecard import lock_all

    def echo(r: dict) -> None:  # as each row is saved: a 20-minute run shows progress, and a kill loses nothing unseen
        if r["status"] == "aborted":
            typer.echo(f"aborted: {r['error']}; not asked: {', '.join(r['skipped']) or 'none'}")
            return
        typer.echo(f"{r['symbol']:5} {r['status']:16} {r['verdict'] or '-':12} {r['confluence_state'] or '-':10} "
                   f"basis {r['basis_pct'] if r['basis_pct'] is not None else '-'}%" + (f"  ({r['error']})" if r["error"] else ""))

    rows = lock_all(_source("live"), _ledger(), [s.strip() for s in symbols.split(",") if s.strip()] or None, on_row=echo)
    _step_summary("lock", ["symbol", "status", "verdict", "confluence", "basis %", "error"],
                  [[r.get("symbol"), r["status"], r.get("verdict"), r.get("confluence_state"), r.get("basis_pct"),
                    r.get("error")] for r in rows])
    if not rows:
        typer.echo("every symbol already has a lock from the last 20 h")
    if any(r["status"] == "aborted" for r in rows):
        raise typer.Exit(1)


@app.command("settle")
def settle_cmd():
    """Scorecard: settle every locked plan whose 24 h / 72 h window has closed, on OKX hourly candles. Keyless."""
    from nota.scorecard import settle_all

    out = settle_all(_ledger())
    if not out:
        typer.echo("nothing to settle")
    for r in out:
        typer.echo(f"{r['lock_id']} {r['symbol']:5} {r['horizon_h']}h {r['status']} {r.get('result', r.get('error', ''))}")
    _step_summary("settle", ["lock", "symbol", "horizon", "status", "result"],
                  [[r["lock_id"], r["symbol"], f"{r['horizon_h']} h", r["status"], r.get("result", r.get("error"))] for r in out])


@app.command("stamp")
def stamp_cmd():
    """Anchor every unstamped scorecard lock and receipt in Bitcoin through OpenTimestamps calendars, and
    upgrade proofs pending for over 3 h once their block is confirmed. Keyless."""
    from nota.stamp import stamp_ledger

    out = stamp_ledger(_ledger())
    for line in out:
        typer.echo(line)
    if not out:
        typer.echo("nothing to stamp or upgrade")


@app.command("merge-db")
def merge_db(other: Path = typer.Argument(..., exists=True, dir_okay=False, help="Another ledger snapshot, e.g. origin/main's")):
    """Add every row of another snapshot that this ledger (NOTA_DB) lacks. The ledger cycle runs it when its
    push is rejected, so a day's locks survive a snapshot committed meanwhile instead of one side being lost."""
    for table, n in _ledger().merge_from(str(other)).items():
        typer.echo(f"{table}: {n} row(s) added")


@app.command("export")
def export_cmd(out: Path = typer.Argument(..., dir_okay=False, help="Where to write the JSON, e.g. data/snapshot.json")):
    """Write the receipts (newest 200) and the scorecard summary at 24 h and 72 h as one JSON file: the
    readable half of what the ledger cycle attests with Sigstore, next to data/demo.db itself."""
    from nota.scorecard import HORIZONS_H, summary

    led = _ledger()
    doc = {"exported_at": now_iso(),
           "receipts": [json.loads(led.get_decision(d["id"])) for d in led.list_decisions(limit=200)],
           "scorecard": {f"{h}h": summary(led, h) for h in HORIZONS_H}}
    out.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    typer.echo(f"wrote {out}: {len(doc['receipts'])} receipts, scorecard at {', '.join(doc['scorecard'])}")


@app.command()
def scores():
    """Per-agent Brier scores and the weights the judge currently uses, with how many decisions are
    scored and how many are still waiting for their horizon - an empty table means "nothing due yet",
    not "nothing works"."""
    from nota.api import scores as scores_api  # same numbers the dashboard reads

    v = scores_api()
    if not v["reliability"]["n"]:  # five empty bins say less than one sentence does
        v["reliability"] = {"n": 0, "note": "no decision has reached its seven-day horizon yet"}
    typer.echo(json.dumps(v, indent=1))


@app.command()
def show(decision_id: str, as_json: bool = typer.Option(False, "--json")):
    """Print a stored receipt."""
    raw = _ledger().get_decision(decision_id)
    if raw is None:
        raise typer.BadParameter(f"no decision {decision_id}")
    typer.echo(raw if as_json else render_markdown(Receipt.model_validate_json(raw)))


@app.command("positions")
def positions_cmd(as_json: bool = typer.Option(False, "--json")):
    """Open practice positions against the newest evidence price - an independent exchange median when
    that receipt carries one, otherwise RYO's own price. The as_of beside it says which moment it is."""
    from nota.api import positions as open_positions

    rows = open_positions()
    if as_json:
        typer.echo(json.dumps(rows, indent=1))
        return
    if not rows:
        typer.echo("no open practice positions")
    for p in rows:
        latest = "unavailable" if p["latest_price"] is None else f"{p['latest_price']:g}"
        pnl = "-" if p["pnl_usd"] is None else f"{p['pnl_usd']} USD"
        typer.echo(f"{p['symbol']:6} {p['side']:5} entry {p['entry']:g} stop {p['stop']:g} target {p['target']:g} | latest {latest} "
                   f"({p['latest_as_of']}) | {p['status']} | {pnl} | receipt {p['decision_id']}")


@app.command("list")
def list_cmd(limit: int = 20):
    """Newest receipts in the ledger: id, timestamp, symbol, model."""
    for d in _ledger().list_decisions(limit):
        typer.echo(f"{d['id']}  {d['created_at']}  {d['symbol']:6}  {d['model']}")


@app.command("record")
def record_cmd(symbol: str):
    """Capture live RYO responses for SYMBOL (all six tools) into fixtures/recorded (needs RYO_MCP_KEY).
    Nothing is written unless every tool answered: a half-recorded set would replace good fixtures with a mix."""
    symbol = _sym(symbol)
    client = _source("live")
    calls = [(SECTIONS[key], args) for key, args in ryo_args(symbol).items()] + [("scan_market", {})]
    failed = []
    with tempfile.TemporaryDirectory() as tmp:
        for tool, args in calls:
            try:
                record(client, tool, args, tmp)
            except RyoError as exc:
                failed.append(f"{tool}: {exc}")
        if failed:
            for f in failed:
                typer.echo(f)
            typer.echo(f"{len(failed)} of {len(calls)} tools failed; {RECORDED_ROOT} left unchanged")
            raise typer.Exit(1)
        shutil.copytree(tmp, RECORDED_ROOT, dirs_exist_ok=True)
        for f in sorted(Path(tmp).rglob("*.json")):
            typer.echo(f"recorded {RECORDED_ROOT / f.relative_to(tmp)}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = typer.Option(8000, min=1, max=65535)):
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
    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"ARGS_JSON is not valid JSON: {exc}") from None
    if not isinstance(args, dict):
        raise typer.BadParameter("""ARGS_JSON must be a JSON object, e.g. '{"symbol": "SOL"}'""")
    deps = {}
    if (name == "news_verify" and args.get("symbol")) or name == "positioning_check":
        try:
            deps["ryo"] = _source(source)
        except typer.BadParameter:
            pass  # skill reports the missing market context itself
    if name == "positioning_check":
        deps["ledger"] = _ledger()  # same-day scorecard locks are the peers RYO's values are compared across
    try:
        env = skill_invoke(name, args, **deps)
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc).strip("'\"")) from None
    typer.echo(env.model_dump_json(indent=1))


if __name__ == "__main__":
    app()
