#!/usr/bin/env python3
import json
import os
import uuid
from datetime import datetime

from google.cloud import storage

from api_client import LTAApiHandler


BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "lta-carpark-489300")


def main() -> int:
    handler = LTAApiHandler()
    rows = handler.get_carpark_availability()
    if not rows:
        print("No data fetched from API.")
        return 1

    # Match DAG format: carpark-data/YYYYMMDD/carpark-*.json (JSON Lines)
    day_folder = datetime.utcnow().strftime("%Y%m%d")
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    filename = f"carpark-{ts}-{str(uuid.uuid4())[:8]}.json"
    blob_path = f"carpark-data/{day_folder}/{filename}"

    payload = "\n".join(json.dumps(r) for r in rows)

    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(blob_path)
    blob.upload_from_string(payload, content_type="application/json")

    print(f"Uploaded {len(rows)} rows to gs://{BUCKET_NAME}/{blob_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
