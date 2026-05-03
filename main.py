from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import os
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = None

@app.on_event("startup")
async def startup():
    global client
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

class AnalyzeRequest(BaseModel):
    image_base64: str
    age: int
    gender: str
    weight: int
    goal: str

@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
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

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
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
        result = json.loads(response.content[0].text)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}
