import os, hashlib
import pymupdf  # fallback: import fitz as pymupdf
from dotenv import load_dotenv
from azure.storage.blob import ContainerClient
from langchain_core.documents import Document

load_dotenv(override=True)

container = ContainerClient.from_connection_string(
    os.environ["AZURE_STORAGE_CONNECTION_STRING"],
    os.getenv("AZURE_STORAGE_CONTAINER", "pharmastudy"),
)

def load_pdf_docs(blob_name: str, user_id: str = "test_user") -> list[Document]:
    data = container.download_blob(blob_name).readall()      # bytes, memory mein
    doc_id = hashlib.sha256(data).hexdigest()
    docs = []
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        for i, page in enumerate(pdf, start=1):
            docs.append(Document(
                page_content=page.get_text(),
                metadata={
                    "user_id": user_id,
                    "doc_id": doc_id,
                    "source": blob_name,
                    "page": i,
                },
            ))
    return docs

names = [b.name for b in container.list_blobs() if b.name.lower().endswith(".pdf")]
print(len(names), "PDFs")

docs = load_pdf_docs(names[0])
print(len(docs), "pages")
print(docs[0].metadata)
print(docs[0].page_content[:300])