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
- `waybill`：运单，记录批次从哪个种植点起运、运到哪里、运送时段。
- `stay`：棚时记录，记录批次在哪个棚、哪个时段停留，是同期接触判定的依据。
- `lockdown`：封控记录，按阳性批次、运单溯源和棚时接触计算出的冻结范围。
- `pending`：待核对记录，断网点回连后对不上的登记单列于此。

## 封控规则

- 一个棚得出阳性结论（`facility` 的 `conclude`，`pest_found=true`）或批次阳性（`consignment` 的 `quarantine`）后，触发封控重算：
  - 顺运单找上头来源（`trace_upstream`），起运种植点一并冻结；
  - 同期到过这些棚的批次算作接触（棚时时段重叠），接触批次冻结调运；
  - 批次和种植点一起冻结，种植点只有在仍有阳性批次撑着时才保持冻结。
- 运送时段或棚时一变（`waybill`/`stay` 的 `correct`），封控范围立即重算；阳性批次被销毁或放开后，没有阳性撑着的种植点自动放开。
- 两个管理员同时提交同一个棚的结论时先到为准：后提交的 `expected_version` 对不上，返回 `409 ConflictError`。
- 断网点登记回连：`POST /api/offline/sync` 按现场时间（`site_recorded_at`）合并；实体不存在或记录冲突的，单列待核对（`GET /api/pending`），由管理员 `resolve`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/offline/sync`：断网点登记回连，请求体`{"records":[...],"point_id":"...","site_recorded_at":"..."}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
