import os
from datetime import datetime
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Load DB URL from secrets or .env
load_dotenv()
DB_URL = os.getenv("DATABASE_URL") 

def clean_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script","style","noscript","svg","nav","header","footer"]):
        t.decompose()
    return "\n".join([ln.strip() for ln in soup.get_text("\n", strip=True).splitlines() if ln.strip()])

folder = r"C:\Users\tessj\Desktop\onagri\html_files"

with engine.begin() as con:
    for fname in os.listdir(folder):
        if not fname.endswith(".html"): 
            continue
        fpath = os.path.join(folder, fname)
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            html = f.read()
        text_content = clean_html_to_text(html)
        title = os.path.splitext(fname)[0]  # use filename as title for now
        con.execute(text("""
            INSERT INTO posts (title, doc_type, published_at, raw_html, text_content)
            VALUES (:title, 'html', :published_at, :raw_html, :text_content)
        """), {
            "title": title,
            "published_at": datetime.utcnow(),
            "raw_html": html,
            "text_content": text_content
        })
        print("Imported:", fname)
