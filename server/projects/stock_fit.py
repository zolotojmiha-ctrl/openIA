"""Shared coupling compatibility and allocation of physical pieces to demand."""
from collections import Counter, defaultdict, deque
from decimal import Decimal

OVERSIZE = ('p1', 'p2')


def length_fits(method, actual, required):
    return bool(actual and required) and (actual >= required if method in OVERSIZE else actual == required)


def reason(item, job, check_diameter=True):
    if item.method != job.method:
        return 'Другой тип конструкции.'
    wall = Decimal(item.wall_mm or 0)
    if not job.wall_mm or (wall < job.wall_mm if job.method in OVERSIZE else wall != job.wall_mm):
        return 'Недостаточная толщина стенки.' if job.method in OVERSIZE else 'Другая толщина стенки.'
    if check_diameter and job.section.diameter_mm and item.diameter_mm != job.section.diameter_mm:
        return 'Другой диаметр.'
    return ''


def consume(job, pieces, remaining=None, check_diameter=True):
    from .stock_planning import requirements
    parts = requirements(job) if remaining is None else remaining
    if parts is None:
        return None
    left = Counter(parts)
    # Short pieces first, taking the longest eligible slot: a larger piece can
    # cover one component, never several smaller components at once.
    for item in sorted(pieces, key=lambda i: i.length_mm or 0):
        if reason(item, job, check_diameter):
            continue
        choices = [length for length, count in left.items()
                   if count > 0 and length_fits(job.method, item.length_mm, length)]
        if choices:
            left[max(choices)] -= 1
    return left


def occupied(job, exclude=()):
    excluded = set(exclude)
    items = [i for i in job.reserved_stock.all() if i.pk not in excluded and i.deleted_at is None]
    counted = {i.coupling_id for i in items if i.coupling_id}
    return items, [u.coupling for u in job.installed_couplings.all() if u.coupling_id not in counted]


def capacity(job, item, reserved, installed):
    if reason(item, job):
        return 0
    left = consume(job, installed, check_diameter=False)
    if left is None:
        return 0
    left = consume(job, reserved, left)
    return sum(count for length, count in left.items()
               if length_fits(job.method, item.length_mm, length))


def allocate(demand, supply):
    """Maximum matching of counted size groups, without expanding stock units.

    Residual paths can reassign an oversized piece to another need, so stock
    that is compatible with several rows is never counted several times.
    """
    demands = [(key, int(q)) for key, q in demand.items() if q > 0]
    supplies = [(key, int(q)) for key, q in supply.items() if q > 0]
    graph = defaultdict(dict)
    source, sink = ('source',), ('sink',)

    def edge(a, b, count):
        graph[a][b] = count
        graph[b][a] = 0

    for n, (key, count) in enumerate(demands):
        edge(('need', n), sink, count)
    for n, (actual, count) in enumerate(supplies):
        node = ('stock', n)
        edge(source, node, count)
        for m, (required, need) in enumerate(demands):
            method, diameter, length, wall = required
            if (actual[:2] == (method, diameter)
                    and length_fits(method, actual[2], length)
                    and (actual[3] >= wall if method in OVERSIZE else actual[3] == wall)):
                edge(node, ('need', m), min(count, need))
    while True:
        parents = {source: None}
        queue = deque([source])
        while queue and sink not in parents:
            node = queue.popleft()
            for target, count in graph[node].items():
                if count > 0 and target not in parents:
                    parents[target] = node
                    queue.append(target)
        if sink not in parents:
            break
        node, count = sink, None
        while parents[node] is not None:
            parent = parents[node]
            count = graph[parent][node] if count is None else min(count, graph[parent][node])
            node = parent
        node = sink
        while parents[node] is not None:
            parent = parents[node]
            graph[parent][node] -= count
            graph[node][parent] += count
            node = parent
    return Counter({key: graph[sink][('need', n)] for n, (key, _) in enumerate(demands)})
