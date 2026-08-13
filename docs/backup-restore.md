# 备份 / 恢复 SOP（Backup / Restore）

本文档是 PostgreSQL 数据备份与恢复的标准操作流程。**备份成功 ≠ 可以恢复**——
每次重大变更前后必须做一次 restore drill（见下文）。

## 备份（Backup）

### 推荐命令

生产环境（PostgreSQL 16，容器化）推荐使用 **custom format** 的 `pg_dump`
（压缩、支持选择性恢复、可与 `pg_restore` 配合）：

```bash
# 1. 进入 PostgreSQL 容器（或使用宿主机 pg_dump 直连）
docker compose -f compose.yaml exec db \
  pg_dump -U lark_ledger -d lark_ledger -Fc -Z 6 \
  -f /tmp/larkledger_$(date +%Y%m%d_%H%M%S).dump

# 2. 把 dump 拷出容器到 NAS 备份目录
docker compose -f compose.yaml exec db \
  sh -c 'ls -t /tmp/larkledger_*.dump | head -1' | xargs \
  docker compose -f compose.yaml cp db:/tmp/larkledger_*.dump /volume1/backup/larkledger/
```

> 不用 plain（SQL text）格式作为唯一备份：text 格式恢复慢、体积大、易在
> 恢复时因环境差异失败。custom format 是首选；plain 仅用于导出小表诊断。
> 不要写死 NAS 密码；用 ssh key / 环境变量注入连接信息。

### 文件命名与时间戳

```text
larkledger_YYYYMMDD_HHMMSS.dump        # 普通备份
larkledger_YYYYMMDD_HHMMSS.dump.sha256 # 校验和
```

命名含时间戳以便 retention 与还原定位。**每个 dump 必须生成 checksum**：

```bash
sha256sum larkledger_*.dump > larkledger_backup_checksums.txt
```

### 保存位置与 Retention

- 至少两份副本、两个不同存储（例如：NAS 本地盘 + 异地/冷备），其中一份**离线**。
- 建议 retention：每日备份保留 14 天、每周备份保留 8 周、每月备份保留 12 个月；
  配合脚本 + crontab 自动执行并轮转：

```bash
# 例：保留最近 14 个每日备份
ls -1t /volume1/backup/larkledger/larkledger_*.dump | tail -n +15 | xargs rm -f
```

- 备份内容还建议包含 `alembic_version` 快照（restore 后校验用）：

```bash
docker compose -f compose.yaml exec db \
  psql -U lark_ledger -d lark_ledger -c "SELECT version_num FROM alembic_version;"
```

### 备份前 Checklist

- [ ] 确认 `pg_dump` 版本与服务器版本兼容（major version 一致或更高）
- [ ] 确认备份目录空间充足
- [ ] 备份后立即 `sha256sum` 校验并记录

## 恢复（Restore）

> 恢复流程会**覆盖目标库**。生产恢复前必须确认：停止写入、已备份现状、创建恢复目标。

1. **停止写入**：停止应用写入路径（见下）。写操作停止后，事件会积压在
   飞书/outbox 侧，恢复完成后继续处理，不会丢账。

   ```bash
   docker compose -f compose.yaml stop app        # 停止应用（保留 db）
   ```

2. **备份现状**（恢复前的最后安全网）：

   ```bash
   docker compose -f compose.yaml exec db \
     pg_dump -U lark_ledger -d lark_ledger -Fc -Z 6 -f /tmp/pre_restore_$(date +%Y%m%d_%H%M%S).dump
   ```

3. **创建恢复目标**：恢复到一个干净的库（生产直接恢复前先确认库内无未同步写入；
   更安全的做法是恢复到临时库验证，再切换）：

   ```bash
   docker compose -f compose.yaml exec db \
     psql -U lark_ledger -d postgres -c "DROP DATABASE IF EXISTS lark_ledger;"
   docker compose -f compose.yaml exec db \
     psql -U lark_ledger -d postgres -c "CREATE DATABASE lark_ledger OWNER lark_ledger;"
   ```

   > 若没有独立 DBA 通道，宁可在**临时容器**里做 restore drill（见下文），
   > 不要在生产库上反复 drop/create。

4. **Restore**：

   ```bash
   # 把 dump 拷回容器
   docker compose -f compose.yaml cp /volume1/backup/larkledger/larkledger_XXXX.dump db:/tmp/restore.dump
   # custom format 用 pg_restore
   docker compose -f compose.yaml exec db \
     pg_restore -U lark_ledger -d lark_ledger --no-owner --no-privileges /tmp/restore.dump
   ```

5. **Alembic 校验**：确认 schema 与代码期望一致（备份时记录的 revision 应与
   恢复后一致；若备份早于当前 head，需要评估是否继续 `alembic upgrade`）：

   ```bash
   docker compose -f compose.yaml exec db \
     psql -U lark_ledger -d lark_ledger -c "SELECT version_num FROM alembic_version;"
   # 应用侧：
   curl -sf http://127.0.0.1:8000/readyz | grep -o '"migration":{[^}]*}'
   ```

6. **应用启动**：

   ```bash
   docker compose -f compose.yaml start app
   ```

7. **健康检查**：

   ```bash
   curl -f http://127.0.0.1:8000/healthz    # 200
   curl -f http://127.0.0.1:8000/readyz     # 200 且 migration current
   ```

8. **关键业务 smoke test**：向飞书发一笔测试账（或 `GET /api/v1/...` 查最近流水），
   确认记账、回复、查询正常；若期间有积压事件，观察 `/ops/status` 的
   `backlog.events.pending` 回落。

## Restore Drill（恢复演练）

**备份成功 ≠ 可以恢复。** 每季度至少执行一次最小 restore drill：

```text
临时 PostgreSQL database / 容器
      ↓
pg_restore 指定 dump
      ↓
alembic current == 备份时的 revision
      ↓
应用启动 → healthz → readyz → smoke test
```

本地最小演练（使用 compose.dev.yaml 的临时库）：

```bash
# 1. 起一个临时 PostgreSQL
docker run -d --name lldr-drill -e POSTGRES_USER=lark_ledger \
  -e POSTGRES_PASSWORD=drill-only -e POSTGRES_DB=lark_ledger \
  -p 55432:5432 postgres:16-alpine

# 2. restore
docker exec -i lldr-drill pg_restore -U lark_ledger -d lark_ledger \
  --no-owner --no-privileges < larkledger_XXXX.dump

# 3. alembic current（用测试库 URL 指向 55432）
LARK_LEDGER_DATABASE_URL='postgresql+asyncpg://lark_ledger:drill-only@127.0.0.1:55432/lark_ledger' \
  alembic current

# 4. 启动应用 → healthz → readyz → smoke test

# 5. 清理
docker rm -f lldr-drill
```

演练通过的标准：restore 无报错、`alembic current` 与备份一致（或已评估升级）、
`/readyz` 200、一笔 smoke 账可写可查。

## 与 Rollback 的关系

- 数据库恢复是 **schema-changing rollback** 的一部分（见
  [release-sop.md](release-sop.md#rollback回滚)）。
- 恢复/回滚前务必先做「备份现状」步骤；**镜像回滚 ≠ 数据库回滚**，两者是不同操作。
