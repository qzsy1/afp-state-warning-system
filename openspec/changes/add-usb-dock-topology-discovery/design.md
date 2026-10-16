# Design

## Context

参见 `proposal.md` 的动机。当前 `AcquisitionManager.discover_interfaces()` 把串口、SMRF HID、UVC 驱动占位和带 IPv4 地址的网卡放入同一个 `physical_interfaces` 扁平数组；空 USB 端口没有对应 PnP 设备，因此现有枚举无法看到。前端 `physicalCandidatesForRole()` 只接受与逻辑接口完全相同的 `kind`，`refreshPhysicalInterfaceOptions()` 生成单层下拉框。`LocalCaptureAgent.discover()` 复用相同结果为五类逻辑接口分配实时设备。

Windows 会以相同的 Hub 设备类表示电脑内部 Hub、普通外接 Hub 和拓展坞中的 Hub，且没有覆盖所有 USB、USB-C 与复合设备厂商的统一拓展坞标志。设备容器用于聚合同一物理设备的多个功能节点，端口可移除性和 `PortIsUserConnectable` 用于区分集成与外接路径，伴随端口关系用于合并共享连接器；这些证据必须组合使用。目标实物的 Genesys Logic USB 2.x/SuperSpeed Hub 与 ASIX 网卡继续作为正向兼容样例，但其 VID/PID 不是通用分类规则。

Windows 8 及以后提供用户态 USB Hub IOCTL，可枚举端口连接状态，并通过 `IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES` 获得共享连接器的伴随 Hub 与端口关系。发行程序为冻结 Python 应用，方案需要避免要求用户安装 Windows SDK、USBView 或额外驱动。

## Goals / Non-Goals

**Goals:**

- 生成能够表达拓展坞、共享物理连接器、实时设备和内置网口的规范化拓扑。
- 在不同电脑、主机控制器和 Root Hub 路径下，根据设备自身及 Windows 通用属性得到一致的设备归属分类。
- 普通外接 Hub 和证据不足的 USB 扩展设备保持端口可见，但不得显示为已确认拓展坞。
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

### 3. 使用设备容器、外接性和功能组成分类设备归属

拓扑提供器读取 `DEVPKEY_Device_ContainerId`、移除策略、设备能力、设备类别、父子 devnode、Location Path、端口用户可连接属性和伴随端口关系。ContainerId 和设备类别在返回给页面前转换为稳定摘要或有限枚举，不公开原始系统标识。

分类器先判断设备是否属于电脑本体。Hub 与电脑或 Root Hub 使用相同设备容器、上游端口被系统报告为不可移除或不可供用户连接，均属于电脑集成路径；其中经过当前占用、历史观察或明确伴随关系确认的用户可连接下行口以 `owner_kind=host` 发布，内部固定功能和未确认空逻辑端口不进入候选。

独立于电脑容器且由可移除属性、用户可连接上游或设备自带 ContainerId 确认的物理设备属于外接附件。外接附件只有同时具备外接性证据和拓展坞角色证据时才使用 `owner_kind=dock`：拓展坞角色证据来自设备类别明确声明，或同一物理设备容器内同时存在用户可连接 Hub 端口与网卡、显示、音频等非 Hub 功能。仅包含 Hub 功能或证据不足的附件使用 `owner_kind=hub`，显示为“USB 集线器”或“未确认 USB 扩展设备”，其端口和实时端点继续参与候选。

VID/PID、友好名称、端口数量、枚举顺序和当前电脑的位置路径均不得单独产生 `dock` 分类。版本化硬件配置只能在已经确认的外接附件中补充端口布局或内部功能，不能绕过外接性和拓展坞角色证据。没有可跨电脑使用的设备属性时，分类降级为未确认附件，而不是根据主机路径猜测。

`IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES` 的 `PortIsTypeC` 单独保存为 `connector_form_factor=type_c|type_a|unknown`，不得用 `connector_type=usb2|usb3` 代替插座形态。Type-C 保留在拓扑中表达上游关系；当前传感器使用的 USB-A 候选池排除 Type-C 和 `internal_function=dock_upstream`。

**替代方案：** 维护特定电脑或 VID/PID 黑名单。该方案更换电脑、主板或相同芯片应用形态后即失效，也会把普通 Hub 与拓展坞混为一类，因此不采用。

### 4. 拓扑数据与兼容的物理接口投影并存

`discover_interfaces()` 保留现有 `ports`、`physical_interfaces`、`defaults` 等字段，并新增版本化的 `usb_topology`：

```text
usb_topology
├─ schema_version
├─ state / provider / errors
├─ docks[]
│  └─ id, label, state, classification_evidence[], hub_refs[]
├─ hubs[]
│  └─ id, label, state, classification, classification_evidence[], hub_refs[]
├─ usb_ports[]
│  └─ id, owner_kind, dock_id, hub_id, system_port_number, connector_type,
│     connector_form_factor, supported_protocols[], state,
│     user_connectable, device_id
└─ devices[]
   └─ id, parent_port_id, class, friendly_name, vid, pid,
      serial, live_interface_id
```

`schema_version=2`。`docks[]` 只包含证据充分的拓展坞，`hubs[]` 包含普通外接 Hub 和未确认 USB 扩展设备；`owner_kind=host|dock|hub` 与 `dock_id`、`hub_id` 的引用必须一致。`physical_interfaces` 继续承载可被现有驱动打开的实时设备和网卡，并为能够定位的项目补充所属设备引用、`parent_port_id` 与系统生成的 `topology_label`。`usb_ports` 承载空端口和端口状态，不伪装为已验证设备。schema v1 结果只能兼容投影为未确认 USB 扩展设备，不继承其拓展坞结论。

**替代方案：** 用端口记录替换 `physical_interfaces`。这会让驱动失去 HID path、COM 和 UVC 端点并破坏旧 helper，故采用并行扩展契约。

### 5. USB 配置同时保存计划端口和实时设备

USB 类接口增加可选 `physical_port_id`，表示操作员选择的物理连接器；现有 `physical_interface_id` 继续表示可由协议驱动访问的实时设备。选择空端口时只设置前者，状态为 `port_selected_device_missing`。刷新发现兼容设备位于该端口后，系统设置实时设备绑定，但仍需原有连接与数据检查把 `physical_verified` 置为真。

“立即检查”继续承担原有驱动与数据诊断职责。除明确标记为错误协议临时回退的配置外，即使 `physical_verified=false`，检查也必须打开已配置驱动并逐项读取所选通道。若收到数据但实时设备尚未与所选物理端口关联，结果使用 `physical_unverified` 分别表达“驱动数据已读”和“端口绑定未验证”；检查不得据此自动改写端口绑定或解除真实采集门禁。

旧配置只有 `physical_interface_id` 时继续可用；如果拓扑能够解析其父级，则在内存配置和页面状态中补充 `physical_port_id`，不修改驱动、端点或通道映射。非 USB 接口维持现有设备级绑定。

重复绑定校验以实际资源为准：两个非共享逻辑接口不得选择同一 USB 物理端口；PLC 与 ABB 仍可共享同一以太网适配器。

**替代方案：** 把端口 ID 直接写入现有 `physical_interface_id`。这会混淆可打开设备与物理插孔并使现有驱动尝试打开无效端点，故不采用。

### 6. 前端使用物理传输族候选模型，不在 HTML 中复制拓扑推断

后端返回规范化拓扑，前端只执行展示和按逻辑类型筛选：

- 热电偶、UVC 和 M3232 共享 `usb_ports` 中全部用户可连接 USB-A 端口；标签包含电脑、拓展坞或 USB 扩展设备归属、系统端口号、连接器能力、状态和当前设备。Type-C 和拓展坞上游连接器保留在拓扑诊断中，但不进入这三个角色的 USB-A 候选池。
- HID、UVC 和 USB 转串口 COM 是物理 USB 端口下的实时通信端点。M3232 选择 USB 转串口时同时保存 `physical_port_id`、`physical_interface_id` 和 COM `endpoint`。
- 只有无法关联 USB 父级或 USB 位置路径的真正主机原生 COM 才在 M3232 下显示为独立串口候选。
- 以太网显示全部实时网卡；已知父级时显示“拓展坞网口”。
- 通过 `<optgroup>` 或等效结构按“电脑本机 USB-A”“拓展坞 USB”“USB 扩展设备”“位置未解析的 USB 设备”“主机原生串口”和“网卡”组织，保持键盘选择和现有表单行为。

前端不生成用户备注，不将 `USB3-1` 解释为物理左侧第一口。空闲和不兼容端口可作为计划位置选择，但启用真实采集前的校验显示具体阻断原因。真正的主机原生 COM 不进入 HID/UVC 候选列表。

### 10. 独立传输目录合并物理端口与实时端点

新增独立传输目录模块，输入 `usb_topology` 和原有 `physical_interfaces`，输出与传感器角色无关的物理候选目录。目录以 `transport_family=usb|serial_native|ethernet` 表示物理传输层，以 `endpoint_kind=hid|uvc|serial|network` 表示可由驱动打开的实时端点。

USB COM 优先通过 PnP 实例、父级链和 Location Path 定位物理端口；VID/PID/序列号只在结果唯一时回退。无法解析位置的原有 HID/UVC/USB 串口仍保留在“位置未解析的 USB 设备”组，避免拓扑增强删除旧候选。物理端口先按 `physical_port_id` 去重，未定位端点再按实时接口 ID 和 endpoint 去重。

拓扑提供器同时处理 Root Hub 和电脑集成 Hub 中系统报告为用户可连接且已被当前占用、历史观察或 USB2/SuperSpeed 伴随关系确认的端口。内部固定功能、未配对且未观察到的预留/未布线空逻辑端口和已确认不可供用户连接的端口不进入候选。外接普通 Hub、未确认 USB 扩展设备和已确认拓展坞的用户可连接 USB-A 端口均保留在候选池中，但分组和标签不得互相冒充。

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
- [Windows 没有覆盖全部厂商的统一拓展坞标志] → 组合设备容器、外接性、用户可连接上游、设备类别和多功能组成；证据不足时使用普通或未确认 USB 扩展设备分类，保持端口可用但不宣称为拓展坞。
- [更换电脑后主机控制器与 Location Path 改变] → 设备类别判断只使用设备自身容器、移除策略、功能组成和连接器关系；主机位置仅参与当前电脑上的端口身份，不参与拓展坞类别判定。
- [Root Hub 将预留或未布线端口报告为用户可连接] → 仅发布当前实际占用、本机历史观察确认或具有明确 USB2/SuperSpeed 伴随关系的 USB-A；通过稳定 ID 记录使已观察端口在拔出设备后仍保持可见。
- [Type-C 同样承载 USB 协议而被误计为 USB-A] → 分开保存协议能力与连接器形态，Type-C 只保留在拓扑诊断和上游关系中，不进入当前传感器的 USB-A 候选池。
- [拓展坞内部网卡端口缺少用户可连接标志] → 使用同一物理设备容器、父子关系和不可外接端口属性确认内部功能，并以三 USB 加一网口实物验收防止误隐藏。
- [无序列号拓展坞移动到另一上游端口后身份变化] → 使用位置相关 ID 并明确不跨位置继承；避免把另一台同型号拓展坞误认为原设备。
- [冻结运行时 ctypes 结构体布局错误] → 对齐 Windows SDK 结构定义，增加大小、偏移、二进制夹具和真实 Windows smoke test；API 失败时局部降级。
- [旧 helper 不理解新增字段] → 新字段只做向后兼容扩展；服务端检测 `schema_version`，旧 helper 仍返回现有扁平接口且页面标记拓扑不可用。
- [端口已选但设备尚未验证造成用户误解] → 页面分别显示端口状态、兼容设备状态和数据检查状态，真实采集门禁只接受最后两项均通过。
- [底层位置或设备标识暴露给非授权用户] → 页面使用规范化摘要 ID，访客模拟不返回拓扑，远程真实结果只来自已配对 helper。

## Migration Plan

1. 在拓扑提供器中读取设备容器、移除策略、设备类别和父子关系，并以不包含固定主机标识的夹具验证分类。
2. 将 `usb_topology` 升级为 schema v2，扩展 `discover_interfaces()`、传输目录和 helper 契约；schema v1 仅降级为未确认 USB 扩展设备。
3. 更新前端分组展示和端口预选，使普通 Hub 与未确认附件保持可用但不显示为拓展坞，并保持现有协议与数据门禁。
4. 在至少两组不同主机控制器、Root Hub 和 Location Path 夹具上验证分类一致，再使用目标拓展坞与无拓展坞电脑完成现场检查。
5. 同步并重建唯一 v2.0.4 发行目录，执行回归、哈希、驻留服务替换和 Git LFS 校验。

回滚时移除前端对 `usb_topology` 和 `physical_port_id` 的使用，保留后端新增字段不会影响旧消费者；如底层提供器有问题，可关闭拓扑提供器并继续使用现有扁平发现路径。
