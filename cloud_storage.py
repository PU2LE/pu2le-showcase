# Copyright (c) 2026 Piero Palmisano — Entrepreneur Individuel, SIREN 999620214.
# Tutti i diritti riservati. Codice sorgente proprietario di PU2LE: ne sono vietate
# la riproduzione, la distribuzione, la modifica o l'uso non autorizzato. Vedi LICENSE.

import boto3
import os
from botocore.exceptions import ClientError

S3_BUCKET_NAME = "pu2le-storage"

def get_s3_client():
    # Nessuna credenziale reale di default: il client eseguibile distribuito ai worker
    # importa questo modulo (tramite main.py) ma non chiama mai davvero queste funzioni
    # in condizioni normali (i binari worker sono gia' inclusi nella build). Un
    # segnaposto qui non causa crash: upload/download_file_from_cloud catturano gia'
    # ClientError e falliscono in modo pulito invece di interrompere l'app.
    return boto3.client(
        's3',
        aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID", "placeholder"),
        aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY", "placeholder"),
        endpoint_url=os.getenv("R2_ENDPOINT_URL", "https://placeholder.r2.cloudflarestorage.com"),
        region_name="auto"  # <-- FONDAMENTALE PER CLOUDFLARE R2
    )

def upload_file_to_cloud(local_file_path: str, s3_file_key: str):
    try:
        s3_client = get_s3_client()
        s3_client.upload_file(local_file_path, S3_BUCKET_NAME, s3_file_key)
        print(f"File caricato con successo sul cloud R2: {s3_file_key}", flush=True)
        return True
    except ClientError as e:
        print(f"Errore durante l'upload su R2 di {s3_file_key}: {e}", flush=True)
        return False

def oggetto_esiste(s3_file_key: str) -> bool:
    """True se l'oggetto e' gia' presente nel bucket. Usato per non avviare un render
    prima che il .blend abbia finito di caricarsi (il plugin ora lo carica in background)."""
    try:
        get_s3_client().head_object(Bucket=S3_BUCKET_NAME, Key=s3_file_key)
        return True
    except ClientError:
        return False
    except Exception as e:
        print(f"Errore verifica esistenza {s3_file_key} su R2: {e}", flush=True)
        return False

def url_download_temporaneo(s3_file_key: str, nome_file_scaricato: str, scadenza_sec: int = 3600):
    """Genera un URL R2 temporaneo (presigned) per scaricare un file direttamente dal
    bucket, senza far passare i byte dal server (i pacchetti dell'app pesano centinaia di
    MB). `nome_file_scaricato` e' il nome con cui il browser salva il file."""
    try:
        s3_client = get_s3_client()
        return s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": S3_BUCKET_NAME,
                "Key": s3_file_key,
                "ResponseContentDisposition": f'attachment; filename="{nome_file_scaricato}"',
            },
            ExpiresIn=scadenza_sec,
        )
    except ClientError as e:
        print(f"Errore nel generare l'URL di download per {s3_file_key}: {e}", flush=True)
        return None


def download_file_from_cloud(s3_file_key: str, local_file_path: str):
    try:
        s3_client = get_s3_client()
        s3_client.download_file(S3_BUCKET_NAME, s3_file_key, local_file_path)
        print(f"File scaricato con successo dal cloud R2: {s3_file_key}", flush=True)
        return True
    except ClientError as e:
        print(f"Errore durante il download da R2 di {s3_file_key}: {e}", flush=True)
        return False