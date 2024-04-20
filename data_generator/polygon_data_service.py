import threading
import traceback

import matplotlib.pyplot as plt

from typing import List

from polygon.rest.models import MarketStatus
from polygon.websocket import WebSocketClient, Market, EquityAgg, CurrencyAgg
from concurrent.futures import ThreadPoolExecutor, as_completed

from data_generator.base_data_service import BaseDataService, POLYGON_PROVIDER_NAME
from time_util.time_util import TimeUtil
from vali_config import TradePair, TradePairCategory
import time

from vali_objects.utils.vali_utils import ValiUtils
import bittensor as bt
from polygon import RESTClient

from vali_objects.vali_dataclasses.price_source import PriceSource


DEBUG = 0



class PolygonDataService(BaseDataService):

    def __init__(self, api_key, disable_ws=False):
        self.init_time = time.time()
        self._api_key = api_key
        self.disable_ws = disable_ws
        timespan_to_ms = {'second': 1000, 'minute': 1000 * 60, 'hour': 1000 * 60 * 60, 'day': 1000 * 60 * 60 * 24}


        trade_pair_category_to_longest_allowed_lag_s = {TradePairCategory.CRYPTO: 30, TradePairCategory.FOREX: 30,
                                                           TradePairCategory.INDICES: 30}
        super().__init__(trade_pair_category_to_longest_allowed_lag_s=trade_pair_category_to_longest_allowed_lag_s,
                         timespan_to_ms=timespan_to_ms,
                         provider_name=POLYGON_PROVIDER_NAME)

        self.MARKET_STATUS = None
        self.LOCK = threading.Lock()
        self.UNSUPPORTED_TRADE_PAIRS = (TradePair.FTSE, TradePair.GDAXI)


        self.POLYGON_CLIENT = RESTClient(api_key=self._api_key)

        self.POLY_WEBSOCKETS = {
            Market.Crypto: None,
            Market.Forex: None,
            Market.Indices: None
        }

        self.POLY_WEBSOCKET_THREADS = {
            Market.Crypto: None,
            Market.Forex: None,
            Market.Indices: None
        }

        # Start thread to refresh market status
        if not disable_ws:
            self.websocket_manager_thread = threading.Thread(target=self.websocket_manager, daemon=True)
            self.websocket_manager_thread.start()
            time.sleep(3) # Let the websocket_manager_thread start

    def main_forex(self):
        self.POLY_WEBSOCKETS[Market.Forex].run(self.handle_msg)

    def main_indices(self):
        self.POLY_WEBSOCKETS[Market.Indices].run(self.handle_msg)

    def main_crypto(self):
        self.POLY_WEBSOCKETS[Market.Crypto].run(self.handle_msg)

    def stop_start_websocket_threads(self):
        if self.POLY_WEBSOCKETS[Market.Indices]:
            self.POLY_WEBSOCKETS[Market.Indices].close()
            self.POLY_WEBSOCKETS[Market.Forex].close()
            self.POLY_WEBSOCKETS[Market.Crypto].close()
        self.POLY_WEBSOCKETS[Market.Indices] = WebSocketClient(market=Market.Indices, api_key=self._api_key)
        self.POLY_WEBSOCKETS[Market.Forex] = WebSocketClient(market=Market.Forex, api_key=self._api_key)
        self.POLY_WEBSOCKETS[Market.Crypto] = WebSocketClient(market=Market.Crypto, api_key=self._api_key)
        self.subscribe_websockets()
        if self.POLY_WEBSOCKET_THREADS[Market.Indices]:
            self.POLY_WEBSOCKET_THREADS[Market.Indices].join()
            self.POLY_WEBSOCKET_THREADS[Market.Forex].join()
            self.POLY_WEBSOCKET_THREADS[Market.Crypto].join()
            time.sleep(5)

        self.LOCK = threading.Lock()
        self.POLY_WEBSOCKET_THREADS[Market.Indices] = threading.Thread(target=self.main_indices, daemon=True)
        self.POLY_WEBSOCKET_THREADS[Market.Forex] = threading.Thread(target=self.main_forex, daemon=True)
        self.POLY_WEBSOCKET_THREADS[Market.Crypto] = threading.Thread(target=self.main_crypto, daemon=True)
        self.POLY_WEBSOCKET_THREADS[Market.Indices].start()
        self.POLY_WEBSOCKET_THREADS[Market.Forex].start()
        self.POLY_WEBSOCKET_THREADS[Market.Crypto].start()

    def websocket_manager(self):
        prev_n_events = None
        last_ws_health_check_s = 0
        last_market_status_update_s = 0
        while True:
            now = time.time()
            if now - last_ws_health_check_s > 180:
                if prev_n_events is None or prev_n_events == self.n_events_global:
                    if prev_n_events is not None:
                        bt.logging.error(
                            f"POLY websocket has not received any events in the last 180 seconds. n_events {self.n_events_global} Restarting websocket.")
                    self.stop_start_websocket_threads()

                last_ws_health_check_s = now
                prev_n_events = self.n_events_global

            if now - last_market_status_update_s > 60:
                self.MARKET_STATUS = self.POLYGON_CLIENT.get_market_status()
                if not isinstance(self.MARKET_STATUS, MarketStatus):
                    bt.logging.error(f"Failed to fetch market status. Received: {self.MARKET_STATUS}")
                last_market_status_update_s = now
                self.debug_log()

            time.sleep(1)

    def handle_msg(self, msgs: List[CurrencyAgg | EquityAgg]):
        """
        received message: CurrencyAgg(event_type='CAS', pair='USD/CHF', open=0.91313, close=0.91317, high=0.91318,
        low=0.91313, volume=3, vwap=None, start_timestamp=1713273701000, end_timestamp=1713273702000,
         avg_trade_size=None) <class 'polygon.websocket.models.models.CurrencyAgg'>

         CurrencyAgg(event_type='XAS', pair='ETH-USD', open=3084.37, close=3084.24, high=3084.37, low=3084.08,
         volume=0.99917426, vwap=3084.1452, start_timestamp=1713273981000, end_timestamp=1713273982000, avg_trade_size=0)
        """
        with self.LOCK:
            try:
                for m in msgs:
                    # bt.logging.info(f"Received price event: {event}")
                    # print('received message:', m, type(m))
                    if isinstance(m, EquityAgg):
                        tp = self.symbol_to_trade_pair(m.symbol[2:])  # I:SPX -> SPX
                    elif isinstance(m, CurrencyAgg):
                        tp = self.symbol_to_trade_pair(m.pair)
                    else:
                        raise ValueError(f"Unknown message in POLY websocket: {m}")
                    start_timestamp = m.start_timestamp
                    end_timestamp = m.end_timestamp
                    timespan_ms = end_timestamp - start_timestamp
                    now_ms = TimeUtil.now_in_millis()
                    symbol = tp.trade_pair
                    # This is a candle so we can make 2 prices, one for the open and one for the close
                    price1 = PriceSource(
                        source=f'{POLYGON_PROVIDER_NAME}_ws',
                        timespan_ms=timespan_ms,
                        open=m.open,
                        close=m.open,
                        vwap=m.vwap,
                        high=m.high,
                        low=m.low,
                        start_ms=start_timestamp,
                        websocket=True,
                        lag_ms=now_ms - start_timestamp,
                        volume=m.volume
                    )
                    price2 = PriceSource(
                        source=f'{POLYGON_PROVIDER_NAME}_ws',
                        timespan_ms=timespan_ms,
                        open=m.close,
                        close=m.close,
                        vwap=m.vwap,
                        high=m.high,
                        low=m.low,
                        start_ms=end_timestamp - 1,  # prioritize a new candle's open over a previous candle's close
                        websocket=True,
                        lag_ms=now_ms - end_timestamp,
                        volume=m.volume
                    )
                    self.latest_websocket_events[symbol] = price2
                    self.trade_pair_to_recent_events[symbol].add_event(price1)
                    self.trade_pair_to_recent_events[symbol].add_event(price2)
                    # Reset the closed market price, indicating that a new close should be fetched after the current day's close
                    self.closed_market_prices[tp] = None
                    self.n_events_global += 1
                    if DEBUG:
                        formatted_time = TimeUtil.millis_to_formatted_date_str(TimeUtil.now_in_millis())
                        self.trade_pair_to_price_history[tp].append((formatted_time, price))
                if DEBUG:
                    print('last message:', m, 'n msgs total:', len(msgs))
                    history_size = sum(len(v) for v in self.trade_pair_to_price_history.values())
                    bt.logging.info("History Size: " + str(history_size))
                    bt.logging.info(f"n_events_global: {self.n_events_global}")
            except Exception as e:
                bt.logging.error(f"Failed to handle POLY websocket message with error: {e}")

    def subscribe_websockets(self):
        for tp in TradePair:
            if tp in self.UNSUPPORTED_TRADE_PAIRS:
                continue  # not supported by polygon
            if tp.is_crypto:
                symbol = "XAS." + tp.trade_pair.replace('/', '-')
                self.POLY_WEBSOCKETS[Market.Crypto].subscribe(symbol)
            elif tp.is_forex:
                symbol = "CAS." + tp.trade_pair
                self.POLY_WEBSOCKETS[Market.Forex].subscribe(symbol)
            elif tp.is_indices:
                symbol = "A.I:" + tp.trade_pair
                self.POLY_WEBSOCKETS[Market.Indices].subscribe(symbol)
            else:
                raise ValueError(f"Unknown trade pair category: {tp.trade_pair_category}")

    def is_market_open(self, trade_pair: TradePair) -> bool:
        if self.MARKET_STATUS is None:
            return False
        if not isinstance(self.MARKET_STATUS, MarketStatus):
            return False
        if trade_pair.trade_pair_category == TradePairCategory.CRYPTO:
            return self.MARKET_STATUS.currencies.crypto == 'open'
        elif trade_pair.trade_pair_category == TradePairCategory.FOREX:
            return self.MARKET_STATUS.currencies.fx == 'open'
        elif trade_pair.trade_pair_category == TradePairCategory.INDICES:
            #if trade_pair == TradePair.SPX:
            #    return MARKET_STATUS.indicesGroups.s_and_p == 'open'
            #elif trade_pair == TradePair.DJI:
            #    return MARKET_STATUS.indicesGroups.dow_jones == 'open'
            #elif trade_pair in UNSUPPORTED_TRADE_PAIRS:
            #    return False
            #else:
            #    raise ValueError(f"Unknown trade pair id: {trade_pair.trade_pair_id}")
            return self.MARKET_STATUS.market == 'open'

        else:
            raise ValueError(f"Unknown trade pair: {trade_pair}")

    def symbol_to_trade_pair(self, symbol: str):
        # Should work for indices and forex
        tp = TradePair.get_latest_tade_pair_from_trade_pair_str(symbol)
        if tp:
            return tp
        # Should work for crypto. Anything else will return None
        tp = TradePair.get_latest_tade_pair_from_trade_pair_str(symbol.replace('-', '/'))
        if not tp:
            raise ValueError(f"Unknown symbol: {symbol}")
        return tp

    def get_closes_rest(self, pairs: List[TradePair]) -> dict:
        all_trade_pair_closes = {}
        # Multi-threaded fetching of REST data over all requested trade pairs. Max parallelism is 5.
        with ThreadPoolExecutor(max_workers=5) as executor:
            # Dictionary to keep track of futures
            future_to_trade_pair = {executor.submit(self.get_close_rest, p): p for p in pairs}

            for future in as_completed(future_to_trade_pair):
                tp = future_to_trade_pair[future]
                try:
                    result = future.result()
                    if result is None:
                        result = {}
                    all_trade_pair_closes[tp] = result
                except Exception as exc:
                    bt.logging.error(f"{tp} generated an exception: {exc}. Continuing...")
                    bt.logging.error(traceback.format_exc())

        return all_trade_pair_closes

    def agg_to_price_source(self, a, now_ms:int, timespan:str, attempting_prev_close:bool=False):
        p_name = f'{POLYGON_PROVIDER_NAME}_rest'
        if attempting_prev_close:
            p_name += '_prev_close'
        return \
            PriceSource(
                source=p_name,
                timespan_ms=self.timespan_to_ms[timespan],
                open=a.open,
                close=a.close,
                vwap=a.vwap,
                high=a.high,
                low=a.low,
                start_ms=a.timestamp,
                websocket=False,
                lag_ms=now_ms - a.timestamp,
                volume=a.volume
            )

    def get_close_rest(
        self,
        trade_pair: TradePair
    ) -> PriceSource | None:
        polygon_ticker = self.trade_pair_to_polygon_ticker(trade_pair)
        #bt.logging.info(f"Fetching REST data for {polygon_ticker}")

        if not self.is_market_open(trade_pair):
            return self.get_event_before_market_close(trade_pair)

        now_ms = TimeUtil.now_in_millis()
        prev_timestamp = None
        final_agg = None
        timespan = "second"
        for a in self.POLYGON_CLIENT.list_aggs(
                polygon_ticker,
                1,
                timespan,
                now_ms - 10000,
                now_ms + 2000
        ):
            #print('agg:', a)
            """
                    agg Agg(open=111.91, high=111.91, low=111.902, close=111.909, volume=3, vwap=111.907,
                    timestamp=1713273876000, transactions=3, otc=None)
            """
            epoch_miliseconds = a.timestamp
            price_source = self.agg_to_price_source(a, now_ms, timespan)
            assert prev_timestamp is None or prev_timestamp < epoch_miliseconds
            #formatted_date = TimeUtil.millis_to_formatted_date_str(epoch_miliseconds // 1000)
            final_agg = price_source
            prev_timestamp = epoch_miliseconds
        if not final_agg:
            bt.logging.error(f"Polygon failed to fetch REST data for {trade_pair.trade_pair}. If you keep seeing this error, report it to the team ASAP")
            final_agg = None

        return final_agg


    def trade_pair_to_polygon_ticker(self, trade_pair: TradePair):
        if trade_pair.trade_pair_category == TradePairCategory.CRYPTO:
            return 'X:' + trade_pair.trade_pair_id
        elif trade_pair.trade_pair_category == TradePairCategory.FOREX:
            return 'C:' + trade_pair.trade_pair_id
        elif trade_pair.trade_pair_category == TradePairCategory.INDICES:
            return 'I:' + trade_pair.trade_pair_id
        else:
            raise ValueError(f"Unknown trade pair category: {trade_pair.trade_pair_category}")

    def get_event_before_market_close(self, trade_pair: TradePair) -> PriceSource | None:
        if self.closed_market_prices[trade_pair] is not None:
            return self.closed_market_prices[trade_pair]
        elif trade_pair in self.UNSUPPORTED_TRADE_PAIRS:
            return None

        # start 7 days ago
        end_time_ms = TimeUtil.now_in_millis()
        start_time_ms = TimeUtil.now_in_millis() - 1000 * 60 * 60 * 24 * 7
        candles = self.get_candles_for_trade_pair(trade_pair, start_time_ms, end_time_ms, attempting_prev_close=True)
        if len(candles) == 0:
            msg = f"Failed to fetch market close for {trade_pair.trade_pair}"
            raise ValueError(msg)

        ans = candles[-1]
        self.closed_market_prices[trade_pair] = ans
        return self.closed_market_prices[trade_pair]

    def get_websocket_event(self, trade_pair: TradePair) -> PriceSource | None:
        symbol = trade_pair.trade_pair
        cur_event = self.latest_websocket_events.get(symbol)
        if not cur_event:
            return None

        timestamp_ms = cur_event.end_ms
        max_allowed_lag_s = self.trade_pair_category_to_longest_allowed_lag_s[trade_pair.trade_pair_category]
        lag_s = time.time() - timestamp_ms / 1000.0
        is_stale = lag_s > max_allowed_lag_s
        if is_stale:
            bt.logging.info(f"Found stale TD websocket data for {trade_pair.trade_pair}. Lag_s: {lag_s} "
                            f"seconds. Max allowed lag for category: {max_allowed_lag_s} seconds. Ignoring this data.")
        return cur_event


    def get_close_in_past_hour_fallback(self, trade_pair: TradePair, timestamp_ms: int):
        polygon_ticker = self.trade_pair_to_polygon_ticker(trade_pair)

        #if not self.is_market_open(trade_pair):
        #    return self.get_event_before_market_close(trade_pair)

        prev_timestamp = None
        smallest_delta = None
        corresponding_price = None
        start_time = None
        n_responses = 0
        candle = None
        timespan = "hour"

        def try_updating_found_price(t, p):
            nonlocal smallest_delta, corresponding_price, start_time, candle
            time_delta_ms = abs(t - timestamp_ms)
            if smallest_delta is None or time_delta_ms < smallest_delta:
                smallest_delta = time_delta_ms
                corresponding_price = p
                start_time = epoch_miliseconds
                candle = a

        for a in self.POLYGON_CLIENT.list_aggs(
                polygon_ticker,
                1,
                timespan,
                timestamp_ms - 1000 * 60 * 60 * 48,
                timestamp_ms + 1000 * 60 * 30
        ):
            n_responses += 1
            epoch_miliseconds = a.timestamp

            try_updating_found_price(epoch_miliseconds, a.open)
            try_updating_found_price(epoch_miliseconds + self.timespan_to_ms[timespan], a.close)

            assert prev_timestamp is None or prev_timestamp < epoch_miliseconds
            prev_timestamp = epoch_miliseconds

        #print(f"hourly fallback smallest delta s: {smallest_delta / 1000 if smallest_delta else None}, input_timestamp: {timestamp_ms}, candle_start_time_ms: {start_time}, candle: {candle}, n_responses: {n_responses}")
        return corresponding_price



    def get_close_at_date_minute_fallback(self, trade_pair: TradePair, timestamp_ms: int):
        polygon_ticker = self.trade_pair_to_polygon_ticker(trade_pair)

        #if not self.is_market_open(trade_pair):
        #    return self.get_event_before_market_close(trade_pair)

        prev_timestamp = None
        smallest_delta = None
        corresponding_price = None
        start_time = None
        n_responses = 0
        candle = None
        timespan = "minute"

        def try_updating_found_price(t, p):
            nonlocal smallest_delta, corresponding_price, start_time, candle
            time_delta_ms = abs(t - timestamp_ms)
            if smallest_delta is None or time_delta_ms <= smallest_delta:
                smallest_delta = time_delta_ms
                corresponding_price = p
                start_time = epoch_miliseconds
                candle = a

        for a in self.POLYGON_CLIENT.list_aggs(
                polygon_ticker,
                1,
                timespan,
                timestamp_ms - 1000 * 60 * 30,
                timestamp_ms + 1000 * 60 * 30
        ):
            n_responses += 1
            epoch_miliseconds = a.timestamp

            try_updating_found_price(epoch_miliseconds, a.open)
            try_updating_found_price(epoch_miliseconds + self.timespan_to_ms[timespan], a.close)

            assert prev_timestamp is None or prev_timestamp < epoch_miliseconds
            prev_timestamp = epoch_miliseconds

        #print(f"minute fallback smallest delta ms: {smallest_delta}, input_timestamp: {timestamp_ms}, candle_start_time_ms: {start_time}, candle: {candle}, n_responses: {n_responses}")
        return corresponding_price

    def get_close_at_date_second(self, trade_pair: TradePair, target_timestamp_ms: int):
        polygon_ticker = self.trade_pair_to_polygon_ticker(trade_pair)

        #if not self.is_market_open(trade_pair):
        #    return self.get_event_before_market_close(trade_pair)

        prev_timestamp = None
        smallest_delta = None
        corresponding_price = None
        n_responses = 0
        timespan = "second"

        def try_updating_found_price(t, p):
            nonlocal smallest_delta, corresponding_price, target_timestamp_ms
            time_delta_ms = abs(t - target_timestamp_ms)
            if smallest_delta is None or time_delta_ms <= smallest_delta:
                #print('Updated best answer', time_delta_ms, smallest_delta, t, p)
                smallest_delta = time_delta_ms
                corresponding_price = p

        for a in self.POLYGON_CLIENT.list_aggs(
                polygon_ticker,
                1,
                timespan,
                target_timestamp_ms - 1000 * 8,
                target_timestamp_ms + 1000 * 8
        ):
            print('agg', a, 'dt', target_timestamp_ms - a.timestamp, 'ms')
            n_responses += 1
            try_updating_found_price(a.timestamp, a.open)
            try_updating_found_price(a.timestamp + self.timespan_to_ms[timespan], a.close)

            assert prev_timestamp is None or prev_timestamp < a.timestamp
            prev_timestamp = a.timestamp

        #print(f"smallest delta ms: {smallest_delta}, input_timestamp: {timestamp_ms}, candle_start_time_ms: {start_time}, candle: {candle}, n_responses: {n_responses}")
        return corresponding_price, smallest_delta

    def get_range_of_closes(self, trade_pair, start_date: str, end_date: str):
        ts = self.td.time_series(symbol=trade_pair, interval='1min', start_date=start_date, end_date=end_date, outputsize=5000)
        response = ts.as_json()
        closes = [(d['datetime'], float(d["close"])) for d in response]
        return closes

    def get_candles(self, trade_pairs: List[TradePair], start_time_ms:int, end_time_ms:int):
        # Dictionary to store the minimum prices for each trade pair
        ret = {}

        # Create a ThreadPoolExecutor with a maximum of 5 threads
        with ThreadPoolExecutor(max_workers=5) as executor:
            # Future objects dictionary to hold the ongoing computations
            futures = {executor.submit(self.get_candles_for_trade_pair, tp, start_time_ms, end_time_ms): tp for tp in trade_pairs}

            # Retrieve the results as they complete
            for future in as_completed(futures):
                trade_pair = futures[future]
                try:
                    # Collect the result from future
                    result = future.result()
                    ret[trade_pair] = result
                except Exception as exc:
                    print(f'{trade_pair} get_candles_for_trade_pair generated an exception: {exc}')

        # Return the collected results
        return ret

    def get_candles_for_trade_pair_simple(self, trade_pair: TradePair, start_timestamp_ms: int, end_timestamp_ms: int):
        polygon_ticker = self.trade_pair_to_polygon_ticker(trade_pair)
        ans = {}
        ub = 0
        lb = float('inf')
        for a in self.POLYGON_CLIENT.list_aggs(
                polygon_ticker,
                1,
                "second",
                start_timestamp_ms,
                end_timestamp_ms,
                limit=50000
        ):
            ans[a.timestamp // 1000] = a.close
            ub = max(ub, a.timestamp)
            lb = min(lb, a.timestamp)
        return ans, lb, ub


    def get_candles_for_trade_pair(
        self,
        trade_pair: TradePair,
        start_timestamp_ms: int,
        end_timestamp_ms: int,
        attempting_prev_close: bool = False,
        force_second: bool = False
    ) -> list[PriceSource] | None:
        """
        agg Agg(open=111.91, high=111.91, low=111.902, close=111.909, volume=3, vwap=111.907,
        timestamp=1713273876000, transactions=3, otc=None)

        agg Agg(open=63010.86, high=63010.86, low=63010.86, close=63010.86, volume=2.158e-05, vwap=63010.86,
         timestamp=1713273888000, transactions=1, otc=None)
        """

        delta_time_ms = end_timestamp_ms - start_timestamp_ms
        delta_time_seconds = delta_time_ms / 1000
        delta_time_minutes = delta_time_seconds / 60
        delta_time_hours = delta_time_minutes / 60

        if delta_time_seconds < 70:
            timespan = "second"
        elif delta_time_minutes < 70:
            timespan = "minute"
        elif delta_time_hours < 70:
            timespan = "hour"
        else:
            timespan = "day"

        if force_second:
            timespan = "second"

        polygon_ticker = self.trade_pair_to_polygon_ticker(trade_pair)

        aggs = []
        prev_timestamp = None
        now_ms = TimeUtil.now_in_millis()
        for i, a in enumerate(self.POLYGON_CLIENT.list_aggs(
                polygon_ticker,
                1,
                timespan,
                start_timestamp_ms,
                end_timestamp_ms
        )):
            epoch_miliseconds = a.timestamp
            assert prev_timestamp is None or epoch_miliseconds >= prev_timestamp, ('candles not sorted', prev_timestamp, epoch_miliseconds)
            #formatted_date = TimeUtil.millis_to_formatted_date_str(epoch_miliseconds)
            #if i != -1:
                #print('        agg:', a.low, formatted_date, trade_pair.trade_pair_id, timespan)
            price_source = self.agg_to_price_source(a, now_ms, timespan, attempting_prev_close=attempting_prev_close)
            aggs.append(price_source)
            prev_timestamp = epoch_miliseconds

        if not aggs:
            bt.logging.trace(f"{POLYGON_PROVIDER_NAME} failed to fetch candle data for {trade_pair.trade_pair}. "
                             f" Perhaps this trade pair was closed during the specified window.")

        return aggs



if __name__ == "__main__":
    secrets = ValiUtils.get_secrets()

    # Initialize client
    polygon_data_provider = PolygonDataService(api_key=secrets['polygon_apikey'])

    #price, time_delta = polygon_data_provider.get_close_at_date_second(trade_pair=TradePair.BTCUSD, target_timestamp_ms=1712671378202)

    trades = polygon_data_provider.POLYGON_CLIENT.list_trades(ticker='X:BTC-USD', params={
        "timestamp.gte": 1712671370202000000 - 1000 * 1000000,
        "timestamp.lte": 1712671370202000000 + 1000 * 1000000
    })
    exchange_to_name = {1: 'Coinbase', 23: 'Kraken', 2: 'Bitfinex'}
    for trade in trades:
        participant_timestamp_ms = trade.participant_timestamp / 1000000.0
        format_date = TimeUtil.millis_to_formatted_date_str(participant_timestamp_ms)
        print(format_date, exchange_to_name[trade.exchange], trade.price)

    print(price, time_delta)
    assert 0

    trade_pairs = [TradePair.BTCUSD, TradePair.ETHUSD, TradePair.SPX, TradePair.GBPUSD, TradePair.DJI]
    print("-----------------REST-----------------")
    ans_rest = polygon_data_provider.get_closes_rest(trade_pairs)
    for k, v in ans_rest.items():
        print(f"@@@{k}@@@: {v}")
    assert 0
    now = TimeUtil.now_in_millis()
    while True:
        # use trade_pair_to_recent_events to get the closest event to "now"
        for tp in TradePair:
            symbol = tp.trade_pair
            closest_event = polygon_data_provider.trade_pair_to_recent_events[symbol].get_closest_event(now)
            n_events = polygon_data_provider.trade_pair_to_recent_events[symbol].count_events()
            delta_time_s = (now - closest_event.start_ms) / 1000.0 if closest_event else None
            print(f"Closest event to {TimeUtil.millis_to_formatted_date_str(now)} for {tp.trade_pair_id}: {closest_event}. Total_n_events: {n_events}. Lag (s): {delta_time_s}")
        time.sleep(10)

    #time.sleep(12)
    print(polygon_data_provider.get_close_at_date_second(TradePair.BTCUSD, 1712671378202))
    assert 0



    #for t in polygon_data_provider.polygon_client.list_tickers(market="indices", limit=1000):
    #    if t.ticker.startswith("I:F"):
    #        print(t.ticker)

    times_to_test = []

    start_time_ms = TimeUtil.now_in_millis() - 1000 * 10  # 10 seconds ago
    end_time_ms = TimeUtil.now_in_millis() - 1000 * 5  # 5 seconds ago
    times_to_test.append((start_time_ms, end_time_ms))

    start_time_ms = TimeUtil.now_in_millis() - 1000 * 60 * 10  # 10 minutes ago
    end_time_ms = TimeUtil.now_in_millis() - 1000 * 60 * 5  # 5 minutes ago
    times_to_test.append((start_time_ms, end_time_ms))

    start_time_ms = TimeUtil.now_in_millis() - 1000 * 60 * 60 * 10  # 10 hours ago
    end_time_ms = TimeUtil.now_in_millis() - 1000 * 60 * 60 * 9  # 9 hours ago
    times_to_test.append((start_time_ms, end_time_ms))

    start_time_ms = TimeUtil.now_in_millis() - 1000 * 60 * 60 * 10 * 24  # 10 days ago
    end_time_ms = TimeUtil.now_in_millis() - 1000 * 60 * 60 * 5 * 24  # 5 days ago
    times_to_test.append((start_time_ms, end_time_ms))

    for start_time_ms, end_time_ms in times_to_test:
        start_date_formatted = TimeUtil.millis_to_formatted_date_str(start_time_ms)
        end_date_formatted = TimeUtil.millis_to_formatted_date_str(end_time_ms)
        print('-------------------------------------------------------------------')
        print(f"Testing between {start_date_formatted} and {end_date_formatted}")

        tps = [tp for tp in TradePair if tp not in self.UNSUPPORTED_TRADE_PAIRS]
        candles = polygon_data_provider.get_candles(tps, start_time_ms, end_time_ms)
        print(f"    candles: {candles}")

    for tp in TradePair:
        if tp != TradePair.GBPUSD:
            continue

        is_open = self.is_market_open(tp)
        print(f'market is open for {tp}: ', is_open)
        print('PRICE BEFORE MARKET CLOSE: ', polygon_data_provider.get_event_before_market_close(tp))
        print('getting close for', tp.trade_pair_id, ':', polygon_data_provider.get_close_rest(tp)[tp])


    trade_pairs = [TradePair.BTCUSD, TradePair.ETHUSD, TradePair.SPX, TradePair.GBPUSD, TradePair.DJI]
    print("-----------------REST-----------------")
    ans_rest = polygon_data_provider.get_closes_rest(trade_pairs)
    for k, v in ans_rest.items():
        print(f"{k.trade_pair_id}: {v}")
    print("-----------------WEBSOCKET-----------------")
    ans_ws = polygon_data_provider.get_closes_websocket(trade_pairs)
    for k, v in ans_ws.items():
        print(f"{k.trade_pair_id}: {v}")
    print("-----------------Normal-----------------")
    ans_n = polygon_data_provider.get_closes(trade_pairs)
    for k, v in ans_n.items():
        print(f"{k.trade_pair_id}: {v}")
    print("-----------------Done-----------------")






    cached_prices = {}
    cached_times = {}
    while True:
        print('main thread perspective - n_events_global:', n_events_global)
        for k, price in latest_websocket_events.items():
            t = last_websocket_ping_time_s[k]
            cached_price = cached_prices.get(k, None)
            cached_time = cached_times.get(k, None)
            if cached_price and cached_time:
                assert cached_time <= t, (cached_time, t)

            cached_prices[k] = price
            cached_times[k] = t
        debug_log()
        time.sleep(10)