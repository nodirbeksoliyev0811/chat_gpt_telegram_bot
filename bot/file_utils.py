import io
import logging
from pypdf import PdfReader
from docx import Document as DocxDocument
import os

logger = logging.getLogger(__name__)

def extract_text(file_buffer: io.BytesIO, extension: str) -> str:
    """Faylni o'qib matnini ajratib olish (Pdf, Docx, Text, Code)"""
    text = ""
    extension = extension.lower().strip('.')
    
    try:
        if extension == "pdf":
            reader = PdfReader(file_buffer)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except:
                    pass
            
            for page in reader.pages:
                try:
                    extracted = page.extract_text()
                    if extracted:
                        text += extracted + "\n"
                except Exception as e:
                    logger.warning(f"Error extracting PDF page: {e}")

        
        elif extension in ["docx", "doc"]:
            doc = DocxDocument(file_buffer)
            for para in doc.paragraphs:
                text += para.text + "\n"
        
        # Text and Code files
        elif extension in ["txt", "py", "js", "html", "css", "json", "md", "yml", "yaml", "xml", "csv", "sh", "sql", "java", "cpp", "c", "h", "cs"]:
            try:
                # Try UTF-8 first
                text = file_buffer.getvalue().decode('utf-8')
            except UnicodeDecodeError:
                # Try latin-1 fallback
                try:
                    text = file_buffer.getvalue().decode('latin-1')
                except:
                    return None
        
        else:
            return None # Qo'llab-quvvatlanmaydigan format

        return text.strip()
        
    except Exception as e:
        logger.error(f"Error reading file {extension}: {e}")
        return None
