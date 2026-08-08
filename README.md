# AFP 实时预测与状态预警系统

该仓库包含自动铺丝（AFP）系统的实时采集、I-ModernTCN 多变量预测、在线健康指标计算，以及“窗口级 → 铺层级 → 五层试样级”的状态预警界面源码。

## 使用方式

普通 Windows 使用者应从项目的 **Releases** 下载 `AFP_State_Warning_System_Windows.zip`，解压后运行 `AFP_State_Warning_System.exe`。该软件包包含运行时、默认预测模型和演示数据，无需安装 Python，也不依赖开发电脑上的工程路径。

源码开发可在 Python 3.11 环境中执行：

```powershell
cd visualization_app
python -m pip install -r requirements.txt
python app.py
```

随后打开 `http://127.0.0.1:8765/`。执行 `python test_app.py` 可进行基本功能测试。

## 功能范围

- 显示全部已选择传感器的采集值、未来预测值与预测/真实对齐结果；
- 支持仅采集、演示回放和实时预测；
- 支持选择输入传感器、预测输出、未来步长及健康指标/异常分数模型；
- 采用 TC-HI、T-HI、C-HI、RFHI、PR-HI 等健康指标，并给出推荐模型；
- 以窗口、铺层和五层试样三级展示预警证据；
- 采集数据按“保存位置/试样名+工艺参数/试样名+工艺参数组合+铺层数”保存，同时保留完整试样与单层文件；
- 对接 JSON Lines 传感器流，并显示连接和数据接收状态。

## 数据和模型边界

原始采集数据、实验结果、训练检查点和个人保存路径不随源码仓库公开。发布包中仅包含运行演示所需的运行时副本。实际生产部署前应以已校准的传感器、经验证的模型和真实缺陷/性能证据重新确认阈值与适用范围。

## 打包

在具备运行资产的开发工作区中执行：

```powershell
cd visualization_app
powershell -ExecutionPolicy Bypass -File .\build_desktop_app.ps1
```

输出位于 `visualization_app/release/AFP_State_Warning_System`。打包脚本会复制用于 I-ModernTCN 推理的运行代码、检查点、健康指标工件和演示数据；源码仓库的 `.gitignore` 会排除这些大体积运行资产。
