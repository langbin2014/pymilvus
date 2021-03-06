# -*- coding: UTF-8 -*-

import queue
import threading
import time
from collections import defaultdict

from .grpc_handler import GrpcHandler
from .http_handler import HttpHandler
from milvus.client.exceptions import ConnectionPoolError


class Duration:
    def __init__(self):
        self.start_ts = time.time()
        self.end_ts = None

    def stop(self):
        if self.end_ts:
            return False

        self.end_ts = time.time()
        return True

    @property
    def value(self):
        if not self.end_ts:
            return None

        return self.end_ts - self.start_ts


class ConnectionRecord:
    def __init__(self, uri, recycle, handler="GRPC", conn_id=-1, **kwargs):
        '''
        @param uri server uri
        @param recycle int, time period to recycle connection.
        @param kwargs connection key-wprds
        '''
        self._conn_id = conn_id
        self._uri = uri
        self.recycle = recycle
        self._last_use_time = time.time()
        self._kw = kwargs

        if handler == "GRPC":
            self._connection = GrpcHandler(uri=uri)
        elif handler == "HTTP":
            self._connection = HttpHandler(uri=uri)
        else:
            raise ValueError("Unknown handler type. Use GRPC or HTTP")

    def connection(self):
        ''' Return a available connection. If connection is out-of-date,
        return new one.
        '''
        if self._kw.get("pre_ping", False):
            self._connection.connect(None, None, uri=self._uri, timeout=2)
        return self._connection


class ConnectionPool:
    def __init__(self, uri, pool_size=10, recycle=-1, wait_timeout=10, **kwargs):
        # Asynchronous queue to store connection
        self._pool = queue.Queue()
        self._uri = uri
        self._pool_size = pool_size
        self._recycle = recycle
        self._wait_timeout = wait_timeout

        # Record used connection number.
        self._used_conn = 0
        self._condition = threading.Condition()
        self._kw = kwargs

        #
        self.durations = defaultdict(list)


    def _inc_used(self):
        with self._condition:
            if self._used_conn < self._pool_size:
                self._used_conn = self._used_conn + 1
                return True

            return False

    def _dec_used(self):
        with self._condition:
            if self._used_conn == 0:
                return False
            self._used_conn -= 1
            return True

    def _full(self):
        with self._condition:
            return self._used_conn >= self._pool_size

    def _empty(self):
        with self._condition:
            return self._pool.qsize() <= 0 and self._used_conn <= 0

    def _create_connection(self):
        with self._condition:
            conn = ConnectionRecord(self._uri, self._recycle, conn_id=self._used_conn - 1, **self._kw)
            return ScopedConnection(self, conn)

    def _inc_connection(self):
        if self._inc_used():
            return self._create_connection()

        return self.fetch(block=True)

    def record_duration(self, conn, duration):
        if len(self.durations[conn]) >= 10000:
            self.durations[conn].pop(0)

        self.durations[conn].append(duration)

    def stats(self):
        out = {'connections': {}}
        connections = out['connections']
        take_time = []
        for conn, durations in self.durations.items():
            total_time = sum(d.value for d in durations)
            connections[id(conn)] = {
                'total_time': total_time,
                'called_times': len(durations)
            }
            take_time.append(total_time)

        out['max-time'] = max(take_time)
        out['num'] = len(self.durations)
        return out

    def count(self):
        with self._condition:
            return self._used_conn

    def activate_count(self):
        with self._condition:
            return self._used_conn - self._pool.qsize()

    def fetch(self, block=False):
        if self._empty():
            return self._inc_connection()

        try:
            conn = self._pool.get(block=block, timeout=self._wait_timeout)
            return ScopedConnection(self, conn)
        except queue.Empty:
            if block:
                raise ConnectionPoolError("Connection pool is full.")

        if self._full():
            return self.fetch(block=True)

        return self._inc_connection()

    def release(self, conn):
        try:
            self._pool.put(conn, False)
        except queue.Full:
            pass


class ScopedConnection:
    def __init__(self, pool, connection):
        self._pool = pool
        self._connection = connection
        self._duration = Duration()
        self._closed = False

    def __getattr__(self, item):
        return getattr(self.client(), item)

    def __enter__(self):
        return self

    def __del__(self):
        self.close()

    def connection(self):
        if self._closed:
            raise ValueError("Connection has been closed.")

        return self._connection

    def client(self):
        conn = self.connection()
        return conn.connection()

    def conn_id(self):
        return self._connection._conn_id

    def close(self):
        self._connection and self._pool.release(self._connection)
        self._connection = None
        if self._duration:
            self._duration.stop()
            self._pool.record_duration(self._connection, self._duration)
        self._duration = None
        self._closed = True
