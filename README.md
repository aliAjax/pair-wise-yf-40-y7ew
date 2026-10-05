# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点。
- `stay`：批次在种植点的驻留记录（`consignment_id`、`facility_id`、`arrived_at`、`departed_at`），是同棚接触追溯的基础。同一批次的驻留时段不允许重叠。

## 封控与追溯

- 批次被检疫（`quarantine`）后自动重算封控范围：顺运单（`parent_id` 与同一 `waybill_no`）找上头来源批次，同期同棚的批次记为接触批次，一并冻结调运（`frozen` 标记，禁止 `dispatch`）；所在种植点置为 `frozen`。
- 驻留时段通过 `stay` 的 `reschedule` 动作调整后，封控范围自动重算；不再重叠的接触批次自动解冻。
- 种植点只有在没有阳性批次支撑时才被放开（自动解除或 `lift` 动作均校验）；已被结论为阳性的种植点不允许放开。
- 种植点结论（`conclude`）先到为准：已有结论再提交返回 409；携带过期 `expected_version` 的并发提交同样冲突。阳性结论会冻结种植点。
- 手动封控：`POST /api/facilities/<id>/lockdown`；手动重算：`POST /api/facilities/<id>/recompute`；范围预览：`GET /api/facilities/<id>/scope`。

## 断网点离线登记

- `POST /api/stays/sync`，请求体 `{"items": [...]}`，每项带 `occurred_at`（现场时间，缺省取 `arrived_at`）。回连后按现场时间排序合并，重复记录幂等跳过；对不上的（时段冲突、引用不存在的批次或种植点）单列进待核对队列。
- `GET /api/pending-reviews` 查看待核对；`POST /api/pending-reviews/<id>/resolve`，请求体 `{"decision": "accept"|"discard"}` 处理后触发重算。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
