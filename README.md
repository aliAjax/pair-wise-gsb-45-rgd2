# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突和冲突检查。
- `src/reception_rules.py`：污染物接收判定（吨升换算、车辆适配、排班、到场结算、离泊闸门）。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：演示页面（靠泊记录、待收区原因、排班看板）。
- `tests/`：完整流程、规则计算、接收排班和失败场景测试。

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

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 污染物接收排班

靠泊计划申报增加 `oily_water_tonnes`（污油水吨）、`sewage_tonnes`（生活污水吨）和`dangerous_class`（危险品类别，危险品船必填）。吨为申报单位、升为结算单位（1吨=1000升）。船舶靠泊（berth动作）后按申报量自动生成接收任务。

- `POST /api/reception/vehicles`：接收车登记，`data`含`plate`、`capacity_litres`（罐容/升）、`medium`（`oily_water`/`sewage`）、`service_start_hour`/`service_end_hour`（可服务时段）。
- `GET /api/reception/vehicles`：接收车登记册。
- `POST /api/reception/vehicles/{id}/active`：停用/启用接收车，body为`{"active":false}`。
- `POST /api/reception/schedule`：对待收任务排班，可选`{"record_id":n}`只排某船。
- `GET /api/reception/waiting`：待收区（含`pending_reason`与`remaining_litres`缺口升数）。
- `GET /api/reception/board`：看板（待收区+已排班/到场时段），可带`record_id`。
- `GET /api/records/{id}/reception-tasks`：某船两种污水的接收任务。
- `POST /api/reception/slots/{id}/arrival`：到场登记实收量，`data`为`{"actual_litres":n}`。

排班规则：逐小时匹配介质相容、车辆可服务时段、同时段未服务他船、罐容足够；同一辆车同一时段不能接两船；介质不相容或容量不足留在待收区，原因写明（如“容量不足：车C-OILY罐容3,000升，污油水还差2,000升”）。到场实收不足的任务转回待收区并保留原因，再排班时滚动到下一可服务时段（原时段标记`incomplete`留痕）。`depart`离泊前校验两种污水全部结清，未结清返回409并列出各介质缺口。接收操作允许角色`reception_operator`、`reception_manager`、`port_controller`和`admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
