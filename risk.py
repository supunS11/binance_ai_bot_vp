"""Position sizing and portfolio limits.

SIZING IS FROM THE STOP, NOT FROM LEVERAGE. quantity = (equity * RISK_PCT) /
|entry - stop|. Leverage is a margin-efficiency setting that determines how much
collateral the position ties up; it does NOT determine risk. Conflating the two is
the single most common way a futures bot blows up: sizing by "10x of equity" makes
the loss on a stop-out a function of how far away the stop happens to be, which is
exactly backwards. Here the loss on a stop-out is RISK_PCT of equity by
construction, and leverage only decides whether the exchange will accept the
position.

EVERY ROUNDING GOES DOWN. Quantity is floored to stepSize, never rounded to
nearest, because rounding up places more risk than authorised. The give-up is a
fraction of one step; the alternative compounds.

THE CLUSTER CAP IS NOT OPTIONAL ON THIS VENUE. Alt perpetuals are close to a
one-factor market: ten alt longs is one leveraged bet on BTC, not a diversified
book. A naive MAX_CONCURRENT_POSITIONS of six permits six correlated positions,
which on a bad hour behaves like a single position at six times the size. So
exposure is capped per DIRECTION as well as in total, and the per-direction cap is
the binding one in practice.

LIMITS ARE CHECKED AGAINST THE EXCHANGE, NOT AGAINST LOCAL STATE. Local counters
drift - a fill that arrived during a restart, a manual intervention, a partially
closed position. positionRisk is the truth, and this module takes it as input
rather than maintaining its own belief.
"""
import logging
from dataclasses import dataclass, field

import config
import sessions
from exchange import filters
from setups.base import RejectReason, reject

log = logging.getLogger(__name__)


@dataclass
class PortfolioState:
    """A snapshot of what is actually open, plus today's realised outcomes.

    Built from positionRisk and the trade journal rather than accumulated in
    memory, so a restart cannot lose track of exposure.
    """
    equity: float = 0.0
    available_balance: float = 0.0
    open_positions: dict = field(default_factory=dict)   # symbol -> signed qty
    realised_r_today: float = 0.0
    consecutive_losses: int = 0
    # Timestamp (ms) of the most recent loss in that streak, or None - risk.py's own
    # automatic release for CONSECUTIVE_LOSS_LIMIT measures from this, not from a win.
    last_loss_closed_at: int = None
    # True when `equity` is PAPER_EQUITY rather than a real balance. Recorded so a
    # journal row can never be mistaken for one sized against real capital.
    notional_equity: bool = False

    @property
    def position_count(self):
        return len([qty for qty in self.open_positions.values() if qty != 0])

    def direction_count(self, direction):
        if direction == "BUY":
            return len([qty for qty in self.open_positions.values() if qty > 0])
        return len([qty for qty in self.open_positions.values() if qty < 0])

    def has_position(self, symbol):
        return abs(self.open_positions.get(symbol.upper(), 0.0)) > 0

    def as_row(self):
        row = {
            "equity": self.equity,
            "positions": self.position_count,
            "longs": self.direction_count("BUY"),
            "shorts": self.direction_count("SELL"),
            "realised_r_today": round(self.realised_r_today, 4),
            "consecutive_losses": self.consecutive_losses,
        }
        # Visible cooldown countdown once the streak is actually over the limit -
        # this is the number that would have answered 2026-10-10's "why has nothing
        # traded" question directly, instead of a log dive.
        if self.consecutive_losses >= config.CONSECUTIVE_LOSS_LIMIT:
            cooldown_ms = config.CONSECUTIVE_LOSS_COOLDOWN_HOURS * 3_600_000
            since_last_loss = sessions.now_ms() - (self.last_loss_closed_at or 0)
            row["consecutive_loss_cooldown_remaining_hours"] = round(
                max(0.0, (cooldown_ms - since_last_loss) / 3_600_000), 2)
        return row


def check_limits(candidate, state, paper=False):
    """Portfolio-level admission. Returns a Rejection or None.

    Ordered so the most informative refusal is recorded: an existing position on the
    symbol is a different fact from being at the global cap, and both are different
    from having hit a loss limit.

    `paper=True` skips ONLY the TRADING_ENABLED check, so an observation run still
    evaluates every real portfolio limit and still gets sized. Without it a disabled
    bot refuses here, before sizing, and the journal records nothing but
    TRADING_DISABLED - which says the flag is off and nothing whatever about what the
    system would have done. An observation run whose journal cannot be compared with a
    replay is not observing anything.
    """
    if not paper and not config.TRADING_ENABLED:
        return reject(RejectReason.TRADING_DISABLED, candidate.setup, candidate.symbol,
                      direction=candidate.direction,
                      detail="TRADING_ENABLED is False")

    if state.has_position(candidate.symbol):
        return reject(RejectReason.ALREADY_IN_POSITION, candidate.setup,
                      candidate.symbol, direction=candidate.direction,
                      detail="one position per symbol, always")

    if state.position_count >= config.MAX_CONCURRENT_POSITIONS:
        return reject(RejectReason.RISK_LIMIT_POSITIONS, candidate.setup,
                      candidate.symbol, direction=candidate.direction,
                      detail=(f"{state.position_count} open >= "
                              f"{config.MAX_CONCURRENT_POSITIONS}"))

    if state.direction_count(candidate.direction) >= config.MAX_CONCURRENT_PER_DIRECTION:
        return reject(RejectReason.RISK_LIMIT_DIRECTION, candidate.setup,
                      candidate.symbol, direction=candidate.direction,
                      detail=(f"{state.direction_count(candidate.direction)} "
                              f"{candidate.direction} positions >= "
                              f"{config.MAX_CONCURRENT_PER_DIRECTION} - alt perps are "
                              f"close to one-factor, so same-direction exposure "
                              f"concentrates rather than diversifies"))

    if state.realised_r_today <= -abs(config.DAILY_LOSS_LIMIT_R):
        return reject(RejectReason.RISK_LIMIT_DAILY_LOSS, candidate.setup,
                      candidate.symbol, direction=candidate.direction,
                      detail=(f"{state.realised_r_today:+.2f}R today <= "
                              f"-{config.DAILY_LOSS_LIMIT_R}R"))

    if state.consecutive_losses >= config.CONSECUTIVE_LOSS_LIMIT:
        cooldown_ms = config.CONSECUTIVE_LOSS_COOLDOWN_HOURS * 3_600_000
        since_last_loss = sessions.now_ms() - (state.last_loss_closed_at or 0)
        if since_last_loss < cooldown_ms:
            remaining_hours = (cooldown_ms - since_last_loss) / 3_600_000
            return reject(RejectReason.RISK_LIMIT_CONSECUTIVE, candidate.setup,
                          candidate.symbol, direction=candidate.direction,
                          detail=(f"{state.consecutive_losses} consecutive losses >= "
                                  f"{config.CONSECUTIVE_LOSS_LIMIT}, releases in "
                                  f"{remaining_hours:.1f}h"))
        # AUTOMATIC RELEASE. The streak's raw count is still over the limit, but the
        # most recent loss is old enough that this is no longer "the bot refusing to
        # try again after a bad run" - see CONSECUTIVE_LOSS_COOLDOWN_HOURS's own
        # comment in config.py for why this exists at all. One candidate gets through;
        # if it also loses, the next check measures from ITS closed_at, so a genuinely
        # bad run still only gets one attempt per cooldown window, not an open door.

    return None


def size(candidate, state, spec, risk_pct=None):
    """Compute quantity for a candidate. Mutates it with qty/notional/risk_amount.

    Returns a Rejection when the position cannot be expressed on this symbol - which
    is a real and frequent outcome on coarse-lot symbols, where flooring to stepSize
    drops the notional under the venue's minimum.

    FIXED_MARGIN_SIZING_ENABLED switches to bot_ds's model: quantity comes from
    `MARGIN_PER_TRADE * LEVERAGE / entry_price` instead of from the stop, and
    risk_amount becomes whatever dollar loss that implies at THIS trade's stop
    distance - informational, no longer the sizing input.
    """
    fixed_margin = config.FIXED_MARGIN_SIZING_ENABLED

    risk_distance = candidate.risk_distance
    if risk_distance <= 0:
        return reject(RejectReason.STOP_TOO_TIGHT, candidate.setup, candidate.symbol,
                      direction=candidate.direction, detail="zero risk distance")
    if state.equity <= 0:
        return reject(RejectReason.INSUFFICIENT_MARGIN, candidate.setup,
                      candidate.symbol, direction=candidate.direction,
                      detail="equity is zero")

    if fixed_margin:
        margin_target = max(float(config.MARGIN_PER_TRADE), 0.0)
        if margin_target <= 0:
            return reject(RejectReason.INSUFFICIENT_MARGIN, candidate.setup,
                          candidate.symbol, direction=candidate.direction,
                          detail="MARGIN_PER_TRADE is zero")
        raw_quantity = (margin_target * config.LEVERAGE) / candidate.entry_price
    else:
        risk_pct = config.RISK_PCT if risk_pct is None else risk_pct
        risk_amount = state.equity * float(risk_pct)
        raw_quantity = risk_amount / risk_distance

    quantity = filters.round_quantity(raw_quantity, spec.step_size)
    if quantity <= 0:
        return reject(RejectReason.QTY_ZERO, candidate.setup, candidate.symbol,
                      direction=candidate.direction,
                      detail=(f"raw qty {raw_quantity:.10g} floors to zero at "
                              f"stepSize {spec.step_size}"))

    try:
        filters.check_quantity(quantity, spec)
    except filters.FilterRejection as exc:
        # Below minQty is a legitimate "cannot trade this symbol at this risk"
        # rather than a bug: raising risk to reach minQty would breach the risk
        # budget, which is never the right trade-off.
        return reject(RejectReason.QTY_ZERO, candidate.setup, candidate.symbol,
                      direction=candidate.direction,
                      detail=f"{exc.code}: {exc.detail}")

    notional = float(quantity) * candidate.entry_price
    try:
        filters.check_notional(candidate.entry_price, quantity, spec)
    except filters.FilterRejection as exc:
        return reject(RejectReason.NOTIONAL_TOO_SMALL, candidate.setup,
                      candidate.symbol, direction=candidate.direction,
                      detail=(f"{exc.detail} - raising size to clear the minimum "
                              f"would breach the risk budget"))

    # Margin check. Isolated margin at LEVERAGE requires notional/leverage of
    # collateral, plus room for fees. Checked against AVAILABLE balance rather than
    # equity, because equity includes margin already committed elsewhere. Under fixed
    # sizing the margin IS the target, by construction - no need to recompute it from
    # notional, which would just be `margin_target` back again up to rounding.
    required_margin = margin_target if fixed_margin else notional / max(1, int(config.LEVERAGE))
    if state.available_balance > 0 and required_margin > state.available_balance:
        return reject(RejectReason.INSUFFICIENT_MARGIN, candidate.setup,
                      candidate.symbol, direction=candidate.direction,
                      detail=(f"needs {required_margin:.2f} margin at "
                              f"{config.LEVERAGE}x, have "
                              f"{state.available_balance:.2f} available"))

    candidate.quantity = float(quantity)
    candidate.notional = notional
    candidate.risk_amount = float(quantity) * risk_distance if fixed_margin else risk_amount
    return None


def realised_r(entry_price, exit_price, stop_price, direction, quantity,
               entry_fee_rate=None, exit_fee_rate=None):
    """Realised R for a closed trade, net of fees.

    R is defined against the ORIGINAL stop distance, not against the actual loss.
    That matters when an exit slips past the stop: a stop-out that fills 20% beyond
    the stop is -1.2R, and recording it as -1.0R would understate slippage exactly
    where it is most expensive. Gross and net are both returned so the fee drag is
    visible rather than baked in.
    """
    entry_fee_rate = config.FEE_TAKER if entry_fee_rate is None else entry_fee_rate
    exit_fee_rate = config.FEE_TAKER if exit_fee_rate is None else exit_fee_rate

    risk_distance = abs(float(entry_price) - float(stop_price))
    if risk_distance <= 0:
        return {"gross_r": 0.0, "net_r": 0.0, "fees": 0.0, "pnl": 0.0}

    if direction == "BUY":
        move = float(exit_price) - float(entry_price)
    else:
        move = float(entry_price) - float(exit_price)

    pnl = move * float(quantity)
    fees = (float(entry_price) * float(quantity) * float(entry_fee_rate)
            + float(exit_price) * float(quantity) * float(exit_fee_rate))
    risk_amount = risk_distance * float(quantity)

    return {
        "gross_r": pnl / risk_amount if risk_amount else 0.0,
        "net_r": (pnl - fees) / risk_amount if risk_amount else 0.0,
        "fees": fees,
        "pnl": pnl - fees,
    }


def portfolio_from_exchange(rest, journal_stats=None):
    """Build PortfolioState from the exchange, with today's R from the journal.

    Exposure comes from positionRisk because that is the only source that cannot be
    stale. Realised R and the loss streak come from the trade journal, since the
    exchange reports PnL in quote currency and R requires each trade's own original
    stop distance - information only we hold.
    """
    state = PortfolioState()

    try:
        account = rest.account()
        state.equity = float(account.get("totalMarginBalance") or 0.0)
        state.available_balance = float(account.get("availableBalance") or 0.0)
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        log.error("account fetch failed: %s", exc)
        # OBSERVATION MODE NEEDS A NOTIONAL EQUITY, or it observes nothing. With
        # equity at 0 every candidate is refused by size() as "equity is zero", so a
        # credential-free observation run journals sizing failures instead of the
        # sized candidates it exists to record - and those rows cannot be compared
        # against a replay, which is the entire purpose of running it.
        #
        # GUARDED ON TRADING_ENABLED BEING FALSE, and that guard is the important
        # half. A notional equity must never size a real order: if trading IS enabled
        # and the account cannot be read, equity stays 0 and the system refuses to
        # trade, which is the only safe response to not knowing what it has.
        if not config.TRADING_ENABLED and config.PAPER_EQUITY > 0:
            state.equity = float(config.PAPER_EQUITY)
            state.available_balance = float(config.PAPER_EQUITY)
            state.notional_equity = True
            log.warning("observation mode: using notional equity %.2f for sizing "
                        "(no account access; nothing will be sent)",
                        config.PAPER_EQUITY)
        return state

    try:
        for row in rest.position_risk() or []:
            amount = float(row.get("positionAmt") or 0.0)
            if amount != 0:
                state.open_positions[(row.get("symbol") or "").upper()] = amount
    except Exception as exc:                      # noqa: BLE001
        log.error("positionRisk fetch failed: %s", exc)

    if journal_stats:
        state.realised_r_today = float(journal_stats.get("realised_r_today", 0.0))
        state.consecutive_losses = int(journal_stats.get("consecutive_losses", 0))
        state.last_loss_closed_at = journal_stats.get("last_loss_closed_at")

    return state
