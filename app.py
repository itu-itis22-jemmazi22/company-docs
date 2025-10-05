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
               text_content: str, storage_path: Optional[str],
               editor: Optional[str], summary: Optional[str], cover_path: Optional[str]) -> int:
    with engine.begin() as con:
        row = con.execute(text("""
            insert into posts (title, doc_type, published_at, source_url, raw_html, text_content,
                               storage_path, editor, summary, cover_path)
            values (:title, :doc_type, :published_at, :source_url, :raw_html, :text_content,
                    :storage_path, :editor, :summary, :cover_path)
            returning id;
        """), {
            "title": title.strip(),
            "doc_type": doc_type,
            "published_at": published_at,
            "source_url": source_url or None,
            "raw_html": raw_html,
            "text_content": text_content,
            "storage_path": storage_path,
            "editor": editor or None,
            "summary": summary or None,
            "cover_path": cover_path or None
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

def upload_cover_to_storage(img_bytes: bytes, suggested_name: str, published_at: Optional[datetime]) -> str:
    year = (published_at.year if published_at else datetime.utcnow().year)
    month = (published_at.month if published_at else datetime.utcnow().month)
    unique = uuid.uuid4().hex[:8]
    safe = suggested_name.replace(" ", "_").replace("/", "-")
    path = f"covers/{year:04d}/{month:02d}/{unique}_{safe}"
    sb.storage.from_("covers").upload(path, img_bytes, {"content-type": "image/jpeg", "x-upsert": "true"})
    return path

def signed_download_url(storage_path: str, expire_sec: int = 3600) -> Optional[str]:
    try:
        res = sb.storage.from_(BUCKET).create_signed_url(storage_path, expire_sec)
        return res.get("signedURL") or res.get("signedUrl")
    except Exception:
        return None

def search_docs_advanced(title_q: str, keyword_q: str,
                         date_from, date_to,
                         doc_types: list[str], limit: int = 200) -> pd.DataFrame:
    base = """
    select id, title, doc_type,
           coalesce(published_at::date, created_at::date) as date,
           editor, summary, source_url, storage_path
    from posts
    where doc_type = any(:types)
    """
    params = {"types": doc_types if doc_types else ["html","pdf"], "lim": limit}

    # date filters
    if date_from:
        base += " and coalesce(published_at::date, created_at::date) >= :d1"
        params["d1"] = str(date_from)
    if date_to:
        base += " and coalesce(published_at::date, created_at::date) <= :d2"
        params["d2"] = str(date_to)

    # title filter (substring)
    if title_q.strip():
        base += " and unaccent(lower(title)) like unaccent(lower(:tq))"
        params["tq"] = f"%{title_q.strip()}%"

    # keyword filter (FTS)
    if keyword_q.strip():
        base += " and text_tsv @@ plainto_tsquery('simple', :kq)"
        params["kq"] = keyword_q.strip()

    # order: if title provided, prefer closest titles using trigram similarity
    if title_q.strip():
        base += " order by similarity(unaccent(lower(title)), unaccent(lower(:tq2))) desc nulls last, id desc"
        params["tq2"] = title_q.strip()
    else:
        base += " order by coalesce(published_at, created_at) desc nulls last, id desc"

    base += " limit :lim"

    with engine.begin() as con:
        return pd.read_sql(text(base), con, params=params)


def delete_document(doc_id: int) -> bool:
    with engine.begin() as con:
        row = con.execute(text("select storage_path, cover_path from posts where id=:i"),
                          {"i": doc_id}).fetchone()
        if not row:
            return False
        storage_path, cover_path = row

        # delete DB row
        con.execute(text("delete from posts where id=:i"), {"i": doc_id})

    # delete files from storage (best-effort; ignore errors)
    try:
        if storage_path:
            sb.storage.from_("docs").remove([storage_path])
    except Exception:
        pass
    try:
        if cover_path:
            sb.storage.from_("covers").remove([cover_path])
    except Exception:
        pass

    return True

# ---- UI ----
tab_search, tab_upload = st.tabs(["🔎 Search", "⬆️ Upload"])

with tab_search:
    st.markdown("### Recherche")
    colA, colB, colC = st.columns([2,2,1])
    with colA:
        title_q = st.text_input("Titre (contient)", "")
    with colB:
        keyword_q = st.text_input("Mots-clés (texte intégral)", "")
    with colC:
        doc_types = st.multiselect("Type", ["html","pdf"], default=["html","pdf"])

    colD, colE, colF = st.columns(3)
    with colD:
        date_from = st.date_input("Date de début", value=None)
    with colE:
        date_to = st.date_input("Date de fin", value=None)
    with colF:
        limit = st.number_input("Max résultats", min_value=10, max_value=2000, value=200)

    if st.button("Rechercher", type="primary") or (not title_q and not keyword_q):
        results = search_docs_advanced(
            title_q=title_q,
            keyword_q=keyword_q,
            date_from=date_from,
            date_to=date_to,
            doc_types=doc_types,
            limit=int(limit)
        )

        # add download link for PDFs
        if not results.empty:
            def mk_link(row):
                if row["doc_type"] == "pdf" and row["storage_path"]:
                    url = signed_download_url(row["storage_path"])
                    return f"[Télécharger]({url})" if url else ""
                return ""
            results["télécharger"] = results.apply(mk_link, axis=1)

        st.write(f"Résultats : {len(results)}")
        st.dataframe(results, use_container_width=True, height=360)

        sel = st.number_input("Ouvrir l’ID", min_value=1, step=1,
                              value=int(results["id"].iloc[0]) if len(results) else 1)
        if st.button("Ouvrir"):
            r = fetch_post(int(sel))
            if r is None:
                st.warning("Document introuvable.")
            else:
                st.subheader(r["title"])
                st.caption(f"{r['doc_type']} • {r.get('published_at')} • id={r['id']}")
                if r.get("editor"):
                    st.caption(f"Éditeur : {r['editor']}")
                if r.get("summary"):
                    st.markdown(f"**Résumé :** {r['summary']}")
                if r.get("cover_path"):
                    cu = signed_download_url(r["cover_path"])
                    if cu:
                        st.image(cu, caption="Affiche / Couverture", use_container_width=False)
                if r.get("source_url"):
                    st.markdown(f"🔗 [Source]({r['source_url']})")
                if r["doc_type"] == "html":
                    st.markdown("### HTML")
                    st.components.v1.html(r.get("raw_html") or "<p>(no content)</p>", height=500, scrolling=True)
                else:
                    st.markdown("### Texte extrait")
                    st.code((r.get("text_content") or "")[:8000])


with tab_upload:
    st.markdown("### Ajouter un document")
    title = st.text_input("Titre *")
    editor = st.text_input("Éditeur (optionnel)")
    summary = st.text_area("Résumé (optionnel)")
    published = st.date_input("Date de publication (optionnel)", value=None)
    source_url = st.text_input("Lien source (optionnel)")
    kind = st.radio("Type de document", ["HTML", "PDF"], horizontal=True)

    cover_file = st.file_uploader("Image de couverture (optionnel)", type=["jpg","jpeg","png"])

    cover_path = None
    if cover_file:
        cover_bytes = cover_file.read()
        cover_path = upload_cover_to_storage(
            cover_bytes,
            suggested_name=cover_file.name,
            published_at=(published if published else None)
        )

    if kind == "HTML":
        html_file = st.file_uploader("Fichier HTML", type=["html","htm"])
        html_paste = st.text_area("Ou coller le HTML")
        if st.button("Enregistrer HTML"):
            if not title or (not html_file and not html_paste.strip()):
                st.error("Titre et HTML requis.")
            else:
                html = (html_file.read().decode("utf-8", errors="replace") if html_file else html_paste)
                text_content = clean_html_to_text(html)
                insert_row(
                    title=title, doc_type="html",
                    published_at=(datetime.combine(published, datetime.min.time()) if published else None),
                    source_url=source_url, raw_html=html,
                    text_content=text_content, storage_path=None,
                    editor=editor, summary=summary, cover_path=cover_path
                )
                st.success("HTML enregistré ✅")

    else:
        pdf_file = st.file_uploader("Fichier PDF", type=["pdf"])
        if st.button("Enregistrer PDF"):
            if not title or not pdf_file:
                st.error("Titre et PDF requis.")
            else:
                pdf_bytes = pdf_file.read()
                storage_path = upload_pdf_to_storage(
                    pdf_bytes, suggested_name=pdf_file.name,
                    published_at=(published if published else None)
                )
                try:
                    text_content = extract_text(io.BytesIO(pdf_bytes)) or ""
                except Exception:
                    text_content = ""
                insert_row(
                    title=title, doc_type="pdf",
                    published_at=(datetime.combine(published, datetime.min.time()) if published else None),
                    source_url=source_url, raw_html=None,
                    text_content=text_content, storage_path=storage_path,
                    editor=editor, summary=summary, cover_path=cover_path
                )
                st.success("PDF enregistré ✅")

tab_admin, = st.tabs(["🔐 Admin"])
with tab_admin:
    st.markdown("### Suppression d’un document (réservé)")
    if "is_admin" not in st.session_state:
        st.session_state["is_admin"] = False

    if not st.session_state["is_admin"]:
        pw = st.text_input("Mot de passe admin", type="password")
        if st.button("Se connecter"):
            if pw == st.secrets.get("ADMIN_PASSWORD"):
                st.session_state["is_admin"] = True
                st.success("Connecté en tant qu’admin.")
            else:
                st.error("Mot de passe incorrect.")
    else:
        del_id = st.number_input("ID du document à supprimer", min_value=1, step=1)
        if st.button("Supprimer définitivement"):
            ok = delete_document(int(del_id))
            if ok:
                st.success(f"Document {int(del_id)} supprimé.")
            else:
                st.error("Échec : ID introuvable.")
