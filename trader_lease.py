"""Fail-closed Mongo ownership, CAS renewal/acquisition; no forced boot takeover."""
import threading
import time

class TraderLease:
    def __init__(self,collection,instance,ttl=45):
        self.collection=collection; self.instance=instance; self.ttl=ttl
        self.lock=threading.RLock(); self.held=False; self.retired=False; self.error=None
    def check(self):
        with self.lock:
            if self.retired or self.collection is None: return False
            now=time.time()
            try:
                from pymongo import ReturnDocument
                # Creation cannot overwrite an existing lease, even when processes boot together.
                self.collection.update_one({'_id':'lease'},{'$setOnInsert':dict(inst=self.instance,at=now,hb=now)},upsert=True)
                doc=self.collection.find_one_and_update(
                    {'_id':'lease','$or':[{'inst':self.instance},{'hb':{'$lt':now-self.ttl}}]},
                    {'$set':dict(inst=self.instance,hb=now)},return_document=ReturnDocument.AFTER)
                ok=bool(doc and doc.get('inst')==self.instance)
                if self.held and not ok: self.retired=True
                self.held=ok; self.error=None
                return ok
            except Exception as e:
                self.error=type(e).__name__; self.held=False
                return False
