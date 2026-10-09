# 本地备份与恢复

原件、封存文件包、PostgreSQL 与对象服务是不同层，不能只备份其中一层。

1. 原件继续保留在 `D:/Erdos_data`。
2. 保留整个 v3 封存包及可信 manifest SHA，它包含全部对象字节、源链、元数据和向量。校验通过后可将副本存到另一个磁盘；副本也按团队内部权限管理。
3. 数据库私有备份在 `.runtime/local/backups`，不放进客户端 SDK 或 exe。本次恢复验收的具体文件、摘要和独立测试库见 `reports/trae/cumcm_local_backup_restore.json`。
4. S3 的原件对象可以从封存包重新导入；SeaweedFS 卷中的内部数据库和数据块另由 Docker 卷备份保存。不要把运行中的若干块文件随意复制后声称一致备份。
5. 私有连接配置、API 凭据和工程外模型版本锁需安全备份，不能随公开 SDK 分发。

在 `D:/Erdos/data-platform/content` 可以重新执行安全的备份/独立恢复验收：

```powershell
$env:PYTHONPATH='scripts'
& 'E:/Anaconda/envs/pytorch/python.exe' -X utf8 -m cumcm_delivery.backup_local
```

该命令新建带时间戳的私有 dump 和独立恢复数据库；如果目标已存在就拒绝。它不覆盖工作库，不删除任何数据库，恢复验收库留供检查。验收对比活跃版本、全部结构指纹、主要表数量、任务表数量和旧 Record 的全部字段。

真正迁移到新机器时，先创建独立目标服务和配置，再恢复数据库、导入对象、检查向量模型空间和源摘要、验证实际客户端下载，最后明确切换消费者。不要直接恢复到正在使用的数据库。生产鉴权、TLS、备份保留周期和远端迁移由 BE/运维另行落地。

本次 PostgreSQL 全量恢复已经实际验收后才会生成通过报告；对象恢复能力来自完整封存包和已验收的导入器，不把这解释成已执行完整新 S3 桶的恢复试验。
