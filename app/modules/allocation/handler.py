from fastapi import APIRouter, Request, Body
from app.config import MODULES
from app.engine import evaluate
from app.web import render
from app.errors import BusinessError
from app import store

router=APIRouter()

def _db():
    from app.main import DB_PATH
    return DB_PATH

@router.get('/allocation')
def page(request:Request):
    db=_db()
    def extra(state):
        state.setdefault('allocation',{'rows':[],'pending':[],'tickets':[]})
        run_id=state['manifest']['run_id']
        confirms=store.list_confirms(db)
        return {'release':store.current_release(db,run_id),
                'confirmed':{(c['ticket'],c['package']) for c in confirms}}
    return render(request,'allocation',extra)

@router.post('/api/allocation')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=len(MODULES))

@router.post('/api/allocation/publish')
def publish(payload:dict=Body(...)):
    run_id=payload.get('run_id')
    if not isinstance(run_id,str) or not run_id: raise BusinessError('BAD_RUN','缺少批次号')
    state=store.get(_db(),run_id)
    if 'allocation' not in state: raise BusinessError('INCOMPLETE_RUN','批次尚未计算分摊')
    return store.publish_allocation(_db(),state)

@router.post('/api/allocation/confirm')
def confirm(payload:dict=Body(...)):
    run_id,package,billable=payload.get('run_id'),payload.get('package'),payload.get('billable_kg')
    if not run_id or not package or not isinstance(billable,str) or not billable:
        raise BusinessError('BAD_CONFIRM','确认须带批次号、货包与计价重')
    return store.confirm_allocation(_db(),run_id,str(package),billable)

@router.get('/api/allocation/{run_id}')
def current(run_id:str):
    release=store.published_release(_db(),run_id)
    if not release: raise BusinessError('NO_RELEASE','当前无已发布分摊结果',404)
    return release

@router.get('/api/allocation/{run_id}/releases')
def releases(run_id:str):
    return store.list_releases(_db(),run_id)
