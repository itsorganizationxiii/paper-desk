"""Local fixtures only; no network, real database or sample fabrication in production."""
import unittest, os, threading
from unittest.mock import patch
from pymongo.errors import ServerSelectionTimeoutError, BulkWriteError, DuplicateKeyError, AutoReconnect
import windowlog as W
class DB:
    def __init__(self): self.saved={}; self.bulk_error=None; self.one_error=None
    def insert_many(self,docs,ordered=False):
        if self.bulk_error is not None: raise self.bulk_error
        for doc in docs: self.saved[doc['_id']]=doc
    def insert_one(self,doc):
        if self.one_error is not None: raise self.one_error
        if doc['_id'] in self.saved: raise DuplicateKeyError('duplicate',11000)
        self.saved[doc['_id']]=doc
class DetailsError(Exception):
    def __init__(self,details): self.details=details
class Stop(BaseException): pass
class WriterTests(unittest.TestCase):
    def setUp(self):
        W._pend[:]=[(100+i,10.,2,10.) for i in range(3)]; W._writer_col=None; W._writer_client=None; W._writer_factory=None; W._writer_single=False
        W.ST.clear(); self.db=DB(); W._col['idxsec']=self.db
    def test_list_none_string_details_and_dropped_connection(self):
        for exc in (DetailsError([]),DetailsError(None),DetailsError('bad'),ServerSelectionTimeoutError('connection dropped'),AutoReconnect('socket dropped')):
            with self.subTest(kind=type(exc).__name__):
                self.setUp(); self.db.bulk_error=exc
                self.assertFalse(W._flush()); self.assertEqual(len(W._pend),3)
                self.assertEqual(W.ST['secRetries'],1)
                self.db.bulk_error=None
                self.assertTrue(W._flush()); self.assertEqual(len(self.db.saved),3); self.assertEqual(W._pend,[])
    def test_mixed_bulk_duplicates_and_failed_row(self):
        self.db.bulk_error=BulkWriteError({'writeErrors':[{'index':0,'code':11000},{'index':1,'code':121}], 'writeConcernErrors':[]})
        self.assertFalse(W._flush()); self.assertEqual([x[0] for x in W._pend],[101]); self.assertEqual(W.ST['secWritten'],2)
        self.db.bulk_error=None; self.assertTrue(W._flush()); self.assertEqual(W._pend,[])
    def test_duplicate_only_bulk_is_acknowledged(self):
        self.db.bulk_error=BulkWriteError({'writeErrors':[{'index':0,'code':11000}], 'writeConcernErrors':[]})
        self.assertTrue(W._flush()); self.assertEqual(W.ST['secWritten'],3)
    def test_write_concern_ambiguity_retains_all_then_deduplicates(self):
        self.db.saved[100]={}; self.db.bulk_error=BulkWriteError({'writeErrors':[{'index':0,'code':11000}], 'writeConcernErrors':[{'code':64}]})
        self.assertFalse(W._flush()); self.assertEqual(len(W._pend),3)
        self.assertTrue(W._flush()); self.assertEqual(len(self.db.saved),3); self.assertEqual(W.ST['secWritten'],3)
    def test_bad_row_does_not_ack_later_rows(self):
        W._writer_single=True; self.db.one_error=AutoReconnect('offline')
        self.assertFalse(W._flush()); self.assertEqual(len(W._pend),3)
    def test_sanitization_and_bad_str(self):
        uri='mongodb://fixture-user:fixture-pass@example.invalid/test'
        with patch.dict(os.environ,{'MONGODB_URI':uri,'FIXTURE_API_KEY':'fixture-secret'}):
            W._writer_error(RuntimeError(uri+' user=another password=hidden token=xyz fixture-user fixture-pass fixture-secret'))
            for secret in (uri,'fixture-user','fixture-pass','fixture-secret','another','hidden','xyz'): self.assertNotIn(secret,W.ST['secErr'])
        class Bad(Exception):
            def __str__(self): raise ValueError('bad string')
        W._writer_error(Bad()); self.assertIn('unavailable',W.ST['secErr'])
        W._writer_error(RuntimeError("{'password': 'unknown secret', \"username\": \"other user\"}"))
        self.assertNotIn('unknown secret',W.ST['secErr']); self.assertNotIn('other user',W.ST['secErr'])
    def test_supervisor_restarts_failed_routine_with_backoff(self):
        sleeps=[]; connects=[]; passes=[]
        def reconnect(): connects.append(True)
        def work():
            passes.append(True)
            if len(passes)==1: raise RuntimeError('worker failure')
            return True
        def wait(delay):
            sleeps.append(delay)
            if len(sleeps)==2: raise Stop()
        with patch.object(W,'_writer_reconnect',reconnect),patch.object(W,'_writer_pass',work),patch.object(W.time,'sleep',wait):
            with self.assertRaises(Stop): W._writer()
        self.assertEqual(len(connects),2); self.assertEqual(sleeps,[4,2]); self.assertEqual(W.ST['writerRestarts'],1)
    def test_backoff_bounded_and_no_sampler_lock(self):
        delays=[]
        def wait(delay):
            delays.append(delay)
            if len(delays)==8: raise Stop()
        with patch.object(W,'_writer_pass',return_value=False),patch.object(W,'_writer_reconnect'),patch.object(W.time,'sleep',wait):
            with self.assertRaises(Stop): W._writer()
        self.assertEqual(max(delays),60)
        W._sec.clear()
        with patch.object(W,'consolidated',return_value=(10.,10.,2)):
            self.assertTrue(W.sample_once(500)); self.assertEqual(W._sec[-1][0],500)
    def test_sampler_continues_during_blocked_write(self):
        entered=threading.Event(); release=threading.Event()
        class Blocked(DB):
            def insert_many(self,docs,ordered=False):
                entered.set(); release.wait(2); super().insert_many(docs,ordered)
        W._col['idxsec']=Blocked(); W._sec.clear()
        worker=threading.Thread(target=W._flush)
        try:
            worker.start(); self.assertTrue(entered.wait(1))
            with patch.object(W,'consolidated',return_value=(10.,10.,2)):
                self.assertTrue(W.sample_once(900))
            self.assertTrue(worker.is_alive())
        finally: release.set(); worker.join(2)
        self.assertEqual([r[0] for r in W._pend],[900])
    def test_reconnect_only_closes_writer_client(self):
        class Client:
            closed=False
            def close(self): self.closed=True
        own=Client(); fresh=Client(); shared=self.db; replacement=DB()
        W._writer_client=own; W._writer_col=DB(); W._writer_factory=lambda:(fresh,replacement)
        W._writer_reconnect()
        self.assertTrue(own.closed); self.assertFalse(fresh.closed)
        self.assertIs(W._col['idxsec'],shared); self.assertIs(W._writer_col,replacement)
        self.assertEqual(W.ST['writerReconnects'],1)
    def test_stats_has_additive_writer_health(self):
        out=W.stats()
        for key in ('writerAlive','writerHeartbeat','writerLastSuccess','writerLastError','writerLastErrorAt','writerErrors','writerRetries','writerRestarts','writerReconnects','writerBackoffSec'):
            self.assertIn(key,out)
    def test_singleton_start(self):
        old=W._writer_thread; release=threading.Event()
        try:
            W._writer_thread=None
            with patch.object(W,'_writer',lambda:release.wait(2)):
                W._start_writer(None); first=W._writer_thread; W._start_writer(None)
                self.assertIs(first,W._writer_thread); self.assertTrue(first.is_alive())
                release.set(); first.join(2)
        finally: release.set(); W._writer_thread=old
if __name__=='__main__': unittest.main()
