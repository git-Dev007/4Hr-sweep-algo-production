"""
Delta Exchange API Client — 4H Sweep Algo Production
Handles authentication, retries, rate-limit backoff, and all trading operations.
Includes get_ohlcv() for fetching candlestick data used in sweep detection.
"""

import time
import hmac
import hashlib
import json
import logging
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("delta_api")


class DeltaExchangeAPI:
    def __init__(self, base_url, api_key, api_secret, max_retries=3, retry_delay=2):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        # Persistent session with connection pooling
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=10,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "python-rest-client",
            "Accept": "application/json",
        })

    # ----------------------------------------------------------------
    # Authentication
    # ----------------------------------------------------------------
    def _generate_signature(self, method, path, query_string="", payload=""):
        timestamp = str(int(time.time()))
        signature_data = method + timestamp + path + query_string + payload
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_data.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return signature, timestamp

    def _auth_headers(self, method, path, query_string="", payload=""):
        signature, timestamp = self._generate_signature(method, path, query_string, payload)
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
        }

    # ----------------------------------------------------------------
    # HTTP helpers
    # ----------------------------------------------------------------
    def _request(self, method, path, params=None, payload=None, auth=False):
        query_string = ""
        if params:
            query_string = "?" + urlencode(params)

        url = self.base_url + path + query_string
        body_str = json.dumps(payload) if payload else ""

        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                headers = {}
                if auth:
                    headers = self._auth_headers(method, path, query_string, body_str)

                if method == "GET":
                    resp = self.session.get(url, headers=headers, timeout=15)
                elif method == "POST":
                    resp = self.session.post(url, data=body_str, headers=headers, timeout=15)
                elif method == "DELETE":
                    resp = self.session.delete(url, data=body_str, headers=headers, timeout=15)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")

                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", self.retry_delay * attempt))
                    logger.warning(f"Rate limited. Waiting {wait}s (attempt {attempt})")
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()

                if not data.get("success", True):
                    error_info = data.get("error", data)
                    raise Exception(f"API Error: {error_info}")

                return data.get("result", data)

            except requests.exceptions.ConnectionError as e:
                last_exc = e
                logger.warning(f"Connection error (attempt {attempt}/{self.max_retries}): {e}")
                time.sleep(self.retry_delay * attempt)
            except requests.exceptions.Timeout as e:
                last_exc = e
                logger.warning(f"Timeout (attempt {attempt}/{self.max_retries}): {e}")
                time.sleep(self.retry_delay * attempt)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and 400 <= e.response.status_code < 500:
                    raise
                last_exc = e
                logger.warning(f"HTTP error (attempt {attempt}/{self.max_retries}): {e}")
                time.sleep(self.retry_delay * attempt)

        raise Exception(f"Request failed after {self.max_retries} retries: {last_exc}")

    def _get(self, path, params=None, auth=False):
        return self._request("GET", path, params=params, auth=auth)

    def _post(self, path, payload=None, auth=True):
        return self._request("POST", path, payload=payload, auth=auth)

    def _delete(self, path, payload=None, auth=True):
        return self._request("DELETE", path, payload=payload, auth=auth)

    # ----------------------------------------------------------------
    # Market Data
    # ----------------------------------------------------------------
    def get_option_tickers(self, underlying_symbol, contract_types="call_options,put_options"):
        """Fetch live option chain for the given underlying."""
        return self._get("/v2/tickers", params={
            "contract_types": contract_types,
            "underlying_asset_symbols": underlying_symbol,
        })

    def get_ticker(self, symbol):
        """Fetch live ticker for a specific symbol."""
        return self._get(f"/v2/tickers/{symbol}")

    def get_ohlcv(self, symbol, resolution="4h", start=None, end=None):
        """
        Fetch OHLCV candlestick data from Delta Exchange history API.

        Args:
            symbol:     Instrument symbol (e.g. 'BTCUSD')
            resolution: Candle resolution string — e.g. '1m', '5m', '15m',
                        '30m', '1h', '4h', '1d'. Default '4h'.
            start:      Unix timestamp (start of range, inclusive)
            end:        Unix timestamp (end of range, inclusive)

        Returns:
            List of candle dicts: [{time, open, high, low, close, volume}, ...]
            Sorted ascending by time.
        """
        # Try string resolution first ('4h'), fall back to numeric (240) on 400
        _RES_TO_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                       "1h": 60, "2h": 120, "4h": 240, "6h": 360, "1d": 1440}
        res_str = str(resolution)
        res_num = _RES_TO_MIN.get(res_str, res_str)
        candidates = [res_str] if res_str == str(res_num) else [res_str, str(res_num)]

        result = None
        for res_candidate in candidates:
            params = {"resolution": res_candidate, "symbol": symbol}
            if start is not None:
                params["start"] = str(start)
            if end is not None:
                params["end"] = str(end)
            try:
                result = self._get("/v2/history/candles", params=params)
                break
            except Exception as e:
                resp = getattr(e, "response", None)
                if resp is not None and resp.status_code == 400 and len(candidates) > 1:
                    logger.debug(f"Resolution '{res_candidate}' rejected (400), trying fallback...")
                    continue
                raise

        if result is None:
            return []

        # API may return a list directly or a dict with 'result'
        if isinstance(result, list):
            candles = result
        elif isinstance(result, dict):
            candles = result.get("result", result.get("candles", []))
        else:
            candles = []

        # Normalise field names (API may use 't'/'o'/'h'/'l'/'c' or full names)
        normalised = []
        for c in candles:
            normalised.append({
                "time":   int(c.get("time", c.get("t", 0))),
                "open":   float(c.get("open", c.get("o", 0))),
                "high":   float(c.get("high", c.get("h", 0))),
                "low":    float(c.get("low",  c.get("l", 0))),
                "close":  float(c.get("close", c.get("c", 0))),
                "volume": float(c.get("volume", c.get("v", 0))),
            })

        normalised.sort(key=lambda x: x["time"])
        return normalised

    # ----------------------------------------------------------------
    # Orders
    # ----------------------------------------------------------------
    def place_order(self, product_id, size, side, order_type="market_order",
                    limit_price=None, reduce_only=False):
        payload = {
            "product_id": product_id,
            "size": size,
            "side": side,
            "order_type": order_type,
        }
        if limit_price is not None:
            payload["limit_price"] = str(limit_price)
        if reduce_only:
            payload["reduce_only"] = True

        logger.info(f"Placing order: {payload}")
        result = self._post("/v2/orders", payload=payload)
        logger.info(f"Order placed: id={result.get('id')} state={result.get('state')}")
        return result

    def place_stop_order(self, product_id, size, side, stop_price, reduce_only=True):
        """
        Place an exchange-native stop-market order (stop_loss_order).
        Fires server-side when mark_price crosses stop_price — independent
        of the algo process being alive.

        Args:
            product_id: int — the option product id
            size:       int — number of contracts
            side:       'buy' (to close a short) or 'sell'
            stop_price: float — trigger price (mark price level)
            reduce_only: bool — always True for a protective stop
        """
        payload = {
            "product_id":   product_id,
            "size":         size,
            "side":         side,
            "order_type":   "stop_loss_order",
            "stop_price":   str(round(stop_price, 2)),
            "reduce_only":  reduce_only,
        }
        logger.info(f"Placing exchange stop order: {payload}")
        result = self._post("/v2/orders", payload=payload)
        logger.info(
            f"Exchange stop order placed: id={result.get('id')} "
            f"stop_price={stop_price:.2f}"
        )
        return result

    def cancel_order(self, order_id, product_id):
        """Cancel a specific order by id."""
        payload = {"id": order_id, "product_id": product_id}
        logger.info(f"Cancelling order id={order_id}")
        return self._delete("/v2/orders", payload=payload)

    def get_order(self, order_id):
        return self._get(f"/v2/orders/{order_id}", auth=True)

    def cancel_all_orders(self, product_id=None, contract_types=None):
        payload = {}
        if product_id:
            payload["product_id"] = product_id
        if contract_types:
            payload["contract_types"] = contract_types
        return self._delete("/v2/orders/all", payload=payload)

    # ----------------------------------------------------------------
    # Leverage
    # ----------------------------------------------------------------
    def set_leverage(self, product_id, leverage):
        path = f"/v2/products/{product_id}/orders/leverage"
        logger.info(f"Setting leverage {leverage}x for product {product_id}")
        return self._post(path, payload={"leverage": str(leverage)})

    # ----------------------------------------------------------------
    # Positions
    # ----------------------------------------------------------------
    def get_margined_positions(self, product_ids=None, contract_types=None):
        params = {}
        if product_ids:
            params["product_ids"] = ",".join(str(p) for p in product_ids)
        if contract_types:
            params["contract_types"] = contract_types
        return self._get("/v2/positions/margined", params=params, auth=True)

    def close_all_positions(self):
        logger.info("Closing all positions")
        return self._post("/v2/positions/close_all", payload={
            "close_all_portfolio": True,
            "close_all_isolated": True,
        })

    def close_position_by_market(self, product_id, size, current_side):
        opposite_side = "buy" if current_side == "sell" else "sell"
        return self.place_order(
            product_id=product_id,
            size=abs(size),
            side=opposite_side,
            order_type="market_order",
            reduce_only=True,
        )

    # ----------------------------------------------------------------
    # Wallet
    # ----------------------------------------------------------------
    def get_wallet_balances(self):
        return self._get("/v2/wallet/balances", auth=True)
