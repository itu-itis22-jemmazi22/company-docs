import io, os, uuid
from datetime import datetime
from typing import Optional

import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text

# Supabase client
from supabase import create_client, Client

# ---- Setup ----
st.set_page_config(page_title="Company Docs", layout="wide")
st.title("📚 Company Docs — Search & Upload (Supabase)")

SB_URL  = st.secrets["SUPABASE_URL"]
SB_ANON = st.secrets["SUPABASE_ANON_KEY"]              # for read
SB_SVC  = st.secrets["SUPABASE_SERVICE_ROLE_KEY"]      # for upload (keep secret)
DB_URL  = st.secrets["DATABASE_URL"]

engine = create_engine(DB_URL, pool_pre_ping=True)
sb: Client = create_client(SB_URL, SB_SVC)  # use service key so we can upload server-side
BUCKET = "docs"

# ---- Helpers ----
def clean_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script","style","noscript","svg","nav","header","footer"]):
        t.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n", strip=True).splitlines()]
    return "\n".join([ln for ln in lines if ln])

def insert_row(title: str, doc_type: str, published_at: Optional[datetime],
               source_url: Optional[str], raw_html: Optional[str],
               text_content: str, storage_path: Optional[str]) -> int:
    with engine.begin() as con:
        row = con.execute(text("""
            insert into posts (title, doc_type, published_at, source_url, raw_html, text_content, storage_path)
            values (:title, :doc_type, :published_at, :source_url, :raw_html, :text_content, :storage_path)
            returning id;
        """), {
            "title": title.strip(),
            "doc_type": doc_type,
            "published_at": published_at,
            "source_url": source_url or None,
            "raw_html": raw_html,
            "text_content": text_content,
            "storage_path": storage_path
        }).fetchone()
        return int(row[0])

def search_docs(q: str, year_from: int, year_to: int, doc_types: list[str], limit: int = 200) -> pd.DataFrame:
    base = """
    select id, title, doc_type,
           coalesce(published_at::date, created_at::date) as date,
           source_url, storage_path
    from posts
    where doc_type = any(:types)
      and (published_at is null or (published_at::date between :d1 and :d2))
    """
    params = {
        "types": doc_types if doc_types else ["html", "pdf"],
        "d1": f"{year_from}-01-01",
        "d2": f"{year_to}-12-31"
    }
    if q.strip():
        base += " and text_tsv @@ plainto_tsquery('simple', :q)"
        params["q"] = q
    base += " order by coalesce(published_at, created_at) desc nulls last, id desc limit :lim"
    params["lim"] = limit
    with engine.begin() as con:
        return pd.read_sql(text(base), con, params=params)

def fetch_post(post_id: int) -> Optional[pd.Series]:
    with engine.begin() as con:
        df = pd.read_sql(text("select * from posts where id=:i"), con, params={"i": post_id})
    return df.iloc[0] if not df.empty else None

def upload_pdf_to_storage(pdf_bytes: bytes, suggested_name: str, published_at: Optional[datetime]) -> str:
    # path example: pdf/2025/09/UUID_name.pdf
    year = (published_at.year if published_at else datetime.utcnow().year)
    month = (published_at.month if published_at else datetime.utcnow().month)
    unique = uuid.uuid4().hex[:8]
    safe = suggested_name.replace(" ", "_").replace("/", "-")
    path = f"pdf/{year:04d}/{month:02d}/{unique}_{safe}"
    # upload
    sb.storage.from_(BUCKET).upload(path, pdf_bytes, {"content-type": "application/pdf", "x-upsert": "true"})
    return path

def signed_download_url(storage_path: str, expire_sec: int = 3600) -> Optional[str]:
    try:
        res = sb.storage.from_(BUCKET).create_signed_url(storage_path, expire_sec)
        return res.get("signedURL") or res.get("signedUrl")
    except Exception:
        return None

# ---- UI ----
tab_search, tab_upload = st.tabs(["🔎 Search", "⬆️ Upload"])

with tab_search:
    col1, col2 = st.columns([2,1])
    with col1:
        q = st.text_input("Search phrase (plain words):", "")
    with col2:
        doc_types = st.multiselect("Type", ["html","pdf"], default=["html","pdf"])
    y1, y2 = st.slider("Year range", 1990, 2030, (1990, 2030))
    if st.button("Search", type="primary") or q == "":
        results = search_docs(q, y1, y2, doc_types)
        # add a "download" column for PDFs
        def mk_link(row):
            if row["doc_type"] == "pdf" and row["storage_path"]:
                url = signed_download_url(row["storage_path"])
                return f"[Download]({url})" if url else "(no url)"
            return ""
        if not results.empty:
            results["download"] = results.apply(mk_link, axis=1)
        st.write(f"Results: {len(results)}")
        st.dataframe(results, use_container_width=True, height=360)

        sel = st.number_input("Open document id", min_value=1, step=1,
                              value=int(results["id"].iloc[0]) if len(results) else 1)
        if st.button("Open"):
            r = fetch_post(int(sel))
            if r is None:
                st.warning("No such document.")
            else:
                st.subheader(r["title"])
                st.caption(f"{r['doc_type']} • {r.get('published_at')} • id={r['id']}")
                if r.get("source_url"):
                    st.markdown(f"🔗 [Source link]({r['source_url']})")
                if r["doc_type"] == "html":
                    st.markdown("### Rendered HTML")
                    st.components.v1.html(r.get("raw_html") or "<p>(no content)</p>", height=500, scrolling=True)
                else:
                    # show extracted text preview + a download link
                    dl = signed_download_url(r.get("storage_path") or "")
                    if dl:
                        st.markdown(f"[⬇️ Download PDF]({dl})")
                    st.markdown("### Extracted text (preview)")
                    st.code((r.get("text_content") or "")[:8000])

with tab_upload:
    st.markdown("#### Add a new document")
    title = st.text_input("Title *")
    published = st.date_input("Published date (optional)", value=None)
    source_url = st.text_input("Source URL (optional)")
    kind = st.radio("Document type", ["HTML", "PDF"], horizontal=True)

    if kind == "HTML":
        html_file = st.file_uploader("Upload HTML file", type=["html","htm"])
        html_paste = st.text_area("Or paste HTML here")
        if st.button("Save HTML"):
            if not title or (not html_file and not html_paste.strip()):
                st.error("Title and HTML are required.")
            else:
                html = (html_file.read().decode("utf-8", errors="replace") if html_file else html_paste)
                text_content = clean_html_to_text(html)
                insert_row(
                    title=title,
                    doc_type="html",
                    published_at=(datetime.combine(published, datetime.min.time()) if published else None),
                    source_url=source_url,
                    raw_html=html,
                    text_content=text_content,
                    storage_path=None
                )
                st.success("Saved HTML ✅")

    else:
        pdf_file = st.file_uploader("Upload PDF file", type=["pdf"])
        if st.button("Save PDF"):
            if not title or not pdf_file:
                st.error("Title and PDF are required.")
            else:
                pdf_bytes = pdf_file.read()
                # upload to Storage
                storage_path = upload_pdf_to_storage(pdf_bytes, suggested_name=pdf_file.name,
                                                     published_at=(published if published else None))
                # extract text for search
                try:
                    text_content = extract_text(io.BytesIO(pdf_bytes)) or ""
                except Exception:
                    text_content = ""
                insert_row(
                    title=title,
                    doc_type="pdf",
                    published_at=(datetime.combine(published, datetime.min.time()) if published else None),
                    source_url=source_url,
                    raw_html=None,
                    text_content=text_content,
                    storage_path=storage_path
                )
                st.success("Saved PDF ✅")
