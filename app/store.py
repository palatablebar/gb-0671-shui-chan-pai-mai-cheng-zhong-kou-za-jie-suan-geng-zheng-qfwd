import sqlite3, json, hashlib
from pathlib import Path
from datetime import datetime, timezone
from app.errors import BusinessError

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核');
CREATE TABLE IF NOT EXISTS allocation_releases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  release_key TEXT NOT NULL,
  run_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_release_live ON allocation_releases(release_key) WHERE status IN ('publishing','published');
CREATE TABLE IF NOT EXISTS allocation_confirms (
  ticket TEXT NOT NULL,
  package TEXT NOT NULL,
  run_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  confirmed_at TEXT NOT NULL,
  PRIMARY KEY (ticket, package)
);
"""

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(SCHEMA)
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def _release(row):
    data=dict(row);data['payload']=json.loads(data['payload']);return data

def _now():
    return datetime.now(timezone.utc).isoformat()

def current_release(path,run_id):
    with connect(path) as con:
        row=con.execute("SELECT * FROM allocation_releases WHERE run_id=? AND status IN ('published','superseded') ORDER BY version DESC LIMIT 1",(run_id,)).fetchone()
    return _release(row) if row else None

def published_release(path,run_id):
    with connect(path) as con:
        row=con.execute("SELECT * FROM allocation_releases WHERE run_id=? AND status='published'",(run_id,)).fetchone()
    return _release(row) if row else None

def list_releases(path,run_id):
    with connect(path) as con:
        return [_release(r) for r in con.execute('SELECT * FROM allocation_releases WHERE run_id=? ORDER BY version',(run_id,))]

def list_confirms(path):
    with connect(path) as con:
        return [_release(r) for r in con.execute('SELECT * FROM allocation_confirms ORDER BY ticket,package')]

def publish_allocation(path,state):
    """两阶段发布分摊结果：先落 publishing 再原子切换 published。
    中断残留的 publishing 一律回滚为 rolled_back，旧 published 保持当前版；
    同一更正内容摘要相同直接返回已发布版，不新增；与已确认货包冲突的行转待处理，确认记录不动。"""
    run_id=state['manifest']['run_id']
    alloc=state['allocation']
    payload={'run_id':run_id,'rows':alloc['rows'],'pending':alloc['pending'],'tickets':alloc['tickets'],
             'deals':state['context'].get('deals',[]),'reweighs':state['context'].get('reweighs',[]),
             'rule_version':state['manifest']['rule_version']}
    key=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    with connect(path) as con:
        con.execute("UPDATE allocation_releases SET status='rolled_back' WHERE status='publishing'")
        row=con.execute("SELECT * FROM allocation_releases WHERE release_key=? AND status='published'",(key,)).fetchone()
        if row: return _release(row)
        confirms={(r['ticket'],r['package']):json.loads(r['payload']) for r in con.execute('SELECT ticket,package,payload FROM allocation_confirms')}
        rows,blocked=[],[]
        for r in payload['rows']:
            confirmed=confirms.get((r['ticket'],r['package']))
            if confirmed and confirmed.get('billable_kg')!=r['billable_kg']:
                blocked.append({'ticket':r['ticket'],'package':r['package'],'buyer':r['buyer'],'code':'CONFIRMED_CONFLICT',
                                'detail':f"已确认计价重{confirmed['billable_kg']}kg，新结果{r['billable_kg']}kg，保留已确认记录"})
            else: rows.append(r)
        if blocked: payload={**payload,'rows':rows,'pending':payload['pending']+blocked}
        version=con.execute('SELECT COALESCE(MAX(version),0)+1 FROM allocation_releases').fetchone()[0]
        con.execute('INSERT INTO allocation_releases(release_key,run_id,version,status,payload,created_at) VALUES(?,?,?,?,?,?)',
                    (key,run_id,version,'publishing',json.dumps(payload,ensure_ascii=False),_now()))
    with connect(path) as con:
        con.execute("UPDATE allocation_releases SET status='superseded' WHERE status='published'")
        con.execute("UPDATE allocation_releases SET status='published' WHERE release_key=? AND status='publishing'",(key,))
    return published_release(path,run_id)

def confirm_allocation(path,run_id,package,expected_billable):
    """确认当前发布中的货包结果。同值重复确认为幂等；已确认记录数值不符返回409，绝不覆盖。"""
    with connect(path) as con:
        rel=con.execute("SELECT * FROM allocation_releases WHERE run_id=? AND status='published' ORDER BY version DESC LIMIT 1",(run_id,)).fetchone()
        if not rel: raise BusinessError('NO_RELEASE','当前无已发布分摊结果',404)
        row=next((r for r in json.loads(rel['payload'])['rows'] if r['package']==package),None)
        if not row: raise BusinessError('NOT_FOUND','货包不在当前发布中',404)
        if row['billable_kg']!=expected_billable: raise BusinessError('STALE_VERSION','发布结果已变化，请刷新',409)
        old=con.execute('SELECT payload FROM allocation_confirms WHERE ticket=? AND package=?',(row['ticket'],package)).fetchone()
        if old:
            if json.loads(old['payload']).get('billable_kg')==row['billable_kg']:
                return json.loads(old['payload'])
            raise BusinessError('CONFIRMED_CONFLICT','已确认记录不可更改',409)
        con.execute('INSERT INTO allocation_confirms(ticket,package,run_id,payload,confirmed_at) VALUES(?,?,?,?,?)',
                    (row['ticket'],package,run_id,json.dumps(row,ensure_ascii=False),_now()))
        return row
