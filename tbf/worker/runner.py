from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from tbf.core.bot import Bot, Targets
from tbf.core.execution import Order, Transfer, execute_targets
from tbf.core.metrics import compute_metrics
from tbf.core.portfolio import Portfolio, effective_leverage, equity, liquidate_venue, liquidated_venues, settle_funding
from tbf.core.risk import check_risk, next_consecutive_losses
from tbf.core.step import run_strategy, utc_day
from tbf.core.types import HOUR, TIMEFRAME_MS, EquityPoint, Fill
from tbf.market import VenueMarket
from tbf.worker.brokers import Broker, OrderRequest
from tbf.worker.notify import Notifier
from tbf.worker.store import BotRow, Store

# 戦略に渡すファンディング履歴の最短期間
FUNDING_WINDOW_MS = 7 * 24 * HOUR


@dataclass
class Runtime:
    mode: str  # paper | live
    store: Store
    markets: Mapping[str, VenueMarket]
    broker: Broker
    notifier: Notifier
    now: int
    notify_fills: bool = True


def bots_for_mode(bots: Sequence[Bot], mode: str) -> list[Bot]:
    return [b for b in bots if mode in b.deploy]


async def fetch_prices(bots: Sequence[Bot], rt: Runtime) -> dict[str, dict[str, float]]:
    """取引所ごとにまとめて現在価格を取る。{venue: {symbol: price}}"""
    want: dict[str, set[str]] = {}
    for b in bots:
        for inst in b.instruments.values():
            want.setdefault(inst.venue, set()).add(inst.symbol)
    out: dict[str, dict[str, float]] = {}
    for venue, symbols in want.items():
        try:
            out[venue] = await rt.markets[venue].prices(sorted(symbols))
        except Exception as e:  # noqa: BLE001  その取引所の bot だけエラーにする
            print(f"prices {venue} failed: {e}")
            out[venue] = {}
    return out


async def tick(bots: Sequence[Bot], rt: Runtime) -> None:
    """cron 1 回分。全 bot を監視し、新しい確定足があれば戦略を実行する"""
    if not bots:
        return
    by_venue = await fetch_prices(bots, rt)
    for bot in bots:
        try:
            prices = {}
            for alias, inst in bot.instruments.items():
                px = by_venue.get(inst.venue, {}).get(inst.symbol)
                if not px or px <= 0:
                    raise RuntimeError(f"no price for {inst}")
                prices[alias] = px
            await run_bot(bot, prices, rt)
        except Exception as e:  # noqa: BLE001  1 つの bot の失敗で他を止めない
            msg = f"{bot.id}: {type(e).__name__}: {e}"
            print(msg, traceback.format_exc())
            await rt.store.add_event(bot.id, "error", msg, rt.now)
            await rt.notifier.send(f":warning: error {msg}")


async def run_bot(bot: Bot, prices: dict[str, float], rt: Runtime) -> None:
    store, now = rt.store, rt.now
    row = await store.get_or_init(bot, now)
    pf = row.load_portfolio(bot)

    # --- 監視 (毎 tick) ---
    eq = equity(pf, bot.instruments, prices)
    day = utc_day(now)
    if row.day != day:
        row.day, row.day_start_equity = day, eq
    row.peak_equity = max(row.peak_equity, eq)
    row.last_prices = json.dumps(prices)
    row.updated_at = now
    await store.save_monitor(row)
    if row.status != "running":
        return

    # ペーパーでは清算も再現する (本番は取引所が執行する)
    if rt.mode == "paper" and any(bot.instruments[a].is_perp for a in pf.holdings):
        venues = liquidated_venues(pf, bot.instruments, prices)
        if venues:
            for v in venues:
                liquidate_venue(pf, bot.instruments, v)
            await store.add_event(bot.id, "warn", f"liquidated on {', '.join(venues)}", now)
            await rt.notifier.send(f":boom: {bot.id} liquidated on {', '.join(venues)} (paper)")
            await rebalance(bot, row, pf, prices, {}, "liquidation", rt)
            return

    breach = check_risk(
        bot.risk,
        equity=eq,
        peak_equity=row.peak_equity,
        day_start_equity=row.day_start_equity,
        consecutive_losses=row.consecutive_losses,
    )
    if breach:
        await halt(bot, f"risk: {breach}", bot.risk.flatten_on_halt, prices, rt)
        return

    # --- 戦略 (新しい確定足ごと) ---
    tf = TIMEFRAME_MS[bot.timeframe]
    latest_closed = now // tf * tf - tf
    if latest_closed <= row.last_candle or not await store.claim_candle(bot.id, latest_closed):
        return
    row = await store.get(bot.id)  # type: ignore[assignment]
    assert row is not None
    pf = row.load_portfolio(bot)

    # ファンディングの受け払いと、戦略に渡す履歴
    perps = [a for a, i in bot.instruments.items() if i.is_perp]
    funding = {}
    if row.last_funding == 0:
        row.last_funding = now
    for alias in perps:
        inst = bot.instruments[alias]
        since = min(row.last_funding, now - max(bot.lookback * tf, FUNDING_WINDOW_MS))
        hist = await rt.markets[inst.venue].funding(inst.symbol, since, now)
        funding[alias] = hist
        for f in hist:
            if f.time > row.last_funding:
                settle_funding(pf, inst, alias, f.rate, prices[alias] / (1 + f.premium))
    if perps:
        row.last_funding = max([row.last_funding] + [f.time for fs in funding.values() for f in fs])

    bars = {}
    for alias, inst in bot.instruments.items():
        bars[alias] = await rt.markets[inst.venue].recent(inst.symbol, bot.timeframe, bot.lookback, now)
    short = [a for a, cs in bars.items() if len(cs) < bot.lookback]
    if short:
        await store.add_event(bot.id, "warn", f"not enough candles for {short}", now)
        await save(bot, row, pf, prices, latest_closed, rt)
        return

    state = json.loads(row.strategy_state)
    targets, reason = run_strategy(bot, latest_closed + tf, bars, funding, pf, prices, state, rt.mode)  # type: ignore[arg-type]
    row.strategy_state = json.dumps(state)
    if targets is None:
        # 戦略が「現状維持」でも、先物口座のレバレッジが目標から外れたら配分し直す
        for venue in {bot.instruments[a].venue for a in pf.holdings if bot.instruments[a].is_perp}:
            lev = effective_leverage(pf, bot.instruments, prices, venue)
            if lev > bot.leverage * bot.rebalance_band or lev < bot.leverage / bot.rebalance_band:
                targets = json.loads(row.targets) if row.targets else None
                reason = f"rebalance: {venue} leverage {lev:.2f}"
                break
    if targets is not None:
        await rebalance(bot, row, pf, prices, targets, reason, rt, candle=latest_closed)
        return
    await save(bot, row, pf, prices, latest_closed, rt)


async def rebalance(
    bot: Bot,
    row: BotRow,
    pf: Portfolio,
    prices: dict[str, float],
    targets: Targets,
    reason: Optional[str],
    rt: Runtime,
    candle: Optional[int] = None,
) -> None:
    """目標まで売買して保存する"""
    was_flat = not pf.holdings
    seq = [0]
    order_ids: list[Optional[str]] = []

    async def fill_fn(o: Order) -> Optional[Fill]:
        seq[0] += 1
        res = await rt.broker.execute(
            OrderRequest(
                bot_id=bot.id,
                alias=o.alias,
                instrument=bot.instruments[o.alias],
                side=o.side,
                qty=o.qty,
                ref_price=o.ref_price,
                client_order_id=client_order_id(bot.id, candle if candle is not None else rt.now, seq[0]),
                now=rt.now,
            )
        )
        if res is None:
            return None
        order_ids.append(res.order_id)
        return res.fill

    async def on_fill(f: Fill) -> None:
        await rt.store.add_fill(bot.id, f, order_ids[-1] if order_ids else None, reason)

    async def transfer_fn(t: Transfer) -> bool:
        if rt.mode == "paper":
            await rt.store.add_event(bot.id, "info", f"transfer {t.amount:.2f} {t.src} -> {t.dst}", rt.now)
            return True
        # 本番の取引所間送金は自動化しない (出金権限のある鍵を置かないため)
        msg = f"{bot.id}: {t.src} から {t.dst} へ {t.amount:.2f} USD の資金移動が必要です (手動)"
        await rt.store.add_event(bot.id, "warn", msg, rt.now)
        await rt.notifier.send(f":money_with_wings: {msg}")
        return False

    try:
        rep = await execute_targets(bot, pf, prices, targets, fill_fn, transfer_fn, on_fill)
    except Exception:
        # 途中まで約定した分は口座に反映済みなので、保存してから失敗を伝える
        await save(bot, row, pf, prices, None, rt)
        raise
    for f in rep.fills:
        row.consecutive_losses = next_consecutive_losses(row.consecutive_losses, f)
        if rt.notify_fills:
            pnl = f" pnl {f.realized_pnl:.2f}" if f.reduce else ""
            why = f" ({reason})" if reason else ""
            await rt.notifier.send(
                f"{bot.id} {f.side} {f.qty:.8g} {bot.instruments[f.instrument]} @ {f.price:.6g}{pnl}{why}"
            )
    for s in rep.skipped:
        await rt.store.add_event(bot.id, "info", f"skipped {s}", rt.now)
    if was_flat and pf.holdings:
        await rt.store.add_event(bot.id, "info", f"enter ({reason or ''})", rt.now)
    elif not was_flat and not pf.holdings:
        await rt.store.add_event(bot.id, "info", f"exit ({reason or ''})", rt.now)
    row.targets = json.dumps(targets)
    await save(bot, row, pf, prices, candle, rt)


async def save(bot: Bot, row: BotRow, pf: Portfolio, prices: dict[str, float], candle: Optional[int], rt: Runtime) -> None:
    row.set_portfolio(pf)
    row.updated_at = rt.now
    if not await rt.store.save_account(row):
        msg = f"{bot.id}: 口座が同時に更新されたため保存できませんでした。持ち高を確認してください"
        await rt.store.add_event(bot.id, "error", msg, rt.now)
        await rt.notifier.send(f":warning: {msg}")
        return
    if candle is not None:
        await rt.store.add_equity(bot.id, EquityPoint(candle + TIMEFRAME_MS[bot.timeframe], equity(pf, bot.instruments, prices)))


async def halt(bot: Bot, reason: str, flatten: bool, prices: dict[str, float], rt: Runtime) -> None:
    """bot を停止する。flatten なら全ポジションを成行で手仕舞う"""
    store, now = rt.store, rt.now
    await store.set_status(bot.id, "halted", reason)
    await store.add_event(bot.id, "warn", f"halted: {reason}", now)
    msg = f":octagonal_sign: {bot.id} halted — {reason}"
    row = await store.get(bot.id)
    if row and flatten:
        pf = row.load_portfolio(bot)
        if pf.holdings:
            if any(prices.get(a, 0) <= 0 for a in pf.holdings):
                msg += " — FLATTEN SKIPPED (no price), manual action required"
            else:
                try:
                    await rebalance(bot, row, pf, prices, {}, "halt", rt)
                    left = {a: p.qty for a, p in pf.holdings.items()}
                    msg += f" (positions partially closed: {left})" if left else " (positions closed)"
                except Exception as e:  # noqa: BLE001
                    await store.add_event(bot.id, "error", f"flatten failed: {e}", now)
                    msg += f" — FLATTEN FAILED, manual action required: {e}"
    await rt.notifier.send(msg)


async def daily_report(bots: Sequence[Bot], rt: Runtime) -> None:
    """1 日 1 回の成績サマリ"""
    if not bots:
        return
    pct = lambda x: f"{x * 100:.2f}%"  # noqa: E731
    lines = [f":bar_chart: daily report ({utc_day(rt.now)})"]
    for bot in bots:
        row = await rt.store.get(bot.id)
        if not row:
            continue
        m = compute_metrics(await rt.store.equity(bot.id), await rt.store.fills(bot.id))
        eq = row.equity(bot)
        lines.append(
            f"• {bot.id} [{row.status}] equity {eq:.2f} ({pct(eq / bot.capital - 1)}) "
            f"maxDD {pct(m.max_drawdown)} trades {m.trades} win {pct(m.win_rate)} sharpe {m.sharpe:.2f}"
        )
    await rt.notifier.send("\n".join(lines))


def client_order_id(bot_id: str, time_ms: int, seq: int) -> str:
    """Binance の newClientOrderId は 36 文字まで"""
    return f"{bot_id[:24]}-{_base36(time_ms // 1000)}{seq}"


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while True:
        n, r = divmod(n, 36)
        s = digits[r] + s
        if n == 0:
            return s
