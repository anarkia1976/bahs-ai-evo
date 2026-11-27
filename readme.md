# 🏀 BAHS — Basketball Automation Highlights Suite

**BAHS** è una suite modulare di script Python progettata per automatizzare l'intera produzione di video di pallacanestro (*Highlights*, *Gameday*, *Friendly Match* e formati *Custom*) a partire dai filmati sorgente inseriti nella cartella `IN/`, affiancata dall'interfaccia grafica desktop **`bahs_gui.py`**.

La pipeline gestisce in automatico la preparazione o l'unione dei video originali, l'identificazione interattiva dei timestamp, il taglio parallelo delle clip, la conversione verticale in formato **REEL (9:16)** con tracciamento della palla tramite **YOLO** e invio diretto via pipe a **FFmpeg**, la normalizzazione parallela e fusione con dissolvenze incrociate (`xfade`/`acrossfade`), l'aggiunta della traccia musicale con fade-out finale e la sovraimpressione combinata di testi animati e logo in un singolo passaggio. Le logiche comuni, la gestione risorse/GPU, i percorsi, il logging e le utilità sui file sono centralizzati in **`bahs_core.py`**.

---

## 📁 Struttura visiva del progetto

```text
BAHS/
├── 📂 IN/                                 # 🔒 [PROTETTA] Video sorgente in ingresso (.mp4, .mov, .avi, .mkv, .m4v, .webm)
├── 📂 OUT/                                # ⚙️ File intermedi (video unito, timestamps.txt, clip 1080P e clip REEL)
├── 📂 FINAL/                              # 🏆 Video assemblati e output finali (fade, music, text, logo)
├── 📂 TMP/                                # 🧹 File temporanei di elaborazione e scrittura atomica (.part.mp4)
├── 📂 LOG/                                # 📜 File di log dettagliati con timestamp di esecuzione
├── 📂 MUSIC/                              # 🔒 [PROTETTA] Tracce audio/musicali di sottofondo
├── 📂 EXTRA/                              # 🔒 [PROTETTA] Risorse aggiuntive della suite (font.ttf, logo.*, ecc.)
│   └── 📂 MODEL/                          # 🤖 Pesi del modello YOLO (es. yolo26l.pt)
│
├── 🧩 bahs_core.py                        # 🛠️ Modulo condiviso (risorse CPU/RAM/GPU, percorsi, log, nomi, timestamp)
├── 🖥️ bahs_gui.py                         # 🖥️ Interfaccia grafica desktop (pipeline, parametri, risorse, log live)
├── 🚀 01_BAHS_main-ALL.py                 # 🚀 Orchestratore CLI principale (controlli, pulizia, parametri e pipeline)
├── 🔗 02_BAHS_merge-originals-ALL.py      # 🔗 Copia o remux/concatenazione dei video originali da IN/ a OUT/
├── ⏱️ 03_BAHS_identify-timestamps-ALL.py  # ⏱️ Player interattivo OpenCV per la registrazione dei timestamp
├── ✂️ 04_BAHS_split-original-ALL.py       # ✂️ Taglio parallelo delle clip dal video unito in base ai timestamp
├── 🏀 05_BAHS_convert-clips-REEL.py       # 🏀 Tracciamento palla con YOLO, stabilizzazione e crop REEL 9:16 via pipe
├── 🎞️ 06_BAHS_merge-clips-ALL.py          # 🎞️ Normalizzazione parallela e unione clip con o senza dissolvenze
├── 🎶 07_BAHS_add-music-overlay-ALL.py    # 🎶 Mix traccia musicale di sottofondo con fade-out finale
├── 🏷️ 11_BAHS_add-text-logo-overlay-ALL.py# 🏷️ Sovraimpressione animata di testi e logo in singolo passaggio FFmpeg
├── 📄 requirements.txt                    # 📦 Dipendenze Python del progetto
└── 📄 README.md                           # 📖 Documentazione completa della suite
```

---

## 🗂️ Dettaglio delle cartelle e del modulo core

Gli script verificano e creano automaticamente tutte le cartelle necessarie all'avvio tramite `ensure_project_dirs()` in `bahs_core.py`:

* 🧩 **`bahs_core.py`**: modulo centrale importato da tutti gli script della suite. Raccoglie costanti condivise (estensioni supportate, colori ANSI, tipologie senza musica, formati ammessi), rilevamento risorse hardware (`compute_system_resources`) e GPU/encoder (`detect_gpu_and_encoder`), verifica strumenti `ffmpeg`/`ffprobe`, normalizzazione di slug/formati/nomi di step e clip REEL, inizializzazione dei logger di step (`setup_step_logger`), parsing/formattazione tempi, gestione file timestamp e pulizia sicura dei file temporanei (`cleanup_temp_paths`).
* 📥 **`IN/`** *(protetta dalla pulizia)*: contiene i video originali della partita da elaborare.
* ⚙️ **`OUT/`**: ospita i file intermedi generati dalla pipeline, tra cui il video originale unito (`{TIPO}_{FORMATO}_merged_orig.mp4`), i file dei timestamp (`{TIPO}_{FORMATO}_timestamps.txt` e il file sincronizzato `timestamps.txt`) e le singole clip ritagliate (`{TIPO}_{FORMATO}_clip_XX_...mp4` e `{TIPO}_REEL_clip_XX_...mp4`).
* 🏆 **`FINAL/`**: raccoglie i video assemblati delle varie fasi fino al render definitivo con testo e logo (`*text_logo.mp4`), scritti in modo atomico tramite `.part.mp4` e `os.replace()`.
* 🧹 **`TMP/`**: cartella di appoggio per file temporanei di elaborazione, liste di concatenazione, font copiato in sessione e file parziali `.part.mp4` (ripulita automaticamente al termine degli step).
* 📜 **`LOG/`**: salva un file `.log` per ogni esecuzione con informazioni su risorse hardware (CPU/RAM/GPU), parametri, statistiche YOLO ed eventuali errori.
* 🎵 **`MUSIC/`** *(protetta dalla pulizia)*: contiene i brani musicali utilizzati per l'overlay audio (`.mp3`, `.wav`, `.m4a`, `.aac`, `.flac`, `.ogg`).
* 🧰 **`EXTRA/`** *(protetta dalla pulizia)*: contiene risorse extra (`font.ttf` o altri `.ttf` e file immagine `logo.*`) e la sottocartella **`EXTRA/MODEL/`** (creata quando `include_model_dir=True`), dove viene caricato o scaricato il modello YOLO utilizzato per il tracking della palla.

---

## 🧩 Scopo dei singoli script e dell'interfaccia grafica

### 🧩 Modulo condiviso
* **`bahs_core.py`**: fornisce l'infrastruttura comune a tutta la suite, evitando duplicazioni tra orchestratore e step operativi (gestione console ANSI e QuickEdit su Windows, stima RAM/CPU/worker, rilevamento dispositivi CUDA e encoder `h264_nvenc` / `h264_amf` / `libx264`, creazione cartelle, logging per singolo step, lettura/sincronizzazione timestamp e rimozione file temporanei).

### 🖥️ Interfaccia grafica desktop
* **`bahs_gui.py`**: interfaccia grafica basata su `ttkbootstrap` (con fallback `tkinter` standard) e `tkinter-icons` (`MaterialIcon` di default, con fallback `Pillow`) per gestire il progetto e la pipeline senza riga di comando. Include selezione della cartella base e creazione cartelle, anteprima del logo in `EXTRA/`, caricamento/pulizia video in `IN/`, esecuzione completa o del singolo step con indicatore di stato e avanzamento step, console di output live, scheda **Parametri Avanzati** (`03`, `04`, `05`, `06`, `11`) e scheda **Risorse Personalizzate** per installare logo (con backup in `EXTRA/BACKUP/`), font e musica.

### 🚀 Orchestratore CLI
* **`01_BAHS_main-ALL.py`**: punto di ingresso principale da terminale. Verifica le cartelle, controlla `ffmpeg`/`ffprobe` e i pacchetti richiesti (`requirements.txt`), attende i video in `IN/`, propone la pulizia delle cartelle di lavoro (`OUT`, `FINAL`, `TMP` e vecchi log preservando `IN`, `EXTRA` e `MUSIC`), acquisisce i parametri dell'incontro e avvia in sequenza tutti gli step previsti. Se `04_BAHS_split-original-ALL.py` fallisce per mancanza di timestamp, propone di avviare `03_BAHS_identify-timestamps-ALL.py` e riprovare il taglio.

### 🎬 Moduli della pipeline
* 🔗 **`02_BAHS_merge-originals-ALL.py`**: copia direttamente un singolo `.mp4` da `IN/`, oppure esegue il remux / concatenazione (`-c copy` con `+faststart`) in `OUT/{TIPO}_{FORMATO}_merged_orig.mp4`.
* ⏱️ **`03_BAHS_identify-timestamps-ALL.py`**: player interattivo OpenCV per segnare i timestamp durante gli *Highlights* (`[SPAZIO]` per registrare, `[P]` pausa, `[F/R]` ±5s, `[Q]` salva ed esci). Sincronizza `OUT/timestamps.txt` quando necessario, ancora l'avanzamento al tempo reale per mantenere fluida la riproduzione (default `1.0x`, selezionabili `2x` e `4x`) e riduce l'overhead di disegno/progresso.
* ✂️ **`04_BAHS_split-original-ALL.py`**: legge i timestamp validi dal file dedicato in `OUT/` o da `OUT/timestamps.txt`, filtra quelli oltre la durata del video e taglia in parallelo (`ThreadPoolExecutor`) le clip usando `libx264` (`-crf 18`, `yuv420p`) e `aac` (`192k`) con margini `--before` e `--after` (default `3.0s` ciascuno) e scrittura atomica tramite file temporaneo in `TMP/`.
* 🏀 **`05_BAHS_convert-clips-REEL.py`**: converte le clip in formato verticale **REEL (`1080x1920`, 9:16)** usando **YOLO** (`ultralytics`, modello gestito in `EXTRA/MODEL/`, supporto multi-GPU CUDA o CPU con fallback automatico) per rilevare la palla (`sports ball` / `basketball` / classe `32`), colmare i buchi fino a `--gap-frames` (default `35`) con interpolazione/estrapolazione, stabilizzare il movimento della camera tramite filtro gaussiano, deadzone e limite di panning (`smooth_camera_track`), e inviare i frame croppati/ridimensionati direttamente via pipe `rawvideo` a **FFmpeg** (`h264_nvenc`, `h264_amf` o `libx264` con fallback automatico).
* 🎞️ **`06_BAHS_merge-clips-ALL.py`**: normalizza le clip in parallelo a `30 fps`, `yuv420p` e audio stereo `48000 Hz` (generando una traccia silenziosa `anullsrc` se la clip sorgente è priva di audio) e le unisce in `FINAL/` tramite `xfade` / `acrossfade` (`1.0s`), oppure in concatenazione diretta `-c copy` con `--no-fade`.
* 🎶 **`07_BAHS_add-music-overlay-ALL.py`**: mixa la traccia musicale di sottofondo (`MUSIC_VOLUME = 0.6`) con l'audio originale (`ORIGINAL_AUDIO_VOLUME = 0.4`, o sola musica se il video sorgente non ha audio), applicando il **fade-out finale** (`MUSIC_FADE_OUT_SEC = 1.0s`, limitato al `45%` della durata su video molto brevi). Viene saltato automaticamente per tipologie come `GAMEDAY` e `FRIENDLY MATCH` (`NO_MUSIC_TYPES`).
* 🏷️ **`11_BAHS_add-text-logo-overlay-ALL.py`**: applica in un **singolo passaggio FFmpeg** la sovraimpressione animata di prima riga, linea verde (`0x00FF66`) e seconda riga scorrevole con dissolvenza (`--duration`, default `15.0s`) insieme al logo (`logo.*` in `EXTRA/`), adattando layout, dimensioni e margini al formato `1080P` (`1920x1080`) o `REEL` (`1080x1920`).

---

## 🔄 Flussi di esecuzione della pipeline

| Modalità selezionata | Formato | Sequenza degli step eseguiti |
| :--- | :---: | :--- |
| 🏆 **HIGHLIGHTS** | `1080P` | `02` Merge → `03` Timestamps *(saltato se `OUT/timestamps.txt` contiene già timestamp validi)* → `04` Split → `06` Merge Clips → `07` Music *(con fade-out finale)* → `11` Text + Logo |
| 📱 **HIGHLIGHTS** | `REEL` | `02` Merge → `03` Timestamps *(saltato se `OUT/timestamps.txt` contiene già timestamp validi)* → `04` Split → **`05` Convert REEL (YOLO + Pipe FFmpeg)** → `06` Merge Clips → `07` Music *(con fade-out finale)* → `11` Text + Logo |
| 🏟️ **GAMEDAY / FRIENDLY MATCH** | `1080P` | `02` Merge → `11` Text + Logo *(senza musica)* |
| ✨ **CUSTOM** | `1080P` | `02` Merge → `07` Music *(con fade-out finale)* → `11` Text + Logo |

---

## 🛠️ Guida all'utilizzo

### 0️⃣ Avvio tramite Interfaccia Grafica (`bahs_gui.py`)
1. Assicurati di aver installato le dipendenze (incluso `tkinter-icons[material]`).
2. Avvia la GUI da terminale:
   ```bash
   python bahs_gui.py
   ```
3. Imposta la cartella base, carica i video in `IN/`, gestisci eventuali risorse personalizzate nella scheda **Risorse Personalizzate** (logo, font, musica) o regola i parametri nella scheda **Parametri Avanzati**, quindi avvia la pipeline completa o un singolo step.

### 1️⃣ Avvio completo tramite Orchestratore CLI (Consigliato da terminale)
1. Copia uno o più video della partita nella cartella **`IN/`**.
2. Inserisci eventuali brani in **`MUSIC/`** (se usi `HIGHLIGHTS` o `CUSTOM`) e il logo (`logo.*`) in **`EXTRA/`** (più `font.ttf` in `EXTRA/`).
3. Avvia lo script principale da terminale:
   ```bash
   python 01_BAHS_main-ALL.py
   ```
4. Segui le istruzioni a schermo per impostare:
   * **Pulizia cartelle di lavoro** (`1` Ripulisci `OUT`/`FINAL`/`TMP` e vecchi log, oppure `2` Non ripulire);
   * **Tipo incontro** (`1. HIGHLIGHTS`, `2. GAMEDAY`, `3. FRIENDLY MATCH`, `4. CUSTOM`);
   * **Seconda riga overlay** (es. `CAMPIONATO - TEAM A vs TEAM B`);
   * **Formato** (per *Highlights*: `1. 1080P` orizzontale o `2. REEL` verticale 9:16).

### 2️⃣ Esecuzione manuale dei singoli script
Ogni modulo può essere avviato anche singolarmente da riga di comando:

* 🔗 **Unione o preparazione originali**:
  ```bash
  python 02_BAHS_merge-originals-ALL.py --base-dir . --tipo HIGHLIGHTS --formato 1080P --auto
  ```
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
  python 05_BAHS_convert-clips-REEL.py --base-dir . --tipo HIGHLIGHTS --formato REEL --model yolo26l.pt --device auto --conf 0.50 --alpha 0.25 --gap-frames 35
  ```
* 🎞️ **Unione clip con transizioni**:
  ```bash
  python 06_BAHS_merge-clips-ALL.py --base-dir . --tipo HIGHLIGHTS --formato REEL --auto
  ```
  *(Aggiungi `--no-fade` per concatenare le clip normalizzate senza dissolvenze incrociate.)*
* 🎶 **Aggiunta musica con fade-out finale**:
  ```bash
  python 07_BAHS_add-music-overlay-ALL.py --base-dir . --tipo HIGHLIGHTS --formato REEL --auto
  ```
* 🏷️ **Sovraimpressione testo e logo in singolo passaggio**:
  ```bash
  python 11_BAHS_add-text-logo-overlay-ALL.py --base-dir . --tipo HIGHLIGHTS --sottotitolo "CAMPIONATO - TEAM vs TEAM" --formato REEL --duration 15.0 --auto
  ```

---

## ⚙️ Requisiti di sistema

* 🐍 **Python 3.10+** con le librerie installate tramite `pip install -r requirements.txt` (`opencv-python`, `tqdm`, `ultralytics`, `torch`, `psutil`, `ttkbootstrap`, `tkinter-icons[material]`).
* 🎥 **FFmpeg & FFprobe** installati e configurati nella variabile d'ambiente `PATH` (con eventuale supporto encoder hardware `h264_nvenc` / `h264_amf` e GPU CUDA per `ultralytics`).