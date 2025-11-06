import os, uuid
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_SERVICE_ROLE")
b_pdf = os.getenv("BUCKET_PDF")
b_cov = os.getenv("BUCKET_COVER")

assert url, "Missing SUPABASE_URL"
assert key, "Missing SUPABASE_SERVICE_ROLE"
assert b_pdf and b_cov, "Missing BUCKET_PDF/BUCKET_COVER"

sb = create_client(url, key)

print("Listing buckets...")
print([b["name"] for b in sb.storage.list_buckets()])

print("Uploading tiny txt into docs...")
sb.storage.from_(b_pdf).upload(
    f"debug/{uuid.uuid4().hex}.txt",
    b"hello",
    {"content-type": "text/plain", "x-upsert": "true"}
)
print("OK (docs)")

print("Uploading tiny txt into covers...")
sb.storage.from_(b_cov).upload(
    f"debug/{uuid.uuid4().hex}.txt",
    b"hi",
    {"content-type": "text/plain", "x-upsert": "true"}
)
print("OK (covers)")
