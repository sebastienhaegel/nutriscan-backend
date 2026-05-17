from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import os
import json
import traceback

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnalyzeRequest(BaseModel):
    image_base64: str
    age: int
    gender: str
    weight: int
    goal: str

@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        
        prompt = f"""Tu es un expert en nutrition. Analyse la photo de ce repas et réponds UNIQUEMENT en JSON valide (sans backticks, sans markdown).

Profil : {req.gender}, {req.age} ans, {req.weight} kg, objectif: {req.goal}.

Retourne exactement ce format JSON :
{{
  "nom": "Nom du plat identifié",
  "description": "Description courte (1-2 phrases)",
  "score": 72,
  "verdict": "Titre du bilan",
  "commentaire": "Commentaire personnalisé (2-3 phrases)",
  "nutrients": [
    {{ "nom": "Protéines", "pct": 65, "niveau": "medium" }},
    {{ "nom": "Glucides",  "pct": 85, "niveau": "good"   }},
    {{ "nom": "Lipides",   "pct": 45, "niveau": "low"    }},
    {{ "nom": "Fibres",    "pct": 30, "niveau": "low"    }},
    {{ "nom": "Vitamines", "pct": 70, "niveau": "medium" }},
    {{ "nom": "Minéraux",  "pct": 55, "niveau": "medium" }}
  ],
  "conseils": ["Conseil 1", "Conseil 2", "Conseil 3"]
}}"""

        response = client.messages.create(
        model="claude-sonnet-4-5",
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": req.image_base64
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )
        
        raw = response.content[0].text
        clean = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)
        return result

    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"ERREUR DÉTAILLÉE: {error_detail}")
        raise HTTPException(status_code=500, detail=str(e))
@app.get("/check")
def check():
    key = os.environ.get("ANTHROPIC_API_KEY", "NON TROUVÉE")
    return {
        "key_found": key != "NON TROUVÉE",
        "key_start": key[:10] if key != "NON TROUVÉE" else "NON TROUVÉE"
    }
@app.get("/health")
def health():
    return {"status": "ok"}
