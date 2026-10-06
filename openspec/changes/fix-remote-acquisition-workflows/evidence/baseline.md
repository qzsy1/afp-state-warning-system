# 修复前基线（2026-09-21）

## Git 状态

- 分支：`feature/original-ui-langchain-agent`
- 跟踪文件无未提交修改。
- 修复前未跟踪目录：`.agents/`、`openspec/`；它们属于规范与本地工作流文件，不计入既有业务代码改动。

## 二进制与关键文件 SHA-256

| 文件 | SHA-256 | 字节数 |
|---|---|---:|
| `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/AFP_Integrated_System_Modular.exe` | `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65` | 56032416 |
| `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/local_helper/AFP_Local_Capture_Helper.exe` | `845DCA5CDD37332D8E44F818B4FB65500C5E0A08AA12F5CFE8C5C51009D1EE9E` | 91613768 |
| `visualization_app/local_capture_helper_entry.py` | `686D6EEE9BCE734E9D767D1CA58481429332931E96283F3503FE05E543AB18B5` | 27936 |
| `visualization_app/app.py` | `37368A696E5E3CF8CA6747358C66EFB1094791C8E0B15E05CE73630BBAC1BDF0` | 250098 |
| `visualization_app/static/app.js` | `52F19A3F05F0F5F1D325C31AAACBA80361C220B02A7FC5C296B5ED7706F1CB09` | 240040 |
| `visualization_app/agentic_diagnosis.py` | `3CA8DE1D4D5870939D1AF3F4207EC128258FBE160EAF47D6281BBC52B8FB759F` | 52698 |
| `visualization_app/diagnosis_jobs.py` | `B8AF0F3B1286F4B5F3ECB293E079967FD6024B1D3B4996F4B3D5D6B03BF57B65` | 4482 |

## 自动测试基线

命令：`py -3.11 -m unittest discover -s visualization_app -p "test_*.py" -q`

- 运行 315 个测试，修复前为 `1 failure, 42 errors`。
- 主要环境错误：系统 Python 3.11 未安装 `torch`，导致依赖 `app.py` 的 29 个左右用例无法导入；交付包中的 PyInstaller 私有模块不能作为普通系统 Python 环境直接复用。
- 其余既有错误：12 个新健康指标用例异常、Windows DPAPI 基线用例异常。
- 既有失败：`test_original_frontend_contains_agent_additions`。
- 本变更新增测试必须单独经历红/绿验证；最终全量报告须保留上述基线分类，不得将其误报为本变更回归。
