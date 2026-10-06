import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from app.mapping import normalize, map_point, map_interval, new_duration, InvalidEditMap


def segs_keep_all(duration=60000):
    return normalize([{"kind": "keep", "src_start": 0, "src_end": duration}], duration)


class TestMapping(unittest.TestCase):
    def test_keep_all_identity(self):
        s = segs_keep_all(60000)
        self.assertEqual(map_point(0, s), 0)
        self.assertEqual(map_point(30000, s), 30000)
        self.assertEqual(map_interval(8000, 15000, s), (8000, 15000))
        self.assertEqual(new_duration(s), 60000)

    def test_cut_shifts_later_points(self):
        # 删掉 10–15s：之后的时间整体前移 5s
        s = normalize([
            {"kind": "keep", "src_start": 0, "src_end": 10000},
            {"kind": "cut", "src_start": 10000, "src_end": 15000},
            {"kind": "keep", "src_start": 15000, "src_end": 60000},
        ], 60000)
        self.assertEqual(new_duration(s), 55000)
        self.assertEqual(map_point(5000, s), 5000)
        self.assertIsNone(map_point(12000, s))          # 被删区间内
        self.assertEqual(map_point(15000, s), 10000)   # 切后起点平移
        self.assertEqual(map_point(60000, s), 55000)   # 结尾边界
        # 区间完整落在切后保留区：重定位成功
        self.assertEqual(map_interval(20000, 25000, s), (15000, 20000))
        # 横跨切口：不能映射
        self.assertIsNone(map_interval(8000, 20000, s))
        # 完全在被删区：不能映射
        self.assertIsNone(map_interval(11000, 12000, s))

    def test_replace_is_unmappable(self):
        s = normalize([
            {"kind": "keep", "src_start": 0, "src_end": 20000},
            {"kind": "replace", "src_start": 20000, "src_end": 30000,
             "dst_start": 20000, "dst_end": 28000},
            {"kind": "keep", "src_start": 30000, "src_end": 60000},
        ], 60000)
        self.assertEqual(new_duration(s), 58000)
        self.assertIsNone(map_point(25000, s))              # 换入新音轨，旧注释无意义
        self.assertEqual(map_point(30000, s), 28000)       # 后续保持映射
        self.assertIsNone(map_interval(15000, 25000, s))   # 横跨换轨点

    def test_invalid_maps_rejected(self):
        with self.assertRaises(InvalidEditMap):
            normalize([{"kind": "keep", "src_start": 100, "src_end": 60000}], 60000)
        with self.assertRaises(InvalidEditMap):
            normalize([{"kind": "keep", "src_start": 0, "src_end": 50000}], 60000)
        with self.assertRaises(InvalidEditMap):
            normalize([
                {"kind": "keep", "src_start": 0, "src_end": 10000},
                {"kind": "keep", "src_start": 12000, "src_end": 60000},
            ], 60000)


if __name__ == "__main__":
    unittest.main()
