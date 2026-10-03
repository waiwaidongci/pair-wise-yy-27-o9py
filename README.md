# 数字人文文本校勘

这是一个 Python 标准库实现的校勘工作台，使用 SQLite 保存作品、版本、残片、转录、段落、异文、注释、修订层和快照，并通过 `http.server` 暴露 JSON API。

## 启动与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8114`，地址 <http://127.0.0.1:8114>。首次启动创建一个带缺页残片和不可辨标记的示例。数据库可通过 `COLLATION_DB` 指定，端口可通过 `PORT` 指定。

## 业务规则

- 版本类型限定为 `version`、`fragment`、`transcription`。
- 段落和版本必须属于同一作品，同一版本不能重复对齐同一段落。
- 只有负责人或被单独授权的编辑可以修改对应版本；其他用户只有查看权限。
- `[缺页]`、`[不可辨]`、`[残损]` 等标记会参与校勘稿导出和缺口统计，不匹配的方括号会拒绝保存。
- 修订按版本（witness）各自推进：每个版本维护自己的层号 `layer`，两个版本可同时保存、互不拦截；同一版本再次提交异文必须携带该版本当前层号 `expected_layer`，过期只拦截这一个版本，不影响其他版本。
- 每次新增或修改异文都会产生递增修订号（段落内全局流水）和 JSON 快照；层号用于乐观并发，修订号用于快照定位。
- 负责人可对段落选定两个（或多个）版本的最新校记生成合并稿；合并稿一旦确认，只要任一被选版本又提交了新层、或出现未参与合并的新版本校记，原合并稿立即失效（`stale`），须重新确认；失效后旧合并稿文本和旧校记不再随导出输出。
- 校勘稿导出分三组返回：`merged`（已确认合并稿）、`conflicts`（仍冲突、含已失效合并稿及各版最新校记）、`undecided`（未裁决段落及各版候选）。
- 锁定段落由负责人执行，锁定后任何新修订都会被拒绝；段落原有查看/编辑权限判定照旧不变。

## 主要接口

- `POST /api/users`、`POST /api/works`
- `POST /api/works/{id}/witnesses`、`POST /api/witnesses/{id}/editors`
- `POST /api/works/{id}/passages`、`POST /api/works/{id}/access`
- `POST /api/alignments`
- `POST /api/variants`、`POST /api/variants/{id}/revisions`（均带 `expected_layer`，兼容旧字段名 `expected_revision`）
- `GET /api/passages/{id}/snapshots/{revision}?user_id=...`
- `POST /api/passages/{id}/lock`
- `POST /api/passages/{id}/merge`（负责人选定 `witness_ids` 与 `merged_text` 生成或重新确认合并稿）
- `GET /api/works/{id}/collation?user_id=...`

导出接口把版本对齐、异文、注释、残损缺口和锁定状态按「合并稿 / 仍冲突的版本 / 未裁决段落」分组输出，可复核；已失效合并稿只提示失效原因和各版最新候选，不导出旧文本。
