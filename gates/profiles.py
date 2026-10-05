"""Per-setup gate profiles: which gates apply to which setup, and in what order.

GATES ARE DECLARED PER SETUP, NEVER GLOBALLY. A gate validated on one setup's
population is not validated for another's - the populations differ, and so can the
sign of the effect. Declaring the mapping as data rather than as `if setup == ...`
scattered through the code makes the whole policy readable in one place and
diffable in review.

ORDER IS THE FUNNEL. Gates run cheapest-and-most-general first, so the reject
reason recorded is the EARLIEST one that applies. That makes the reject
distribution a funnel - profile quality, then context, then geometry, then venue -
rather than an arbitrary slice of whichever gate happened to run first. A funnel is
analysable; an arbitrary ordering is not.

WHAT THE DEFAULT-ON SET HAS IN COMMON: every one is a statement about whether the
MEASUREMENT is trustworthy or the ARITHMETIC is sound. None expresses a view on
direction. Every opinion-bearing gate ships off.
"""
from gates import checks

# Mechanical, apply to every setup. Order is the funnel.
_COMMON = (
    checks.profile_thin,
    checks.value_area_degenerate,
    checks.direction_geometry,
    checks.stop_too_tight,
    checks.stop_too_wide,
    checks.reward_below_minimum,
    checks.funding_window,
    checks.venue_filters,
)

# Opinion-bearing, all default OFF via their own config flags. Listed for every
# setup so that enabling a flag takes effect without editing this table - the flag
# is the single control, which is what makes a Phase 3 ablation a config sweep
# rather than a code change.
_OPINION = (
    checks.shape_opposed,
    checks.delta_opposed,
    checks.delta_opposed_multibin,
    checks.htf_value_opposed,
    checks.stop_inside_hvn,
    checks.target_behind_hvn,
)

GATE_PROFILES = {
    # S1 stakes a stop on the POC, so POC quality is decisive. It reads only the
    # FROZEN prior profile, so developing maturity is irrelevant.
    "S1-POC": (
        checks.profile_thin,
        checks.poc_unstable,
        checks.poc_not_prominent,
        checks.value_area_degenerate,
        checks.direction_geometry,
    checks.stop_too_tight,
        checks.stop_too_wide,
        checks.reward_below_minimum,
        checks.funding_window,
        checks.venue_filters,
    ) + _OPINION,

    # S1-LVN also needs POC quality - the POC is its TARGET, so an unstable POC
    # means an unstable target rather than an unstable stop.
    "S1-LVN": (
        checks.profile_thin,
        checks.poc_unstable,
        checks.poc_not_prominent,
        checks.value_area_degenerate,
        checks.direction_geometry,
    checks.stop_too_tight,
        checks.stop_too_wide,
        checks.reward_below_minimum,
        checks.funding_window,
        checks.venue_filters,
    ) + _OPINION,

    # S2 and S3 stake everything on the value BOUNDS, and both read the developing
    # profile - S2 for the excursion, S3 for POC migration - so both need maturity.
    "S2-VAR": (
        checks.profile_thin,
        checks.value_bounds_unstable,
        checks.value_area_degenerate,
        checks.developing_immature,
        checks.direction_geometry,
    checks.stop_too_tight,
        checks.stop_too_wide,
        checks.reward_below_minimum,
        checks.funding_window,
        checks.venue_filters,
    ) + _OPINION,

    "S3-BRK": (
        checks.profile_thin,
        checks.value_bounds_unstable,
        checks.value_area_degenerate,
        checks.developing_immature,
        checks.direction_geometry,
    checks.stop_too_tight,
        checks.stop_too_wide,
        checks.reward_below_minimum,
        checks.funding_window,
        checks.venue_filters,
    ) + _OPINION,
}


def gates_for(setup):
    """Gate sequence for a setup, falling back to the mechanical common set.

    An unknown setup gets the common gates rather than none. Defaulting to no gates
    would mean a newly added setup trades ungated until someone remembers this
    table - the failure mode is silent and expensive, so the default is the safe
    direction.
    """
    return GATE_PROFILES.get(setup, _COMMON + _OPINION)


def evaluate(candidate, ctx, spread_bps=None, extra=()):
    """Run the setup's gates in order. Returns the first Rejection, or None.

    First-fail rather than collect-all is deliberate and must be remembered when
    analysing the reject journal: a row saying PROFILE_THIN does not mean the other
    gates would have passed, only that this one failed first. That is what makes the
    ordering above part of the design rather than a detail.
    """
    for gate in tuple(gates_for(candidate.setup)) + tuple(extra):
        rejection = gate(candidate, ctx)
        if rejection is not None:
            return rejection

    if spread_bps is not None:
        rejection = checks.spread_too_wide(candidate, ctx, spread_bps=spread_bps)
        if rejection is not None:
            return rejection

    return None


def describe():
    """Human-readable gate table, logged at startup so the active policy is on the
    record for every run rather than inferred from the config later."""
    lines = []
    for setup, gates in sorted(GATE_PROFILES.items()):
        names = ", ".join(gate.__name__ for gate in gates)
        lines.append(f"  {setup}: {names}")
    return "\n".join(lines)
