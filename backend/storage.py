"""storage.py - keep the original PDF in Azure Blob, one folder per user: users/<user_id>/<file>.pdf"""
import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv(override=True)


def upload_pdf(user_id: str, filename: str, data: bytes) -> Optional[str]:
    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        return None  # no storage configured (local testing)
    from azure.storage.blob import ContainerClient
    safe = os.path.basename(filename).replace(" ", "_")
    name = f"users/{user_id}/{safe}"
    container = ContainerClient.from_connection_string(conn, os.getenv("AZURE_STORAGE_CONTAINER", "pharmastudy"))
    container.upload_blob(name, data, overwrite=True)
    return name
