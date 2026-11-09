# ESP32-C5 5 GHz CSI / RSSI 實驗專案

兩台 ESP32-C5 透過 **5 GHz HE20 SU ESP-NOW** 建立直接感測路徑，連接電腦的 **5 GHz Wi-Fi 熱點**，接收端再以 **UDP** 傳送原始 CSI 與同封包 RSSI 到電腦視窗。電腦端顯示 CSI 熱圖、振幅、RSSI、動作變化與取樣間隔。呼吸計算與介面暫時停用，原始模組保留供之後恢復。

本專案用於觀察、錄製、重播動作造成的訊號變化，目前只判斷 **活動／靜止**。**初始模型未訓練，完成一次靜止背景訓練即可使用**；可分別驗證靜止和活動，不需走道或兩側模板。靜止不代表無人，本版不判定有人／無人。模擬模式只測軟體，不代表硬體實驗效果。

活動／靜止訓練與驗證操作見 [MOTION_TRAINING.md](MOTION_TRAINING.md)。這次辨識功能只修改電腦端，重新開啟視窗即可使用，不需要重新燒錄。

## 1. 設備與預設設定

| 項目 | 設定 |
|---|---|
| 發送板 hostname | `esp32c5-1` |
| 接收板 hostname | `esp32c5-2` |
| 發送板本地管理 MAC | `02:45:53:4f:c5:01` |
| 接收板本地管理 MAC | `02:45:53:4f:c5:02` |
| 熱點 SSID | `ESOCESP32` |
| 熱點密碼 | `ESOCESP32` |
| 頻段 | **5 GHz only**，不自動退回 2.4 GHz |
| 探測封包 | ESP-NOW 廣播、802.11ax HE20 SU、MCS0、long GI |
| 目標發送率 | 500 Hz（每 2 ms 一次發送機會；NVS 保存值可覆寫） |
| UDP 目的地 | DHCP gateway，通常是開熱點的電腦 |
| UDP 埠 | `5500` |
| 發送率控制埠 | `5501`（電腦傳至 esp32c5-1） |
| 電腦接收介面 | `0.0.0.0:5500` |
| 韌體環境 | ESP-IDF **6.0.x**；本機為 **6.0.2** |

名稱會寫入 DHCP hostname、開機日誌及 UDP 狀態／CSI 封包。Windows 熱點介面是否顯示 hostname 取決於系統；不保證可用名稱解析 IP。本版未提供 mDNS。

兩台使用不同 MAC 與不同角色韌體。同一熱點上請只放一組使用這些固定 MAC 的設備。

```text
esp32c5-1 ── 5 GHz ESP-NOW 探測封包 ──> esp32c5-2
     │                                     │
     │  Wi-Fi 連線＋低頻 UDP 狀態           │  Wi-Fi / UDP CSI＋RSSI
     └──────────────────┬──────────────────┘
                        ▼
             電腦 5 GHz 熱點 ESOCESP32
                        ▼
              Python 即時視窗 UDP :5500
```

感測路徑是板 1 → 板 2，不是電腦熱點 → 板 2。板 2 依來源 MAC、廣播目的 MAC、action frame、HT 格式與 CSI valid flag 篩選，避免混入板 1 上傳到熱點的資料。

## 2. 開啟 Windows 5 GHz 熱點

1. 在「設定 → 網路和網際網路 → 行動熱點」選擇透過 Wi-Fi 共用。
2. 編輯熱點：名稱 `ESOCESP32`、密碼 `ESOCESP32`、網路頻帶選 **5 GHz**。
   若可設定 Wi-Fi 6／802.11ax，請啟用；HE20 SU 探測封包需兩台 C5 都支援並使用 11ax PHY。
3. 開啟行動熱點。實驗期間關閉熱點自動省電／無裝置自動關閉選項（若系統有提供）。
4. 若沒有 5 GHz 選項，先處理電腦網卡、驅動及熱點能力；此韌體不會連接 2.4 GHz 熱點。

兩台板會跟隨 AP 的頻道，程式不硬鎖 channel 36。Wi-Fi 頻道必須符合 AP、板子國碼與當地允許範圍；本程式不覆寫法規國碼。若電腦能選擇，先用當地允許的非 DFS 頻道。Windows 行動熱點常無法手動選頻道。

SSID／密碼相同不代表一定是 5 GHz。確認日誌及視窗 `channel > 14`，兩板的頻道必須相同。

## 3. 燒錄韌體

### 前置準備

- 兩台 ESP32-C5、可傳資料的 USB 線、適用 5 GHz 的板載／外接天線。
- 安裝 ESP-IDF 6.0.x 與 ESP32-C5 工具鏈。本專案固定增益元件 `espressif/esp_csi_gain_ctrl 0.1.4`，第一次編譯需要網路下載相依元件。
- 在 PowerShell 切到專案目錄：

```powershell
cd C:\Users\howar\Documents\code\espcsi_main
```

本機工具啟動腳本為 `C:\Espressif\tools\Microsoft.v6.0.2.PowerShell_profile.ps1`。若你換了 IDF 安裝位置，可在 ESP-IDF PowerShell 執行，或向下列腳本傳入 `-IdfActivation '你的啟動腳本.ps1'`。

### 自動選擇 COM 埠

燒錄不需要填 `-Port`：腳本會列舉 USB 序列埠並排除藍牙虛擬 COM 埠。只找到一台時自動選取；多台時顯示 COM、USB 類型及序號供選擇；沒有設備時可插入後按 Enter 重新掃描。

**最簡單的方式是一次只插入要燒錄的那台 ESP32。** USB 資訊可辨認 Espressif 或一般 USB-UART 裝置，但無法推斷你想讓哪台成為 1 或 2，也不能保證一般 USB-UART 後面一定是 ESP32。若有其他 USB 串口設備，先拔掉或在選單選擇正確設備。

只查看設備、不編譯也不燒錄：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1 -ListPorts
```

仍可用 `-Port COM3` 手動指定已知連接埠。COM 編號可能隨 USB 插槽或開機模式改變，不要永久假定某個號碼。

### 第一台：esp32c5-1（TX）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1 -Node 1 -Flash -Monitor
```

### 第二台：esp32c5-2（RX）

先退出第一個 monitor（`Ctrl+]`），拔掉第一台的 USB，插入第二台：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1 -Node 2 -Flash -Monitor
```

`-ExecutionPolicy Bypass` 只用於這次 PowerShell 程序，不修改系統永久執行原則。

腳本會分別使用 `build-esp32c5-1`／`build-esp32c5-2` 及獨立 sdkconfig，避免兩個角色相互覆蓋。省略 `-Flash -Monitor` 就只編譯。腳本停用 ccache，避免本機不同執行帳號共用快取產生鎖定。

若卡在 `Connecting...`，依開發板操作：按住 BOOT，短按 RESET／EN，開始連線後放開 BOOT。確認 USB 線可傳資料、選對 USB 接口，並關閉占用 COM 的其他 monitor。燒錄後可能需要按 RESET 或重接電源。不同板子的原生 USB 與 USB-UART COM 可能不同。

預設使用 IDF 的 UART console；原生 USB 燒錄可用但不一定能看到 UART 日誌。若板子只用 USB Serial/JTAG，請在 `menuconfig → Component config → ESP System Settings → Channel for console output` 選 USB Serial/JTAG 後重編譯。UDP 功能不依賴 console。

### 更改 IP／熱點設定

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1 -Node 2 -Menuconfig
```

進入 **ESOC CSI experiment**。一般不需填 UDP IP；若 gateway 不是電腦接收介面的 IP，填入該 IP。用 `ipconfig` 查找熱點虛擬網卡的 IPv4，不能誤填上網網卡的 IP。

不要在 Node 2 的 menuconfig 裡改成 Node 1；角色由兩份設定管理。修改共同 Wi-Fi 設定時，兩個 Node 都要修改並重新燒錄。修改 node1/node2 defaults 不會覆寫已存在的 sdkconfig；請使用 menuconfig 更新現有設定。

發送率可在電腦介面直接更改，不需再進入 menuconfig。新版首次開機預設為 500 Hz；既有 sdkconfig 或 NVS 保存值可能不同，以板端回報為準。

### 500 Hz 收樣與速率診斷

新版發射端使用 `esp_timer` 每 2 ms 喚醒發送工作，不在計時器 callback 中傳送；仍等待上一包完成，延遲時合併喚醒，不補送一串突發封包。接收端 CSI queue 預設 128 筆（500 Hz 下約 256 ms），可在 menuconfig 的 `CSI sample queue depth` 調整。電腦短窗變異改用增量統計，保留原有特徵定義。

更新時分別燒錄兩端，再重新開啟接收視窗，選 500 Hz、勾「記住」並按「套用」。這樣既有 NVS 的開機速率也會更新。視窗顯示近 1 秒筆數與近 5 秒平均，並依兩板 status 計數差顯示實際發送／擷取速率。新版 status 使用板端 uptime 作為計時基準；舊版則以 status 到達間隔估算。

即時接收筆數以 socket 收包時間計算，不再把 UI 批次處理時間當成收包時間。5 秒平均只統計實際收到且成功處理的 CSI，不填補遺失樣本；長時間無封包時會降為零。

若 500 Hz 仍不穩，先關閉接收視窗，再用無 GUI 診斷收集 15 秒：

```powershell
.\.venv\Scripts\python.exe .\scripts\check_rate.py --seconds 15
```

| 觀察 | 可能瓶頸／下一步 |
|---|---|
| TX 實際發送低、`busy_skips_delta` 上升 | 上一包未送完；檢查無線負載與同頻干擾 |
| TX 實際發送低、`schedule_skips_delta` 上升 | 排程延遲；保存診斷結果檢查板端工作負載 |
| TX 接近 500，但 RX 擷取較低 | 無線接收或接收篩選路徑；檢查天線、兩板同頻、RSSI、格式與環境干擾 |
| RX 擷取接近 500，但 `queue_drops_delta` 上升 | RX 傳輸速度不足；檢查 UDP／熱點負載，queue 只能吸收短暫延遲 |
| RX 擷取接近 500、PC 較低 | 檢查 UDP errors、接收後缺包、PC queue drops 與防火牆 |
| 無 GUI 測量正常、視窗不順 | 電腦顯示／處理負載；使用新版視窗與近 5 秒平均對照 |

`500 Hz` 是發送機會的目標，不保證每秒恰好收到 500 筆。Wi-Fi／ESP-NOW 與 UDP 不提供固定延遲、無丟包保證；只有實機診斷能確認目前擺位與熱點的持續收樣率。

## 4. 開啟 UDP 接收器

### 第一次安裝 Python 套件

使用 Python 3.11 或相容版本，在專案目錄執行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\host\requirements.txt
```

本次實作已在這個工作目錄建立 `.venv` 並安裝套件。一般使用不需重裝，也不需啟動 virtualenv。

### 先看模擬視窗（不需板子）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\receiver.ps1 -Demo
```

前 10 秒校正，第 12 秒後會出現人工合成的週期變化。介面會顯示「模擬資料」，錄製也會保存此來源標記。

### 實際接收 UDP

先開熱點與視窗，再開兩板電源：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\receiver.ps1
```

等效的 Python 指令：

```powershell
.\.venv\Scripts\python.exe .\host\receiver.py --bind 0.0.0.0 --port 5500
```

板子 USB 可僅供電；資料由 Wi-Fi UDP 傳回。兩台設備每秒回報狀態，即使還沒有 CSI，也能辨認板子是否連線。只有一台或兩板頻道不一致，無法完成此雙板感測。

### Windows 防火牆

如果 Windows 詢問是否允許 Python 接收網路連線，允許目前熱點使用的網路設定檔。不要關閉整個防火牆。

若沒有提示且 UDP 收不到，可在系統管理員 PowerShell 建立限定程式、UDP 5500、LocalSubnet 的規則（在專案目錄執行）：

```powershell
$csiPython = (Resolve-Path .\.venv\Scripts\python.exe).Path
New-NetFirewallRule -DisplayName 'ESOC CSI UDP 5500' -Direction Inbound -Action Allow -Protocol UDP -LocalPort 5500 -Program $csiPython -RemoteAddress LocalSubnet -Profile Any
```

不再使用時可移除：

```powershell
Remove-NetFirewallRule -DisplayName 'ESOC CSI UDP 5500'
```

本次實作沒有修改電腦熱點或防火牆。

## 5. 視窗與錄製操作

| 控制／顯示 | 說明 |
|---|---|
| 靜止校正 | 重新採集 10 秒背景；過程中人、板子及天線不要移動 |
| 振幅正規化 | 將每筆增益補償振幅除以其有效 bins 中位數；可比較頻譜形狀變化，但也會移除整體振幅變化，切換後重新校正 |
| 發送率／套用 | 選擇 50、100、200、250、500 或 1000 Hz，傳送至 esp32c5-1；收到板子確認後才顯示「已套用」 |
| 記住 | 勾選後把這次頻率設為板子下次開機值；未勾選時重開機回到先前保存值 |
| 開始錄製 | 保存原始 UDP 到 `captures/csi-時間.jsonl`，並自動重新校正，使錄影開頭有基線 |
| 停止錄製 | 完成並關閉檔案；關閉視窗也會結束錄製 |
| 標記動作 | 輸入文字後按下按鈕；RSSI／動作圖加垂直線，錄製中會保存標記 |
| CSI 熱圖 | 校正前顯示增益補償振幅；校正後顯示相對基線偏差，色階固定 -8 到 +8 |
| 振幅圖 | 原始平均振幅、增益補償平均振幅、50 ms EMA 與 Kalman，可直接比較延遲和抑噪效果 |
| Kalman Q／R | `Q` 是每秒程序雜訊，越大越快跟隨變化；`R` 是量測雜訊，越大越平滑。兩者單位皆為平均振幅平方，預設均為 25 |
| RSSI 圖 | 同一 A → B 探測封包的 RSSI dBm，不是熱點 RSSI |
| 動作變化 | 基線偏差、0.5 秒窗內變異、相鄰樣本差分 |
| 取樣間隔 | 接收板 callback 捕捉時間差（ms），不是電腦 UDP 到達間隔 |
| 詳細資訊 | 展開 AGC、FFT、增益補償、設備計數、丟包與錯誤資訊 |

熱圖每 40 ms 顯示一筆，曲線／特徵計算及錄製仍使用每筆接收到的資料。無資料時間槽留白，不以插值假裝連續。圖的橫軸來自接收板 uptime；校正、換頻道、重開機或格式變化會重設顯示時間與基線。

「每秒筆數」是最近一秒送進 UI 的有效樣本數，UDP 暫時批次到達時可能浮動；請連同取樣間隔判讀。介面採繁體中文，完整算法與指標說明保留在本文件。

振幅圖的「增益補償」是未經時間濾波的共同輸入；EMA 與 Kalman 都由它計算。Kalman 採一維 random-walk 模型，依每筆 ESP32 時間戳調整預測共變異。調整 Q 或 R 時會清除畫面上的舊曲線並重建 Kalman 狀態，但不會重做 CSI 靜止校正。熱圖與動作變化仍使用未經 Kalman 濾波的逐子載波資料，讓濾波比較不改變原有特徵基準。

### 從電腦更改發送率

1. 等待介面顯示 `esp32c5-1：已連線`。
2. 選擇發送率後按「套用」。
3. 看到「已套用 xxx Hz」才表示板子接受設定。
4. 若希望斷電後仍使用此值，套用前勾選「記住」。只有勾選時才寫入板子的 NVS。

此功能需要先把新版韌體燒錄到 `esp32c5-1` 一次。`esp32c5-2` 的 CSI 格式沒有改變，可沿用既有韌體；為保持版本一致也可一併更新。之後調整頻率不需 USB 或重新燒錄。切換頻率不會自動重做 CSI 靜止校正。

每個週期若上一次 ESP-NOW 傳送尚未完成就會跳過，不堆積要求，因此設定值是目標發送率，實際 CSI 收樣率可能較低。先以 500 Hz 觀察每秒筆數、取樣間隔與缺包；若接收不穩，依速率診斷結果調整。490-byte HE20 CSI 使 500 Hz 的應用資料約為 2.256 Mbit/s。

### 指定檔名錄製

```powershell
.\.venv\Scripts\python.exe .\host\receiver.py --record .\captures\wave-01.jsonl
```

為避免覆蓋實驗，已存在的檔名會被拒絕。錄製包含完整二進位 datagram 的 Base64、電腦接收時間、來源 IP、模式、設定與標記。JSONL 並非只儲存平滑後曲線。
先前 HT20 的 114-byte 錄製檔不符合本版 HE20 SU 解析器，請使用新韌體重新錄製；不要把舊檔誤當成 HE20 資料。

### 重播

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\receiver.ps1 -Replay .\captures\wave-01.jsonl
```

或：

```powershell
.\.venv\Scripts\python.exe .\host\receiver.py --replay .\captures\wave-01.jsonl
```

依錄製到達時間重播，不需開熱點或連板子，會重新計算特徵並還原設定變更與動作標記。錄製前的舊基線不會寫入檔案，所以開始錄製會重新校正。錄製結束可能有少量已收到但尚未處理的封包，關閉時會排空佇列並保存。

## 6. 實验方法與算法

### 呼吸功能暫停

目前 `Processor` 不執行呼吸濾波或頻譜搜尋，視窗也不建立呼吸圖表或顯示呼吸候選值。`host/respiration.py` 與原有模組測試保留供之後恢復。這次只修改電腦端，重新開啟接收視窗即可套用，不需要重新燒錄。

### 建議第一輪

1. 兩板固定在桌面／腳架，初始間距約 1–2 公尺，天線朝向固定；距離是起始實驗值，不是有效距離保證。
2. 確認兩板 online、相同 5 GHz 頻道、接收率穩定，沒有持續增加的丟棄／錯誤。
3. 開始錄製並保持靜止完成 10 秒校正，再靜止觀察 20 秒。
4. 標記「揮手」，在兩板之間做動作 5 秒，再靜止 5–10 秒，至少重複 5 次。
5. 先確認大動作穩定可見，再測敲擊、手指動作；每次只改一個因素（擺位、距離或動作）。
6. 如果振幅一起跳，先看 AGC／FFT；若 CSI 與 RSSI 反應不同，也保留結果，不強迫兩者一致。
7. 改變板子／天線／環境位置之後，重新校正。

### CSI 格式與前處理

- 僅接受 HE20 SU 的 HE-LTF；接收端篩選來自板 1 的 ESP-NOW action frame。
- 依 IDF C5 文件，HE20 SU 非 STBC 為 **490 bytes = 245 個複數值**，排列 `0..122,-122..-1`，每組為 signed int8 虛部、實部。
- 接收器拒絕其他長度，避免未辨認的格式進入算法。日後要支援其他 PHY，必須一起新增格式描述、解析與子載波遮罩。
- 排除 `-1、0、+1` 子載波（陣列 index `244、0、1`）；`first_word_invalid` 為真時排除前 4 bytes，但不移動其他 bin 的索引。
- 振幅 `A = sqrt(real² + imag²)`。使用 float 計算，避免 int8 平方溢位。
- 韌體先記錄 100 筆增益建立官方元件的增益基線，再取得每包 AGC／FFT 與補償因子；原始 CSI 不改寫。補償尚未就緒時明確標為 warming up，進入可補償狀態時 PC 重新校正。
- 本版不固定 AGC，也不把未校正相位用於動作判斷。

### 背景與特徵定義

對每個有效 bin 的 10 秒校正資料，使用中位數 `b[k]` 為背景。尺度：

```text
s[k] = max(1.4826 × median(abs(A[k] - b[k])), 0.03 × abs(b[k]), 1e-6)
z[k,t] = (A[k,t] - b[k]) / s[k]
baseline deviation = sqrt(mean(z[k,t]^2))
0.5 s variation    = sqrt(mean_over_bins(var_over_time(z[k,t])))
adjacent difference = sqrt(mean(((A[k,t] - A[k,t-1]) / s[k])^2))
```

以上 A 使用增益補償振幅；開啟 shape normalization 時再除以該封包的有效振幅中位數。基線中不完整、為零的 bins 不參與分數。

超過 250 ms 的資料間隙不計相鄰差分，並清空短窗；尚在校正時重新累積連續背景。接收時間來自 `esp_timer_get_time()` 在 CSI callback 的擷取時間；另保留 Wi-Fi 硬體 32-bit timestamp 供之後研究，不直接拿未展開回繞的值畫長時間曲線。

EMA 只用於平均振幅的顯示，時間常數 50 ms；不拿它覆蓋原始資料。基線 MAD 對少量離群值較穩健，但目前不自動刪掉疑似動作尖峰，以免把敲擊特徵消掉。本版未加入 PCA、相位校正或模型訓練。

特徵是否有用，要比較多次動作與靜止的分布、重複性及背景誤觸發，不能只看單次漂亮曲線。分數也可能因靜態擺位改变而升高，不等同持續運動。

### 丟包數字的意思

- 「接收後缺包」：接收板 CSI callback 序號之缺口，包含韌體 queue／UDP／PC 路徑遺失；**不包含未被板 2 收到的空中封包**。
- `queue drop`：韌體 CSI 佇列滿。callback 不等待 UDP，資料會明確丟棄並計數。
- `UDP errors`：板子 `sendto` 失敗；sendto 成功不代表電腦確實收到。
- `PC queue drops`：電腦收包執行緒到 UI 的有界佇列滿。
- 「晚到或重複」：晚到或重複的 CSI 序號，保留原始錄製但不納入即時算法。
- TX `sent`：成功提交給 ESP-NOW 的探測封包數；`send_errors` 包含提交／傳送 callback 失敗。廣播成功不是板 2 的接收確認。
- 不應把 TX sent 與 RX captured 直接當精確空中遺失率：兩板計數起點不同，且本版沒有把 probe application sequence 與 CSI 做嚴格關聯。

## 7. UDP 協定與專案結構

每個 CSI datagram 為 **74-byte little-endian header + 原始 CSI bytes**；Python 定義在 `host/protocol.py` 的 `HEADER`。韌體 CSI 緩衝上限為 512 bytes，低於一般 IPv4 1500 MTU 的 UDP payload 上限；目前 HE20 SU 資料為 564 bytes（74 + 490）。

Header 依序為：magic `CSI5`、version、node、flags、boot ID、capture sequence、64-bit callback timestamp、32-bit hardware timestamp、queue drops、UDP errors、RSSI、noise floor、channel、RX format、AGC、FFT gain、802.11 rx sequence、CSI length、reserved、float gain factor、source MAC、16-byte hostname。

flags bit 0 表示前四 bytes 無效、bit 1 表示增益補償可用。板子每次開機產生 boot ID，電腦可辨認序號重新開始。每秒 JSON status 與 binary CSI 共用 UDP 5500；電腦向板 1 的 5501 埠傳送固定 12-byte 發送率命令，板 1 回覆帶 token 的確認。UDP 使用系統 checksum，格式／長度／來源識別會在應用層再次驗證；這不是加密或具認證的協定，只用於受控熱點實驗。

```text
firmware/
  main/main.c              Wi-Fi、ESP-NOW、CSI callback、queue、UDP
  main/Kconfig.projbuild   可調設定
  main/idf_component.yml  IDF / 增益元件相依
  dependencies.lock       已解析的元件版本
  sdkconfig.defaults      共用預設
  sdkconfig.node1/2        角色預設
host/
  motion.py                背景門檻、區域模板、拒判與獨立驗證
  motion_store.py          模型與振幅基線保存、設備條件核對
  receiver.py             Qt 即時視窗
  protocol.py             二進位解析與序號追蹤
  processing.py           基線、增益補償與變化特徵
  stream.py               UDP、模擬、錄製、重播
  requirements.txt
scripts/build.ps1         編譯／燒錄／monitor
scripts/receiver.ps1      啟動接收器
tests/test_host.py        協定、算法與 UDP 測試
```

## 8. 排錯

| 現象 | 檢查 |
|---|---|
| 兩板不斷重連 | 熱點是否真的 5 GHz、SSID／密碼、天線、WPA2 相容性、頻道支援；讀串口 disconnect reason |
| 板子有 IP，但視窗兩台 waiting | PC 防火牆、UDP 5500、是否誤開 Demo／Replay、接收器是否重複開啟占用埠、gateway 是否指向 PC |
| 兩台 online，但沒有 CSI | 確認板 1 sent 增加、兩台同頻道、正確角色、天線支援 5 GHz；查看板 2 captured／invalid_length |
| 有 captured，但 invalid 持續增加 | 看視窗錯誤訊息的實際長度；本版只接受 490-byte HE20 SU；保留錄製與日誌後核對 PHY／IDF 版本，不任意截斷資料 |
| 整片振幅跳動 | 對照 AGC／FFT／補償倍數；保持原始資料，試 shape normalization 做對照 |
| 等待資料／未更新 | 板子斷線、熱點關閉／換頻道或收包停止；避免把停住的曲線當穩定背景 |
| 動作完全看不出差異 | 先以人穿越／揮手測試、調整幾何位置；確認資料率與格式後再測細微動作 |
| 封包缺口上升 | 降發送率、減少 Wi-Fi 負载、檢查 PC 佇列；不要先增加平滑掩蓋掉包 |
| 基線一直重來 | 觀察是否有 >250 ms 間隙、頻道／格式／boot 改變，或增益暖機切換 |

## 9. 驗證方式與限制

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

軟體驗證包含協定往返、壞封包、時間倒退、序號回繞／重開機、DC／無效字、增益補償、動作與靜止差異、localhost UDP、原始錄製與重播。視窗使用模擬模式及重播模式作 smoke test，截圖檢查布局。

韌體必須以兩個角色分別編譯。**編譯／模擬成功不代表實機 5 GHz、ESP-NOW 與熱點併行已驗證**；本次未燒錄硬體，未測實際無線收樣率、板子天線或敲擊特徵。這些是第一輪實機驗收項目。

本次 HE20 SU 兩個角色均已編譯成功，電腦端 21 項測試通過；先前 HT20 驗證紀錄與 binary SHA256 見 [VALIDATION.md](VALIDATION.md)。

實測至少確認：兩板開機、同頻、名稱正確、板 2 有 CSI、靜止校正成功、重複揮手可見、拔掉板 1 後 CSI 停止、重新上電恢復，以及錄製可重播。1000 Hz 不作保證；先完成 200 Hz 的穩定資料路徑。

## 10. 參考來源

- [Espressif ESP-CSI](https://github.com/espressif/esp-csi)：C5 CSI／ESP-NOW 設定、增益元件使用方式。
- [ESP32-C5 高速 CSI 專案](https://github.com/TryTwoTop/esp32c5-csi-keystroke)：參考 callback 與二進位輸出解耦的方向；本專案重寫為 UDP，不直接使用 USB serial 協定。
- [ESP-IDF C5 Wi-Fi CSI 文件](https://docs.espressif.com/projects/esp-idf/en/v6.0/esp32c5/api-guides/wifi-driver/wifi-vendor-features.html)：HE20 SU 子載波排列、490-byte 長度、無效子載波與 invalid word。
- [ESP-NOW C5 文件](https://docs.espressif.com/projects/esp-idf/en/stable/esp32c5/api-reference/network/esp_now.html)：5 GHz 頻道、同頻需求、callback 使用方式。

本專案程式採 Apache-2.0；相依元件的授權隨其原始套件保留。
