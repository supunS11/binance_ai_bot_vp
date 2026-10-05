"""SetupContext: everything a detector is allowed to look at, assembled once.

WHY THIS EXISTS. Each detector needs the frozen prior profile, the developing
current profile, both level sets, the shape, the open relationship, the ATR, the
confirmation candles, maturity, and the naked-POC registry. Passing those
individually produces fifteen-argument functions whose call sites drift apart, and
- worse - lets one detector quietly reach for something another cannot see. A
single immutable context makes the input surface identical for every setup, which
is what makes the Phase 1 comparison between them meaningful.

WHY IT IS ALSO THE ANTI-LOOKAHEAD BOUNDARY. The context is built for a specific
`as_of` and every profile inside it was produced by profile_as_of() at that same
instant. A detector cannot see the future because the context does not contain it -
it has no access to the candle list, only to profiles already computed under the
as-of contract, plus confirmation candles that the builder has already filtered to
closed bars.

THE ONE UNIFORM RULE ACROSS ALL SETUPS: the reference entry price is the CLOSE OF
THE CANDLE THAT COMPLETED THE SETUP. Not the level itself. The source material
enters after confirmation, and confirmation is only known once a candle closes, so
the close is the first price at which the setup actually existed. Using the level
instead would backdate the entry to before the evidence arrived, which flatters
every backtest and is unreachable live. execution/router.py then converts this
reference into a passive limit price that is never WORSE than the reference.
"""
from dataclasses import dataclass, field

import config


@dataclass(frozen=True)
class SetupContext:
    symbol: str
    spec: object                      # exchange.symbols.SymbolSpec
    as_of: int
    session_id: str

    # --- frozen previous session: the reference levels for the whole day ---
    prior_profile: object = None
    prior_levels: object = None
    prior_shape: object = None
    prior_stability: object = None

    # --- developing current session ---------------------------------------
    dev_profile: object = None
    dev_levels: object = None
    dev_shape: object = None
    maturity: dict = field(default_factory=dict)
    # Visits this session to each prior-session HVN zone, aligned with prior_levels.hvns.
    hvn_tests: tuple = ()

    # --- session relationships --------------------------------------------
    open_relationship: object = None
    migration: object = None

    # --- measurement scale and price action -------------------------------
    atr: float = 0.0
    confirm_candles: tuple = ()       # confirmation timeframe, closed only
    session_candles: tuple = ()       # current session at source interval
    last_price: float = 0.0
    mark_price: float = 0.0

    # --- higher timeframe and cross-session context ------------------------
    naked_pocs: tuple = ()
    weekly_levels: object = None

    @property
    def reference_entry(self):
        """The close of the most recent confirmation candle.

        This is the reference entry for every setup - see the module docstring.
        Falls back to last_price only when no confirmation candle is available,
        which a detector should already have rejected on.
        """
        if self.confirm_candles:
            return self.confirm_candles[-1].close
        return self.last_price

    @property
    def confirm_candle(self):
        return self.confirm_candles[-1] if self.confirm_candles else None

    @property
    def prior_confirm_rate(self):
        """Yesterday's volume per CONFIRMATION candle - acceptance's fallback baseline.

        Acceptance needs a reference for "normal business per unit time" that is
        independent of the excursion being measured. This session's pre-excursion
        candles are the first choice; when the session opened outside value there are
        none, and this is what stands in. See acceptance._baseline_rate.

        Derived here rather than stored so it cannot fall out of step with
        prior_profile, and scaled from the profile's 1m source interval to the
        confirmation interval so the numerator and denominator count the same unit of
        time. Returns 0.0 when unavailable, which acceptance reports as NO_BASELINE
        rather than treating as thin volume.
        """
        profile = self.prior_profile
        if profile is None or not profile.candle_count or profile.total_volume <= 0:
            return 0.0
        from data.klines import INTERVAL_MS
        source_ms = INTERVAL_MS.get(profile.source_interval or "1m", 60_000)
        confirm_ms = INTERVAL_MS.get(config.CONFIRM_INTERVAL, 900_000)
        per_source_candle = profile.total_volume / profile.candle_count
        return per_source_candle * (confirm_ms / source_ms)

    def profile_row(self):
        """Flat snapshot of the auction context, stamped onto every candidate.

        Stored as raw measurements rather than pass/fail booleans so a later phase
        can re-sweep any threshold from stored rows instead of re-running the
        replay. A stored comparison cannot be re-swept; a stored value can.
        """
        row = {
            "session_id": self.session_id,
            "as_of": self.as_of,
            "atr": self.atr,
            "last_price": self.last_price,
        }
        if self.prior_levels is not None:
            row.update({
                "prior_poc": self.prior_levels.poc_price,
                "prior_vah": self.prior_levels.vah,
                "prior_val": self.prior_levels.val,
                "prior_value_width": self.prior_levels.value_width,
                "prior_value_fraction": self.prior_levels.value_fraction,
                "prior_vwap": self.prior_levels.vwap,
                "prior_poc_prominence": self.prior_levels.poc_prominence,
                "prior_hvn_count": len(self.prior_levels.hvns),
                "prior_lvn_count": len(self.prior_levels.lvns),
                "prior_delta_poc": self.prior_levels.delta_poc_price,
            })
        if self.prior_profile is not None:
            row.update({
                "prior_high": self.prior_profile.high,
                "prior_low": self.prior_profile.low,
                "prior_bin_size": self.prior_profile.bin_size,
                "prior_bin_count": self.prior_profile.bin_count,
                "prior_candle_count": self.prior_profile.candle_count,
                "prior_quote_volume": self.prior_profile.total_quote_volume,
            })
        if self.prior_shape is not None:
            row.update(self.prior_shape.as_row())
        if self.prior_stability is not None:
            row.update(self.prior_stability.as_row())
        if self.open_relationship is not None:
            row.update(self.open_relationship.as_row())
        if self.migration is not None:
            row.update(self.migration.as_row())
        if self.dev_levels is not None:
            row.update({
                "dev_poc": self.dev_levels.poc_price,
                "dev_vah": self.dev_levels.vah,
                "dev_val": self.dev_levels.val,
            })
        if self.maturity:
            row.update({
                "dev_elapsed_minutes": self.maturity.get("elapsed_minutes"),
                "dev_volume_fraction": self.maturity.get("volume_fraction"),
                "dev_mature": self.maturity.get("mature"),
            })
        row["naked_poc_count"] = len(self.naked_pocs)
        return row
