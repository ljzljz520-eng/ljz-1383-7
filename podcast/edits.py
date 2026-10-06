"""编辑映射（edit map）：把旧时间轴上的注释重定位到新时间轴。

操作语义（时间单位毫秒，半开区间 [start, end)）：
  {"op":"delete","start":S,"end":E}   删除旧时间轴 [S,E)
  {"op":"insert","at":P,"length":L}   在旧位置 P 之前插入 L 毫秒新内容

重定位规则：
  - 区间两端都能映射且中间没有被删内容 -> 平移到新秒数
  - 任一端落入被删区域、区间横跨删除点、或映射后长度为零 -> 无法映射 -> pending_repair
  绝不原封保留旧秒数。
"""

def _merge_deletes(deletes):
    deletes = sorted(deletes)
    merged = []
    for s, e in deletes:
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(x) for x in merged]

def build_atoms(ops):
    """把操作编译成原子区间列表：[(old_start, old_end, kind, new_start)]。
    kind: 'keep' 有线性映射 new = old + (new_start - old_start)；'delete' 无映射。"""
    deletes = _merge_deletes([(o["start"], o["end"]) for o in ops if o.get("op") == "delete"])
    inserts = sorted([(o["at"], o["length"]) for o in ops if o.get("op") == "insert" and o.get("length", 0) > 0])
    points = {0}
    for s, e in deletes:
        points.add(s); points.add(e)
    for at, _ in inserts:
        points.add(at)
    pts = sorted(points)
    ins_at = {}
    for at, ln in inserts:
        ins_at[at] = ins_at.get(at, 0) + ln

    atoms, offset = [], 0
    for i, a in enumerate(pts):
        offset += ins_at.get(a, 0)          # 插入作用于其后的旧内容
        b = pts[i + 1] if i + 1 < len(pts) else None
        if b is None:
            break
        if b <= a:
            continue
        if any(ds <= a and b <= de for ds, de in deletes):
            atoms.append((a, b, "delete", None))
            offset -= (b - a)
        else:
            atoms.append((a, b, "keep", a + offset))
    # 尾部：最后一个切点之后的内容沿最终累计偏移整体平移
    tail_start = pts[-1] if pts else 0
    atoms.append((tail_start, float("inf"), "keep", tail_start + offset))
    return atoms

def _map_point(t, atoms, as_end=False):
    last_keep = None
    for a, b, kind, ns in atoms:
        if kind == "keep":
            last_keep = (a, b, ns)
        if as_end:
            if a < t <= b:
                return (ns + (t - a)) if kind == "keep" else None
        else:
            if a <= t < b:
                return (ns + (t - a)) if kind == "keep" else None
    if as_end and last_keep and t >= last_keep[1]:   # 末尾之后：沿最后 keep 段外推
        return last_keep[2] + (t - last_keep[0])
    if not as_end and last_keep and t >= last_keep[1]:
        return last_keep[2] + (t - last_keep[0])
    return None

def relocate_range(start_ms, end_ms, ops):
    """返回 (new_start, new_end) 或 (None, reason)。"""
    if end_ms <= start_ms:
        return None, "unmappable"
    atoms = build_atoms(ops)
    ns = _map_point(start_ms, atoms, as_end=False)
    if ns is None:
        return None, "source_deleted"
    ne = _map_point(end_ms, atoms, as_end=True)
    if ne is None:
        return None, "partial_overlap"
    for a, b, kind, _ in atoms:                      # 区间内部横跨删除点
        if kind == "delete" and start_ms < b and a < end_ms:
            return None, "partial_overlap"
    if ne <= ns:
        return None, "unmappable"
    return (ns, ne), None
