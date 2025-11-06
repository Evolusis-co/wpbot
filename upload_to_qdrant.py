"""
Upload scenarios from JSON file to Qdrant Cloud.
Reads 'scenarios_qdrant_corrected.json' and uploads to your Qdrant cluster.
"""

import os
import json
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from dotenv import load_dotenv
from tqdm import tqdm
import time

# Load environment variables
load_dotenv()

# Configuration
JSON_FILE = "scenarios_qdrant_corrected.json"
COLLECTION_NAME = "bridgetext_scenarios"

def load_json_data(file_path):
    """Load Qdrant points from JSON file."""
    print(f"📖 Loading data from {file_path}...")
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    print(f"✅ Loaded {len(data)} points")
    return data

def connect_to_qdrant():
    """Connect to Qdrant Cloud."""
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")
    
    if not qdrant_url or not qdrant_api_key:
        raise ValueError("❌ QDRANT_URL and QDRANT_API_KEY must be set in .env file")
    
    print(f"\n🔌 Connecting to Qdrant Cloud...")
    print(f"   URL: {qdrant_url}")
    
    client = QdrantClient(
        url=qdrant_url,
        api_key=qdrant_api_key,
    )
    
    print("✅ Connected to Qdrant Cloud")
    return client

def create_collection(client, collection_name, vector_size):
    """Create Qdrant collection (or use existing)."""
    print(f"\n📦 Setting up collection: {collection_name}")
    
    # Check if collection exists
    collections = client.get_collections().collections
    collection_names = [col.name for col in collections]
    
    if collection_name in collection_names:
        print(f"✅ Collection '{collection_name}' already exists")
        # Optionally, you can delete and recreate:
        # client.delete_collection(collection_name)
        # print(f"🗑️  Deleted existing collection")
    else:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(
                size=vector_size,
                distance=Distance.COSINE
            )
        )
        print(f"✅ Created collection '{collection_name}'")
        print(f"   - Vector size: {vector_size}")
        print(f"   - Distance metric: COSINE")

def upload_points(client, collection_name, points_data, batch_size=100):
    """Upload points to Qdrant collection."""
    print(f"\n⬆️  Uploading {len(points_data)} points to Qdrant...")
    print(f"   Batch size: {batch_size}")
    
    start_time = time.time()
    
    # Convert to PointStruct objects and upload in batches
    for i in tqdm(range(0, len(points_data), batch_size), desc="Uploading batches"):
        batch = points_data[i:i + batch_size]
        
        points = [
            PointStruct(
                id=point["id"],
                vector=point["vector"],
                payload=point["payload"]
            )
            for point in batch
        ]
        
        client.upsert(
            collection_name=collection_name,
            points=points
        )
    
    elapsed = time.time() - start_time
    print(f"✅ Uploaded {len(points_data)} points in {elapsed:.1f}s")

def verify_upload(client, collection_name, expected_count):
    """Verify upload by checking collection info."""
    print(f"\n🔍 Verifying upload...")
    
    collection_info = client.get_collection(collection_name)
    actual_count = collection_info.points_count
    
    print(f"   Expected points: {expected_count}")
    print(f"   Actual points: {actual_count}")
    
    if actual_count == expected_count:
        print("✅ Verification successful!")
    else:
        print(f"⚠️  Warning: Point count mismatch!")

def main():
    """Main upload pipeline."""
    print("=" * 60)
    print("🚀 UPLOAD TO QDRANT CLOUD")
    print("=" * 60)
    
    # Check if input file exists
    if not os.path.exists(JSON_FILE):
        print(f"❌ Error: File '{JSON_FILE}' not found!")
        print(f"   Run 'python convert_scenarios_to_qdrant.py' first")
        return
    
    # Step 1: Load JSON data
    points_data = load_json_data(JSON_FILE)
    
    if not points_data:
        print("❌ Error: No data found in JSON file")
        return
    
    vector_size = len(points_data[0]["vector"])
    
    # Step 2: Connect to Qdrant
    client = connect_to_qdrant()
    
    # Step 3: Create/verify collection
    create_collection(client, COLLECTION_NAME, vector_size)
    
    # Step 4: Upload points
    upload_points(client, COLLECTION_NAME, points_data)
    
    # Step 5: Verify upload
    verify_upload(client, COLLECTION_NAME, len(points_data))
    
    print("\n" + "=" * 60)
    print("✅ UPLOAD COMPLETE!")
    print("=" * 60)
    print(f"\n📋 Summary:")
    print(f"  - Collection: {COLLECTION_NAME}")
    print(f"  - Points uploaded: {len(points_data)}")
    print(f"  - Vector dimension: {vector_size}")
    print(f"  - Cluster URL: {os.getenv('QDRANT_URL')}")
    print(f"\n📌 Your scenarios are now live in Qdrant Cloud!")
    print(f"   Run your Flask app to test: python app.py")

if __name__ == "__main__":
    main()
