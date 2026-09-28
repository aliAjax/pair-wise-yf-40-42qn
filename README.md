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
- `zone`：疫区。检疫员在确认苗圃出现病虫害后登记，把中心设施（阳性苗圃）以及借过同一批苗、共用过车辆等需要一并管控的种植点连同原因纳入。

## 疫区管理

- 登记疫区（`POST /api/zones`）：必填 `code`（疫区编号，全局唯一）、`center_facility_id`（中心设施）；`facilities` 为纳入设施列表，每项包含 `facility_id` 和 `reason`（纳入原因）。可选用 `declared_at` 指定生效日期，默认为当天；中心设施当天自动记为一次阳性事件。可由 `admin`、`inspector`、`quarantine` 登记。
- 调运管控：新增 `consignment` 动作 `dispatch`（调运）。批次通过 `origin_facility_id` / `destination_facility_id` 关联设施；生效疫区期间，区内批次不能调运到区外（区内流转、区外调入不受影响）。被拦截时批次停留在 `declared`，不会改变状态。
- 上报新增阳性设施：`report_positive` 动作可把新阳性设施追加进疫区并重置21天观察期（`facility_id` 必填，`positive_at` 默认当天）。
- 解除疫区：`lift` 动作。条件为最近一次阳性之后已满21天，且21天观察窗口内没有新增阳性设施；条件不满足时返回400。解除后原来被拦住的批次可以重新发起 `dispatch`。
- `GET /api/zones/<id>/detail?as_of=YYYY-MM-DD`：查看疫区详情，包括纳入设施（名称、原因、中心/纳入角色）和逐条解除条件是否满足；`as_of` 省略时按当天计算。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（`kind`支持 `consignments`、`facilities`、`zones` 复数别名）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/zones/<id>/detail`：疫区纳入设施与解除条件视图。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
