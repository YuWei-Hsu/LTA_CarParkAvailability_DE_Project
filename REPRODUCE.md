# 重現專案完整步驟

依照以下順序執行，即可重現「新加坡停車位可用數量」資料管線（API → GCS → BigQuery → dbt → Looker Studio）。

---

## 前置需求（先備齊）

| 項目 | 說明 |
|------|------|
| **GCP 帳號** | 需啟用計費（可使用免費額度） |
| **LTA API Key** | 至 [LTA DataMall](https://datamall.lta.gov.sg/content/datamall/en/request-for-api.html) 申請 |
| **dbt Cloud 帳號** | [dbt Cloud](https://cloud.getdbt.com/) 註冊（免費方案可） |
| **本機環境** | Terraform、Google Cloud SDK（gcloud）、Python 3.8+、Docker & Docker Compose、Git |

---

## 步驟 1：取得專案與環境變數

```bash
git clone https://github.com/your-username/LTA_CarParkAvailability_DE_Project.git
cd LTA_CarParkAvailability_DE_Project
```

在**專案根目錄**建立 `.env`：

```bash
# .env
AIRFLOW_UID=501
AIRFLOW_GID=20
LTA_API_KEY=你的LTA_API_Key
```

（若在 Linux 上 UID/GID 不同，可執行 `id -u`、`id -g` 填入。）

---

## 步驟 2：GCP 專案與服務帳戶

### 2.1 建立 GCP 專案

1. 開啟 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立新專案，記下 **專案 ID**（例如 `my-lta-project`）
3. 啟用計費（可僅用免費額度）

### 2.2 建立服務帳戶與金鑰

1. **IAM 與管理** → **服務帳戶** → **建立服務帳戶**
2. 名稱可填 `lta-pipeline`
3. 授予角色（至少）：
   - **Storage 管理員**（或 Storage 物件管理員）
   - **BigQuery 管理員**
   - **Dataflow 管理員**
   - **檢視者**（可選，用於列出專案資源）
4. **金鑰** → **新增金鑰** → **JSON** → 下載
5. 將 JSON 金鑰放到專案內，例如：`terraform/keys/your-service-account.json`（**勿將金鑰提交到 Git**）

### 2.3 本機 GCP 認證（給 Terraform / 本機指令用）

```bash
export GOOGLE_APPLICATION_CREDENTIALS="$(pwd)/terraform/keys/your-service-account.json"
gcloud auth application-default login
```

### 2.4 啟用所需 API

在 GCP Console **API 與服務** → **已啟用的 API** 中啟用：

- Cloud Storage API  
- BigQuery API  
- Dataflow API  
- IAM API  

（若用 Terraform 建立資源，第一次執行時也可依錯誤訊息補啟用。）

---

## 步驟 3：Terraform 建立 GCS、BigQuery

專案預設會建立 **GCS bucket**（`lta-carpark`）與 **BigQuery 資料集**（`carpark_raw` 等）。請改成你的專案與金鑰路徑。

### 3.1 修改 `terraform/variables.tf`

至少改這三項：

```hcl
variable "credentials" {
  default = "./keys/your-service-account.json"   # 金鑰檔名
}
variable "project" {
  default = "你的GCP專案ID"
}
# gcs_bucket_name 若要用預設 "lta-carpark" 可不改（需全球唯一）
variable "gcs_bucket_name" {
  default = "lta-carpark"   # 若名稱被佔用可改，例如 "lta-carpark-你的專案ID"
}
```

金鑰檔案請放在 `terraform/keys/` 下。

### 3.2 執行 Terraform

```bash
cd terraform
terraform init
terraform plan    # 確認即將建立的資源
terraform apply   # 輸入 yes 確認
```

完成後應有：

- GCS bucket（如 `gs://lta-carpark`）
- BigQuery 資料集 `carpark_raw`（及 Terraform 內定義的其他資源）

---

## 步驟 4：Airflow 排程（API → GCS → BigQuery）

### 4.1 讓 Airflow 能讀到 GCP 金鑰

金鑰請放在 **`terraform/keys/`**（與步驟 3 相同）。`orchestration/docker-compose.yml` 已掛載 `../terraform/keys` 到容器內 `/opt/airflow/keys`。

請編輯 **`orchestration/docker-compose.yml`** 第 15 行，把金鑰檔名改成你實際的檔名：

```yaml
GOOGLE_APPLICATION_CREDENTIALS: /opt/airflow/keys/你的金鑰檔名.json
```

例如金鑰檔是 `terraform/keys/my-gcp-key.json`，就填 `/opt/airflow/keys/my-gcp-key.json`。

### 4.2 專案 ID、Bucket 與 DAG 一致

目前 DAG 內寫死：

- GCS bucket：`lta-carpark`
- GCP 專案：`lta-caravailability`
- BigQuery：`lta-caravailability:carpark_raw.carpark_availability`

若你在步驟 3 用了**不同的專案 ID 或 bucket 名稱**，需要改 DAG：

- 開啟 `orchestration/dags/lta_carpark_dag.py`
- 搜尋並替換 `lta-carpark`（bucket）、`lta-caravailability`（專案 ID）為你的值

### 4.3 啟動 Airflow

```bash
cd orchestration
docker-compose up -d
```

第一次建議先跑一次 init（若 compose 有 airflow-init 服務會自動執行）。等約 1～2 分鐘後：

- 瀏覽器開啟 **http://localhost:8085**
- 登入：帳號 `airflow`、密碼 `airflow`

### 4.4 在 Airflow 設定 LTA API Key

1. 上方選單 **Admin** → **Variables**
2. **Add** → Key 填 `lta_api_key`，Val 填你的 **LTA API Key** → Save

### 4.5 啟用 DAG

1. 在 DAG 列表找到 **lta_carpark_pipeline**
2. 將左側開關打開（Unpause）
3. 可手動 Trigger 一次測試，或等待每 5 分鐘自動執行

管線會：呼叫 LTA API → 寫入 GCS → 上傳 transform 腳本 → 啟動 Dataflow 將 GCS 載入 BigQuery → 清理重複資料。

---

## 步驟 5：dbt Cloud 轉換與建模

### 5.1 註冊並建立專案

1. 登入 [dbt Cloud](https://cloud.getdbt.com/)
2. 建立新專案，名稱例如 `lta-carpark-availability`
3. 資料倉儲選 **BigQuery**
4. 連線時上傳**同一個** GCP 服務帳戶 JSON 金鑰，並選擇你在步驟 3 建立的專案與資料集

### 5.2 連結 GitHub

1. 在 dbt Cloud 專案中選擇 **Link an Existing GitHub Repository**
2. 授權後選擇本專案的 GitHub repo
3. 依畫面設定 **Deploy key**（在 GitHub repo Settings → Deploy keys 新增，並勾選 Allow write access）

### 5.3 對齊 BigQuery 資料集

專案 dbt 模型會寫入的 dataset（例如 `dbt_yzheng`）需與 dbt Cloud 設定的 **Target Dataset** 一致。請在 `dbt/dbt_project.yml` 或 dbt Cloud 的連線設定中，將 dataset 設成你在 BigQuery 要使用的名稱（若沿用 Terraform 預設，可能是 `carpark_processed` 或你自訂的）。

### 5.4 執行 dbt

1. 在 dbt Cloud **Develop** 裡確認模型存在（staging、marts、reporting）
2. 執行 **Run** → **Start a run**（`dbt run`、必要時 `dbt test`）
3. 在 **Environments** 可建立 Production、排程每日執行

完成後 BigQuery 會有整理好的維度、事實與報表表，可供 Looker Studio 使用。

---

## 步驟 6：Looker Studio 儀表板

1. 前往 [Looker Studio](https://lookerstudio.google.com/)
2. **建立** → **報表**
3. 資料來源選 **BigQuery** → 選你的專案 → 選 dbt 寫入的 dataset（如 `dbt_yzheng` 或你在 dbt 設定的）→ 選報表表（如 `rpt_carpark_utilization`）
4. 依 README 的「Insights & Visualizations」章節設計：
   - 第 1 頁：即時分析（地圖、指標卡、依區域的圖表、篩選）
   - 第 2 頁：歷史分析（24 小時趨勢、時段分類、熱力圖、最擁擠停車場）

---

## 步驟 7：驗證整條管線

1. **Airflow**：DAG 是否每 5 分鐘成功跑完（無紅字）
2. **GCS**：`gsutil ls gs://lta-carpark/carpark-data/` 是否有依日期產生的 JSON
3. **BigQuery**：`carpark_raw.carpark_availability` 是否有新資料
4. **dbt**：報表表是否有資料、`dbt test` 是否通過
5. **Looker Studio**：儀表板是否正常顯示並可篩選

---

## 常見問題

| 狀況 | 建議 |
|------|------|
| Terraform `credentials` 找不到 | 確認金鑰路徑為 `terraform/keys/xxx.json`，且從 `terraform/` 目錄執行 |
| Airflow 任務報錯 "Could not determine credentials" | 確認 `orchestration/terraform/keys/` 內有金鑰檔，且 docker-compose 中 `GOOGLE_APPLICATION_CREDENTIALS` 檔名正確 |
| Airflow 報 "Variable lta_api_key not found" | 在 Admin → Variables 新增 Key=`lta_api_key`，Val=你的 LTA API Key |
| Dataflow 或 BigQuery 權限錯誤 | 確認服務帳戶具備 Dataflow 管理員、BigQuery 管理員、Storage 管理員 |
| Bucket 名稱已被使用 | GCS bucket 名稱需全球唯一，在 `variables.tf` 改 `gcs_bucket_name`，並同步改 DAG 內的 bucket 名稱 |

---

## 流程總覽

```
LTA API → (Airflow 排程) → GCS → (Dataflow) → BigQuery raw
                                              → dbt 模型 → 報表表 → Looker Studio
```

依序完成步驟 1～7 即可重現此專案。
