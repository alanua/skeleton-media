"""Durable bounded HLS attempt reservation; no resolver or player access."""
import os
from pathlib import Path
import re
import sqlite3
import math


class SQLiteRecoveryBudget:
    def __init__(self, path):
        self.path = Path(path)

    def reserve(self, session, now):
        if (not isinstance(session,str) or not re.fullmatch('[0-9a-f]{64}',session)
                or isinstance(now,bool) or not isinstance(now,(int,float))
                or not math.isfinite(now) or now < 0):
            return {'state':'budget_unavailable'}
        connection = None
        try:
            self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            parent = self.path.parent.stat()
            if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                return {'state':'budget_unavailable'}
            if not self.path.exists():
                descriptor=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                os.close(descriptor)
            stat=self.path.lstat()
            if not self.path.is_file() or self.path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                return {'state':'budget_unavailable'}
            connection=sqlite3.connect(self.path,timeout=.25)
            connection.execute('PRAGMA busy_timeout=250')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('CREATE TABLE IF NOT EXISTS attempts(session TEXT PRIMARY KEY,count INTEGER NOT NULL,last_at REAL NOT NULL)')
            connection.execute('BEGIN IMMEDIATE')
            row=connection.execute('SELECT count,last_at FROM attempts WHERE session=?',(session,)).fetchone()
            if row:
                count,previous=row
                if type(count) is not int or not 1<=count<=2 or not isinstance(previous,(int,float)) or not math.isfinite(previous):
                    return {'state':'budget_unavailable'}
                if count>=2:return {'state':'retry_exhausted'}
                if now-previous<60:return {'state':'cooldown'}
            else:
                if connection.execute('SELECT count(*) FROM attempts').fetchone()[0]>=128:
                    return {'state':'session_capacity'}
                count=0
            connection.execute('INSERT OR REPLACE INTO attempts(session,count,last_at) VALUES(?,?,?)',(session,count+1,now))
            connection.commit()
            return {'state':'reserved','attempt':count+1}
        except Exception:
            return {'state':'budget_unavailable'}
        finally:
            if connection is not None:connection.close()
