import pytest
from app.engine import evaluate
from app import store
from app.errors import BusinessError

@pytest.fixture
def state():
    return evaluate({'dataset':'clean'})

def rows(state,ticket):
    return {r['package']:r for r in state['allocation']['rows'] if r['ticket']==ticket}

def pending_codes(state,package):
    return [p['code'] for p in state['allocation']['pending'] if p['package']==package]

# ---------- 分摊算法 ----------

def test_reweigh_adopted_real(state):
    row=rows(state,'00001')['A-01']
    assert row['basis']=='复秤值' and row['billable_kg']=='58.900' and row['delta_kg']=='1.900'

def test_remainder_to_unweighed_real(state):
    row=rows(state,'00001')['A-02']
    assert row['basis']=='比例分摊' and row['billable_kg']=='45.600' and row['delta_amount']=='93.48'

def test_package_deltas_sum_to_ticket_correction(state):
    # 逐货包差额合计必须等于整票更正待付，不允许抹平复秤差异
    from decimal import Decimal
    group=rows(state,'00001')
    assert sum(Decimal(r['delta_kg']) for r in group.values())==Decimal('9.500')
    assert sum(Decimal(r['delta_amount']) for r in group.values())==Decimal('116.85')
    assert [f['due'] for f in state['final'] if f['ticket']=='00001']==['116.85']

def test_proportional_split_real(state):
    group=rows(state,'00002')
    assert group['B-01']['billable_kg']=='48.556' and group['B-02']['billable_kg']=='38.844'

def test_tail_residual_to_largest_real(state):
    group=rows(state,'00004')
    assert group['C-01']['billable_kg']=='29.766' and group['C-01']['basis']=='比例分摊+尾差'
    assert group['C-02']['billable_kg']=='29.767' and group['C-03']['billable_kg']=='29.767'

def test_ticket_balanced_real(state):
    tickets={t['ticket']:t for t in state['allocation']['tickets']}
    for ticket in ('00001','00002','00004'):
        assert tickets[ticket]['balanced'] and tickets[ticket]['allocated_kg']==tickets[ticket]['billable_kg']

def test_reweigh_overflow_blocks_whole_ticket(state):
    assert rows(state,'00005')=={}
    assert pending_codes(state,'D-01')==['REWEIGH_OVERFLOW'] and pending_codes(state,'D-02')==['REWEIGH_OVERFLOW']
    ticket={t['ticket']:t for t in state['allocation']['tickets']}['00005']
    assert not ticket['balanced']

def test_ghost_ticket_pending(state):
    assert pending_codes(state,'X-01')==['TICKET_NOT_EFFECTIVE']

def test_orphan_reweigh_pending(state):
    assert pending_codes(state,'Z-99')==['ORPHAN_REWEIGH']

def test_no_share_blocks_ticket(state):
    s=evaluate({'overrides':{'deals':[{'package':'E-01','ticket':'00002','buyer':'买家甲','deal_kg':'50','price':'12.30'},
                                      {'package':'E-02','ticket':'00002','buyer':'买家乙','deal_kg':'0','price':'12.30'}],
                             'reweighs':[{'package':'E-01','reweigh_kg':'50'}]}})
    assert rows(s,'00002')=={}
    assert pending_codes(s,'E-01')==['NO_SHARE'] and pending_codes(s,'E-02')==['NO_SHARE']

def test_duplicate_package_rejected(state):
    s=evaluate({'overrides':{'deals':[{'package':'E-01','ticket':'00002','buyer':'甲','deal_kg':'50','price':'12.30'},
                                      {'package':'E-01','ticket':'00002','buyer':'乙','deal_kg':'10','price':'12.30'}]}})
    assert rows(s,'00002')=={}
    assert pending_codes(s,'E-01')==['DUPLICATE_PACKAGE','DUPLICATE_PACKAGE']

# ---------- 发布：幂等、中断恢复、确认保护 ----------

@pytest.fixture
def db(tmp_path):
    return str(tmp_path/'alloc.sqlite')

def test_publish_idempotent(state,db):
    first=store.publish_allocation(db,state)
    second=store.publish_allocation(db,state)
    assert first['version']==second['version']==1
    assert len(store.list_releases(db,state['manifest']['run_id']))==1

def test_rerun_same_correction_no_new_release(state,db):
    store.save(db,state)
    rid=state['manifest']['run_id']
    store.publish_allocation(db,state)
    rerun=evaluate({'dataset':state['source']},snapshot=state['input_snapshot'])
    assert rerun['manifest']['run_id']==rid
    again=store.publish_allocation(db,store.get(db,rid))
    assert again['version']==1 and len(store.list_releases(db,rid))==1

def _insert_zombie(db,run_id,key='zombie-key',version=1):
    with store.connect(db) as con:
        con.execute("INSERT INTO allocation_releases(release_key,run_id,version,status,payload,created_at) VALUES(?,?,?,?,?,?)",
                    (key,run_id,version,'publishing','{}','2026-11-20T00:00:00+00:00'))

def test_interrupted_publish_rolls_back(state,db):
    rid=state['manifest']['run_id']
    _insert_zombie(db,rid)  # 模拟上次发布中断留下的 publishing 残留
    release=store.publish_allocation(db,state)
    assert release['status']=='published' and release['version']==2
    statuses=[r['status'] for r in store.list_releases(db,rid)]
    assert statuses==['rolled_back','published']  # 残留被回滚且记录保留，未丢失

def test_crash_before_switch_keeps_old_current(state,db):
    rid=state['manifest']['run_id']
    old=store.publish_allocation(db,state)
    changed=evaluate({'dataset':'clean','overrides':{'reweighs':[{'package':'A-01','reweigh_kg':'63'}]}})
    _insert_zombie(db,changed['manifest']['run_id'],key=changed['manifest']['run_id'],version=2)
    # 切换前中断：旧版仍是当前已发布版
    current=store.current_release(db,rid)
    assert current['status']=='published' and current['version']==old['version']
    # 恢复重发：残留回滚，旧版被取代但保留，新版成为当前
    release=store.publish_allocation(db,changed)
    assert release['status']=='published' and release['version']==3
    assert store.current_release(db,rid)['status']=='superseded'
    statuses=[r['status'] for r in store.list_releases(db,changed['manifest']['run_id'])]
    assert statuses==['rolled_back','published']

def test_superseded_kept_no_data_loss(state,db):
    old_rid=state['manifest']['run_id']
    store.save(db,state)
    store.publish_allocation(db,state)
    changed=evaluate({'dataset':'clean','overrides':{'reweighs':[{'package':'A-01','reweigh_kg':'63'}]}})
    new_rid=changed['manifest']['run_id']
    store.publish_allocation(db,changed)
    assert [r['status'] for r in store.list_releases(db,old_rid)]==['superseded']
    assert [r['status'] for r in store.list_releases(db,new_rid)]==['published']
    old=store.list_releases(db,old_rid)[0]
    assert old['payload']['deals'] and old['payload']['rows']  # 旧版成交凭据与结果快照仍在
    assert store.get(db,old_rid)['records']  # 过磅记录不因发布变化丢失

def test_confirmed_record_never_overwritten(state,db):
    rid=state['manifest']['run_id']
    store.publish_allocation(db,state)
    store.confirm_allocation(db,rid,'A-01','58.900')
    # 同一更正重跑：不新增、已确认不动
    again=store.publish_allocation(db,state)
    assert again['version']==1
    # 新更正改变该货包计价重：冲突货包转待处理，确认记录保留原值
    changed=evaluate({'dataset':'clean','overrides':{'reweighs':[{'package':'A-01','reweigh_kg':'63'}]}})
    release=store.publish_allocation(db,changed)
    conflict=[p for p in release['payload']['pending'] if p['package']=='A-01']
    assert conflict and conflict[0]['code']=='CONFIRMED_CONFLICT'
    assert all(r['package']!='A-01' for r in release['payload']['rows'])
    confirms=store.list_confirms(db)
    assert len(confirms)==1 and confirms[0]['payload']['billable_kg']=='58.900'

def test_confirm_conflict_409(state,db):
    rid=state['manifest']['run_id']
    store.publish_allocation(db,state)
    with pytest.raises(BusinessError) as err:
        store.confirm_allocation(db,rid,'A-01','1.000')
    assert err.value.status==409

def test_confirm_idempotent(state,db):
    rid=state['manifest']['run_id']
    store.publish_allocation(db,state)
    store.confirm_allocation(db,rid,'A-01','58.900')
    again=store.confirm_allocation(db,rid,'A-01','58.900')
    assert again['billable_kg']=='58.900' and len(store.list_confirms(db))==1

def test_confirm_requires_publish(state,db):
    with pytest.raises(BusinessError) as err:
        store.confirm_allocation(db,state['manifest']['run_id'],'A-01','58.900')
    assert err.value.code=='NO_RELEASE'

def test_unbalanced_ticket_not_published(state,db):
    release=store.publish_allocation(db,state)
    assert all(r['ticket']!='00005' for r in release['payload']['rows'])
    assert any(p['code']=='REWEIGH_OVERFLOW' for p in release['payload']['pending'])

# ---------- HTTP 层 ----------

def client(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'test.sqlite'))
    return TestClient(main.app)

def test_page_and_api(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=run['manifest']['run_id']
    assert c.post('/api/allocation',json={}).status_code==200
    html=c.get('/allocation?run_id='+rid).text
    assert '采用依据' in html and '待处理货包' in html and '原成交 kg' in html

def test_publish_confirm_http(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={'dataset':'clean'}).json()['manifest']['run_id']
    first=c.post('/api/allocation/publish',json={'run_id':rid}).json()
    assert first['version']==1
    again=c.post('/api/allocation/publish',json={'run_id':rid}).json()
    assert again['version']==1  # 同一更正重跑不新增
    ok=c.post('/api/allocation/confirm',json={'run_id':rid,'package':'A-01','billable_kg':'58.900'})
    assert ok.status_code==200
    stale=c.post('/api/allocation/confirm',json={'run_id':rid,'package':'A-01','billable_kg':'1.000'})
    assert stale.status_code==409
    current=c.get('/api/allocation/'+rid).json()
    assert current['status']=='published'
    assert len(c.get('/api/allocation/'+rid+'/releases').json())==1

def test_publish_unknown_run_http(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    assert c.post('/api/allocation/publish',json={'run_id':'missing'}).status_code==404
