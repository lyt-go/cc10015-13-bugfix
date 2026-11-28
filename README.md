# 海洋科考航次管理系统

首席科学家、调查队员和船长共同规划航次站位，记录海洋观测数据，签认样品移交后按标准流程归档。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 航次计划 | `cruise_plan` | 航次计划书 | 航次编号、调查海域、起止日期 |
| 站位布设 | `station_deploy` | 站位记录 | 站位编号、经纬度坐标、水深数据 |
| 设备清单 | `equipment_manifest` | 设备记录 | 设备名称、设备类型、出厂编号 |
| 采样记录 | `sampling_record` | 采样单 | 采样站位、采样深度、样品类型 |
| 水文测量 | `hydro_measure` | 水文剖面 | 测量站位、温度剖面、盐度剖面 |
| 生物拖网 | `bio_trawl` | 拖网记录 | 拖网站位、网具型号、拖网深度 |
| 沉积物采样 | `sediment_sample` | 沉积物样品 | 采样站位、采样深度、柱状长度 |
| 数据质控 | `data_qc` | 质控记录 | 数据来源、质控标准、异常值个数 |
| 队员排班 | `crew_schedule` | 排班表 | 队员姓名、岗位职责、作业时段 |
| 气象观测 | `weather_obs` | 气象记录 | 观测时间、风速风向、浪高数据 |
| 样品登记 | `sample_registry` | 样品登记簿 | 样品编号、样品类型、采集站位 |
| 分析申请 | `analysis_request` | 分析申请单 | 申请样品、分析项目、分析方法 |
| 航次日志 | `cruise_log` | 航海日志 | 记录日期、船舶位置、作业内容 |
| 仪器校准 | `instrument_calib` | 校准记录 | 仪器名称、校准项目、标准参数 |
| 潜水作业 | `diving_ops` | 潜水记录 | 潜水地点、潜水深度、潜水时长 |
| 数据传输 | `data_transfer` | 传输记录 | 数据内容、数据量、传输方式 |
| 航迹记录 | `track_record` | 航迹点 | 记录时间、经纬度、航速 |
| 成果汇编 | `results_compile` | 汇编报告 | 航次编号、成果类别、主要发现 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 样品入库同步回执

样品登记簿的一行代表一份**实物**，不是样品编号。同一编号在不同站位（例如站位 A 的
W9 与站位 B 的 W9）是两份实物，来源补送按「样品编号 + 采集站位 / 登记项」落位。

- 采样单的「样品入库」只把来源补送到登记项，并生成一张入库同步回执；
  登记簿的库存状态（已入库 / 已移交）只由登记簿动作推进，采样补送不得退回前置状态。
- 登记详情 `GET /api/sample_registry/{id}` 带 `入库同步回执`、`关联实物`、
  `实际保管`、`取样资格`、`后续处理`；原列表 / 快照接口同样带
  `实际保管`、`取样资格`、`analysis_eligible`。
- 回执按来源单据幂等：`GET /api/sampling_record/{id}/sync-receipt`、
  `GET /api/sample_inbound_receipts`、`GET /api/sample_inbound_receipts/{回执编号}`。
  来源侧已成功后失联，重送只找回原回执，不生成新的入库工作。
- 迟来的补送更正保存批次时，批次进入「需核对」；已移交的实物只能由接收人退还复查，
  办结前不开放新的取样。分析申请凭该登记项自己的有效入库记录取得资格，同号另一份
  实物的状态不影响。

## 测试

```bash
cd backend && .venv/bin/python -m pytest -q
```

领域：应用开发
子方向：服务端开发
