# supplement/ 说明

这是综评探究报告那轮补充探测的原始响应留档：`xuanti`（选题）与 `pingyu`（评语）各一次
`xhs-search search` 调用，每个子目录是该次调用的直接产物。

| 文件 | 内容 |
|---|---|
| `pingyu.json` / `xuanti.json` | CLI 返回的完整信封（schemaVersion、命令、耗时、退出码与结果） |
| `<组>/response.json` / `<组>/results.json` | 同一响应的不同落盘形态 |
| `<组>/notes.csv` | 卡片逐条展开：note id、标题、链接、点赞原文、发布日期 |
| `<组>/suggestions.csv` | 该轮未产生联想词，只保留表头 |

## 发布前脱敏

对外发布的副本做了三处替换，其余字段一律保留：

- URL 查询串里的 `xsec_token=<真实值>` → `xsec_token=REDACTED`
- 作者昵称 `author` → `REDACTED`
- 作者主页链接 `authorUrl`（含平台用户 ID）→ `REDACTED`

**保留**：note id、笔记标题、点赞原文、发布日期、封面地址。报告正文里的证据核对依赖这些字段，
脱敏不影响复核。

被替换掉的三项属于第三方账号的身份信息，与本报告结论无关，且不随采集行为获得发布授权；原始未脱敏
响应仍保留在维护者本机的运行数据目录 `data_live/` 中，不进版本库。
