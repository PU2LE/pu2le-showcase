# Copyright (c) 2026 Piero Palmisano — Entrepreneur Individuel, SIREN 999620214.
# Tutti i diritti riservati. Codice sorgente proprietario di PU2LE: ne sono vietate
# la riproduzione, la distribuzione, la modifica o l'uso non autorizzato. Vedi LICENSE.

# Un tassello/chunk assegnato non deve tornare disponibile per un altro worker finche'
# il worker che lo tiene manda i ping (render lunghi: scene pesanti, chunk video). Torna
# disponibile solo se i ping si fermano davvero per TIMEOUT_ASSEGNAZIONE_SECONDI.

import time

import main
from database import JobModel, UserModel, WorkerModel
from main import GRIGLIA


def _ts(entry):
    return main._ts_assegnazione(entry)


def test_ts_assegnazione_tutti_i_formati():
    assert _ts({"ts": 123.0, "worker": "A"}) == 123.0
    assert _ts(123.0) == 123.0          # job vecchi: timestamp nudo
    assert _ts(None) == 0               # chiave assente
    assert _ts({}) == 0                 # dict senza ts


def test_task_disponibili_rispetta_assegnazione_fresca():
    ora = time.time()
    assegnati = {"0": {"ts": ora, "worker": "ARIEL"}}
    # tassello 0 appena assegnato -> NON disponibile; gli altri si'
    disp = main._task_disponibili(assegnati, completati=[], totale=9)
    assert 0 not in disp
    assert set(disp) == set(range(1, 9))


def test_task_disponibili_libera_dopo_timeout():
    vecchio = time.time() - main.TIMEOUT_ASSEGNAZIONE_SECONDI - 10
    assegnati = {"0": {"ts": vecchio, "worker": "ARIEL"}}
    disp = main._task_disponibili(assegnati, completati=[], totale=9)
    assert 0 in disp  # assegnazione scaduta -> di nuovo disponibile


def _utente_worker_job(db, tipo="animazione", nodo="ARIEL"):
    u = UserModel(email=f"{nodo}@t.test", hashed_password="x", email_verificata=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    secret = f"pu2le-sec-{nodo}-123"
    db.add(WorkerModel(id=nodo, owner_id=u.id, secret_token=secret))
    job = JobModel(
        job_id="job_riassegnazione",
        tipo=tipo,
        status="renderizza",
        owner_id=u.id,
        worker_autorizzati=[nodo],
        assegnati={"0": {"ts": time.time() - 9999, "worker": nodo}},  # gia' scaduta
        completati=[],
    )
    db.add(job)
    db.commit()
    return u, secret, job.job_id


def test_ping_rinnova_assegnazione(client, db_session):
    _u, secret, job_id = _utente_worker_job(db_session)

    # Prima del ping l'assegnazione e' scaduta (ts vecchissimo).
    j = db_session.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert time.time() - main._ts_assegnazione(j.assegnati["0"]) > main.TIMEOUT_ASSEGNAZIONE_SECONDI

    r = client.post(f"/ping/ARIEL", headers={"X-Worker-Secret": secret})
    assert r.status_code == 200

    db_session.expire_all()
    j = db_session.query(JobModel).filter(JobModel.job_id == job_id).first()
    # Dopo il ping il timestamp e' fresco -> il chunk 0 resta "di ARIEL".
    assert time.time() - main._ts_assegnazione(j.assegnati["0"]) < 5
    assert j.assegnati["0"]["worker"] == "ARIEL"
    assert 0 not in main._task_disponibili(
        main._normalizza_assegnati(j.assegnati), completati=[], totale=9
    )


def test_ping_non_rinnova_task_di_altri_worker(client, db_session):
    _u, secret, job_id = _utente_worker_job(db_session, nodo="ARIEL")
    # aggiungo un task tenuto da un ALTRO worker, scaduto
    j = db_session.query(JobModel).filter(JobModel.job_id == job_id).first()
    j.assegnati = {
        "0": {"ts": time.time() - 9999, "worker": "ARIEL"},
        "1": {"ts": time.time() - 9999, "worker": "ARNALDO"},
    }
    db_session.commit()

    client.post(f"/ping/ARIEL", headers={"X-Worker-Secret": secret})

    db_session.expire_all()
    j = db_session.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert time.time() - main._ts_assegnazione(j.assegnati["0"]) < 5      # rinnovato
    assert time.time() - main._ts_assegnazione(j.assegnati["1"]) > 9000   # NON toccato


def test_ping_non_rinnova_task_gia_completato(client, db_session):
    _u, secret, job_id = _utente_worker_job(db_session)
    j = db_session.query(JobModel).filter(JobModel.job_id == job_id).first()
    j.assegnati = {"0": {"ts": time.time() - 9999, "worker": "ARIEL"}}
    j.completati = [0]
    db_session.commit()

    client.post(f"/ping/ARIEL", headers={"X-Worker-Secret": secret})

    db_session.expire_all()
    j = db_session.query(JobModel).filter(JobModel.job_id == job_id).first()
    # gia' completato: niente rinnovo (non e' piu' "in lavorazione")
    assert time.time() - main._ts_assegnazione(j.assegnati["0"]) > 9000


# --------------------------------------------------------------------------------------
# ESECUZIONE SPECULATIVA: quando non ci sono task freschi e uno e' molto in ritardo,
# un worker libero ne riceve una COPIA (non blocca la fine del progetto su un PC lento).
# --------------------------------------------------------------------------------------

def test_inizio_assegnazione_tutti_i_formati():
    assert main._inizio_assegnazione({"inizio": 10.0, "ts": 99.0, "worker": "A"}) == 10.0
    assert main._inizio_assegnazione({"ts": 50.0, "worker": "A"}) == 50.0  # entry senza "inizio"
    assert main._inizio_assegnazione(50.0) == 50.0                          # formato vecchio
    assert main._inizio_assegnazione(None) == 0


class _J:
    """Job finto per testare _task_speculativo senza toccare il DB."""
    def __init__(self, avviato_il, job_id="job_spec"):
        self.avviato_il = avviato_il
        self.job_id = job_id


def _reset_antispam():
    main._speculazioni_recenti.clear()


def test_speculativo_none_se_pochi_completati():
    _reset_antispam()
    job = _J(avviato_il=time.time() - 1000)
    # 1 solo completato su 9 -> troppo pochi dati
    assert main._task_speculativo(job, "LIBERO", completati=[0],
                                  assegnati={"1": {"worker": "X", "inizio": time.time() - 999}},
                                  totale=9) is None


def test_speculativo_none_se_nessun_ritardo():
    _reset_antispam()
    ora = time.time()
    job = _J(avviato_il=ora - 100)          # media = 100/5 = 20s per task
    assegnati = {str(i): {"worker": f"W{i}", "inizio": ora - 10} for i in range(5, 9)}  # tutti freschi
    assert main._task_speculativo(job, "LIBERO", completati=[0, 1, 2, 3, 4],
                                  assegnati=assegnati, totale=9) is None


def test_speculativo_ritorna_il_task_in_ritardo():
    _reset_antispam()
    ora = time.time()
    job = _J(avviato_il=ora - 100)          # media ~20s/task; soglia ritardo = 2*20 = 40s
    assegnati = {
        "5": {"worker": "VELOCE", "inizio": ora - 10},    # fresco
        "6": {"worker": "LENTO",  "inizio": ora - 300},   # in ritardo da 5 min
    }
    assert main._task_speculativo(job, "LIBERO", completati=[0, 1, 2, 3, 4],
                                  assegnati=assegnati, totale=9) == 6


def test_speculativo_non_da_al_worker_il_suo_stesso_task():
    _reset_antispam()
    ora = time.time()
    job = _J(avviato_il=ora - 100)
    assegnati = {"6": {"worker": "LENTO", "inizio": ora - 300}}
    # LENTO chiede: non gli si ridà una copia del task che sta già facendo lui
    assert main._task_speculativo(job, "LENTO", completati=[0, 1, 2, 3, 4],
                                  assegnati=assegnati, totale=9) is None


def test_speculativo_antispam():
    _reset_antispam()
    ora = time.time()
    job = _J(avviato_il=ora - 100)
    assegnati = {"6": {"worker": "LENTO", "inizio": ora - 300}}
    a = main._task_speculativo(job, "LIB1", [0, 1, 2, 3, 4], assegnati, 9)
    b = main._task_speculativo(job, "LIB2", [0, 1, 2, 3, 4], assegnati, 9)
    assert a == 6 and b is None  # seconda richiesta ravvicinata sullo stesso task -> niente


def test_task_endpoint_speculativo_non_tocca_assegnati(client, db_session):
    _reset_antispam()
    u = UserModel(email="spec@t.test", hashed_password="x", email_verificata=True)
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    for nome in ("LENTO", "LIBERO"):
        db_session.add(WorkerModel(id=nome, owner_id=u.id, secret_token=f"pu2le-sec-{nome}"))
    ora = time.time()
    totale = GRIGLIA * GRIGLIA
    # Con GRIGLIA=5 (25 tasselli) ogni tassello non completato deve risultare assegnato,
    # altrimenti il dispatch assegna a LIBERO uno dei tanti tasselli davvero liberi
    # invece di innescare la riassegnazione speculativa che questo test vuole verificare.
    # _task_speculativo richiede anche almeno totale//3 completamenti prima di fidarsi
    # della media come stima di durata: con 25 tasselli servono almeno 8 completati.
    completati = list(range(8))
    assegnati = {
        str(i): {"worker": "LENTO", "ts": ora, "inizio": ora - 10}
        for i in range(8, totale)
    }
    assegnati["9"] = {"worker": "LENTO", "ts": ora, "inizio": ora - 300}   # in ritardo
    job = JobModel(
        job_id="job_spec_ep", tipo="immagine", status="renderizza", owner_id=u.id,
        avviato_il=ora - 100,
        worker_autorizzati=["LENTO", "LIBERO"],
        completati=completati,
        assegnati=assegnati,
    )
    db_session.add(job); db_session.commit()

    r = client.get("/task/LIBERO", headers={"X-Worker-Secret": "pu2le-sec-LIBERO"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "renderizza"
    assert body["job_id"] == 9   # la copia del task in ritardo

    db_session.expire_all()
    j = db_session.query(JobModel).filter(JobModel.job_id == "job_spec_ep").first()
    # la copia speculativa NON registra LIBERO: il task 9 resta di LENTO
    assert j.assegnati["9"]["worker"] == "LENTO"


def test_errore_non_riaccoda_task_gia_completato(client, db_session):
    u = UserModel(email="err@t.test", hashed_password="x", email_verificata=True)
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    db_session.add(WorkerModel(id="W", owner_id=u.id, secret_token="pu2le-sec-W"))
    job = JobModel(job_id="job_err", tipo="immagine", status="renderizza", owner_id=u.id,
                   completati=[3], falliti=[])
    db_session.add(job); db_session.commit()

    r = client.post("/errore/job_err/W/3", headers={"X-Worker-Secret": "pu2le-sec-W"})
    assert r.status_code == 200

    db_session.expire_all()
    j = db_session.query(JobModel).filter(JobModel.job_id == "job_err").first()
    assert 3 not in (j.falliti or [])  # gia' completato -> non torna in coda
