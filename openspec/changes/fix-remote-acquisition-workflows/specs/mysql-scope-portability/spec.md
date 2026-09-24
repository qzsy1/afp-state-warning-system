## Purpose

定义访问电脑本机 MySQL 与服务器或目标 MySQL 的清晰作用域、凭据保护、换机初始化、故障诊断和降级保存行为，避免将服务器凭据错误套用于另一台电脑。

## ADDED Requirements

### Requirement: 模拟采集保存与 MySQL 预检必须一致
授权远程模拟采集启用 MySQL 时，停止后的保存结果 MUST 对应此前通过预检的目标作用域；系统 MUST NOT 静默关闭已启用的 MySQL。访问电脑本机 MySQL 仅可由该电脑的 helper 写入；若当前模拟工作流不支持该写入，启动前 MUST 明确拒绝，不能预检通过后报告已保存。访客模拟不得获得 MySQL 写入权限。

#### Scenario: 已授权模拟写入服务器目标库
- **WHEN** 用户启用服务器目标 MySQL 并通过预检后开始远程模拟采集
- **THEN** 停止结果 MUST 显示该库的实际写入或待补传状态，CSV 保存 MUST 不受数据库故障影响

#### Scenario: 目标库密码框留空但服务器已有匹配配置
- **WHEN** 已授权用户选择服务器已配置的目标库，网页密码框为空，并在真实或模拟模式执行连接预检
- **THEN** 服务器 MUST 以会话绑定的非秘密配置标识解析该目标库的已有凭据完成只读预检，MUST NOT 把空字符串当作密码提交，也 MUST NOT 将已有密码回传浏览器或发送给 helper；实际保存 MUST 使用同一目标配置

#### Scenario: 自定义目标与已存配置不匹配
- **WHEN** 用户改动目标主机、端口、数据库或用户名，且未显式提供该自定义目标的有效凭据
- **THEN** 系统 MUST 在预检和启动前提示重新提供凭据，MUST NOT 静默借用另一目标库的已存密码；显式提供的错误密码 SHALL 保留真实认证错误而不得回退到已存密码

#### Scenario: 访问电脑本机配置被定时刷新
- **WHEN** 用户正在修改本机 MySQL 主机、用户名或密码，而 helper 状态定时刷新
- **THEN** 未提交输入 MUST 不被覆盖或清空；预检 MUST 检查当前表单对应的配置，不得静默检查旧配置

#### Scenario: 已授权模拟选择本机 MySQL
- **WHEN** 该模拟工作流不能在访问电脑 helper 中完成写入
- **THEN** 系统 MUST 在开始采集前给出明确作用域限制，并保持服务器目标库和 CSV 两种保存路径可用

### Requirement: MySQL 连接必须标明作用域
系统 SHALL 将 MySQL 目标明确区分为“访问电脑本机 MySQL”和“服务器或指定目标 MySQL”，界面、配置状态和诊断结果 MUST 始终显示当前作用域与实际主机，且 `127.0.0.1` MUST 按执行连接的机器解释。

#### Scenario: 远程用户选择本机 MySQL
- **WHEN** 远程用户选择“访问电脑本机 MySQL”
- **THEN** 数据库预检和写入 MUST 由该电脑上的 local helper 执行，`127.0.0.1` MUST 指向访问电脑而不是服务器

#### Scenario: 用户选择服务器或目标 MySQL
- **WHEN** 用户选择“服务器或指定目标 MySQL”
- **THEN** 预检、真实与模拟采集的最终写入 MUST 均由服务器侧执行，并 SHALL 显示服务器实际使用的主机、端口和数据库名；helper MAY 采集并传输样本，但 MUST NOT 持有服务器目标库密码或代替服务器写入该目标库

#### Scenario: 访问电脑 helper 执行真实采集且同时启用本机库和目标库
- **WHEN** 真实样本由访问电脑 helper 采集，用户同时选择访问电脑本机 MySQL 和服务器目标 MySQL
- **THEN** 本机库预检与写入 MUST 仅由该 helper 执行，目标库预检与写入 MUST 仅由服务器执行，两个结果 MUST 分别报告，任一数据库失败 MUST NOT 伪装成另一数据库成功

### Requirement: 服务器目标库保存必须覆盖完整采集并可恢复
服务器 SHALL 将访问电脑 helper 发来的真实采集批次按会话和 `capture_uuid` 有界、持久地记录，以完整铺层而非仅用于绘图的有限实时镜像写入目标 MySQL。服务器 MUST 在对应批次持久接受后确认，停止时 MUST 等待最终状态和全部已确认样本，且断线重传、重复停止或写入重试 MUST 不产生重复或缺失的数据库采样行。持久记录和状态接口 MUST 不包含数据库密码。

#### Scenario: 采样点数超过实时镜像容量
- **WHEN** 真实采集的完整铺层超过实时镜像保留上限后停止并保存
- **THEN** 目标库的采样行数和连续样本序号 MUST 对应完整采集，不得仅保存镜像中最后保留的样本

#### Scenario: 断线重传与重复结束
- **WHEN** helper 重传已接受的批次，或停止与目标库写入因网络中断而重试
- **THEN** 服务器 MUST 按会话、`capture_uuid` 和批次序号去重，并仅对该完整铺层形成一个有效的目标库采样序列；未收到最终批次确认时 MUST NOT 报告目标库保存成功

#### Scenario: 服务器目标库写入失败
- **WHEN** 服务器已完整接收样本而目标库暂时不可写
- **THEN** helper 的本机 CSV 与已启用的本机库保存 MUST 不受影响；服务器 SHALL 保留不含凭据的有界待补传记录并报告实际失败与可重试状态，MUST NOT 把预检成功当作最终保存成功

### Requirement: 本机凭据按 Windows 用户加密保存
local helper SHALL 使用 Windows 当前用户作用域的 DPAPI 加密保存本机 MySQL 密码；浏览器、服务器持久化配置、状态接口和日志 MUST NOT 包含密码明文，只能暴露主机、端口、数据库、用户名、是否已配置和最近预检状态等安全元数据。

#### Scenario: 保存访问电脑凭据
- **WHEN** 用户在 HTTPS 会话中提交本机 MySQL 凭据并选择保存
- **THEN** 凭据 MUST 仅由对应 local helper 加密持久化，服务器 MUST 不将密码写入磁盘或日志

#### Scenario: 重新打开浏览器
- **WHEN** 同一 Windows 用户重新启动 helper 并再次访问应用
- **THEN** 系统 SHALL 从 helper 获得不含密码的已配置状态，并 MUST 能使用 helper 内部解密的凭据进行预检而无需向浏览器回传密码

### Requirement: 新电脑必须执行显式初始化
系统 MUST 将 DPAPI 凭据视为机器和 Windows 用户绑定数据，不得假定其可以复制到另一台电脑；新电脑无本机配置时 SHALL 引导用户填写并预检，而不是静默套用服务器保存的用户名和密码。

#### Scenario: 首次在另一台电脑连接
- **WHEN** 新电脑的 helper 尚未保存本机 MySQL 配置
- **THEN** 界面 MUST 显示“本机 MySQL 未配置”，并 SHALL 提供主机、端口、数据库、用户名、密码和连接预检入口

#### Scenario: 复制旧电脑配置文件
- **WHEN** helper 无法在当前 Windows 用户下解密复制来的凭据
- **THEN** 系统 MUST 将其视为未配置并提示重新输入，MUST NOT 回退到空密码或服务器凭据

#### Scenario: 新电脑本机数据库存在但 AFP 表为空
- **WHEN** 用户已在访问电脑创建目标数据库和最小权限账号，并显式点击本机 MySQL 的“保存并检查”，但该数据库尚无 AFP 关系表
- **THEN** local helper SHALL 仅在已选择的现有数据库内创建缺失 AFP 表并执行可回滚写测试，MUST NOT 创建其它数据库，也 MUST NOT 把该行为应用到服务器目标 MySQL；成功结果 SHALL 明确标记首次表初始化已完成

#### Scenario: 本机账号没有建表权限
- **WHEN** 本机数据库可连接但账号执行首次 AFP 表初始化返回 1142 或等价权限错误
- **THEN** 页面 MUST 显示建表授权不足及实际错误码，MUST NOT 继续报告“仅表结构不完整”而隐藏初始化失败；初始化账号 SHALL 对所选数据库具有 `SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES, CREATE VIEW, SHOW VIEW, DROP`，MUST NOT 因此获得全局权限

#### Scenario: 首次初始化中断后重试视图创建
- **WHEN** AFP 表或视图只完成部分初始化，用户再次显式执行本机“保存并检查”
- **THEN** helper SHALL 能重试外键与 `CREATE OR REPLACE VIEW`，账号缺少 `REFERENCES` 或数据库级 `DROP` 时 MUST 显示 1142 授权错误而不得报告初始化成功

### Requirement: 认证错误必须给出可操作分类
系统 SHALL 区分网络不可达、服务未启动、数据库不存在、驱动缺失和认证失败；收到 MySQL 1045 时 MUST 同时提示检查密码以及 MySQL `用户@来源主机` 授权范围，并显示不含密码的连接目标和来源作用域。

#### Scenario: MySQL 返回 1045
- **WHEN** 连接预检或写入收到 MySQL 1045 错误
- **THEN** 诊断 MUST 标记为认证或授权失败，并 SHALL 给出核对用户名密码、授权来源主机和最小权限授权的建议

#### Scenario: 本机服务未启动
- **WHEN** helper 连接本机端口失败且未建立 MySQL 握手
- **THEN** 诊断 MUST 优先提示检查本机 MySQL 服务和端口，而不得误报为用户名密码错误

#### Scenario: 服务器重启后目标 MySQL 进程未启动
- **WHEN** 服务器已有匹配目标配置但其 MySQL 监听进程在重启后尚未启动
- **THEN** 目标库预检 MUST 标记网络/服务不可达而非密码错误；服务恢复后 SHALL 使用同一服务器侧配置重新预检，MUST NOT 要求向访问电脑重新分发目标密码

### Requirement: 数据库失败不得中断主采集链路
MySQL 配置错误或暂时不可用时，系统 MUST 保持实时采集、预测和 CSV 保存可运行，并 SHALL 将待写入记录放入现有有界补传机制或明确记录无法补传的状态。

#### Scenario: 采集中数据库失联
- **WHEN** 实时采集期间 MySQL 连接失败
- **THEN** 系统 MUST 继续采集与 CSV 保存，展示数据库降级状态，并 MUST 不把数据库异常传播为采集线程终止

#### Scenario: 已初始化表结构启用外键约束
- **WHEN** 本机库或服务器目标库使用现有 `afp_layer` 到两张采样表的外键关系保存新铺层
- **THEN** 同一事务 MUST 先建立或更新父铺层记录，再流式写入 `afp_sensor_sample` 与 `afp_sample_all`，最终更新样本数；MUST NOT 因先写子表触发 1452，也不得通过删除外键规避顺序错误

### Requirement: 隔离客户端必须从独立的本机 MySQL 身份开始
Windows Sandbox 或等价的新 Windows 用户验收环境 SHALL 从没有可用 helper-local MySQL 配置的状态开始。验收 MySQL MUST 只绑定 `127.0.0.1`，凭据只能在隔离客户端内生成和使用，导出的环境、状态、计数和诊断证据 MUST 不包含密码或可复用密文。

#### Scenario: 干净客户端缺少 MySQL 系统运行库
- **WHEN** 隔离客户端尚未安装 MySQL 二进制依赖的 Microsoft Visual C++ 运行库
- **THEN** 验收引导 SHALL 只接受有效 Microsoft Authenticode 签名的 x64 Redistributable 并只安装到隔离客户端，MUST NOT 修改宿主机运行库

#### Scenario: 在禁用名称解析时建立回环账号
- **WHEN** MySQL 以 `skip-name-resolve=1` 完成首次数据目录初始化，且仅存在 `root@localhost`
- **THEN** 引导 SHALL 使用 Sandbox 内一次性 `init-file` 创建 `afp_app@127.0.0.1` 和目标数据库，验证后删除引导文件，MUST NOT 通过 `root@127.0.0.1` 尝试首次登录或放宽监听地址

#### Scenario: 首次启动隔离客户端
- **WHEN** 真实 helper 第一次在 Sandbox Windows 用户下启动且尚未保存本机 MySQL 配置
- **THEN** `local_mysql_profile` MUST 显示 `configured=false`，不得继承宿主机、服务器或交付目录中的密码

#### Scenario: 复制宿主 DPAPI 密文到隔离用户
- **WHEN** 验收仅把宿主机加密配置的测试副本放入 Sandbox 工作目录并尝试读取
- **THEN** helper MUST 返回 `missing` 或 `decrypt_failed` 并要求重新配置，MUST NOT 将解密失败回退为空密码，也不得覆盖真实 Sandbox 配置

#### Scenario: 同一 Sandbox 用户重启 helper
- **WHEN** 用户在 Sandbox 中保存并预检 helper-local MySQL 后只重启真实 helper
- **THEN** helper SHALL 在相同 Windows 用户保护范围内继续报告已配置并可再次预检，浏览器和服务器仍不得获得密码

#### Scenario: 隔离客户端完成本机写入
- **WHEN** Sandbox 内 `afp_app` 以 `127.0.0.1:3306` 通过预检并完成 50 Hz × 600 秒真实 helper 合成采集
- **THEN** 连接结果 MUST 标记 `scope=helper_local` 和访问电脑执行端，本机数据库行数 MUST 与该会话已接受完整行数相等；若写入失败，CSV MUST 仍存在且证据 MUST 记录独立失败状态而不泄露密码
