"""Level stability: does a level survive being measured on a different ruler?

THE PROBLEM THIS SOLVES. A POC is an argmax over a histogram whose bin width we
chose. On a well-traded session with a genuine mode, that argmax is robust - widen
or narrow the bins by a third and the peak stays in the same place. On a thin or
choppy session it is not: the "POC" is whichever adjacent bin happened to
accumulate a few more units, and re-binning moves it somewhere else entirely.

Both cases produce a POC. Only the first one is a level. Nothing in the histogram
itself distinguishes them, which is why this check exists as a first-class engine
output rather than as an afterthought - a setup that stakes a stop on a level is
entitled to know whether that level is real.

HOW. Rebuild the profile at 0.75x and 1.33x the nominal bin width and measure how
far each level moves, expressed in NOMINAL bins so the numbers are comparable
across the perturbations. Multipliers either side of 1.0 are deliberate: a level
can be robust to coarsening (which merges bins and tends to preserve a peak) while
being fragile to refinement (which splits it), so testing only one direction
misses half the failures.

COST. Two extra profile builds per profile. At ~1440 candles that is a few
milliseconds, and it runs once per symbol-session for frozen profiles rather than
per cycle. Cheap enough that gating on it is free.
"""
from dataclasses import dataclass

import config
from profile import builder, levels as levels_mod


@dataclass
class Stability:
    poc_shift_bins: float
    vah_shift_bins: float
    val_shift_bins: float
    poc_prominence: float
    perturbations: int

    @property
    def poc_stable(self):
        return self.poc_shift_bins <= config.STABILITY_MAX_POC_SHIFT_BINS

    @property
    def value_bounds_stable(self):
        """Value bounds get a looser threshold than the POC, by design.

        A POC is an argmax and moves only when re-binning changes which bin is
        tallest. A value bound is where a cumulative sum crosses 70%, so it shifts a
        bin or two under almost any change in bin width. Holding both to the same
        tolerance rejects every real session's value area - see the measurements in
        config.STABILITY_MAX_VA_SHIFT_BINS.
        """
        return (self.vah_shift_bins <= config.STABILITY_MAX_VA_SHIFT_BINS
                and self.val_shift_bins <= config.STABILITY_MAX_VA_SHIFT_BINS)

    @property
    def poc_prominent(self):
        return self.poc_prominence >= config.POC_MIN_PROMINENCE

    def as_row(self):
        """Flat dict for the journal - stability is recorded on every candidate."""
        return {
            "poc_shift_bins": round(self.poc_shift_bins, 3),
            "vah_shift_bins": round(self.vah_shift_bins, 3),
            "val_shift_bins": round(self.val_shift_bins, 3),
            "poc_prominence": round(self.poc_prominence, 3),
            "poc_stable": self.poc_stable,
            "value_bounds_stable": self.value_bounds_stable,
            "poc_prominent": self.poc_prominent,
        }


def assess(symbol, candles, window_start, window_end, as_of, bin_size,
           base_levels, multipliers=None, value_area_pct=None):
    """Measure how far POC/VAH/VAL move when the bin width changes.

    `base_levels` is the level set at the nominal bin width, passed in rather
    than recomputed so this function cannot disagree with the profile the rest of
    the system is using.

    Shifts are reported in nominal-bin units: a 0.75x rebuild has narrower bins,
    so a one-bin move there is a smaller price move than a one-bin move at 1.33x,
    and converting both to price-then-nominal-bins is what makes them comparable.
    A profile that cannot be rebuilt at some multiplier (no volume, degenerate
    width) contributes no measurement rather than a zero, since a zero would read
    as perfect stability.
    """
    multipliers = (config.STABILITY_BIN_MULTIPLIERS if multipliers is None
                   else multipliers)
    value_area_pct = (config.VALUE_AREA_PCT if value_area_pct is None
                      else value_area_pct)

    poc_shift = vah_shift = val_shift = 0.0
    measured = 0

    for multiplier in multipliers:
        perturbed_size = bin_size * float(multiplier)
        if perturbed_size <= 0:
            continue

        perturbed = builder.profile_as_of(
            symbol, candles, window_start, window_end, as_of, perturbed_size,
            coverage=1.0,
        )
        if not perturbed.volume:
            continue

        perturbed_levels = levels_mod.compute(perturbed, value_area_pct=value_area_pct)
        if perturbed_levels is None:
            continue

        measured += 1
        poc_shift = max(poc_shift,
                        abs(perturbed_levels.poc_price - base_levels.poc_price) / bin_size)
        vah_shift = max(vah_shift,
                        abs(perturbed_levels.vah - base_levels.vah) / bin_size)
        val_shift = max(val_shift,
                        abs(perturbed_levels.val - base_levels.val) / bin_size)

    return Stability(
        poc_shift_bins=poc_shift,
        vah_shift_bins=vah_shift,
        val_shift_bins=val_shift,
        poc_prominence=base_levels.poc_prominence,
        perturbations=measured,
    )
