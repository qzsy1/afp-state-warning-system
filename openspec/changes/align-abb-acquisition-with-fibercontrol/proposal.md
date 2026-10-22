# Proposal

## Why

AFP 与 `D:\铺丝头\铺丝头上位机\程序\FiberControl_Fusion` 读取相同的 ABB ROB_1/Base 位置并使用相同速度公式，但采样调度、缓存时间和故障恢复不同，影响数据对照。需要对齐有效测量的采样目标并补齐可持久化的来源证据，同时避免把陈旧坐标或速度当成当前实测值。

## What Changes

- 保留既有 RWS 坐标资源、XYZ 单位和相邻位置空间距离除以实际时间差的线速度公式；默认机械单元 `ROB_1`、坐标系 `Base`，不新增倍率、偏置、滤波或补偿。
- 将 ABB 独立轮询改为单调时钟下的 100 ms 目标周期，替代每次请求结束后额外等待至少 100 ms；忙时跳过，不堆积请求，不复制参考程序的控制优先级队列。
- 为位置响应保存主机实际接收时间、源序号、会话代次、资源身份及原始 XYZ；线速度保存前后位置关联、距离、实际时间差和计算依据。速度间隔使用单调时钟，墙钟只用于对账，不冒充机器人设备时间。
- 保留首点速度未验证、新鲜度内沿用、过期留空和故障质量标记。故障、重连或过期后重新建立两点速度依据，不照搬参考程序无限保留成功缓存的行为。
- 使检查、本机直采、helper、WebSocket、主 CSV、时间戳侧车及 MySQL 来源记录共享测量合同；旧文件继续可读，规范通道、单位及模型维度保持兼容。
- 增加可执行的 RWS、时序、故障、跨模式与保存对账回归，通过后受控更新唯一正式运行目录，并将软件交付与实物一致性分别验收。

## Capabilities

### New Capabilities

- `abb-motion-measurement`：ABB 坐标与派生线速度的资源身份、采样周期、时间依据、质量和来源追溯。

### Modified Capabilities

无。当前没有已归档主规范；本能力与在途 `fix-plc-abb-uvc-real-acquisition` 的 Digest、路由、会话及设备隔离合同衔接，保留 PLC 兼容更新已有行为。

## Impact

- 涉及 `visualization_app/acquisition.py` 的 ABB 驱动及配置、`local_direct_acquisition.py`、统一来源结构、helper、持久化、前端说明和相邻测试；优先复用现有独立适配线程与有界缓存。
- 不修改参考源码、不引入 C#/.NET 生产依赖，不移植 Mastership、RAPID 写入、运动控制、压力闭环或报警握手。
- `currentLayer`、`currentAngle`、`currentPathNo`、`zLayerOffset`、`zCorr` 等额外字段及控制功能单列为范围外信息；不新增采集列，不改变 `zMovespeed` 设定值含义。
- 实际轮询频率和时钟异常时速度行为可能变化，需以来源档与回归明确；同工况 XYZ 固定偏移或倍率差异的现场根因仍未确认，不据此添加校正。
