# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。含污染物（污油水/生活污水）接收排班。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验（资料）。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突，以及接收排班判定：介质适配、危险品类别、罐容与时段冲突、结转（判定）。
- `src/repository.py`：SQLite建表、事务和查询，含接收车与接收任务表（保存）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应（接口）。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面，可查看待收区原因。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/vehicles`：登记接收车，请求体为`{"vehicle_no":"...","capacity_liters":8000,"media":["oily_water","sewage"],"dangerous_classes":["3"],"slots":[8,10,12]}`。
- `GET /api/vehicles`：接收车列表。
- `POST /api/records/{id}/reception/schedule`：对该航次待收任务排班。
- `GET /api/records/{id}/reception`：该航次接收任务列表。
- `GET /api/reception/pending`：待收区列表，含待收原因与短量（升）。
- `POST /api/reception/tasks/{id}/receive`：到场登记实收量，请求体为`{"actual_liters":1200,"reason":"..."}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。接收车登记、排班与实收登记需`reception_operator`角色（或`admin`）。

## 污染物接收排班

- 靠泊计划在`data`中申报`oily_water_tons`（污油水）与`sewage_tons`（生活污水）吨数及危险品类别，创建时按1吨=1000升生成接收任务，初始进入待收区（`pending`，原因"待排班"）。
- 排班判定：介质不相容、危险品类别无适配车辆、时段被排满或罐容不足的任务留在待收区，`pending_reason`写明原因、`shortfall_liters`写明还差多少升；同一辆车同一时段只能接一船，时段须落在靠泊窗口`[eta_hour, etd_hour)`内。
- 到场登记实收量：收满即结清（`settled`）；未收满的按同车下一可用时段结转并保留原因（`carry_overs`），无后续时段则回待收区。
- 离泊（`depart`）前必须结清全部接收任务，否则返回409并列出各介质剩余量与原因。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
