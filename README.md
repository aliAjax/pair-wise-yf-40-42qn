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

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点；`zone`：疫区。

## 疫区管理

- 检疫员（`inspector`/`admin`）通过`POST /api/zones`登记疫区：必填`zone_no`（疫区编号，全局唯一）、`center_facility_id`（中心设施）、`facility_ids`（纳入设施）、`reason`（纳入原因）；可选`facility_reasons`按设施单独登记原因、`positive_since`指定中心设施阳性日期（默认当天）。中心设施自动并入纳入设施，并记为首个阳性事件。
- 疫区登记即生效（`active`）。生效期间，起点设施在区内而终点设施在区外的批次，`ship`调运动作会被拒绝并提示拦截疫区；区内互调不受影响。
- `mark_positive`动作登记区内新增阳性：新阳性设施自动追加进纳入设施，最近阳性日期随之更新，二十一天观察期重新计算。
- `lift`动作解除疫区：须满足最近一次阳性满21天且观察期内无新增阳性设施，否则返回未满足的条件。解除后（`lifted`）原被拦批次可重新发起`ship`调运。
- 疫区视图（`GET /api/zones`、`GET /api/entities/<id>`）的`data.release`字段实时给出解除条件：已过天数、是否满21天、观察期内新增阳性设施、最早可解除日期；生效中的疫区还带`blocked_consignment_ids`（当前被拦批次）。
- 批次可携带`origin_facility_id`/`destination_facility_id`关联设施；`ship`动作（`declared`/`released`→`shipped`）会复用批次上已登记的设施，也可在动作数据里补充。

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
