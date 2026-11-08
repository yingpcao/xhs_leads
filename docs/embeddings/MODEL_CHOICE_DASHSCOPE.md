# 模型选择决定：远程 DashScope API 替代本地权重下载

日期：2026-10-29　任务：P2-C-DASHSCOPE-EMBEDDING-r1

## 背景与备选

P2 原计划（DEVELOPMENT_PLAN.md §P2）引入本地 Qwen3-Embedding-0.6B 或经本机
资源验证的替代模型；P2_NEXT_INPUTS.md 记录了本地下载方案（约 1.21GB 权重、
需 torch 兼容环境、GPU 未知、磁盘预算 10GB），状态 WAITING_USER_MODEL_CHOICE。

## 用户决定

用户明确选择考虑远程 API 模型 **qwen3.7-text-embedding**（阿里云百炼
DashScope，OpenAI 兼容 embeddings 接口），凭据走 **DASHSCOPE_API_KEY**
环境变量，使用**北京**控制台/地域的官方兼容 endpoint。即：以隔离的真实
API provider + 受限构造文本 smoke benchmark 替代本地权重下载与 Python/torch
安装。本包不下载本地权重、不安装新依赖、不启用生产模型。

## 授权范围（仅此）

- 最多 1 个 HTTP 请求；4 条预先写死的非敏感中文构造短句（每条 <=80 字、
  总 <=320 字符）；timeout<=30s、retries=0。
- 只允许 `https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings` 与
  模型 `qwen3.7-text-embedding`、显式 dimensions=1024。
- 不发送项目文本、帖子内容或人工标签文本；不执行真实检索；不上传业务数据；
  不改变平台额度配置；失败即停止，不重试、不换地域/模型/账户。

## 明确不做的结论

- smoke 输入为 synthetic 构造文本：只能验证接口契约、响应校验与缓存身份
  链路，**不能**得出任何模型质量结论，不代表 human gold。
- 官方无不可变权重版本号：缓存身份 model_version 记为 `unknown-mutable`，
  不捏造版本。
- P2/G2 仍 NOT_MET：真人 200-300 条标注仍缺；本决定不使 P2 自验通过，
  本地模型方案（P2_NEXT_INPUTS.md）保留待后续选择。

## 官方依据

https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai
（2026-10-29 查阅）：北京兼容旧域名仍有效；新工作空间域名需真实
WorkspaceId（不猜测、不使用）。公布同步价 0.0005 元/千输入 token，仅作
估计记录，不冒充账单。
