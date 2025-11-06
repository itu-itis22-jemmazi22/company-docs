import io, os, uuid, re
from datetime import datetime
from typing import Optional

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text
from supabase import create_client, Client

# ---- Page ----
st.set_page_config(page_title="Company Docs", layout="wide")
st.title("📚 Company Docs — Search & Upload (Supabase)")

# ---- Secrets / Setup ----
SB_URL  = st.secrets["SUPABASE_URL"]
SB_ANON = st.secrets["SUPABASE_ANON_KEY"]              # for read (not used directly here)
SB_SVC  = st.secrets["SUPABASE_SERVICE_ROLE_KEY"]      # for server-side upload
DB_URL  = st.secrets["DATABASE_URL"]

engine = create_engine(DB_URL, pool_pre_ping=True)
sb: Client = create_client(SB_URL, SB_SVC)

# Storage buckets
BUCKET_PDF   = os.getenv("BUCKET_PDF", "docs")
BUCKET_COVER = os.getenv("BUCKET_COVER", "covers")

# ---- Session state ----
if "is_admin" not in st.session_state:
    st.session_state["is_admin"] = False

# Load-more state (advanced search)
POSTS_STEP_CARDS = 30
POSTS_STEP_TABLE = 200
if "adv_cards_shown" not in st.session_state:
    st.session_state.adv_cards_shown = POSTS_STEP_CARDS
if "adv_table_shown" not in st.session_state:
    st.session_state.adv_table_shown = POSTS_STEP_TABLE
if "adv_last_sig" not in st.session_state:
    st.session_state.adv_last_sig = ""

# ---- Small utils ----
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

def require_admin():
    if not st.session_state["is_admin"]:
        st.info("⬆️ L’upload est réservé aux administrateurs. Ouvrez l’onglet **Admin** pour vous connecter.")
        st.stop()

def normalize_url(u: str | None) -> str | None:
    if not u: return None
    u = u.strip()
    if not u: return None
    if not u.startswith(("http://", "https://")):
        return "https://" + u
    return u

def extract_title_from_html(html: str) -> str | None:
    soup = BeautifulSoup(html or "", "html.parser")
    t = soup.find("title")
    if t and t.text.strip(): 
        return t.text.strip()
    h1 = soup.find("h1")
    if h1 and h1.text.strip():
        return h1.text.strip()
    return None

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
            if not name: 
                continue
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

# ---- Storage helpers ----
def upload_pdf_to_storage(pdf_bytes: bytes, suggested_name: str, published_at: Optional[datetime]) -> str:
    year = (published_at.year if published_at else datetime.utcnow().year)
    month = (published_at.month if published_at else datetime.utcnow().month)
    unique = uuid.uuid4().hex[:8]
    safe = suggested_name.replace(" ", "_").replace("/", "-")
    path = f"pdf/{year:04d}/{month:02d}/{unique}_{safe}"
    sb.storage.from_(BUCKET_PDF).upload(path, pdf_bytes, {"content-type": "application/pdf", "x-upsert": "true"})
    return path

def upload_cover_to_storage(img_bytes: bytes, suggested_name: str, published_at: Optional[datetime]) -> str:
    year = (published_at.year if published_at else datetime.utcnow().year)
    month = (published_at.month if published_at else datetime.utcnow().month)
    unique = uuid.uuid4().hex[:8]
    safe = suggested_name.replace(" ", "_").replace("/", "-")
    path = f"covers/{year:04d}/{month:02d}/{unique}_{safe}"
    sb.storage.from_(BUCKET_COVER).upload(path, img_bytes, {"content-type": "image/jpeg", "x-upsert": "true"})
    return path

def signed_url(bucket: str, storage_path: str, expire_sec: int = 3600) -> Optional[str]:
    try:
        res = sb.storage.from_(bucket).create_signed_url(storage_path, expire_sec)
        return res.get("signedURL") or res.get("signedUrl")
    except Exception:
        return None

# ---- DB helpers ----
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

def delete_document(doc_id: int) -> bool:
    with engine.begin() as con:
        row = con.execute(text("select storage_path, cover_path from posts where id=:i"),
                          {"i": doc_id}).fetchone()
        if not row:
            return False
        storage_path, cover_path = row
        con.execute(text("delete from posts where id=:i"), {"i": doc_id})

    # best-effort: delete storage files
    try:
        if storage_path:
            sb.storage.from_(BUCKET_PDF).remove([storage_path])
    except Exception:
        pass
    try:
        if cover_path:
            sb.storage.from_(BUCKET_COVER).remove([cover_path])
    except Exception:
        pass
    return True

# ---- Advanced search (with Load more) ----
def build_advanced_where(title_q: str, keyword_q: str, year: int | None,
                         doc_types: list[str], cats: list[str]):
    where = ["p.doc_type = any(:types)"]
    params = {"types": doc_types if doc_types else ["html", "pdf"]}

    if year:
        where.append("p.year = :yr")
        params["yr"] = int(year)

    if title_q.strip():
        where.append("unaccent(lower(p.title)) like unaccent(lower(:tq))")
        params["tq"] = f"%{title_q.strip()}%"

    if keyword_q.strip():
        where.append("p.text_tsv @@ plainto_tsquery('simple', :kq)")
        params["kq"] = keyword_q.strip()

    if cats:
        where.append("""
            exists (
              select 1 from post_categories pc2
              join categories c2 on c2.id = pc2.category_id
              where pc2.post_id = p.id and c2.name = any(:cats)
            )
        """)
        params["cats"] = cats

    return " and ".join(where), params

def count_posts_advanced(title_q: str, keyword_q: str, year: int | None,
                         doc_types: list[str], cats: list[str]) -> int:
    where_sql, params = build_advanced_where(title_q, keyword_q, year, doc_types, cats)
    sql = f"""
        select count(distinct p.id)
        from posts p
        left join post_categories pc on pc.post_id = p.id
        left join categories c on c.id = pc.category_id
        where {where_sql}
    """
    with engine.begin() as con:
        return int(con.execute(text(sql), params).scalar() or 0)

def fetch_posts_page(title_q: str, keyword_q: str, year: int | None,
                     doc_types: list[str], cats: list[str],
                     limit: int, offset: int) -> pd.DataFrame:
    where_sql, params = build_advanced_where(title_q, keyword_q, year, doc_types, cats)
    order_sql = (
        " order by similarity(unaccent(lower(p.title)), unaccent(lower(:tq2))) desc nulls last, p.id desc"
        if title_q.strip()
        else " order by coalesce(p.published_at, p.created_at) desc nulls last, p.id desc"
    )
    if title_q.strip():
        params["tq2"] = title_q.strip()

    sql = f"""
        select p.id, p.title, p.doc_type,
               coalesce(p.published_at::date, p.created_at::date) as date,
               p.year, p.editor, p.author, p.isbn, p.summary,
               p.source_url, p.storage_path, p.cover_path,
               coalesce(string_agg(c.name, ', ' order by c.name), '') as categories
        from posts p
        left join post_categories pc on pc.post_id = p.id
        left join categories c on c.id = pc.category_id
        where {where_sql}
        group by p.id
        {order_sql}
        limit :lim offset :off
    """
    params["lim"] = int(limit)
    params["off"] = int(offset)
    with engine.begin() as con:
        return pd.read_sql(text(sql), con, params=params)

# ---- Tabs ----
if st.session_state["is_admin"]:
    tab_search, tab_upload, tab_admin = st.tabs(["🔎 Search", "⬆️ Upload", "🔐 Admin"])
else:
    tab_search, tab_admin = st.tabs(["🔎 Search", "🔐 Admin"])
    tab_upload = None

# ===========================
# 🔎 SEARCH TAB (Advanced)
# ===========================
with tab_search:
    st.markdown("### Recherche avancée")

    # global total
    with engine.begin() as con:
        total_all = con.execute(text("select count(*) from posts")).scalar()
    st.caption(f"Total documents (tous types) : **{total_all}**")

    # categories list
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

    # reset counters if filters changed
    sig = f"{title_q}|{keyword_q}|{year}|{','.join(sorted(doc_types))}|{','.join(sorted(cats))}|{view_mode}"
    if sig != st.session_state.adv_last_sig:
        st.session_state.adv_last_sig = sig
        st.session_state.adv_cards_shown = POSTS_STEP_CARDS
        st.session_state.adv_table_shown = POSTS_STEP_TABLE

    with st.spinner("Calcul du total…"):
        total = count_posts_advanced(title_q, keyword_q, year, doc_types, cats)

    if total == 0:
        st.info("Aucun résultat.")
        st.stop()

    def dl_link(row):
        if row["doc_type"] == "pdf" and row["storage_path"]:
            u = signed_url(BUCKET_PDF, row["storage_path"])
            return f"[Télécharger]({u})" if u else ""
        return ""

    def src_link(row):
        u = normalize_url(row["source_url"])
        return f"[Source]({u})" if u else ""

    if view_mode == "Cartes":
        limit = st.session_state.adv_cards_shown
        with st.spinner("Chargement des cartes…"):
            df = fetch_posts_page(title_q, keyword_q, year, doc_types, cats, limit=limit, offset=0)

        # grid of cards
        cards_per_row = 3
        rows = (len(df) + cards_per_row - 1) // cards_per_row
        idx = 0
        for _ in range(rows):
            cols = st.columns(cards_per_row, gap="large")
            for c in cols:
                if idx >= len(df): break
                row = df.iloc[idx]
                idx += 1
                with c:
                    # cover
                    if row.get("cover_path"):
                        cu = signed_url(BUCKET_COVER, row["cover_path"])
                        if cu: st.image(cu, use_column_width=True)


                    st.markdown(f"**{row['title']}**")
                    st.caption(f"ID: `{row['id']}`")

                    meta = []
                    if row.get("year"):   meta.append(str(row["year"]))
                    if row.get("editor"): meta.append(row["editor"])
                    if row.get("author"): meta.append(row["author"])
                    if row.get("isbn"):   meta.append(f"ISBN: {row['isbn']}")
                    if meta: st.caption(" • ".join(meta))

                    if row.get("summary"):
                        st.write((row["summary"][:220] + "…") if len(row["summary"])>220 else row["summary"])

                    links = [x for x in (src_link(row), dl_link(row)) if x]
                    if links:
                        st.markdown(" | ".join(links))

        if len(df) < total:
            more = min(POSTS_STEP_CARDS, total - len(df))
            if st.button(f"Charger plus ({more})"):
                st.session_state.adv_cards_shown = min(st.session_state.adv_cards_shown + POSTS_STEP_CARDS, total)
                st.rerun()

    else:
        limit = st.session_state.adv_table_shown
        with st.spinner("Chargement du tableau…"):
            df = fetch_posts_page(title_q, keyword_q, year, doc_types, cats, limit=limit, offset=0)

        if not df.empty:
            df["télécharger"] = df.apply(dl_link, axis=1)
            df["source"] = df.apply(src_link, axis=1)
            df = df[["id","title","year","editor","author","isbn","categories","source","télécharger","date","doc_type"]]
            st.caption(f"Total: {total:,} | Affichés: {len(df):,}")
            st.dataframe(df, use_container_width=True, hide_index=True)

        if len(df) < total:
            more = min(POSTS_STEP_TABLE, total - len(df))
            if st.button(f"Charger plus de lignes (+{more})"):
                st.session_state.adv_table_shown = min(st.session_state.adv_table_shown + POSTS_STEP_TABLE, total)
                st.rerun()

# ===========================
# ⬆️ UPLOAD TAB (Admin)
# ===========================
if tab_upload is not None:
    with tab_upload:
        require_admin()
        st.markdown("### Ajouter un document")

        MODE_HTML = "HTML (fichier / coller)"
        MODE_PDF  = "PDF (fichier)"
        kind = st.radio("Type de document", [MODE_HTML, MODE_PDF], index=0, horizontal=True, key="kind_radio_global")

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

        # Upload cover now (so both forms can use it)
        cover_path = None
        if cover_file is not None:
            cover_bytes = cover_file.read()
            cover_path = upload_cover_to_storage(
                cover_bytes,
                suggested_name=cover_file.name,
                published_at=(datetime(year,1,1) if year else None)
            )

        categories = [c.strip() for c in (cats_text or "").split(",") if c.strip()]

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
                st.success("Document HTML enregistré ✅")

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

# ===========================
# 🔐 ADMIN TAB
# ===========================
with tab_admin:
    st.markdown("### Zone administrateur")

    if st.session_state["is_admin"]:
        st.success("Connecté en tant qu’administrateur.")
        if st.button("Se déconnecter"):
            st.session_state["is_admin"] = False
            st.rerun()

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
