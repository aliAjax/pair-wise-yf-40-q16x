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

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。批次可附`parent_id`关联来源批次。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/trace`：传播链台账，列出全部源头批次及其下游数量；没有历史来源的旧批次按独立起点显示。
- `GET /api/trace/<id>`：从任意批次回溯到源头，查看整条链条、每个批次的当前状态和下游数量。
- `POST /api/trace/<id>/quarantine`：源头批次发现阳性（已隔离）后，批量隔离其全部下游批次；已销毁、已放行或已隔离的批次自动跳过并在结果中列出。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 传播链台账

- 批次创建时通过`parent_id`记录来源批次，`origin`/`destination`记录去向；父批次必须存在且不能自引用或成环。
- 批量隔离要求源头批次处于`quarantined`（阳性）状态，执行角色为`admin`或`quarantine`；下游`declared`/`inspected`状态的批次直接隔离，已销毁、已放行或已隔离的跳过，操作可安全重试。
- 链条数据全部落在SQLite，服务重启后页面仍可按源头查看整条链条、当前状态和下游数量。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
