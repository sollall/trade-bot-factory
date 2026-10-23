from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from tbf.core.bot import Bot
from tbf.core.metrics import compute_metrics
from tbf.core.portfolio import apply_fill
from tbf.core.risk import check_risk, next_consecutive_losses
from tbf.core.step import run_strategy, utc_day
from tbf.core.types import TIMEFRAME_MS, Candle, EquityPoint
from tbf.worker.brokers import Broker, OrderRequest
from tbf.worker.notify import Notifier
from tbf.worker.store import BotRow, Store


class Market(Protocol):
    async def prices(self, symbols: list[str]) -> dict[str, float]: ...
    async def recent(self, symbol: str, timeframe: str, count: int, now: int) -> list[Candle]: ...


@dataclass
class Runtime:
    mode: str  # paper | live
    store: Store
    market: Market
    broker: Broker
    notifier: Notifier
    now: int
    notify_fills: bool = True


def bots_for_mode(bots: Sequence[Bot], mode: str) -> list[Bot]:
    return [b for b in bots if mode in b.deploy]


async def tick(bots: Sequence[Bot], rt: Runtime) -> None:
    """cron 1 回分。全 bot を監視し、新しい確定足があれば戦略を実行する"""
    if not bots:
        return
    prices = await rt.market.prices([b.symbol for b in bots])
    for bot in bots:
        try:
            price = prices.get(bot.symbol)
            if not price or price <= 0:
                raise RuntimeError(f"no price for {bot.symbol}")
            await run_bot(bot, price, rt)
        except Exception as e:  # noqa: BLE001  1 つの bot の失敗で他を止めない
            msg = f"{bot.id}: {type(e).__name__}: {e}"
            print(msg, traceback.format_exc())
            await rt.store.add_event(bot.id, "error", msg, rt.now)
            await rt.notifier.send(f":warning: error {msg}")


async def run_bot(bot: Bot, price: float, rt: Runtime) -> None:
    store, now = rt.store, rt.now
    row = await store.get_or_init(bot.id, bot.capital, now)

    # --- 監視 (毎 tick) ---
    eq = row.equity(price)
    day = utc_day(now)
    if row.day != day:
        row.day, row.day_start_equity = day, eq
    row.peak_equity = max(row.peak_equity, eq)
    row.last_price = price
    row.updated_at = now
    await store.save_monitor(row)

    if row.status != "running":
        return
    breach = check_risk(
        bot.risk,
        equity=eq,
        peak_equity=row.peak_equity,
        day_start_equity=row.day_start_equity,
        consecutive_losses=row.consecutive_losses,
    )
    if breach:
        await halt(bot, f"risk: {breach}", bot.risk.flatten_on_halt, price, rt)
        return

    # --- 戦略 (新しい確定足ごと) ---
    tf = TIMEFRAME_MS[bot.timeframe]
    latest_closed = now // tf * tf - tf
    if latest_closed <= row.last_candle or not await store.claim_candle(bot.id, latest_closed):
        return

    candles = await rt.market.recent(bot.symbol, bot.timeframe, bot.lookback, now)
    row = await store.get(bot.id)
    assert row is not None
    if len(candles) < bot.lookback:
        await store.add_event(bot.id, "warn", f"only {len(candles)}/{bot.lookback} candles available", now)
        return

    state = json.loads(row.strategy_state)
    step = run_strategy(bot, candles, row.portfolio, price, state, rt.mode)  # type: ignore[arg-type]
    row.strategy_state = json.dumps(state)
    if step.qty:
        row = await trade(bot, row, step.qty, price, client_order_id(bot.id, latest_closed), step.reason, rt)
    row.updated_at = now
    await store.save_account(row)
    await store.add_equity(bot.id, EquityPoint(latest_closed, row.equity(price)))


async def trade(
    bot: Bot, row: BotRow, qty: float, price: float, order_id: str, reason: Optional[str], rt: Runtime
) -> BotRow:
    side = "buy" if qty > 0 else "sell"
    res = await rt.broker.execute(
        OrderRequest(
            bot_id=bot.id,
            symbol=bot.symbol,
            side=side,
            qty=abs(qty),
            ref_price=price,
            client_order_id=order_id,
            taker_rate=bot.fees.taker_rate,
            slippage_bps=bot.fees.slippage_bps,
            now=rt.now,
        )
    )
    if res is None:
        await rt.store.add_event(bot.id, "info", f"{side} {abs(qty)} skipped (below exchange minimum)", rt.now)
        return row
    portfolio, fill = apply_fill(row.portfolio, res.fill)
    row.set_portfolio(portfolio)
    row.consecutive_losses = next_consecutive_losses(row.consecutive_losses, fill)
    await rt.store.add_fill(bot.id, fill, res.order_id, reason)
    if rt.notify_fills:
        pnl = f" pnl {fill.realized_pnl:.2f}" if fill.side == "sell" else ""
        why = f" ({reason})" if reason else ""
        await rt.notifier.send(f"{bot.id} {fill.side} {fill.qty:.8g} {bot.symbol} @ {fill.price:.6g}{pnl}{why}")
    return row


async def halt(bot: Bot, reason: str, flatten: bool, price: float, rt: Runtime) -> None:
    """bot を停止する。flatten なら保有を成行で手仕舞う"""
    store, now = rt.store, rt.now
    await store.set_status(bot.id, "halted", reason)
    await store.add_event(bot.id, "warn", f"halted: {reason}", now)
    msg = f":octagonal_sign: {bot.id} halted — {reason}"
    row = await store.get(bot.id)
    if flatten and row and row.qty > 0:
        if price <= 0:
            msg += " — FLATTEN SKIPPED (no price), manual action required"
        else:
            try:
                row = await trade(bot, row, -row.qty, price, client_order_id(bot.id, now, "h"), "halt", rt)
                row.updated_at = now
                await store.save_account(row)
                msg += f" (position partially closed, {row.qty:.8g} left)" if row.qty > 0 else " (position closed)"
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
        eq = row.equity()
        lines.append(
            f"• {bot.id} [{row.status}] equity {eq:.2f} ({pct(eq / bot.capital - 1)}) "
            f"maxDD {pct(m.max_drawdown)} trades {m.trades} win {pct(m.win_rate)} sharpe {m.sharpe:.2f}"
        )
    await rt.notifier.send("\n".join(lines))


def client_order_id(bot_id: str, time_ms: int, prefix: str = "") -> str:
    """Binance の newClientOrderId は 36 文字まで"""
    return f"{bot_id[:24]}-{prefix}{_base36(time_ms // 1000)}"


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while True:
        n, r = divmod(n, 36)
        s = digits[r] + s
        if n == 0:
            return s
