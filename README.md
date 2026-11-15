# 环境监测数仓 · 字段变更核查 + 观测契约校验

两个工具共用一个服务：

1. **字段变更核查**（`/`，原有功能）：原始站点表字段调整上线前，上传「变更前/后源字段清单 +
   跨作业字段依赖」三份 JSON，逐列给出 **正常 / 断供 / 待核实** 判定和完整依赖路径。
2. **观测契约校验**（`/contract`，新增）：值班数据员在站点观测批次入库前，按已发布的数据
   契约拦截格式不合格的记录——上传带版本号的契约 JSON 与观测 CSV，逐行标出错误并生成
   隔离清单，仅全量通过的批次可发布。

技术栈：FastAPI + SQLite + 原生 HTML/JS 单页，Docker Compose 一键运行。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000        （字段变更核查）
# 打开 http://localhost:8000/contract （观测契约校验）
```

页面上可直接点「载入内置示例核查」一键体验，或下载示例文件修改后上传：
`examples/sources_before.json`、`examples/sources_after.json`、`examples/dependencies.json`。
契约校验页同样内置了契约 v1/v2（放宽）/v3（违规）与合格/含错观测 CSV 示例，可一键演示
「发布契约 → 校验批次 → 契约兼容演进 → 违规契约被拒 → 失败批次隔离」的完整流程。

本地无 Docker 运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

运行单元测试（仅标准库，无需安装依赖）：

```bash
python3 -m unittest discover -s tests -v
```

## 判定口径

每个产出列允许 **多组输入**：

- **组内全部输入可用，该组才可用；任一组可用即可产出** → `正常`
  （主用组失效但有完整备用组时，仍然正常，判定说明会标注「含备用组」）
- 所有输入组都确定不可用（源字段缺失、或上游列自身已断供）→ `断供`
- 仅能依靠 **缺失引用**（依赖清单中不存在的作业/列、既非源表也非作业的引用）
  或 **无源支撑的循环** 才能产出 → `待核实`

源字段缺失时会对照另一版清单标注「本版已移除（对照版存在）」还是「两版均无」。
判定沿依赖链传播：上游断供会导致下游断供；上游待核实只会让下游待核实，不会升级为断供。
「最终列」= 未被任何其他产出列消费的列；`external` 标记的列视为对外数据集列，页面默认聚焦这两类。

## 版本与一致性

- 核查（解析 + 评估）全部在写库前完成，**核查进行中旧版照常可查**；
- 成功后在一个事务内整版替换（旧版归档），**失败保留旧版**并在历史中留痕；
- 每个快照自包含三份输入与判定报告，查询只读 `active` 的唯一版本，**绝不混用两版**；
- SQLite 开启 WAL，写入期间并发读取不受阻塞。

## 观测契约校验（/contract）

值班数据员在观测批次入库前，先用**现役契约**拦截格式不合格的记录。

### 契约 JSON 格式

```json
{
  "version": "1.0.0",
  "columns": [
    {"name": "obs_id",   "type": "string",   "required": true, "unique": true,
     "pattern": "[A-Z]{2}-\\d{6}"},
    {"name": "station",  "type": "string",   "required": true,
     "allowed": ["ST-01", "ST-02"]},
    {"name": "utc_hour", "type": "utc_hour", "required": true},
    {"name": "reading",  "type": "number",   "required": true, "min": -40, "max": 50},
    {"name": "unit",     "type": "string",   "required": true, "allowed": ["°C"]}
  ]
}
```

- 类型：`string` / `integer` / `number` / `boolean` / `utc_hour`
  （UTC 整点，ISO 8601，可带偏移，换算后必须对齐整点）；
- 公共键：`required`（可空性）、`unique`（批次内唯一）、`description`；
- 分类型约束：`string` 支持 `max_length` / `pattern`（整体匹配）/ `allowed`（枚举，
  **单位约束**即 `unit` 列的 `allowed`）；`integer` / `number` 支持 `min` / `max`。

### 契约演进（兼容）规则

新契约只有两类变更能替换现役契约，其余一律**拒绝并保留旧版**（拒绝记录留痕）：

- **新增可选列**（`required: false`）；
- **放宽约束**：版本号递增、必填转可空、关闭 `unique`、`min` 下调或删除、
  `max` / `max_length` 上调或删除、`allowed` 扩大或删除、`pattern` 删除。

**删列、改类型、新增必填列、任何收紧**（含模式变更，因无法证明是放宽）均判定不兼容。

### 批次校验与隔离

- 上传观测 CSV，按**上传时的现役契约快照**逐行校验：表头（缺必填列/未知列/列名重复）
  为批次级错误；单元格的类型、可空性、范围、枚举、模式、唯一性错误逐行记录；
- **仅全量通过的批次可发布**；任何错误即整批隔离，隔离清单含 CSV 物理行号、
  逐列错误与原始记录，**失败不改变上次可用批次**；
- 每个批次（含隔离批次）都固化校验时的契约快照；契约更新与批次上传交错时，
  查询某批次只返回它自己绑定的快照，**绝不混用现役契约**。

## 输入文件格式

**源字段清单**（变更前/后各一份，三种写法等价）：

```json
{ "tables": { "raw_air_station_hourly": ["station_id", "pm25"] } }
{ "raw_air_station_hourly": ["station_id", "pm25"] }
[ "raw_air_station_hourly.station_id", "raw_air_station_hourly.pm25" ]
```

**跨作业字段依赖**：

```json
{
  "jobs": [
    {
      "name": "job_air_quality_hourly",
      "outputs": {
        "wind_speed_avg": [
          ["raw_air_station_hourly.wind_speed"],
          ["job_meteo_clean.wind_speed"]
        ]
      }
    }
  ],
  "external": ["job_daily_report.report_wind"]
}
```

- `outputs.列名` 的值是 **组的数组**：外层每一项是一组备用输入，组内是所有必须同时可用的输入；
  简写 `"col": ["t.a", "t.b"]`（元素非数组）等价于单组 `[["t.a", "t.b"]]`。
- 输入引用 `"属主.名"` 自动判定：属主是已定义作业 → 上游列；是源表 → 源字段；都不是 → 缺失引用（待核实）。
  同名歧义时可用 `{"source": "t.c"}` 或 `{"column": "j.c"}` 强制指定。
- `external`（可选）列出对外数据集列，必须引用已定义的产出列。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/snapshots` | multipart 上传 `sources_before` / `sources_after` / `dependencies` + 可选 `name`；成功整版发布，失败 400 且保留旧版 |
| GET | `/api/snapshots/current` | 现役版本全量内容（输入 + 判定报告） |
| GET | `/api/snapshots` | 版本历史（含失败记录） |
| POST | `/api/contracts` | multipart 上传 `contract`（契约 JSON）；兼容则替换现役契约，不兼容 400 且保留旧版并留痕 |
| GET | `/api/contracts/current` | 现役契约（版本 + 列定义） |
| GET | `/api/contracts` | 契约历史（现役/已归档/已拒绝及拒绝原因） |
| POST | `/api/batches` | multipart 上传 `observations`（CSV）+ 可选 `name`；按现役契约快照校验，200 返回 `state=published/quarantined` 与隔离清单 |
| GET | `/api/batches/current` | 上次可用批次（最新发布成功）及其绑定的契约快照 |
| GET | `/api/batches` | 批次历史（已发布/已隔离） |
| GET | `/api/batches/{id}` | 批次详情：隔离清单或类型化记录 + 绑定的契约快照 |
| GET | `/examples/{name}` | 示例文件下载（JSON/CSV） |
| GET | `/` | 字段变更核查页面 |
| GET | `/contract` | 契约校验页面 |

## 目录结构

```
app/
  main.py            FastAPI 入口与接口（核查 + 契约 + 批次）
  evaluator.py       字段变更：解析 + 三值判定 + 依赖路径生成（纯标准库）
  contract.py        观测契约：解析 + 兼容性判定 + CSV 逐行校验（纯标准库）
  store.py           SQLite 存储（快照/契约/批次，整版原子替换、失败留痕、WAL）
  static/index.html  字段变更核查页面
  static/contract.html  契约校验页面
examples/            核查示例 + 契约 v1/v2/v3 与合格/含错观测 CSV
tests/               单元测试（evaluator 19 例 + contract 28 例，仅标准库）
Dockerfile / compose.yaml / requirements.txt
```
