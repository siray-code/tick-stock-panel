# 上游 PR 吸收评审

评审日期：2026-10-22。目标基线：`v0.3.12` / `463729ea5e4ec7621438b1aeacce7e3c88d605cc`。上游：[shy3130/tick-stock-panel](https://github.com/shy3130/tick-stock-panel)。

后续进展：#494、#499 的独立测试提交与 #495 已在二开分支吸收并适配，最终完整后端套件 2977 项通过。详见 [吸收记录与行为差异](upstream-integrations.md)。下文保留吸收前的评审快照。

本轮仅审阅并列出候选，没有把这些 PR 应用到本地、合入 fork 或代作者提交评审。证据包括完整 PR 差异、关键独立提交、当前基线调用链、测试内容与 GitHub Actions 记录；未在本地执行这些 PR 的补丁测试。远程正式 review 列表在核查时均为空，以下为本 fork 的技术判断。

## 结论与核查版本

| PR | 核查 head SHA | 建议 | 核查时 CI |
| --- | --- | --- | --- |
| [#494 分钟源失败隔离](https://github.com/shy3130/tick-stock-panel/pull/494) | `62affd5a4185e2c7b7a8b16f52e00e4d22611dcc` | 优先候选，与 #499 的独立测试提交配套 | [run 172](https://github.com/shy3130/tick-stock-panel/actions/runs/39298281310) 失败，原因已核对 |
| [#499 异动时区测试](https://github.com/shy3130/tick-stock-panel/pull/499) | `102688cd11cbfcff87c8083ca6e689aee1f84b59` | 优先提取独立测试提交 | [run 178](https://github.com/shy3130/tick-stock-panel/actions/runs/39441984183) 通过 |
| [#495 标的维表失败保护](https://github.com/shy3130/tick-stock-panel/pull/495) | `d179a3e780c2932fcba73a32130dfd47c93a7665` | 优先候选，补边界验证 | [run 173](https://github.com/shy3130/tick-stock-panel/actions/runs/39387553914) `action_required` |
| [#496 分钟分块落盘](https://github.com/shy3130/tick-stock-panel/pull/496) | `3d29ff992f33e9d28101844f3be9671e0327bd30` | 修改后考虑 | [run 174](https://github.com/shy3130/tick-stock-panel/actions/runs/39387636062) `action_required` |
| [#498 管道分钟门控](https://github.com/shy3130/tick-stock-panel/pull/498) | `9d0769dda81b89af6b0b9892033010a37291ea12` | 修改后考虑 | [run 176](https://github.com/shy3130/tick-stock-panel/actions/runs/39387904800) `action_required` |
| [#497 桌面启动诊断](https://github.com/shy3130/tick-stock-panel/pull/497) | `55d9e116aa0b6d7beff6fa2d515d5d06a489eafa` | 有桌面需求时修改后考虑 | [run 175](https://github.com/shy3130/tick-stock-panel/actions/runs/39387764529) `action_required` |
| [#493 日 K 暂存性能](https://github.com/shy3130/tick-stock-panel/pull/493) | `4d53f66cd11c5ea56dce7dcaa40b07db33519d36` | 不建议原样吸收 | [run 169](https://github.com/shy3130/tick-stock-panel/actions/runs/39217820900) 通过，但与当前基线有冲突 |
| [#424 MA120/MA200](https://github.com/shy3130/tick-stock-panel/pull/424) | `97a7aa1ff569e5c22cdff328b7b333af6b90be9a` | 功能储备，提取独立提交后重新评审 | 本轮未核验 CI；完整 PR 有冲突 |
| [#468 外部网页研究示例](https://github.com/shy3130/tick-stock-panel/pull/468) | `ef40a5b08d8e80d637059a8687bb76e2fb33cfcf` | 低优先级储备，仅提取示例相关提交 | 本轮未核验 CI；完整 PR 有冲突 |

`action_required` 不能记为测试通过，也不能据此判定代码测试失败。作者描述中的测试数量属于作者证据，吸收前仍需本地复验。此表是固定 SHA 的评审快照，PR 后续更新必须重新核查。

## 优先候选

### #494 与 #499：分钟路由修复和独立测试修正

#494 修复用户选择自定义分钟源后，插件失效、缺数据集、请求失败或时间戳不合法时静默改用 TickFlow 的问题。变更覆盖 `kline_sync`、分钟接口门控、全市场分钟刷新及指数分钟缓存键，并增加失效源、空结果、时区和缓存隔离测试。失败后不跨源的语义适合本 fork 的数据一致性要求。

已核对 #494 的 [后端失败日志](https://github.com/shy3130/tick-stock-panel/actions/runs/39298281310/job/118216412497)：2897 项通过，只有 `test_abnormal_moves.py` 中两个日期相关用例失败；前端构建通过。#499 的[独立提交](https://github.com/mynameisbreak/tick-stock-panel/commit/102688cd11cbfcff87c8083ca6e689aee1f84b59)仅将这两个测试快照的 `date.today()` 改成应用使用的 `cn_today()`，组合后的 CI 通过。

注意：#499 的完整 PR 是叠加分支，差异含 #494 的 9 个文件及异动测试，不能在吸收 #494 后再重复合入整个 #499。建议先接入分钟失败隔离，再单独提取时区测试提交，并以最终组合运行分钟路由、能力、监控、时区、全量分钟、指数缓存与异动测试。

吸收前还应同步核对数据源文档中的旧回退描述；此 PR 改变了失败降级行为，相关接口数据字段和 Parquet 格式不变。

### #495：避免残缺维表覆盖旧数据

`_fetch_all_exchanges_tickflow` 为单交易所增加两次退避重试；有旧维表时，任一交易所失败就保留旧文件。测试验证了旧表内容保持不变、正常覆盖、首次建表降级写入部分数据，并非仅断言接口成功。问题和修复范围都比较明确。

吸收条件：保留当前基线新增的 `_preserve_share_fields` 调用；补充重试后恢复、空响应和全部失败路径测试。首次建表仍会允许部分数据，这不是完整性保证，需在任务结果中明确反映，不能显示成全市场同步成功。核查 CI 通过后再决定接入。

## 修改后再考虑

### #496：分块落盘方向有价值，失败语义需重新对齐

持久化路径按每块 50 个标的取数并调用 `on_segment`，可减少中断时尚未落盘的数据量；未传回调的实时补拉路径保持一次调用返回。已有测试覆盖分块、顺序、空标的和实时路径。

问题在于原 PR 明确测试“自定义源失败后整体转 TickFlow”，与 #494 的失败后不跨源语义不同。接入 #494 后，空帧还可能表示失败，不能仅用 `fallback` 判断一块取数成功，也不能据此声称完整同步。需覆盖中间块失败、已提交分块保留、不跨源、重复写入不重复及进度/失败状态；测试应断言磁盘数据，而非只收集回调。

### #498：门控修复需检查 resolver 错误

修复盘后管道只检查 TickFlow 能力、忽略自定义 `minute` 数据集的问题，现有两个测试能覆盖核心正常分支。

但补丁使用 `not minute_fallback` 放行，并忽略 `_minute_resolve_err`。在 #494 契约下，自定义源解析失败返回 `fallback=False` 和错误，此处会错误放行；下层可能返回 0 行后管道仍展示完成。还需防止失效偏好经 getter 自愈为 TickFlow 后错误换源。吸收时统一使用实际选择源与 resolver 成功状态，补缺数据集、失效源、解析异常、用户关闭等用例。

### #497：桌面诊断需补异常收尾

`done_event` 检测后端线程提前退出、失败原因 HTML 转义及挖掘锁预检均有实际价值，测试涉及锁释放和等待时长。

补丁将预检及静态窗口放在 `main()` 的顶层 `try/finally` 之前；创建/打开锁文件的 `OSError`，或静态窗口初始化异常，可能绕过既有崩溃提示及显式单实例锁释放。应把该分支纳入顶层异常与资源收尾保护，增加失败注入测试，并在目标桌面系统验证窗口显示。源码 Web/Docker 使用者可延后。

## 暂不直接吸收

### #493：绕过仓库分区约定，有数据一致性风险

`_persist_daily_chunks` 对新日期直接生成 `part-stream-*.parquet`，而当前仓库的 `_write_daily_partition` / `_optimistic_upsert_partition` 后续只读取、合并并替换 `part.parquet`。

明确触发路径：首次同步生成分片 A；随后实时或普通日 K 写入更新同一标的，生成 `part.parquet`，但旧分片 A 仍在。按 `*.parquet` 读取会同时得到旧记录与新记录，原先单文件 merge-upsert 的唯一性不再成立。首次移动分片也绕开仓库写锁；多次文件移动不是整个日期分区的原子提交，提交中断可能暴露半个分区。现有测试主要验证同一暂存入口重复调用，未覆盖与其他写入入口交错。

因此即使旧 CI 通过、暂存阶段有基准，也不建议原样接入。应先把多分片提交、去重、并发、故障恢复和所有读写入口的约定收敛到仓库层，再讨论性能收益。当前工作树的无文本冲突也不是接入标准。

### #424 / #468：只提取目的明确的功能提交

核查时完整 #424 差异涉及 821 个文件、765 个提交；#468 涉及 982 个文件、869 个提交，均与当前上游显示冲突。这是当前分支历史/比较范围问题，不能把标题当作实际吸收范围，也不能据此评价作者的全部代码质量。

- #424 的[独立功能提交](https://github.com/Yi-111-a/tick-stock-panel/commit/97a7aa1ff569e5c22cdff328b7b333af6b90be9a)有滚动均线、历史不足返回空、盘中与盘后一致性、接口映射和展示测试，适合长期均线需求。提取后仍需核对 200 个交易日暖机、已有历史缓存覆盖、ETF/指数回退及内存开销，不能整合完整 PR。
- #468 的研究示例隔离了面板凭据与匿名外部检索，带 stdio 网关 fixture 和错误路径测试，作为外部研究脚本有价值。但它不接入内置助手，也未解决内置报告的新闻注入。需提取完整示例相关提交，不能只拿最后一个代理设置修正提交，也不应整合整个分支；无此需求时延后。

## 吸收次序与边界

未来获准吸收时，建议顺序为：#494 + #499 独立时区测试 → #495 → 配套适配 #498/#496 → 按桌面需求处理 #497。#493 暂缓，#424/#468 按明确功能需求单独立项。

每次吸收重新固定 head SHA、核对完整最终差异与本 fork 已有修改，并执行受影响测试。保留来源提交与适配说明，不把作者的本机测试、旧 CI、本轮静态审阅和本地最终验证混为同一结果。
