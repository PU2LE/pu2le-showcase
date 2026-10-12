# --- Excerpt from PU2LE's production backend (main.py) ---
#
# This is the task-dispatch core of PU2LE, a distributed Blender render-farm
# coordinator: it decides which image tile / animation chunk each worker PC
# gets next, how slow "straggler" workers get helped via speculative
# re-execution, and how heavier tiles get routed to GPU-capable workers.
#
# This is a direct, unmodified excerpt (lines 2277-2598 of the real main.py,
# ~3,100 lines total) -- not a rewritten demo. It's included here as-is,
# which is why it isn't meant to run standalone: it depends on symbols
# defined elsewhere in the real file (not included in this showcase repo),
# namely:
#   - `app`            : the FastAPI instance (`@app.get`/`@app.post` routes)
#   - `JobModel`, `StoricoTasselloModel`, `StimaComplessitaModel`,
#     `StatoGpuWorkerModel` : SQLAlchemy ORM models (see database.py)
#   - `get_db`          : FastAPI dependency yielding a SQLAlchemy session
#   - `verifica_worker_secret` : authenticates a worker via a per-account secret
#   - `_totale_task`    : returns how many tiles/chunks a given job expects
#   - `GRIGLIA`, `NUM_CHUNK_ANIMAZIONE` : grid size constants (see config_griglia.py,
#     also included in this repo)
#   - `Image`, `ImageStat` : from Pillow
#
# See README.md for the full architecture and why this slice was chosen.

# un task il cui timestamp non viene rinnovato da piu' di TIMEOUT_ASSEGNAZIONE_SECONDI
# torna disponibile per essere riassegnato (a se stesso o a un altro worker).
#
# Il timestamp viene rinnovato a ogni /ping del worker che tiene il task (vedi ping_nodo):
# finche' il worker da' segni di vita, il SUO tassello/chunk NON deve tornare disponibile
# per un altro, anche se il render dura piu' di 5 minuti (scene pesanti, chunk video
# lunghi). Senza il rinnovo, un lavoro lungo veniva ri-assegnato a meta' e renderizzato
# due volte -- il lavoro di uno dei due worker buttato.
TIMEOUT_ASSEGNAZIONE_SECONDI = 300

def _normalizza_assegnati(raw):
    # Job vecchi: "assegnati" era una lista, poi {task_id: timestamp_nudo}. Ora e'
    # {task_id: {"ts": ..., "worker": ...}}. _ts_assegnazione legge tutti i formati;
    # qui basta garantire un dict.
    return raw if isinstance(raw, dict) else {}

def _ts_assegnazione(entry):
    # Ultimo segno di vita per un'assegnazione (assegnazione o ping piu' recente),
    # qualunque sia il formato: dict {"ts": ...} (nuovo) o numero nudo (job vecchi).
    # Usato per il timeout "worker morto": se non arriva un ping da 5 min il task torna
    # libero.
    if isinstance(entry, dict):
        return entry.get("ts", 0)
    return entry or 0

def _inizio_assegnazione(entry):
    # Quando il task e' stato assegnato la PRIMA volta (NON rinnovato dai ping). Usato
    # per capire se un task e' "in ritardo" rispetto a quanto stanno durando gli altri.
    if isinstance(entry, dict):
        return entry.get("inizio") or entry.get("ts", 0)
    return entry or 0

def _task_disponibili(assegnati_dict, completati, totale):
    ora = time.time()
    return [
        i for i in range(totale)
        if i not in completati and (
            str(i) not in assegnati_dict or
            ora - _ts_assegnazione(assegnati_dict.get(str(i))) > TIMEOUT_ASSEGNAZIONE_SECONDI
        )
    ]

# --- ESECUZIONE SPECULATIVA (aiuto ai task "in ritardo") ---
# Quando NON ci sono piu' task freschi (tutti assegnati a worker vivi) ma il progetto
# non e' finito, e un worker e' libero: se un task e' assegnato da molto piu' tempo di
# quanto stanno durando gli altri, ne diamo una COPIA al worker libero. Vince chi
# finisce prima (il secondo risultato viene ignorato, vedi /finito). Cosi' un solo PC
# lento non blocca l'intero progetto alla fine, e durante il lavoro normale nessuno
# duplica niente.
FATTORE_STRAGGLER = 2.0        # un task che dura > 2x la media degli altri e' "in ritardo"
RATEO_SPECULAZIONE_S = 90      # non rilanciare una copia dello stesso task prima di 90s
_speculazioni_recenti = {}     # {"job_id:task_id" -> ts} in memoria (anti-spam, non critico)

def _task_speculativo(job, nodo_str, completati, assegnati, totale):
    # Ritorna il task_id di una copia da assegnare a un worker libero, o None.
    if len(completati) < max(2, totale // 3):
        return None  # troppo pochi dati per stimare quanto dovrebbe durare un task
    if not job.avviato_il:
        return None
    ora = time.time()
    durata_media = (ora - job.avviato_il) / len(completati)
    limite_inizio = ora - FATTORE_STRAGGLER * durata_media
    candidati = []
    for chiave, entry in assegnati.items():
        if not isinstance(entry, dict):
            continue
        i = int(chiave)
        if i in completati or entry.get("worker") == nodo_str:
            continue
        inizio = _inizio_assegnazione(entry)
        if not inizio or inizio >= limite_inizio:
            continue
        if ora - _speculazioni_recenti.get(f"{job.job_id}:{i}", 0) < RATEO_SPECULAZIONE_S:
            continue
        candidati.append((inizio, i))
    if not candidati:
        return None
    candidati.sort()  # il piu' in ritardo (assegnato da piu' tempo) per primo
    task_id = candidati[0][1]
    _speculazioni_recenti[f"{job.job_id}:{task_id}"] = ora
    return task_id

# --- DISPATCH PER COMPLESSITA' (pesante alla GPU, leggero alla CPU) ---
# Non possiamo garantire che ogni worker sfrutti davvero la propria GPU (driver mancanti,
# schede integrate deboli...): invece di inseguire quel problema, bilanciamo il carico in
# base a cosa ogni worker riesce davvero a gestire. Priorita' di classificazione, dalla
# piu' affidabile alla piu' debole: storico reale di un precedente invio della STESSA
# scena (StoricoTasselloModel, chiave progetto_id) > stima dall'anteprima gia' generata
# per ogni job (StimaComplessitaModel, solo immagini) > ordine naturale (comportamento
# di sempre, quando non c'e' nessuna delle due).
def _ordina_per_complessita(disponibili, job, totale, db):
    if job.progetto_id:
        righe = db.query(StoricoTasselloModel).filter_by(
            owner_id=job.owner_id, progetto_id=job.progetto_id, tipo=job.tipo, totale_task=totale
        ).all()
        storico = {r.task_id: r.durata_secondi for r in righe}
        if len(storico) >= totale:
            return sorted(disponibili, key=lambda i: storico[i], reverse=True)
    stima = db.query(StimaComplessitaModel).filter_by(job_id=job.job_id).first()
    if stima and stima.punteggi and len(stima.punteggi) >= totale:
        punteggi = stima.punteggi
        return sorted(disponibili, key=lambda i: punteggi[i], reverse=True)
    return sorted(disponibili)

def _stima_complessita_da_anteprima(job_id, preview_local_path, db):
    """Stima quanto sara' pesante ciascun tassello analizzando l'anteprima gia'
    generata (vedi _render_anteprima in pu2le_linker.py) -- disponibile fin dal primo
    invio di un progetto, prima che esista qualunque storico reale. Proxy economico:
    la deviazione standard dei toni di grigio in ciascuna regione (una zona piatta come
    un cielo o uno sfondo uniforme converge in fretta; una con molto dettaglio/ombre no).
    Qualunque errore qui viene solo loggato -- mai un errore che blocca il caricamento
    del progetto."""
    try:
        with Image.open(preview_local_path) as anteprima:
            anteprima = anteprima.convert("RGB")
            larghezza, altezza = anteprima.size
            w_regione, h_regione = larghezza / GRIGLIA, altezza / GRIGLIA
            punteggi = [0.0] * (GRIGLIA * GRIGLIA)
            for task_id in range(GRIGLIA * GRIGLIA):
                riga, col = task_id // GRIGLIA, task_id % GRIGLIA
                # Stessa inversione asse Y di ricomponi.py: la riga Blender (0 = basso
                # dell'immagine) corrisponde alla riga PIL (GRIGLIA-1-riga) dato che PIL
                # ha origine in alto a sinistra.
                riga_pil = (GRIGLIA - 1) - riga
                box = (
                    round(col * w_regione), round(riga_pil * h_regione),
                    round((col + 1) * w_regione), round((riga_pil + 1) * h_regione),
                )
                regione = anteprima.crop(box).convert("L")
                punteggi[task_id] = ImageStat.Stat(regione).stddev[0]
        riga_esistente = db.query(StimaComplessitaModel).filter_by(job_id=job_id).first()
        if riga_esistente:
            riga_esistente.punteggi = punteggi
            riga_esistente.creato_il = time.time()
        else:
            db.add(StimaComplessitaModel(job_id=job_id, punteggi=punteggi, creato_il=time.time()))
        db.commit()
    except Exception as e:
        print(f"⚠️ Stima complessita' da anteprima fallita per {job_id} ({e}); il dispatch usera' l'ordine naturale", flush=True)

def _registra_storico_task(job, task_id, db):
    """Chiamata a completamento di un tassello/chunk: registra quanto e' durato, per
    usarlo come classifica di pesantezza al PROSSIMO reinvio della stessa scena (vedi
    _ordina_per_complessita). Solo per job con progetto_id (id di scena stabile, lo
    stesso usato dalla cache texture) -- senza non c'e' modo di ritrovare questa riga in
    un invio successivo."""
    if not job.progetto_id:
        return
    assegnati = _normalizza_assegnati(job.assegnati)
    entry = assegnati.get(str(task_id))
    if not isinstance(entry, dict) or not entry.get("inizio"):
        return
    durata = time.time() - entry["inizio"]
    totale = _totale_task(job)
    riga = db.query(StoricoTasselloModel).filter_by(
        owner_id=job.owner_id, progetto_id=job.progetto_id, tipo=job.tipo,
        totale_task=totale, task_id=task_id,
    ).first()
    if riga:
        riga.durata_secondi = durata
        riga.aggiornato_il = time.time()
    else:
        db.add(StoricoTasselloModel(
            owner_id=job.owner_id, progetto_id=job.progetto_id, tipo=job.tipo,
            totale_task=totale, task_id=task_id, durata_secondi=durata,
            aggiornato_il=time.time(),
        ))
    db.commit()

def _scegli_task(disponibili, job, totale, nodo_str, worker, db):
    """Tra i task liberi, sceglie quello piu' pesante per un worker con GPU attiva,
    quello piu' leggero altrimenti (anche quando non sappiamo ancora se ha la GPU --
    default prudente: mai il tassello piu' pesante a un worker di cui non sappiamo
    nulla)."""
    if not disponibili:
        return None
    ordinati = _ordina_per_complessita(disponibili, job, totale, db)
    stato_gpu = db.query(StatoGpuWorkerModel).filter_by(owner_id=worker.owner_id, worker_id=nodo_str).first()
    gpu_attiva = bool(stato_gpu and stato_gpu.gpu_attiva)
    return ordinati[0] if gpu_attiva else ordinati[-1]

@app.get("/task/{nodo_id}")
def get_task(nodo_id: str, x_worker_secret: Optional[str] = Header(None), db: Session = Depends(get_db)):
    worker = verifica_worker_secret(x_worker_secret, db)
    nodo_str = str(nodo_id)
    # Ordinato per job_id: il timestamp incorporato nell'ID (es. "job_1786909757256") lo rende
    # equivalente a un ordine cronologico. Fondamentale per il controllo che segue: se un worker
    # e' prenotato su piu' progetti non finiti, deve sempre lavorare prima sul piu' vecchio.
    jobs = db.query(JobModel).order_by(JobModel.job_id).all()

    # 1. CONTROLLO RIGOROSO: Cerca se il worker è autorizzato su un progetto attualmente in render non completato
    for job in jobs:
        if job.status != "renderizza":
            continue

        if nodo_str in (job.worker_autorizzati or []):
            completati = list(job.completati or [])
            totale = _totale_task(job)

            # Se questo progetto NON è completato, il worker appartiene a questo progetto e NON DEVE MAI guardare altrove
            if len(completati) < totale:
                assegnati = _normalizza_assegnati(job.assegnati)
                falliti = list(job.falliti or [])

                if job.tipo == "animazione":
                    if falliti:
                        chunk_fallito = falliti.pop(0)
                        job.falliti = falliti
                        db.commit()
                        return assegna_chunk_animazione(job, chunk_fallito, nodo_id, db)
                    chunk_rimasti = _task_disponibili(assegnati, completati, totale)
                    scelto = _scegli_task(chunk_rimasti, job, totale, nodo_str, worker, db)
                    if scelto is not None:
                        return assegna_chunk_animazione(job, scelto, nodo_id, db)
                else:
                    if falliti:
                        t_id = falliti.pop(0)
                        job.falliti = falliti
                        db.commit()
                        return assegna_tassello(job, t_id, nodo_id, db)
                    tasselli_rimasti = _task_disponibili(assegnati, completati, totale)
                    scelto = _scegli_task(tasselli_rimasti, job, totale, nodo_str, worker, db)
                    if scelto is not None:
                        return assegna_tassello(job, scelto, nodo_id, db)

                # Nessun task fresco: il progetto non e' finito ma sono tutti assegnati a
                # worker vivi. Se uno di questi e' molto in ritardo, do una COPIA di quel
                # task a questo worker libero (esecuzione speculativa) invece di lasciarlo
                # fermo -- cosi' un PC lento non blocca la fine del progetto.
                spec = _task_speculativo(job, nodo_str, completati, assegnati, totale)
                if spec is not None:
                    print(f"⚡ Speculativo: {nodo_str} riceve una copia del task {spec} in ritardo ({job.job_id})", flush=True)
                    if job.tipo == "animazione":
                        return assegna_chunk_animazione(job, spec, nodo_id, db, speculativo=True)
                    return assegna_tassello(job, spec, nodo_id, db, speculativo=True)

                # Se il progetto non è completato ma i task sono momentaneamente tutti assegnati,
                # rimaniamo bloccati qui in attesa, impedendo tassativamente di passare ad altri progetti.
                return {"status": "attesa"}

    # 2. SOLO SE IL WORKER NON HA ALCUN PROGETTO ATTIVO IN CORSO, può prendere un nuovo progetto
    for job in jobs:
        if job.status != "renderizza":
            continue
        totale = _totale_task(job)
        if len(job.completati or []) >= totale:
            continue
        if nodo_str not in (job.worker_autorizzati or []):
            continue

        assegnati = _normalizza_assegnati(job.assegnati)
        completati = list(job.completati or [])
        falliti = list(job.falliti or [])

        if job.tipo == "animazione":
            if falliti:
                chunk_fallito = falliti.pop(0)
                job.falliti = falliti
                db.commit()
                return assegna_chunk_animazione(job, chunk_fallito, nodo_id, db)
            chunk_rimasti = _task_disponibili(assegnati, completati, totale)
            scelto = _scegli_task(chunk_rimasti, job, totale, nodo_str, worker, db)
            if scelto is not None:
                return assegna_chunk_animazione(job, scelto, nodo_id, db)
        else:
            if falliti:
                t_id = falliti.pop(0)
                job.falliti = falliti
                db.commit()
                return assegna_tassello(job, t_id, nodo_id, db)
            tasselli_rimasti = _task_disponibili(assegnati, completati, totale)
            scelto = _scegli_task(tasselli_rimasti, job, totale, nodo_str, worker, db)
            if scelto is not None:
                return assegna_tassello(job, scelto, nodo_id, db)

        # Nessun task fresco su questo progetto: prova ad aiutarlo su un task in ritardo
        # (esecuzione speculativa) prima di passare al progetto successivo.
        spec = _task_speculativo(job, nodo_str, completati, assegnati, totale)
        if spec is not None:
            print(f"⚡ Speculativo: {nodo_str} riceve una copia del task {spec} in ritardo ({job.job_id})", flush=True)
            if job.tipo == "animazione":
                return assegna_chunk_animazione(job, spec, nodo_id, db, speculativo=True)
            return assegna_tassello(job, spec, nodo_id, db, speculativo=True)

    return {"status": "attesa"}

def assegna_tassello(job, task_id, nodo_id, db, speculativo=False):
    # speculativo=True: e' una COPIA di un task gia' in corso su un altro worker (worker
    # lento). NON si registra in "assegnati" -- il titolare resta quello -- si mandano
    # solo le istruzioni di render. Vince chi finisce prima (vedi /finito).
    if not speculativo:
        # Nuovo dict (non mutazione in place): cosi' SQLAlchemy rileva sempre il
        # cambiamento sulla colonna JSON. "worker" = chi tiene il task (per il rinnovo
        # su /ping); "inizio" = prima assegnazione (per capire se e' in ritardo); "ts" =
        # ultimo segno di vita (rinnovato dai ping).
        ora = time.time()
        assegnati = dict(_normalizza_assegnati(job.assegnati))
        assegnati[str(task_id)] = {"worker": str(nodo_id), "ts": ora, "inizio": ora}
        job.assegnati = assegnati
        db.commit()
    riga, col = task_id // GRIGLIA, task_id % GRIGLIA
    step = 1.0 / GRIGLIA
    coords = [col * step, (col + 1) * step, riga * step, (riga + 1) * step]
    return {"status": "renderizza", "job_id": task_id, "coords": coords, "proj_id": job.job_id}

def calcola_intervallo_chunk(start_frame, end_frame, chunk_id):
    durata = (end_frame - start_frame) + 1
    dim = durata // NUM_CHUNK_ANIMAZIONE
    resto = durata % NUM_CHUNK_ANIMAZIONE
    inizio = start_frame + chunk_id * dim + min(chunk_id, resto)
    durata_chunk = dim + (1 if chunk_id < resto else 0)
    return inizio, inizio + durata_chunk - 1

def assegna_chunk_animazione(job, chunk_id, nodo_id, db, speculativo=False):
    if not speculativo:
        ora = time.time()
        assegnati = dict(_normalizza_assegnati(job.assegnati))
        assegnati[str(chunk_id)] = {"worker": str(nodo_id), "ts": ora, "inizio": ora}
        job.assegnati = assegnati
        db.commit()
    start_f, end_f = calcola_intervallo_chunk(job.start_frame, job.end_frame, chunk_id)
    return {"status": "renderizza_chunk", "chunk_id": chunk_id, "start_frame": start_f, "end_frame": end_f, "proj_id": job.job_id}
