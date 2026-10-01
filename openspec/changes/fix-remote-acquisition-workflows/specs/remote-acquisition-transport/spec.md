## Purpose

定义访问电脑 local helper、服务器和远程浏览器之间低延迟、有序、可恢复且可观测的实时采集传输合同，避免同机环境掩盖跨电脑积压、重复和无效探测问题。

## ADDED Requirements

### Requirement: 五类传感器接口必须保持原有独立协议
系统 SHALL 分别保留 PLC（Modbus TCP）、ABB（RWS）、热电偶（SMRF USB HID）、薄膜压力（M3232 串口）和热成像仪（UVC）的接口卡、驱动、端点及通道归属。模拟模式 MAY 让同一 CSV 提供数据，但 MUST NOT 将五类接口改写为一个通用接口或同一个物理接口；切回真实模式 MUST 恢复原有五类映射且不得启动错误类型的硬件驱动。

#### Scenario: 新采集方案与已有预测检查点保持边界
- **WHEN** `new_collection_v11_3` 采集方案启用 PLC 压力、薄膜压力、热成像、ABB 和 8 路热电偶共 17 个采集通道，而已有预测检查点的元数据只声明其中 16 个输入通道
- **THEN** 原始采集和保存 MUST 保留 17 个通道，预测 SHALL 只使用检查点明确声明且属于采集通道的输入子集，MUST NOT 删除薄膜压力采集通道，也不得向模型伪造未训练输入

#### Scenario: 模拟与真实模式往返
- **WHEN** 操作员在五类接口配置下切到模拟采集并再切回真实采集
- **THEN** 两种界面中五类逻辑接口的类型、协议和通道归属 MUST 可区分，真实模式原有物理绑定 MUST 保持不变

#### Scenario: 模拟数据源类型切换后保留接口选择
- **WHEN** 操作员在模拟模式中调整五类接口启用状态、驱动或通道归属，然后切换 CSV 与文件夹数据源，或切回真实模式后再次进入模拟模式
- **THEN** 模拟接口卡 MUST 保留刚才的独立选择，连接检查结果 MUST 报告每张卡原有协议而非统一的 `simulator`；真实模式 MUST 恢复此前的物理接口映射

#### Scenario: 局域网或客户端实验室模拟会话启动
- **WHEN** 局域网或客户端实验室已授权用户用模拟 CSV 启动实际模拟采集
- **THEN** 后端会话 MUST 保留五类接口元数据及通道映射；当该会话由 helper 本地回放时，helper MUST 只使用已校验模拟源生成样本，不得连接 PLC、ABB、热电偶、薄膜压力或热像仪硬件

### Requirement: helper 批次传输有界且有序
系统 SHALL 以确认驱动的方式连续排空 helper 样本队列，同一会话内任何时刻最多存在一个未确认批次，并 MUST 保持既有序号、重复确认和断线重传语义。

#### Scenario: 持续采集不形成增长性积压
- **WHEN** 在受控局域网测试环境中以 50 Hz 连续产生样本 10 分钟且服务器正常确认批次
- **THEN** helper MUST 无丢失、无重复地提交全部样本，未发送积压 MUST 不随时间持续增长，端到端样本延迟 P95 MUST 不超过 1 秒

#### Scenario: 确认后立即继续排空
- **WHEN** 服务器确认当前未确认批次且 helper 队列仍有样本
- **THEN** helper MUST 无需等待固定一秒周期即可发送下一有界批次

#### Scenario: 断线后幂等恢复
- **WHEN** 批次已发送但确认前连接断开并随后恢复
- **THEN** helper MUST 重发同一批次，服务器 MUST 以会话和批次序号幂等确认且不得重复写入样本

#### Scenario: 新连接先于旧连接清理完成
- **WHEN** helper 已建立替代 WebSocket，而旧连接处理器随后才执行断开清理
- **THEN** 旧处理器 MUST NOT 将替代连接标记离线或清除其发送通道；服务器从多个线程发送命令、心跳确认和采样确认时 MUST 串行生成完整 WebSocket 帧

### Requirement: helper 模拟回放必须复用真实采集的传输与保存边界
访问电脑 helper 在局域网或客户端实验室执行实际模拟回放时 SHALL 使用与真实采集相同的本地样本队列、CSV 保存、可选 helper-local MySQL、单在途批次、ACK、断线重传和服务器完整采集日志边界。模拟回放的结果 MUST 标记为模拟数据，MUST NOT 伪装成物理传感器证据。公网预计算演示不适用本要求，不得建立这些运行时传输与保存边界。

#### Scenario: 十赫兹持续回放
- **WHEN** 已校验的模拟源至少包含 6000 行且 helper 以 10 Hz 连续回放 600 秒
- **THEN** 受控虚拟时钟验证 MUST 生成恰好 6000 个样本；实时验证的有效采样率 MUST 保持在 9.8–10.2 Hz，样本序号 MUST 连续，未发送积压 MUST 不随时间持续增长，端到端样本延迟 P95 MUST 不超过 1 秒

#### Scenario: 回放调度不累积处理耗时
- **WHEN** 文件读取、本地保存或批次发送在某个周期消耗额外时间
- **THEN** helper MUST 以单调运行时间和目标样本序号计算后续调度，MUST NOT 将每次处理时间累加到 100 ms 周期而使回放持续变慢

#### Scenario: 回放期间公网中断
- **WHEN** helper 模拟回放期间与服务器的公网连接暂时中断后恢复
- **THEN** helper MUST 在本地按计划继续回放和保存，恢复后 MUST 从未确认批次开始幂等补传，服务器和数据库 MUST 不产生重复或缺失行

#### Scenario: 采集频率与页面刷新解耦
- **WHEN** helper 正在以 10 Hz 回放且浏览器绘图或预测处理暂时慢于样本产生
- **THEN** helper 本地采集与保存 MUST 继续按 10 Hz 运行，服务器和浏览器 MAY 合并显示更新，但 MUST 报告最新样本序号和各阶段延迟，MUST NOT 用页面刷新频率反向限制采集频率

### Requirement: 远程初始化不得探测服务器本机设备
系统 SHALL 按访问角色决定是否执行服务器本机 PLC、ABB 和 RTSP 发现；远程 helper 或纯浏览器会话 MUST 跳过不会被该会话使用的服务器本机发现。

#### Scenario: 公网远程 Bootstrap
- **WHEN** 非 `local_admin` 用户通过公网请求应用 Bootstrap 数据
- **THEN** 响应 MUST 不等待服务器本机 PLC、ABB 或 RTSP 超时探测，并 MUST 返回适用于该远程角色的接口状态

#### Scenario: 授权远程用户打开真实模式
- **WHEN** 已授权远程用户打开网页而访问电脑 helper 尚未配对或正在连接
- **THEN** Bootstrap MUST 仍返回与软件端一致的五类静态传感器类型、独立驱动和默认接口卡，MUST NOT 因跳过服务器硬件探测而仅显示自定义 JSON；实际物理接口仍 SHALL 等待访问电脑 helper 识别，不得伪称服务器设备可用

#### Scenario: 服务器本机管理访问
- **WHEN** `local_admin` 在回环管理入口请求 Bootstrap 数据
- **THEN** 系统 MAY 保留现有服务器本机接口发现行为

### Requirement: 实时推送按数据版本更新
系统 SHALL 仅在会话采集数据或状态版本变化时重新生成实时业务载荷，并 MUST 使用轻量心跳维持空闲连接，而不是周期性重复计算未变化的预测和状态。

#### Scenario: 数据未变化
- **WHEN** WebSocket 会话保持连接且采集版本与状态版本均未变化
- **THEN** 系统 MUST 不重复执行同一业务载荷的聚合或预测计算，并 SHALL 仅按心跳策略维持连接

#### Scenario: 新样本到达
- **WHEN** 当前会话收到新批次并推进采集版本
- **THEN** 系统 MUST 生成一次与新版本对应的实时载荷并推送给订阅该会话的浏览器

#### Scenario: 预测结果暂含非有限数值
- **WHEN** 实时预测载荷中的数值暂时为 NaN 或无穷大
- **THEN** WebSocket 推送 MUST 与现有 HTTP JSON 行为一致地将该值转换为 null，并 MUST 不因此中断整个实时数据服务；传感器读取与预测算法本身不变

### Requirement: 传输延迟可观测
系统 SHALL 暴露不含凭据和样本敏感内容的实际执行端、队列深度、未确认批次、最后确认序号、样本产生时间、服务器接收时间和浏览器发布版本，用于区分采集慢、上传慢和显示慢。

#### Scenario: 查看远程采集状态
- **WHEN** 有权访问会话状态的用户查看远程采集诊断信息
- **THEN** 系统 MUST 返回足以计算各阶段延迟的安全指标，并 MUST 标明指标所属 helper 会话

#### Scenario: 区分队列年龄与浏览器处理耗时
- **WHEN** helper 待传队列增长，且样本产生时间与浏览器显示时间相差数秒
- **THEN** 页面 MUST 分别显示由 `待传点数/采样率` 计算的队列年龄、含跨电脑时钟偏差的样本端到端年龄、载荷到达频率和浏览器本机处理耗时；MUST NOT 将样本端到端年龄命名为浏览器发布耗时，也 MUST NOT 将载荷到达频率命名为浏览器绘图帧率

#### Scenario: 公网角色在浏览器回放预计算演示
- **WHEN** 访客或公网已授权页面播放已校验的预计算演示包
- **THEN** 页面 MUST 完全以浏览器本地演示时钟和代次状态显示进度，MUST NOT 启动服务器采集/保存会话或定时读取采样状态；停止后的旧计时器、绘图任务或迟到资源回调 MUST NOT 覆盖冻结结果

### Requirement: 公网预计算演示不得进入实时采集传输链
公网模拟演示 SHALL 在演示包下载并校验完成后停止使用 helper、采集 WebSocket、实时 HTTP 轮询、批次 ACK、服务器镜像和保存终结协议。开始和停止 SHALL 只改变浏览器本地演示状态；非阻塞的日志或统计即使存在也 MUST NOT 位于响应关键路径，且 MUST 不包含样本、凭据或本地路径。

#### Scenario: 数据就绪后开始演示
- **WHEN** 演示包已就绪且用户点击开始
- **THEN** 页面 MUST 不发起 `/api/acquisition/start`、`/api/simulation/start`、helper `start_capture` 或实时订阅请求，并 MUST 在 200 ms 内显示首帧

#### Scenario: 运行中绘图较慢
- **WHEN** 某次图表绘制耗时超过一个 100 ms 采样周期
- **THEN** 浏览器 MUST 根据单调时钟计算当前应显示的最新样本索引并合并中间点，MUST NOT 排队逐帧补画或降低虚拟 10 Hz 时间线

#### Scenario: 停止演示
- **WHEN** 用户点击停止
- **THEN** 页面 MUST 先同步设置停止状态并递增演示代次，再取消 timeout/animation frame，所有异步回调 MUST 在渲染前核对代次；波形和游标 MUST 在 100 ms 内冻结

#### Scenario: 公网真实采集保持原传输
- **WHEN** 已授权用户通过公网选择真实采集并由访问电脑 helper 读取五类物理接口
- **THEN** 系统 MUST 继续使用既有配对、WebSocket、批次 ACK、断线补传、CSV/MySQL 和服务器预测链路；公网演示优化 MUST 不改变真实采集协议或把真实数据替换成演示包

### Requirement: 隔离客户端验收必须可重复且保持会话边界
系统 SHALL 支持在独立 Windows 用户环境中用真实 helper 和只绑定回环地址的合成 TCP JSON 源验证公网采集链路，并 SHALL 支持用不加载物理驱动的协议级虚拟 helper 重复验证传输合同。验收数据 MUST 标记为合成数据，且 MUST NOT 被描述为五类物理设备或真实缺陷证据。

#### Scenario: 真实 helper 持续采集十分钟
- **WHEN** 隔离客户端使用真实 helper 以 50 Hz 接收回环合成源并持续 600 秒
- **THEN** 合成源 MUST 生成恰好 30,000 行，helper 确认序号 MUST 持续推进，同一会话最多一个未确认批次，浏览器 MUST 保持可响应，且验收记录 MUST 分别报告生成、服务器接受、CSV 和数据库行数

#### Scenario: 协议级虚拟 helper 断线恢复
- **WHEN** 虚拟 helper 在 50 Hz × 600 秒运行到第 300 秒时断开 10 秒，并以相同采集身份和未确认批次重连
- **THEN** 服务器 MUST 接受恰好 30,000 行、零缺失、零重复，重发 MUST 幂等确认，最大在途批次数 MUST 为 1，端到端延迟 P95 MUST 不超过 1 秒

#### Scenario: 两个验收会话并行或先后运行
- **WHEN** 真实 helper 和协议级虚拟 helper 使用各自的一次性配对会话执行验收
- **THEN** 各会话的 `capture_uuid`、确认序号、状态、文件和数据库结果 MUST 保持隔离，任一会话的断开或停止 MUST NOT 清理或覆盖另一会话

### Requirement: 局域网网址必须指向实际可用的物理网络
系统 SHALL 动态识别当前可供其它局域网电脑访问的地址并返回唯一推荐网址，同时 MAY 返回其它候选地址。推荐排序 MUST 优先具有 IPv4 默认网关且已启用的物理以太网或 Wi-Fi，MUST NOT 将回环、APIPA、Hyper-V、WSL、Docker、Tailscale、代理隧道或测试网段地址作为首选。系统 MUST NOT 硬编码现场 DHCP 地址。

#### Scenario: 物理网卡与 Hyper-V 同时存在
- **WHEN** 服务器同时具有 `192.168.101.31` 的物理网卡和 `172.29.32.1` 的 Hyper-V 虚拟网卡，且只有物理网卡具有实际默认网关
- **THEN** 状态 API 和右上角显示 MUST 推荐 `http://192.168.101.31:8770/`，同时 MAY 在候选列表保留虚拟地址但 MUST 标明其非推荐状态

#### Scenario: DHCP 地址发生变化
- **WHEN** 物理网卡重连或续租后推荐 IPv4 发生变化
- **THEN** 状态 API SHALL 在下一次刷新返回新网址，页面 MUST 更新显示和复制目标，不得继续展示旧地址

#### Scenario: 浏览器已通过局域网地址访问
- **WHEN** 页面当前 origin 是私有 IPv4 且健康接口可用
- **THEN** 页面 SHALL 将当前 origin 视为已验证可达路径并优先展示，除非服务端明确标记该地址已失效

### Requirement: 局域网与公网必须采用一致的实时传输策略
支持 WebSocket 的浏览器 SHALL 在公网 HTTPS 使用 `wss://`，在局域网或回环 HTTP 使用 `ws://` 订阅同一实时会话合同。只有 WebSocket 握手或连接失败时 MAY 降级为 HTTP 轮询；降级轮询 MUST 单在途且间隔不得低于 500 ms，MUST NOT 使用 100 ms 高频短连接作为局域网默认路径。

#### Scenario: 局域网实时页面
- **WHEN** 用户通过 `http://192.168.x.x:8770/` 打开实时页面且浏览器支持 WebSocket
- **THEN** 页面 MUST 建立 `ws://` 实时连接并停止周期性 `/api/live` 请求，预测载荷、会话权限和心跳行为 SHALL 与公网 `wss://` 一致

#### Scenario: WebSocket 暂时失败
- **WHEN** WebSocket 握手失败或连接中断
- **THEN** 页面 SHALL 以单在途低频 HTTP 请求维持可用状态并有界重试 WebSocket，MUST NOT 并发堆积请求或无限缩短轮询间隔

### Requirement: helper 硬件检查不得阻塞采集控制
helper SHALL 将可能阻塞的真实硬件检查置于独立、可终止且有总时限的执行边界。`start_capture`、`stop_capture`、`status`、心跳和采样确认 MUST NOT 排在卡死检查之后；重复检查 MUST NOT 形成无界 FIFO。真实采集开始前 SHALL 取消或终止仍在运行的旧检查，模拟采集 MUST NOT 创建真实硬件检查任务。

#### Scenario: 供应商驱动在检查中阻塞
- **WHEN** 任一 PLC、ABB、HID、串口或 UVC 检查超过总时限
- **THEN** helper MUST 终止检查执行边界并返回超时及对应阶段，心跳 SHALL 继续推进；后续状态或停止命令 MUST 在有界时间内响应

#### Scenario: 检查未结束时开始真实采集
- **WHEN** 用户在自动检查仍运行时点击开始真实采集
- **THEN** helper SHALL 请求取消检查并在有界等待后开始采集或返回明确的“检查正在终止”状态，MUST NOT 把开始命令静默排在检查之后直至浏览器超时

#### Scenario: helper 版本不兼容
- **WHEN** helper 握手缺少系统要求的协议版本或命令生命周期能力
- **THEN** 页面 MUST 将其与离线状态区分并提示更新 helper；服务器 MUST NOT 将版本不兼容误报为网络掉线

### Requirement: helper 必须保持用户配对时选择的固定服务地址
局域网和公网授权采集 SHALL 默认使用同一 helper 采集、保存和上传合同，但 helper MUST 持续使用用户配对或保存配置时输入的服务地址。系统 MUST NOT 根据浏览器当前 origin、延迟测量或网卡变化自动切换 helper 的局域网/公网服务地址，也 MUST NOT 静默改写 helper 配置。页面 SHALL 显示 helper 当前服务地址类型、测得往返延迟和与浏览器访问路径是否一致；不一致时 SHALL 提醒用户按当前验收场景重新配对，但 MUST NOT 自动重配。

#### Scenario: 局域网验收使用局域网地址配对
- **WHEN** 用户通过实际可用的 LAN URL 打开页面并将 helper 配对到该 LAN URL
- **THEN** helper SHALL 在该固定 LAN 链路上传数据，页面 MUST 显示局域网路径和当前往返延迟，不得改用公网地址

#### Scenario: 公网真实采集验收使用公网地址配对
- **WHEN** 用户通过公网 HTTPS 页面选择真实采集并将 helper 配对到公网服务地址
- **THEN** helper SHALL 在该固定公网链路上传数据，页面 MUST 显示公网路径和当前往返延迟，不得因同时存在局域网而改用 LAN 地址

#### Scenario: 浏览器和 helper 使用不同路径
- **WHEN** 浏览器从 LAN URL 打开但 helper 仍配对公网地址，或反之
- **THEN** 页面 SHALL 显示明确的不一致提醒和重新配对入口，但服务器与 helper MUST 继续使用当前已确认地址直至用户主动更改

### Requirement: 首批样本等待和新采集边界必须可恢复且可诊断
服务器 SHALL 仅在 helper 明确返回 `ok=true`、`running=true` 和与当前命令一致的 `capture_uuid` 后进入首批样本等待。helper 返回 `ok=false`、缓存未就绪、源无有效样本或身份不一致时，页面 MUST 立即显示原始可操作错误，不得继续固定等待十五秒。开始新采集时 helper SHALL 清除或隔离上一采集的待确认批次、确认序号和唤醒状态，使新 `capture_uuid` 从自身序列边界独立运行。

#### Scenario: helper 拒绝开始命令
- **WHEN** helper 对 `start_capture` 返回 `ok=false` 或未返回当前 `capture_uuid` 的运行状态
- **THEN** 服务器 MUST 立即结束开始流程并透传分类后的根因，MUST NOT 将其改写为“十五秒内未收到首批有效采集数据”

#### Scenario: 上一次采集仍存在未确认批次
- **WHEN** 用户停止或失败后启动新的 `capture_uuid`，且 helper 内仍保留旧采集的未确认批次
- **THEN** helper MUST 将旧批次归档到旧会话的恢复边界并为新会话重置发送状态，新会话 MUST NOT 因旧批次等待 ACK 而无法发送首批样本

#### Scenario: 首批样本超过目标时间
- **WHEN** helper 已报告运行但服务器在目标时间内未收到首批样本
- **THEN** 状态 SHALL 根据 helper 本地产生样本数、队列深度、未确认批次、最后 ACK、连接在线状态和传输错误区分“本地未产生样本”“等待上传或 ACK”“helper 掉线”，不得只返回单一超时提示

### Requirement: helper 样本上传必须由新样本和 ACK 事件驱动
helper SHALL 在产生新样本或收到 ACK 时唤醒发送循环，并 MUST 保持同一会话最多一个未确认批次。发送循环 MUST NOT 把固定 250 ms 轮询等待作为每批数据的必经延迟。批量大小 SHALL 根据当前固定配对链路的实测 RTT、采样率和积压在有界范围内调整，并 MUST NOT 触发服务地址切换；局域网目标发送周期 SHALL 为 100–200 ms，公网批量 MUST 至少覆盖一个 ACK 往返期间产生的样本并保留安全余量，积压增长时 MAY 在行数和字节上限内扩大批量。采样调度、本机 CSV、本机 MySQL、上传 ACK、服务器保存和浏览器绘图 SHALL 解耦，任一较慢环节不得改变 helper 的 10 Hz 样本时间线。

#### Scenario: 局域网低延迟回放
- **WHEN** helper 通过固定 LAN 地址执行已缓存的 10 Hz 模拟回放且无网络故障
- **THEN** 新样本 SHALL 在 100–200 ms 发送目标内形成批次，ACK 到达后 SHALL 立即尝试排空下一批，固定空等不得使有效采样率下降

#### Scenario: 公网高 RTT 回放
- **WHEN** helper 通过固定公网地址执行 10 Hz 回放且 RTT 高于局域网
- **THEN** helper SHALL 以 `ceil(采样率 × (RTT + 安全余量))` 为最低可持续批量，并根据队列年龄在不超过 50 行和传输字节上限的范围内恢复积压；在 RTT 约 1.2 秒时 10 Hz 批量 MUST 不低于 13 点，且 SHALL 保留原始 10 Hz 时间戳、连续序号、单在途 ACK、零重复和断线补传能力

#### Scenario: 浏览器渲染变慢
- **WHEN** 浏览器标签页降频、绘图耗时或网络发布频率低于 10 Hz
- **THEN** helper 本地采样与保存 SHALL 继续稳定推进；页面 SHALL 分别显示本地有效采样率、待发队列与预计队列年龄、ACK RTT、含时钟偏差的样本端到端年龄、载荷到达频率和浏览器本机处理耗时，不得将载荷频率冒充采样率或绘图帧率

#### Scenario: 十分钟耐久验收
- **WHEN** helper 使用已校验的 6000 点合成数据包以 10 Hz 回放 600 秒
- **THEN** 本地生成、服务器接受和已启用保存端 SHALL 按各自合同记录 6000 个连续序号，零缺失、零重复，有效采样率 SHALL 为 9.8–10.2 Hz，队列不得持续增长，端到端延迟 P95 SHALL 不超过 1 秒

### Requirement: 停止必须区分本地采样停止与可靠补传完成
helper 收到停止命令后 MUST 立即停止产生新样本并返回本地产生总数、待传点数和本地停止时间；为保证完整保存，既有待传批次 SHALL 继续按原 `capture_uuid` 可靠补传。服务器和页面 MUST 将该阶段显示为“本地已停止，正在同步历史数据”，不得继续显示为“采集中”。只有最终状态及全部批次确认、已启用保存端进入终态后，系统才 SHALL 显示“停止并保存完成”。

#### Scenario: 停止时仍有待传积压
- **WHEN** 用户点击停止时 helper 已停止本地采样但仍有 450 个历史样本待确认
- **THEN** helper 产生总数 MUST 不再增长，页面 MUST 立即进入 `local_stopped_flushing` 状态并显示剩余点数；服务器镜像 MAY 随补传继续更新，但 MUST 明确标记为历史数据同步而不是新采集

#### Scenario: 最后批次已确认
- **WHEN** 停止后的全部样本和最终状态均已被服务器确认，且已启用保存端成功或明确失败
- **THEN** 状态 MUST 进入 `completed` 或持久失败终态，页面 SHALL 解除停止按钮的忙碌状态并显示实际保存结果，不得把仍有待传点数的中间状态当成完成
