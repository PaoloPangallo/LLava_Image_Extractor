"""
QuestMaster – Vision Pipeline 2-Phase (High-Quality)

Fase 1 → LLaVA (Vision): analisi visiva dettagliata e coerente in JSON
Fase 2 → Llama 3 (Text): espansione linguistica se la descrizione è troppo breve
"""

import os
import io
import json
import base64
import fitz
import requests
from PIL import Image
from typing import List, Dict

# ========================= CONFIG =========================
OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")
VISION_MODEL = os.getenv("VISION_MODEL", "llava:latest")   # o llava:13b se hai GPU >8 GB
TEXT_MODEL   = os.getenv("TEXT_MODEL", "llama3.2")
TIMEOUT      = int(os.getenv("TIMEOUT", "240"))

OUTDIR_IMAGES = "output_images"
OUTDIR_JSON   = "output"
OUT_JSON_PATH = os.path.join(OUTDIR_JSON, "image_analysis_highq.json")

# ================== UTIL – estrazione immagini ==================
def extract_images(pdf_path: str, outdir=OUTDIR_IMAGES) -> List[Dict]:
    os.makedirs(outdir, exist_ok=True)
    doc = fitz.open(pdf_path)
    images = []
    for i, page in enumerate(doc, start=1):
        for j, img in enumerate(page.get_images(full=True), start=1):
            xref = img[0]
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            ext = base_image.get("ext", "png")
            im = Image.open(io.BytesIO(img_bytes))
            im.thumbnail((1280, 1280))
            path = os.path.join(outdir, f"page_{i}_img_{j}.{ext}")
            im.save(path)
            images.append({"id": f"img_{i}_{j}", "page": i, "path": path})
    print(f"✅ Estratte {len(images)} immagini da {pdf_path}")
    return images

# ================== NORMALIZZA CATEGORIA ==================
def normalize_category(analysis: Dict) -> Dict:
    desc = (" " + analysis.get("descrizione", "") + " " + analysis.get("sintesi", "")).lower()
    if any(k in desc for k in ["table", "tabella", "columns", "rows", "grid"]):
        cat = "TABELLA"
    elif any(k in desc for k in ["chart", "plot", "graph", "curve", "barre", "axis", "assi"]):
        cat = "GRAFICO"
    elif any(k in desc for k in ["diagram", "flow", "schema", "structure", "block", "uml"]):
        cat = "DIAGRAMMA"
    elif any(k in desc for k in ["photo", "foto", "real", "camera", "portrait", "person"]):
        cat = "FOTO"
    elif any(k in desc for k in ["text", "document", "paragraph", "screenshot", "testo", "pagina"]):
        cat = "TESTO"
    else:
        cat = analysis.get("categoria", "ALTRO").upper()
        if cat not in {"GRAFICO","TABELLA","FOTO","DIAGRAMMA","TESTO","ALTRO"}:
            cat = "ALTRO"
    analysis["categoria"] = cat
    return analysis

# ================== FASE 1 – Vision (High-Quality) ==================
def analyze_image_ollama(path: str) -> Dict:
    with open(path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    prompt = """
Guarda attentamente l'immagine allegata e analizzala in modo professionale.
Descrivi in dettaglio cosa rappresenta (forme, colori, testo, struttura, elementi principali).

Restituisci un JSON coerente con i seguenti campi:

{
  "categoria": "<una tra [GRAFICO, TABELLA, FOTO, DIAGRAMMA, TESTO, ALTRO]>",
  "descrizione": "<descrizione accurata di almeno 4–5 frasi complete>",
  "sintesi": "<riassunto in una sola frase>"
}

Regole:
- Numeri o griglie → TABELLA
- Assi, curve, barre → GRAFICO
- Blocchi logici o frecce → DIAGRAMMA
- Fotografie → FOTO
- Testo o pagine scritte → TESTO
- Altro → ALTRO
Non dire mai che l'immagine è 'non chiara' o 'sfocata'.
Rispondi solo in JSON valido.
"""
    payload = {
        "model": VISION_MODEL,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
        "options": {
            "num_predict": 800,
            "temperature": 0.4,
            "top_p": 0.9,
            "stop": []
        }
    }

    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        raw = r.text.strip()

        text_response = ""
        for line in raw.splitlines():
            try:
                obj = json.loads(line)
                text_response += obj.get("response", "")
            except Exception:
                text_response += line

        text_response = text_response.strip()
        if not text_response.endswith("}"):
            text_response += "}"

        try:
            data = json.loads(text_response)
            categoria   = str(data.get("categoria","ALTRO")).upper()
            descrizione = str(data.get("descrizione","")).strip()
            sintesi     = str(data.get("sintesi","")).strip()
            return {"categoria": categoria, "descrizione": descrizione, "sintesi": sintesi}
        except Exception:
            return {"categoria":"ALTRO","descrizione":text_response,"sintesi":text_response[:120]}

    except Exception as e:
        print(f"⚠️ Vision errore {path}: {e}")
        return {"categoria":"ERRORE","descrizione":str(e),"sintesi":""}

# ================== FASE 2 – Arricchimento linguistico ==================
def enrich_description(short_desc: str) -> str:
    if not short_desc:
        return ""
    enrich_prompt = f"""
Espandi la seguente descrizione visiva in almeno cinque frasi ricche e coerenti.
Mantieni fedeltà all'immagine e usa uno stile chiaro e informativo.

Descrizione breve:
\"\"\"{short_desc}\"\"\"

Rispondi con solo il testo esteso, senza introduzioni.
"""
    payload = {
        "model": TEXT_MODEL,
        "prompt": enrich_prompt,
        "stream": False,
        "options": {"num_predict": 400, "temperature": 0.6, "stop": []}
    }
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=120)
        r.raise_for_status()
        raw = r.text.strip()
        text = ""
        parsed_any = False
        for line in raw.splitlines():
            try:
                obj = json.loads(line)
                text += obj.get("response", "")
                parsed_any = True
            except Exception:
                pass
        if parsed_any:
            return text.strip()
        return raw
    except Exception:
        return short_desc

# ================== PIPELINE ==================
def process_document(pdf_path: str) -> List[Dict]:
    os.makedirs(OUTDIR_JSON, exist_ok=True)
    images = extract_images(pdf_path, OUTDIR_IMAGES)
    results: List[Dict] = []

    for img in images:
        analysis = analyze_image_ollama(img["path"])
        analysis = normalize_category(analysis)

        desc = analysis.get("descrizione","")
        if len(desc.split()) < 25:  # soglia: meno di 25 parole = troppo corto
            enriched = enrich_description(desc)
            if len(enriched.split()) > len(desc.split()) + 8:
                analysis["descrizione"] = enriched

        result = {
            **img,
            "categoria": analysis.get("categoria","ALTRO"),
            "descrizione": analysis.get("descrizione",""),
            "sintesi": analysis.get("sintesi","")
        }
        results.append(result)
        print(f"📷 {img['id']} → {result['categoria']}: {result['sintesi'] or (result['descrizione'][:80]+'...')}")

    with open(OUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n✅ Analisi completata. Risultati salvati in {OUT_JSON_PATH}")
    return results

# ================== MAIN ==================
if __name__ == "__main__":
    PDF_PATH = "Tesi_Pangallo_Paolo.pdf"
    process_document(PDF_PATH)
