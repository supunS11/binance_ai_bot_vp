"""binance_ai_bot_vp entry point: the scan-to-order loop.

THE FULL PATH, once per cycle per symbol:

    scan ......... assemble the SetupContext (scanner.py) - frozen prior profile,
                   developing current profile, levels, shape, stability, open
                   relationship, confirmation candles. Everything as_of the last
                   CLOSED 1m candle.
    detect ....... the state machine runs only the setups the auction state makes
                   eligible, and arbitrates any genuine conflict (state_machine.py)
    gate ......... the setup's own gate profile, first-fail, mechanical gates on and
                   opinion gates off (gates/profiles.py)
    size ......... portfolio limits, then quantity from the STOP, not from leverage
                   (risk.py)
    place ........ passive entry, then stop, then target - in that order
                   (execution/router.py, execution/positions.py)

Every refusal along the way is journaled with its reason. Nothing declines silently.

ORDER OF OPERATIONS WITHIN A CYCLE, and why:

  1. RECONCILE FIRST, ALWAYS. Before looking for new trades, make sure every open
     position has a stop and every resting exit order has a position. Opening
     something new while an existing position is unprotected would be exactly the
     wrong priority.
  2. SESSION BOUNDARY next, so the rest of the cycle works from the correct frozen
     profile rather than yesterday's.
  3. THEN scan for entries, and only if the kill switch is clear.

WHAT THIS LOOP DOES NOT DO: it never closes a position to take a profit or cut a
loss. Exits belong to the stop and target orders resting at the venue, which continue
to work if this process dies. The only automatic close in the system is
PositionManager's last-resort exit for a position it cannot protect.
"""
import argparse
import logging
import sys
import time

import config
import risk
import scanner as scanner_mod
import sessions
import state_machine as state_machine_mod
from data.store import KlineCache
from data.ws_feed import KlineFeed
from exchange.rest import RestClient
from exchange.symbols import SymbolCatalog
from execution.positions import PositionManager
from execution.router import OrderRouter, spread_bps
from gates import profiles as gate_profiles
from journal.sinks import Journal
from ops import runtime
from setups import orderflow_reversal as ofr
from setups import zone_watch as zone_watch_mod

log = logging.getLogger(__name__)


def _seconds_until_next_cycle(started, now=None):
    """Next cycle start. With the zone watch on, cycles align to 1-minute candle closes,
    so a closed candle is processed seconds after it closes, not up to a scan interval later."""
    now = time.time() if now is None else now
    if not config.ZONE_WATCH_ENABLED:
        return max(1.0, config.SCAN_INTERVAL_SECONDS - (now - started))
    due = (now // 60 + 1) * 60 + config.ZONE_WATCH_CLOSE_DELAY_SECONDS
    return max(1.0, due - now)


class VolumeProfileBot:
    def __init__(self):
        self.rest = RestClient()
        self.catalog = SymbolCatalog(self.rest)
        self.cache = KlineCache(self.rest)
        self.feed = KlineFeed(self.cache.ingest) if config.WS_ENABLED else None
        self.journal = Journal()
        self.router = OrderRouter(self.rest, self.catalog, self.journal)
        self.positions = PositionManager(self.rest, self.catalog, self.router,
                                         self.journal)
        self.scanner = scanner_mod.Scanner(self.rest, self.catalog, self.cache,
                                          self.journal)
        self.state_machine = state_machine_mod.StateMachine(self.journal)
        self.state_store = runtime.StateStore()
        self.zone_watch = zone_watch_mod.ZoneWatch(self.state_store)
        self.kill_switch = runtime.KillSwitch()

        self._session_id = sessions.session_id(sessions.now_ms())
        self._cycle = 0
        self._safe_to_trade = False

    # ------------------------------------------------------------------ setup

    def start(self):
        log.info("=" * 78)
        log.info("%s starting", config.BOT_NAME)
        log.info("  venue        : %s", self.rest.base)
        log.info("  trading      : %s", "ENABLED" if config.TRADING_ENABLED
                 else "DISABLED (scan and journal only)")
        log.info("  session      : %s", sessions.describe())
        log.info("  config hash  : %s", config.config_hash())
        log.info("  risk/trade   : %.3f%%  leverage %sx  %s",
                 config.RISK_PCT * 100, config.LEVERAGE, config.MARGIN_TYPE)
        log.info("  target mode  : %s (%.1fR)", config.TARGET_MODE,
                 config.TARGET_FIXED_R)
        log.info("  arbitration  : %s / S1 %s", config.ARBITER_MODE,
                 config.S1_ARBITRATION)
        log.info("  gate profiles:\n%s", gate_profiles.describe())
        log.info("=" * 78)

        self.rest.sync_time()
        self.catalog.refresh(force=True)
        self.zone_watch.restore()

        # Recovery gates trading, not scanning: an unexplained position means stop
        # placing orders, but keeping the profile pipeline running is harmless and
        # keeps the journal continuous.
        self._safe_to_trade = runtime.recover(self.rest, self.positions,
                                              self.state_store)
        if not self._safe_to_trade:
            log.error("starting in SCAN-ONLY mode until state is reconciled")

    # ------------------------------------------------------------------- loop

    def run(self):
        self.start()
        try:
            while True:
                started = time.time()
                try:
                    self.cycle()
                except KeyboardInterrupt:
                    raise
                except Exception:                 # noqa: BLE001
                    log.exception("cycle failed - continuing")

                time.sleep(_seconds_until_next_cycle(started))
        except KeyboardInterrupt:
            log.info("interrupted - shutting down")
        finally:
            self.shutdown()

    def cycle(self):
        self._cycle += 1
        now = sessions.now_ms()

        # 1. Advance entries in flight FIRST, every cycle, unconditionally. A fill
        #    that has not yet been protected is the most time-critical state in the
        #    system - ahead of reconciliation and far ahead of any new opportunity.
        #    Never gated on RECONCILE_EVERY_CYCLES for that reason.
        #    record_taken is NOT called here: try_open already recorded the setup when
        #    the entry was placed. Recording it again on fill would double-count it
        #    against MAX_SETUPS_PER_SESSION_PER_SYMBOL.
        for managed in self.positions.advance_pending():
            self.state_store.save_position(managed)

        # 2. Reconcile. An unprotected position outranks any opportunity.
        if self._cycle % max(1, config.RECONCILE_EVERY_CYCLES) == 0:
            self.positions.reconcile()
            for symbol, managed in self.positions.managed().items():
                self.state_store.save_position(managed)

        # 3. Session boundary.
        current_session = sessions.session_id(now)
        if current_session != self._session_id:
            log.info("session boundary: %s -> %s", self._session_id, current_session)
            self._session_id = current_session
            self.state_machine.reset_session(current_session)
            self.cache.clear_frozen()
            self.positions.on_session_boundary(current_session)

        # 4. Entries.
        scanned = contexts = candidates_found = 0

        if self.kill_switch.engaged:
            log.warning("KILL SWITCH engaged (%s) - managing positions only",
                        self.kill_switch.reason())
        else:
            scanned, contexts, candidates_found = self.scan_for_entries()

        if self._cycle % max(1, config.HEARTBEAT_EVERY_CYCLES) == 0:
            portfolio = risk.portfolio_from_exchange(
                self.rest, self.journal.stats_today())
            runtime.heartbeat(self.journal, self.state_machine, self.cache,
                              portfolio, self.rest.limiter.snapshot(),
                              scanned=scanned, contexts=contexts,
                              candidates=candidates_found)

    # --------------------------------------------------------------- scanning

    def scan_for_entries(self):
        """One pass over the trade universe. Returns (scanned, contexts, candidates)."""
        universe = self.scanner.trade_universe()
        if self.feed is not None and universe:
            self.feed.set_symbols([row["symbol"] for row in universe])
        if not universe:
            log.warning("trade universe is empty")
            return 0, 0, 0

        portfolio = risk.portfolio_from_exchange(self.rest,
                                                self.journal.stats_today())

        scanned = contexts = found = 0

        for row in universe:
            symbol = row["symbol"]
            scanned += 1

            # Skip symbols already holding a position before doing any work: one
            # position per symbol is structural, so the scan would be wasted.
            if self.positions.has_position(symbol) or portfolio.has_position(symbol):
                continue

            ctx, reason = self.scanner.build_context(symbol)
            if ctx is None:
                log.debug("%s not scannable: %s", symbol, reason)
                continue
            contexts += 1

            if config.ZONE_WATCH_ENABLED:
                candidate, rejections = self._watch_step(ctx)
            else:
                candidate, rejections = self.state_machine.evaluate(ctx)

            if rejections:
                self.journal.record_rejects(
                    rejections, session_id=ctx.session_id,
                    profile_snapshot=ctx.profile_row())

            if candidate is None:
                continue
            found += 1

            self.try_open(candidate, ctx, portfolio)

            # Re-read the portfolio: a fill changes the limits for every symbol after
            # this one in the pass. Using a stale snapshot would let a single cycle
            # open more positions than MAX_CONCURRENT_POSITIONS allows.
            portfolio = risk.portfolio_from_exchange(
                self.rest, self.journal.stats_today())

        return scanned, contexts, found

    def _watch_step(self, ctx):
        """The zone watch in place of evaluate(). Refusals are journaled as evaluate's are."""
        refusal = self.state_machine.admission(ctx, ofr.SETUP)
        if refusal is not None:
            return None, [refusal]
        candidate, rejections = self.zone_watch.observe(ctx)
        return candidate, rejections

    def try_open(self, candidate, ctx, portfolio):
        """Gate, size, and place one candidate. Journals every refusal."""
        snapshot = ctx.profile_row()

        # OBSERVATION MODE. Trading disabled, or state not yet reconciled: the
        # candidate is still gated, still sized, and still journaled - it is simply not
        # sent. That distinction is the whole value of the mode. A run that refuses
        # before sizing records only "the flag is off", which cannot be compared with a
        # replay of the same sessions; a run that sizes and journals produces exactly
        # the rows Phase 1 needs to verify that live and replay agree.
        paper = not (config.TRADING_ENABLED and self._safe_to_trade)

        # Live book: needed for the spread gate and for passive entry pricing.
        book = {}
        spread = None
        if not paper:
            try:
                book = self.rest.book_ticker(candidate.symbol) or {}
                spread = spread_bps(book)
            except Exception as exc:              # noqa: BLE001
                log.warning("%s book fetch failed: %s", candidate.symbol, exc)

        rejection = gate_profiles.evaluate(candidate, ctx, spread_bps=spread)
        if rejection is not None:
            self.journal.record_reject(rejection, session_id=ctx.session_id,
                                       profile_snapshot=snapshot)
            return

        rejection = risk.check_limits(candidate, portfolio, paper=paper)
        if rejection is not None:
            self.journal.record_reject(rejection, session_id=ctx.session_id,
                                       profile_snapshot=snapshot)
            return

        rejection = risk.size(candidate, portfolio, ctx.spec)
        if rejection is not None:
            self.journal.record_reject(rejection, session_id=ctx.session_id,
                                       profile_snapshot=snapshot)
            return

        if paper:
            log.info("%s %s %s WOULD TRADE qty=%.10g entry=%.8g stop=%.8g "
                     "target=%.8g (%.2fR) - observation mode, nothing sent",
                     candidate.symbol, candidate.setup, candidate.direction,
                     candidate.quantity, candidate.entry_price,
                     candidate.stop_price, candidate.target_price,
                     candidate.r_multiple)
            self.journal.record_setup(candidate)
            return

        managed, pending, rejection = self.positions.open(candidate, book, portfolio)
        if rejection is not None:
            self.journal.record_reject(rejection, session_id=ctx.session_id,
                                       profile_snapshot=snapshot)
            return

        # The setup counts as TAKEN the moment its entry is placed, filled or not.
        # Counting only fills would let one setup be re-entered repeatedly while its
        # first order rested, defeating MAX_SETUPS_PER_SESSION_PER_SYMBOL.
        self.state_machine.record_taken(candidate.symbol, ctx.session_id,
                                        candidate.setup, candidate.level_price)
        if managed is not None:
            self.state_store.save_position(managed)

    # --------------------------------------------------------------- shutdown

    def shutdown(self):
        """Persist state and close handles. Orders are deliberately LEFT RESTING.

        The stop and target live at the venue and keep working whether this process is
        running or not. Cancelling them on shutdown would turn a routine restart into
        an unprotected position, which is the opposite of safe.
        """
        log.info("shutting down: %d managed position(s) left protected at the venue",
                 len(self.positions.managed()))
        for managed in self.positions.managed().values():
            self.state_store.save_position(managed)
        if self.feed is not None:
            self.feed.stop()
        self.state_store.close()
        self.journal.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="binance_ai_bot_vp")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit (smoke test)")
    parser.add_argument("--symbol", help="scan one symbol and print its context")
    args = parser.parse_args(argv)

    runtime.setup_logging()

    bot = VolumeProfileBot()

    if args.symbol:
        bot.start()
        ctx, reason = bot.scanner.build_context(args.symbol.upper())
        if ctx is None:
            print(f"{args.symbol}: not scannable ({reason})")
            return 1
        print(f"\n{args.symbol} context as_of {ctx.as_of}")
        for key, value in sorted(ctx.profile_row().items()):
            print(f"  {key:32} {value}")
        candidate, rejections = bot.state_machine.evaluate(ctx)
        print(f"\ncandidate: {candidate.setup if candidate else 'none'}")
        for rejection in rejections:
            print(f"  reject {rejection.setup:8} {rejection.reason.value:28} "
                  f"{rejection.detail}")
        return 0

    if args.once:
        bot.start()
        bot.cycle()
        bot.shutdown()
        return 0

    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
