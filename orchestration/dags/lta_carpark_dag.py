from datetime import datetime, timedelta
import os
import json
import logging
import requests
from airflow.decorators import dag, task
from airflow.operators.python import PythonOperator
from airflow.providers.google.cloud.operators.dataflow import DataflowStartFlexTemplateOperator
from airflow.providers.google.cloud.operators.bigquery import BigQueryExecuteQueryOperator
from google.cloud import storage
from airflow.models import Variable

PROJECT_ID = "lta-carpark-489300"
BUCKET_NAME = "lta-carpark-489300"
BQ_RAW_TABLE = f"{PROJECT_ID}:carpark_raw.carpark_availability"
REGION = "us-west1"
SERVICE_ACCOUNT_EMAIL = "lta-carpark-pipeline@lta-carpark-489300.iam.gserviceaccount.com"
NETWORK = "default"
SUBNETWORK = f"regions/{REGION}/subnetworks/default"
WORKER_ZONE = "us-west1-c"


# Default arguments
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 3,  # Increased retries
    'retry_delay': timedelta(minutes=5),
}

# DAG definition
@dag(
    default_args=default_args,
    description='LTA Carpark Availability Pipeline',
    schedule_interval=timedelta(minutes=5),  # Runs every 5 minutes
    start_date=datetime(2025, 3, 28),
    catchup=False,
    tags=['carpark', 'lta', 'availability'],
)
def lta_carpark_pipeline():
    """
    LTA Carpark Availability ETL Pipeline:
    1. Fetch data from API and write to GCS (data lake)
    2. Use Dataflow to load GCS data to BigQuery (data warehouse)
    3. Clean duplicate data in BigQuery
    """
    
    # Step 1: Fetch data from API and write to GCS
    @task(task_id="fetch_api_to_gcs")
    def fetch_api_to_gcs(**kwargs):
        """Fetch carpark availability data from LTA API and write to GCS"""
        import requests
        from google.cloud import storage
        import json
        from datetime import datetime
        import uuid
        
        # API parameters
        base_url = 'https://datamall2.mytransport.sg/ltaodataservice/'
        endpoint = "CarParkAvailabilityv2"
        url = base_url + endpoint
        
        # API key
        api_key = Variable.get("lta_api_key", default_var=os.getenv("LTA_API_KEY"))
        if not api_key:
            raise ValueError("Missing API key. Set Airflow Variable 'lta_api_key' or env var LTA_API_KEY.")
        
        # Prepare headers
        headers = {'AccountKey': api_key, 'accept': 'application/json'}
        
        try:
            # Send API request
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            
            # Parse response
            data = response.json()
            carparks = data.get('value', [])
            
            # Add timestamp and process coordinates
            timestamp = datetime.utcnow().isoformat()
            ingestion_time = datetime.utcnow().isoformat()
            processing_time = datetime.utcnow().isoformat()
            for carpark in carparks:
                # Add timestamp
                carpark['timestamp'] = timestamp
                # BigQuery raw table requires these fields (TIMESTAMP, REQUIRED)
                carpark['ingestion_time'] = ingestion_time
                carpark['processing_time'] = processing_time
                
                # Parse location coordinates
                if 'Location' in carpark:
                    try:
                        lat, lng = map(float, carpark['Location'].split())
                        carpark['Latitude'] = lat
                        carpark['Longitude'] = lng
                    except (ValueError, TypeError):
                        carpark['Latitude'] = None
                        carpark['Longitude'] = None
            
            # Get current time for filename - use UTC time for consistency
            current_time = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
            unique_id = str(uuid.uuid4())[:8]
            day_folder = datetime.utcnow().strftime('%Y%m%d')
            filename = f"carpark-{current_time}-{unique_id}.json"
            
            # Connect to GCS and store data - organize data with date folders
            client = storage.Client()
            bucket = client.get_bucket(BUCKET_NAME)
            blob = bucket.blob(f"carpark-data/{day_folder}/{filename}")
            
            # Write JSON formatted data, one record per line
            json_lines = '\n'.join([json.dumps(record) for record in carparks])
            blob.upload_from_string(json_lines)
            
            logging.info(f"Successfully fetched and wrote {len(carparks)} carpark records to GCS: gs://{BUCKET_NAME}/carpark-data/{day_folder}/{filename}")
            
            # Return current date folder for next task
            return {
                "day_folder": day_folder,
                "blob_name": f"carpark-data/{day_folder}/{filename}",
                "gcs_uri": f"gs://{BUCKET_NAME}/carpark-data/{day_folder}/{filename}",
            }
            
        except Exception as e:
            logging.error(f"API request or save to GCS failed: {str(e)}")
            raise
    
    # Step 2: Prepare JavaScript transform file
    @task(task_id="prepare_transform_script")
    def prepare_transform_script(**kwargs):
        """Prepare JavaScript transform script on GCS"""
        from google.cloud import storage
        
        # Define transform script content
        transform_script = """
        function transform(line) {
          // Parse JSON
          var carparkData = JSON.parse(line);
          
          // Keep original fields
          return JSON.stringify(carparkData);
        }
        """
        
        # Upload to GCS
        client = storage.Client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob("scripts/transform.js")
        blob.upload_from_string(transform_script)
        
        # Create schema file
        schema_json = {
            "BigQuery Schema": [
                {"name": "timestamp", "type": "TIMESTAMP", "mode": "REQUIRED"},
                {"name": "CarParkID", "type": "STRING", "mode": "REQUIRED"},
                {"name": "Area", "type": "STRING", "mode": "NULLABLE"},
                {"name": "Development", "type": "STRING", "mode": "NULLABLE"},
                {"name": "Location", "type": "STRING", "mode": "NULLABLE"},
                {"name": "Latitude", "type": "FLOAT", "mode": "NULLABLE"},
                {"name": "Longitude", "type": "FLOAT", "mode": "NULLABLE"},
                {"name": "AvailableLots", "type": "INTEGER", "mode": "NULLABLE"},
                {"name": "LotType", "type": "STRING", "mode": "NULLABLE"},
                {"name": "Agency", "type": "STRING", "mode": "NULLABLE"},
                {"name": "ingestion_time", "type": "TIMESTAMP", "mode": "REQUIRED"},
                {"name": "processing_time", "type": "TIMESTAMP", "mode": "REQUIRED"}
            ]
        }
        
        # Upload schema file
        schema_blob = bucket.blob("schemas/carpark_schema.json")
        schema_blob.upload_from_string(json.dumps(schema_json))
        
        return {
            "transform_path": f"gs://{BUCKET_NAME}/scripts/transform.js",
            "schema_path": f"gs://{BUCKET_NAME}/schemas/carpark_schema.json"
        }
    
    # Step 3: Use Dataflow template to load GCS data to BigQuery
    @task(task_id="start_gcs_to_bigquery", retries=5, retry_delay=timedelta(minutes=2))
    def start_gcs_to_bigquery(script_paths, target, **kwargs):
        """
        Load JSON Lines from GCS into BigQuery using BigQuery API.

        Note:
        - 專案原本使用 Dataflow Flex Template，但目前 template 在這個環境下反覆 JOB_STATE_FAILED。
        - 我們改用 BigQuery 原生 load 測試資料管線是否正確（資料格式/權限/Schema）。
        - 後續若要恢復 Dataflow，只要再把這段替換回原 operator 即可。
        """
        from google.cloud import bigquery, storage

        bq_client = bigquery.Client(project=PROJECT_ID)
        storage_client = storage.Client(project=PROJECT_ID)
        table_ref = f"{PROJECT_ID}.carpark_raw.carpark_availability"

        if not target or "gcs_uri" not in target:
            raise ValueError("Missing target info from fetch_api_to_gcs (expected keys: gcs_uri)")

        json_uris = [target["gcs_uri"]]

        schema_fields = [
            bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("CarParkID", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("Area", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("Development", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("Location", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("Latitude", "FLOAT", mode="NULLABLE"),
            bigquery.SchemaField("Longitude", "FLOAT", mode="NULLABLE"),
            bigquery.SchemaField("AvailableLots", "INTEGER", mode="NULLABLE"),
            bigquery.SchemaField("LotType", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("Agency", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("ingestion_time", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("processing_time", "TIMESTAMP", mode="REQUIRED"),
        ]

        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=schema_fields,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            ignore_unknown_values=True,
        )

        for uri in json_uris:
            logging.info(f"Loading into {table_ref}: {uri}")
            load_job = bq_client.load_table_from_uri(uri, table_ref, job_config=job_config)
            load_job.result()  # wait for each file

        return {"loaded_files": len(json_uris)}
    
    # Step 4: Clean duplicate data in BigQuery
    @task(task_id="clean_duplicate_data", trigger_rule="all_done")
    def clean_duplicate_data(**kwargs):
        """Clean duplicate data in BigQuery"""
        # Fix deduplication query to maintain partitioning
        dedup_query = """
        -- Create a temporary table with distinct records
        CREATE OR REPLACE TEMP TABLE temp_deduped AS
        SELECT DISTINCT * FROM `lta-carpark-489300.carpark_raw.carpark_availability`;

        -- Delete all records from the partitioned table
        DELETE FROM `lta-carpark-489300.carpark_raw.carpark_availability` 
        WHERE TRUE;

        -- Insert the deduplicated records back
        INSERT INTO `lta-carpark-489300.carpark_raw.carpark_availability`
        SELECT * FROM temp_deduped;
        """
        
        clean_task = BigQueryExecuteQueryOperator(
            task_id="clean_duplicates_in_bigquery",
            sql=dedup_query,
            use_legacy_sql=False,
            location=REGION,
        )
        
        return clean_task.execute(context=kwargs)

    # Define task dependencies
    # (XCom target info returned from fetch_api_to_gcs)
    target = fetch_api_to_gcs()
    transform_files = prepare_transform_script()
    load_to_bq = start_gcs_to_bigquery(transform_files, target)
    clean_duplicates = clean_duplicate_data()

    # Set task order
    target >> transform_files >> load_to_bq >> clean_duplicates

# Instantiate DAG
carpark_dag = lta_carpark_pipeline()