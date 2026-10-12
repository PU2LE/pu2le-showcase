# Copyright (c) 2026 Piero Palmisano — Entrepreneur Individuel, SIREN 999620214.
# Tutti i diritti riservati. Codice sorgente proprietario di PU2LE: ne sono vietate
# la riproduzione, la distribuzione, la modifica o l'uso non autorizzato. Vedi LICENSE.

# Verifica la logica di ripiego GPU degli script che girano dentro Blender sui worker
# (force_render*.py) con un "bpy" finto: nessun Blender necessario. Copre i casi reali
# visti/temuti -- OptiX rotto ma CUDA sano, PC sano, tutto rotto -> CPU -- e soprattutto
# che i backend di produttori NON presenti non vengano mai interrogati.

import importlib.util
import os
import sys
import tempfile
import time
import types
from types import SimpleNamespace

import pytest

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIPI_VALIDI = {"OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"}


class FakePrefs:
    def __init__(self, dispositivi_per_tipo):
        self._per_tipo = dispositivi_per_tipo
        self.devices = []
        self._tipo = None
        self.sondati = []   # ogni get_devices() registra per quale backend e' stato chiamato

    @property
    def compute_device_type(self):
        return self._tipo

    @compute_device_type.setter
    def compute_device_type(self, valore):
        if valore not in TIPI_VALIDI:
            raise TypeError(valore)
        self._tipo = valore

    def get_devices(self):
        self.sondati.append(self._tipo)
        for d in self._per_tipo.get(self._tipo, []):
            if d not in self.devices:
                self.devices.append(d)


def _dev(nome, tipo):
    return SimpleNamespace(name=nome, type=tipo, use=False)


def _carica(nome_file, prefs, backend_rotti=(), estrai_prefisso=False, cpu_rotta=False):
    """Carica lo script con un bpy finto. Ritorna (modulo, scene, chiamate_render)."""
    scene = SimpleNamespace(render=SimpleNamespace(engine="CYCLES"), cycles=SimpleNamespace(device="CPU"))
    chiamate = []

    def render(**kw):
        chiamate.append((prefs.compute_device_type, scene.cycles.device))
        if scene.cycles.device == "GPU" and prefs.compute_device_type in backend_rotti:
            raise RuntimeError("Error: Failed to load OptiX kernel (OPTIX_ERROR_INTERNAL_COMPILER_ERROR)")
        if scene.cycles.device == "CPU" and cpu_rotta:
            raise RuntimeError("scena non renderizzabile")

    bpy = types.ModuleType("bpy")
    bpy.context = SimpleNamespace(
        preferences=SimpleNamespace(addons={"cycles": SimpleNamespace(preferences=prefs)}), scene=scene)
    bpy.data = SimpleNamespace(images=[])
    bpy.ops = SimpleNamespace(render=SimpleNamespace(render=render))
    sys.modules["bpy"] = bpy
    percorso = os.path.join(RADICE, nome_file)
    sorgente = open(percorso, encoding="utf-8").read()
    vecchio_argv = sys.argv
    sys.argv = ["blender", "--", "comando.json", "stato.json"]
    try:
        if estrai_prefisso:
            # force_render_persistente.py dopo le funzioni entra in un ciclo infinito:
            # si esegue solo la parte con le definizioni.
            sorgente = sorgente[: sorgente.index("scene = bpy.context.scene\n_ricarica_texture_esterne()")]
        mod = types.ModuleType("script_sotto_test")
        exec(compile(sorgente, percorso, "exec"), mod.__dict__)
    finally:
        sys.argv = vecchio_argv
    mod._log_gpu = lambda msg: None
    # Isola il marker "backend rotti" in una cartella temporanea dedicata a questa
    # chiamata: senza questo, i test scriverebbero davvero in ~/Documents/PU2LE sulla
    # macchina che esegue i test, e un test sporcherebbe quello successivo.
    if hasattr(mod, "_MARKER_BACKEND_ROTTI"):
        mod._MARKER_BACKEND_ROTTI = os.path.join(tempfile.mkdtemp(), "pu2le_backend_rotto.json")
    return mod, scene, chiamate


SCRIPT_UNO_SHOT = ["force_render.py", "force_render_animazione.py"]


@pytest.fixture(autouse=True)
def _simula_windows(monkeypatch):
    # Gli script scelgono i backend in base al sistema operativo (su Mac solo Metal):
    # senza questo i test dipenderebbero da dove girano. Il test del Mac lo sovrascrive.
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_optix_rotto_ripiega_su_cuda_senza_toccare_altri_produttori(script, monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")], "CUDA": [_dev("RTX 3080", "CUDA")],
                       "HIP": [_dev("Radeon", "HIP")], "ONEAPI": [_dev("Arc", "ONEAPI")]})
    mod, scene, chiamate = _carica(script, prefs, backend_rotti={"OPTIX"})
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    esito = mod._render_con_fallback(scene, lambda: sys.modules["bpy"].ops.render.render())
    assert esito == (True, "RTX 3080")
    assert chiamate[-1] == ("CUDA", "GPU")
    assert "HIP" not in prefs.sondati and "ONEAPI" not in prefs.sondati


@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_pc_sano_usa_solo_il_primo_backend(script, monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")], "CUDA": [_dev("RTX 3080", "CUDA")]})
    mod, scene, chiamate = _carica(script, prefs)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    assert mod._render_con_fallback(scene, lambda: sys.modules["bpy"].ops.render.render()) == (True, "RTX 3080")
    assert len(chiamate) == 1
    assert set(prefs.sondati) == {"OPTIX"}   # CUDA non e' mai stato interrogato


@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_tutto_rotto_ripiega_sulla_cpu(script, monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")], "CUDA": [_dev("RTX 3080", "CUDA")]})
    mod, scene, chiamate = _carica(script, prefs, backend_rotti={"OPTIX", "CUDA"})
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    assert mod._render_con_fallback(scene, lambda: sys.modules["bpy"].ops.render.render()) == (False, None)
    assert scene.cycles.device == "CPU" and chiamate[-1] == ("CUDA", "CPU")
    assert "HIP" not in prefs.sondati and "ONEAPI" not in prefs.sondati


@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_produttore_sconosciuto_prova_i_backend_uno_alla_volta(script, monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")]})
    mod, scene, chiamate = _carica(script, prefs)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: set())
    assert mod._render_con_fallback(scene, lambda: sys.modules["bpy"].ops.render.render()) == (True, "RTX 3080")
    assert set(prefs.sondati) == {"OPTIX"}   # ricerca completa ma lazy: si ferma al primo che funziona


@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_nessuna_gpu_va_su_cpu(script, monkeypatch):
    prefs = FakePrefs({})
    mod, scene, chiamate = _carica(script, prefs)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: set())
    assert mod._render_con_fallback(scene, lambda: sys.modules["bpy"].ops.render.render()) == (False, None)
    assert scene.cycles.device == "CPU"


def test_portatile_ibrido_nvidia_e_intel_prova_prima_nvidia(monkeypatch):
    prefs = FakePrefs({})
    mod, scene, _ = _carica("force_render.py", prefs)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"INTEL", "NVIDIA"})
    assert mod._backend_da_provare() == ["OPTIX", "CUDA", "ONEAPI"]


def test_amd_prova_solo_hip(monkeypatch):
    mod, _, _ = _carica("force_render.py", FakePrefs({}))
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"AMD"})
    assert mod._backend_da_provare() == ["HIP"]


def test_mac_usa_solo_metal(monkeypatch):
    mod, _, _ = _carica("force_render.py", FakePrefs({}))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert mod._backend_da_provare() == ["METAL"]


def test_riconoscimento_produttore_da_testo():
    mod, _, _ = _carica("force_render.py", FakePrefs({}))
    assert mod._produttore_da_testo(r"PCI\VEN_10DE&DEV_2206&SUBSYS_1234") == "NVIDIA"
    assert mod._produttore_da_testo(r"PCI\VEN_1002&DEV_73BF") == "AMD"
    assert mod._produttore_da_testo("Intel(R) UHD Graphics 630") == "INTEL"
    assert mod._produttore_da_testo("NVIDIA GeForce RTX 3080 Laptop GPU") == "NVIDIA"
    assert mod._produttore_da_testo("Microsoft Basic Display Adapter") is None


def test_sessione_persistente_ricorda_il_backend_che_funziona(monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")], "CUDA": [_dev("RTX 3080", "CUDA")]})
    mod, scene, chiamate = _carica("force_render_persistente.py", prefs, backend_rotti={"OPTIX"}, estrai_prefisso=True)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    azione = lambda: sys.modules["bpy"].ops.render.render()
    assert mod._render_con_fallback(scene, azione) == (True, "RTX 3080")
    prefs.sondati.clear()
    assert mod._render_con_fallback(scene, azione) == (True, "RTX 3080")
    assert prefs.sondati[0] == "CUDA" and "OPTIX" not in prefs.sondati   # niente piu' OptiX rotto


def test_sessione_persistente_dopo_ripiego_sulla_cpu_non_riprova_la_gpu(monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")], "CUDA": [_dev("RTX 3080", "CUDA")]})
    mod, scene, chiamate = _carica("force_render_persistente.py", prefs, backend_rotti={"OPTIX", "CUDA"}, estrai_prefisso=True)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    azione = lambda: sys.modules["bpy"].ops.render.render()
    assert mod._render_con_fallback(scene, azione) == (False, None)
    prefs.sondati.clear()
    n_prima = len(chiamate)
    assert mod._render_con_fallback(scene, azione) == (False, None)
    assert prefs.sondati == [] and len(chiamate) == n_prima + 1   # un solo render, diretto su CPU


@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_stato_gpu_segnalato_prima_del_render_anche_se_tutto_fallisce(script, monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")], "CUDA": [_dev("RTX 3080", "CUDA")]})
    mod, scene, chiamate = _carica(script, prefs, backend_rotti={"OPTIX", "CUDA"}, cpu_rotta=True)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    segnalazioni = []
    with pytest.raises(RuntimeError):
        mod._render_con_fallback(scene, lambda: sys.modules["bpy"].ops.render.render(),
                                 lambda attiva, nome: segnalazioni.append((attiva, nome)))
    # la scheda e' stata segnalata al server PRIMA che il render fallisse (come prima del ripiego)
    assert segnalazioni and segnalazioni[0] == (True, "RTX 3080")


@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_scrittura_sidecar_su_file(script, tmp_path, monkeypatch):
    mod, _, _ = _carica(script, FakePrefs({}))
    destinazione = tmp_path / "stato.json"
    monkeypatch.setattr(mod, "_GPU_STATO_SIDECAR", str(destinazione))
    mod._scrivi_sidecar(True, "RTX 3080")
    import json
    assert json.loads(destinazione.read_text()) == {"gpu_attiva": True, "dispositivo": "RTX 3080"}


def test_sessione_persistente_dopo_dieci_minuti_riprova_la_gpu(monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")], "CUDA": [_dev("RTX 3080", "CUDA")]})
    mod, scene, chiamate = _carica("force_render_persistente.py", prefs, backend_rotti={"OPTIX", "CUDA"}, estrai_prefisso=True)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    azione = lambda: sys.modules["bpy"].ops.render.render()
    assert mod._render_con_fallback(scene, azione) == (False, None)
    assert mod._cpu_fino_a > 0
    mod._cpu_fino_a = 0.0          # simula il passare dei 10 minuti
    prefs.sondati.clear()
    mod._render_con_fallback(scene, azione)
    assert prefs.sondati            # ha ritentato la GPU invece di restare sulla CPU per sempre


@pytest.mark.parametrize("vendor_ids, attesi", [
    (["0x8086"], ["ONEAPI"]),                       # Intel integrata
    (["0x8086", "0x10de"], ["OPTIX", "CUDA", "ONEAPI"]),   # portatile ibrido Intel + NVIDIA
    (["0x1002"], ["HIP"]),                          # AMD
    (["0x1af4"], ["OPTIX", "CUDA", "HIP", "ONEAPI"]),      # scheda virtuale: non riconosciuta -> ricerca completa
])
def test_riconoscimento_su_linux_da_sys_class_drm(vendor_ids, attesi, tmp_path, monkeypatch):
    mod, _, _ = _carica("force_render.py", FakePrefs({}))
    file_vendor = []
    for i, v in enumerate(vendor_ids):
        cartella = tmp_path / f"card{i}" / "device"
        cartella.mkdir(parents=True)
        (cartella / "vendor").write_text(v + "\n")
        file_vendor.append(str(cartella / "vendor"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(mod.glob, "glob", lambda pattern: file_vendor)
    assert mod._backend_da_provare() == attesi


# --- Marker "backend rotti di recente", persistente su disco tra un progetto e l'altro ---

@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_backend_fallito_viene_segnalato_su_disco(script, monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")], "CUDA": [_dev("RTX 3080", "CUDA")]})
    mod, scene, _ = _carica(script, prefs, backend_rotti={"OPTIX"})
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    mod._render_con_fallback(scene, lambda: sys.modules["bpy"].ops.render.render())
    assert mod._leggi_backend_da_evitare() == {"OPTIX"}


@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_progetto_nuovo_evita_subito_il_backend_gia_segnalato(script, monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")], "CUDA": [_dev("RTX 3080", "CUDA")]})
    mod, scene, chiamate = _carica(script, prefs)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    mod._segnala_backend_rotti(["OPTIX"])
    esito = mod._render_con_fallback(scene, lambda: sys.modules["bpy"].ops.render.render())
    assert esito == (True, "RTX 3080")
    assert "OPTIX" not in prefs.sondati   # mai nemmeno interrogato: evitato subito
    assert chiamate == [("CUDA", "GPU")]  # va dritto su CUDA, nessun tentativo sprecato


@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_marker_scaduto_viene_ignorato(script, monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")]})
    mod, scene, _ = _carica(script, prefs)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    with open(mod._MARKER_BACKEND_ROTTI, "w") as f:
        import json
        json.dump({"tipi": ["OPTIX"], "scaduto_il": time.time() - 1}, f)
    assert mod._leggi_backend_da_evitare() == set()
    assert mod._render_con_fallback(scene, lambda: sys.modules["bpy"].ops.render.render()) == (True, "RTX 3080")
    assert "OPTIX" in prefs.sondati   # scaduto: riprovato normalmente


@pytest.mark.parametrize("script", SCRIPT_UNO_SHOT)
def test_marker_illeggibile_non_rompe_nulla(script, monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")]})
    mod, scene, _ = _carica(script, prefs)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    with open(mod._MARKER_BACKEND_ROTTI, "w") as f:
        f.write("questo non e' json valido {{{")
    assert mod._leggi_backend_da_evitare() == set()
    assert mod._render_con_fallback(scene, lambda: sys.modules["bpy"].ops.render.render()) == (True, "RTX 3080")


def test_sessione_persistente_consulta_il_marker_solo_alla_prima_chiamata(monkeypatch):
    prefs = FakePrefs({"OPTIX": [_dev("RTX 3080", "OPTIX")], "CUDA": [_dev("RTX 3080", "CUDA")]})
    mod, scene, _ = _carica("force_render_persistente.py", prefs, estrai_prefisso=True)
    monkeypatch.setattr(mod, "_produttori_gpu_presenti", lambda: {"NVIDIA"})
    mod._segnala_backend_rotti(["OPTIX"])   # segnalato PRIMA che la sessione parta (progetto precedente)
    azione = lambda: sys.modules["bpy"].ops.render.render()

    # Prima chiamata: consulta il marker, evita subito OPTIX -> va dritto su CUDA.
    assert mod._render_con_fallback(scene, azione) == (True, "RTX 3080")
    assert "OPTIX" not in prefs.sondati

    # Un progetto successivo (fuori da questo test) potrebbe sbloccare di nuovo OPTIX
    # cancellando il marker: qui verifichiamo solo che DENTRO questa sessione il marker
    # non venga riconsultato una seconda volta (il flag e' scattato).
    prefs.sondati.clear()
    import os as _os
    _os.remove(mod._MARKER_BACKEND_ROTTI)  # anche se sparisse, non dovrebbe più leggerlo
    mod._ultimo_backend_ok = None
    mod._render_con_fallback(scene, azione)
    assert mod._marker_disco_consultato is True
