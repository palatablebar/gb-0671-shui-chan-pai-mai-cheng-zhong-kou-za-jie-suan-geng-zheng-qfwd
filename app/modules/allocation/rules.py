from decimal import Decimal, ROUND_HALF_UP
from app.coerce import number, money
from app.errors import BusinessError

Q = Decimal('.001')

def kg(value):
    return Decimal(value).quantize(Q, rounding=ROUND_HALF_UP)

def _pending(ticket, package, buyer, code, detail):
    return {'ticket': ticket, 'package': package, 'buyer': buyer, 'code': code, 'detail': detail}

def parse_deals(raw):
    items, bad = [], []
    for d in raw if isinstance(raw, list) else []:
        pack = ticket = buyer = ''
        try:
            if not isinstance(d, dict): raise BusinessError('BAD_DEAL', '成交凭据须为对象')
            pack, ticket, buyer = (str(d.get(k, '')).strip() for k in ('package', 'ticket', 'buyer'))
            if not pack or not ticket or not buyer: raise BusinessError('BAD_DEAL', '成交凭据缺货包、票号或买家')
            deal_kg, price = number(d.get('deal_kg')), number(d.get('price'))
            if deal_kg < 0 or price < 0: raise BusinessError('BAD_DEAL', '成交重量或单价越界')
            items.append({'package': pack, 'ticket': ticket, 'buyer': buyer, 'deal_kg': str(deal_kg), 'price': str(price)})
        except BusinessError as error:
            bad.append(_pending(ticket, pack, buyer, error.code, error.message))
    groups = {}
    for d in items: groups.setdefault(d['package'], []).append(d)
    good = []
    for pack, group in groups.items():
        identities = {(g['ticket'], g['buyer'], g['deal_kg'], g['price']) for g in group}
        if len(identities) > 1:
            bad.extend(_pending(g['ticket'], pack, g['buyer'], 'DUPLICATE_PACKAGE', '同一货包存在不同成交凭据') for g in group)
        else:
            good.append(group[0])
    return good, bad

def parse_reweighs(raw):
    good, dupes, bad = {}, set(), []
    for r in raw if isinstance(raw, list) else []:
        pack = ''
        try:
            if not isinstance(r, dict): raise BusinessError('BAD_REWEIGH', '复秤记录须为对象')
            pack = str(r.get('package', '')).strip()
            weight = number(r.get('reweigh_kg'))
            if not pack or weight <= 0: raise BusinessError('BAD_REWEIGH', '复秤记录缺货包或重量越界')
            if pack in good: dupes.add(pack)
            good[pack] = str(weight)
        except BusinessError as error:
            bad.append(_pending('', pack, '', error.code, error.message))
    for pack in dupes: good.pop(pack)
    return good, dupes, bad

def allocate(final, deals, reweighs, dupes, blocked):
    """整票计价重分摊到逐买家货包：已复秤货包采用复秤值，剩余按原成交份额比例分摊，
    尾差归集到份额最大（并列取货包号最小）的未复秤货包；分摊合计与整票计价重对平才产出。"""
    rows, pending, tickets = [], [], []
    final_map = {t['ticket']: t for t in final}
    by_ticket = {}
    for d in deals: by_ticket.setdefault(d['ticket'], []).append(d)
    dealt = {d['package'] for d in deals}
    for pack in sorted(reweighs):
        if pack not in dealt:
            pending.append(_pending('', pack, '', 'ORPHAN_REWEIGH', '复秤无对应货包成交'))
    for ticket in sorted(by_ticket):
        packs = by_ticket[ticket]
        ticket_row = {'ticket': ticket, 'billable_kg': '', 'allocated_kg': '', 'balanced': False, 'packages': len(packs)}
        tickets.append(ticket_row)
        current = final_map.get(ticket)
        if current is None:
            code, detail = ('TICKET_BLOCKED', '票存在版本冲突被阻断') if ticket in blocked else ('TICKET_NOT_EFFECTIVE', '票未生效或不存在')
            pending.extend(_pending(ticket, p['package'], p['buyer'], code, detail) for p in packs)
            continue
        rate, whole = Decimal(current['rate']), Decimal(current['billable_kg'])
        ticket_row['billable_kg'] = str(whole)
        valid = [p for p in packs if p['package'] not in dupes]
        pending.extend(_pending(ticket, p['package'], p['buyer'], 'DUPLICATE_REWEIGH', '货包存在多条复秤，无法采信')
                       for p in packs if p['package'] in dupes)
        results, rest = {}, whole
        for p in valid:
            if p['package'] in reweighs:
                adopted = kg(Decimal(reweighs[p['package']]) * (1 - rate))
                results[p['package']] = {'billable': adopted, 'basis': '复秤值'}
                rest -= adopted
        def block(code, detail):
            pending.extend(_pending(ticket, p['package'], p['buyer'], code, detail) for p in valid)
            if not ticket_row['allocated_kg']: ticket_row['allocated_kg'] = str(whole - rest)
        if rest < 0:
            block('REWEIGH_OVERFLOW', '复秤合计超过整票计价重，整票未对平'); continue
        unweighed = [p for p in valid if p['package'] not in reweighs]
        share_total = sum((Decimal(p['deal_kg']) for p in unweighed), Decimal(0))
        if unweighed and share_total <= 0:
            block('NO_SHARE', '未复秤货包无原成交份额，整票未对平'); continue
        if not unweighed and rest != 0:
            block('RESIDUAL_UNALLOCATED', '全部复秤后剩余重量无处分摊，整票未对平'); continue
        allocated = Decimal(0)
        for p in unweighed:
            share = kg(rest * Decimal(p['deal_kg']) / share_total)
            results[p['package']] = {'billable': share, 'basis': '比例分摊'}
            allocated += share
        residual = rest - allocated
        if residual:
            target = sorted(unweighed, key=lambda p: (-Decimal(p['deal_kg']), p['package']))[0]
            entry = results[target['package']]
            entry['billable'] += residual
            entry['basis'] = '比例分摊+尾差'
        total = sum((r['billable'] for r in results.values()), Decimal(0))
        ticket_row['allocated_kg'] = str(total)
        if total != whole:
            block('UNBALANCED', '分摊合计与整票计价重不符，整票未对平'); continue
        ticket_row['balanced'] = True
        for p in sorted(valid, key=lambda p: p['package']):
            adopted = results[p['package']]
            deal_billable = kg(Decimal(p['deal_kg']) * (1 - rate))
            amount = money(adopted['billable'] * Decimal(p['price']))
            deal_amount = money(deal_billable * Decimal(p['price']))
            rows.append({'ticket': ticket, 'package': p['package'], 'buyer': p['buyer'],
                         'deal_kg': p['deal_kg'], 'deal_billable_kg': str(deal_billable),
                         'basis': adopted['basis'], 'billable_kg': str(adopted['billable']),
                         'delta_kg': str(adopted['billable'] - deal_billable),
                         'price': p['price'], 'amount': amount, 'deal_amount': deal_amount,
                         'delta_amount': money(Decimal(amount) - Decimal(deal_amount)),
                         'version': current['version']})
    return rows, pending, tickets

def apply(state):
    deals, bad_deals = parse_deals(state['context'].get('deals'))
    reweighs, dupes, bad_reweighs = parse_reweighs(state['context'].get('reweighs'))
    blocked = {c['ticket'] for c in state['version_conflicts']}
    rows, pending, tickets = allocate(state['final'], deals, reweighs, dupes, blocked)
    pending = sorted(pending + bad_deals + bad_reweighs, key=lambda p: (p['ticket'], p['package']))
    state['allocation'] = {'rows': rows, 'pending': pending, 'tickets': tickets}
    state['views']['allocation'] = {'title': '货包分摊', 'rows': rows, 'metrics': {
        '分摊货包': len(rows), '待处理货包': len(pending), '对平票': sum(1 for t in tickets if t['balanced'])}}
