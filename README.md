# AFP 瀹炴椂棰勬祴涓庣姸鎬侀璀︾郴缁?

## 当前公开版本：包含模型训练过程

本仓库当前版本已包含完整的模型训练链路，不仅是采集、I-ModernTCN 预测和三级状态预警演示。训练中心支持导入采集格式的 CSV 文件夹或 MySQL 数据，统一整理数据，配置训练参数，实时显示 epoch/patience 进度，停止训练并将预测模型和预警模型保存到指定位置。原生 Windows 软件也已将训练中心与实时采集、预测和预警界面合并，启动软件后可从顶部的“模型训练中心”进入。

Windows 发布包请从仓库 Releases 下载，软件包名称为 `AFP_Integrated_Native_System_Windows_20260814_0020.zip`。解压后保持 `AFP_Integrated_System.exe` 与 `_internal` 文件夹在同一目录。

源码入口：

- `visualization_app/native_frontend_launcher.py`：原生窗口入口，嵌入原三栏监测前端；
- `visualization_app/static/training.html`、`training.js`：模型训练页面；
- `visualization_app/web_training.py`、`web_training_pipeline.py`：数据导入、整理、预测训练和预警训练链路；
- `visualization_app/build_native_integrated_app.ps1`：包含训练过程的原生 Windows 打包脚本。

璇ヤ粨搴撳寘鍚嚜鍔ㄩ摵涓濓紙AFP锛夌郴缁熺殑瀹炴椂閲囬泦銆両-ModernTCN 澶氬彉閲忛娴嬨€佸湪绾垮仴搴锋寚鏍囪绠楋紝浠ュ強鈥滅獥鍙ｇ骇 鈫?閾哄眰绾?鈫?瀹為檯閾哄眰鏁拌瘯鏍风骇鈥濈殑鐘舵€侀璀︾晫闈㈡簮鐮併€?

## 浣跨敤鏂瑰紡

鏅€?Windows 浣跨敤鑰呭簲浠庨」鐩殑 **Releases** 涓嬭浇 `AFP_State_Warning_System_Windows.zip`锛岃В鍘嬪悗杩愯 `AFP_State_Warning_System.exe`銆傝杞欢鍖呭寘鍚繍琛屾椂銆侀粯璁ら娴嬫ā鍨嬪拰婕旂ず鏁版嵁锛屾棤闇€瀹夎 Python锛屼篃涓嶄緷璧栧紑鍙戠數鑴戜笂鐨勫伐绋嬭矾寰勩€?

婧愮爜寮€鍙戝彲鍦?Python 3.11 鐜涓墽琛岋細

```powershell
cd visualization_app
python -m pip install -r requirements.txt
python app.py
```

闅忓悗鎵撳紑 `http://127.0.0.1:8765/`銆傛墽琛?`python test_app.py` 鍙繘琛屽熀鏈姛鑳芥祴璇曘€?

## 鍔熻兘鑼冨洿

- 鏄剧ず鍏ㄩ儴宸查€夋嫨浼犳劅鍣ㄧ殑閲囬泦鍊笺€佹湭鏉ラ娴嬪€间笌棰勬祴/鐪熷疄瀵归綈缁撴灉锛?
- 鏀寔浠呴噰闆嗐€佹紨绀哄洖鏀惧拰瀹炴椂棰勬祴锛?
- 鏀寔閫夋嫨杈撳叆浼犳劅鍣ㄣ€侀娴嬭緭鍑恒€佹湭鏉ユ闀垮強鍋ュ悍鎸囨爣/寮傚父鍒嗘暟妯″瀷锛?
- 閲囩敤 TC-HI銆乀-HI銆丆-HI銆丷FHI銆丳R-HI 绛夊仴搴锋寚鏍囷紝骞剁粰鍑烘帹鑽愭ā鍨嬶紱
- 浠ョ獥鍙ｃ€侀摵灞傚拰璇曟牱涓夌骇灞曠ず棰勮璇佹嵁锛岃瘯鏍风骇鎸夊疄闄呭凡缁忛噰闆嗙殑閾哄眰鏁板姩鎬佽仛鍚堬紱
- 閲囬泦鏁版嵁鎸夆€滀繚瀛樹綅缃?璇曟牱鍚?宸ヨ壓鍙傛暟/璇曟牱鍚?宸ヨ壓鍙傛暟缁勫悎+閾哄眰鏁扳€濅繚瀛橈紝鍚屾椂淇濈暀瀹屾暣璇曟牱涓庡崟灞傛枃浠讹紱
- 瀵规帴 JSON Lines 浼犳劅鍣ㄦ祦锛屽苟鏄剧ず杩炴帴鍜屾暟鎹帴鏀剁姸鎬侊紱
- 鏈湴鏂囦欢淇濆瓨鎴愬姛鍚庯紝鍙寜灞備簨鍔″悓姝ヨ嚦鎸囧畾 MySQL 鏁版嵁搴擄紝骞堕€氳繃鍏崇郴瑙嗗浘鏌ョ湅宸ュ喌鈥旇瘯鏍封€旂嫭绔嬮噸澶嶁€旈摵灞傚叧绯汇€?

## 鏁版嵁鍜屾ā鍨嬭竟鐣?

鍘熷閲囬泦鏁版嵁銆佸疄楠岀粨鏋溿€佽缁冩鏌ョ偣鍜屼釜浜轰繚瀛樿矾寰勪笉闅忔簮鐮佷粨搴撳叕寮€銆傚彂甯冨寘涓粎鍖呭惈杩愯婕旂ず鎵€闇€鐨勮繍琛屾椂鍓湰銆傚疄闄呯敓浜ч儴缃插墠搴斾互宸叉牎鍑嗙殑浼犳劅鍣ㄣ€佺粡楠岃瘉鐨勬ā鍨嬪拰鐪熷疄缂洪櫡/鎬ц兘璇佹嵁閲嶆柊纭闃堝€间笌閫傜敤鑼冨洿銆?

## 鎵撳寘

鍦ㄥ叿澶囪繍琛岃祫浜х殑寮€鍙戝伐浣滃尯涓墽琛岋細

```powershell
cd visualization_app
powershell -ExecutionPolicy Bypass -File .\build_desktop_app.ps1
```

杈撳嚭浣嶄簬 `visualization_app/release/AFP_State_Warning_System`銆傛墦鍖呰剼鏈細澶嶅埗鐢ㄤ簬 I-ModernTCN 鎺ㄧ悊鐨勮繍琛屼唬鐮併€佹鏌ョ偣銆佸仴搴锋寚鏍囧伐浠跺拰婕旂ず鏁版嵁锛涙簮鐮佷粨搴撶殑 `.gitignore` 浼氭帓闄よ繖浜涘ぇ浣撶Н杩愯璧勪骇銆?

## 绯荤粺浠嬬粛鏂囨。

涓巚1.11.0瀹炵幇瀵瑰簲鐨勮鏂囧熀纭€绔犺妭浣嶄簬锛?

`thesis_draft/AFP瀹炴椂棰勬祴涓庝笁绾х姸鎬侀璀︾郴缁熸牳蹇冩祦绋嬭鏄巁杞欢鏁版嵁搴撻泦鎴愭洿鏂扮増_v5.docx`

鍏朵腑璇存槑浜?6涓疄闄呴噰闆嗛€氶亾銆佺洰鏍囨椂鍒荤粦瀹氶娴嬨€佺獥鍙ｂ€旈摵灞傗€斿疄闄呴摵灞傛暟璇曟牱涓夌骇CAP銆佷换鎰忛摵灞?鐙珛閲嶅銆佹湰鍦版枃浠朵紭鍏堜繚瀛樸€丮ySQL澶栭敭/绱㈠紩/鍏崇郴瑙嗗浘鍜岃蒋浠堕獙璇佽竟鐣屻€?
