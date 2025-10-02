# ingest_wxr_supabase.py
import os
from datetime import datetime
from lxml import etree
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL")
engine = create_engine(DB_URL, pool_pre_ping=True)

NS = {
    "wp": "http://wordpress.org/export/1.2/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

def clean_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script","style","noscript","svg","nav","header","footer"]):
        t.decompose()
    return "\n".join([ln.strip() for ln in soup.get_text("\n", strip=True).splitlines() if ln.strip()])

def to_iso(s):
    if not s: return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S +0000"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except Exception:
            pass
    return None

def ingest(xml_path: str):
    parser = etree.XMLParser(recover=True)
    tree = etree.parse(xml_path, parser)
    channel = tree.getroot().find("channel")
    items = channel.findall("item")

    inserted = 0
    with engine.begin() as con:
        for it in items:
            title = (it.findtext("title") or "").strip() or "(untitled)"
            post_type = (it.findtext("{http://wordpress.org/export/1.2/}post_type") or "").strip()
            if post_type not in ("post","page","attachment"):
                # skip menus, etc.
                continue
            published_at = (it.findtext("{http://wordpress.org/export/1.2/}post_date") 
                            or it.findtext("pubDate") or "")
            published_dt = to_iso(published_at)

            link = (it.findtext("link") or "").strip()
            raw_html = (it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
            text_content = clean_html_to_text(raw_html)

            con.execute(text("""
                INSERT INTO posts (title, doc_type, published_at, source_url, raw_html, text_content)
                VALUES (:title, 'html', :published_at, :source_url, :raw_html, :text_content)
            """), {
                "title": title,
                "published_at": published_dt,
                "source_url": link or None,
                "raw_html": raw_html,
                "text_content": text_content
            })
            inserted += 1
    print(f"Inserted {inserted} items")

if __name__ == "__main__":
    ingest(r"C:\Users\tessj\Desktop\onagri\html_files\agri-docspce.wordpress.2025-09-10.000.xml")  # <-- put your exact path here
