# Final 3 Ide: RYO-CHAN Hackathon (sintesis per 18 Sep 2026)

Deadline 3 Okt 23:59 JST (config bilang 23:59Z; rencanakan pakai JST yang lebih awal). Tiga ide di bawah sengaja dibuat beda sumbu:
1. **positioning_check**: gerbang evidence di dalam agent (T1).
2. **RYO Verdict Scorecard**: mengukur engine RYO dari waktu ke waktu (T2).
3. **move_base_rate**: skill probabilitas stateless (T3).

Overlap `verdict_track` di Base Rate saya pindahkan ke Scorecard, jadi tidak ada fitur yang dobel.

---

## IDE 1: positioning_check (gabungan positioning_check + derivatives_check)

**Nama:** positioning_check, gerbang derivatives-evidence untuk council Nota, sekaligus skill ke-5.

**One-liner:** Sebelum technician Nota boleh mengutip blok `derivatives` dari RYO, gerbang ini memutuskan dulu apakah angkanya benar-benar tentang token itu. Setelah itu ia menyuplai positioning per-token yang jujur: premium perp (bukan funding default), perubahan OI dalam coin terms, dan percentile long/short terhadap history token itu sendiri.

**Masalah nyata + bukti (live-verified 18 Sep):**
- `nota/council.py:82` menyuruh technician membaca `derivatives` dari `deep_analysis`. Di semua probe (scratchpad/probe/raw/13-16, raw3/0-6), `funding_rate_bps` = 0.0 dan `long_short_ratio` = null, padahal `availability.derivatives` = "available".
- OI change berulang lintas token: -10.44 (ETH, SOL, WIF, ONDO), -8.44 (SOL, PEPE), -4.02 (BTC, AERO, DGAI). Nilainya ikut bucket `liquidation_pressure`, bukan ikut token.
- Sign conflict tetap berlaku setelah dikoreksi ke coin terms. SOL di OKX +10.72% (coin) / +19.87% (USD), sementara RYO bilang -10.44%.
- Premium per-token memang beda-beda: SOL di bawah spot di dua venue (OKX -2.3 bps, HL -1.8 bps), sementara ONDO +4.9, AERO +3.9, kPEPE +7.8 di HL.
- Guide RYO (`docs/MCP-Builder-Guide.md:258-263`) cuma menyebut derivatives sebagai "optional derivatives evidence", tanpa unit, venue, atau window.
- TradeTrap (https://arxiv.org/abs/2512.02261) menunjukkan input market-intel yang korup ikut merambat ke keputusan LLM trading agent.

**User konkret:**
- Primer: council Nota. Keputusannya: boleh tidak technician mengutip derivatives RYO sebagai bukti token ini, dan apa gantinya.
- Sekunder: pemula yang membaca receipt sebelum practice trade. Contoh baris EN/JP dari template: "OI SOL naik ~11% sehari, tapi futures sedikit di bawah spot di 2 exchange, jadi taruhan naik tidak menumpuk." Dengan baris itu mereka bisa membedakan "banyak posisi dibuka" dari "sisi long crowded", lalu memutuskan mau size down atau tidak.

**Track:** T1 (lead: agent menolak evidence yang bukan tentang token ini, dan receipt mencatat alasannya). T3 sekunder, sebagai skill ke-5 di `/api/skills` dan MCP `io.github.PugarHuda/nota`.

**Kenapa RYO esensial:**
- Input-nya envelope `deep_analysis(include_perp=True)` yang sudah di-fetch Nota (`nota/evidence.py:42`): `data.derivatives.{funding_rate_bps, open_interest_change_24h_pct, long_short_ratio}`, `availability.derivatives`, `data.confluence.gates`, `as_of`, `request.symbol`.
- `monitor_market_sentiment_shift` memberi konteks regime (phase, BTC funding percentile).
- Output-nya berupa putusan per field RYO: `citable`, `not_token_specific`, `sign_conflict`, atau `unverified`, dan diisi pakai nama field RYO sendiri.
- Tidak memakai kuota RYO tambahan, jadi aman di bawah limit fanout 6/min.
- Tanpa RYO tidak ada yang perlu digerbangi. OKX dan HL hanya jadi referensi.

**Sumber eksternal (keyless, verified 18 Sep):**
- OKX `/api/v5/public/funding-rate?instId=SOL-USDT-SWAP`. Field `premium` dan `interestRate` dipakai, `fundingRate` tidak.
- OKX `/api/v5/rubik/stat/contracts/open-interest-history?instId=…&period=1H&limit=25`, pakai field `oiCcy` (coin terms).
- OKX `/api/v5/rubik/stat/contracts/long-short-account-ratio-contract?…&period=1H&limit=100`. 100 titik per jam cukup untuk percentile ~4 hari.
- Hyperliquid `POST /info {type: metaAndAssetCtxs}`: premium dan level OI saat ini. Tidak ada OI history; PEPE tercatat sebagai kPEPE; DGAI tidak ada.
- Deribit public untuk BTC/ETH (opsional, yang pertama dipotong kalau waktu mepet).
- Limit (dari docs publik, belum saya uji beban): OKX public ~10-20 req/2s per endpoint, rubik ~5 req/2s. Hyperliquid info ~1200 weight/min per IP. Untuk 3-5 simbol per hari jauh di bawah limit.

**Kenapa susah:**
1. Funding default bukan sinyal. OKX meng-clamp ke `interestRate` 0.0001, dan HL 0.125 bps/h itu komponen bunga. Karena itu yang dibaca premium, dengan band "at default" ±1 bp/8h.
2. OI harus dalam coin terms, karena seri USD tercampur pergerakan harga.
3. Rasio L/S altcoin secara struktural condong long, jadi hanya percentile-nya yang bermakna.
4. Jumlah venue harus jujur: kesepakatan lintas venue hanya untuk premium, sementara OI dan L/S cuma dari OKX.
5. Mapping ticker (kPEPE = 1000 PEPE, token tanpa perp mengembalikan "unavailable").
6. RYO tidak boleh dinilai "salah" terhadap definisi yang tidak terdokumentasi. Yang dipakai hanya fakta yang tidak bergantung definisi: nilai identik lintas simbol dalam satu run, atau sign berlawanan terhadap perubahan coin-terms ≥3% dalam lag 2 jam. Sisanya ditandai `unverified`.
7. Payload venue di-hash-pin supaya replay offline tetap identik.

**Hubungan dengan Nota:** extend, bukan BUIDL baru. Mengikuti pola `price_check.py` / `technicals.py`.

**Scope MVP:**
- Stage 1 (~1 hari, kerjakan dulu):
  - deteksi `not_token_specific` lintas envelope dalam satu run;
  - 1 call OKX OI per simbol untuk `sign_conflict`;
  - prompt council hanya menerima field `citable`;
  - receipt mencatat putusan dan payload yang di-hash-pin;
  - test dengan fixture 18 Sep (SOL, ETH, WIF, ONDO, DGAI).
- Stage 2 (~1.5 hari, opsional):
  - premium OKX + HL, percentile L/S;
  - consensus: `premium_above_spot` / `premium_below_spot` / `at_default` / `venues_disagree` / `unavailable`;
  - baris EN/JP dari template;
  - test bahwa funding default tidak pernah menghasilkan "crowded";
  - controlled replay: pack yang sama dijalankan dengan dan tanpa gate, kedua output dipublikasikan termasuk kalau hasilnya null.

**Rubric case:**
- T1 real thinking (25): memisahkan "posisi dibuka" dari "long crowded".
- T1 cause and effect (20): receipt berbunyi "technician tidak mengutip OI RYO karena identik lintas SOL/ETH/WIF/ONDO; pakai OKX coin-terms +10.7% (single venue)", ditambah replay terkontrol.
- T1 repeatability (10): payload di-hash-pin.
- Common combining sources: RYO + OKX + HL (+ Deribit), jumlah venue per field, tidak di-average.
- Common coping with failure: tiap kasus (unlisted, timeout, geo-block Binance/Bybit 451/403, field non-token-specific) diakhiri status eksplisit.
- T3: realistisnya belasan poin di "fills a gap". Dijual sebagai reference implementation.

**Risiko terbesar yang masih hidup:** RYO memperbaiki lane derivatives sebelum judging. Gate lalu meloloskan field sebagai `citable` (itu perilaku yang benar) dan fixture bertanggal membuktikan gate pernah menangkap masalahnya. Tapi cerita T3 "fills a gap" melemah. Sisa risiko: threshold 3% dan band ±1 bp adalah judgement call, dan baseline percentile cuma ~4 hari.

**Perubahan lewat debat:** Awalnya "ryo_evidence_integrity", BUIDL terpisah yang menilai semua kesalahan RYO. Itu di-kill karena nilainya bergantung pada bug RYO dan referensi CoinGecko sendiri stale ("Ethash" untuk ETH). Lalu dipersempit ke derivatives. Serangan kedua menangkap dua jebakan data: angka +20% itu USD, dan "crowded 2 of 2" ternyata dibangun dari funding default. Sekarang gate membaca premium dan coin-terms, dan derivatives_check dilebur ke sini.

---

## IDE 2: RYO Verdict Scorecard (+ skill verdict_track_record, menyerap verdict_track dari Base Rate)

**One-liner:** Tiap hari mengunci `deep_analysis` (verdict, confluence, 3 gate, trade_plan) untuk ~25 major yang di-map manual ke receipt yang di-hash-pin. Tiap plan di-settle di candle OKX independen (first-touch stop vs +1R pada 24h dan 72h, level di-re-anchor ke harga OKX). Lalu dipublikasikan apakah verdict RYO mengubah seberapa sering long bracket RYO sendiri berhasil, sebagai kontras dalam-hari yang sama dengan bootstrap per lock day.

**Masalah nyata + bukti:**
- RYO mengeluarkan plan yang bisa difalsifikasi dan bertimestamp untuk tiap token, tapi tidak pernah melaporkan hasilnya.
- Leaderboard RYO hanya meranking P&L. `LeaderboardRow` berisi `pnl_pct`, `pnl_usd`, `trades` (https://app-ryochan.com/api/openapi.json). Aturan hackathon bilang P&L jangka pendek tidak dinilai.
- Temuan live 18 Sep: DOGE berverdict "cautious" tapi tetap membawa long trade_plan. Verdict dan plan saling kontradiksi.
- Ada variasi state di set yang bisa di-settle: BTC/ETH neutral/MIXED 0.67, DOGE cautious/MIXED, SOL constructive/CONFIRMED (fixture).
- Nota sendiri: judge Brier 0.294 vs coin flip 0.25 (n=5) di https://nota-ryo.vercel.app/api/scores. KalshiBench (https://arxiv.org/abs/2512.16030) melaporkan LLM overconfident dengan ECE sampai 0.395.

**User konkret:**
- Pemula di app RYO-CHAN yang melihat "BTC neutral, MIXED, stop 77,535, target 84,283" dan harus memutuskan seberapa berat mempercayai verdict itu sebelum practice trade. Scorecard memberi jawaban jujur seperti "CONFIRMED vs MIXED di hari yang sama: +1R duluan 54% vs 47%, CI 90% -6..+19, belum bisa dibedakan (12 lock days)".
- Tim produk RYO menjelang fase implementasi 15 Okt: mendapat outcome tracking yang belum mereka punya.

**Track:** T2 (primer), T3 (verdict_track_record), Social (thread X, receipt dipilih pakai aturan tetap), Judges' Pick. Nota sendiri tetap di T1, plus fix `resolve()`.

**Kenapa RYO esensial:** RYO adalah hal yang diukur.
- Setiap receipt mem-pin `verdict.call`, `confluence.state/score`, 3 gate (momentum, market_activity, derivatives), `trade_plan` (entry, stop, targets, `atr_14_pct`, `atr_multiplier`, method), `trace_id`, plus snapshot `monitor_market_sentiment_shift`.
- `analyze_token` dicatat sebagai arm terpisah dengan vocab sendiri, tanpa mapping.
- Pengukurannya memakai mekanika RYO sendiri: bracket long 1.5-ATR yang sama untuk semua verdict. Jadi perbedaan antar-state di hari yang sama mengisolasi apa yang ditambahkan lapisan verdict.
- Settlement di OKX sengaja independen supaya RYO tidak menilai dirinya sendiri.

**Sumber eksternal:**
- OKX `/api/v5/market/history-candles` (1H, turun ke 1m untuk tie-break; history 1m sampai 2024, verified keyless dari Indonesia).
- OKX `/api/v5/market/ticker` untuk harga lock. Gap BTC 0.02%. ZCAT, PROM, H mengembalikan 51001 (tidak listed).
- Limit OKX market data publik ~20 req/2s menurut docs. Budget RYO: key 60 req/min, fanout 6/min, key kedaluwarsa 2026-12-17. `deep_analysis` makan 20-46 detik per call.

**Kenapa susah:**
1. Sampel berkorelasi dan ter-confound regime harian. Solusinya kontras dalam-hari, cluster bootstrap per lock day, dan yang ditampilkan jumlah lock day, bukan jumlah plan.
2. Dua price feed. Level di-re-anchor ±1.5×ATR dari harga OKX; gap >2% ditandai `basis mismatch` dan tidak di-settle.
3. Identitas aset. Hanya map RYO→OKX yang dicek manual (nama + mcap); contoh: G tetap unmapped sampai terverifikasi.
4. Settlement bergantung path: 1h → 1m → `ambiguous`, dan pembacaan pada timestamp horizon, bukan waktu cron.
5. Latency dan kuota: pacing ≥12 detik, tidak retry ke 429, kegagalan dicatat sebagai `RYO unavailable`.
6. Nondeterminism: receipt mem-pin apa yang dikatakan RYO saat itu juga.

**Hubungan dengan Nota:** modul Nota, bukan BUIDL terpisah. Reuse: pinning/hash, ledger Neon/SQLite, GH Actions, RyoClient, MCP entry, halaman receipt/OG, backing (sebagai tap agree/disagree opsional). Yang baru: logger, map ~25 entri, resolver OKX (dipakai juga oleh `resolve()` Nota yang sekarang membaca harga saat run, `nota/calibration.py:117-125`), kontras + bootstrap (stdlib `random`), satu section scorecard, dan skill `verdict_track_record` (yang menggantikan verdict_track milik Base Rate).

**Scope MVP:**
- 18-20 Sep: map ~25 major dan logger cron **live di production paling lambat 20 Sep**. Capture hari ini dihitung lock day 0.
- 20-24 Sep: resolver + fix `resolve()` + test assert pada candle rekaman.
- 24-28 Sep: section scorecard, urutan dari atas: plan terbuka dengan progress live dan flag verdict≠side; feed yang sudah settled; hitungan coverage (settled, ambiguous, unlisted, unavailable, mismatch); lalu kontras dengan interval.
- 28 Sep-1 Okt: skill di envelope RYO.
- 1-3 Okt: hasil dikirim ke RYO dulu, lalu thread X.
- Out of scope: leaderboard manusia, chart regime (baru 1 regime teramati), 7d sebagai headline, backfill, baseline Polymarket/Kalshi.

**Rubric case:**
- T2 easy to read (25): layar pertama tetap berguna walaupun semua hasil inconclusive.
- T2 sorted by importance (25): plan yang paling dekat settle, lalu settlement baru, lalu kontras, lalu coverage. Tidak ada yang diranking berdasarkan noise.
- T2 works for everyone (10): mobile-first, tabel teks di balik setiap bar, headline JP.
- T3: RYO belum punya outcome tracking; envelope RYO diikuti dan null tidak pernah jadi 0; skill read-only, `requires_guard=false`.
- Common: plan RYO di-settle di OKX; tiap mode gagal dihitung dan ditampilkan.

**Risiko terbesar yang masih hidup:** Headline saat judging hampir pasti "inconclusive" (12-14 lock days, 1 regime). Ditambah ini scope terbesar buat solo dev, dan setiap hari logger telat berarti sampel hilang permanen. Risiko lain: ~2/3 verdict major neutral/MIXED sehingga banyak hari tanpa kontras; job harian 10-20 menit di GH Actions rapuh; sensitivitas sponsor karena mem-publish rapor engine RYO.

**Perubahan lewat debat:** Awalnya "Called It / Proof-of-Call", leaderboard kalibrasi manusia + agent + RYO. Itu gugur karena prior art (Sanitizer, DexCheck), p yang dikarang, dan n kecil. Pivot ke rapor engine RYO. Serangan kedua memaksa: bootstrap per hari (bukan Wilson iid), re-anchor level, map manual, capture live yang menemukan kontradiksi "cautious + long plan", dan status sebagai modul Nota.

---

## IDE 3: move_base_rate (inti Nota Base Rate, verdict_track dipindah ke Ide 2)

**One-liner:** Skill stateless. Masukkan `atr_14_pct` live dari RYO, keluar probabilitas empiris yang terkalibrasi untuk move ±k·ATR dalam 1d/3d. Tabelnya di-fit dari 12 bulan candle OKX dan divalidasi out-of-sample dengan tes bahwa strategi constant-shading tidak bisa mendapat skill. Ledger Nota mendapat kolom "skill vs volatility base rate" (BSS).

**Masalah nyata + bukti:**
- RYO mengeluarkan ATR14, verdict, dan confluence, tapi tidak ada probabilitas. Enum `SkillName` di OpenAPI punya 29 nilai dan tidak ada skill probability/base-rate.
- Ledger Nota hanya raw Brier (macro 0.235, technician 0.328, narrative 0.344, judge 0.294, n=5). Tanpa referensi volatilitas, skill tidak bisa dibedakan dari pasar yang kebetulan tenang.
- Merkley et al. (https://link.springer.com/article/10.1007/s11142-024-09838-4): nilai sebuah call crypto berbalik tanda tergantung horizon (+1.83% di 1d vs -6.53% di 30d). Jadi base rate harus bergantung horizon.
- Config hackathon mengumumkan fase "module refinement and implementation" mulai 15 Okt (https://app-ryochan.com/api/hackathon/config), jadi ada jalur adopsi.

**User konkret:**
- Builder atau agent yang sudah memakai RYO (council Nota, entry lain, chat RYO-CHAN nanti). Sebelum bertindak atas "SOL cautious, ATR 4.1%", mereka bisa bertanya: seberapa mungkin move 1 ATR dalam 3 hari cuma karena kebetulan? Keputusannya: apakah call itu layak diberi size, atau cukup jadi catatan.
- Sekunder: tim RYO, sebagai proposal `SkillName`.

**Track:** T3 (primer, sebagai entry T3 Nota). Plus T1 spillover: BSS vs base rate di ledger adalah bukti real-thinking. Judges' Pick otomatis.

**Kenapa RYO esensial:**
- `analyze_token.technical_analysis.atr_14_pct` (plus `price`, `rsi_14`) dibaca saat request. Nilai ATR memilih tercile regime dan men-scale k.
- Kalau ATR RYO tidak ada, statusnya `unavailable`, tanpa fallback diam-diam.
- Diakui terang-terangan: history kalibrasinya dari OKX, karena RYO tidak punya endpoint history. Foundation claim-nya moderat, dan itu dinyatakan.

**Sumber eksternal:**
- OKX candle harian publik (keyless, ≥12 bulan, 13 simbol), hanya dipakai offline sekali untuk fit. Limit bukan masalah.
- Witkowski et al. (https://arxiv.org/abs/2101.01816): dasar tes anti-gaming.
- KalshiBench: dasar ekspektasi bahwa agent Nota awalnya akan skor negatif.

**Kenapa susah:**
1. Referensi yang tidak bisa di-game. ATR bukan sigma dan ekornya gemuk, jadi referensi yang bias memberi skill gratis ke constant shading. Tabel di-fit per (k, h, tercile ATR, arah) pada bulan 1-9 dan divalidasi di bulan 10-12. Ship gate: BSS constant shading ±0.05/±0.10 harus di dalam ±0.01 dari 0. Cell yang gagal di-mask.
2. Mapping dua sumber: gap antara `atr_14_pct` RYO dan ATR14 hasil hitung OKX harus diukur sebelum 25 Sep (Wilder vs simple smoothing?). Kalau perlu, fit map linear dan publish.
3. Block bootstrap per hari, `n_days` ditampilkan duluan.
4. Envelope RYO: null tetap null, patuh 6/min, tidak retry ke 429.

**Hubungan dengan Nota:** extend, 1 skill + 1 kolom ledger. Reuse cron `0 9 * * *`, Brier, fallback harga berlabel (a894738, f29a0c0), dan MCP registry.

**Scope MVP (~3 hari setelah verdict_track dipindah):**
1. Script kalibrasi offline menghasilkan tabel JSON yang di-hash, plus reliability plot dan hasil tes anti-gaming.
2. `move_base_rate(symbol, k, h, direction)` dalam envelope RYO: p, tercile, n fit, gap ATR RYO-OKX, warnings.
3. Kolom BSS per agent di Nota.
4. README berisi contoh call/response dan teks proposal SkillName.
5. Satu post X untuk social award.

**Rubric case:**
- T3 fills a gap (25): RYO tidak punya probabilitas; kalibrasi, plot out-of-sample, dan tes anti-gaming dipublikasikan.
- T3 follows spec (15): envelope RYO field demi field; hanya simbol, tanpa wallet address.
- T3 could we use it (20): stateless dan bisa dipanggil hari ini di MCP Nota. Dijual sebagai proposal, bukan drop-in.
- Common: sumber per bagian output dilabeli; tiap mode gagal punya status.

**Risiko terbesar yang masih hidup:** Definisi `atr_14_pct` RYO bisa berbeda dari ATR OKX. Kalau gap-nya besar dan tidak stabil, lookup-nya tidak valid, dan itu menentukan apakah ide ini hidup atau tidak. Ukur ini **dulu**, setengah hari, sebelum menulis apa pun. Risiko lain: ship gate gagal di holdout 3 bulan (cell di-mask); juri menganggap lookup table terlalu simpel; agent Nota tampil dengan skill negatif.

**Perubahan lewat debat:** Dimulai sebagai "Called It" untuk KOL eksternal, dua kali di-kill (prior art Sanitizer/DexCheck, p yang dikarang, closed-form sqrt(h) bisa di-game). Yang tersisa adalah salvage reviewer: kalibrasi empiris dengan tes anti-gaming. Di sintesis ini `verdict_track` dicabut dan digabung ke Ide 2, supaya kedua ide tidak dobel dan Ide 3 murni skill probabilitas.

---

## Ranking dan rekomendasi

Total skor tiga juri: positioning_check 212 (67/64/81), Verdict Scorecard 204 (72/72/60), Base Rate 193 (68/52/73).

1. **positioning_check.** Mulai bangun **sekarang**. Stage 1 cuma ~1 hari, memperbaiki eksposur live di `council.py:82` yang bisa ketahuan juri waktu membaca receipt, dan tidak butuh kuota RYO. Rasio manfaat/biaya terbaik.
2. **RYO Verdict Scorecard.** RYO-as-foundation paling kuat, peluang terbesar di T2 dan Judges' Pick. Tapi scope-nya ~13 hari. **Satu pengecualian urutan:** logger-nya harus live paling lambat 20 Sep, karena sampel yang hilang tidak bisa diulang. Jadi 18-19 Sep: map + logger (tanpa UI); 19-20 Sep: positioning Stage 1; lalu resolver dan UI Scorecard.
3. **move_base_rate.** Kerjakan hanya kalau cek gap ATR RYO-vs-OKX lolos dan Scorecard sudah aman (~28 Sep). Kalau waktu mepet, ini yang pertama dipotong.

Catatan jujur: total ketiganya (~2.5 + ~13 + ~3 hari) melebihi 15 hari solo. Realistisnya: positioning (penuh), Scorecard (versi ramping: 24h saja, 72h kalau sempat), dan Base Rate sebagai bonus.

---

## Ide yang di-kill atau tidak dipilih

- **Called It (KOL calibration + paid-campaign flag):** Sanitizer, DexCheck, Phanes, CallAnalyser sudah ada. Brier atas p hasil mapping itu opini builder. Deteksi koordinasi mustahil di 20 voice. Ada risiko defamasi di hukum Jepang.
- **Which Token (symbol→contract safety gate):** clone dari RYO Scam Launch Detector (mrHeinrichh) yang sudah punya CoinGecko identity + collision warning. CoinGecko `/coins/{id}.platforms` menyelesaikan identitas dalam satu call. Price matching salah metode. Gate tidak pernah menyala di SOL/BTC/ETH.
- **safety_crosscheck (salvage):** showcase AKE ternyata false positive Honeypot.is (heuristik 2.2% sell-failure; GoPlus clean; volume ~$20M/hari). RYO sudah punya TokenSafety internal. Maksimal jadi polish setengah hari di Nota.
- **ryo_evidence_integrity:** nilainya bergantung pada bug RYO yang bisa diperbaiki dalam hitungan jam, referensi CoinGecko stale, dan deteksi proxy butuh banyak call. Intinya diselamatkan jadi Ide 1.
- **derivatives_check:** tidak di-kill, tapi versi lebih lemah dari positioning_check (masih membandingkan funding default dan OI dalam USD). Dilebur ke Ide 1.
- **Nota Rounds (7-day forecast rounds):** tidak di-kill dan paling pas dengan tema SocialFi, tapi tidak dipilih. Setelah "beat RYO" dibuang, RYO tinggal jadi context pack + price resolver, dekat dengan pola "price oracle" yang diperingatkan 16 Sep. Backers masih 0, baru ~8 ronde resolve saat judging, dan satu cron yang skip membatalkan satu ronde penuh. Kalau mau sudut sosial, pakai tap agree/disagree di Scorecard.
- **Proof-of-Call "beat RYO" 24h:** verdict RYO adalah label tren 30 hari, bukan forecast 24h. Menilainya sebagai 24h tidak adil ke sponsor, dan horizon riil dengan cron harian jadi 24-48 jam.

---

## Sumber terverifikasi terpenting

1. https://app-ryochan.com/api/openapi.json: `LeaderboardRow` hanya P&L, `SkillName` enum 29 nilai, `TokenSafety` internal (401 untuk builder key). Fetch 18 Sep.
2. https://app-ryochan.com/api/hackathon/config: fase implementasi mulai 15 Okt; deadline 23:59Z.
3. https://nota-ryo.vercel.app/api/scores: judge Brier 0.294 vs 0.25, n=5; `/api/backers` = []. Fetch 18 Sep.
4. Probe RYO 18 Sep: `C:/Users/pc/AppData/Local/Temp/claude/C--Hackathons-Ryochan-Hackathon/f10ec77a-d638-4ec4-9652-d870f1fd71b0/scratchpad/probe/raw/13-16_deep_analysis_*.json` dan `raw3/0-6`. Isinya derivatives identik lintas token, funding 0.0, trade_plan selalu long.
5. https://www.okx.com/api/v5/public/funding-rate?instId=SOL-USDT-SWAP: field `premium` vs `interestRate` default. Live 18 Sep.
6. https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history?instId=SOL-USDT-SWAP&period=1H&limit=25: SOL +10.72% coin vs +19.87% USD.
7. https://www.okx.com/api/v5/market/history-candles?instId=SOL-USDT&bar=1H: keyless dari Indonesia, 1m tersedia.
8. https://api.hyperliquid.xyz/info (`metaAndAssetCtxs`): premium per aset, kPEPE, tanpa OI history.
9. `C:/Hackathons/Ryochan Hackathon/nota/council.py:82` dan `nota/calibration.py:117-125`: eksposur derivatives dan `resolve()` yang membaca harga saat run.
10. https://arxiv.org/abs/2512.16030 (KalshiBench, LLM overconfidence) dan https://arxiv.org/abs/2101.01816 (Witkowski, insentif ekstrem di leaderboard proper-score).
11. https://link.springer.com/article/10.1007/s11142-024-09838-4 (Merkley et al., nilai call crypto bergantung horizon).
12. https://arxiv.org/abs/2512.02261 (TradeTrap, error input merambat ke LLM trading agent).

Batas free-tier OKX dan Hyperliquid di atas saya ambil dari docs publik dan belum diuji beban. Budget RYO (60/min key, fanout 6/min, kedaluwarsa 2026-12-17) berasal dari catatan memory proyek.