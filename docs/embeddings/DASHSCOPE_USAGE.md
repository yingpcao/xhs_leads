# DashScope Embedding Provider 使用说明（P2-C-DASHSCOPE-EMBEDDING-r1）

## 定位

`src/xhs_discovery/embeddings/dashscope.py` 是一个**显式 opt-in** 的真实 API
provider（阿里云百炼·北京，OpenAI 兼容 embeddings 接口），仅供受限 benchmark
使用。它不接入 `build_provider`，产品默认配置（`models.embedding_enabled=False`）
下不会构造、不会联网、不加载本模块以外依赖。普通生产路径请求
`qwen3.7-text-embedding` 仍得到 `RealProviderNotAvailableError`。

## 契约要点

- Endpoint 白名单：仅 `https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings`
  （官方北京兼容旧域名；新工作空间域名需真实 WorkspaceId，本项目不猜测、不使用）。
- 请求体：`model=qwen3.7-text-embedding`、`encoding_format=float`、显式
  `dimensions=1024`（官方支持 256/512/768/1024/1536/2048/2560，构造时校验）；
  单次最多 20 条（超限在任何网络动作前拒绝）。
- 密钥：只在 `embed` 调用函数内部 `os.environ.get("DASHSCOPE_API_KEY")` 读取；
  不打印、不回显、不序列化、不写文件、不命令行传递、不枚举环境、不读 .env/注册表/
  用户 profile。`security_check()` 只输出布尔存在性。缺失即 `missing_api_key`
  fail closed（BLOCKED），绝不寻找其他来源。
- Redirect：永不跟随（防 Authorization 外泄）；HTTP/JSON/超时/认证/限流均
  fail closed：不自动重试、不降级合成。错误对象只含安全分类与 HTTP 状态，
  不含服务原始 body/header/请求对象。
- 响应校验：模型名一致、数量一致、index 唯一且连续（乱序可确定性重排）、
  维度一致、全部有限数值、非零范数；任一失败即拒绝整批。

## 协议语义（task_type / instruction / normalize）

OpenAI 兼容 embeddings 请求体不支持任务定制：

- `task_type`：只登记 `p2-smoke-benchmark`；其他值**明确拒绝**（不静默忽略）。
- `instruction`：只允许空串；非空**明确拒绝**（不私自拼接进文本）。
- `normalize`：文档化的确定后处理——`True` 时对返回向量做 L2 归一化，
  `False` 时原样返回。

## 缓存身份

`cache_key()` 扩展字段（省略时旧键不漂移）：
`provider / endpoint / api_contract_revision / dimensions` 加上既有
`model_name / model_version / preprocess_version / task_type / instruction /
normalize / content_sha256 / provenance`。API 模型没有公布不可变权重版本，
`model_version="unknown-mutable"` 如实记录，不捏造版本或质量结论。
真实记录 `provenance=real`，与合成 stub 记录天然键隔离；生产读取
`require_real=True` 仍拒绝合成向量。

## Smoke 命令（scripts/p2_dashscope_smoke.py）

```
.venv/Scripts/python.exe scripts/p2_dashscope_smoke.py --check-only   # 零网络预检
.venv/Scripts/python.exe scripts/p2_dashscope_smoke.py                # 唯一一次真实调用
```

真实调用：最多 1 个 HTTP 请求、4 条预先写死的非敏感中文构造短句
（每条 <=80 字、总 <=320 字符）、timeout=30s、retries=0。不发送项目/帖子/
人工标签文本，不执行真实检索。成功输出：请求ID（若响应体提供）、返回模型名、
维度、usage、耗时、向量哈希、成本估计；失败输出：安全分类 + HTTP 状态，
随即停止（不换地域/模型/账户、不重试）。

成本：公布同步标价 0.0005 元/千输入 token（2026-10-29 查阅官方文档）。
输出中的 `estimated_cost_cny` 只是估计，不冒充账单。

缓存文件：`data/embeddings/dashscope_benchmark_cache.sqlite3`（benchmark 专用，
独立文件，不属于生产库迁移，不参与产品聚类/评分）。

## 安全边界声明

构造短句输入是 synthetic benchmark 语料，不是 human gold，不构成任何质量
评估结论；P2/G2 门槛仍需真人 200-300 条标注与后续授权方案，未因此包自验通过。
