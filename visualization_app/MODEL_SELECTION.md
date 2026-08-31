# 预测算法选择与权重匹配

软件只暴露小论文中实际使用过的预测模型：I-ModernTCN、TCN、Transformer、Informer、DeepVAR、DLinear、NLinear、Linear、MLP 和 FNN 对比模型。NHITS、GBRT 等未写入小论文的试验性模型不再出现在算法选择框中。

## 自动匹配规则

1. 算法选择框与数据模式（旧数据/新数据）共同决定权重搜索范围。
2. 软件先读取“上次使用该数据模式 + 算法”的权重位置；文件仍存在时优先恢复该位置。
3. 如果没有历史位置，则查找软件目录下的训练结果、随软件附带的 comparison checkpoint，以及源代码目录中的论文对比权重。
4. 新数据模式优先匹配 `new_collection_demo_v11_3/models` 和新数据训练输出；旧数据模式优先匹配 `models/<算法>/checkpoint.pth` 及旧数据训练输出。
5. 找不到匹配权重时不会静默切换到另一个算法，而是提示用户选择该算法的 checkpoint 或先训练该算法。

服务器端会把选择记录写入 `%LOCALAPPDATA%/AFP_State_Warning_System/model_selection_history.json`；网页端也会保存一份浏览器历史，因此重启软件后仍能恢复上次位置。

## 无现成权重的模型

训练页面对没有现成权重的论文模型采用统一默认值：`epoch=100`、`patience=10`。训练完成后生成的 `prediction_model_best.pth` 与同目录元数据会被自动纳入下一次算法匹配；元数据包含 `model_type`、输入/输出通道、序列长度和标准化参数，避免把不兼容的权重加载到当前数据模式。

## 运行约束

所有算法统一遵守 `[batch, history, channels] -> [batch, horizon, channels]` 接口。算法切换后会重新加载模型并重新校验输入/输出传感器；健康指标和窗口—层—试样聚合流程保持不变。

## ATAVN 重训版本

All 22 runtime checkpoints (11 thesis algorithms x legacy/new schema) were
retrained with Adaptive Terminal-Aligned Variance Normalization (ATAVN).
For comparison models, the last observation in each input window is retained
as the terminal baseline, the window is divided by its unbiased temporal
standard deviation with numerical smoothing, the network predicts normalized
residuals, and the output is restored to standardized absolute values. The
legacy data contain near-constant stretches, so a minimum normalized scale of
0.1 is recorded in the metadata to prevent unbounded residual targets. The
I-ModernTCN-GAT implementation already contains the same closed-loop
normalization internally and is marked `mode=native`; it is not normalized a
second time. Each `checkpoint.pth.json` contains the `atavn` object, version,
mode, epsilon, and minimum scale used during training.
