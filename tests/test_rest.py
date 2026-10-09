"""RestClient's retry/terminal dispatch - the part fakevenue.py cannot cover.

fakevenue.py duck-types the RestClient surface for the execution tests, so it never
exercises _request()'s own retry logic; these tests hit that logic directly by
mocking the HTTP transport underneath a real RestClient.
"""
import unittest
from unittest.mock import MagicMock, patch

import config
from exchange.rest import ApiError, RestClient


def _response(status_code=200, body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    resp.json.return_value = body if body is not None else {}
    return resp


class RetryDispatchTests(unittest.TestCase):
    def setUp(self):
        self.client = RestClient(api_key="k", api_secret="s", testnet=True)
        self._sleep = patch("time.sleep").start()
        self.addCleanup(patch.stopall)

    def test_4120_from_the_algo_endpoint_is_retried_not_given_up_on(self):
        """Found 2026-10-09: place_stop()'s only recourse on a hard failure is to
        close the position at a realised loss, so a code the venue returns for an
        undocumented reason on the (already correct) algo endpoint must get a real
        retry before that happens, not an immediate raise."""
        fail = _response(400, {"code": -4120, "msg": "Order type not supported"})
        ok = _response(200, {"algoId": 123})
        self.client._session.request = MagicMock(side_effect=[fail, ok])

        result = self.client.new_algo_order(
            symbol="BTCUSDT", side="SELL", type="STOP_MARKET",
            triggerPrice="100", closePosition="true", clientAlgoId="x")

        self.assertEqual(result, {"algoId": 123})
        self.assertEqual(self.client._session.request.call_count, 2)

    def test_4120_still_gives_up_after_exhausting_retries(self):
        """If the rejection really is persistent, the caller must still hear about
        it (and so still fall through to the existing close-the-position safety
        net) rather than retrying forever."""
        fail = _response(400, {"code": -4120, "msg": "Order type not supported"})
        self.client._session.request = MagicMock(return_value=fail)

        with self.assertRaises(ApiError) as ctx:
            self.client.new_algo_order(
                symbol="BTCUSDT", side="SELL", type="STOP_MARKET",
                triggerPrice="100", closePosition="true", clientAlgoId="x")

        self.assertEqual(ctx.exception.code, -4120)
        self.assertEqual(self.client._session.request.call_count,
                         config.REST_MAX_RETRIES + 1)

    def test_a_terminal_code_is_never_retried(self):
        fail = _response(400, {"code": -2010, "msg": "new order rejected"})
        self.client._session.request = MagicMock(return_value=fail)

        with self.assertRaises(ApiError) as ctx:
            self.client.new_order(symbol="BTCUSDT", side="BUY", type="MARKET",
                                  quantity="1", newClientOrderId="x")

        self.assertEqual(ctx.exception.code, -2010)
        self.assertEqual(self.client._session.request.call_count, 1,
                         "a terminal code must fail fast, not spend retry budget")

    def test_an_unknown_code_also_fails_fast_not_silently_retried_forever(self):
        """Codes this client has no documented stance on default to non-retryable -
        the safe default for an order endpoint, where retrying a request that may
        already have landed risks a duplicate."""
        fail = _response(400, {"code": -9999, "msg": "something new"})
        self.client._session.request = MagicMock(return_value=fail)

        with self.assertRaises(ApiError):
            self.client.new_order(symbol="BTCUSDT", side="BUY", type="MARKET",
                                  quantity="1", newClientOrderId="x")

        self.assertEqual(self.client._session.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
