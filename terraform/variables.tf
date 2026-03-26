variable "credentials" {
    description = "GCP Credentials"
    # Service account key file path (relative to terraform/)
    default = "./keys/lta-carpark-489300-b7f39e2b0b2b.json"
}

variable "project" {
    description = "GCP Project ID"
    # From your service account key: project_id
    default = "lta-carpark-489300"
}

variable "region" {
    description = "GCP Region"
    # Replace with your location
    default = "us-west1"
}

variable "location" {
    description = "Data Location"
    default = "us-west1"
}

variable "raw_dataset_name" {
  description = "BigQuery Raw Dataset Name"
  default     = "carpark_raw"
}

variable "processed_dataset_name" {
  description = "BigQuery Processed Dataset Name"
  default     = "carpark_processed"
}

variable "gcs_bucket_name" {
    description = "Data Lake Bucket Name"
    # Bucket name must be globally unique
    default = "lta-carpark-489300"
}

variable "gcs_storage_class" {
    description = "Bucket Storage Class"
    default = "STANDARD"
}

variable "dataproc_cluster_name" {
  description = "Dataproc Cluster Name"
  default     = "carpark-flink-cluster"
}

variable "dataproc_machine_type" {
  description = "Machine type for Dataproc cluster nodes"
  default     = "n1-standard-2"
}

variable "dataproc_image_version" {
  description = "Dataproc image version"
  default     = "2.1-debian10"
}