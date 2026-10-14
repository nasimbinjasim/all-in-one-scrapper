import os
import re
import time
import tempfile
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import trafilatura
from ebooklib import epub
import streamlit as st

st.set_page_config(
    page_title="Universal Web to EPUB & Markdown",
    page_icon="🌐",
    layout="wide"
)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
}

def clean_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip() or "universal_ebook"

def detect_language(text):
    """টেক্সটে বাংলা অক্ষর থাকলে ভাষা 'bn' এবং না থাকলে 'en' নির্ধারণ করবে"""
    if re.search(r'[\u0980-\u09FF]', text):
        return 'bn'
    return 'en'

def extract_universal_metadata(soup, url):
    """যেকোনো ওয়েবসাইট থেকে টাইটেল, লেখক এবং কভার ছবি বের করা"""
    # ১. টাইটেল শনাক্তকরণ
    title = None
    og_title = soup.find('meta', property='og:title') or soup.find('meta', attrs={'name': 'twitter:title'})
    if og_title and og_title.get('content'):
        title = og_title['content'].strip()
    if not title:
        h1 = soup.find('h1')
        title = h1.get_text(strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "Web Article")

    # ২. লেখক শনাক্তকরণ
    author = None
    for meta_name in ['author', 'article:author', 'dc.creator', 'byl']:
        tag = soup.find('meta', attrs={'name': meta_name}) or soup.find('meta', property=meta_name)
        if tag and tag.get('content'):
            author = tag['content'].strip()
            break

    if not author:
        for sep in [' – ', ' - ', ' — ', ' | ']:
            if sep in title:
                parts = [p.strip() for p in title.split(sep) if p.strip()]
                if len(parts) >= 2:
                    title = parts[0]
                    author = parts[-1]
                    break

    if not author:
        author = urlparse(url).netloc.replace("www.", "")

    # ৩. কভার ছবি শনাক্তকরণ
    img_url = None
    og_img = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
    if og_img and og_img.get('content'):
        img_url = og_img['content']
    else:
        article_area = soup.find('article') or soup.find('main') or soup.body
        if article_area:
            first_img = article_area.find('img')
            if first_img:
                img_url = first_img.get('data-src') or first_img.get('src')

    cover_bytes, cover_ext = None, None
    if img_url:
        img_url = urljoin(url, img_url)
        try:
            r = requests.get(img_url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            ctype = r.headers.get('Content-Type', '').lower()
            ext = '.png' if 'png' in ctype else '.jpg'
            cover_bytes, cover_ext = r.content, f"cover{ext}"
        except Exception:
            pass

    return title, author, cover_bytes, cover_ext

def find_main_container(soup):
    """যেকোনো ওয়েবসাইটের মূল আর্টিকেল কন্টেইনার খুঁজে বের করার হিউরিস্টিক অ্যালগরিদম"""
    for selector in ['article', 'main', '[role="main"]', '.entry-content', '.post-content', '.article-body', '.content', '#content']:
        found = soup.select_one(selector)
        if found and len(found.get_text(strip=True)) > 150:
            return found

    # প্যারাগ্রাফের ঘনত্বের ওপর ভিত্তি করে সেরা div নির্বাচন
    best_div = soup.body
    max_score = 0
    for div in soup.find_all(['div', 'section']):
        p_tags = div.find_all('p', recursive=False)
        score = sum(len(p.get_text(strip=True)) for p in p_tags)
        if score > max_score:
            max_score = score
            best_div = div
    return best_div or soup.body

def discover_sub_links(url, filter_keyword=""):
    """সূচিপত্র বা ইনডেক্স পেজ থেকে সব অধ্যায় বা সাব-পেজের লিংক খুঁজে বের করা"""
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'lxml')

    title, author, cover_bytes, cover_ext = extract_universal_metadata(soup, url)
    container = find_main_container(soup)

    base_domain = urlparse(url).netloc
    normalized_base = url.split('#')[0].rstrip('/')
    links = []
    seen = set()

    ignore_words = [
        '/tag/', '/category/', '/author/', '/login', '/signup', '/register',
        '/cart', '/checkout', 'facebook.com', 'twitter.com', 'whatsapp',
        'linkedin.com', 'pinterest.com', 'javascript:', 'mailto:', '.jpg', '.png', '.pdf'
    ]

    for a in container.find_all('a', href=True):
        raw_href = a['href'].strip()
        text = a.get_text(strip=True)
        if not raw_href or raw_href.startswith('#') or len(text) < 2:
            continue

        full_url = urljoin(url, raw_href).split('#')[0].rstrip('/')
        parsed = urlparse(full_url)

        if parsed.netloc != base_domain or full_url == normalized_base:
            continue
        if any(w in full_url.lower() for w in ignore_words):
            continue
        if filter_keyword and filter_keyword.lower() not in full_url.lower():
            continue

        if full_url not in seen:
            seen.add(full_url)
            links.append((text, full_url))

    if not links:
        links.append((title, url))

    return {
        "title": title,
        "author": author,
        "cover_bytes": cover_bytes,
        "cover_ext": cover_ext,
        "links": links
    }

def extract_universal_article(url, fallback_title="অধ্যায়"):
    """Trafilatura এবং BeautifulSoup ফলব্যাক দিয়ে যেকোনো পেজের মূল টেক্সট সংগ্রহ করা"""
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html_text = resp.text

    soup = BeautifulSoup(html_text, 'lxml')
    h1 = soup.find('h1')
    ch_title = h1.get_text(strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else fallback_title)

    # ১ম প্রচেষ্টা: Trafilatura দিয়ে ক্লিন মার্কডাউন টেক্সট বের করা
    extracted_md = trafilatura.extract(
        html_text,
        include_formatting=True,
        include_links=False,
        include_images=False,
        output_format='markdown'
    )

    paragraphs = []
    if extracted_md and len(extracted_md.strip()) > 100:
        for block in extracted_md.split('\n\n'):
            line = block.strip()
            if not line:
                continue
            if line.startswith('#'):
                clean_h = line.lstrip('#').strip()
                if clean_h != ch_title:
                    paragraphs.append(('h3', clean_h))
            elif line.startswith('>'):
                paragraphs.append(('blockquote', line.lstrip('>').strip()))
            elif line.startswith(('- ', '* ')):
                for item in line.split('\n'):
                    paragraphs.append(('li', item.lstrip('-* ').strip()))
            else:
                paragraphs.append(('p', line))
        return ch_title, paragraphs

    # ২য় প্রচেষ্টা (Fallback): হিউরিস্টিক DOM ক্লিনিং
    container = find_main_container(soup)
    for bad in container.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form', 'iframe', 'ins']):
        bad.decompose()

    for elem in container.find_all(['p', 'h2', 'h3', 'h4', 'blockquote', 'li', 'pre']):
        txt = elem.get_text(strip=True)
        if txt:
            paragraphs.append((elem.name, txt))

    if not paragraphs:
        raw_lines = [l.strip() for l in container.get_text(separator='\n').split('\n') if l.strip()]
        paragraphs = [('p', l) for l in raw_lines]

    return ch_title, paragraphs

def compile_epub_and_md(book_title, author_name, cover_bytes, cover_ext, chapters_data):
    """Markdown এবং EPUB ফাইল মেমোরিতে তৈরি করা"""
    sample_text = book_title + " " + " ".join(
        p[1] for ch in chapters_data[:2] for p in ch[1][:5]
    )
    lang = detect_language(sample_text)

    book = epub.EpubBook()
    book.set_identifier(f"uid_{int(time.time())}")
    book.set_title(book_title)
    book.set_language(lang)
    book.add_author(author_name)

    if cover_bytes and cover_ext:
        book.set_cover(cover_ext, cover_bytes)

    style = '''
    body { font-family: "Kalpurush", "SolaimanLipi", "Noto Sans Bengali", Georgia, serif; line-height: 1.75; padding: 4%; }
    h1, h2, h3 { color: #1e293b; border-bottom: 1px solid #e2e8f0; padding-bottom: 5px; }
    .title-page { text-align: center; margin-top: 18%; }
    p { text-align: justify; margin-bottom: 1em; }
    blockquote { font-style: italic; border-left: 4px solid #94a3b8; margin-left: 0; padding-left: 14px; color: #334155; }
    pre { background: #f1f5f9; padding: 10px; border-radius: 6px; overflow-x: auto; font-family: monospace; }
    '''
    nav_css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style)
    book.add_item(nav_css)

    epub_chapters = []
    intro_ch = epub.EpubHtml(title="Title Page", file_name="intro.xhtml", lang=lang)
    intro_ch.content = f"""
    <html><head><link rel='stylesheet' href='style/nav.css' type='text/css'/></head>
    <body><div class='title-page'><h1>{book_title}</h1><p>✍️ <strong>{author_name}</strong></p></div></body></html>
    """
    intro_ch.add_item(nav_css)
    book.add_item(intro_ch)
    epub_chapters.append(intro_ch)

    md_lines = [f"# {book_title}\n", f"**Author / Source:** {author_name}\n\n---\n\n"]

    for idx, (ch_title, paragraphs) in enumerate(chapters_data, start=1):
        md_lines.append(f"## {ch_title}\n\n")
        html_body = f"<h2>{ch_title}</h2>"

        for tag, text in paragraphs:
            if tag in ['h2', 'h3', 'h4']:
                md_lines.append(f"### {text}\n\n")
                html_body += f"<h3>{text}</h3>"
            elif tag == 'blockquote':
                md_lines.append(f"> {text}\n\n")
                html_body += f"<blockquote>{text}</blockquote>"
            elif tag == 'li':
                md_lines.append(f"- {text}\n")
                html_body += f"<p>• {text}</p>"
            elif tag == 'pre':
                md_lines.append(f"```\n{text}\n```\n\n")
                html_body += f"<pre>{text}</pre>"
            else:
                md_lines.append(f"{text}\n\n")
                html_body += f"<p>{text}</p>"

        md_lines.append("\n---\n\n")

        epub_ch = epub.EpubHtml(title=ch_title, file_name=f"chap_{idx:03d}.xhtml", lang=lang)
        epub_ch.content = f"<html><head><link rel='stylesheet' href='style/nav.css' type='text/css'/></head><body>{html_body}</body></html>"
        epub_ch.add_item(nav_css)
        book.add_item(epub_ch)
        epub_chapters.append(epub_ch)

    book.toc = tuple(epub_chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ['cover', 'nav'] + epub_chapters if (cover_bytes and cover_ext) else ['nav'] + epub_chapters

    with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
        tmp_path = tmp.name

    epub.write_epub(tmp_path, book, {})
    with open(tmp_path, "rb") as f:
        epub_bytes = f.read()
    os.remove(tmp_path)

    return "".join(md_lines), epub_bytes


# ==================== STREAMLIT UI ====================

st.title("🌐 Universal Web ➔ EPUB & Markdown Creator")
st.markdown("যেকোনো ওয়েবসাইটের আর্টিকেল, টিউটোরিয়াল বা অনলাইন বই থেকে সরাসরি **EPUB (.epub)** এবং **Markdown (.md)** তৈরি করুন।")

if "uni_info" not in st.session_state:
    st.session_state.uni_info = None
if "uni_files" not in st.session_state:
    st.session_state.uni_files = None

mode = st.radio(
    "কাজের মোড নির্বাচন করুন:",
    [
        "১. সিঙ্গেল আর্টিকেল / পেজ (Single URL)",
        "২. মাল্টি-চ্যাপ্টার ইনডেক্স পেজ (Index / Table of Contents URL)",
        "৩. একাধিক লিংকের তালিকা (Custom Batch URLs)"
    ],
    horizontal=True
)

with st.sidebar:
    st.header("⚙️ অ্যাডভান্সড অপশন")
    delay_sec = st.slider("রিকোয়েস্ট বিরতি (সেকেন্ড)", 0.2, 3.0, 0.6, 0.2)
    url_filter = st.text_input("URL কীওয়ার্ড ফিল্টার (ঐচ্ছিক):", placeholder="যেমন: /chapter/ বা /docs/")

if mode.startswith("৩."):
    batch_input = st.text_area(
        "প্রতি লাইনে একটি করে লিংক (URL) দিন:",
        height=140,
        placeholder="https://example.com/part-1\nhttps://example.com/part-2"
    )
    if st.button("🔍 লিংকগুলো প্রস্তুত করুন", type="primary"):
        urls = [u.strip() for u in batch_input.split('\n') if u.strip().startswith("http")]
        if not urls:
            st.error("⚠️ অন্তত একটি সঠিক লিংক দিন!")
        else:
            with st.spinner("প্রথম লিংক থেকে মেটাডেটা লোড হচ্ছে..."):
                resp = requests.get(urls[0], headers=HEADERS, timeout=15)
                soup = BeautifulSoup(resp.text, 'lxml')
                title, author, cover_bytes, cover_ext = extract_universal_metadata(soup, urls[0])
                st.session_state.uni_info = {
                    "title": title,
                    "author": author,
                    "cover_bytes": cover_bytes,
                    "cover_ext": cover_ext,
                    "links": [(f"পর্ব {i+1}", u) for i, u in enumerate(urls)]
                }
                st.session_state.uni_files = None
else:
    target_url = st.text_input("ওয়েবপেজের লিংক (URL) পেস্ট করুন:", placeholder="https://...")
    if st.button("🔍 পেজ বিশ্লেষণ করুন", type="primary"):
        if not target_url.startswith("http"):
            st.error("⚠️ অনুগ্রহ করে সঠিক URL দিন (http বা https সহ)।")
        else:
            with st.spinner("ওয়েবপেজ বিশ্লেষণ করা হচ্ছে..."):
                try:
                    if mode.startswith("১."):
                        resp = requests.get(target_url.strip(), headers=HEADERS, timeout=20)
                        resp.raise_for_status()
                        soup = BeautifulSoup(resp.text, 'lxml')
                        title, author, cover_bytes, cover_ext = extract_universal_metadata(soup, target_url)
                        st.session_state.uni_info = {
                            "title": title,
                            "author": author,
                            "cover_bytes": cover_bytes,
                            "cover_ext": cover_ext,
                            "links": [(title, target_url.strip())]
                        }
                    else:
                        st.session_state.uni_info = discover_sub_links(target_url.strip(), url_filter.strip())
                    st.session_state.uni_files = None
                except Exception as e:
                    st.error(f"লিংক লোড করতে সমস্যা হয়েছে: {e}")

# প্রিভিউ এবং বিল্ড সেকশন
if st.session_state.uni_info:
    info = st.session_state.uni_info
    st.divider()

    col1, col2 = st.columns([1, 3])
    with col1:
        if info["cover_bytes"]:
            st.image(info["cover_bytes"], caption="সনাক্তকৃত কভার", use_container_width=True)
        else:
            st.info("🖼️ কভার ছবি পাওয়া যায়নি")
        custom_cover = st.file_uploader("কাস্টম কভার আপলোড করুন (ঐচ্ছিক)", type=["jpg", "jpeg", "png"])
        if custom_cover:
            info["cover_bytes"] = custom_cover.read()
            info["cover_ext"] = "cover.png" if custom_cover.name.endswith(".png") else "cover.jpg"

    with col2:
        c_title, c_author = st.columns(2)
        with c_title:
            final_title = st.text_input("বই / আর্টিকেলের নাম:", value=info["title"])
        with c_author:
            final_author = st.text_input("লেখক / ওয়েবসাইট:", value=info["author"])

        all_options = [f"{i+1}. {name} ({url})" for i, (name, url) in enumerate(info["links"])]
        selected_options = st.multiselect(
            f"অধ্যায় বা পেজ নির্বাচন করুন (মোট {len(all_options)} টি পাওয়া গেছে):",
            options=all_options,
            default=all_options
        )

        if st.button("🚀 ই-বুক (.epub) ও Markdown (.md) তৈরি করুন", type="primary", use_container_width=True):
            if not selected_options:
                st.warning("অন্তত ১টি পেজ নির্বাচন করুন!")
            else:
                selected_indices = [all_options.index(opt) for opt in selected_options]
                chosen_links = [info["links"][i] for i in selected_indices]

                chapters_data = []
                pbar = st.progress(0, text="এক্সট্র্যাকশন শুরু হচ্ছে...")
                status = st.empty()

                try:
                    for idx, (ch_name, ch_url) in enumerate(chosen_links, start=1):
                        status.info(f"⏳ ডাউনলোড হচ্ছে [{idx}/{len(chosen_links)}]: **{ch_name}**")
                        ch_title, paras = extract_universal_article(ch_url, ch_name)
                        chapters_data.append((ch_title, paras))
                        pbar.progress(idx / len(chosen_links))
                        if idx < len(chosen_links):
                            time.sleep(delay_sec)

                    md_text, epub_bytes = compile_epub_and_md(
                        final_title, final_author, info["cover_bytes"], info["cover_ext"], chapters_data
                    )
                    safe_fn = clean_filename(final_title)
                    st.session_state.uni_files = {
                        "md": md_text,
                        "epub": epub_bytes,
                        "fn": safe_fn
                    }
                    status.success("✅ সফলভাবে তৈরি হয়েছে! নিচে থেকে ফাইল ডাউনলোড করুন।")
                except Exception as e:
                    status.error(f"❌ ত্রুটি ঘটেছে: {e}")

if st.session_state.uni_files:
    f_data = st.session_state.uni_files
    st.divider()
    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            f"📚 Download EPUB ({f_data['fn']}.epub)",
            data=f_data["epub"],
            file_name=f"{f_data['fn']}.epub",
            mime="application/epub+zip",
            type="primary",
            use_container_width=True
        )
    with d2:
        st.download_button(
            f"📄 Download Markdown ({f_data['fn']}.md)",
            data=f_data["md"].encode("utf-8"),
            file_name=f"{f_data['fn']}.md",
            mime="text/markdown",
            use_container_width=True
        )
    with st.expander("👁️ মার্কডাউন প্রিভিউ"):
        st.text_area("Preview", value=f_data["md"][:4000], height=250)