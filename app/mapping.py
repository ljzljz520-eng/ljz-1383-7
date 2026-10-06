"""时间码编辑映射引擎。

重新剪辑 = 在旧时间线上声明若干 keep / cut / replace 段：
  keep    旧音频保留，但在新时间线上整体平移；
  cut     被删除（如删掉一段嘉宾发言）；
  replace 换音轨（新内容，语义不连续，旧注释无法落到新内容上）。

所有点/区间都必须经过映射才能在新版本上使用；不能映射的内容
（落入 cut/replace，或横跨非保留区间）一律置空时间码并进入待修复。
系统不会“原封保留旧秒数”。
"""
from __future__ import annotations

from dataclasses import dataclass

KEEP, CUT, REPLACE = "keep", "cut", "replace"


@dataclass
class Seg:
    kind: str
    src_start: int
    src_end: int
    dst_start: int | None = None
    dst_end: int | None = None

    def overlaps_interval(self, a: int, b: int) -> bool:
        """半开区间 [a,b) 与本段是否有正长度的相交；仅端点接触（零长度）不算。"""
        lo, hi = max(a, self.src_start), min(b, self.src_end)
        return hi > lo


class InvalidEditMap(ValueError):
    pass


def normalize(raw: list[dict], src_duration_ms: int) -> list[Seg]:
    """校验并规整编辑段：必须从 0 连续铺满到 src_duration_ms。"""
    if not raw:
        raise InvalidEditMap("编辑映射为空")
    segs = []
    for r in raw:
        kind = r["kind"]
        if kind not in (KEEP, CUT, REPLACE):
            raise InvalidEditMap(f"未知段类型: {kind}")
        s, e = int(r["src_start"]), int(r["src_end"])
        if s < 0 or e <= s:
            raise InvalidEditMap("非法源区间")
        segs.append(Seg(kind, s, e, r.get("dst_start"), r.get("dst_end")))
    segs.sort(key=lambda x: x.src_start)
    if segs[0].src_start != 0:
        raise InvalidEditMap("编辑映射必须从 0ms 开始")
    cursor = 0
    dst_cursor = 0
    for s in segs:
        if s.src_start != cursor:
            raise InvalidEditMap(f"映射段不连续：期望 {cursor}，实际 {s.src_start}")
        if s.kind == KEEP:
            span = s.src_end - s.src_start
            s.dst_start = dst_cursor
            s.dst_end = dst_cursor + span
            dst_cursor += span
        elif s.kind == REPLACE:
            if s.dst_end is None or s.dst_start is None or int(s.dst_end) <= int(s.dst_start):
                raise InvalidEditMap("replace 段必须给出目标区间")
            s.dst_start, s.dst_end = int(s.dst_start), int(s.dst_end)
            dst_cursor = s.dst_end
        else:  # cut
            s.dst_start = s.dst_end = None
        cursor = s.src_end
    if cursor != src_duration_ms:
        raise InvalidEditMap(f"映射未覆盖完整旧时长 {src_duration_ms}，只到 {cursor}")
    return segs


def new_duration(segs: list[Seg]) -> int:
    return max(s.dst_end or 0 for s in segs)


def locate(t: int, segs: list[Seg]) -> Seg | None:
    """定位点 t 所属段。

    边界约定：点 t 等于某段 src_start 时归属该段（边界属于起点侧）。
    因此切到 cut/replace 边界上的点不可映射；keep 段起点（包括最后一段
    的结束边界）可映射。
    """
    # 总时长终点：最后一段若是 keep，映射为其 dst_end
    last = segs[-1]
    if t == last.src_end and last.kind == KEEP:
        return last
    for s in segs:
        if s.src_start <= t < s.src_end:
            return s
    return None


def map_point(t: int | None, segs: list[Seg]) -> int | None:
    """单个时间点重定位（点视为段起点语义）；cut/replace 上的点 -> None。"""
    if t is None:
        return None
    s = locate(t, segs)
    if s is None or s.kind != KEEP:
        return None
    return s.dst_start + (t - s.src_start)


def map_endpoint(t: int | None, segs: list[Seg]) -> int | None:
    """区间右端点重定位（点视为段结束语义）。

    t 等于某段 src_end 且该段为 keep 时允许（例如 [0,8000) 整段保留时，
    终点 8000 映射到 8000）；t 等于 cut/replace 的 src_start（=前段 cut 结尾）
    不允许。
    """
    if t is None:
        return None
    for s in segs:
        if s.kind == KEEP and s.src_start < t <= s.src_end:
            return s.dst_start + (t - s.src_start)
    return None


def map_interval(a: int | None, b: int | None, segs: list[Seg]) -> tuple[int, int] | None:
    """区间重定位。

    仅当整个 [a,b) 完整落在 keep 区域（可以横跨相邻 keep 段）时成功；
    任何与 cut/replace 的相交都判定不可映射 -> None。
    终点 b 允许恰好等于某个 keep 段的源结束边界。
    """
    if a is None or b is None or b < a:
        return None
    for s in segs:
        if s.kind != KEEP and s.overlaps_interval(a, b):
            return None
    na = map_point(a, segs)
    nb = map_endpoint(b, segs)
    if na is None or nb is None or nb < na:
        return None
    return na, nb
