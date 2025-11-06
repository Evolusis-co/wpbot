"""
Convert Word document scenarios to Qdrant format with embeddings.
This script reads 'Scenarios for Ai.docx' and creates a JSON file
with proper Qdrant point structure (id, vector, payload).
"""

import os
import json
from docx import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from dotenv import load_dotenv
from tqdm import tqdm
import time

# Load environment variables
load_dotenv()

# Configuration
DOCX_FILE = "Scenarios for Ai.docx"
OUTPUT_FILE = "scenarios_qdrant_corrected.json"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
EMBEDDING_MODEL = "models/embedding-001"

def read_docx(file_path):
    """Read content from Word document."""
    print(f"📖 Reading document: {file_path}")
    doc = Document(file_path)
    content = []
    for para in doc.paragraphs:
        if para.text.strip():
            content.append(para.text.strip())
    full_text = "\n\n".join(content)
    print(f"✅ Loaded {len(full_text)} characters from document")
    return full_text

def detect_scenario_info(text):
    """Detect scenario category and generate title."""
    text_lower = text.lower()
    
    # Detect category based on keywords
    if "step" in text_lower or "situation" in text_lower and "task" in text_lower:
        category = "STEP Framework"
        tags = ["step", "adaptability", "change_management"]
    elif "4rs" in text_lower or "4r" in text_lower:
        category = "4Rs Framework"
        tags = ["4rs", "emotional_intelligence", "relationships"]
    elif "crisis" in text_lower or "urgent" in text_lower:
        category = "Crisis Handling"
        tags = ["crisis", "conflict_resolution", "stress_management"]
    elif "redirect" in text_lower:
        category = "Redirection"
        tags = ["redirection", "goal_setting", "focus"]
    elif "guideline" in text_lower or "rule" in text_lower:
        category = "Guidelines"
        tags = ["guidelines", "best_practices", "standards"]
    else:
        category = "General Coaching"
        tags = ["coaching", "workplace", "communication"]
    
    # Generate title from first meaningful line
    lines = text.split('\n')
    title = next((line.strip() for line in lines if len(line.strip()) > 20), "Coaching Scenario")
    title = title[:100]  # Limit title length
    
    return category, title, tags

def chunk_document(content, chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP):
    """Split document into chunks."""
    print(f"\n📝 Chunking document (size: {chunk_size}, overlap: {chunk_overlap})")
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        separators=["\n\n", "\n", ". ", " ", ""]
    )
    chunks = text_splitter.split_text(content)
    print(f"✅ Created {len(chunks)} chunks")
    return chunks

def generate_embeddings(chunks):
    """Generate embeddings for chunks using Google's embedding model."""
    print(f"\n🧮 Generating embeddings for {len(chunks)} chunks...")
    
    embeddings_model = GoogleGenerativeAIEmbeddings(
        model=EMBEDDING_MODEL,
        google_api_key=os.getenv("GOOGLE_API_KEY")
    )
    
    # Generate embeddings in batches to show progress
    all_embeddings = []
    batch_size = 50
    
    start_time = time.time()
    for i in tqdm(range(0, len(chunks), batch_size), desc="Embedding batches"):
        batch = chunks[i:i + batch_size]
        batch_embeddings = embeddings_model.embed_documents(batch)
        all_embeddings.extend(batch_embeddings)
    
    elapsed = time.time() - start_time
    print(f"✅ Generated {len(all_embeddings)} embeddings (768-dim) in {elapsed:.1f}s")
    
    return all_embeddings

def create_qdrant_format(chunks, embeddings):
    """Convert chunks and embeddings to Qdrant point format."""
    print(f"\n🔄 Creating Qdrant format for {len(chunks)} points...")
    
    qdrant_points = []
    
    # Detect overall document info from first chunk
    category, title, tags = detect_scenario_info(chunks[0] if chunks else "")
    
    for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        point = {
            "id": idx + 1,
            "vector": embedding,
            "payload": {
                "chunk_id": f"scenario_chunk_{idx+1:04d}",
                "scenario_title": title,
                "category": category,
                "chunk_index": idx + 1,
                "total_chunks": len(chunks),
                "content": chunk,
                "tags": tags
            }
        }
        qdrant_points.append(point)
    
    print(f"✅ Created {len(qdrant_points)} Qdrant points")
    return qdrant_points

def save_to_json(qdrant_points, output_path):
    """Save Qdrant points to JSON file."""
    print(f"\n💾 Saving to {output_path}...")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(qdrant_points, f, indent=2, ensure_ascii=False)
    
    file_size = os.path.getsize(output_path) / (1024 * 1024)  # MB
    print(f"✅ Saved {len(qdrant_points)} points to {output_path} ({file_size:.2f} MB)")

def main():
    """Main conversion pipeline."""
    print("=" * 60)
    print("🚀 SCENARIOS TO QDRANT CONVERTER")
    print("=" * 60)
    
    # Check if input file exists
    if not os.path.exists(DOCX_FILE):
        print(f"❌ Error: File '{DOCX_FILE}' not found!")
        return
    
    # Step 1: Read document
    content = read_docx(DOCX_FILE)
    
    # Step 2: Chunk document
    chunks = chunk_document(content)
    
    # Step 3: Generate embeddings
    embeddings = generate_embeddings(chunks)
    
    # Step 4: Create Qdrant format
    qdrant_points = create_qdrant_format(chunks, embeddings)
    
    # Step 5: Save to JSON
    save_to_json(qdrant_points, OUTPUT_FILE)
    
    print("\n" + "=" * 60)
    print("✅ CONVERSION COMPLETE!")
    print("=" * 60)
    print(f"\n📋 Summary:")
    print(f"  - Input: {DOCX_FILE}")
    print(f"  - Output: {OUTPUT_FILE}")
    print(f"  - Total points: {len(qdrant_points)}")
    print(f"  - Vector dimension: {len(embeddings[0]) if embeddings else 0}")
    print(f"\n📌 Next step: Run 'python upload_to_qdrant.py' to upload to Qdrant Cloud")

if __name__ == "__main__":
    main()
