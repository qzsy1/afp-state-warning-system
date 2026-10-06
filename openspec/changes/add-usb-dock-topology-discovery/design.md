# Design

## Context

参见 `proposal.md` 的动机。当前 `AcquisitionManager.discover_interfaces()` 把串口、SMRF HID、UVC 驱动占位和带 IPv4 地址的网卡放入同一个 `physical_interfaces` 扁平数组；空 USB 端口没有对应 PnP 设备，因此现有枚举无法看到。前端 `physicalCandidatesForRole()` 只接受与逻辑接口完全相同的 `kind`，`refreshPhysicalInterfaceOptions()` 生成单层下拉框。`LocalCaptureAgent.discover()` 复用相同结果为五类逻辑接口分配实时设备。

当前连接的拓展坞由 Windows 表现为 Genesys Logic USB 2.x Hub（VID 05E3、PID 0610）、SuperSpeed Hub（VID 05E3、PID 0626）以及挂在 SuperSpeed Hub 下的 ASIX AX88179B 网卡（VID 0B95、PID 1790）。目标实物有三个用户可连接 USB 3.0 口和一个网口。设计必须避免把两套 Hub 通道计为六个端口，也不能把连接内置网卡的内部端口算作第四个用户 USB 口。当前规范化阶段排除 Root Hub，因此电脑机身的空闲 USB 连接器缺失；前端又把 `physical_kind` 当成物理类型，导致 USB 转串口 COM 与其实际 USB 端口分离。

Windows 8 及以后提供用户态 USB Hub IOCTL，可枚举端口连接状态，并通过 `IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES` 获得共享连接器的伴随 Hub 与端口关系。发行程序为冻结 Python 应用，方案需要避免要求用户安装 Windows SDK、USBView 或额外驱动。

## Goals / Non-Goals

**Goals:**

- 生成能够表达拓展坞、共享物理连接器、实时设备和内置网口的规范化拓扑。
- 让 USB 类接口看到所有外部 USB 口，并在空闲、占用、异常和断开之间保持明确状态。
- 同时纳入能够由 Windows 确认的电脑原生用户可连接 USB 端口，并把 HID、UVC、COM 当作端口下的通信端点。
- 为热电偶、UVC、M3232、PLC 和 ABB 生成可解释、稳定且尊重手动选择的默认物理绑定。
- 保持本机、LAN 与公网 helper 的同一接口契约和硬件来源边界。
- 让现有设备级绑定逐步兼容端口级预选，而不改变五类采集协议。
- 对 Windows API、单个 Hub 和未知拓展坞实施局部降级。

**Non-Goals:**

- 不提供端口备注、用户别名或左/中/右位置标定。
- 不根据系统端口号推断拓展坞的物理排列方向。
- 不把拓扑识别当作设备协议握手、传感器数据有效性或采集成功证明。
- 不修改 SMRF HID、M3232 串口、UVC、PLC Modbus TCP、ABB RWS 的驱动实现和通道解析。
- 不在非 Windows 平台模拟不存在的空 USB 端口。

## Decisions

### 1. 新增独立的 Windows USB 拓扑提供器

新增 `visualization_app/windows_usb_topology.py`，使用标准库 `ctypes` 调用 SetupAPI、CfgMgr32、Kernel32 `CreateFileW` 和 `DeviceIoControl`。模块负责枚举 `GUID_DEVINTERFACE_USB_HUB`、打开 Hub、获取端口数量、逐端口查询连接信息、速度、描述符、下级 Hub 名称和伴随连接器属性，并通过配置管理器补充 PnP 实例、父级、友好名称和 Location Path。

主要 IOCTL 包括：

- `IOCTL_USB_GET_HUB_INFORMATION_EX` 或兼容的节点信息查询：获得 Hub 能力与端口上限。
- `IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX`：获得空闲、连接、枚举、供电不足、过流等连接状态及设备描述符。
- `IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2`：获得端口和当前设备支持的 USB 协议与速度信息。
- `IOCTL_USB_GET_NODE_CONNECTION_NAME`：递归进入下级 Hub。
- `IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES`：读取用户可连接属性、伴随 Hub 符号链接和伴随端口号。

所有原始句柄在单次发现结束时关闭；底层结构与常量集中在该模块并通过固定二进制夹具测试。生产代码不启动 PowerShell、WMI 或外部 USBView 进程。

**替代方案：** 只使用 `Get-PnpDevice`/WMI。该方法只能看到已经枚举的设备，不能可靠列出空端口或共享连接器，无法满足需求。捆绑 USBView 可执行文件会扩大交付体积和进程边界，因此不采用。

### 2. 使用系统伴随连接器关系合并 USB 2.x 与 SuperSpeed 通道

拓扑提供器先保留原始 Hub/端口节点，再以 `CompanionHubSymbolicLinkName + CompanionPortNumber` 建立无向连接器关系。一个关系分量归一化为一个 `usb_port`，记录其 USB 2.x/3.x 通道、各自状态和系统端口号。稳定 ID 由电脑范围内的控制器/上游位置、拓展坞路径和连接器关系生成摘要，原始符号链接不直接用作网页标识。

只有显式伴随属性或版本化的受信拓展坞规则能够触发合并。端口数量、VID/PID 相似或枚举顺序只能作为诊断证据，不能单独决定合并。未知或查询失败时保留独立节点并报告 `merge_state=unknown`。

**替代方案：** 按两套 Hub 端口序号直接一一配对。不同厂商可能改变端口编号或包含内部端口，该方法会错误合并，故不采用。

### 3. 拓展坞使用“用户可连接”属性，本机 Root Hub 还需实际观察证据

若连接器属性明确报告是否可供用户连接，以此区分外部端口和固定内部功能。无法获得该属性时，默认保持端口可见，避免隐藏真实接口。针对当前实物建立窄范围、版本化规则：只有 Hub VID/PID、父级关系、端口布局和 ASIX VID/PID 全部匹配时，才将相应端口建模为 `internal_function=ethernet`，并把 ASIX 设备投影到该拓展坞的以太网接口。

Root Hub 的 `PortIsUserConnectable` 在部分电脑上会覆盖同一平台不同配置的预留或未布线端口，不能单独证明机身插孔存在。本机端口只有在用户可连接属性成立且当前出现设备/下级 Hub，或稳定端口 ID 已存在于本机观察记录时才进入候选。观察记录写入本机可变运行目录，不进入发行清单；设备拔出后依靠该记录保留空闲端口。连接拓展坞的 Root Hub 上游连接器属于真实机身插孔，继续显示为“电脑本机 USB”并标记已占用；拓展坞下行端口另行显示且不会与上游插孔合并。

规则不依赖当前动态 COM 号或 IPv4 地址。规则未完全匹配时不排除端口，并输出原因供测试和诊断。

**替代方案：** 所有网卡设备所在端口都视为内部端口。这会隐藏用户外接的 USB 网卡，故不采用。

### 4. 拓扑数据与兼容的物理接口投影并存

`discover_interfaces()` 保留现有 `ports`、`physical_interfaces`、`defaults` 等字段，并新增版本化的 `usb_topology`：

```text
usb_topology
├─ schema_version
├─ state / provider / errors
├─ docks[]
│  └─ id, label, state, upstream_location, hub_refs[]
├─ usb_ports[]
│  └─ id, dock_id, system_port_number, connector_type,
│     supported_protocols[], state, user_connectable, device_id
└─ devices[]
   └─ id, parent_port_id, class, friendly_name, vid, pid,
      serial, live_interface_id
```

`physical_interfaces` 继续承载可被现有驱动打开的实时设备和网卡，并为能够定位的项目补充 `dock_id`、`parent_port_id` 与系统生成的 `topology_label`。`usb_ports` 承载空端口和端口状态，不伪装为已验证设备。

**替代方案：** 用端口记录替换 `physical_interfaces`。这会让驱动失去 HID path、COM 和 UVC 端点并破坏旧 helper，故采用并行扩展契约。

### 5. USB 配置同时保存计划端口和实时设备

USB 类接口增加可选 `physical_port_id`，表示操作员选择的物理连接器；现有 `physical_interface_id` 继续表示可由协议驱动访问的实时设备。选择空端口时只设置前者，状态为 `port_selected_device_missing`。刷新发现兼容设备位于该端口后，系统设置实时设备绑定，但仍需原有连接与数据检查把 `physical_verified` 置为真。

旧配置只有 `physical_interface_id` 时继续可用；如果拓扑能够解析其父级，则在内存配置和页面状态中补充 `physical_port_id`，不修改驱动、端点或通道映射。非 USB 接口维持现有设备级绑定。

重复绑定校验以实际资源为准：两个非共享逻辑接口不得选择同一 USB 物理端口；PLC 与 ABB 仍可共享同一以太网适配器。

**替代方案：** 把端口 ID 直接写入现有 `physical_interface_id`。这会混淆可打开设备与物理插孔并使现有驱动尝试打开无效端点，故不采用。

### 6. 前端使用物理传输族候选模型，不在 HTML 中复制拓扑推断

后端返回规范化拓扑，前端只执行展示和按逻辑类型筛选：

- 热电偶、UVC 和 M3232 共享 `usb_ports` 中全部用户可连接端口；标签包含电脑或拓展坞归属、系统端口号、连接器能力、状态和当前设备。
- HID、UVC 和 USB 转串口 COM 是物理 USB 端口下的实时通信端点。M3232 选择 USB 转串口时同时保存 `physical_port_id`、`physical_interface_id` 和 COM `endpoint`。
- 只有无法关联 USB 父级或 USB 位置路径的真正主机原生 COM 才在 M3232 下显示为独立串口候选。
- 以太网显示全部实时网卡；已知父级时显示“拓展坞网口”。
- 通过 `<optgroup>` 或等效结构按“电脑本机 USB”“拓展坞 USB”“位置未解析的 USB 设备”“主机原生串口”和“网卡”组织，保持键盘选择和现有表单行为。

前端不生成用户备注，不将 `USB3-1` 解释为物理左侧第一口。空闲和不兼容端口可作为计划位置选择，但启用真实采集前的校验显示具体阻断原因。真正的主机原生 COM 不进入 HID/UVC 候选列表。

### 10. 独立传输目录合并物理端口与实时端点

新增独立传输目录模块，输入 `usb_topology` 和原有 `physical_interfaces`，输出与传感器角色无关的物理候选目录。目录以 `transport_family=usb|serial_native|ethernet` 表示物理传输层，以 `endpoint_kind=hid|uvc|serial|network` 表示可由驱动打开的实时端点。

USB COM 优先通过 PnP 实例、父级链和 Location Path 定位物理端口；VID/PID/序列号只在结果唯一时回退。无法解析位置的原有 HID/UVC/USB 串口仍保留在“位置未解析的 USB 设备”组，避免拓扑增强删除旧候选。物理端口先按 `physical_port_id` 去重，未定位端点再按实时接口 ID 和 endpoint 去重。

拓扑提供器同时处理 Root Hub 中系统报告为用户可连接且已被当前占用或历史观察确认的端口。Root Hub 内部功能、未观察到的预留/未布线空逻辑端口和已确认不可供用户连接的端口不进入候选。拓展坞上游连接作为电脑本机已占用插孔保留，拓展坞下行端口仍单独归入拓展坞。Root Hub 属性或观察证据不足时不伪造空端口，但保留已枚举实时设备作为兼容降级。

### 11. 默认选择使用确定性优先级并记录选择来源

前端在真实接口发现完成后以统一候选目录分配默认值，优先级为：已保存选择、当前手动选择、已连接兼容端点、未分配空闲端口、未分配未知端口、其余可见端口。前三个 USB 标准角色按热电偶、UVC、M3232 顺序占用不同物理端口；PLC 与 ABB 保留共享网卡规则。

每个页面绑定记录 `selection_origin=saved|manual|auto`。刷新按稳定端口 ID 保持 `saved` 和 `manual` 选择；设备拔出只清除实时接口和验证状态。只有尚未被用户修改的 `auto` 选择能够在候选消失时重新计算。默认选中空端口不设置 `physical_verified`，协议与真实数据门禁保持现状。

### 7. helper 传输完整规范化拓扑，服务端只做会话投影

`LocalCaptureAgent.discover()` 返回原有 `bindings` 和 `raw_discovery`，其中 `raw_discovery.usb_topology` 来自 helper 所在电脑。helper 的绑定选择只从实时兼容设备产生，不把空端口声明为已检测设备。服务端在授权 helper 会话中转发规范化结果，并继续剥离服务器本机 `physical_interfaces`。

对网页公开的拓扑使用摘要 ID 和系统标签；内核符号链接、设备接口路径等仅驱动打开所需的值保留在 helper 内部或既有授权执行负载中。访客模拟 Bootstrap 保持空 `physical_interfaces`，且不新增拓扑内容。

### 8. 发现采用局部失败与有界刷新

每个 Hub 的打开和查询独立捕获错误。单个 Hub 失败不会删除串口、网卡和其他成功 Hub；结果以 `usb_topology.state=complete|partial|unavailable` 和去敏错误摘要表达。拓扑查询不做网络探测，也不等待传感器输出。

发现结果只在用户点击刷新、现有 helper 重新发现事件和既有受控自动刷新点更新，不增加高频轮询。刷新按稳定端口 ID合并状态：拔出设备会清除实时设备绑定但保留计划端口；拓展坞断开不会把选择自动迁移到另一拓展坞。

### 9. 源码为权威，验证后同步唯一 v2.0.4 发行目录

先在根目录 `visualization_app` 和测试中实现与验证。通过定向回归后，将相同外置 Python、JavaScript、HTML/CSS 和 helper 文件同步到 `delivery/AFP_Integrated_System_Modular_v2.0.4_Agentic` 的对应运行位置，重建受影响的 helper 或应用产物，更新静态资源缓存键、`VERSION.json`（若现有发布规则要求）和 `SHA256SUMS.txt`。

Git LFS 继续承载完整 v2.0.4 中已配置的大文件；实现阶段应校验 LFS 指针、工作树状态和远端提交，但不得创建新的并行发行版本。

启动器还需以发行目录 `SHA256SUMS.txt` 的内容摘要作为运行版本指纹，并由 `/api/health` 返回当前进程启动时加载的指纹。再次打开同一 v2.0.4 时，只有监听 8770/8771 的 AFP 服务指纹与当前发行目录一致才允许复用；若指纹不同，启动器仅终止与当前启动器可执行文件路径一致的旧 AFP 监听进程，再从当前外置模块重新启动。其他程序占用端口时不得强制终止。

## Risks / Trade-offs

- [厂商固件不完整或错误报告伴随端口] → 仅接受显式伴随映射或完整匹配的窄范围规则；不确定时保留端口并标记未知。
- [Root Hub 将预留或未布线端口报告为用户可连接] → 仅发布当前实际占用或本机历史观察确认的端口；通过稳定 ID 记录使已观察端口在拔出设备后仍保持可见。
- [当前拓展坞内部网卡端口缺少用户可连接标志] → 使用包含 Hub、父级、布局和 ASIX 身份的版本化规则，并以三 USB 加一网口实物验收防止误隐藏。
- [无序列号拓展坞移动到另一上游端口后身份变化] → 使用位置相关 ID 并明确不跨位置继承；避免把另一台同型号拓展坞误认为原设备。
- [冻结运行时 ctypes 结构体布局错误] → 对齐 Windows SDK 结构定义，增加大小、偏移、二进制夹具和真实 Windows smoke test；API 失败时局部降级。
- [旧 helper 不理解新增字段] → 新字段只做向后兼容扩展；服务端检测 `schema_version`，旧 helper 仍返回现有扁平接口且页面标记拓扑不可用。
- [端口已选但设备尚未验证造成用户误解] → 页面分别显示端口状态、兼容设备状态和数据检查状态，真实采集门禁只接受最后两项均通过。
- [底层位置或设备标识暴露给非授权用户] → 页面使用规范化摘要 ID，访客模拟不返回拓扑，远程真实结果只来自已配对 helper。

## Migration Plan

1. 先加入拓扑提供器和纯夹具测试，默认不改变现有 `physical_interfaces` 输出。
2. 扩展 `discover_interfaces()` 与 helper 契约；旧客户端忽略新增 `usb_topology` 字段。
3. 增加 `physical_port_id` 的兼容解析和校验，旧配置继续按设备级绑定运行。
4. 更新前端分组展示和端口预选，在真实采集前保持现有协议与数据门禁。
5. 使用当前拓展坞逐口插拔，确认三个 USB 口、一个 ASIX 网口、伴随通道合并和重连稳定性。
6. 同步并重建唯一 v2.0.4 发行目录，执行回归、哈希和 Git LFS 校验后提交。
7. 更新同一 v2.0.4 的外置代码后重新打开软件，确认旧驻留服务不会被复用，健康接口指纹与当前发行清单一致，并重新检查拓展坞接口下拉列表。

回滚时移除前端对 `usb_topology` 和 `physical_port_id` 的使用，保留后端新增字段不会影响旧消费者；如底层提供器有问题，可关闭拓扑提供器并继续使用现有扁平发现路径。
