"""Short-lived read reuse for one Telegram update, never across requests.

Only three exact primary-key lookups are reusable. All other statements flush
this small cache conservatively; transactions and locking reads always execute.
"""
import asyncio
import copy
import logging
import re
import time
from contextvars import ContextVar, copy_context
from telegram.ext import Application

LOG=logging.getLogger(__name__)
_SCOPE=ContextVar('biology_request_reads',default=None)
_READ=re.compile(r'^SELECT \* FROM biology_(students|student_modules|tasks) WHERE (user_id|id)=%s;$')

class ResponsiveApplication(Application):
    async def process_update(self,update):
        state={'rows':{},'active':True,'hits':0,'queries':0,'owner':asyncio.current_task()}
        token=_SCOPE.set(state);started=time.monotonic()
        try:return await super().process_update(update)
        finally:
            state['active']=False
            elapsed=time.monotonic()-started
            if elapsed>=1:
                LOG.info('Update latency %.3fs; database statements=%s reused_reads=%s',elapsed,state['queries'],state['hits'])
            _SCOPE.reset(token)

class Cursor:
    def __init__(self,factory,state):
        self.factory=factory;self._raw=None;self.state=state;self.cached=None;self.metadata=None
    @property
    def raw(self):
        if self._raw is None:
            self._raw=self.factory();self._raw.__enter__()
        return self._raw
    def __getattr__(self,name):return getattr(self.raw,name)
    @property
    def description(self):return self.metadata[0] if self.metadata else self.raw.description
    @property
    def rowcount(self):return self.metadata[1] if self.metadata else self.raw.rowcount
    def __enter__(self):return self
    def __exit__(self,*args):return self._raw.__exit__(*args) if self._raw is not None else False
    def execute(self,statement,params=None,*args,**kwargs):
        self.cached=None;self.metadata=None
        text=' '.join(statement.split()) if isinstance(statement,str) else ''
        match=_READ.fullmatch(text)
        valid=match and ((match[1]=='tasks' and match[2]=='id') or (match[1]!='tasks' and match[2]=='user_id'))
        key=(text,tuple(params or ())) if valid else None
        state=self.state
        if not state['active']:return self.raw.execute(statement,params,*args,**kwargs)
        if key is not None and key in state['rows']:
            state['hits']+=1
            rows,self.metadata=state['rows'][key]
            self.cached=copy.deepcopy(rows);return self
        # Even another SELECT may call a mutating function. No broad SQL guessing.
        if key is None:state['rows'].clear()
        state['queries']+=1
        self.raw.execute(statement,params,*args,**kwargs)
        if key is not None:
            rows=self.raw.fetchall()
            self.cached=copy.deepcopy(rows)
            self.metadata=(self.raw.description,self.raw.rowcount)
            if len(state['rows'])>=128:state['rows'].clear()
            state['rows'][key]=(copy.deepcopy(rows),self.metadata)
        return self
    def executemany(self,*args,**kwargs):
        self.cached=None;self.metadata=None;self.state['rows'].clear()
        return self.raw.executemany(*args,**kwargs)
    def fetchone(self):
        if self.cached is None:return self.raw.fetchone()
        return self.cached.pop(0) if self.cached else None
    def fetchall(self):
        if self.cached is None:return self.raw.fetchall()
        rows=self.cached;self.cached=[];return rows

class Connection:
    def __init__(self,manager,state):self.manager=manager;self.state=state;self.raw=None
    def _open(self):
        if self.raw is None:self.raw=self.manager.__enter__()
        return self.raw
    def __enter__(self):return self
    def __exit__(self,*args):
        if args[0]:self.state['rows'].clear()
        try:return self.manager.__exit__(*args) if self.raw is not None else False
        except Exception:
            self.state['rows'].clear();raise
    def __getattr__(self,name):return getattr(self._open(),name)
    def cursor(self,*args,**kwargs):
        if args or kwargs:
            self.state['rows'].clear();return self._open().cursor(*args,**kwargs)
        return Cursor(lambda:self._open().cursor(),self.state)
    def execute(self,statement,params=None,*args,**kwargs):
        return self.cursor().execute(statement,params,*args,**kwargs)
    def commit(self):
        if self.raw is not None:return self.raw.commit()
    def rollback(self,*args,**kwargs):
        self.state['rows'].clear()
        if self.raw is not None:return self.raw.rollback(*args,**kwargs)

def install(db):
    original=db.connect
    def connect():
        manager=original();state=_SCOPE.get()
        return Connection(manager,state) if state and state['active'] else manager
    async def run(fn):
        state=_SCOPE.get()
        token=None
        if state and state['owner'] is not asyncio.current_task():token=_SCOPE.set(None)
        try:context=copy_context()
        finally:
            if token is not None:_SCOPE.reset(token)
        return await asyncio.get_running_loop().run_in_executor(None,context.run,fn)
    db.connect=connect;db.run=run
