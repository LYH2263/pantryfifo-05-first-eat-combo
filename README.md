# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

## 先吃组合（recipe_suggest）

组合页按当前在架**正余量**（`qty_remain>0` 且 `data_quality='clean'`）对多品做 FEFO 拟扣：

- `POST /api/combo/preview` — 只读预览拟扣量，不改 lots。
- `POST /api/combo/confirm` — 整组原子写入：`BEGIN IMMEDIATE` 内重读库存重新规划，
  与单品消费 `/api/consume`、过期下架 `/api/expire-sweep` 互斥到一种结果；
  一品不够则整组 409 回滚，不写任何一品（all-or-nothing，与并发旁路共存）。
- dirty / 非正余量批次不进组合 deductions；确认快照（含当时 `warn_days`）写入
  `consumptions` 后不再回改，`PUT /api/settings` 改 warn_days 只影响顶条现算。
- 确认后回「全层」页按分品余量小计核对这些品的剩余。

0-1：`shopping_list` / `temp_zone`。
