import datetime
import lmdb
import pickle
from typing import List

import atpy.data.iqfeed.util as iqfeedutil
from atpy.data.iqfeed.filters import *
from atpy.data.iqfeed.util import *
import pyevents.events as events


class NewsFilter(NamedTuple):
    """
    News filter parameters
    """
    sources: List[str]
    symbols: List[str]
    date: datetime.date
    timeout: int
    limit: int


NewsFilter.__new__.__defaults__ = (None, None, None, None, 100000)


class DefaultNewsFilterProvider(DefaultFilterProvider):
    """Default news filter provider, which contains a list of filters"""

    def _default_filter(self):
        return NewsFilter()


class IQFeedNewsListener(object, metaclass=events.GlobalRegister):
    """
    IQFeed news listener (not streaming). See the unit test on how to use
    """

    def __init__(self, minibatch=None, attach_text=False, random_order=False, key_suffix='', filter_provider=DefaultNewsFilterProvider(), column_mode=True, lmdb_path=None):
        """
        :param minibatch: minibatch size
        :param attach_text: attach news text (separate request for each news item)
        :param random_order: random order
        :param key_suffix: suffix in the output dictionary
        :param filter_provider: iterator for filters
        :param column_mode: column/row mode
        :param lmdb_path: path to lmdb folder. If specified, requests will be cached
        """
        self.minibatch = minibatch
        self.attach_text = attach_text
        self.conn = None
        self.random_order = random_order
        self.key_suffix = key_suffix
        self.filter_provider = filter_provider
        self.column_mode = column_mode

        self.db = lmdb.open(lmdb_path) if lmdb_path is not None else None

        self.current_minibatch = None

    def __enter__(self):
        iqfeedutil.launch_service()

        self.conn = iq.NewsConn()
        self.conn.connect()
        self.cfg = self.conn.request_news_config()

        self.is_running = True
        self.producer_thread = threading.Thread(target=self.produce, daemon=True)
        self.producer_thread.start()

        return self

    def __exit__(self, exception_type, exception_value, traceback):
        """Disconnect connection etc"""
        self.conn.disconnect()
        self.quote_conn = None
        self.is_running = False

    def __del__(self):
        if self.conn is not None:
            self.conn.disconnect()
            self.cfg = None

    def __getattr__(self, name):
        if self.conn is not None:
            return getattr(self.conn, name)
        else:
            raise AttributeError

    def produce(self):
        for f in self.filter_provider:
            ids = list()
            titles = list()

            if self.db is not None:
                with self.db.begin() as txn:
                    headlines = txn.get(bytearray(f.__str__(), encoding='ascii'))

                if headlines is None:
                    _headlines = self.conn.request_news_headlines(sources=f.sources, symbols=f.symbols, date=f.date, limit=f.limit, timeout=f.timeout)
                    headlines = [h._asdict() for h in _headlines]

                    with self.db.begin(write=True) as txn:
                        txn.put(bytearray(f.__str__(), encoding='ascii'), pickle.dumps(headlines))
                else:
                    headlines = pickle.loads(headlines)
            else:
                _headlines = self.conn.request_news_headlines(sources=f.sources, symbols=f.symbols, date=f.date, limit=f.limit, timeout=f.timeout)
                headlines = [h._asdict() for h in _headlines]

            processed_data = None

            for h in headlines:
                if h['story_id'] not in ids and h['headline'] not in titles:
                    ids.append(h['story_id'])
                    titles.append(h['headline'])

                    if self.attach_text:
                        if self.db is not None:
                            with self.db.begin() as txn:
                                h['text'] = txn.get(bytearray('news_story_' + h['story_id'], encoding='ascii'))

                            if h['text'] is None:
                                h['text'] = self.conn.request_news_story(h['story_id'])._asdict()

                                with self.db.begin(write=True) as txn:
                                    txn.put(bytearray('news_story_' + h['story_id'], encoding='ascii'), pickle.dumps(h['text']))
                        else:
                            h['text'] = self.conn.request_news_story(h['story_id'])

                    if self.column_mode:
                        if processed_data is None:
                            processed_data = {f + self.key_suffix: list() for f in h.keys()}

                        if self.current_minibatch is None:
                            self.current_minibatch = {f + self.key_suffix: list() for f in h.keys()}

                        for key, value in h.items():
                            self.current_minibatch[key + self.key_suffix].append(value)
                            processed_data[key + self.key_suffix].append(value)

                        if len(self.current_minibatch[list(self.current_minibatch.keys())[0]]) == self.minibatch:
                            self.process_minibatch(self.current_minibatch)
                            self.current_minibatch = None
                    else:
                        if processed_data is None:
                            processed_data = list()

                        h = {k + self.key_suffix: v for k, v in h.items()}

                        processed_data.append(h)

                        if self.minibatch is not None:
                            if self.current_minibatch is None:
                                self.current_minibatch = list()

                            self.current_minibatch.append(h)

                            if len(self.current_minibatch) == self.minibatch:
                                self.process_minibatch(self.current_minibatch)
                                self.current_minibatch = list()

            self.process_batch(processed_data)

            if not self.is_running:
                break

    @events.after
    def process_batch(self, data):
        return {'type': 'news_batch', 'data': data}

    def batch_provider(self):
        return IQFeedDataProvider(self.process_batch)

    @events.after
    def process_minibatch(self, data):
        return {'type': 'news_minibatch', 'data': data}

    def minibatch_provider(self):
        return IQFeedDataProvider(self.process_minibatch)