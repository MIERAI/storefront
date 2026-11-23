# storefront v2 复盘（M7）

> 版本 v0.2（2026-11-23）· v0.2：独立扩容的结果（§2.4，#21）；更正"小新和 winpc 在同一个局域网"的说法
> 版本 v0.1（2026-11-23）· 草稿，独立扩容待补
> 范围：设计书 [`design/v2-microservices.md`](design/v2-microservices.md)（v0.4.1）的 M0–M6；M6 的做法和原始结果见
> [`m6-verification.md`](m6-verification.md)。
> 写法：每个结论都给 PR 编号或测量值；测不出来的写"没测"，不写"应该可以"。

---

## 0. 一句话结论

从设计评审通过（#8，2026-11-22 10:56 JST）到四个承诺全部测完（2026-11-23 晚）大约一天半。**四个承诺里三个有测量证据通过（独立部署、故障隔离、最终一致），外加"不超卖"。第四个（独立扩容）FAIL：只扩 order + inventory，拐点是 1.0×（45/s → 45/s）。这次实验找到了精确、可修的根因：运行时在每个写 outbox 的事务里发 NOTIFY，导致提交在每个 Postgres 实例上串行**（§2.4）。
后端 5 个服务的 29 个 API 里 28 个由 api-factory 确定性生成并带着验证证据交付；进镜像的代码里手写占 5.2%，其中九成是一个工厂缺口（G31）的绕过代码复制了 5 份。
但 **生产上的下单仍然是 v1**：设计书里的绞杀步骤 M3–M5 没有在生产做，v2 的整套服务只在 lab 上跑过验证。

---

## 1. 时间线

| 时间（JST） | 事件 | 证据 |
|---|---|---|
| 11-22 10:56 | M0：设计书 v0.1 合并 = 设计评审通过 | #8 |
| 11-22 11:04 | 决定"先利其器"：工厂能生成 95% 的 API 之后再开发 | api-factory #19（readiness 文档，分母 28） |
| 11-22 17:51 | 设计书 v0.3：与工厂的 R4 / R5 设计对齐（状态存订单行、没有 PENDING、UUIDv7、RESERVING 15 分钟期限） | #9；api-factory #31 |
| 11-22 17:16 – 11-23 08:42 | 工厂 R1 → R5、R4.1：6 → 9 → 13 → 19 → 27 / 28 → 28 / 29 | api-factory #29、#28、#32、#34、#39、#42、#44 |
| 11-23 02:55 / 08:41 | GitHub Actions 当月花费上限用完 → api-factory 公开（MIT），托管 runner 免费 | api-factory #46 |
| 11-23 09:11 | 工厂：审查打转的修复 | api-factory #48 |
| 11-23 09:59 | M2 / S1：catalog（第一个 v2 服务）+ web 的 `CATALOG_URL` 开关 | #10、#11；gitops-manifests #123、#124 |
| 11-23 11:34 | inventory / payment / notification / order 进仓库；整套 v2 部署到 lab（`v2-lab`） | #13；gitops-manifests #127 |
| 11-23 11:54 | M6 的测量工具 | #14 |
| 11-23 12:00 – 13:04 | 独立部署（第 1 次 FAIL，第 2 次 PASS）、容量测试（未证明）、不超卖（PASS） | #16、#18、#19；gitops-manifests #132、#133、#134、#135 |
| 11-23 15:33 | catalog 上生产：prod 的 web 浏览走 catalog | gitops-manifests #136、#137、#138；infrastructure-live #32 |
| 11-23 16:48 – 18:06 | 故障实验：最终一致 PASS；故障隔离 FAIL、FAIL、第 3 次通过 | `scripts/m6/out/` 的报告（§2.2、§2.3） |
| 11-23 晚 | 独立扩容在 winpc 性能环境重测：耐久 Postgres 上 1.0×（FAIL），根因 = outbox 的 NOTIFY 提交锁 | #21 |

---

## 2. 目标和结果：四个承诺

设计书 §0 的原话是"每一条都要能被测出来，而不是靠嘴说"。下面每一行都来自 `check.py` 的报告（判定行全部 PASS 才算 PASS）。

| 承诺 | 成功标准（§0） | 结果 | 关键数字 |
|---|---|---|---|
| **独立部署** | 发布一个服务，其他零变更、零重启；旅程 SLO 不受影响 | ✅ PASS（第 2 次） | payment `ef9c9b8 → 43542ad`：其他工作负载 0 变更 / 0 替换 / 0 重启；453 单 60 s 内 100% 终态；浏览 100%，下单 99.81%（524 单里 1 单超时，在发布之前） |
| **故障隔离** | inventory 完全宕机时浏览 ≥ 99.5%，已提交订单不丢 | ✅ PASS（第 3 次，有保留，见 §2.2） | inventory 停 10.0 分钟：浏览 100%（12061）、下单受理 100%（1206）；恢复后 28 s 全部终态；0 丢单、0 重复扣款、0 超卖、DLQ 0 |
| **最终一致** | payment 停 10 分钟，恢复后 5 分钟内全部终态；不重复扣款、不超卖 | ✅ PASS | 1184 单积压在 CHARGING，恢复后 **99 s** 全部终态；2161 单 0 丢、0 重复扣款、0 已取消却扣款、0 超卖、outbox 全空、DLQ 0 |
| **独立扩容** | 只扩 order + inventory，下单拐点 ≥ 2× | ❌ **FAIL**（§2.4） | 耐久 Postgres：拐点 45/s（1 / 1 副本）→ 45/s（3 / 3）= **1.0×**；瓶颈是 outbox 的 NOTIFY 让提交在每个实例上串行，CPU 空闲（order 0.22 / 0.5 核）。诊断（`synchronous_commit=off`）：200/s → 400/s = 2.0×，不算通过。所有运行 0 服务端错误、0 DLQ |
| （附加）不超卖 | 秒杀时库存不为负 | ✅ PASS | 200 件库存，30 单/s × 40 s 涌入 1201 单 → 恰好 200 CONFIRMED、1001 CANCELLED，账目平衡 |

### 2.1 独立部署：第 1 次的 FAIL 留着

第 1 次的隔离本身是对的（0 变更 / 0 替换 / 0 重启），但旅程只有 73% 在 60 s 内终态：RabbitMQ 的内存水位 160 MiB **低于它自己空闲时的内存**，90 分钟里内存告警约 40 次挡住了所有发布者（§5.2）。修掉之后第 2 次 PASS。第 1 次的报告没有删（`m6-verification.md` 的结果表两行都在）。

### 2.2 故障隔离：前两次 FAIL 是测量链路的问题，不是系统的问题

| 次 | 用户侧（k6） | 服务端（catalog 的 `rpc_requests_total`） | 判定 |
|---|---|---|---|
| 1（16:48） | 浏览 96.29%（12326），下单 96.27%（1233） | 浏览 100%（11731） | FAIL |
| 2（17:26） | 浏览 96.40%（12094），下单 96.44%（1209） | 浏览 100%（11649） | FAIL |
| 3（17:47） | 浏览 100%（12061），下单 100%（1206） | 浏览 100%（12039） | 判定行全部 PASS |

失败只集中在 inventory **恢复**的 1–2 分钟，服务端同一时段 100%。唯一的结构差别：inventory 开着测量用的临时 NodePort（payment 实验没有，payment 实验也没有这个现象），同一时刻小新的 tailscaled 日志有路由删除 / 重建。推断：Pod 就绪变化 → kube-proxy 重写 NodePort 的 iptables → 干扰经 Tailscale 进来的连接。第 3 次去掉 inventory 的 NodePort，用户侧 100%（§5.7）。

**保留意见**：
- "失败了不要重跑到通过为止"是 M6 的规矩（`m6-verification.md`）。这里重跑了两次，理由是第 1、2 次服务端都是 100%、原因定位到测量链路之后才改了测量方式；但"推断"没有被单独复现（没有在不开 NodePort 的条件下故意制造 Tailscale 路由变化）。
- 第 3 次 `check.py` 的总判定是 **INCONCLUSIVE**，不是 PASS：其余 14 行 PASS，"没有订单因 RESERVING 期限（15 分钟）被取消"这一行是 N/A（指标没有样本）。故障只持续 10 分钟，按设计期限不可能触发；38 个取消订单的比例（1.8%）和 k6 故意放进去的超额订单（2% 含 `M6-PRICY`，被支付拒绝）一致。我们把它记为通过，但工具的这一行应该改成直接查库（§6）。

### 2.3 最终一致

payment 停 10.0 分钟期间订单在 CHARGING 堆到 1184；saga 按 30 s / 60 s 重发了 6350 次命令，迟到的回答（例：payment 对重发的扣款命令的第二次回答）被忽略 6109 次（设计如此：状态机的条件更新 `WHERE 状态 = 从`）。99 s 后全部终态。没有重复扣款靠的是 payment 的幂等新建（订单号唯一），不是 RabbitMQ 的投递保证。

### 2.4 独立扩容：FAIL（耐久的 Postgres 上 1.0×），根因已定位

结果和做法的原文在 [`m6-verification.md`](m6-verification.md) 的 "Independent scaling on winpc" 一节（#21）；环境在 [`perf/winpc/`](../perf/winpc)。

**环境**：被测系统 = winpc 的 WSL 里的性能环境（12 vCPU / 20 GB，Docker Compose）。order、inventory、其余三个服务分别用独立的 Postgres 16：pg-order、pg-inventory、pg-rest。每个副本的 CPU 上限是 order 0.5 核、inventory 0.5 核。HAProxy 轮询全部副本。压测机 = 小新上的 k6。**两台机器在不同的地点**（不同的子网和公网出口），压力走 Tailscale 的直连路径（约 77 ms RTT），路径上没有 Kubernetes NodePort。判定看服务端指标。每一步 30 s 预热 + 60 s 测量，**拐点 = 两次都守住的最高速率**。

| 运行 | Postgres | order / inventory 副本 | 拐点 | 倍数 |
|---|---|---|---|---|
| durable-base | 耐久（fsync、`synchronous_commit=on`） | 1 / 1 | **45/s** | |
| durable-x3 | 耐久 | 3 / 3 | **45/s** | **1.0× → FAIL** |
| async-base（诊断） | `synchronous_commit=off` | 1 / 1 | 200/s | |
| async-x3（诊断） | `synchronous_commit=off` | 3 / 3 | 400/s | 2.0×（**不算通过**：放弃了耐久性） |

所有运行的每一步：服务端错误 0、DLQ 0、k6 的下单受理 100%。

**根因**（实验找到的，不是推测）：
- 卡在拐点时没有任何东西接近 CPU 上限：order 用了 0.22 / 0.5 核，pg-order 0.26 / 2 核，整机 2 / 12 核。
- pg-order 上大多数活跃连接在 `commit` 时等 `Lock / object`。`pg_locks` 显示 `pg_database` 0 上的 `AccessExclusiveLock`。
- 原因是运行时 api-factory-runtime v0.5.1 的 `outbox.Writer.Write` 在**每个写 outbox 的事务里**都调用 `pg_notify` 去唤醒中继。发过 NOTIFY 的事务在提交时要拿这把全局锁，一直持有到它的 WAL 刷盘（fdatasync 约 1.8 ms）。
- 结果是写 outbox 的提交在**每个 Postgres 实例上一个接一个**执行。order 库里，PlaceOrder 和 saga 的每一步都写 outbox。
- 这个上限属于数据库，不属于副本：副本加到 3 个只是让更多事务排在同一把锁前面，50/s 时下单的 p95 从 0.49 s 变成了 2.2 s。

**诊断**：关掉 `synchronous_commit` 之后，提交不再拿着锁等刷盘：
- 单副本的拐点从 45/s 升到 200/s，限制变成 order 自己的 CPU（0.50 / 0.5 核）。
- 这时扩 order + inventory **正好 2.0×**（400/s），直到 pg-order / pg-inventory 用满各自的 2 核。

也就是说服务本身是能独立扩容的，挡住的是 outbox 的 NOTIFY。但诊断丢掉了崩溃时最后约 600 ms 的提交，对订单不可接受，所以**判定仍是 FAIL**。

**修法**：把 NOTIFY 移出写事务（运行时的 outbox）。可以在提交之后再通知，也可以只靠中继已有的每秒轮询，或者只在中继空闲时通知。然后用 `perf/winpc/perf.sh` 重跑同一个测试。

**第一次（2026-11-23 12:59，小新）的记录**：1 副本时 10/s 稳、20/s 一次通过一次失败（unstable）、25/s 破；order + inventory ×3 时 20/s 两次都稳、25/s 破。`check.py` 打印了 2.00×，**只是因为基线的 20/s 不稳**，不能算证明。两边都在 25/s 破在"saga 跟得上"（99% 订单 10 s 内终态）这一条，当时小新 8 个线程用了约 99%，单个 Postgres（5 个库，每单约 10 个事务）1.8 核。当时的结论是"整机 CPU 和共享的数据库"；现在知道，共享数据库那一半就是上面的 NOTIFY 提交锁：5 个库在同一个 Postgres 实例里，共用同一把锁。

设计上的已知限制（无论在哪测都成立）：outbox 中继每个库一个 leader，扩 order 增加的是 PlaceOrder 处理和 saga 的消费者，不是中继；inventory 对同一个 SKU 的扣减在行锁上串行，副本再多也一样。

---

## 3. 工厂生成了什么

### 3.1 API 个数：28 / 29（97%）

分母是 5 个后端服务的全部 API、消息处理器和定时任务（api-factory `docs/design/storefront-v2-readiness.md` v0.7）：查询 9、写 5、消息处理器 13、定时任务 2。生成的 28 个包括 saga（OrderSaga 的 6 个状态转移 + 超时扫描）。剩下的 1 个是 **J1"释放过期的预留"**（批量任务，工厂的 R6 没做）——**也没有手写**，所以现在 v2 里过期的预留不会自动释放（§6）。
分母中途从 28 变成 29：端到端骨架发现"商品登记时建库存记录"这一步在清单里漏了（G18 → M13），照实写。

### 3.2 代码行：手写 5.2%

`factory --project services/<svc> stats`（api-factory `boundaries.md` §8.2 的量法：进镜像的 `cmd/ internal/ gen/` 的 `*.go` 和 `db/queries/*.sql`，非空行；生成 = 文件头有 `Code generated … DO NOT EDIT.`）：

| 服务 | 生成（工厂 / protoc·sqlc） | 手写 | 手写占比 | 只算工厂的输出 |
|---|---|---|---|---|
| catalog | 1923（627 / 1296） | 115 | 5.6% | 15.5% |
| inventory | 2476（842 / 1634） | 114 | 4.4% | 11.9% |
| payment | 1235（421 / 814） | 114 | 8.5% | 21.3% |
| notification | 1074（397 / 677） | 114 | 9.6% | 22.3% |
| order | 3630（1527 / 2103） | 114 | 3.0% | 6.9% |
| **合计** | **10338（3814 / 6524）** | **571** | **5.2%** | **13.0%** |

两种口径都要看：protoc / sqlc 的输出也是确定性生成的，但不是工厂"证明过"的东西，所以保守口径（只算工厂自己的输出）是 13.0%。

**手写的是什么、为什么**：每个服务只有两个文件。
- `internal/migrations/migrations.go`（101 行 × 5，内容除了服务名完全相同）：goose 迁移嵌进二进制、`/server migrate up` 由 init container 执行。原因是工厂的打包模板还没有迁移（api-factory GAPS **G31**）。修掉 G31，手写从 571 行降到约 65 行（≈ 0.6%）。
- `cmd/server/main.go`（13 行 × 5）：`migrate` 子命令的分支 + `server.Run`。

镜像外面的手写（不在上表）：
- 设计书（工厂的输入）：33 个 YAML，1719 行非空。即每 1 行设计约生成 6 行代码（不含测试）。
- 迁移 SQL 11 个文件（00003 是 v1 商品的数据迁移）。
- web（BFF）283 行：按 `CATALOG_CATEGORIES` 逐个分类调 `ListProducts`（G35：没有"全部商品"的 API）。
- M6 的测量工具 `scripts/m6/`（Python / k6 / shell，约 1700 行）、CI 和 9 个 release 工作流。
- 多服务连起来的端到端测试（api-factory `examples/storefront-v2/e2e`，21 步）：按 `boundaries.md` §8.1 属于"手写 + 平台"层。

### 3.3 "带着证据"指什么

每个生成的 API 都附带工厂的验证，不是只有代码：

| 证据 | 内容 | 数字 |
|---|---|---|
| 验收用例 | 人写的例子 + 自动边界用例 + 差分测试 | catalog 327、inventory 557、notification 180、order 950、payment 212（api-factory #44 的闸门） |
| 状态差分测试 | 参考模型（内存里的状态机解释器 / 处理器模型）和真数据库对同一串请求，比较写完之后的状态 + 返回值 + 发出的消息 | 同上 |
| 故障注入 | 第 k 个写操作之后强制失败 → 数据库回到请求之前、没有发出消息；再投 → 只处理一次 | 每个命令、每个 k |
| 并发 | 同一条消息 8 次 → 只处理一次；回答和超时同时到 | R3-2 / R4 / R5 |
| 变异余量 | 故意改坏生成的代码（去掉条件、去掉去重、事务提前提交……），测试必须抓到 | 工厂仓库合计 **201 个**变异（api-factory #45），R5 状态机 18 个、R4 15 个、R3-2 12 个、R3-1 8 个，每个 ≥ 3 个差分用例 × 3 个种子 |
| 静态检查 | 跨服务的消息契约（每条被消费的消息有发送方、版本一致）、状态图（可达、能到终态、等待状态有超时） | `factory system --check`、R5 的 DRC |

**工厂不证明的**：多个服务连起来之后的行为（saga 走完一圈、宕机恢复、超卖）。这一层由手写的 e2e 和 M6 的故障实验证明——M6 也确实抓到了工厂证明不了的东西：RabbitMQ 的配置（§5.2）、中继在确认超时后的行为（§5.4）。

### 3.4 通用性：工厂不只是为 storefront 做的

工厂原来的需求来自一家公司的参照 API（只读、按店铺分库）。R2 之后把那 100 个参照 API 全部重新生成并通过验收（3 个种子各约 5740 个用例，变异 94 个），说明为 storefront 加的能力没有破坏已有的使用者（readiness §2.6）。

---

## 4. 做得好的

1. **先利其器的决定是对的。** 工厂补到 28 / 29 之后，5 个服务从 #10（09:59）到 #13（11:34）不到两小时进了仓库；之后的问题全部是平台和运行时的，没有一个是"生成的 API 写错了"。
2. **承诺写成可测的标准、工具先于实验。** `check.py` 的判定行在实验之前就定好；第 1 次独立部署的 FAIL 原样保留，没有改标准去凑结果。
3. **安全闸在工具里，不在人的记忆里。** `m6lib.guard()` 拒绝笔记本的 kubeconfig、要求节点有 lab 标签、namespace 像 v2；故障实验从未碰生产。
4. **生产晋级按成熟公司的流程走**（catalog prod 的变更记录在运维备忘录里，不在公开仓库）：闸门证据（staging 3 h、18978 次浏览 0 错误）→ 备份先于数据 → web 先晋级（不带开关时行为等于 v1）→ 再加 `CATALOG_URL` → 浸泡 → 观察 22 分钟。回滚 = revert 一个 PR，没有触发。
5. **服务端指标救了故障隔离的结论。** 用户侧 96% 时，服务端 100% 这一列让我们去查测量链路，而不是去"修"一个没有坏的系统。
6. **工厂的缺口有地方记。** GAPS G1–G36 每一条都写了发现时间、怎么绕过、要不要修；G18–G23 在 R4.1 修掉，G31–G36 在 S1 发现时当天记入（api-factory #49）。

---

## 5. 做得不好的：事故和教训

| # | 现象 | 根因 | 修复 | 教训 |
|---|---|---|---|---|
| 5.1 | 两个集群的根 Application 渲染失败约 3 分钟（ComparisonError，没删任何东西） | 同一个提交里把 slo-platform 升到 v0.1.2 **并且**用了新键 `applications.exclude`；根 Application 先用正在运行的旧版本渲染 platform.yaml，旧 schema 不认识新键 | 拆成两步：gitops-manifests #121 只升版本、#122 再用新键。v0.1.3（#139 → #140）、v0.1.4（#141 → #142）照这个做法，没有再出问题 | **升版本和用新键永远分两个提交** |
| 5.2 | 独立部署第 1 次：旅程只有 73% 在 60 s 内终态 | RabbitMQ 内存水位 160 MiB 低于它空闲时的内存，90 分钟内告警约 40 次挡住发布者 | gitops-manifests #132：水位 300 MiB | 资源阈值要对照实测的空闲值设，不能只按 limit 的比例 |
| 5.3 | #132 之后的重启：RabbitMQ crash-loop | Erlang cookie 文件权限不是 0600（潜伏缺陷：第一次启动时没暴露） | gitops-manifests #133：启动前 chmod 0600 | 第一次能起来不代表能重启；有状态组件上线时做一次重启演练 |
| 5.4 | M6 期间观察到中继的 leader 抖动 | 运行时 `outbox/relay.go`（v0.5.1）的 `round`：一批消息的发布和 confirm 共用一个 `ConfirmTimeout` 的 context（`bctx`）；**confirm 超时之后，同一个已经过期的 `bctx` 又被拿去 `UPDATE factory_outbox`（标记已发送 / `markFailed`）** → 写库失败 → 被当作数据库错误结束这一任期 → 放弃 leader 再竞选 | **未修，也还没记进 GAPS**（§6） | 超时之后的清理要用新的 context |
| 5.5 | 一个 PR 在 verify 还没跑完时被自动合并（api-factory #35） | 合并脚本只判断"没有失败的检查"，pending 也算通过 | 脚本改成"所有检查 pass 才合并、超时仍 pending = 不合并"，并在 main 补跑全量闸门 | 合并条件写成正面的"全部通过"，不要写成"没有失败" |
| 5.6 | AI 式样审查一个 PR 来回 3 轮不收敛 | 本地审查和 CI 审查对同一个问题措辞不同，答案按原文匹配；轮次历史不共享；回答照抄选项原文、与实现不符被下一轮判矛盾 | api-factory #48：轮次历史共享（提交的 ledger）、按问题 id 匹配答案、prompt 列出全部已答、`max_rounds` 上限、`keep:` 按实际行为回答 | 不确定的检查要有收敛机制（历史 + 上限），否则会变成无限循环的闸门 |
| 5.7 | 故障隔离前两次 FAIL（用户侧 96.3% / 96.4%） | 测量链路：会频繁变动后端的 NodePort + 经 Tailscale 访问（推断，§2.2） | 第 3 次去掉 inventory 的 NodePort。winpc 的容量测试也走 Tailscale（两台机器不在同一个地点，没有局域网可走），但用的是直连路径，路径上没有 NodePort，并且按服务端指标判定 | 可用性以服务端指标为主判定；测量路径上不要放会频繁变动后端的 NodePort |
| 5.8 | GitHub Actions 当月花费上限用完，所有任务 2 秒失败 | 一夜十几个 PR × 每次推送都重跑约 40 分钟的 verify（margin 160+ 个变异） | 用户决定 api-factory 公开（MIT，api-factory #46），托管 runner 免费；margin 分 4 片并行（#43） | CI 的费用也是容量；长闸门要分片、避免无谓重跑 |
| 5.9 | 独立扩容第一次没能证明 | 小新（4 核 8 线程、7 GB）上被测系统、Argo CD、Prometheus、k3s 共用 CPU，单个 Postgres 承担 5 个库 | 设备角色重新划分（用户确认）：小新 = 功能 / 故障实验 + 压测机，不放生产复制品（gitops-manifests #140、#143、#144）；winpc = AI + WSL 里的性能环境 | 容量结论只对测的那台机器成立；"副本 ×3"在共享 CPU 上测不出独立扩容。换到独立的性能环境之后才看清真正的瓶颈（§5.11） |
| 5.10 | 故障注入（Chaos Mesh）第一次被会话的权限策略拒绝 | 注入故障是有破坏性的操作，自动化代理不能自己批准 | 工具先做到 `--preflight` 通过，由用户明确批准后再执行 | 破坏性操作的审批是流程的一部分，计划里要留出等待批准的时间 |
| 5.11 | 独立扩容 FAIL（耐久 Postgres 上 1.0×） | 运行时 v0.5.1 的 `outbox.Writer.Write` 在每个写 outbox 的事务里调用 `pg_notify`；发过 NOTIFY 的事务提交时持有 `pg_database` 0 的全局锁直到 WAL 刷盘 → 写 outbox 的提交在每个 Postgres 实例上串行（§2.4） | **未修**：把 NOTIFY 移出写事务，然后重跑 `perf/winpc/perf.sh` | 工厂的验证（差分、故障注入、变异）都是功能性的，测不出性能问题。"每个事务唤醒一次中继"这种看起来无害的设计，只有在负载下、而且 CPU 空闲时去看锁等待才看得见 |

小插曲（没有造成影响）：catalog prod 晋级时 web 滚动更新期间内部探测失败 1 次（旧 Pod 先关了监听，endpoints 还没摘掉；web-probe 预算 100% → 98.7%，无告警）。待做：web 加 preStop / 优雅退出。

---

## 6. 没做完的

**生产仍然是 v1 的下单**
- 设计书的绞杀步骤只做了 S1（catalog，已上 prod）。**M3（order 接管订单）、M4（saga，worker 下线）、M5（notification、订单历史）都没有在生产做**。v2 的 5 个服务只在 lab 的 `v2-lab` 里整套跑过，M6 是在那里测的。
- S1 收尾（删 `services/api`、`release-api`）等 prod 稳定一晚之后做。
- catalog 的每日恢复校验没接上（作为接受的风险推迟；`check.sh` 写死了 api-factory 的库名）。

**v2 的追踪**
- v1（web / api / worker）已经发 trace 到 Tempo（#7；gitops-manifests #142）。v2 的运行时只把 W3C `traceparent` 从请求带到发出的消息里（`message/trace.go`），**不产生 span、不导出**；v2 的服务目前在 Tempo 里看不到。`boundaries.md` §8.1 把"埋点"放在运行时层，这是运行时的待做。

**功能缺口**
- J1（释放过期的预留）既没有生成也没有手写：预留过期时间写进了库（24 h），但没有任务去释放。
- `PAGE_TOKEN_KEY` 未设（G36）：多副本时翻页令牌跨实例失效；目前 web 不翻页。

**值得修的工厂缺口**（api-factory `examples/storefront-v2/GAPS.md`）

| 缺口 | 为什么值得 |
|---|---|
| **新：outbox 的 NOTIFY 在写事务里**（§5.11） | 独立扩容的唯一阻碍 |
| **G31** 打包模板没有迁移 | 手写 571 行里约 505 行是它的绕过代码 |
| **新：中继 confirm 超时后复用过期的 context**（§5.4） | leader 抖动；要先记进 GAPS，再在运行时修 |
| **G33** 工具缺失时 `check` 退出码 0 | 消费方的 CI 漏装 sqlc 也是绿的 |
| **G26** 宕机 / 恢复的跨服务不变量不在工厂的 e2e 里 | M6 的故障实验是手工搭的，应该能在 CI 里以缩短的时钟跑 |
| G32 工厂不能 `go install`；G34 认证不分读写；G24 / G36 | 第二个使用者会马上碰到 |

**测量工具**
- `check.py isolation` 的"期限取消"行改成直接查库，不依赖可能没有样本的指标（§2.2）。
- 故障实验报告进仓库：现在 `m6-verification.md` 的结果表还写着"故障实验未跑"，原始报告只在本地的 `scripts/m6/out/`。

---

## 7. 决策日志

"用户"= 用户本人决定或批准；"代为"= 实施时由代理决定、事后记录在设计书 / PR 里供审查。

| # | 决定 | 谁 | 理由 | 记录 |
|---|---|---|---|---|
| D1 | 消息中间件用 RabbitMQ（不是 NATS） | 用户 | — | 设计书 v0.2 |
| D2 | 先利其器：工厂到 95% 再开发 | 用户 | "这样也有意义了" | 设计书 v0.2；api-factory #19 |
| D3 | 工厂的 R3 / R4 / R5 设计的待决项全部按推荐（状态存订单行、无 PENDING、UUIDv7、RESERVING 15 分钟、预留 24 h…） | 用户 | — | #9；api-factory #23、#31 |
| D4 | S1 用"新服务 + 配置开关"而不是把 `api` 原地改名 | 代为 | 同一个 web 镜像在各环境按开关走 catalog 或 v1；晋级 web 不依赖 catalog 已经在那个环境；回滚 = 去掉开关 | 设计书 §14.1 |
| D5 | catalog 自己的 CNPG 实例（偏离 §12 的"共用集群"） | 代为 | Pod 只能读自己 namespace 的 Secret，小新上没有跨 namespace 复制密钥的手段，也不能写进公开仓库；实测约 50 MB | 设计书 §14.1 |
| D6 | 迁移嵌进服务二进制（goose + init container） | 代为 | 工厂的打包模板还没有迁移（G31） | 设计书 §14.1 |
| D7 | `AUTH_MODE=none` + NetworkPolicy | 代为 | 运行时的认证读写不分（G34）；写接口出现时再上令牌 | 设计书 §14.1 |
| D8 | 工厂钉在一个提交当工具用；AI 式样审查不进 storefront 的 CI | 代为 | 审查要模型凭证（公开仓库的 CI）且结果不确定；改设计时本地跑、回答提交 | 设计书 §14.1 |
| D9 | v2 整套先只部署到 lab（一个 CNPG、每服务一个库和账号、客户端证书认证；5 个服务共用一个 RabbitMQ 用户），在生产按绞杀步骤推进之前先测承诺 | 代为 | 先证明架构的承诺再改生产；lab 内存紧 | gitops-manifests #127 |
| D10 | catalog 上 prod：GO（按"成熟公司"规则执行） | 用户批准 staging 和 prod，执行细节代为 | 闸门证据见 §4 第 4 条 | gitops-manifests #136–#138 |
| D11 | catalog 的每日恢复校验推迟，作为接受的风险 | 代为 | 参数化 `check.sh` 估计 > 1 h，还会在小新上多起一个集群；prod 库只有 3 个商品，可由迁移重建 | 运维备忘录 |
| D12 | 事件也开 `mandatory`：路由不到的事件标成已发送但留下错误、指标和 ERROR 日志 | 代为（工厂） | `外部` 事件可能永远没有队列，扣着会堵 outbox；预防靠拓扑清单 + 静态检查 | api-factory #44；`boundaries.md` §8.1 |
| D13 | RabbitMQ 水位 160 → 300 MiB | 代为 | 实测空闲内存高于 160 MiB | gitops-manifests #132 |
| D14 | 故障隔离第 3 次去掉 inventory 的 NodePort 后重跑，结果记为通过 | 代为 | 前两次服务端 100%、失败只在恢复的 1–2 分钟、原因指向测量链路；工具总判定 INCONCLUSIVE 的那一行按设计不可能触发 | §2.2 |
| D15 | 故障注入（Chaos Mesh）执行 | 用户批准 | 破坏性操作 | §5.10 |
| D16 | Tempo 放手机（独立第三方，不进生产请求路径） | 用户 | 手机挂了生产不受影响 | slo-platform v0.1.4；gitops-manifests #141、#142 |
| D17 | api-factory 公开（MIT），外部 PR 一律要批准 | 用户 | Actions 额度用完；不申请专利 | api-factory #46 |
| D18 | 设备角色重新划分；独立扩容改到 winpc 性能环境重测 | 用户确认 | 小新的 CPU 和单 Postgres 是瓶颈（§2.4） | gitops-manifests #140、#143、#144 |
| D19 | 独立扩容记为 FAIL，不把 `synchronous_commit=off` 下的 2.0× 当作通过 | 代为 | 承诺是对耐久的系统说的；崩溃时丢掉最后约 600 ms 的提交，对订单不可接受（除非用户另行决定） | #21 |

---

## 8. 下一步

1. **先修 NOTIFY 提交锁，再测独立扩容**：在运行时的 outbox 里把 NOTIFY 移出写事务，然后用 `perf/winpc/perf.sh` 在耐久 Postgres 上重跑。`m6-verification.md` 的结果表补上故障实验的结果。
2. **生产的绞杀**：M3 order 接管下单（web 不再直接写库，worker 仍在）→ M4 saga + RabbitMQ 上笔记本、worker 下线 → M5。每一步照 catalog 的流程（备份 → staging 浸泡 → 开关 → prod → 观察）。上生产之前先解决 RabbitMQ 每服务一个用户（需要密钥手段）。
3. **运行时**：§5.11 的 NOTIFY（见第 1 条）；修 §5.4 的 context 复用；加 span 导出（OTLP，没有端点时关闭），让 v2 在 Tempo 里可见。
4. **工厂**：G31（删掉 5 份手写迁移）、G33、J1（R6 或手写，二选一要明确）。
5. 测量工具：`check.py` 的期限取消行、报告进仓库；把 G26 的跨服务不变量做成 CI 里可跑的缩短时钟版本。
6. web 的优雅退出（preStop），S1 收尾删 `services/api`。
