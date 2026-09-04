# Analisis RYO-CHAN Hackathon 2026

Disusun 4 Sep 2026 dari sumber resmi (landing ryobuild.com, builder guide, API publik app-ryochan.com, GitBook, situs partner). Semua tautan sudah diverifikasi hidup kecuali yang ditandai.

---

## 1. Ringkasan cepat

| Item | Nilai |
|---|---|
| Penyelenggara | RYO Digital (ekosistem RYO Coin, LIFE Wallet, Global Mall, ATM Network, RYO-CHAN) |
| Partner akademik | Blockchain Legal Institute (BLI) |
| Format | Virtual, gratis |
| Periode build | 14 Agu 2026 → 8 Sep 2026 23:59 JST (landing page) |
| Judging | 9–16 Sep 2026 JST (audit kode, cek orisinalitas, security review, evaluasi teknis) |
| Pengumuman | 17–19 Sep 2026 JST |
| Setelahnya | 20 Sep: refinement modul terpilih; 10 Okt 2026: target platform launch |
| Hadiah | $15.000 dalam token RYO-CHAN, 16 award + Grand Prize trip ke Tokyo HQ |
| Landing | https://ryobuild.com dan https://ryobuild.com/hackathon |
| Platform | https://app-ryochan.com ("Japan's First AI Trading Assistant") |

**Peringatan tanggal.** Tiga sumber resmi tidak konsisten:

| Sumber | Deadline | Judging | Winners |
|---|---|---|---|
| ryobuild.com (landing) | 8 Sep 23:59 JST | 9–16 Sep | 17–19 Sep |
| ryobuild.com/hackathon (builder page, bagian Key dates) | 8 Sep 23:59 JST | 7–14 Sep | 15–17 Sep |
| API `GET /api/hackathon/config` | 31 Agu 23:59:59 UTC, `current_phase: "submission_close"` | TBD | TBD |

Anggap 8 Sep 23:59 JST sebagai deadline riil (countdown di landing memakai angka itu), tapi konfirmasi ke Help Desk Discord karena API server menandai fase sudah "close". Jangan submit di menit terakhir.

---

## 2. Apa itu RYO-CHAN (konteks produk)

Dua "wajah" RYO-CHAN yang perlu dipahami karena keduanya muncul di materi:

1. **Token RYO-CHAN (RYOCHAN)** di Solana. Supply 200T, presale 3 fase (harga fase 2 $0.00000025), 1% burn kuartalan, 20% fee platform untuk buyback-and-burn, 5% ke animal welfare. Kontrak: `5WzmuNt4iYqviMX7C8SvtVYxohhRaee7LTw8N9TFEe9j`. Endorsement UFC/boxing (Gilbert Burns, Fabricio Werdum, Popó Freitas). Founder Anthony Diaz, President Lani Dizon. Hadiah hackathon dibayar dalam token ini.
2. **Platform RYO-CHAN (app-ryochan.com)** = "Japan's first no-code AI trading platform". User menulis strategi dalam bahasa biasa, jadi agent yang scan token, cek safety, compare, dan trade. Backend FastAPI "RYO Limited API" v0.0.1, env masih `development`, dependensi: Supabase, Redis, CoinMarketCap (CMC), signer. Fitur internal (dari OpenAPI): chat agent, wallet (generate, swap, transfer), trade (preview/confirm/open/sell-limit), paper trading mode, positions, gamification (XP, badges, quests, streaks, affinity), leaderboard + share, voice transcribe, notifications, referral, Google/Telegram login.

Skill registry internal platform (enum `SkillName`): scan_market, analyze_token, deep_analysis, compare_tokens, check_safety, build_trade_setup, market_overview, get_portfolio, get_pnl, execute_trade, place_limit_order, cancel_order, swap, swap_batch, open_position, sell_limit, list_positions, close_position, watch_token, transfer_token, transfer_batch, supported_tokens, create_wallet, find_skill, execute_skill, transaction_log, notifications. Skill `execute_*` dan order melewati "Ryo Guard" sebelum signing. Ini gambaran "skill contract" yang dipakai Track 3.

Asal produk (GitBook): RYO SCAN = anti-rug scanner Solana (konsentrasi holder, penarikan likuiditas, whale), lalu AI Companion yang direncanakan menjadi trading agent. Hackathon ini adalah langkah "brain" untuk agent tersebut.

---

## 3. Yang disediakan untuk builder: MCP research layer

Sumber: `docs/MCP-Builder-Guide.md` (salinan dari https://ryobuild.com/MCP-Builder-Guide.md, last updated 13 Agu 2026). Link PDF (`/MCP-Builder-Guide.pdf`, `/project-submission-form.pdf`) yang tercantum di API **tidak berfungsi**, keduanya mengembalikan halaman SPA HTML, bukan PDF.

| Hal | Detail |
|---|---|
| Endpoint MCP | `https://app-ryochan.com/api/mcp` (JSON-RPC: initialize, tools/list, tools/call) |
| REST | `POST /api/mcp/tools/{tool}/call` body = argumen polos, misal `{"symbol":"SOL"}` |
| Auth | `Authorization: Bearer ryo_mcp_...` (key diberikan organiser; admin bisa create/revoke/batch) |
| Health (tanpa auth) | `GET /api/mcp/health` → `{"status":"ok","protocol_version":"2024-11-05","server":"ryo-chan","tools":6}` (diverifikasi hidup 4 Sep) |
| Whoami | `GET /api/mcp/whoami` → key id, label, scope, expiry, kuota, daftar tool. Tidak memakan kuota |
| Katalog | `GET /api/mcp/tools` → name, description, inputSchema. Tidak memakan kuota. Katalog live = sumber kebenaran final |
| Rate limit | Header `X-RateLimit-Limit/Remaining/Reset`, `Retry-After`. Per key ada `rate_per_min`. Backoff eksponensial + jitter untuk 429/503 |
| Error | REST: HTTP 400 + `ErrorEnvelope {code,message,details,trace_id}`. MCP: tool failure = HTTP 200 + `result.isError:true` |

### Enam tool (versi builder guide, otoritatif)

| Tool | Input wajib | Opsional | Output inti |
|---|---|---|---|
| `market_overview` | – | – | regime (risk_on/risk_off/rotation/chop), total mcap, dominance, Fear & Greed, breadth, top movers |
| `scan_market` | – | `chain`, `theme`, `top_n` | shortlist kandidat berperingkat berdasarkan momentum live; `theme` hanya konteks, bukan filter berita |
| `analyze_token` | `symbol` | – | data USD, performa multi-window, RSI(14), ATR(14), market intelligence, verdict terstruktur |
| `deep_analysis` | `symbol` | `include_perp` | evidence pack: konteks pasar, technicals, confluence, verdict deterministik, token profile, derivatives, catalysts, risks, preview plan berbasis ATR |
| `compare_tokens` | `symbols` (2–4, string dipisah koma/spasi) | `intent`: swing/hold/spot | perbandingan momentum, aktivitas, volatilitas; coverage per token jujur |
| `monitor_market_sentiment_shift` | – | `time_window` tetap `7d` | perubahan Fear & Greed 7 hari, fase pasar, konteks derivatif, regime gabungan |

**Ketidaksesuaian penting.** Landing page ryobuild.com menyebut 7 tool termasuk `check_safety` dan `supported_tokens`. Builder guide dan `/api/mcp/health` menyebut 6 tool, dan guide secara eksplisit menyatakan "tidak mempublikasikan portfolio-analysis atau symbol-only safety tool". `check_safety` ada di skill registry internal platform tapi **tidak** di MCP builder. Jangan rencanakan produk yang bergantung pada `check_safety` via MCP.

### Kontrak respons (semua tool)

```
schema_version, tool, status (ok|partial|unavailable), data_mode (live|mixed|simulated|unknown),
as_of, request, data, summary{headline,key_points}, availability, warnings
```

Aturan kejujuran ("honesty convention"): pakai `data` untuk logika, `summary` hanya display. Nilai `null`/unavailable **tidak boleh** diubah jadi 0. Optional evidence yang gagal tidak menurunkan status evidence utama. Ini yang dinilai judges di "Coping with failure" dan yang bisa mendiskualifikasi kalau dilanggar.

### Yang tidak bisa dilakukan MCP
Tidak bisa buat/akses wallet, baca balance/posisi, place/execute trade, terima wallet address (hanya symbol). Guide meminta builder **tidak** probe kemampuan wallet/trading. Practice trade dan state ada di sisi builder.

### Yang harus dibawa builder
- Model LLM sendiri (RYO tidak memanggil model).
- Sumber berita hanya jika tema memakai berita. Rekomendasi: Tavily, free Researcher plan 1.000 kredit/bulan, endpoint Search/Extract/Research/Crawl/Map.
- Logika produk, state, UI, agent loop, mekanik sosial.

---

## 4. Tracks, hadiah, dan rubrik

Satu BUIDL boleh masuk lebih dari satu track. Tiap track dinilai independen: 100 poin = 40 poin umum + 60 poin rubrik track.

### Skor umum (40 poin, semua track)
- **Combining sources (20%)**: menggabungkan beberapa tool/sumber jadi satu keputusan, bukan sekadar print satu respons.
- **Coping with failure (20%)**: tetap jalan saat rate-limited, restart, atau dependensi mati.

### Track 1: Autonomous Agents ($6.000: 3.500 / 1.500 / 1.000) — headline track
Rubrik 60: Real thinking 25, Cause and effect 20, Repeatability 10, Advanced execution 5.
Agent menafsirkan bukti pasar dan mencatat practice trade yang akan dibuat. Harus ada reasoning trail yang bisa diulang. Stack: Python/TypeScript/LLM apa saja.
Ide resmi: Multi-KOL Narrative Agent (spotlight, sampai 20 akun X + Discord/Telegram; token, sentimen, conviction, urgency; deteksi konvergensi; practice position dengan aturan sinyal transparan dan risk limit), News Checker, Bull/Bear Debate (dua agent berdebat, agen ketiga menghakimi), Meme/Token Hunter (jelaskan yang dipilih dan ditolak), Whale Movement Tracker, Buy the Dip Agent (entry, sizing, exit sebagai satu keputusan).

### Track 2: Dashboards & Interfaces ($3.500: 2.000 / 1.000 / 500)
Rubrik 60: Easy to read 25, Sorted by importance 25, Works for everyone 10 (keyboard/aksesibilitas).
"Apa yang berubah dan kenapa penting dalam 30 detik." Stack: React/D3/frontend apa saja.
Ide resmi: News Dashboard (rank event menurut dampak ke posisi), Market Screener (sort + drill ke analisis penuh), Tech Indicator Interface (define, preview, monitor indikator).

### Track 3: New Skills ($3.500: 2.000 / 1.000 / 500)
Rubrik 60: Fills a gap 25, Follows the spec 15, Could we use it 20.
Bangun research tool yang belum ada di RYO, ikut spesifikasi tool (skill contract + honesty convention), bentuk respons predictable, minim kerja untuk ship. Stack: Python.
Ide resmi: Tech Indicator (kondisi user-defined vs data live), Scam Wallet Detection (sumber wallet-history eksternal → verdict risiko jelas), atau gap lain.
Catatan: RYO sendiri belum punya safety tool di MCP builder dan `SkillDefinition` internal memakai `{name, description, args[{name,type,required,description,enum,items}], requires_guard, xp}`. Skill baru idealnya mengikuti bentuk itu plus envelope respons di bagian 3.

### Cross-track
- **Social Media Awards** 5 × $200: post publik di X, tag @ryodigital, tunjukkan apa yang dibangun dan kenapa penting, link BUIDL/demo, tambahkan URL post ke submission sebelum judging. Satu award per tim.
- **Judges' Pick** $1.000: satu award lintas track.
- **Grand Prize**: trip ke Tokyo HQ (flight, akomodasi, transfer). Dipilih dari kontribusi keseluruhan: code quality, architecture, functionality, stability, bug count. Detail travel TBA.

### Yang menang vs tidak
Menang: jalur bukti → kesimpulan yang terlihat; produk yang cepat dipahami, dijalankan, dievaluasi; user dan keputusan yang jelas; karya orisinal memakai RYO sebagai fondasi.
Tidak dinilai: volume API call, tembok chart tanpa argumen, P&L jangka pendek, data palsu/placeholder.

---

## 5. Alur pendaftaran dan submission

1. Register sebagai hacker (Discord).
2. Organiser menambahkan Discord ID ke server "RYO-CHAN Virtual Hackathon 2026".
3. Organiser membuat **private GitHub repo** untuk proyek, detail dikirim via DM. Boleh mulai coding sebelum repo ada. Ada endpoint `POST /api/hackathon/repo-request` (status none/pending/created, ada cooldown `next_request_allowed_at`).
4. Push kode ke branch `main` repo tersebut.

Isi submission lengkap:
- Semua source code final di `main`.
- `.env.example` dengan semua env var yang dibutuhkan (contoh minimal: `RYO_MCP_URL`, `RYO_MCP_KEY`).
- Project Submission Form (PDF) di-commit ke repo. Link PDF resmi saat ini rusak; minta ke Help Desk.
- Demo video (file atau link).

Field form submission (dari schema API `HackathonSubmissionFields`, semua wajib saat final submit): team_name, participant_name, email, discord_id, github_username, project_name, tracks[] (track_1/track_2/track_3), project_description, demo_video_url, repo_url, x_post_url, agree_rules, confirm_no_secrets. Status: not_started → draft → submitted → under_review → accepted / rejected / needs_changes (ada `review_comment`).

Aturan:
- **Diskualifikasi**: commit API key/team token asli; menyajikan data fabrikasi/placeholder sebagai data asli (judges cek provenance).
- Kode ditulis selama event. Library/starter template boleh jika diungkap di README.
- Multi-track dan multi-BUIDL diperbolehkan.

---

## 6. Partner dan pihak terkait

### RYO Digital (penyelenggara)
- Situs korporat ryodigital.com: "closely held investment group incorporated in Hong Kong" (China Hong Kong City, 33 Canton Road, Tsim Sha Tsui). Kontak support@ryodigital.com. Narasi publik: "Japan-based Web3 infrastructure company".
- Ekosistem: **RYO Coin** (ERC-20 di Ethereum, sedang bangun chain sendiri; listing XT, LBank, MEXC, DigiFinex, Bitrue; KYC Bronze CertiK), **LIFE Wallet** (App Store 30 Sep 2025), **Global Mall** (e-commerce terima RYO/BTC/ETH/kartu), **Crypto ATM Network** (beta, Jepang, target 300 lokasi 2026), **RYOPAY** (stablecoin untuk grain trade, partner GRNX Global 10 Jun 2025), **RYO-CHAN**.
- Sosial: X @ryodigital, Telegram t.me/OfficialRyoDigital, Discord "RYO Digital" (guild id 1135579714430455868, vanity `ryo-global-1135579714430455868`), Instagram @ryodigital, YouTube @RYO_Digital, LinkedIn ryo-digital.
- Milestone 2025 (Chainwire 26 Jan 2026): partnership BLI 1 Mar 2025, sponsor BLI Global Summit 21 Mar dan 23 Okt 2025, listing Bitrue 15 Okt 2025, Forbes Digital Assets Mar 2025.

### Blockchain Legal Institute (BLI) — partner akademik
- Blockchain Legal Institute Foundation: 501(c)(3) nonprofit Maryland, "bukan law firm". Misi: kejelasan hukum blockchain, compliant innovation, edukasi, harmonisasi regulasi global. Program: Technology Accelerator, Global Legal & RegTech Hackathon di DoraHacks (LegalHack 2025: 320+ peserta, 91 submission; BLI Legal Tech Hackathon 2, deadline 1 Nov 2026, bounty $50K+, sponsor Chainlink Labs, MDBA, ICP, Constellation, Story, Cogent Law), youth education, summit, riset legislasi 50 negara bagian + 100 negara.
- RYO tercantum sebagai "2025 Global Strategic Partner" di bli.tools dan inisiatif "Japan Rival x Ryo x BLI" (cultural & environmental preservation) di bli.foundation.
- Peran di hackathon ini: payung "academic, university and continuing-education outreach". Implikasi praktis: judging menekankan responsible development, provenance data, dan keamanan; framing edukatif.
- Link: https://bli.foundation, https://bli.tools, https://bli.foundation/hackathon/, https://dorahacks.io/hackathon/1904/detail, hello@bli.tools.

### Tavily (rekomendasi, bukan partner resmi)
Free Researcher plan 1.000 kredit/bulan tanpa kartu, endpoint Search/Extract/Research/Crawl/Map. https://www.tavily.com/pricing

### Infrastruktur platform (terlihat dari readiness API)
Supabase, Redis, CoinMarketCap sebagai sumber harga ("real CMC data only" untuk OHLCV), signer. Berguna untuk memahami dari mana angka MCP berasal.

---

## 7. Semua tautan

| Kategori | URL | Status |
|---|---|---|
| Landing hackathon | https://ryobuild.com | OK (SPA React) |
| Builder page (overview, tracks, judging, FAQ) | https://ryobuild.com/hackathon | OK |
| MCP Builder Guide (MD) | https://ryobuild.com/MCP-Builder-Guide.md | OK, disalin ke `docs/` |
| MCP Builder Guide (PDF) | https://ryobuild.com/MCP-Builder-Guide.pdf | RUSAK (HTML SPA) |
| Project Submission Form | https://ryobuild.com/project-submission-form.pdf | RUSAK (HTML SPA) |
| Platform RYO-CHAN | https://app-ryochan.com | OK |
| Hackathon config API (publik) | https://app-ryochan.com/api/hackathon/config | OK |
| MCP health (publik) | https://app-ryochan.com/api/mcp/health | OK |
| MCP endpoint | https://app-ryochan.com/api/mcp | butuh key |
| OpenAPI platform | https://app-ryochan.com/api/openapi.json | OK; Swagger UI di /api/docs |
| System status | https://app-ryochan.com/api/system/status | OK |
| Discord "Register" | https://discord.gg/6bK76HcyV | server RYO Digital, invite **expire 9 Sep 2026** |
| Discord "Join" | https://discord.gg/Pqd3gMd7KF | server RYO Digital, tidak expire |
| Discord Help Desk | https://discord.gg/qkWPjxzxtC | server RYO Digital |
| Discord vanity | https://discord.com/invite/ryo-global-1135579714430455868 | OK |
| Telegram builder chat (topic #Hackathon) | https://t.me/OfficialRyoDigital/176291 | OK |
| X | https://x.com/ryodigital | OK |
| Situs RYO-CHAN (token) | https://ryochan.com | OK |
| GitBook RYO-CHAN | https://ryo-chan.gitbook.io/ryo-chan (index: /llms.txt) | OK; halaman `hackthon/faq` kosong |
| Video launch | https://www.youtube.com/watch?v=g9-D88cpNDg | OK |
| Facebook launch | https://www.facebook.com/RyoDigital/videos/1774533690256045/ | tidak dicek |
| RYO Coin | https://ryocoin.com, https://ryocoin.gitbook.io/ryocoin/faq | OK |
| RYO x BLI | https://ryocoin.com/blockchain-legal-institute/ | halaman ada, konten kosong (JS) |
| RYO Digital corp | https://ryodigital.com | OK |
| BLI | https://bli.foundation, https://bli.tools | OK |
| Listing agregator | https://hacklist.io | OK |
| Tavily | https://www.tavily.com/pricing | OK |

---

## 8. Implikasi strategis untuk kita

1. **Track 1 adalah pool terbesar dan headline**, tapi paling ramai. Track 3 punya bar masuk paling rendah secara teknis dan hadiah 1st ($2.000) sama dengan Track 2. Kombinasi Track 3 skill + Track 2 UI untuk mengonfigurasinya adalah contoh resmi yang disebut organiser.
2. **40 poin umum murah didapat kalau disengaja**: gabungkan minimal 2–3 tool per keputusan, simpan reasoning trail, dan tangani `partial`/`unavailable`/429 dengan tampilan "data tidak tersedia" bukan angka nol. Ini juga syarat anti-diskualifikasi.
3. **Butuh MCP key dari organiser** sebelum bisa memanggil tool. Register Discord dulu, minta key di Help Desk. Sambil menunggu, bangun terhadap kontrak respons di guide (status/data_mode/availability) dan mock yang **jelas berlabel simulated** (`data_mode: "simulated"`), jangan pernah tampilkan sebagai live.
4. **Sisa waktu ~4,5 hari.** Prioritaskan produk kecil yang lengkap: README, `.env.example`, video demo, post X, form. Hindari fitur yang butuh `check_safety` atau wallet data.
5. **Grand Prize dinilai dari kualitas kode dan stabilitas**, bukan ide. Test, error handling, dan arsitektur yang bersih bernilai nyata.
6. Konfirmasi ke Help Desk: (a) deadline final (8 Sep vs 31 Agu di API), (b) link PDF form yang rusak, (c) apakah key MCP sudah diberikan ke semua yang register.
