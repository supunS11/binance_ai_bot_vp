"""The per-symbol, per-session auction state machine.

WHY THIS IS THE CENTRE OF THE SYSTEM AND NOT A TIE-BREAKER. S2 and S3 trade the same
location in opposite directions. Resolving that with a static priority list would be
the worst available decision: whichever setup sat higher would silently win every
contested move, and the reject journal would show the loser as "suppressed" without
ever revealing that the contest was decided by an arbitrary ordering rather than by
the market.

The auction state resolves it properly, in two layers.

LAYER 1 - ELIGIBILITY FROM THE OPEN RELATIONSHIP. Decided once per session, the
moment the boundary passes:

    INSIDE_VALUE                 {S2-VAR, S3-BRK}
    OUTSIDE_VALUE_INSIDE_RANGE   {S1-POC, S1-LVN, S3-BRK}
    OUTSIDE_RANGE                {S3-BRK} only

S1 and S2 are therefore NEVER simultaneously eligible - the worst conflict is removed
by construction rather than by arbitration. And opening beyond the entire prior range
is an imbalance statement, so mean reversion is not offered there at all.

LAYER 2 - ACCEPTANCE RESOLVES THE REMAINING PAIR. S2 requires LITTLE volume outside
value plus a close back inside; S3 requires MUCH volume outside plus continuation.
With a deliberate gap between the two thresholds, the conditions are mutually
exclusive, so they cannot both produce a candidate from the same data. Verified by
tests rather than assumed.

WHAT IS LEFT TO ARBITRATE is narrow and honest: S1-POC against S1-LVN, which can both
fire on one session when a thin area sits close to the POC. That pair is the Phase 1
comparison, so in research BOTH are recorded and in live exactly one is taken.

EVERY SUPPRESSION IS JOURNALED with the reason and the winner. Without that record, a
systematic bias in how conflicts resolve would be invisible in the results - you
would only ever see the trades the machine chose, never the population it chose from.
"""
import logging
from dataclasses import dataclass, field

import config
from setups import lvn_rejection, orderflow_reversal, poc_rotation, va_breakout, va_reversion
from setups.base import RejectReason, reject

# Per-setup session caps, read through callables so a test (or a live reload) that
# changes the config value takes effect without re-importing this module.
_PER_SETUP_CAPS = {
    "S2-VAR": lambda: config.S2_MAX_PER_SESSION,
}

log = logging.getLogger(__name__)

# Auction states, in the order a session moves through them.
OPENING = "OPENING"
BALANCE = "BALANCE"
EXCURSION = "EXCURSION"
DISCOVERY = "DISCOVERY"        # excursion accepted - imbalance confirmed
REVERTED = "REVERTED"          # excursion rejected - balance reasserted

ELIGIBILITY = {
    "INSIDE_VALUE": ("S2-VAR", "S3-BRK", "S4-OFR"),
    "OUTSIDE_VALUE_INSIDE_RANGE": ("S1-POC", "S1-LVN", "S3-BRK", "S4-OFR"),
    "OUTSIDE_RANGE": ("S3-BRK",),
}

DETECTORS = {
    "S1-POC": poc_rotation.detect,
    "S1-LVN": lvn_rejection.detect,
    "S2-VAR": va_reversion.detect,
    "S3-BRK": va_breakout.detect,
    "S4-OFR": orderflow_reversal.detect,
}


@dataclass
class SymbolState:
    """Per-symbol state for one session. Persisted on every transition."""
    symbol: str
    session_id: str
    auction_state: str = OPENING
    open_relationship: str = ""
    eligible: tuple = ()
    setups_taken: int = 0
    setups_by_type: dict = field(default_factory=dict)
    last_excursion_side: str = ""
    invalidated: list = field(default_factory=list)
    s4_zone_prices: list = field(default_factory=list)

    def can_take_another(self):
        """Cap re-entries. Uncapped, "keep looking for setups while the session
        lasts" is an unbounded loop that behaves like revenge trading on a bad day."""
        return self.setups_taken < config.MAX_SETUPS_PER_SESSION_PER_SYMBOL

    def can_take(self, setup):
        """The global cap AND this setup's own per-session cap.

        S2 gets its own limit because repeated fades of the same excursion is the
        specific failure mode of a mean-reversion setup: each individual fade looks
        like a fresh signal, while together they are one increasingly wrong opinion
        about the same move, averaged into at ever worse prices.

        S2_MAX_PER_SESSION existed in config and in .env.example and was never read by
        anything, which is worse than having no cap at all - an operator setting it to 1
        would reasonably believe S2 was limited when it was not. A knob that appears to
        control risk and does not is a safety problem, not untidiness.
        """
        if not self.can_take_another():
            return False, (f"{self.setups_taken} setups already taken this session, "
                           f"cap {config.MAX_SETUPS_PER_SESSION_PER_SYMBOL}")

        per_setup = _PER_SETUP_CAPS.get(setup)
        if per_setup is not None:
            limit = per_setup()
            taken = self.setups_by_type.get(setup, 0)
            if taken >= limit:
                return False, (f"{taken} {setup} already taken this session, "
                               f"cap {limit}")
        return True, ""

    def record_taken(self, setup, level_price=None):
        self.setups_taken += 1
        self.setups_by_type[setup] = self.setups_by_type.get(setup, 0) + 1
        if setup == "S4-OFR" and level_price is not None:
            self.s4_zone_prices.append(level_price)

    def as_row(self):
        return {
            "symbol": self.symbol,
            "session_id": self.session_id,
            "auction_state": self.auction_state,
            "open_relationship": self.open_relationship,
            "eligible": ",".join(self.eligible),
            "setups_taken": self.setups_taken,
        }


class StateMachine:
    """Holds SymbolState for every symbol and runs the eligible detectors."""

    def __init__(self, journal=None):
        self._states = {}
        self._journal = journal

    # ------------------------------------------------------------- lifecycle

    def begin_session(self, symbol, session_id, open_relationship):
        """Classify the session once, at the boundary, and fix eligibility.

        Eligibility is decided here rather than re-derived per cycle so it cannot
        drift mid-session as price moves. The open relationship is a statement about
        how the session STARTED, and re-deriving it later would quietly turn it into
        a statement about where price happens to be now.
        """
        label = getattr(open_relationship, "label", "INSIDE_VALUE")
        state = SymbolState(
            symbol=symbol.upper(),
            session_id=session_id,
            auction_state=OPENING,
            open_relationship=label,
            eligible=ELIGIBILITY.get(label, ("S3-BRK",)),
        )
        self._states[symbol.upper()] = state
        return state

    def state_for(self, symbol, session_id, open_relationship=None):
        """Current state, starting a new session's state when the id changes."""
        key = symbol.upper()
        state = self._states.get(key)
        if state is None or state.session_id != session_id:
            return self.begin_session(key, session_id, open_relationship)
        return state

    def reset_session(self, session_id):
        """Drop every symbol's state at the boundary.

        Pending candidates die with it, deliberately: their levels reference a profile
        that is no longer the current reference. Open POSITIONS are owned by
        PositionManager and are untouched here.
        """
        dropped = len(self._states)
        self._states.clear()
        log.info("state machine reset for %s (%s symbol states dropped)",
                 session_id, dropped)

    # ------------------------------------------------------------ evaluation

    def evaluate(self, ctx, research_mode=False):
        """Run eligible detectors and arbitrate. Returns (candidate, rejections).

        `rejections` contains EVERY refusal from this evaluation, including
        suppressions, and the caller journals all of them. In research_mode the
        arbitration step is skipped and all candidates are returned, because Phase 1
        needs to measure the setups independently rather than measure the arbiter.
        """
        state = self.state_for(ctx.symbol, ctx.session_id, ctx.open_relationship)
        rejections = []

        # Ineligible setups are recorded as rejects rather than skipped silently.
        # The rate at which each setup is ineligible is a real measurement - it says
        # how often each auction regime actually occurs.
        for setup in DETECTORS:
            if setup not in state.eligible:
                rejections.append(reject(
                    RejectReason.OPEN_RELATIONSHIP_WRONG, setup, ctx.symbol,
                    detail=(f"not eligible under open={state.open_relationship} "
                            f"(eligible: {','.join(state.eligible)})"),
                ))

        if not state.can_take_another():
            for setup in state.eligible:
                rejections.append(reject(
                    RejectReason.SESSION_SETUP_LIMIT, setup, ctx.symbol,
                    detail=(f"{state.setups_taken} setups already taken this "
                            f"session, cap {config.MAX_SETUPS_PER_SESSION_PER_SYMBOL}"),
                ))
            return None, rejections

        candidates = []
        for setup in sorted(state.eligible, key=lambda name: name != "S4-OFR"):
            if setup not in config.ACTIVE_SETUPS:
                rejections.append(reject(RejectReason.SETUP_DISABLED, setup, ctx.symbol,
                                         detail="not in ACTIVE_SETUPS"))
                continue

            # The per-setup cap is checked BEFORE the detector runs, so a capped setup
            # costs nothing and its refusal is recorded as a cap rather than as whatever
            # the detector would have said about the market.
            allowed, why = state.can_take(setup)
            if not allowed:
                rejections.append(reject(RejectReason.SESSION_SETUP_LIMIT, setup,
                                         ctx.symbol, detail=why))
                continue

            result = DETECTORS[setup](ctx)
            if (not research_mode and setup == "S2-VAR" and not result.is_rejection
                    and self._zone_claimed_by_s4(state, result, candidates)):
                result = reject(RejectReason.ZONE_CLAIMED_BY_S4, setup, ctx.symbol,
                                direction=result.direction, level_price=result.level_price,
                                detail="same zone as S4-OFR, which has priority")
            if result.is_rejection:
                rejections.append(result)
            else:
                candidates.append(result)

        if not candidates:
            return None, rejections

        self._advance_state(state, candidates, ctx)

        if research_mode:
            return candidates, rejections

        winner, suppressed = self._arbitrate(candidates, ctx)
        rejections.extend(suppressed)
        return winner, rejections

    def admission(self, ctx, setup):
        """The refusal a setup would meet in evaluate(), or None when it may be detected.

        For setups that are driven outside evaluate() - the zone watch - so they obey the same
        session eligibility, activation and per-session caps.
        """
        state = self.state_for(ctx.symbol, ctx.session_id, ctx.open_relationship)
        if setup not in state.eligible:
            return reject(RejectReason.OPEN_RELATIONSHIP_WRONG, setup, ctx.symbol,
                          detail=(f"not eligible under open={state.open_relationship} "
                                  f"(eligible: {','.join(state.eligible)})"))
        if setup not in config.ACTIVE_SETUPS:
            return reject(RejectReason.SETUP_DISABLED, setup, ctx.symbol,
                          detail="not in ACTIVE_SETUPS")
        allowed, why = state.can_take(setup)
        if not allowed:
            return reject(RejectReason.SESSION_SETUP_LIMIT, setup, ctx.symbol, detail=why)
        return None

    @staticmethod
    def _zone_claimed_by_s4(state, candidate, this_tick):
        """S4-OFR has priority on a shared zone: S2-VAR is refused where S4 traded or fired."""
        tol = config.OFR_TOUCH_TOL_ATR * candidate.atr
        s4_prices = state.s4_zone_prices + [c.level_price for c in this_tick
                                            if c.setup == "S4-OFR"]
        return any(abs(price - candidate.level_price) <= tol for price in s4_prices)

    def _advance_state(self, state, candidates, ctx):
        """Move the auction state to reflect what the detectors just observed.

        Derived from the candidates rather than tracked independently, so the recorded
        state can never disagree with the setups that fired.
        """
        setups = {candidate.setup for candidate in candidates}
        if "S3-BRK" in setups:
            state.auction_state = DISCOVERY
        elif "S2-VAR" in setups:
            state.auction_state = REVERTED
        elif setups:
            state.auction_state = BALANCE

    def _arbitrate(self, candidates, ctx):
        """Pick one candidate. Returns (winner, [Rejection] for the rest).

        Only ever called with a genuine conflict, because eligibility and acceptance
        have already removed the structural ones. In practice that means S1-POC
        against S1-LVN.
        """
        if len(candidates) == 1:
            return candidates[0], []

        winner = self._prefer(candidates)
        suppressed = [
            reject(RejectReason.SUPPRESSED_BY_ARBITER, candidate.setup, ctx.symbol,
                   direction=candidate.direction, level_price=candidate.level_price,
                   detail=(f"suppressed in favour of {winner.setup} "
                           f"(policy {config.S1_ARBITRATION})"),
                   winner=winner.setup,
                   winner_net_r=round(winner.net_r_at_target(), 4),
                   own_net_r=round(candidate.net_r_at_target(), 4))
            for candidate in candidates if candidate is not winner
        ]
        return winner, suppressed

    @staticmethod
    def _prefer(candidates):
        """Resolve S1-POC vs S1-LVN under the configured policy.

        The default prefers S1-LVN because auction theory predicts it: an LVN is
        where a reaction is mechanically likely, whereas the POC is where price
        rotates. That is a PREDICTION, not a result - Phase 1's head-to-head decides
        it, and this default is stated as a reasoned prior so that a later change is
        visibly evidence-driven rather than a quiet preference.

        NET_R breaks any remaining tie: between two otherwise equal candidates, the
        one with more reward per unit of risk after fees is the better trade on the
        only metric that pays.
        """
        by_setup = {candidate.setup: candidate for candidate in candidates}

        policy = config.S1_ARBITRATION.upper()
        if policy == "LVN_FIRST":
            order = ("S1-LVN", "S1-POC", "S3-BRK", "S2-VAR")
        elif policy == "POC_FIRST":
            order = ("S1-POC", "S1-LVN", "S3-BRK", "S2-VAR")
        else:
            order = ()

        for setup in order:
            if setup in by_setup:
                return by_setup[setup]

        return max(candidates, key=lambda candidate: candidate.net_r_at_target())

    # -------------------------------------------------------------- recording

    def record_taken(self, symbol, session_id, setup, level_price=None):
        state = self._states.get(symbol.upper())
        if state is not None and state.session_id == session_id:
            state.record_taken(setup, level_price)

    def snapshot(self):
        """All symbol states, for the heartbeat and for crash recovery."""
        return {symbol: state.as_row() for symbol, state in self._states.items()}

    def regime_tally(self):
        """How many symbols are in each auction regime right now.

        Genuinely informative rather than decorative: if almost every symbol reads
        OUTSIDE_RANGE then the whole market is trending and the near-absence of
        mean-reversion trades is correct behaviour, not a fault to investigate.
        """
        tally = {}
        for state in self._states.values():
            key = state.open_relationship or "UNKNOWN"
            tally[key] = tally.get(key, 0) + 1
        return tally
