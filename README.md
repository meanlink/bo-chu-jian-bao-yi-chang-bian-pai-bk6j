# 播出简报异常编排

异常摘要与定时分发的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery。

### 连续审计封存
审计轨迹页之外的“审计封存”页把已提交业务动作按顺序连续封存为证据包：每包列明审计范围（起止编号）、条数与前包包头指纹，形成不可跳过的哈希链。封存期间产生的新动作留给下一包；同一时间只允许一包在途，两人并发封存只出一包，范围不交叠、不漏项；写包中断后保留待重试包，重试复用同一包号，不会重复出包，未完成登记的包不可下载。
验真以数据库中单独登记的封存头（最后包号、包头指纹、最后审计编号）为准，并与落库快照、原始审计逐条核对，可发现记录改写、误删、尾部整包移除与包间/范围断点并给出断点位置；不依赖包内自检指纹。负责人与分析员可创建、重试和下载，观察员只读且可验真；原有分析与分发流程不受影响。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18219；后端文档 http://localhost:19219/docs；数据库宿主端口 17719。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、封存、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、封存、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读、下载与验真；不能创建封存包|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；MySQL 8.0；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。
审计封存：backend/src/services/seals.py（封存、重试、整链与单包验真）、backend/src/models/seal.py（封存头、包索引、快照）、backend/src/routes/seals.py；页面 frontend/src/pages/audit.js，路由 #/audit。封存文件位于共享产物卷 `/artifacts/audit-seals/`，索引 index.json 可由封存头重建；封存管理动作单独记录，不进入业务封存包。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=bulletin-ewma；FRONTEND_PORT=18219；BACKEND_PORT=19219；DB_PORT=17719；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1020/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
