from datetime import datetime, timedelta
import os
import json
import logging
import requests
from airflow.decorators import dag, task
from airflow.operators.python import PythonOperator
from airflow.providers.google.cloud.operators.dataflow import DataflowStartFlexTemplateOperator
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


# Default parameters
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

# DAG definition (Kafka 版本：避免與非 Kafka 版 dag_id 衝突)
@dag(
    dag_id="lta_carpark_pipeline_kafka",
    default_args=default_args,
    description='LTA Carpark Availability Pipeline (Kafka)',
    schedule_interval=timedelta(minutes=5),  # Run once every 5 minutes
    start_date=datetime(2025, 3, 27),
    catchup=False,
    tags=['carpark', 'lta', 'availability', 'kafka'],
)
def lta_carpark_pipeline_kafka():
    """
    LTA Parking Availability ETL Pipeline:

    1. Fetch data from API and send it to Kafka

    2. Use Dataflow to write Kafka data to GCS (data lake)

    3. Use Dataflow to load GCS data into BigQuery (data warehouse)
    """
    
    # Step 1: Obtain data from the LTA API and send it to Kafka
    @task(task_id="fetch_and_send_to_kafka")
    def fetch_and_send_to_kafka(**kwargs):
        """Retrieve parking availability data from the LTA API and send it to Kafka."""
        import requests
        from kafka import KafkaProducer
        
        # Set API parameters
        base_url = 'https://datamall2.mytransport.sg/ltaodataservice/'
        endpoint = "CarParkAvailabilityv2"
        url = base_url + endpoint
        
        # API Key
        api_key = Variable.get("lta_api_key", default_var=os.getenv("LTA_API_KEY"))
        if not api_key:
            raise ValueError("Missing API key. Set Airflow Variable 'lta_api_key' or env var LTA_API_KEY.")
        
        # Prepare request headers
        headers = {'AccountKey': api_key, 'accept': 'application/json'}
        
        try:
            # Send API request
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            
            # Parse the response
            data = response.json()
            carparks = data.get('value', [])
            
            # Add timestamp and process coordinates
            timestamp = datetime.now().isoformat()
            for carpark in carparks:
                # Add timestamp
                carpark['timestamp'] = timestamp
                
                # Parsing position coordinates
                if 'Location' in carpark:
                    try:
                        lat, lng = map(float, carpark['Location'].split())
                        carpark['Latitude'] = lat
                        carpark['Longitude'] = lng
                    except (ValueError, TypeError):
                        carpark['Latitude'] = None
                        carpark['Longitude'] = None
            
            # Connect to Kafka and send data
            producer = KafkaProducer(
                bootstrap_servers=['34.126.86.205:9093'],  # Use your Kafka service
                value_serializer=lambda x: json.dumps(x).encode('utf-8')
            )
            
            # Send each record
            for record in carparks:
                producer.send('carpark-availability', value=record)
            
            # Ensure that all messages have been sent
            producer.flush()
            producer.close()
            
            logging.info(f"成功获取并发送了 {len(carparks)} 条停车场记录到Kafka")
            return len(carparks)
            
        except Exception as e:
            logging.error(f"API请求或发送到Kafka失败: {str(e)}")
            raise
    
    # Step 2: Use the Dataflow template to write Kafka data to GCS
    kafka_to_gcs = DataflowStartFlexTemplateOperator(
        task_id="kafka_to_gcs_dataflow",
        project_id=PROJECT_ID,
        location=REGION,
        wait_until_finished=True,
        body={
            "launchParameter": {
                "containerSpecGcsPath": "gs://dataflow-templates-us-west1/latest/flex/Kafka_to_Gcs_Flex",
                "jobName": "kafka-to-gcs-job",
                "parameters": {
                    "readBootstrapServerAndTopic": "34.126.86.205:9093,carpark-availability",
                    "outputDirectory": f"gs://{BUCKET_NAME}/carpark-data/",
                    "outputFilenamePrefix": "carpark-",
                    "windowDuration": "5m",
                    "kafkaReadAuthenticationMode": "NONE",
                    "messageFormat": "JSON",
                    "useBigQueryDLQ": "false",
                    "tempLocation": f"gs://{BUCKET_NAME}/temp/"
                },

                "environment": {
                    "network": NETWORK,
                    "subnetwork": SUBNETWORK,
                    "workerZone": WORKER_ZONE,
                    "serviceAccountEmail": SERVICE_ACCOUNT_EMAIL
                }
        }
        }
    )
    
    # Step 3: Create a task for the JavaScript transformation file
    @task(task_id="prepare_transform_script")
    def prepare_transform_script(**kwargs):
        """Prepare the JavaScript conversion script on the GCS"""
        from google.cloud import storage
        
        # Define the content of the conversion script
        transform_script = """
        function transform(line) {
          // Analysis JSON
          var carparkData = JSON.parse(line);
          
          // Maintain the original field
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
                {"name": "Agency", "type": "STRING", "mode": "NULLABLE"}
            ]
        }
        
        # Upload schema file
        schema_blob = bucket.blob("schemas/carpark_schema.json")
        schema_blob.upload_from_string(json.dumps(schema_json))
        
        return {
            "transform_path": f"gs://{BUCKET_NAME}/scripts/transform.js",
            "schema_path": f"gs://{BUCKET_NAME}/schemas/carpark_schema.json"
        }
    
    # Step 4: Use the Dataflow template to load GCS data into BigQuery
    @task(task_id="start_gcs_to_bigquery")
    def start_gcs_to_bigquery(script_paths, **kwargs):
        """Start the Dataflow job from GCS to BigQuery"""
        from airflow.providers.google.cloud.operators.dataflow import DataflowStartFlexTemplateOperator
        
        gcs_to_bigquery = DataflowStartFlexTemplateOperator(
            task_id="gcs_to_bigquery_dataflow",
            project_id=PROJECT_ID,
            location=REGION,
            wait_until_finished=False,  # Do not wait for completion, avoid resource issues blocking Airflow
            body={
                "launchParameter": {
                    "containerSpecGcsPath": "gs://dataflow-templates-us-west1/latest/flex/GCS_Text_to_BigQuery_Flex",
                    "jobName": "gcs-to-bq-job",
                    "parameters": {
                        "javascriptTextTransformFunctionName": "transform",
                        "javascriptTextTransformGcsPath": script_paths["transform_path"],
                        "JSONPath": script_paths["schema_path"],
                        "inputFilePattern": f"gs://{BUCKET_NAME}/carpark-data/carpark-*.json",
                        "outputTable": BQ_RAW_TABLE,
                        "bigQueryLoadingTemporaryDirectory": f"gs://{BUCKET_NAME}/temp/",
                        "tempLocation": f"gs://{BUCKET_NAME}/temp/"
                    }
                },
                "environment": {
                    "network": NETWORK,
                    "subnetwork": SUBNETWORK,
                    "workerZone": WORKER_ZONE,
                    "serviceAccountEmail": SERVICE_ACCOUNT_EMAIL
                }
            }
        )
        
        # Execute task
        return gcs_to_bigquery.execute(context=kwargs)
    
    # Define task dependencies
    fetch_kafka = fetch_and_send_to_kafka()
    transform_files = prepare_transform_script()
    
    # Set task sequence
    fetch_kafka >> kafka_to_gcs >> transform_files >> start_gcs_to_bigquery(transform_files)

# Instantiate DAG
carpark_dag = lta_carpark_pipeline_kafka()