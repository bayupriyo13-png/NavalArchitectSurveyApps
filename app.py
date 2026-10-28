"""
Naval Architect Survey Apps — Futuristic Edition

Instalasi:
    pip install streamlit google-genai reportlab

Menjalankan aplikasi:
    streamlit run app.py

Catatan:
- Letakkan folder .streamlit/config.toml (tema gelap) di sebelah app.py.
- PDF aturan private dibaca dari folder Google Drive melalui Service Account read-only.
  * rules dasar.pdf atau rules_dasar.pdf -> dipakai pada SETIAP analisis (wajib ada).
  * PDF aturan per standar -> hanya dipakai bila standarnya dipilih. Lihat
    NAMA_FILE_STANDAR di bawah untuk nama file yang digunakan.
- Environment deployment yang wajib: GEMINI_API_KEY dan GOOGLE_SERVICE_ACCOUNT_JSON.
  GOOGLE_DRIVE_RULES_FOLDER_ID bersifat opsional; default memakai folder yang dikonfigurasi aplikasi.
"""

import html as htmllib
from io import BytesIO
import json
import os
import random
import re
import time
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st
from google import genai
from google.genai import types

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    DRIVE_SDK_TERSEDIA = True
except ImportError:
    DRIVE_SDK_TERSEDIA = False

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    REPORTLAB_TERSEDIA = True
except ImportError:
    REPORTLAB_TERSEDIA = False


st.set_page_config(
    page_title="Naval Architect Survey Apps",
    page_icon="⚓",
    layout="wide",
)

_CATATAN_KEPATUHAN = (
    "Jangan menyatakan kepatuhan pada klausul tertentu tanpa dokumen teknis lengkap."
)

# Teks konteks per standar. Hanya PDF aturan milik standar yang dipilih (ditambah
# rules_dasar.pdf) yang dibaca dan dikirim ke AI.
STANDAR_ACUAN = {
    "BKI (Biro Klasifikasi Indonesia)": (
        f"Gunakan prinsip umum regulasi dan praktik klasifikasi BKI yang relevan. {_CATATAN_KEPATUHAN}"
    ),
    "NCVS (Non-Convention Vessel Standards)": (
        f"Gunakan prinsip umum NCVS yang relevan untuk kapal non-konvensi. {_CATATAN_KEPATUHAN}"
    ),
    "High Speed Craft (HSC)": (
        f"Gunakan prinsip umum HSC Code yang relevan untuk kapal cepat. {_CATATAN_KEPATUHAN}"
    ),
    "Penumpang Tradisional": (
        f"Gunakan prinsip umum aturan kapal penumpang tradisional yang relevan. {_CATATAN_KEPATUHAN}"
    ),
    "Kapal Layar Motor (KLM)": (
        f"Gunakan prinsip umum aturan Kapal Layar Motor yang relevan. {_CATATAN_KEPATUHAN}"
    ),
}

# Pemetaan eksplisit standar ke PDF lokal. Tambahkan file BKI sebagai `bki.pdf`
# bila standar BKI hendak diaktifkan di daftar pilihan.
NAMA_FILE_STANDAR = {
    "BKI (Biro Klasifikasi Indonesia)": "bki.pdf",
    "NCVS (Non-Convention Vessel Standards)": "ncvs.pdf",
    "High Speed Craft (HSC)": "hsc.pdf",
    "Penumpang Tradisional": "penumpang_tradisional.pdf",
    "Kapal Layar Motor (KLM)": "kapal_layar_motor.pdf",
}

NAMA_ATURAN_DASAR = "rules_dasar.pdf"
NAMA_FILE_ATURAN_DASAR = ("rules_dasar.pdf", "rules dasar.pdf")
MAX_TOTAL_PDF_MB = 50
MODEL_GEMINI = "gemini-3.8-flash"
MAKS_PERCOBAAN_ANALISIS = 3
GOOGLE_DRIVE_FOLDER_ID_DEFAULT = "150i6xh0X7JX_dQOHAKVnTIHY4tYZfwQr"
GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def konfigurasi_drive() -> tuple[dict | None, str, str | None]:
    """Membaca konfigurasi private Drive dari environment tanpa mengekspos kredensial."""
    folder_id = ambil_rahasia("GOOGLE_DRIVE_RULES_FOLDER_ID") or GOOGLE_DRIVE_FOLDER_ID_DEFAULT
    raw_json = ambil_rahasia("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        return None, folder_id, "GOOGLE_SERVICE_ACCOUNT_JSON belum dikonfigurasi."
    try:
        info = json.loads(raw_json)
    except json.JSONDecodeError:
        return None, folder_id, "GOOGLE_SERVICE_ACCOUNT_JSON bukan JSON Service Account yang valid."
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        return None, folder_id, "JSON Service Account tidak memuat client_email atau private_key."
    return info, folder_id, None


def _layanan_drive():
    info, _, kesalahan = konfigurasi_drive()
    if kesalahan:
        raise RuntimeError(kesalahan)
    if not DRIVE_SDK_TERSEDIA:
        raise RuntimeError("Paket Google Drive SDK belum terpasang.")
    kredensial = service_account.Credentials.from_service_account_info(
        info,
        scopes=GOOGLE_DRIVE_SCOPES,
    )
    return build("drive", "v3", credentials=kredensial, cache_discovery=False)


@st.cache_data(ttl=600, show_spinner=False)
def daftar_pdf_drive(folder_id: str) -> list[dict]:
    """Daftar metadata PDF langsung dari folder Google Drive private."""
    layanan = _layanan_drive()
    hasil: list[dict] = []
    page_token = None
    while True:
        respons = layanan.files().list(
            q=(f"'{folder_id}' in parents and trashed = false and mimeType = 'application/pdf'"),
            fields="nextPageToken, files(id,name,mimeType,modifiedTime,size,md5Checksum)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        hasil.extend(respons.get("files", []))
        page_token = respons.get("nextPageToken")
        if not page_token:
            return hasil


@st.cache_data(ttl=3600, show_spinner=False)
def unduh_pdf_drive(file_id: str, modified_time: str) -> bytes:
    """Mengunduh PDF ke cache Streamlit; modified_time menginvalidasi cache saat file diperbarui."""
    del modified_time
    layanan = _layanan_drive()
    permintaan = layanan.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = BytesIO()
    pengunduh = MediaIoBaseDownload(buffer, permintaan)
    selesai = False
    while not selesai:
        _, selesai = pengunduh.next_chunk()
    return buffer.getvalue()


def indeks_pdf_drive(daftar_file: list[dict]) -> dict[str, dict]:
    return {str(file["name"]).casefold(): file for file in daftar_file}


def aturan_drive_terpilih(indeks_file: dict[str, dict], standar_nama: str) -> tuple[list[dict], bool, bool]:
    """Menyusun urutan aturan: rules dasar selalu lebih dahulu, lalu aturan standar pilihan."""
    aturan: list[dict] = []
    dasar = next((indeks_file.get(nama.casefold()) for nama in NAMA_FILE_ATURAN_DASAR), None)
    if dasar:
        aturan.append(dasar)

    perlu_standar = standar_nama in NAMA_FILE_STANDAR
    if perlu_standar:
        standar = indeks_file.get(NAMA_FILE_STANDAR[standar_nama].casefold())
        if standar:
            aturan.append(standar)
    return aturan, dasar is not None, (not perlu_standar) or len(aturan) > 1


def standar_dengan_pdf(indeks_file: dict[str, dict]) -> list[str]:
    """Hanya tampilkan standar yang PDF acuannya benar-benar tersedia di Drive."""
    return [
        nama for nama, nama_file in NAMA_FILE_STANDAR.items()
        if nama_file.casefold() in indeks_file
    ]

CHECKLIST_LSA = """
- Peralatan keselamatan jiwa yang ditunjukkan: survival craft/rakit penolong, sekoci,
  rescue boat, jaket penolong, lifebuoy, alat piroteknik, EPIRB/SART bila relevan.
- Penempatan, akses tanpa hambatan, kapasitas/kuantitas indikatif, penandaan dan pencahayaan.
- Muster station, embarkation station, jalur menuju titik tersebut, dan embarkation ladder.
- Davit, launching appliance, perangkat pelepasan, winch, dan area bebas peluncuran bila tampak.
- Kesesuaian antara jumlah orang yang tampak pada GA dan kapasitas LSA: tandai sebagai
  "perlu verifikasi" jika data persons on board atau kapasitas tidak tersedia.
- Kebutuhan bukti tambahan: sertifikat, record servicing, daftar inventaris, kapasitas,
  tata letak keselamatan, dan prosedur/drill.
""".strip()

CHECKLIST_FCP = """
- Kejelasan, judul, nomor dokumen, revisi, legenda, orientasi, dan keterbacaan Fire Control Plan.
- Batas zona kebakaran/fire division, pintu tahan api, penetrasi, dan bukaan kedap api bila ditunjukkan.
- Sistem deteksi/alarm, manual call point, fire alarm panel, serta lokasi pengendalian jarak jauh.
- Posisi fire extinguisher, hydrant, hose box, fire main, fixed fire-fighting system,
  fire pump/emergency pump, dan aksesnya bila ditampilkan.
- Ventilation shutdown, fire damper, fuel shut-off, emergency stop, dan isolasi listrik bila ditunjukkan.
- Jalur pelarian, emergency escape, muster station, tangga/akses vertikal, dan titik kumpul.
- Kebutuhan bukti tambahan: spesifikasi sistem, hydraulic calculation, data detector/alarm,
  daftar alat pemadam, sertifikat, dan inspeksi lapangan.
""".strip()


# ---------------------------------------------------------------------------
# Tema visual: blueprint futuristik bertema desain kapal & perkapalan
# ---------------------------------------------------------------------------

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root{
  --bg:#050b16; --panel:rgba(10,26,48,.72); --line:rgba(34,211,238,.22);
  --cyan:#22d3ee; --blue:#3b82f6; --orange:#ff7a1a; --green:#34d399;
  --text:#d7e6f7; --muted:#7f96b3;
}

html, body, [class*="css"], .stApp { font-family:'Rajdhani', sans-serif; color:var(--text); }
.stApp{
  background:
    radial-gradient(1100px 560px at 85% -10%, rgba(34,211,238,.13), transparent 60%),
    radial-gradient(900px 520px at -10% 35%, rgba(59,130,246,.13), transparent 60%),
    linear-gradient(rgba(34,211,238,.045) 1px, transparent 1px) 0 0/42px 42px,
    linear-gradient(90deg, rgba(34,211,238,.045) 1px, transparent 1px) 0 0/42px 42px,
    var(--bg);
}
[data-testid="stHeader"]{ background:transparent; }
.block-container{ padding-top:1.6rem; max-width:1280px; }

/* Sidebar */
[data-testid="stSidebar"]{
  background:linear-gradient(180deg, rgba(7,18,36,.96), rgba(5,11,22,.98));
  border-right:1px solid var(--line);
}
[data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3{
  font-family:'Orbitron', sans-serif; letter-spacing:.12em; text-transform:uppercase;
  font-size:.85rem !important; color:var(--cyan);
}
label, .stMarkdown p, .stCaption, li { color:var(--text); }
[data-testid="stWidgetLabel"] p{ font-weight:600; letter-spacing:.03em; color:#a9c4e2; }

/* Input */
div[data-baseweb="select"] > div, .stNumberInput input, .stTextArea textarea{
  background:rgba(8,22,42,.85) !important; border:1px solid var(--line) !important;
  color:var(--text) !important; border-radius:10px !important;
  font-family:'JetBrains Mono', monospace; font-size:.9rem;
}
div[data-baseweb="select"] > div:hover, .stNumberInput input:focus, .stTextArea textarea:focus{
  border-color:var(--cyan) !important; box-shadow:0 0 0 1px var(--cyan), 0 0 18px rgba(34,211,238,.25) !important;
}
[data-baseweb="tag"]{ background:rgba(34,211,238,.16) !important; border:1px solid var(--line); }
[data-testid="stExpander"]{ background:var(--panel); border:1px solid var(--line); border-radius:12px; }
[data-testid="stAlert"]{ border-radius:12px; border:1px solid var(--line); background:rgba(10,26,48,.8); }

/* Uploader */
[data-testid="stFileUploader"] section{
  background:linear-gradient(135deg, rgba(34,211,238,.06), rgba(59,130,246,.06));
  border:1.5px dashed rgba(34,211,238,.55); border-radius:16px; padding:1.4rem;
  transition:all .25s ease;
}
[data-testid="stFileUploader"] section:hover{
  border-color:var(--orange); box-shadow:0 0 30px rgba(255,122,26,.18);
}
[data-testid="stFileUploader"] button{
  border:1px solid var(--cyan); background:transparent; color:var(--cyan);
}

/* Tombol */
.stButton > button[kind="primary"]{
  font-family:'Orbitron', sans-serif; letter-spacing:.14em; text-transform:uppercase; font-weight:700;
  border:none; border-radius:14px; padding:.95rem 1rem;
  background:linear-gradient(90deg, #0891b2, #3b82f6 55%, #6366f1);
  color:#fff; box-shadow:0 0 26px rgba(34,211,238,.35), inset 0 0 0 1px rgba(255,255,255,.18);
  transition:transform .2s ease, box-shadow .2s ease;
}
.stButton > button[kind="primary"]:hover{
  transform:translateY(-2px); box-shadow:0 0 44px rgba(34,211,238,.6), inset 0 0 0 1px rgba(255,255,255,.3);
}
.stDownloadButton > button{
  border:1px solid var(--orange); color:var(--orange); background:rgba(255,122,26,.07);
  border-radius:12px; font-family:'Orbitron', sans-serif; letter-spacing:.1em; font-size:.8rem;
}
.stDownloadButton > button:hover{ background:rgba(255,122,26,.18); box-shadow:0 0 24px rgba(255,122,26,.35); color:#ffb070; }

/* Hero */
.hero{
  position:relative; overflow:hidden; border:1px solid var(--line); border-radius:22px;
  padding:2rem 2.2rem 0 2.2rem; margin-bottom:1.2rem;
  background:linear-gradient(135deg, rgba(8,24,46,.9), rgba(5,12,26,.92));
  box-shadow:0 0 60px rgba(34,211,238,.08), inset 0 0 80px rgba(34,211,238,.03);
}
.hero::before{
  content:""; position:absolute; left:0; right:0; top:-40%; height:40%;
  background:linear-gradient(180deg, transparent, rgba(34,211,238,.10), transparent);
  animation:scan 7s linear infinite; pointer-events:none;
}
@keyframes scan{ to{ top:120%; } }
.hero-tag{
  display:inline-block; font-family:'JetBrains Mono', monospace; font-size:.72rem; letter-spacing:.22em;
  color:var(--cyan); border:1px solid var(--line); padding:.28rem .7rem; border-radius:999px;
  background:rgba(34,211,238,.07);
}
.hero h1{
  font-family:'Orbitron', sans-serif; font-weight:900; font-size:2.7rem; line-height:1.1;
  margin:.9rem 0 .4rem 0; letter-spacing:.04em;
  background:linear-gradient(90deg, #e6fbff 0%, #22d3ee 45%, #60a5fa 100%);
  -webkit-background-clip:text; background-clip:text; color:transparent;
}
.hero p{ max-width:640px; color:var(--muted); font-size:1.12rem; margin:0 0 .6rem 0; }
.hero-chips{ display:flex; flex-wrap:wrap; gap:.5rem; margin:.7rem 0 .2rem 0; }
.chip{
  font-family:'JetBrains Mono', monospace; font-size:.74rem; padding:.25rem .65rem; border-radius:8px;
  border:1px solid var(--line); color:#a9c4e2; background:rgba(34,211,238,.05);
}
.chip b{ color:var(--orange); font-weight:500; }
.ship-svg{ width:100%; height:auto; display:block; margin-top:.4rem; }
.draw{ stroke-dasharray:1; stroke-dashoffset:1; animation:draw 3.2s ease forwards; }
.d2{ animation-delay:.5s } .d3{ animation-delay:1s } .d4{ animation-delay:1.5s }
@keyframes draw{ to{ stroke-dashoffset:0; } }
.wave{ animation:drift 9s linear infinite; }
.wave2{ animation:drift 14s linear infinite reverse; opacity:.5; }
@keyframes drift{ from{ transform:translateX(0);} to{ transform:translateX(-120px);} }
.bob{ animation:bob 5s ease-in-out infinite; transform-origin:center; }
@keyframes bob{ 0%,100%{ transform:translateY(0) rotate(0deg);} 50%{ transform:translateY(-4px) rotate(-.4deg);} }
.blink{ animation:blink 1.6s ease-in-out infinite; }
@keyframes blink{ 50%{ opacity:.2; } }

/* Step tracker */
.steps{ display:grid; grid-template-columns:repeat(4,1fr); gap:.8rem; margin:0 0 1.2rem 0; }
.step{
  position:relative; border:1px solid var(--line); border-radius:14px; padding:.8rem 1rem;
  background:var(--panel); backdrop-filter:blur(6px);
}
.step .n{ font-family:'Orbitron', sans-serif; font-size:.72rem; letter-spacing:.2em; color:var(--muted); }
.step .t{ font-weight:700; font-size:1.05rem; letter-spacing:.04em; }
.step .s{ font-size:.82rem; color:var(--muted); }
.step.done{ border-color:rgba(52,211,153,.55); box-shadow:0 0 20px rgba(52,211,153,.12); }
.step.done .n{ color:var(--green); }
.step.active{ border-color:var(--cyan); box-shadow:0 0 26px rgba(34,211,238,.22); }
.step.active .n{ color:var(--cyan); }

/* Kartu status */
.cards{ display:grid; grid-template-columns:repeat(4,1fr); gap:.8rem; margin:.6rem 0 1rem 0; }
.card{
  border:1px solid var(--line); border-radius:14px; padding:.8rem 1rem; background:var(--panel);
}
.card .k{ font-family:'JetBrains Mono', monospace; font-size:.68rem; letter-spacing:.18em; color:var(--muted); text-transform:uppercase; }
.card .v{ font-family:'Orbitron', sans-serif; font-size:1.15rem; margin-top:.2rem; color:#eaf6ff; word-break:break-word; }
.card .v.ok{ color:var(--green); } .card .v.bad{ color:var(--orange); }

.sec-title{
  font-family:'Orbitron', sans-serif; letter-spacing:.14em; text-transform:uppercase; font-size:.95rem;
  color:var(--cyan); margin:1.2rem 0 .6rem 0; display:flex; align-items:center; gap:.7rem;
}
.sec-title::after{ content:""; flex:1; height:1px; background:linear-gradient(90deg, var(--line), transparent); }

.brand{ display:flex; align-items:center; gap:.7rem; margin-bottom:.8rem; }
.brand .logo{
  width:42px; height:42px; border-radius:12px; display:grid; place-items:center; font-size:1.3rem;
  background:linear-gradient(135deg, #0891b2, #3b82f6); box-shadow:0 0 22px rgba(34,211,238,.45);
}
.brand .nm{ font-family:'Orbitron', sans-serif; font-weight:700; letter-spacing:.1em; font-size:.85rem; line-height:1.2; }
.brand .sb{ font-family:'JetBrains Mono', monospace; font-size:.66rem; color:var(--muted); letter-spacing:.14em; }

/* Hasil */
[data-testid="stVerticalBlockBorderWrapper"]{ border-color:var(--line) !important; background:var(--panel); border-radius:16px; }
.stMarkdown h2{ font-family:'Orbitron', sans-serif; font-size:1.15rem; color:var(--cyan); letter-spacing:.06em; }
.stMarkdown table{ width:100%; border-collapse:collapse; font-size:.92rem; }
.stMarkdown th{ background:rgba(34,211,238,.12); color:var(--cyan); font-family:'Orbitron', sans-serif; font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; }
.stMarkdown th, .stMarkdown td{ border:1px solid var(--line) !important; padding:.5rem .6rem; vertical-align:top; }
.stMarkdown tr:hover td{ background:rgba(34,211,238,.05); }

.footer{
  text-align:center; color:var(--muted); font-size:.85rem; padding:1rem 0 .4rem 0;
  font-family:'JetBrains Mono', monospace; letter-spacing:.04em;
}

@media (max-width:900px){
  .steps, .cards{ grid-template-columns:repeat(2,1fr); }
  .hero h1{ font-size:1.8rem; }
}
</style>
"""

SHIP_SVG = """
<svg class="ship-svg" viewBox="0 0 900 270" xmlns="http://www.w3.org/2000/svg" fill="none" stroke-linecap="round" stroke-linejoin="round">
<defs>
<linearGradient id="gl" x1="0" x2="1"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#60a5fa"/></linearGradient>
<linearGradient id="sea" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#22d3ee" stop-opacity=".28"/><stop offset="1" stop-color="#22d3ee" stop-opacity="0"/></linearGradient>
</defs>
<g stroke="rgba(34,211,238,.14)" stroke-width="1">
<line x1="150" y1="40" x2="150" y2="215"/><line x1="270" y1="40" x2="270" y2="215"/><line x1="390" y1="40" x2="390" y2="215"/>
<line x1="510" y1="40" x2="510" y2="215"/><line x1="630" y1="40" x2="630" y2="215"/><line x1="750" y1="40" x2="750" y2="215"/>
</g>
<g class="bob">
<path class="draw" pathLength="1" stroke="url(#gl)" stroke-width="2.4" d="M60 150 L845 150 L790 205 L112 205 Q76 200 60 150 Z"/>
<path class="draw d2" pathLength="1" stroke="#22d3ee" stroke-width="1.8" d="M150 150 L150 98 L335 98 L335 150"/>
<path class="draw d2" pathLength="1" stroke="#22d3ee" stroke-width="1.8" d="M190 98 L190 62 L300 62 L300 98"/>
<path class="draw d3" pathLength="1" stroke="#60a5fa" stroke-width="1.8" d="M215 62 L215 36 L270 36 L270 62"/>
<path class="draw d3" pathLength="1" stroke="#ff7a1a" stroke-width="2" d="M235 36 L235 14 L255 14 L255 36"/>
<path class="draw d4" pathLength="1" stroke="#60a5fa" stroke-width="1.6" d="M700 150 L700 52 M700 52 L770 150 M700 62 L640 150"/>
<path class="draw d4" pathLength="1" stroke="#22d3ee" stroke-width="1.6" d="M380 150 L380 120 L520 120 L520 150 M410 120 L410 150 M450 120 L450 150 M490 120 L490 150"/>
<g stroke="#22d3ee" stroke-width="1.2" opacity=".75">
<rect x="165" y="112" width="22" height="14" rx="2"/><rect x="200" y="112" width="22" height="14" rx="2"/><rect x="235" y="112" width="22" height="14" rx="2"/><rect x="270" y="112" width="22" height="14" rx="2"/>
</g>
<circle class="blink" cx="700" cy="52" r="3.5" fill="#ff7a1a" stroke="none"/>
</g>
<path stroke="#ff7a1a" stroke-width="1.2" stroke-dasharray="6 5" d="M20 185 L880 185"/>
<text x="22" y="178" fill="#ff7a1a" font-family="JetBrains Mono, monospace" font-size="10">WL / DRAFT</text>
<g stroke="#22d3ee" stroke-width="1.2" opacity=".85">
<line x1="60" y1="232" x2="845" y2="232"/><line x1="60" y1="224" x2="60" y2="240"/><line x1="845" y1="224" x2="845" y2="240"/>
<path d="M60 232 l9 -4 v8 z M845 232 l-9 -4 v8 z" fill="#22d3ee"/>
</g>
<text x="452" y="226" fill="#22d3ee" text-anchor="middle" font-family="JetBrains Mono, monospace" font-size="11" letter-spacing="3">LOA</text>
<g fill="#7f96b3" font-family="JetBrains Mono, monospace" font-size="9"><text x="145" y="34">FR 20</text><text x="385" y="34">FR 60</text><text x="625" y="34">FR 100</text><text x="745" y="34">FR 120</text></g>
<g>
<path class="wave" d="M-120 252 q30 -14 60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 V270 H-120 Z" fill="url(#sea)" stroke="#22d3ee" stroke-width="1.2"/>
<path class="wave2" d="M-120 262 q30 -10 60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 t60 0 V270 H-120 Z" fill="none" stroke="#60a5fa" stroke-width="1"/>
</g>
</svg>
"""


def render_html(s: str) -> None:
    """Render HTML ke Streamlit; baris kosong/indentasi dibuang agar tidak dianggap code block."""
    s = "\n".join(line.strip() for line in s.splitlines() if line.strip())
    st.markdown(s, unsafe_allow_html=True)


def esc(teks: str) -> str:
    return htmllib.escape(str(teks))


def judul_seksi(teks: str) -> None:
    render_html(f'<div class="sec-title">{esc(teks)}</div>')


def _teks_aman_pdf(teks: str) -> str:
    """Menormalkan Markdown/karakter agar aman dirender oleh font PDF bawaan."""
    teks = re.sub(r"\*\*(.*?)\*\*", r"\1", str(teks))
    teks = teks.replace("—", "-").replace("–", "-").replace("•", "-")
    teks = teks.replace("…", "...")
    return htmllib.escape(teks).replace("\n", "<br/>")


def _tabel_laporan_pdf(baris: list[str], lebar: float, gaya_sel: ParagraphStyle) -> Table:
    """Mengubah tabel Markdown sederhana menjadi tabel ReportLab yang dapat membungkus teks."""
    data = []
    jumlah_kolom = 1
    for baris_tabel in baris:
        sel = [bagian.strip() for bagian in baris_tabel.strip().strip("|").split("|")]
        jumlah_kolom = max(jumlah_kolom, len(sel))
        data.append(sel)

    data = [baris + [""] * (jumlah_kolom - len(baris)) for baris in data]
    gaya_header = ParagraphStyle(
        name="PdfTableHeader",
        parent=gaya_sel,
        fontName="Helvetica-Bold",
        textColor=colors.white,
    )
    data_pdf = []
    for indeks, baris in enumerate(data):
        gaya_baris = gaya_header if indeks == 0 else gaya_sel
        data_pdf.append([Paragraph(_teks_aman_pdf(sel), gaya_baris) for sel in baris])
    tabel = Table(data_pdf, colWidths=[lebar / jumlah_kolom] * jumlah_kolom, repeatRows=1)
    tabel.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#12355B")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C4CC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7F9")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return tabel


def buat_laporan_pdf(
    hasil: str,
    nama_dokumen: str,
    standar: str,
    jenis_kapal: str,
    loa_meter: float,
    gt: float,
    nama_aturan: list[str],
) -> bytes:
    """Membuat laporan A4 yang siap diunduh tanpa menyimpan hasil ke server."""
    if not REPORTLAB_TERSEDIA:
        raise RuntimeError("Paket ReportLab belum terpasang. Jalankan: py -m pip install reportlab")

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1.7 * cm,
        rightMargin=1.7 * cm,
        topMargin=2.1 * cm,
        bottomMargin=1.8 * cm,
        title="Laporan Evaluasi Rancang Bangun Kapal",
        author="Naval Architect Survey Apps",
    )

    gaya = getSampleStyleSheet()
    gaya.add(ParagraphStyle(
        name="PdfTitle", parent=gaya["Title"], fontName="Helvetica-Bold", fontSize=18,
        leading=22, textColor=colors.HexColor("#12355B"), alignment=TA_CENTER, spaceAfter=5,
    ))
    gaya.add(ParagraphStyle(
        name="PdfSubTitle", parent=gaya["BodyText"], fontName="Helvetica", fontSize=9,
        leading=12, textColor=colors.HexColor("#52616B"), alignment=TA_CENTER, spaceAfter=14,
    ))
    gaya.add(ParagraphStyle(
        name="PdfHeading", parent=gaya["Heading2"], fontName="Helvetica-Bold", fontSize=12,
        leading=15, textColor=colors.HexColor("#12355B"), spaceBefore=12, spaceAfter=6,
    ))
    gaya.add(ParagraphStyle(
        name="PdfBody", parent=gaya["BodyText"], fontName="Helvetica", fontSize=9,
        leading=12.5, textColor=colors.HexColor("#182B3A"), spaceAfter=6,
    ))
    gaya.add(ParagraphStyle(
        name="PdfTable", parent=gaya["BodyText"], fontName="Helvetica", fontSize=7.2,
        leading=9, textColor=colors.HexColor("#182B3A"),
    ))

    dibuat_pada = datetime.now(ZoneInfo("Asia/Makassar")).strftime("%d %B %Y, %H:%M WITA")
    isi = [
        Paragraph("LAPORAN EVALUASI RANCANG BANGUN KAPAL", gaya["PdfTitle"]),
        Paragraph("Naval Architect Survey Apps - AI-assisted preliminary review", gaya["PdfSubTitle"]),
    ]

    metadata = [
        ["Dokumen diperiksa", nama_dokumen],
        ["Standar acuan", standar],
        ["Profil kapal", f"{jenis_kapal} | LOA {loa_meter:.2f} m | GT {gt:.2f}"],
        ["Aturan yang digunakan", ", ".join(nama_aturan) or "-"],
        ["Dibuat pada", dibuat_pada],
    ]
    metadata_pdf = [
        [Paragraph(f"<b>{_teks_aman_pdf(label)}</b>", gaya["PdfBody"]),
         Paragraph(_teks_aman_pdf(nilai), gaya["PdfBody"])]
        for label, nilai in metadata
    ]
    tabel_metadata = Table(metadata_pdf, colWidths=[4.1 * cm, doc.width - 4.1 * cm])
    tabel_metadata.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E8F3F7")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C4CC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    isi.extend([tabel_metadata, Spacer(1, 10)])

    baris = hasil.splitlines()
    indeks = 0
    while indeks < len(baris):
        baris_ini = baris[indeks].strip()
        if not baris_ini:
            indeks += 1
            continue
        if baris_ini.startswith("##"):
            isi.append(Paragraph(_teks_aman_pdf(baris_ini.lstrip("#").strip()), gaya["PdfHeading"]))
            indeks += 1
            continue
        if indeks + 1 < len(baris) and "|" in baris_ini and re.fullmatch(r"[|:\-\s]+", baris[indeks + 1].strip()):
            tabel_baris = [baris_ini]
            indeks += 2
            while indeks < len(baris) and "|" in baris[indeks] and baris[indeks].strip():
                tabel_baris.append(baris[indeks])
                indeks += 1
            isi.extend([_tabel_laporan_pdf(tabel_baris, doc.width, gaya["PdfTable"]), Spacer(1, 8)])
            continue

        if baris_ini.startswith(("- ", "* ")):
            baris_ini = "- " + baris_ini[2:]
        isi.append(Paragraph(_teks_aman_pdf(baris_ini), gaya["PdfBody"]))
        indeks += 1

    isi.extend([
        Spacer(1, 8),
        Paragraph("Catatan Legal", gaya["PdfHeading"]),
        Paragraph(
            "Laporan ini adalah telaah awal berbasis dokumen dan bantuan AI. Laporan bukan sertifikat "
            "kelaiklautan, persetujuan desain, atau pernyataan kepatuhan formal. Validasi akhir harus "
            "dilakukan oleh Naval Architect atau Surveyor berwenang.",
            gaya["PdfBody"],
        ),
    ])

    def halaman(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#22A9C6"))
        canvas.setLineWidth(0.7)
        canvas.line(document.leftMargin, A4[1] - 1.25 * cm, A4[0] - document.rightMargin, A4[1] - 1.25 * cm)
        canvas.setFillColor(colors.HexColor("#52616B"))
        canvas.setFont("Helvetica", 8)
        canvas.drawString(document.leftMargin, 1.05 * cm, "Naval Architect Survey Apps")
        canvas.drawRightString(A4[0] - document.rightMargin, 1.05 * cm, f"Halaman {document.page}")
        canvas.restoreState()

    doc.build(isi, onFirstPage=halaman, onLaterPages=halaman)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Logika inti (tidak diubah)
# ---------------------------------------------------------------------------

def buat_prompt(
    standar_nama: str,
    standar_teks: str,
    lingkup_pemeriksaan: list[str],
    aturan_tambahan: str,
    jenis_kapal: str,
    loa_meter: float,
    gt: float,
    nama_dokumen_pemeriksaan: str,
    nama_dokumen_acuan: list[str],
) -> str:
    return f"""
Anda adalah Marine Surveyor dan Naval Architect senior. Analisis dokumen PDF rancang
bangun kapal yang diberikan. Gunakan bahasa Indonesia profesional, jelas, dan praktis.

Standar acuan: {standar_nama}
Konteks standar/acuan: {standar_teks}
Lingkup pemeriksaan: {", ".join(lingkup_pemeriksaan)}
Profil kapal dari pengguna: jenis kapal = {jenis_kapal}; LOA = {loa_meter:.2f} m; GT = {gt:.2f}.
Dokumen utama yang diperiksa: {nama_dokumen_pemeriksaan}
Dokumen aturan/acuan tambahan: {", ".join(nama_dokumen_acuan) or "Tidak ada."}
Aturan/kriteria tambahan: {aturan_tambahan or "Tidak ada."}

Lakukan hal berikut:
1. Identifikasi jenis dokumen PDF, nomor/revisi bila terbaca, halaman yang relevan,
   dan bagian kapal yang benar-benar terlihat. Jika tidak cukup jelas, nyatakan batasannya.
   Jangan mengarang dimensi, klaim kepatuhan, atau item yang tidak terlihat.
2. Nilai tata letak: pembagian ruang/dek, akses, jalur evakuasi yang tampak,
   visibilitas, area kerja, dan keselamatan operasional.
3. Nilai proporsi desain: hubungan lambung, bangunan atas, freeboard, bukaan,
   stabilitas secara indikatif, serta kelayakan konstruksi visual.
4. Berikan temuan dengan prioritas KRITIS, TINGGI, SEDANG, atau RENDAH.
   Untuk setiap temuan, jelaskan alasan, dampak, dan rekomendasi perbaikan.
5. Buat daftar data yang diperlukan sebelum persetujuan desain, seperti GA plan,
   lines plan, hydrostatics, stability booklet, scantling calculation,
   arrangement keselamatan, dan spesifikasi material.
6. Perlakukan jenis kapal, LOA, dan GT sebagai data masukan pengguna. Cocokkan dengan
   dokumen bila dapat dilakukan; apabila berbeda atau tidak terbaca, tandai "Perlu verifikasi".
7. PDF pertama adalah dokumen utama yang diperiksa. PDF setelahnya adalah dokumen aturan/acuan,
   sesuai urutan pada daftar "Dokumen aturan/acuan" di atas: file aturan dasar adalah aturan
   yang berlaku untuk SEMUA analisis, sedangkan PDF lainnya adalah aturan khusus untuk
   standar acuan yang dipilih. Saat menggunakan aturan dari PDF acuan, sebutkan nama dokumen,
   nomor klausul dan halaman bila terbaca. Jika klausul/halaman tidak terbaca, tulis
   "Perlu verifikasi"; jangan mengarang referensi atau menyatakan kepatuhan formal.
8. Evaluasi HANYA terhadap aturan dasar dan standar acuan yang dipilih di
   atas. Jangan menggunakan, membandingkan, atau mengutip standar/aturan lain di luar itu, dan
   abaikan bagian dokumen yang tidak relevan dengan standar dan lingkup pemeriksaan.

Gunakan format Markdown berikut:

## Ringkasan Eksekutif
## Bukti dari Dokumen
## Temuan dan Rekomendasi

Buat tabel dengan kolom:
Prioritas | Item | Bukti/Halaman | Status (Terverifikasi visual/Perlu verifikasi/Tidak terlihat) | Risiko | Rekomendasi

## Data yang Perlu Diverifikasi
## Batasan Penilaian

Penting: ini adalah telaah awal berbasis dokumen PDF, bukan sertifikasi kelas,
persetujuan desain, atau pengganti inspeksi dan perhitungan oleh tenaga berwenang.
""".strip()


def analisis_dokumen(
    api_key: str,
    data_dokumen_pemeriksaan: bytes,
    data_dokumen_acuan: list[bytes],
    prompt: str,
    saat_retry: Callable[[int, int, float], None] | None = None,
) -> str:
    client = genai.Client(api_key=api_key)

    parts = [
        types.Part.from_bytes(
            data=data_dokumen_pemeriksaan,
            mime_type="application/pdf",
        )
    ]
    parts.extend(
        types.Part.from_bytes(data=data_pdf, mime_type="application/pdf")
        for data_pdf in data_dokumen_acuan
    )
    parts.append(prompt)

    for percobaan in range(MAKS_PERCOBAAN_ANALISIS):
        try:
            response = client.models.generate_content(
                model=MODEL_GEMINI,
                contents=parts,
            )
            if not response.text:
                raise RuntimeError("Gemini tidak mengembalikan teks analisis.")
            return response.text
        except Exception as error:
            pesan = str(error).upper()
            sementara = any(
                kode in pesan
                for kode in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "408", "TIMEOUT")
            )
            percobaan_terakhir = percobaan == MAKS_PERCOBAAN_ANALISIS - 1
            if not sementara or percobaan_terakhir:
                raise

            # 2, 4 detik + jitter kecil agar klien tidak melakukan retry serentak.
            jeda = (2**(percobaan + 1)) + random.uniform(0, 0.75)
            if saat_retry:
                saat_retry(percobaan + 1, MAKS_PERCOBAAN_ANALISIS, jeda)
            time.sleep(jeda)

    raise RuntimeError("Analisis tidak dapat dijalankan.")


def ambil_api_key() -> str | None:
    """Mengambil API key dari environment atau Streamlit Secrets tanpa menampilkannya di UI."""
    return ambil_rahasia("GEMINI_API_KEY") or None


def ambil_rahasia(nama: str) -> str:
    """Environment diprioritaskan; Streamlit Secrets dipakai untuk deployment Community Cloud."""
    nilai = os.getenv(nama, "").strip()
    if nilai:
        return nilai
    try:
        nilai_secret = st.secrets.get(nama, "")
    except Exception:
        return ""
    return str(nilai_secret).strip()


# ---------------------------------------------------------------------------
# Antarmuka
# ---------------------------------------------------------------------------

render_html(CSS)

_, FOLDER_DRIVE_ID, KESALAHAN_KONFIGURASI_DRIVE = konfigurasi_drive()
DAFTAR_FILE_DRIVE: list[dict] = []
KESALAHAN_DRIVE = KESALAHAN_KONFIGURASI_DRIVE
if KESALAHAN_DRIVE is None:
    try:
        DAFTAR_FILE_DRIVE = daftar_pdf_drive(FOLDER_DRIVE_ID)
    except Exception as error:
        KESALAHAN_DRIVE = f"Google Drive tidak dapat diakses: {error}"

INDEKS_FILE_DRIVE = indeks_pdf_drive(DAFTAR_FILE_DRIVE)
PILIHAN_STANDAR = standar_dengan_pdf(INDEKS_FILE_DRIVE)

with st.sidebar:
    render_html(
        """
        <div class="brand">
        <div class="logo">⚓</div>
        <div><div class="nm">NAVAL ARCHITECT<br>SURVEY APPS</div><div class="sb">MARINE DESIGN REVIEW · v2</div></div>
        </div>
        """
    )
    st.header("Konfigurasi")
    api_key = ambil_api_key()
    if api_key:
        st.success("Ready to use", icon="✅")
    else:
        st.error("Gemini API key belum dikonfigurasi pada sistem web.")

    standar_pilihan = st.selectbox(
        "Standar acuan evaluasi",
        [*PILIHAN_STANDAR, "Custom"],
        help="Setiap standar yang tampil sudah terhubung ke PDF acuan private di Google Drive.",
    )

    st.subheader("Profil Kapal")
    jenis_kapal = st.selectbox(
        "Jenis kapal",
        ["Penumpang", "Barang", "Penangkap ikan"],
        help="Dipakai sebagai konteks pemeriksaan, bukan sebagai pengganti data pada dokumen desain.",
    )
    loa_meter = st.number_input(
        "LOA (Length Overall) — meter",
        min_value=0.0,
        value=0.0,
        step=0.1,
        format="%.2f",
        help="Masukkan panjang keseluruhan kapal dalam meter.",
    )
    gt = st.number_input(
        "GT (Gross Tonnage)",
        min_value=0.0,
        value=0.0,
        step=1.0,
        format="%.2f",
        help="Masukkan Gross Tonnage kapal.",
    )

    if standar_pilihan == "Custom":
        standar_teks = st.text_area(
            "Teks standar/acuan custom",
            placeholder="Contoh: Evaluasi sesuai spesifikasi proyek dan pedoman internal perusahaan.",
            height=150,
        )
    else:
        standar_teks = STANDAR_ACUAN[standar_pilihan]
        st.info(standar_teks)

    lingkup_pemeriksaan = st.multiselect(
        "Lingkup pemeriksaan",
        ["Rancang bangun umum", "Life Saving Appliance (LSA)", "Fire Control Plan (FCP)"],
        default=["Rancang bangun umum", "Life Saving Appliance (LSA)", "Fire Control Plan (FCP)"],
    )
    aturan_tambahan = st.text_area(
        "Aturan/kriteria tambahan (opsional)",
        placeholder="Masukkan nomor klausul, revisi dokumen, atau kriteria proyek yang sah.",
        height=120,
    )

    with st.expander("Checklist LSA & Fire Control Plan"):
        st.markdown("**Life Saving Appliance (LSA)**")
        st.markdown(CHECKLIST_LSA)
        st.markdown("**Fire Control Plan (FCP)**")
        st.markdown(CHECKLIST_FCP)

# --- Hero ---
render_html(
    f"""
    <div class="hero">
    <span class="hero-tag">◉ MARINE DESIGN INTELLIGENCE</span>
    <h1>NAVAL ARCHITECT<br>SURVEY APPS</h1>
    <p>Telaah awal General Arrangement, LSA Plan, dan Fire Control Plan berbasis AI —
    dari lambung hingga jalur evakuasi, dalam satu panel kendali.</p>
    <div class="hero-chips">
    <span class="chip">ENGINE <b>GEMINI 3.8 FLASH</b></span>
    <span class="chip">STANDAR <b>BKI · NCVS · HSC · TRADISIONAL · KLM</b></span>
    <span class="chip">SCOPE <b>GA · LSA · FCP</b></span>
    <span class="chip">OUTPUT <b>MARKDOWN REPORT</b></span>
    </div>
    {SHIP_SVG}
    </div>
    """
)

unggahan = st.file_uploader(
    "Unggah dokumen utama yang diperiksa (PDF)",
    type=["pdf"],
    help="Unggah General Arrangement, LSA Plan, Fire Control Plan, atau dokumen desain PDF terkait.",
)

data_pdf = None
if unggahan is not None:
    data_pdf = unggahan.getvalue()

# Aturan dari Drive: rules dasar (selalu) + PDF standar pilihan (bukan untuk Custom).
ATURAN_DRIVE, dasar_ok, standar_ok = aturan_drive_terpilih(INDEKS_FILE_DRIVE, standar_pilihan)
NAMA_ATURAN_LIST = [str(file["name"]) for file in ATURAN_DRIVE]
NAMA_ATURAN = " + ".join(NAMA_ATURAN_LIST) or "—"
ukuran_aturan = sum(int(file.get("size", 0)) for file in ATURAN_DRIVE)
ukuran_total_mb = (len(data_pdf) if data_pdf else 0) + ukuran_aturan
ukuran_total_mb /= 1024 * 1024

melebihi_batas = ukuran_total_mb > MAX_TOTAL_PDF_MB
if melebihi_batas:
    data_pdf = None

profil_lengkap = loa_meter > 0 and gt > 0
aturan_lengkap = KESALAHAN_DRIVE is None and dasar_ok and standar_ok
siap_analisis = (
    bool(api_key) and data_pdf is not None and aturan_lengkap
    and bool(lingkup_pemeriksaan) and profil_lengkap
)
sudah_ada_hasil = "hasil_analisis" in st.session_state


def kelas_step(selesai: bool, aktif: bool) -> str:
    return "step done" if selesai else ("step active" if aktif else "step")


s1 = profil_lengkap and bool(lingkup_pemeriksaan)
s2 = data_pdf is not None
s3 = sudah_ada_hasil
s4 = sudah_ada_hasil
render_html(
    f"""
    <div class="steps">
    <div class="{kelas_step(s1, not s1)}"><div class="n">01 · {"SELESAI" if s1 else "LANGKAH"}</div><div class="t">Profil Kapal</div><div class="s">Jenis, LOA, GT &amp; lingkup</div></div>
    <div class="{kelas_step(s2, s1 and not s2)}"><div class="n">02 · {"SELESAI" if s2 else "LANGKAH"}</div><div class="t">Unggah Dokumen</div><div class="s">PDF GA / LSA / FCP</div></div>
    <div class="{kelas_step(s3, s1 and s2 and not s3)}"><div class="n">03 · {"SELESAI" if s3 else "LANGKAH"}</div><div class="t">Analisis AI</div><div class="s">Telaah terhadap aturan</div></div>
    <div class="{kelas_step(s4, False)}"><div class="n">04 · {"SELESAI" if s4 else "LANGKAH"}</div><div class="t">Laporan</div><div class="s">Temuan &amp; rekomendasi</div></div>
    </div>
    """
)

# --- Kartu status ---
nama_dok = unggahan.name if unggahan is not None else "—"
rules_ok = aturan_lengkap
status_aturan = "AKTIF" if rules_ok else "TIDAK LENGKAP"
render_html(
    f"""
    <div class="cards">
    <div class="card"><div class="k">Dokumen utama</div><div class="v">{esc(nama_dok)}</div></div>
    <div class="card"><div class="k">Total unggahan</div><div class="v {'bad' if melebihi_batas else ''}">{ukuran_total_mb:.2f} MB</div></div>
    <div class="card"><div class="k">Aturan: {esc(NAMA_ATURAN)}</div><div class="v {'ok' if rules_ok else 'bad'}">{status_aturan}</div></div>
    <div class="card"><div class="k">Profil kapal</div><div class="v">{esc(jenis_kapal)} · {loa_meter:.1f} m · {gt:.0f} GT</div></div>
    </div>
    """
)

if KESALAHAN_DRIVE:
    st.error(KESALAHAN_DRIVE)
elif not dasar_ok:
    st.error(
        "File aturan dasar tidak ditemukan di Google Drive. "
        "Gunakan nama `rules dasar.pdf` atau `rules_dasar.pdf` pada folder Drive yang dibagikan."
    )
elif not standar_ok:
    nama_file = NAMA_FILE_STANDAR[standar_pilihan]
    st.error(
        f"PDF aturan untuk standar **{standar_pilihan}** tidak ditemukan. "
        f"Tambahkan file `{nama_file}` pada folder Google Drive yang dibagikan."
    )
if melebihi_batas:
    st.error(
        f"Total dokumen utama dan {NAMA_ATURAN} melebihi {MAX_TOTAL_PDF_MB} MB. "
        "Gunakan PDF yang lebih kecil."
    )

judul_seksi("Jalankan Analisis")

if st.button("🔍 Analisis Rancang Bangun", type="primary", use_container_width=True):
    if not api_key:
        st.warning("Gemini API key belum dikonfigurasi pada sistem. Hubungi administrator.")
    elif data_pdf is None:
        st.warning("Unggah dokumen PDF rancang bangun, LSA Plan, atau Fire Control Plan terlebih dahulu.")
    elif not aturan_lengkap:
        st.warning("Aturan dasar dan PDF standar terpilih harus tersedia serta dapat diakses dari Google Drive.")
    elif not lingkup_pemeriksaan:
        st.warning("Pilih minimal satu lingkup pemeriksaan.")
    elif loa_meter <= 0 or gt <= 0:
        st.warning("Masukkan nilai LOA dan GT yang lebih besar dari 0.")
    elif standar_pilihan == "Custom" and not standar_teks.strip():
        st.warning("Masukkan teks standar custom terlebih dahulu.")
    else:
        checklist = []
        if "Life Saving Appliance (LSA)" in lingkup_pemeriksaan:
            checklist.append(f"CHECKLIST LSA:\n{CHECKLIST_LSA}")
        if "Fire Control Plan (FCP)" in lingkup_pemeriksaan:
            checklist.append(f"CHECKLIST FIRE CONTROL PLAN:\n{CHECKLIST_FCP}")
        prompt = buat_prompt(
            standar_pilihan,
            standar_teks.strip(),
            lingkup_pemeriksaan,
            f"{aturan_tambahan.strip()}\n\n" + "\n\n".join(checklist),
            jenis_kapal,
            loa_meter,
            gt,
            unggahan.name,
            NAMA_ATURAN_LIST,
        )

        with st.spinner("Analyzing design..."):
            status_retry = st.empty()

            def tampilkan_status_retry(percobaan_gagal: int, total: int, jeda: float) -> None:
                status_retry.warning(
                    "Layanan Gemini sedang padat. "
                    f"Percobaan {percobaan_gagal}/{total} gagal; mencoba ulang dalam {jeda:.0f} detik..."
                )

            try:
                DATA_ATURAN = [
                    unduh_pdf_drive(str(file["id"]), str(file.get("modifiedTime", "")))
                    for file in ATURAN_DRIVE
                ]
                hasil = analisis_dokumen(
                    api_key,
                    data_pdf,
                    DATA_ATURAN,
                    prompt,
                    saat_retry=tampilkan_status_retry,
                )
                status_retry.empty()
                st.session_state["hasil_analisis"] = hasil
                st.session_state["nama_dokumen"] = unggahan.name
                st.rerun()
            except Exception as error:
                status_retry.empty()
                pesan_error = str(error)
                if "503" in pesan_error or "UNAVAILABLE" in pesan_error.upper():
                    st.error(
                        "Gemini masih padat setelah percobaan ulang otomatis. "
                        "Tunggu beberapa menit, lalu jalankan analisis kembali."
                    )
                else:
                    st.error(f"Analisis gagal: {error}")

if "hasil_analisis" in st.session_state:
    judul_seksi("Laporan Hasil Analisis AI")
    st.caption(f"Sumber dokumen: {st.session_state['nama_dokumen']}")
    with st.container(border=True):
        st.markdown(st.session_state["hasil_analisis"])

    st.download_button(
        "⬇ Unduh hasil sebagai Markdown",
        data=st.session_state["hasil_analisis"],
        file_name="hasil_evaluasi_kapal.md",
        mime="text/markdown",
    )

    if REPORTLAB_TERSEDIA:
        try:
            laporan_pdf = buat_laporan_pdf(
                hasil=st.session_state["hasil_analisis"],
                nama_dokumen=st.session_state["nama_dokumen"],
                standar=standar_pilihan,
                jenis_kapal=jenis_kapal,
                loa_meter=loa_meter,
                gt=gt,
                nama_aturan=NAMA_ATURAN_LIST,
            )
            st.download_button(
                "⬇ Unduh laporan PDF",
                data=laporan_pdf,
                file_name="laporan_evaluasi_kapal.pdf",
                mime="application/pdf",
                type="primary",
            )
        except Exception as error:
            st.error(f"Laporan PDF tidak dapat dibuat: {error}")
    else:
        st.warning(
            "Fitur PDF membutuhkan ReportLab. Jalankan: py -m pip install reportlab",
            icon="⚠️",
        )

render_html(
    """
    <div class="footer">
    ⚓ Gunakan hasil sebagai bantuan telaah awal. Validasi akhir harus dilakukan oleh Naval Architect
    atau Surveyor yang kompeten dengan dokumen dan perhitungan lengkap.
    </div>
    """
)
