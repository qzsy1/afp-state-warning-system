# AFP 采集数据 MySQL 保存说明

## 保存时机

系统先将当前铺层完整写入本地 CSV、时间戳文件和采集摘要；停止并保存成功后，才执行一次 MySQL 事务写入。数据库异常不会中断采集。

同一试样使用 `specimen_id + condition_id + replicate + dataset_schema` 作为稳定标识。再次保存同一层时使用主键幂等更新，不会产生重复采样点。后续层会更新试样的层数和完整试样文件路径。

## 界面设置

在“真实采集与保存”中勾选“采集完成后同步保存到 MySQL”，填写主机、端口、用户名、密码和数据库名称，然后点击“检查 MySQL”。密码默认为空，清空后会按空密码连接。数据库名称只能包含字母、数字和下划线，且必须以字母开头。

## 数据表

- `afp_condition`：工况和工艺参数；
- `afp_specimen`：试样、工况、独立重复和工艺参数；
- `afp_layer`：每个铺层的文件路径、采样点数和采集摘要；
- `afp_sensor_sample`：每个采样点的传感器 JSON 和工艺参数 JSON；
- `afp_sample_all`：保留全部采样点的总表，便于统一分析；
- `afp_mysql_upload_log`：每次写入的结果和错误信息。

系统同时创建 `afp_view_condition`、`afp_view_specimen` 和 `afp_view_layer`，用于按工况、试样/独立重复和铺层快速查询。

## Navicat 查看方法

1. 新建 MySQL 连接：主机填写 `127.0.0.1`，端口填写 `3306`，用户名填写 `root`，当前默认密码为空。
2. 打开连接后刷新数据库，展开 `afp_state_warning`。
3. 在“表”中查看 `afp_condition`、`afp_specimen`、`afp_layer` 和 `afp_sample_all`。
4. 在“视图”中查看 `afp_view_condition`、`afp_view_specimen` 和 `afp_view_layer`。
5. 右键表或视图，选择“打开表”即可查看数据；也可以打开查询窗口执行：

```sql
SELECT * FROM afp_view_condition;
SELECT * FROM afp_view_specimen;
SELECT * FROM afp_view_layer
WHERE condition_id = 'H01' AND replicate_no = 1
ORDER BY layer_no;
SELECT * FROM afp_sample_all
WHERE specimen_key = '数据方案|试样名|H01|1|H01'
  AND layer_no = 1
ORDER BY sample_index;
```

系统会自动建立工况、试样、铺层、时间和同步日志索引；数据库初始化可重复执行，不会重复创建索引。

`afp_flat_all` 是面向查看和分析的平面视图，每一行同时显示工况、方案、试样、独立重复、铺层、采样点和传感器数据。它不复制数据，只是通过外键关系实时组合查询。

系统维护以下外键关系：

```text
afp_condition.condition_id
        ↓
afp_specimen.condition_id
        ↓ specimen_key
afp_layer.specimen_key
        ↓ specimen_key + layer_no
afp_sample_all / afp_sensor_sample
```

在 Navicat 中可以打开“设计表”或“对象关系图”查看这些外键。打开 `afp_flat_all` 则无需手动 JOIN 即可直接查看完整关系数据。

原始 CSV 仍然保留，MySQL 用于实时查询、状态分析和后续统计，不替代原始文件。

## 依赖

安装 `requirements.txt` 中的 `mysql-connector-python`。如果数据库暂时不可用，系统会在采集记录目录生成 `mysql_pending.json`，同时保留全部本地数据。
