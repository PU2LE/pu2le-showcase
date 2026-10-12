# Copyright (c) 2026 Piero Palmisano — Entrepreneur Individuel, SIREN 999620214.
# Tutti i diritti riservati. Codice sorgente proprietario di PU2LE: ne sono vietate
# la riproduzione, la distribuzione, la modifica o l'uso non autorizzato. Vedi LICENSE.

from PIL import Image
import os
import glob
import sys
import cloud_storage
from config_griglia import GRIGLIA as griglia

def trova_cartella_pu2le():
    user_home = os.path.expanduser("~")
    standard_path = os.path.join(user_home, "Documents", "PU2LE")
    if os.path.exists(standard_path):
        return standard_path
    for root, dirs, files in os.walk(user_home):
        if "PU2LE" in dirs:
            return os.path.join(root, "PU2LE")
    return standard_path

root_dir = trova_cartella_pu2le()
output_dir = os.path.join(root_dir, "output")
projects_dir = os.path.join(root_dir, "projects")

def ricomponi(proj_id: str):
    # Le funzioni chiamanti (main.py) decidono se e' sicuro cancellare le sorgenti
    # (tasselli, .blend) in base al fatto che questa funzione sollevi o no
    # un'eccezione: un semplice "stampa errore e ritorna" veniva scambiato per un
    # successo (nessuna eccezione = try riuscito), causando la cancellazione di tutto
    # -- tasselli sorgente COMPRESI -- anche quando la ricomposizione era in realta'
    # fallita, senza lasciare nulla da recuperare o ritentare.
    os.makedirs(output_dir, exist_ok=True)
    job_output_dir = os.path.join(output_dir, str(proj_id))

    if not os.path.exists(job_output_dir):
        raise RuntimeError(f"La cartella del job {job_output_dir} non esiste.")

    # 1. Trova il primo tassello disponibile per calcolare le dimensioni
    file_esempio = os.path.join(job_output_dir, "render_0_tile.png")
    if not os.path.exists(file_esempio):
        raise RuntimeError(f"Il file di esempio {file_esempio} non esiste.")

    with Image.open(file_esempio) as primo_tassello:
        w_tassello, h_tassello = primo_tassello.size
    
    larghezza_totale = w_tassello * griglia
    altezza_totale = h_tassello * griglia
    immagine_finale = Image.new('RGB', (larghezza_totale, altezza_totale))
    
    # 2. Ricostruzione geometrica precisa della griglia 3x3
    file_trovati = 0
    for i in range(griglia * griglia):
        percorso = os.path.join(job_output_dir, f"render_{i}_tile.png")
        if os.path.exists(percorso):
            with Image.open(percorso) as img:
                col = i % griglia
                riga = i // griglia
                
                # Inversione asse Y corretta per allineare Blender a PIL
                riga_invertita = (griglia - 1) - riga
                
                x = col * w_tassello
                y = riga_invertita * h_tassello
                
                immagine_finale.paste(img, (x, y))
            file_trovati += 1
            print(f"✅ Incollato tassello {i} (riga {riga}->{riga_invertita}, col {col}) a x={x}, y={y}", flush=True)
    
    # 3. Salvataggio finale pulito
    if file_trovati > 0:
        percorso_finale = os.path.join(output_dir, f"{proj_id}.png")
        immagine_finale.save(percorso_finale)
        print(f"🎉 SALVATAGGIO IMMAGINE FINALE COMPLETATO: {percorso_finale}", flush=True)

        # Il server puo' girare su una macchina diversa da quella di chi ha inviato il
        # progetto (es. hosted su Render): senza questo upload il risultato finale resta
        # bloccato sul filesystem del server e l'utente non lo vede mai.
        cloud_storage.upload_file_to_cloud(percorso_finale, f"output/{proj_id}_final.png")

        # --- PULIZIA LOCALE ---
        print("🧹 Avvio pulizia file temporanei locali...", flush=True)
        for f in glob.glob(os.path.join(job_output_dir, "render_*_tile.png")):
            os.remove(f)
            
        try:
            os.rmdir(job_output_dir)
        except Exception as e:
            print(f"⚠️ Impossibile rimuovere la cartella locale {proj_id}: {e}", flush=True)
            
        blend_file = os.path.join(projects_dir, f"{proj_id}.blend")
        if os.path.exists(blend_file):
            os.remove(blend_file)

        preview_file = os.path.join(projects_dir, f"preview_{proj_id}.png")
        if os.path.exists(preview_file):
            os.remove(preview_file)
    else:
        raise RuntimeError(f"Nessun tassello trovato per la ricomposizione del job {proj_id}.")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        ricomponi(sys.argv[1])
    else:
        print("❌ Errore: Nessun proj_id passato allo script di ricomposizione.", flush=True)