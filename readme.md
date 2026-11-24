# 🏀 BAHS — Basketball Automation Highlights Suite

**BAHS** è una suite modulare di script Python progettata per automatizzare l'intera produzione di video di pallacanestro (*Highlights*, *Gameday*, *Friendly Match* e formati *Custom*) a partire dai filmati sorgente inseriti nella cartella `IN/`.

La pipeline gestisce in automatico l'unione dei video originali, il rilevamento e il taglio delle azioni salienti, la conversione verticale in formato **REEL (9:16)** con tracciamento della palla tramite **YOLO**, la fusione con transizioni audio/video, l'aggiunta della traccia musicale, la sovraimpressione dei testi e l'applicazione del logo finale.

---

## 📁 Struttura visiva del progetto

```text
BAHS/
├── 📂 IN/                         # 🔒 [PROTETTA] Video sorgente in ingresso (.mp4, .mov, .avi, .mkv, .m4v, .webm)
├── 📂 OUT/                        # ⚙️ File intermedi (video unito, timestamps.txt, clip 1080P e clip REEL)
├── 📂 FINAL/                      # 🏆 Video assemblati e output finali (fade, music, text, logo)
├── 📂 TMP/                        # 🧹 File temporanei di elaborazione e muxing
├── 📂 LOG/                        # 📜 File di log dettagliati con timestamp di esecuzione
├── 📂 MUSIC/                      # 🔒 [PROTETTA] Tracce audio/musicali di sottofondo
├── 📂 EXTRA/                      # 🔒 [PROTETTA] Risorse aggiuntive della suite (font, loghi, ecc.)
│   └── 📂 MODEL/                  # 🤖 Pesi del modello YOLO (es. yolo26l.pt)
│
├── 🐍 01_BAHS_main-ALL.py         # 🚀 Orchestratore principale (controlli, pulizia, parametri e pipeline)
├── 🐍 02_BAHS_merge-originals-ALL.py
├── 🐍 03_BAHS_identify-timestamps-ALL.py
├── 🐍 04_BAHS_split-original-ALL.py
├── 🐍 05_BAHS_convert-clips-REEL.py
├── 🐍 06_BAHS_merge-clips-ALL.py
├── 🐍 07_BAHS_add-music-overlay-ALL.py
├── 🐍 08_BAHS_add-text-overlay-ALL.py
├── 🐍 10_BAHS_add-logo-overlay-ALL.py
├── 📄 requirements.txt            # 📦 Dipendenze Python del progetto
└── 📄 README.md                   # 📖 Documentazione completa della suite
```

---

## 🗂️ Dettaglio delle cartelle di lavoro

Gli script verificano e creano automaticamente tutte le cartelle necessarie all'avvio:

* 📥 **`IN/`** *(protetta dalla pulizia)*: contiene i video originali della partita da elaborare.
* ⚙️ **`OUT/`**: ospita i file intermedi generati dalla pipeline, tra cui il video originale unito (`*_merged_orig.mp4`), il file unificato dei timestamp (`timestamps.txt`) e le singole clip ritagliate (`1080P` e `REEL`).
* 🏆 **`FINAL/`**: raccoglie i video assemblati delle varie fasi fino al render definitivo con logo (`*logo.mp4`).
* 🧹 **`TMP/`**: cartella di appoggio per file temporanei video/audio e muxing FFmpeg (ripulita automaticamente a fine step).
* 📜 **`LOG/`**: salva un file `.log` per ogni esecuzione con informazioni su risorse hardware (CPU/RAM), parametri, statistiche YOLO ed eventuali errori.
* 🎵 **`MUSIC/`** *(protetta dalla pulizia)*: contiene i brani musicali utilizzati per l'overlay audio.
* 🧰 **`EXTRA/`** *(protetta dalla pulizia)*: contiene risorse extra (loghi, font) e la sottocartella **`EXTRA/MODEL/`**, dove viene scaricato o caricato il modello YOLO utilizzato per il tracking della palla.

---

## 🧩 Scopo dei singoli script

### 🚀 Orchestratore
* **`01_BAHS_main-ALL.py`**: punto di ingresso principale della suite. Verifica le cartelle, controlla la disponibilità di `ffmpeg`/`ffprobe` e dei pacchetti in `requirements.txt`, attende i video in `IN/`, propone la pulizia delle cartelle di lavoro (`OUT`, `FINAL`, `TMP` e vecchi log preservando `IN`, `EXTRA` e `MUSIC`), acquisisce i parametri dell'incontro e lancia in sequenza tutti gli step previsti.

### 🎬 Moduli della pipeline
* 🔗 **`02_BAHS_merge-originals-ALL.py`**: unisce i file video sorgente presenti in `IN/` in un unico filmato di lavoro in `OUT/`.
* ⏱️ **`03_BAHS_identify-timestamps-ALL.py`**: player interattivo per segnare i timestamp dei canestri/azioni durante gli *Highlights* (`[SPAZIO]` per registrare, `[P]` pausa, `[F/R]` ±5s, `[Q]` salva ed esci). Salva e sincronizza `OUT/timestamps.txt` e viene saltato automaticamente se `OUT/timestamps.txt` è già presente e valido (sia per `1080P` che per `REEL`).
* ✂️ **`04_BAHS_split-original-ALL.py`**: legge `OUT/timestamps.txt` e taglia in parallelo (multi-thread) le singole clip dal video sorgente, applicando i secondi di margine prima e dopo l'azione (`--before` e `--after`, default `3.0s`).
* 🏀 **`05_BAHS_convert-clips-REEL.py`**: trasforma le clip in formato verticale **REEL (`1080x1920`, 9:16)** utilizzando **YOLO** (`ultralytics`, modello gestito in `EXTRA/MODEL/`) per individuare la palla (`sports ball` / `basketball` / classe COCO `32`), interpolare eventuali frame senza rilevamento, stabilizzare la camera virtuale tramite filtro esponenziale (**EMA**), ritagliare/ridimensionare i fotogrammi e rimuxare l'audio con `ffmpeg`. Registra nel log le statistiche complete su modello, frame analizzati, confidenza e match rilevati.
* 🎞️ **`06_BAHS_merge-clips-ALL.py`**: normalizza le clip (`30 fps`, `yuv420p`, audio stereo `48000 Hz`) e le unisce in `FINAL/` applicando dissolvenze incrociate audio/video (`xfade` / `acrossfade` da `1.0s`, disattivabili con `--no-fade`).
* 🎶 **`07_BAHS_add-music-overlay-ALL.py`**: inserisce la traccia musicale di sottofondo (saltato automaticamente per tipologie come `GAMEDAY` e `FRIENDLY MATCH`).
* 📝 **`08_BAHS_add-text-overlay-ALL.py`**: aggiunge le scritte in sovraimpressione (1ª riga con il tipo incontro e 2ª riga con il sottotitolo) adattandole al formato `1080P` o `REEL` (con dissolvenza in uscita a 30 secondi).
* 🛡️ **`10_BAHS_add-logo-overlay-ALL.py`**: sovrappone il logo grafico generando il video finale definitivo in `FINAL/`.

---

## 🔄 Flussi di esecuzione della pipeline

| Modalità selezionata | Formato | Sequenza degli step eseguiti |
| :--- | :---: | :--- |
| 🏆 **HIGHLIGHTS** | `1080P` | `02` Merge → `03` Timestamps *(saltato se `OUT/timestamps.txt` esiste)* → `04` Split → `06` Merge Clips → `07` Music → `08` Text → `10` Logo |
| 📱 **HIGHLIGHTS** | `REEL` | `02` Merge → `03` Timestamps *(saltato se `OUT/timestamps.txt` esiste)* → `04` Split → **`05` Convert REEL (YOLO)** → `06` Merge Clips → `07` Music → `08` Text → `10` Logo |
| 🏟️ **GAMEDAY / FRIENDLY MATCH** | `1080P` | `02` Merge → `08` Text → `10` Logo *(senza musica)* |
| ✨ **CUSTOM** | `1080P` | `02` Merge → `07` Music → `08` Text → `10` Logo |

---

## 🛠️ Guida all'utilizzo

### 1️⃣ Avvio completo tramite Orchestratore (Consigliato)
1. Copia uno o più video della partita nella cartella **`IN/`**.
2. Avvia lo script principale da terminale:
   ```bash
   python 01_BAHS_main-ALL.py
   ```
3. Segui le istruzioni a schermo per impostare:
   * **Pulizia cartelle di lavoro** (`1` Ripulisci `OUT`/`FINAL`/`TMP` e vecchi log, oppure `2` Non ripulire);
   * **Tipo incontro** (`1. HIGHLIGHTS`, `2. GAMEDAY`, `3. FRIENDLY MATCH`, `4. CUSTOM`);
   * **Seconda riga overlay** (es. `CAMPIONATO - TEAM A vs TEAM B`);
   * **Formato** (per *Highlights*: `1. 1080P` orizzontale o `2. REEL` verticale 9:16).

### 2️⃣ Esecuzione manuale dei singoli script
Ogni modulo può essere avviato anche singolarmente da riga di comando:

* ⏱️ **Identificazione timestamp**:
  ```bash
  python 03_BAHS_identify-timestamps-ALL.py --base-dir . --tipo HIGHLIGHTS --formato 1080P
  ```
* ✂️ **Taglio clip da timestamp**:
  ```bash
  python 04_BAHS_split-original-ALL.py --base-dir . --tipo HIGHLIGHTS --formato REEL --before 3.0 --after 3.0 --auto
  ```
* 🏀 **Conversione clip in REEL con tracciamento YOLO**:
  ```bash
  python 05_BAHS_convert-clips-REEL.py --base-dir . --tipo HIGHLIGHTS --formato REEL --model yolo26l.pt --conf 0.50 --alpha 0.12 --gap-frames 15
  ```
* 🎞️ **Unione clip con transizioni**:
  ```bash
  python 06_BAHS_merge-clips-ALL.py --base-dir . --tipo HIGHLIGHTS --formato REEL --auto
  ```
  *(Aggiungi `--no-fade` per concatenare le clip senza dissolvenze incrociate.)*

---

## ⚙️ Requisiti di sistema

* 🐍 **Python 3.10+** con le librerie installate tramite `pip install -r requirements.txt` (`opencv-python`, `tqdm`, `ultralytics`, `psutil`, `numpy`, `pillow`).
* 🎥 **FFmpeg & FFprobe** installati e configurati nella variabile d'ambiente `PATH`.