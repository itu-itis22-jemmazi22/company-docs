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
if "is_admin" not in st.session_state:
    st.session_state["is_admin"] = False

def require_admin():
    if not st.session_state["is_admin"]:
        st.info("⬆️ L’upload est réservé aux administrateurs. Ouvrez l’onglet **Admin** pour vous connecter.")
        st.stop()

# ---- Helpers ----
def normalize_url(u: str | None) -> str | None:
    if not u: return None
    u = u.strip()
    if not u: return None
    if not u.startswith(("http://", "https://")):
        return "https://" + u
    return u

from bs4 import BeautifulSoup

def extract_title_from_html(html: str) -> str | None:
    soup = BeautifulSoup(html or "", "html.parser")
    t = soup.find("title")
    if t and t.text.strip(): 
        return t.text.strip()
    h1 = soup.find("h1")
    if h1 and h1.text.strip():
        return h1.text.strip()
    return None

import re
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

def guess_year_from_text(txt: str) -> int | None:
    if not txt: return None
    m = YEAR_RE.search(txt)
    return int(m.group(0)) if m else None

def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "cat"

def upsert_categories(names: list[str]) -> list[int]:
    ids = []
    with engine.begin() as con:
        for raw in names:
            name = raw.strip()
            if not name: continue
            slug = slugify(name)
            row = con.execute(text("""
                insert into categories(name, slug) values (:n, :s)
                on conflict (name) do update set name=excluded.name
                returning id
            """), {"n": name, "s": slug}).fetchone()
            ids.append(int(row[0]))
    return ids

def link_post_categories(post_id: int, cat_ids: list[int]) -> None:
    with engine.begin() as con:
        for cid in cat_ids:
            con.execute(text("""
                insert into post_categories(post_id, category_id)
                values (:p, :c) on conflict do nothing
            """), {"p": post_id, "c": cid})


def clean_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script","style","noscript","svg","nav","header","footer"]):
        t.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n", strip=True).splitlines()]
    return "\n".join([ln for ln in lines if ln])

def insert_row(title: str, doc_type: str, published_at, source_url,
               raw_html, text_content, storage_path,
               editor, summary, author, isbn, year,
               cover_path, categories: list[str]) -> int:
    with engine.begin() as con:
        row = con.execute(text("""
            insert into posts
              (title, doc_type, published_at, source_url, raw_html, text_content,
               storage_path, editor, summary, author, isbn, year, cover_path)
            values
              (:title, :doc_type, :published_at, :source_url, :raw_html, :text_content,
               :storage_path, :editor, :summary, :author, :isbn, :year, :cover_path)
            returning id;
        """), {
            "title": title.strip(),
            "doc_type": doc_type,
            "published_at": published_at,
            "source_url": normalize_url(source_url),
            "raw_html": raw_html,
            "text_content": text_content,
            "storage_path": storage_path,
            "editor": editor or None,
            "summary": summary or None,
            "author": author or None,
            "isbn": isbn or None,
            "year": int(year) if year else None,
            "cover_path": cover_path or None
        }).fetchone()
        post_id = int(row[0])
    if categories:
        cat_ids = upsert_categories(categories)
        link_post_categories(post_id, cat_ids)
    return post_id



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

def search_docs_advanced(title_q: str, keyword_q: str, year: int | None,
                         doc_types: list[str], cats: list[str], limit: int = 200) -> pd.DataFrame:
    base = """
    select p.id, p.title, p.doc_type,
           coalesce(p.published_at::date, p.created_at::date) as date,
           p.year, p.editor, p.author, p.isbn, p.summary,
           p.source_url, p.storage_path, p.cover_path,
           coalesce(string_agg(c.name, ', ' order by c.name), '') as categories
    from posts p
    left join post_categories pc on pc.post_id = p.id
    left join categories c on c.id = pc.category_id
    where p.doc_type = any(:types)
    """
    params = {"types": doc_types if doc_types else ["html","pdf"], "lim": limit}

    if year:
        base += " and p.year = :yr"
        params["yr"] = int(year)

    if title_q.strip():
        base += " and unaccent(lower(p.title)) like unaccent(lower(:tq))"
        params["tq"] = f"%{title_q.strip()}%"

    if keyword_q.strip():
        base += " and p.text_tsv @@ plainto_tsquery('simple', :kq)"
        params["kq"] = keyword_q.strip()

    if cats:
        base += " and exists (select 1 from post_categories pc2 join categories c2 on c2.id=pc2.category_id where pc2.post_id=p.id and c2.name = any(:cats))"
        params["cats"] = cats

    base += " group by p.id "

    if title_q.strip():
        base += " order by similarity(unaccent(lower(p.title)), unaccent(lower(:tq2))) desc nulls last, p.id desc"
        params["tq2"] = title_q.strip()
    else:
        base += " order by coalesce(p.published_at, p.created_at) desc nulls last, p.id desc"

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
def normalize_url(u: str | None) -> str | None:
    if not u: return None
    u = u.strip()
    if not u: return None
    if not u.startswith(("http://", "https://")):
        return "https://" + u
    return u
import re
def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "cat"

def upsert_categories(names: list[str]) -> list[int]:
    ids = []
    with engine.begin() as con:
        for raw in names:
            name = raw.strip()
            if not name: continue
            slug = slugify(name)
            row = con.execute(text("""
                insert into categories(name, slug) values (:n, :s)
                on conflict (name) do update set name=excluded.name
                returning id
            """), {"n": name, "s": slug}).fetchone()
            ids.append(int(row[0]))
    return ids

def link_post_categories(post_id: int, cat_ids: list[int]) -> None:
    with engine.begin() as con:
        for cid in cat_ids:
            con.execute(text("""
                insert into post_categories(post_id, category_id)
                values (:p, :c) on conflict do nothing
            """), {"p": post_id, "c": cid})
def search_docs_advanced(title_q: str, keyword_q: str, year: int | None,
                         doc_types: list[str], cats: list[str], limit: int = 200) -> pd.DataFrame:
    base = """
    select p.id, p.title, p.doc_type,
           coalesce(p.published_at::date, p.created_at::date) as date,
           p.year, p.editor, p.author, p.isbn, p.summary,
           p.source_url, p.storage_path,
           coalesce(string_agg(c.name, ', ' order by c.name), '') as categories
    from posts p
    left join post_categories pc on pc.post_id = p.id
    left join categories c on c.id = pc.category_id
    where p.doc_type = any(:types)
    """
    params = {"types": doc_types if doc_types else ["html","pdf"], "lim": limit}

    if year:
        base += " and p.year = :yr"
        params["yr"] = int(year)

    if title_q.strip():
        base += " and unaccent(lower(p.title)) like unaccent(lower(:tq))"
        params["tq"] = f"%{title_q.strip()}%"

    if keyword_q.strip():
        base += " and p.text_tsv @@ plainto_tsquery('simple', :kq)"
        params["kq"] = keyword_q.strip()

    if cats:
        # match any of the selected categories
        base += " and exists (select 1 from post_categories pc2 join categories c2 on c2.id=pc2.category_id where pc2.post_id=p.id and c2.name = any(:cats))"
        params["cats"] = cats

    base += """
    group by p.id
    """

    if title_q.strip():
        base += " order by similarity(unaccent(lower(p.title)), unaccent(lower(:tq2))) desc nulls last, p.id desc"
        params["tq2"] = title_q.strip()
    else:
        base += " order by coalesce(p.published_at, p.created_at) desc nulls last, p.id desc"

    base += " limit :lim"

    with engine.begin() as con:
        return pd.read_sql(text(base), con, params=params)


# ---- UI ----
# Initialize admin state once
if "is_admin" not in st.session_state:
    st.session_state["is_admin"] = False

# Create tabs depending on admin state
if st.session_state["is_admin"]:
    tab_search, tab_upload, tab_admin = st.tabs(["🔎 Search", "⬆️ Upload", "🔐 Admin"])
else:
    tab_search, tab_admin = st.tabs(["🔎 Search", "🔐 Admin"])
    tab_upload = None

with tab_search:
    st.markdown("### Recherche")
    # total count
    with engine.begin() as con:
        total = con.execute(text("select count(*) from posts")).scalar()
    st.caption(f"Total documents : **{total}**")

    # category options
    with engine.begin() as con:
        all_cats = [r[0] for r in con.execute(text("select name from categories order by name")).fetchall()]

    col1, col2, col3 = st.columns([2,2,1])
    with col1:
        title_q = st.text_input("Titre (contient)", "")
    with col2:
        keyword_q = st.text_input("Mots-clés (texte intégral)", "")
    with col3:
        doc_types = st.multiselect("Type", ["html","pdf"], default=["html","pdf"])

    col4, col5 = st.columns([1,3])
    with col4:
        year = st.number_input("Année", min_value=1800, max_value=2100, value=None, step=1, format="%d")
    with col5:
        cats = st.multiselect("Catégories", options=all_cats)

    view_mode = st.radio("Affichage", ["Cartes", "Table"], horizontal=True)

    if st.button("Rechercher", type="primary") or (not title_q and not keyword_q and not year and not cats):
        df = search_docs_advanced(title_q, keyword_q, year, doc_types, cats, limit=500)
        st.write(f"Résultats : {len(df)}")

        def dl_link(row):
            if row["doc_type"] == "pdf" and row["storage_path"]:
                u = signed_download_url(row["storage_path"])
                return f"[Télécharger]({u})" if u else ""
            return ""

        def src_link(row):
            u = normalize_url(row["source_url"])
            return f"[Source]({u})" if u else ""

        if view_mode == "Table":
            if not df.empty:
                df["télécharger"] = df.apply(dl_link, axis=1)
                df["source"] = df.apply(src_link, axis=1)
                st.dataframe(df[["id","title","year","editor","author","isbn","categories","source","télécharger","date","doc_type"]],
                             use_container_width=True, height=420)
        else:
            # Cartes en grille (comme l’exemple)
            if df.empty:
                st.info("Aucun résultat.")
            else:
                cards_per_row = 3
                rows = (len(df) + cards_per_row - 1) // cards_per_row
                idx = 0
                for _ in range(rows):
                    cols = st.columns(cards_per_row, gap="large")
                    for c in cols:
                        if idx >= len(df): 
                            break
                        row = df.iloc[idx]
                        idx += 1
                        with c:
                            # image de couverture si dispo
                            if row.get("cover_path"):
                                cu = signed_download_url(row["cover_path"])
                                if cu: st.image(cu, use_container_width=True)
                            st.markdown(f"**{row['title']}**")
                            meta = []
                            if row.get("year"): meta.append(str(row["year"]))
                            if row.get("editor"): meta.append(row["editor"])
                            if row.get("author"): meta.append(row["author"])
                            if row.get("isbn"): meta.append(f"ISBN: {row['isbn']}")
                            if meta: st.caption(" • ".join(meta))
                            if row.get("summary"):
                                st.write((row["summary"][:220] + "…") if len(row["summary"])>220 else row["summary"])
                            # actions
                            links = []
                            sl = src_link(row)
                            if sl: links.append(sl)
                            dl = dl_link(row)
                            if dl: links.append(dl)
                            if links:
                                st.markdown(" | ".join(links))



if tab_upload is not None:
    with tab_upload:
        if not st.session_state["is_admin"]:
            st.info("⬆️ L’upload est réservé aux administrateurs. Ouvrez l’onglet **Admin** pour vous connecter.")
            st.stop()

        st.markdown("### Ajouter un document")

        # ---- 1) Choose mode OUTSIDE any form (prevents widget reuse) ----
        MODE_HTML = "HTML (fichier / coller)"
        MODE_PDF  = "PDF (fichier)"
        kind = st.radio("Type de document", [MODE_HTML, MODE_PDF], index=0, horizontal=True, key="kind_radio_global")

        # ---- 2) Common fields (we'll read them inside each form via session state) ----
        title   = st.text_input("Titre *", key="common_title")
        year    = st.number_input("Année (du document)", min_value=1800, max_value=2100, value=None, step=1,
                                  format="%d", key="common_year")
        author  = st.text_input("Auteur", key="common_author")
        editor  = st.text_input("Éditeur", key="common_editor")
        isbn    = st.text_input("ISBN", key="common_isbn")
        summary = st.text_area("Résumé", key="common_summary")
        source_url = st.text_input("Lien (URL si pas de fichier)", key="common_source")
        cover_file = st.file_uploader("Image de couverture (jpg/png)", type=["jpg","jpeg","png"], key="common_cover")
        cats_text = st.text_input("Catégories (séparées par des virgules)", key="common_cats")

        # helper to upload cover now (so both forms can use it)
        cover_path = None
        if cover_file is not None:
            cover_bytes = cover_file.read()
            cover_path = upload_cover_to_storage(
                cover_bytes,
                suggested_name=cover_file.name,
                published_at=(datetime(year,1,1) if year else None)
            )

        categories = [c.strip() for c in (cats_text or "").split(",") if c.strip()]

        # ---- 3) Show exactly ONE form depending on 'kind' ----
        if kind == MODE_HTML:
            with st.form("form_html", clear_on_submit=True):
                html_file = st.file_uploader("Fichier HTML", type=["html","htm"], key="html_uploader_unique")
                html_paste = st.text_area("Ou coller le HTML", key="html_paste_unique")
                submit_html = st.form_submit_button("Enregistrer (HTML)")

            if submit_html:
                if not (html_file or html_paste.strip() or source_url.strip()):
                    st.error("Fournir un fichier HTML, ou coller le HTML, ou un lien source.")
                    st.stop()

                raw_html = (html_file.read().decode("utf-8", errors="replace") if html_file else html_paste)
                text_content = clean_html_to_text(raw_html) if raw_html else ""

                # auto fill title / year if missing
                if not title:
                    auto = extract_title_from_html(raw_html)
                    if auto: title = auto
                if not year:
                    y = guess_year_from_text(text_content)
                    if y: year = y

                if not title:
                    st.error("Le champ Titre est requis (impossible de le déduire automatiquement).")
                    st.stop()

                insert_row(
                    title=title, doc_type="html",
                    published_at=(datetime(year,1,1) if year else None),
                    source_url=source_url, raw_html=raw_html, text_content=text_content,
                    storage_path=None,
                    editor=editor, summary=summary, author=author, isbn=isbn, year=year,
                    cover_path=cover_path, categories=categories
                )
                st.success("Document HTML enregistré ")

        elif kind == MODE_PDF:
            with st.form("form_pdf", clear_on_submit=True):
                pdf_file = st.file_uploader("Fichier PDF", type=["pdf"], key="pdf_uploader_unique")
                submit_pdf = st.form_submit_button("Enregistrer (PDF)")

            if submit_pdf:
                if not (pdf_file or source_url.strip()):
                    st.error("Télécharger un PDF ou fournir un lien source.")
                    st.stop()

                storage_path = None
                text_content = ""

                if pdf_file:
                    pdf_bytes = pdf_file.read()
                    storage_path = upload_pdf_to_storage(
                        pdf_bytes, suggested_name=pdf_file.name,
                        published_at=(datetime(year,1,1) if year else None)
                    )
                    try:
                        text_content = extract_text(io.BytesIO(pdf_bytes)) or ""
                    except Exception:
                        text_content = ""

                # auto title/year if missing
                if not title:
                    if pdf_file:
                        base = pdf_file.name.rsplit(".", 1)[0]
                        title = base.replace("_"," ").replace("-"," ").strip()
                    if not title and text_content:
                        first_line = next((ln.strip() for ln in text_content.splitlines() if ln.strip()), "")
                        if len(first_line) > 5:
                            title = first_line[:180]
                if not year:
                    y = guess_year_from_text(text_content)
                    if y: year = y

                if not title:
                    st.error("Le champ Titre est requis (impossible de le déduire automatiquement).")
                    st.stop()

                insert_row(
                    title=title, doc_type="pdf",
                    published_at=(datetime(year,1,1) if year else None),
                    source_url=source_url, raw_html=None, text_content=text_content,
                    storage_path=storage_path,
                    editor=editor, summary=summary, author=author, isbn=isbn, year=year,
                    cover_path=cover_path, categories=categories
                )
                st.success("Document PDF enregistré ✅")


with tab_admin:
    st.markdown("### Zone administrateur")

    if st.session_state["is_admin"]:
        st.success("Connecté en tant qu’administrateur.")
        if st.button("Se déconnecter"):
            st.session_state["is_admin"] = False
            st.rerun()


        # Delete section
        st.markdown("#### Supprimer un document")
        del_id = st.number_input("ID du document à supprimer", min_value=1, step=1)
        if st.button("Supprimer définitivement"):
            ok = delete_document(int(del_id))
            if ok:
                st.success(f"Document {int(del_id)} supprimé.")
            else:
                st.error("Échec : ID introuvable.")

    else:
        pw = st.text_input("Mot de passe admin", type="password")
        if st.button("Se connecter"):
            if pw == st.secrets.get("ADMIN_PASSWORD"):
                st.session_state["is_admin"] = True
                st.success("Connecté.")
                st.rerun()
            else:
                st.error("Mot de passe incorrect.")


