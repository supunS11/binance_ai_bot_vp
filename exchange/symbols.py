"""Symbol specifications parsed from exchangeInfo, cached, and exposed as one
immutable object per symbol.

Every filter the venue will enforce is pulled out once and kept together, so no
call site ever has to reach into a raw filter list. The alternative - looking up
`filters[0]["tickSize"]` where it is needed - breaks silently when Binance
reorders the array, which it does.

Only PERPETUAL contracts in TRADING status are returned. Delivery futures have
an expiry and a settlement mechanic this system does not model, and a symbol in
any other status (SETTLING, PENDING_TRADING, BREAK) will accept a profile
computation but reject an order, which is the worst possible ordering.
"""
import time
from dataclasses import dataclass, field

import config


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    contract_type: str

    # PRICE_FILTER
    tick_size: str = "0"
    min_price: str = "0"
    max_price: str = "0"

    # LOT_SIZE
    step_size: str = "0"
    min_qty: str = "0"
    max_qty: str = "0"

    # MARKET_LOT_SIZE - genuinely different bounds for market orders, because a
    # market order eats the book. Defaults fall back to LOT_SIZE when absent.
    market_step_size: str = "0"
    market_min_qty: str = "0"
    market_max_qty: str = "0"

    # MIN_NOTIONAL
    min_notional: str = "0"

    # PERCENT_PRICE - band around the mark inside which a resting order is legal
    multiplier_up: str = "0"
    multiplier_down: str = "0"

    # Order-count caps. Exceeding these returns -1015 and is easy to hit when a
    # stop and a target are maintained for every open position.
    max_num_orders: int = 0
    max_num_algo_orders: int = 0

    # Reported precisions, kept for validation cross-checks only. The lattice
    # from tick_size/step_size is authoritative - these two disagree with it on
    # some symbols, and the filter is what the matching engine enforces.
    price_precision: int = 0
    quantity_precision: int = 0

    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def tradable(self):
        return self.status == "TRADING" and self.contract_type == "PERPETUAL"


def _filter_of(filters, filter_type):
    for entry in filters:
        if entry.get("filterType") == filter_type:
            return entry
    return {}


def parse_symbol(item):
    """One exchangeInfo symbol entry -> SymbolSpec.

    MARKET_LOT_SIZE falls back to LOT_SIZE when the venue omits it, so callers
    can always read the market_* fields without a presence check.
    """
    filters = item.get("filters", []) or []
    price_f = _filter_of(filters, "PRICE_FILTER")
    lot_f = _filter_of(filters, "LOT_SIZE")
    mlot_f = _filter_of(filters, "MARKET_LOT_SIZE")
    notional_f = _filter_of(filters, "MIN_NOTIONAL")
    percent_f = _filter_of(filters, "PERCENT_PRICE")
    max_orders_f = _filter_of(filters, "MAX_NUM_ORDERS")
    max_algo_f = _filter_of(filters, "MAX_NUM_ALGO_ORDERS")

    step = lot_f.get("stepSize", "0")

    return SymbolSpec(
        symbol=item.get("symbol", "").upper(),
        base_asset=item.get("baseAsset", ""),
        quote_asset=item.get("quoteAsset", ""),
        status=item.get("status", ""),
        contract_type=item.get("contractType", ""),
        tick_size=price_f.get("tickSize", "0"),
        min_price=price_f.get("minPrice", "0"),
        max_price=price_f.get("maxPrice", "0"),
        step_size=step,
        min_qty=lot_f.get("minQty", "0"),
        max_qty=lot_f.get("maxQty", "0"),
        market_step_size=mlot_f.get("stepSize", step),
        market_min_qty=mlot_f.get("minQty", lot_f.get("minQty", "0")),
        market_max_qty=mlot_f.get("maxQty", lot_f.get("maxQty", "0")),
        # Binance labels this key "notional" inside a MIN_NOTIONAL filter on
        # USD-M; older docs say "minNotional". Accept either.
        min_notional=notional_f.get("notional", notional_f.get("minNotional", "0")),
        multiplier_up=percent_f.get("multiplierUp", "0"),
        multiplier_down=percent_f.get("multiplierDown", "0"),
        max_num_orders=int(max_orders_f.get("limit", 0) or 0),
        max_num_algo_orders=int(max_algo_f.get("limit", 0) or 0),
        price_precision=int(item.get("pricePrecision", 0) or 0),
        quantity_precision=int(item.get("quantityPrecision", 0) or 0),
        raw=item,
    )


class SymbolCatalog:
    """exchangeInfo, cached with a TTL.

    exchangeInfo carries real request weight and changes rarely - new listings,
    status transitions, occasional filter revisions - so refetching it per cycle
    wastes budget that the kline fetches need. An hour is short enough to pick
    up a delisting before it matters and long enough to be free.
    """

    def __init__(self, rest, ttl_seconds=3600):
        self._rest = rest
        self._ttl = ttl_seconds
        self._specs = {}
        self._fetched_at = 0.0

    def _stale(self):
        return not self._specs or (time.time() - self._fetched_at) >= self._ttl

    def refresh(self, force=False):
        if not force and not self._stale():
            return self._specs
        payload = self._rest.exchange_info()
        specs = {}
        for item in payload.get("symbols", []) or []:
            spec = parse_symbol(item)
            if spec.symbol:
                specs[spec.symbol] = spec
        if specs:
            self._specs = specs
            self._fetched_at = time.time()
        return self._specs

    def get(self, symbol):
        self.refresh()
        return self._specs.get(symbol.upper())

    def require(self, symbol):
        spec = self.get(symbol)
        if spec is None:
            raise KeyError(f"unknown symbol {symbol}")
        return spec

    def tradable_symbols(self):
        """Perpetuals in TRADING status, quoted in the configured asset.

        The denylist is applied here rather than at the scan loop so that a
        symbol excluded for operational reasons is excluded from research runs
        too - otherwise measurements include a population the bot cannot trade.
        """
        self.refresh()
        deny = set(config.SYMBOL_DENYLIST)
        return sorted(
            spec.symbol
            for spec in self._specs.values()
            if spec.tradable
            and spec.quote_asset == config.UNIVERSE_QUOTE_ASSET
            and spec.symbol not in deny
        )


def rank_by_quote_volume(tickers, catalog, limit):
    """Rank tradable symbols by 24h quote volume, descending.

    The rank is RETURNED ALONGSIDE the symbol and must be recorded by callers.
    This matters for research integrity: ranking by *current* volume means a
    universe fetched today is not the universe fetched last week, so an
    in-sample/held-out split built from two separate ranked fetches silently
    overlaps. One fetch, ranks recorded, split by rank afterwards.
    """
    tradable = set(catalog.tradable_symbols())
    rows = []
    for ticker in tickers:
        symbol = (ticker.get("symbol") or "").upper()
        if symbol not in tradable:
            continue
        try:
            volume = float(ticker.get("quoteVolume") or 0.0)
        except (TypeError, ValueError):
            continue
        rows.append((symbol, volume))

    rows.sort(key=lambda row: row[1], reverse=True)
    return [
        {"symbol": symbol, "qv_rank": index, "quote_volume": volume}
        for index, (symbol, volume) in enumerate(rows[:limit], start=1)
    ]
