REASONS = {
    'COLUMN_COUNT':'列数错误', 'MISSING_ID':'编号缺失', 'DUPLICATE_CONFLICT':'重复编号冲突',
    'MISSING_VALUE':'数值缺失', 'BAD_NUMBER':'数值格式错误', 'BAD_TIME':'时间格式错误',
    'BAD_UNIT':'单位不受支持', 'UNKNOWN_REFERENCE':'引用对象不存在', 'INVALID_RANGE':'范围错误',
    'VERSION_CONFLICT':'版本冲突', 'EMPTY_FILE':'空文件', 'BLANK_FILE':'全空行',
    'HEADER_ONLY':'仅有表头', 'STALE_VERSION':'复核版本已更新', 'NOT_FOUND':'批次不存在',
    'TICKET_NOT_EFFECTIVE':'票未生效或不存在', 'TICKET_BLOCKED':'票版本冲突被阻断',
    'REWEIGH_OVERFLOW':'复秤合计超整票计价重', 'NO_SHARE':'无原成交份额',
    'RESIDUAL_UNALLOCATED':'剩余重量无处分摊', 'UNBALANCED':'分摊未对平',
    'ORPHAN_REWEIGH':'复秤无对应货包', 'BAD_DEAL':'成交凭据无效',
    'BAD_REWEIGH':'复秤记录无效', 'DUPLICATE_PACKAGE':'货包重复成交',
    'DUPLICATE_REWEIGH':'货包多条复秤', 'CONFIRMED_CONFLICT':'与已确认记录冲突',
    'NO_RELEASE':'无已发布分摊结果',
}
def reject(state, record, code, detail):
    state['quarantine'].append({'line':record.get('_line',0),'id':record.get('id',''),
       'code':code,'detail':detail,'raw':{k:v for k,v in record.items() if not k.startswith('_')}})
