"""PDF extraction and token-based chunking utilities."""
import uuid
import PyPDF2
import tiktoken
import pandas as pd
import io


def extract_text_from_pdf(pdf_bytes: bytes) -> tuple[str, list]:
    """
    Extract full text and per-page text from PDF bytes.
    Returns (combined_text, [(page_number, page_text), ...])
    """
    reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
    full_text = ""
    page_texts = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        full_text += text + "\n"
        page_texts.append((i + 1, text))
    return full_text, page_texts


def count_tokens(text: str, encoding: str = "cl100k_base") -> tuple[int, list]:
    """Return (token_count, tokens) for the given text."""
    tokenizer = tiktoken.get_encoding(encoding)
    tokens = tokenizer.encode(text)
    return len(tokens), tokens


def chunk_document(text: str, page_texts: list, chunk_size: int = 10000, overlap: int = 1000) -> pd.DataFrame:
    """
    Split document text into token-based chunks with page tracking.
    Returns DataFrame with columns: ChunkText, TokenCount, StartPage, EndPage, ChunkID
    """
    tokenizer = tiktoken.get_encoding("cl100k_base")
    _, tokens = count_tokens(text)

    # Build token → page mapping
    page_token_map = []
    for page_number, page_text in page_texts:
        page_tokens = tokenizer.encode(page_text)
        page_token_map.extend([page_number] * len(page_tokens))

    chunks = []
    for start in range(0, len(tokens), chunk_size - overlap):
        end = min(start + chunk_size, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = tokenizer.decode(chunk_tokens)

        start_page = page_token_map[start] if start < len(page_token_map) else None
        end_page = (
            page_token_map[end - 1]
            if end - 1 < len(page_token_map)
            else (page_texts[-1][0] if page_texts else None)
        )

        chunks.append({
            "ChunkText": chunk_text,
            "TokenCount": len(chunk_tokens),
            "StartPage": start_page,
            "EndPage": end_page,
            "ChunkID": str(uuid.uuid4()),
        })

    return pd.DataFrame(chunks)
